"""Reading the resource use of this machine's processes: what the resource sidecar does.

A sidecar (resources_agent.py) follows the processes its own config names and serves what this module
reads. Which processes, and how to find each, is configuration (`Target`); how to read them is a backend:

* `linux`  - /proc and cgroups, standard library only. The default where /proc exists.
* `psutil` - the `psutil` package, for Windows and macOS (and Linux if you prefer). `pip install psutil`.

Both give the same numbers, so what the status page shows does not depend on the OS. The GPU is read
with `nvidia-smi` (any OS) or `xpu-smi` inside a container (Intel). A machine or OS this does not cover
can run its own sidecar instead: the contract is in docs/SPEC_RESOURCE_SIDECAR.md.

Standard library only (plus psutil for that backend), so a sidecar runs without this project's dependencies.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

CARD = "gpu card"  # the pseudo-process that holds whole-card GPU numbers
KINDS = ("unit", "port", "match", "container")
BACKENDS = ("auto", "linux", "psutil")
GPU_PROVIDERS = ("auto", "nvidia", "xpu-smi", "none")

try:
    _CLK_TCK = os.sysconf("SC_CLK_TCK")
except (AttributeError, ValueError, OSError):  # not a Unix
    _CLK_TCK = 100


class BackendError(RuntimeError):
    """The chosen backend cannot run here (psutil not installed, an unsupported target kind)."""


# --- what to follow -----------------------------------------------------------------


@dataclass(frozen=True)
class Target:
    """How to find one process. kind: unit | port | match | container (pid is for code, not config)."""

    kind: str
    value: str

    @staticmethod
    def from_config(entry: dict) -> Target:
        given = [(k, entry[k]) for k in KINDS if entry.get(k) not in (None, "")]
        if len(given) != 1:
            raise ValueError("set exactly one of " + ", ".join(KINDS))
        if given[0][0] == "match":
            try:
                re.compile(str(given[0][1]))
            except re.error as exc:
                raise ValueError(f"match is not a valid regex: {exc}") from exc
        return Target(given[0][0], str(given[0][1]))


async def _output(*args: str, timeout: float = 5.0) -> str:
    """stdout of a command, or "" if it is missing, fails, or is slow."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
        return out.decode(errors="replace") if proc.returncode == 0 else ""
    except (TimeoutError, OSError):
        return ""


# --- the linux backend ----------------------------------------------------------------


def _pid_listening_on(port: int, proc_root: Path = Path("/proc")) -> int | None:
    inodes: set[str] = set()
    for name in ("tcp", "tcp6"):
        try:
            lines = (proc_root / "net" / name).read_text().splitlines()[1:]
        except OSError:
            continue
        for line in lines:
            f = line.split()
            if (
                len(f) > 9
                and f[3] == "0A"
                and int(f[1].rsplit(":", 1)[1], 16) == port
            ):
                inodes.add(f[9])
    if not inodes:
        return None
    found: list[int] = []
    try:
        entries = list(proc_root.iterdir())
    except OSError:
        return None
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            for fd in (entry / "fd").iterdir():
                try:
                    link = os.readlink(fd)
                except OSError:
                    continue
                if link.startswith("socket:[") and link[8:-1] in inodes:
                    found.append(int(entry.name))
                    break
        except OSError:
            continue  # another user's process
    return min(found) if found else None


def _pid_matching(pattern: str, proc_root: Path = Path("/proc")) -> int | None:
    rx = re.compile(pattern)
    me = os.getpid()
    found: list[int] = []
    try:
        entries = list(proc_root.iterdir())
    except OSError:
        return None
    for entry in entries:
        if not entry.name.isdigit() or int(entry.name) == me:
            continue
        try:
            cmd = (
                (entry / "cmdline")
                .read_bytes()
                .replace(b"\0", b" ")
                .decode(errors="replace")
            )
        except OSError:
            continue
        if cmd and rx.search(cmd):
            found.append(int(entry.name))
    return min(found) if found else None


