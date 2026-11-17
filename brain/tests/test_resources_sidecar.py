"""The resource sidecar: reading processes and GPUs, its config and per-client tokens, and what it serves."""

from __future__ import annotations

import os
import threading
import urllib.error

import pytest

from status_page import resources
from status_page import resources_agent as agent
from status_page import resources_collect as collect

GPU_TABLE = """\
+-----------------------------------------------------------------------------------------+
| Processes:                                                                              |
|  GPU   GI   CI              PID   Type   Process name                        GPU Memory |
|        ID   ID                                                               Usage      |
|=========================================================================================|
|    0   N/A  N/A            2425      G   .../build/deskmate_overlay                 25MiB |
|    0   N/A  N/A            2446      G   /usr/bin/kwin_wayland                      30MiB |
|    0   N/A  N/A           12711    C+G   ...rack-uuid=3190708988185955192          102MiB |
+-----------------------------------------------------------------------------------------+
"""
XPU_STATS = """\
+-----------------------------+------------------------------+
| Average % utilization of    | 22                           |
|   all GPU Engines           |                              |
| GPU Power (W)               | 246                          |
| GPU Core Temperature (C)    | 78                           |
| GPU Memory Temperature (C)  | 90                           |
| GPU Memory Used (MiB)       | 32504                        |
| Xe Link Throughput (kB/s)   | N/A                          |
+-----------------------------+------------------------------+
"""
XPU_PS = """\
PID       Command                       DeviceID       SHR            MEM
2109      Qwen-model-name               0              0              1024
2186      VLLM::EngineCore              0              0              33554432
"""


def test_the_gpu_table_gives_memory_per_process_id():
    assert collect.parse_gpu_table(GPU_TABLE) == {
        2425: 25.0,
        2446: 30.0,
        12711: 102.0,
    }
    assert collect.parse_gpu_table("") == {}


def test_intel_gpu_numbers_come_from_xpu_smi_output():
    assert collect.parse_xpu_stats(XPU_STATS) == {
        "gpu_util_pct": 22.0,
        "gpu_power_w": 246.0,
        "gpu_temp_c": 78.0,
        "gpu_mb": 32504.0,
    }
    assert collect.parse_xpu_ps(XPU_PS) == 32769.0  # MiB: (1024 + 33554432) KiB
    assert collect.parse_xpu_ps("") == 0.0


def test_a_live_process_is_read_and_a_missing_one_is_none():
    info = collect.read_process(os.getpid())
    assert info is not None
    assert info["rss_mb"] > 0 and info["anon_mb"] > 0
    assert info["threads"] >= 1 and info["fds"] >= 3 and info["cpu_s"] >= 0
    assert collect.read_process(2**22 + 1) is None


def test_a_target_is_exactly_one_way_of_finding_a_process():
    assert collect.Target.from_config({"port": 7501}) == collect.Target(
        "port", "7501"
    )
    assert collect.Target.from_config(
        {"container": "c", "unit": ""}
    ) == collect.Target("container", "c")
    for bad in ({}, {"port": 1, "unit": "u"}, {"match": "("}):
        with pytest.raises(ValueError):
            collect.Target.from_config(bad)


def test_a_process_is_found_by_its_port_and_by_its_command_line():
    import socket
    import subprocess
    import sys
    import uuid

    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen()
    port = server.getsockname()[1]
    marker = f"marker-{uuid.uuid4().hex}"  # not in any other command line
    child = subprocess.Popen(
        [sys.executable, "-c", f"import time  # {marker}\ntime.sleep(30)"]
    )
    try:
        assert collect._pid_listening_on(port) == os.getpid()
        assert collect._pid_listening_on(1) is None
        assert collect._pid_matching(marker) == child.pid
        assert collect._pid_matching(f"absent-{uuid.uuid4().hex}") is None
    finally:
        child.kill()
        child.wait()
        server.close()


def test_a_container_is_read_from_its_cgroup(tmp_path):
    proc, cg = tmp_path / "proc", tmp_path / "cg"
    (proc / "9").mkdir(parents=True)
    (proc / "9" / "cgroup").write_text("0::/system.slice/docker-abc.scope\n")
    scope = cg / "system.slice" / "docker-abc.scope"
    scope.mkdir(parents=True)
    mb = 1024 * 1024
    (scope / "memory.current").write_text(str(600 * mb))
    (scope / "memory.stat").write_text(f"anon {300 * mb}\nfile {100 * mb}\n")
    (scope / "cpu.stat").write_text("usage_usec 2500000\nuser_usec 1\n")
    (scope / "pids.current").write_text("186\n")
    assert collect.read_cgroup(9, proc, cg) == {
        "rss_mb": 600.0,
        "pss_mb": None,
        "anon_mb": 300.0,
        "threads": 186,
        "fds": None,
        "cpu_s": 2.5,
    }
    assert collect.read_cgroup(10, proc, cg) is None


