"""The status page's resource history: the store, the puller, the routes, and the config that feeds them."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from brain.config import Settings
from status_page import app as status_app
from status_page import resources


@pytest.fixture
def path(tmp_path):
    return tmp_path / "resources.db"


def test_history_has_a_line_per_process_and_marks_a_restart(path):
    for i in range(4):
        resources.record(
            path,
            [
                {
                    "proc": "m/overlay",
                    "pid": 10 if i < 2 else 11,
                    "anon_mb": 100.0 + i,
                }
            ],
            now=1_000.0 + 60 * i,
        )
    resources.record(
        path, [{"proc": "m/brain", "pid": 5, "anon_mb": 300.0}], now=1_180.0
    )
    got = resources.history(path, "anon", 1.0, now=1_200.0)
    assert set(got["series"]) == {"m/overlay", "m/brain"}
    assert [p[1] for p in got["series"]["m/overlay"]] == [
        100.0,
        101.0,
        102.0,
        103.0,
    ]
    assert got["restarts"] == [[1_120.0, "m/overlay", 11]]
    assert got["unit"] == "MB"


def test_a_long_range_is_thinned_to_the_point_limit(path):
    for i in range(3_000):
        resources.record(
            path,
            [{"proc": "a", "pid": 1, "anon_mb": float(i)}],
            now=1_000.0 + 30 * i,
        )
    got = resources.history(path, "anon", 25.0, now=1_000.0 + 30 * 3_000)
    assert 0 < len(got["series"]["a"]) <= resources.MAX_POINTS + 1


def test_old_samples_are_dropped(path):
    resources.record(
        path, [{"proc": "a", "pid": 1, "anon_mb": 1.0}], now=1_000.0
    )
    later = 1_000.0 + 3_600.0
    resources.record(
        path,
        [{"proc": "a", "pid": 1, "anon_mb": 2.0}],
        now=later,
        retention_s=600.0,
    )
    got = resources.history(path, "anon", 2.0, now=later)
    assert [p[1] for p in got["series"]["a"]] == [2.0]


def test_current_reports_the_latest_and_the_six_hour_change(path):
    now = 100_000.0
    resources.record(
        path,
        [{"proc": "m/overlay", "pid": 7, "anon_mb": 300.0}],
        now=now - 5 * 3600,
    )
    resources.record(
        path,
        [{"proc": "m/overlay", "pid": 7, "anon_mb": 340.0, "gpu_mb": 20.0}],
        now=now - 10,
    )
    (row,) = resources.current(path, now=now)
    assert row["anon_mb"] == 340.0 and row["gpu_mb"] == 20.0
    assert row["anon_change_6h"] == 40.0


def test_no_change_is_claimed_across_a_restart(path):
    now = 100_000.0
    resources.record(
        path,
        [{"proc": "m/overlay", "pid": 7, "anon_mb": 500.0}],
        now=now - 3600,
    )
    resources.record(
        path, [{"proc": "m/overlay", "pid": 8, "anon_mb": 200.0}], now=now - 10
    )
    (row,) = resources.current(path, now=now)
    assert row["anon_change_6h"] is None


def test_a_stale_process_is_not_current(path):
    resources.record(
        path, [{"proc": "m/gone", "pid": 7, "anon_mb": 1.0}], now=100.0
    )
    assert resources.current(path, now=100.0 + 3_600) == []


def test_missing_database_is_empty_not_an_error(path):
    assert resources.current(path) == []
    assert resources.history(path, "rss", 1.0)["series"] == {}


def test_a_card_is_a_card_whatever_machine_it_is_on():
    assert resources.is_card("gpu card") and resources.is_card("box/gpu card")
    assert not resources.is_card("box/llm")


def test_a_database_from_before_the_gpu_columns_is_upgraded(path):
    import sqlite3

    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE samples (ts REAL NOT NULL, proc TEXT NOT NULL, pid INTEGER, rss_mb REAL, pss_mb REAL,"
        " anon_mb REAL, threads INTEGER, fds INTEGER, gpu_mb REAL, cpu_pct REAL);"
        "INSERT INTO samples (ts, proc, pid, anon_mb) VALUES (1000, 'old', 1, 5.0);"
    )
    conn.commit()
    conn.close()
    resources.record(
        path, [{"proc": "gpu card", "gpu_temp_c": 70.0}], now=1_010.0
    )
    assert (
        resources.history(path, "gpu_temp", 1.0, now=1_020.0)["series"][
            "gpu card"
        ][0][1]
        == 70.0
    )
    assert (
        resources.history(path, "anon", 1.0, now=1_020.0)["series"]["old"][0][1]
        == 5.0
    )


@pytest.mark.asyncio
async def test_the_sampler_names_each_series_by_its_source_and_reports_an_unreachable_one(
    path, monkeypatch
):
    seen = []

    def fetch(url, token, timeout_s):
        seen.append((url, token))
        if "down" in url:
            raise OSError("refused")
        if "junk" in url:
            raise TypeError("rows is not a list")
        return [
            {"proc": "llm", "pid": 4, "anon_mb": 70.0},
            {"proc": "gpu card", "gpu_mb": 9.0},
        ]

    monkeypatch.setattr(resources, "fetch_rows", fetch)
    sampler = resources.Sampler(
        path,
        {
            "box": resources.Source("http://up", "t1"),
            "here": resources.Source("http://up2", "t2"),
            "gone": resources.Source("http://down"),
            "odd": resources.Source("http://junk"),
        },
    )
    await sampler.sample_once()
    assert {r["proc"] for r in resources.current(path)} == {
        "box/llm",
        "box/gpu card",
        "here/llm",
        "here/gpu card",
    }
    assert ("http://up", "t1") in seen and (
        "http://up2",
        "t2",
    ) in seen  # each source gets its own token
    assert sampler.status["box"]["ok"] is True
    assert sampler.status["gone"] == {"ok": False, "error": "OSError: refused"}
    assert sampler.status["odd"]["ok"] is False


def test_the_status_page_reads_nothing_that_is_specific_to_an_os():
    """It must run where there is no /proc and no systemd: the sidecar is what knows the OS."""
    from pathlib import Path

    for name in ("resources.py", "app.py"):
        text = (Path(status_app.__file__).parent / name).read_text()
        assert "resources_collect" not in text, name
        assert (
            "/proc" not in text
            and "smaps" not in text
            and "sysconf" not in text
        ), name


@pytest.fixture
def settings(tmp_path, monkeypatch) -> Settings:
    monkeypatch.setenv("LLM_BOX_TEST_TOKEN", "from-env")
    s = Settings.model_validate(
        {
            "storage": {
                "workspace_dir": str(tmp_path / "ws"),
                "deploy_dir": str(tmp_path),
            },
            "llm": {
                "local": {"base_url": "http://127.0.0.1:1/v1", "model": "m"}
            },
            "status": {
                "port": 8780,
                "units": ["deskmate-brain"],
                "resources": {
                    "sources": {
                        "here": {"url": "http://127.0.0.1:8781"},
                        "box": {
                            "url": "http://192.0.2.1:8781",
                            "token": "env:LLM_BOX_TEST_TOKEN",
                        },
                    },
                },
            },
        }
    )
    monkeypatch.setattr(status_app, "_settings", lambda: s)
    return s


def _client() -> TestClient:
    return TestClient(status_app.app, base_url="http://127.0.0.1:8780")


def test_the_history_route_validates_and_serves(settings):
    client = _client()
    resources.record(
        resources.db_path(settings.workspace_dir),
        [{"proc": "here/overlay", "pid": 1, "rss_mb": 5.0}],
    )
    body = client.get("/api/resources?metric=rss&hours=1").json()
    assert list(body["series"]) == ["here/overlay"]
    assert client.get("/api/resources?metric=nope").status_code == 400
    assert (
        client.get("/api/resources?metric=rss&hours=1e9").status_code == 200
    )  # clamped, not refused


def test_the_overview_carries_the_current_values(settings):
    resources.record(
        resources.db_path(settings.workspace_dir),
        [{"proc": "here/overlay", "pid": 1, "anon_mb": 5.0}],
    )
    assert [
        r["proc"] for r in _client().get("/api/overview").json()["resources"]
    ] == ["here/overlay"]


def test_the_sampler_pulls_exactly_the_sources_in_the_config(settings):
    sampler = status_app._build_sampler(settings)
    assert set(sampler.sources) == {"here", "box"}
    assert (
        sampler.sources["box"].token == "from-env"
    )  # an env: reference is resolved
    assert sampler.sources["here"].token == ""


def test_the_overview_says_when_a_sidecar_has_not_answered(settings):
    sources = _client().get("/api/overview").json()["resource_sources"]
    assert sources == {
        "here": {"ok": None, "error": "not polled yet"},
        "box": {"ok": None, "error": "not polled yet"},
    }


def test_a_source_needs_a_plain_name():
    from pydantic import ValidationError

    from brain.config import ResourcesConfig

    for bad in ({"a/b": {"url": "u"}}, {"": {"url": "u"}}):
        with pytest.raises(ValidationError):
            ResourcesConfig.model_validate({"sources": bad})
    with pytest.raises(ValidationError):
        ResourcesConfig.model_validate(
            {"sources": {"x": {"url": "u", "oops": 1}}}
        )
    with pytest.raises(
        ValidationError
    ):  # the status page no longer follows processes itself
        ResourcesConfig.model_validate({"local": {"x": {"port": 1}}})