async def resolve_pid(target: Target) -> int | None:
    """(linux) The process id the target stands for right now (a restarted service has a new one), or None."""
    if target.kind == "pid":
        return int(target.value) or None
    if target.kind == "unit":
        out = await _output(
            "systemctl",
            "--user",
            "show",
            f"{target.value}.service",
            "-p",
            "MainPID",
            "--value",
        )
        return int(out.strip() or 0) or None
    if target.kind == "container":
        out = await _output(
            "docker", "inspect", "-f", "{{.State.Pid}}", target.value
        )
        return int(out.strip() or 0) or None
    if target.kind == "port":
        return await asyncio.to_thread(_pid_listening_on, int(target.value))
    if target.kind == "match":
        return await asyncio.to_thread(_pid_matching, target.value)
    return None


def _kb(text: str, key: str) -> float | None:
    match = re.search(rf"^{key}:\s+(\d+) kB", text, re.MULTILINE)
    return round(int(match.group(1)) / 1024, 1) if match else None


def read_process(pid: int, proc_root: Path = Path("/proc")) -> dict | None:
    """Memory, threads, open files and CPU time of one process, or None if it is gone or unreadable."""
    base = proc_root / str(pid)
    try:
        rollup = (base / "smaps_rollup").read_text()
        stat = (base / "stat").read_text()
        threads = len(os.listdir(base / "task"))
        fds = len(os.listdir(base / "fd"))
    except OSError:
        return None
    # The command name may contain spaces and parentheses; the fields after it are fixed.
    fields = stat[stat.rindex(")") + 2 :].split()
    cpu_s = (int(fields[11]) + int(fields[12])) / _CLK_TCK  # utime + stime
    return {
        "rss_mb": _kb(rollup, "Rss"),
        "pss_mb": _kb(rollup, "Pss"),
        "anon_mb": _kb(rollup, "Anonymous"),
        "threads": threads,
        "fds": fds,
        "cpu_s": cpu_s,
    }


def read_cgroup(
    pid: int,
    proc_root: Path = Path("/proc"),
    cgroup_root: Path = Path("/sys/fs/cgroup"),
) -> dict | None:
    """Everything in the cgroup the process belongs to (a container), or None if it cannot be read.
    "resident" here is the cgroup's memory.current, which includes file cache; private memory is its
    anonymous part."""
    try:
        lines = (proc_root / str(pid) / "cgroup").read_text().splitlines()
        line = next(ln for ln in lines if ln.startswith("0::"))
        base = cgroup_root / line[3:].lstrip("/")
        current = int((base / "memory.current").read_text())
        stat = dict(
            ln.split() for ln in (base / "memory.stat").read_text().splitlines()
        )
        usage = dict(
            ln.split() for ln in (base / "cpu.stat").read_text().splitlines()
        )
        pids = int((base / "pids.current").read_text())
    except (OSError, StopIteration, ValueError):
        return None
    mb = 1024 * 1024
    return {
        "rss_mb": round(current / mb, 1),
        "pss_mb": None,
        "anon_mb": round(int(stat.get("anon", 0)) / mb, 1),
        "threads": pids,
        "fds": None,
        "cpu_s": int(usage.get("usage_usec", 0)) / 1_000_000,
    }


class LinuxBackend:
    name = "linux"

    async def sample(self, target: Target) -> dict | None:
        pid = await resolve_pid(target)
        if not pid:
            return None
        read = read_cgroup if target.kind == "container" else read_process
        info = await asyncio.to_thread(read, pid)
        return None if info is None else {"pid": pid, **info}


# --- the psutil backend (Windows, macOS, or Linux by choice) ---------------------------

_SIZE = re.compile(r"^\s*([\d.]+)\s*([KMGT]?i?B)\s*$", re.IGNORECASE)
_UNITS = {
    "B": 1,
    "KB": 1e3,
    "MB": 1e6,
    "GB": 1e9,
    "TB": 1e12,
    "KIB": 1024,
    "MIB": 1024**2,
    "GIB": 1024**3,
    "TIB": 1024**4,
}


def parse_docker_size(text: str) -> float | None:
    """Megabytes (MiB) from docker's "603.7MiB" or "1.2GB"."""
    match = _SIZE.match(text)
    if not match:
        return None
    return round(
        float(match.group(1)) * _UNITS[match.group(2).upper()] / 1024**2, 1
    )