@pytest.mark.asyncio
async def test_the_collector_follows_each_target_and_skips_a_dead_one(
    monkeypatch,
):
    async def fake_gpu(provider, container=""):
        return collect.Gpu(
            {os.getpid(): 12.0}, None, {"gpu_mb": 900.0, "gpu_util_pct": 40.0}
        )

    monkeypatch.setattr(collect, "read_gpu", fake_gpu)
    collector = collect.Collector(
        {
            "me": collect.Target("pid", str(os.getpid())),
            "dead": collect.Target("pid", str(2**22 + 1)),
            "unstarted": collect.Target("pid", "0"),
        },
        backend="linux",
    )
    rows = await collector.collect()
    assert {r["proc"] for r in rows} == {"me", collect.CARD}
    assert (
        rows[0]["gpu_mb"] == 12.0 and rows[0]["cpu_pct"] is None
    )  # no earlier reading to compare
    assert rows[-1]["gpu_util_pct"] == 40.0
    assert (await collector.collect())[0]["cpu_pct"] is not None


@pytest.mark.asyncio
async def test_the_gpu_container_gets_the_memory_xpu_smi_reports_for_it(
    monkeypatch,
):
    async def fake_gpu(provider, container=""):
        return collect.Gpu({}, 31000.0, {"gpu_mb": 32000.0})

    class Backend:
        name = "fake"

        async def sample(self, target):
            return {"pid": 5, "anon_mb": 1.0, "cpu_pct": 3.0}

    monkeypatch.setattr(collect, "read_gpu", fake_gpu)
    collector = collect.Collector(
        {
            "llm": collect.Target("container", "vllm"),
            "other": collect.Target("container", "ollama"),
        },
        "xpu-smi",
        "vllm",
        backend="linux",
    )
    collector.backend = Backend()
    rows = {r["proc"]: r for r in await collector.collect()}
    assert rows["llm"]["gpu_mb"] == 31000.0
    assert (
        rows["other"]["gpu_mb"] is None
    )  # not on that GPU, so no number is better than a wrong one
    assert rows["llm"]["cpu_pct"] == 3.0  # a backend may report CPU directly


def test_docker_sizes_are_read_in_mib():
    assert collect.parse_docker_size("603.7MiB") == 603.7
    assert collect.parse_docker_size("1GiB") == 1024.0
    assert collect.parse_docker_size("1.2GB") == round(1.2e9 / 1024**2, 1)
    assert collect.parse_docker_size("nonsense") is None


def test_the_psutil_backend_reads_a_process_the_same_way():
    pytest.importorskip("psutil")
    import asyncio

    backend = collect.PsutilBackend()
    info = asyncio.run(backend.sample(collect.Target("pid", str(os.getpid()))))
    assert info is not None
    assert (
        info["pid"] == os.getpid()
        and info["rss_mb"] > 0
        and info["threads"] >= 1
    )
    assert info["cpu_s"] >= 0
    with pytest.raises(collect.BackendError):
        asyncio.run(backend.sample(collect.Target("unit", "x")))