class PsutilBackend:
    name = "psutil"

    def __init__(self) -> None:
        try:
            import psutil
        except ImportError as exc:
            raise BackendError(
                "backend 'psutil' needs the psutil package: pip install psutil"
            ) from exc
        self.psutil = psutil
        self._me = os.getpid()

    def _find(self, target: Target) -> int | None:
        ps = self.psutil
        if target.kind == "pid":
            return int(target.value) or None
        if target.kind == "port":
            port = int(target.value)
            pids = [
                c.pid
                for c in ps.net_connections(kind="inet")
                if c.status == ps.CONN_LISTEN
                and c.laddr
                and c.laddr.port == port
                and c.pid
            ]
            return min(pids) if pids else None
        rx = re.compile(target.value)
        found = []
        for p in ps.process_iter():
            try:
                info = p.as_dict(attrs=["pid", "cmdline"])
            except ps.Error:  # gone, or not ours to read
                continue
            cmd = " ".join(info["cmdline"] or [])
            if info["pid"] != self._me and cmd and rx.search(cmd):
                found.append(info["pid"])
        return min(found) if found else None

    def _read(self, pid: int) -> dict | None:
        ps = self.psutil
        try:
            proc = ps.Process(pid)
            with proc.oneshot():
                mem = proc.memory_info()
                times = proc.cpu_times()
                try:
                    # unique set size: what is private to it (not every OS reports it)
                    private = getattr(proc.memory_full_info(), "uss", None)
                except (ps.AccessDenied, AttributeError):
                    private = None
                try:
                    # open files on Unix, handles on Windows (each exists on only one of them)
                    name = (
                        "num_fds" if hasattr(proc, "num_fds") else "num_handles"
                    )
                    fds = getattr(proc, name)()
                except ps.AccessDenied:
                    fds = None
                mb = 1024 * 1024
                return {
                    "pid": pid,
                    "rss_mb": round(mem.rss / mb, 1),
                    "pss_mb": None,
                    "anon_mb": (
                        None if private is None else round(private / mb, 1)
                    ),
                    "threads": proc.num_threads(),
                    "fds": fds,
                    "cpu_s": times.user + times.system,
                }
        except (ps.NoSuchProcess, ps.AccessDenied):
            return None

    async def _container(self, name: str) -> dict | None:
        out = await _output(
            "docker",
            "stats",
            "--no-stream",
            "--format",
            "{{json .}}",
            name,
            timeout=15.0,
        )
        try:
            stats = json.loads(out.strip().splitlines()[0])
            used = parse_docker_size(stats["MemUsage"].split("/")[0])
            pids = int(stats.get("PIDs") or 0)
            cpu = float(stats["CPUPerc"].strip("%"))
        except (IndexError, KeyError, ValueError):
            return None
        pid_out = await _output(
            "docker", "inspect", "-f", "{{.State.Pid}}", name
        )
        return {
            "pid": int(pid_out.strip() or 0) or None,
            "rss_mb": used,
            "pss_mb": None,
            "anon_mb": None,
            "threads": pids,
            "fds": None,
            "cpu_pct": cpu,
        }

    async def sample(self, target: Target) -> dict | None:
        if target.kind == "unit":
            raise BackendError(
                "target kind 'unit' (systemd) is only available on the linux backend"
            )
        if target.kind == "container":
            return await self._container(target.value)
        pid = await asyncio.to_thread(self._find, target)
        return await asyncio.to_thread(self._read, pid) if pid else None


class Backend(Protocol):
    """What the collector needs of an OS backend: a name, and one reading of a followed process."""

    name: str

    async def sample(self, target: Target) -> dict | None: ...


def make_backend(name: str) -> Backend:
    if name == "auto":
        name = "linux" if Path("/proc/self/smaps_rollup").exists() else "psutil"
    if name == "linux":
        return LinuxBackend()
    if name == "psutil":
        return PsutilBackend()
    raise BackendError(f"unknown backend {name!r}")


# --- the GPU --------------------------------------------------------------------------

_NVIDIA_ROW = re.compile(
    r"^\|\s+\d+\s+\S+\s+\S+\s+(\d+)\s+\S+\s+.*?(\d+)MiB\s*\|\s*$"
)


def parse_gpu_table(text: str) -> dict[int, float]:
    """GPU memory (MB) per process id from the table `nvidia-smi` prints (graphics clients show up there
    and not in --query-compute-apps)."""
    out: dict[int, float] = {}
    for line in text.splitlines():
        match = _NVIDIA_ROW.match(line)
        if match:
            pid, mb = int(match.group(1)), float(match.group(2))
            out[pid] = out.get(pid, 0.0) + mb
    return out