def test_the_psutil_backend_says_what_is_missing_when_it_is_not_installed(
    monkeypatch,
):
    import builtins

    real = builtins.__import__

    def fake(name, *a, **k):
        if name == "psutil":
            raise ImportError
        return real(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", fake)
    with pytest.raises(collect.BackendError, match="pip install psutil"):
        collect.PsutilBackend()


# --- the sidecar's config and tokens ------------------------------------------------------

PROC = {"process": {"a": {"port": 1}}}


def test_a_new_token_is_unique_and_the_config_holds_only_its_hash():
    token, table = agent.new_token("windows-box")
    other, _ = agent.new_token("windows-box")
    assert token != other and len(token) >= 40
    assert token not in table and agent.hash_token(token) in table
    cfg = agent.parse_config(
        {**PROC, "host": "0.0.0.0", **_toml_clients(table)}
    )
    assert agent.client_for(cfg.clients, token) == "windows-box"
    assert agent.client_for(cfg.clients, other) is None
    with pytest.raises(agent.ConfigError):
        agent.new_token("bad name!")


def _toml_clients(table: str) -> dict:
    import tomllib

    return tomllib.loads(table)


def test_each_client_is_recognised_by_its_own_token():
    a, ta = agent.new_token("laptop")
    b, tb = agent.new_token("desktop")
    cfg = agent.parse_config(
        {**PROC, "host": "0.0.0.0", **_toml_clients(ta + "\n" + tb)}
    )
    assert agent.client_for(cfg.clients, a) == "laptop"
    assert agent.client_for(cfg.clients, b) == "desktop"
    assert agent.client_for(cfg.clients, "") is None
    # Revoking one is deleting its table: the other is untouched.
    only_b = agent.parse_config(
        {**PROC, "host": "0.0.0.0", **_toml_clients(tb)}
    )
    assert (
        agent.client_for(only_b.clients, a) is None
        and agent.client_for(only_b.clients, b) == "desktop"
    )


def test_a_client_may_name_its_token_by_env_but_not_both_ways(monkeypatch):
    monkeypatch.setenv("SIDECAR_T", "tok")
    cfg = agent.parse_config(
        {**PROC, "client": {"x": {"token": "env:SIDECAR_T"}}}
    )
    assert agent.client_for(cfg.clients, "tok") == "x"
    bad: list[dict] = [
        {"client": {"x": {"token": "t", "token_sha256": "0" * 64}}},
        {"client": {"x": {}}},
        {"client": {"x": {"token_sha256": "nothex"}}},
        {"client": {"x": {"token": "env:NO_SUCH_VAR_ZZ"}}},
        {"client": {"x": {"token": "same"}, "y": {"token": "same"}}},
        {"client": {"bad name": {"token": "t"}}},
    ]
    for entry in bad:
        with pytest.raises(agent.ConfigError):
            agent.parse_config({**PROC, **entry})


def test_the_sidecar_config_refuses_an_open_unprotected_listener():
    _, table = agent.new_token("c")
    assert agent.parse_config(PROC).host == "127.0.0.1"
    assert agent.parse_config(
        {**PROC, "host": "0.0.0.0", **_toml_clients(table)}
    ).clients
    assert (
        agent.parse_config({**PROC, "host": "0.0.0.0", "open": True}).open
        is True
    )
    for bad in (
        {**PROC, "host": "0.0.0.0"},
        {},
        {**PROC, "gpu": "xpu-smi"},
        {**PROC, "mystery": 1},
        {**PROC, "backend": "wmi"},
        {"process": {"a": {"port": 1, "unit": "u"}}},
        {**PROC, "token": "the old shared way"},
    ):
        with pytest.raises(agent.ConfigError):
            agent.parse_config(bad)


def test_the_sidecar_serves_samples_only_to_a_known_client(monkeypatch, capsys):
    async def collect_rows(self):
        return [{"proc": "llm", "pid": 1, "anon_mb": 5.0}]

    monkeypatch.setattr(collect.Collector, "collect", collect_rows)
    good, table = agent.new_token("status-a")
    revoked, _ = agent.new_token("status-b")
    cfg = agent.parse_config({**PROC, **_toml_clients(table)})
    cfg.port = 0  # any free port
    server = agent.make_server(cfg)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        assert resources.fetch_rows(url, good, 5.0) == [
            {"proc": "llm", "pid": 1, "anon_mb": 5.0}
        ]
        for token in (revoked, "", "x"):
            with pytest.raises(urllib.error.HTTPError) as err:
                resources.fetch_rows(url, token, 5.0)
            assert err.value.code == 401
        assert "client status-a connected" in capsys.readouterr().out
    finally:
        server.shutdown()
        server.server_close()


def test_a_loopback_sidecar_without_clients_needs_no_token(monkeypatch):
    async def collect_rows(self):
        return [{"proc": "p", "pid": 1}]

    monkeypatch.setattr(collect.Collector, "collect", collect_rows)
    cfg = agent.parse_config(PROC)
    cfg.port = 0
    server = agent.make_server(cfg)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        assert resources.fetch_rows(
            f"http://127.0.0.1:{server.server_address[1]}", "", 5.0
        )
    finally:
        server.shutdown()
        server.server_close()