def _number(text: str) -> float | None:
    try:
        return float(text.strip().strip("[]"))
    except ValueError:
        return None  # N/A and the like


def parse_xpu_stats(text: str) -> dict:
    """The card's numbers from `xpu-smi stats -d 0`, whose rows are `| name | value |`."""
    wanted = (
        ("Average % utilization", "gpu_util_pct"),
        ("GPU Power (W)", "gpu_power_w"),
        ("GPU Core Temperature (C)", "gpu_temp_c"),
        ("GPU Memory Used (MiB)", "gpu_mb"),
    )
    out: dict = {}
    for line in text.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) != 2:
            continue
        for label, key in wanted:
            if cells[0].startswith(label) and key not in out:
                out[key] = _number(cells[1])
    return out


def parse_xpu_ps(text: str) -> float:
    """GPU memory (MB) held by all the processes `xpu-smi ps` lists (its MEM column is in KiB)."""
    total_kib = 0
    for line in text.splitlines():
        fields = line.split()
        if len(fields) >= 5 and fields[0].isdigit() and fields[-1].isdigit():
            total_kib += int(fields[-1])
    return round(total_kib / 1024, 1)


@dataclass
class Gpu:
    per_pid: dict[int, float]  # NVIDIA: memory by process id
    per_container: (
        float | None
    )  # Intel: memory held by everything in gpu_container
    card: dict  # gpu_mb, gpu_util_pct, gpu_temp_c, gpu_power_w (any may be missing)


async def read_gpu(provider: str, container: str = "") -> Gpu:
    if provider == "auto":
        provider = "nvidia" if shutil.which("nvidia-smi") else "none"
    if provider == "nvidia":
        table, query = await asyncio.gather(
            _output("nvidia-smi"),
            _output(
                "nvidia-smi",
                "--query-gpu=memory.used,utilization.gpu,temperature.gpu,power.draw",
                "--format=csv,noheader,nounits",
            ),
        )
        first = query.strip().splitlines()[0] if query.strip() else ""
        cells = first.split(",") if first else []
        card: dict = {}
        if len(cells) == 4:
            keys = ("gpu_mb", "gpu_util_pct", "gpu_temp_c", "gpu_power_w")
            card = dict(zip(keys, map(_number, cells), strict=True))
        return Gpu(parse_gpu_table(table), None, card)
    if provider == "xpu-smi" and container:
        stats, ps = await asyncio.gather(
            _output("docker", "exec", container, "xpu-smi", "stats", "-d", "0"),
            _output("docker", "exec", container, "xpu-smi", "ps"),
        )
        return Gpu({}, parse_xpu_ps(ps) if ps else None, parse_xpu_stats(stats))
    return Gpu({}, None, {})


# --- collecting -----------------------------------------------------------------------


class Collector:
    """Reads the followed processes and the GPU. `targets` maps a name to a Target; the names are the series."""

    def __init__(
        self,
        targets: dict[str, Target],
        gpu: str = "auto",
        gpu_container: str = "",
        backend: str = "auto",
    ) -> None:
        self.targets = targets
        self.gpu = gpu
        self.gpu_container = gpu_container
        self.backend: Backend = make_backend(backend)
        self._cpu: dict[str, tuple[int, float, float]] = (
            {}
        )  # name -> (pid, cpu seconds, when)

    async def collect(self) -> list[dict]:
        gpu = await read_gpu(self.gpu, self.gpu_container)
        now = time.monotonic()
        rows: list[dict] = []
        for name, target in self.targets.items():
            info = await self.backend.sample(target)
            if info is None:
                continue
            pid = info.get("pid")
            cpu_s = info.pop("cpu_s", None)
            before = self._cpu.get(name)
            if cpu_s is not None:
                info["cpu_pct"] = (
                    round(100 * (cpu_s - before[1]) / (now - before[2]), 1)
                    if before and before[0] == pid and now > before[2]
                    else None
                )
                self._cpu[name] = (pid or 0, cpu_s, now)
            held: float | None = None
            if gpu.per_container is not None:
                if (
                    target.kind == "container"
                    and target.value == self.gpu_container
                ):
                    held = gpu.per_container
            elif gpu.per_pid and pid:
                held = gpu.per_pid.get(pid, 0.0)
            rows.append({"proc": name, **info, "gpu_mb": held})
        if gpu.card:
            rows.append({"proc": CARD, **gpu.card})
        return rows
