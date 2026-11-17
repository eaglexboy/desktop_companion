"""Resource use over time: the status page's history store and the puller that fills it.

The status page measures nothing itself. Every machine, this one included, runs a resource sidecar
(resources_agent.py, see docs/SPEC_RESOURCE_SIDECAR.md) that reads its own processes and GPU in whatever
way its OS needs and serves the result over HTTP. Every interval the status page asks each configured
source for its rows and appends them to a small SQLite file in the workspace dir (scratch, so it can
never touch persistent state). That history is what shows a slow leak: a value that only climbs over
hours. Each row's series is named "<source>/<process>".

Nothing here reads the process table or runs a platform tool, so the status page runs on any OS.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

DEFAULT_INTERVAL_S = 30.0
DEFAULT_RETENTION_DAYS = 14.0
MAX_POINTS = 600  # per series, per history request
MAX_BODY = 1_000_000  # the most a sidecar may answer with
CARD = "gpu card"  # the pseudo-process that holds whole-card GPU numbers

# What the history page can chart: metric -> (column, label, unit)
METRICS = {
    "anon": ("anon_mb", "Private memory", "MB"),
    "rss": ("rss_mb", "Resident memory", "MB"),
    "pss": ("pss_mb", "Proportional memory", "MB"),
    "gpu": ("gpu_mb", "GPU memory", "MB"),
    "gpu_util": ("gpu_util_pct", "GPU utilization", "%"),
    "gpu_temp": ("gpu_temp_c", "GPU temperature", "°C"),
    "gpu_power": ("gpu_power_w", "GPU power", "W"),
    "threads": ("threads", "Threads", ""),
    "fds": ("fds", "Open files", ""),
    "cpu": ("cpu_pct", "CPU", "%"),
}
_COLUMNS = (
    "rss_mb",
    "pss_mb",
    "anon_mb",
    "threads",
    "fds",
    "gpu_mb",
    "cpu_pct",
    "gpu_util_pct",
    "gpu_temp_c",
    "gpu_power_w",
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS samples (
    ts REAL NOT NULL,
    proc TEXT NOT NULL,
    pid INTEGER,
    rss_mb REAL, pss_mb REAL, anon_mb REAL,
    threads INTEGER, fds INTEGER,
    gpu_mb REAL, cpu_pct REAL
);
CREATE INDEX IF NOT EXISTS samples_ts ON samples (ts);
CREATE INDEX IF NOT EXISTS samples_proc_ts ON samples (proc, ts);
"""
_ADDED_COLUMNS = ("gpu_util_pct REAL", "gpu_temp_c REAL", "gpu_power_w REAL")


def is_card(proc: str) -> bool:
    return proc == CARD or proc.endswith("/" + CARD)


def db_path(workspace_dir: Path) -> Path:
    return workspace_dir / "resources.db"


def _connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=5.0)
    conn.executescript(_SCHEMA)
    have = {row[1] for row in conn.execute("PRAGMA table_info(samples)")}
    for column in _ADDED_COLUMNS:
        if (
            column.split()[0] not in have
        ):  # a database from before these existed
            conn.execute(f"ALTER TABLE samples ADD COLUMN {column}")
    return conn


# --- the store ------------------------------------------------------------------------


def record(
    path: Path,
    rows: list[dict],
    now: float | None = None,
    retention_s: float | None = None,
) -> None:
    """Append one sample per row ({"proc", "pid", and any of the metric columns}), and drop what is older
    than the retention."""
    now = time.time() if now is None else now
    retention_s = (
        DEFAULT_RETENTION_DAYS * 86_400 if retention_s is None else retention_s
    )
    conn = _connect(path)
    try:
        with conn:
            conn.executemany(
                f"INSERT INTO samples (ts, proc, pid, {', '.join(_COLUMNS)})"
                f" VALUES (?, ?, ?, {', '.join('?' * len(_COLUMNS))})",
                [
                    (
                        now,
                        r["proc"],
                        r.get("pid"),
                        *(r.get(c) for c in _COLUMNS),
                    )
                    for r in rows
                ],
            )
            conn.execute(
                "DELETE FROM samples WHERE ts < ?", (now - retention_s,)
            )
    finally:
        conn.close()


def _open_read(path: Path) -> sqlite3.Connection | None:
    if not path.exists():
        return None
    try:
        return sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=1.0)
    except sqlite3.Error:
        return None


def current(
    path: Path, now: float | None = None, interval_s: float = DEFAULT_INTERVAL_S
) -> list[dict]:
    """The latest sample of every process, with how its private memory moved over the last 6 hours
    (None if the process was restarted in that time, since the two numbers would not be comparable).
    """
    now = time.time() if now is None else now
    conn = _open_read(path)
    if conn is None:
        return []
    names = ("ts", "proc", "pid", *_COLUMNS)
    try:
        latest = conn.execute(
            f"SELECT {', '.join(names)} FROM samples WHERE ts >= ? AND ts = "
            "(SELECT MAX(ts) FROM samples s WHERE s.proc = samples.proc) ORDER BY proc",
            (now - 10 * interval_s,),
        ).fetchall()
        out = []
        for values in latest:
            entry: dict = dict(zip(names, values, strict=True))
            entry["anon_change_6h"] = None
            proc, pid, anon = entry["proc"], entry["pid"], entry["anon_mb"]
            if anon is not None and not is_card(proc):
                row = conn.execute(
                    "SELECT ts, anon_mb FROM samples WHERE proc = ? AND ts >= ? ORDER BY ts LIMIT 1",
                    (proc, now - 6 * 3600),
                ).fetchone()
                restarted = conn.execute(
                    "SELECT 1 FROM samples WHERE proc = ? AND ts >= ? AND pid != ? LIMIT 1",
                    (proc, now - 6 * 3600, pid),
                ).fetchone()
                if row and row[1] is not None and not restarted:
                    entry["anon_change_6h"] = round(anon - row[1], 1)
                    entry["window_s"] = round(entry["ts"] - row[0])
            out.append(entry)
        return out
    except sqlite3.Error:
        return []
    finally:
        conn.close()


def history(
    path: Path, metric: str, hours: float, now: float | None = None
) -> dict:
    """One metric over time per process: {"series": {proc: [[ts, value], ...]}, "restarts": [[ts, proc, pid]]}.
    A long range is thinned by averaging into buckets, so the page never draws more than MAX_POINTS.
    """
    column = METRICS[metric][0]
    now = time.time() if now is None else now
    since = now - hours * 3600
    out: dict = {
        "metric": metric,
        "label": METRICS[metric][1],
        "unit": METRICS[metric][2],
        "since": since,
        "now": now,
        "series": {},
        "restarts": [],
    }
    conn = _open_read(path)
    if conn is None:
        return out
    bucket = max(5.0, (now - since) / MAX_POINTS)
    try:
        for proc, b, avg in conn.execute(
            f"SELECT proc, CAST(ts / ? AS INTEGER) AS b, AVG({column}) FROM samples "
            f"WHERE ts >= ? AND {column} IS NOT NULL GROUP BY proc, b ORDER BY proc, b",
            (bucket, since),
        ):
            out["series"].setdefault(proc, []).append(
                [round(b * bucket + bucket / 2, 1), round(avg, 2)]
            )
        previous: dict[str, int] = {}
        for ts, proc, pid in conn.execute(
            "SELECT ts, proc, pid FROM samples WHERE ts >= ? AND pid IS NOT NULL ORDER BY ts",
            (since,),
        ):
            if proc in previous and previous[proc] != pid:
                out["restarts"].append([ts, proc, pid])
            previous[proc] = pid
    except sqlite3.Error:
        return {**out, "series": {}, "restarts": []}
    finally:
        conn.close()
    return out


# --- the sources ----------------------------------------------------------------------


@dataclass
class Source:
    """A resource sidecar to pull from, and the token this status page presents to it."""

    url: str
    token: str = ""
    timeout_s: float = 5.0


def fetch_rows(url: str, token: str, timeout_s: float) -> list[dict]:
    """The rows a sidecar serves from GET /samples. Raises OSError, ValueError or TypeError on any failure."""
    request = urllib.request.Request(url.rstrip("/") + "/samples")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(request, timeout=timeout_s) as response:
        body = json.loads(response.read(MAX_BODY))
    rows = body["rows"]
    if not isinstance(rows, list):
        raise TypeError("rows is not a list")
    return rows


class Sampler:
    """Pulls every source each `interval_s` and records what it gets. `status` says, per source, whether the
    last attempt worked, so an unreachable sidecar is shown and not just absent.
    """

    def __init__(
        self,
        path: Path,
        sources: dict[str, Source],
        interval_s: float = DEFAULT_INTERVAL_S,
        retention_s: float | None = None,
    ) -> None:
        self.path = path
        self.sources = sources
        self.interval_s = interval_s
        self.retention_s = retention_s
        self.status: dict[str, dict] = {}

    async def _pull(self, name: str, source: Source) -> list[dict]:
        try:
            rows = await asyncio.to_thread(
                fetch_rows, source.url, source.token, source.timeout_s
            )
        except (OSError, ValueError, KeyError, TypeError) as exc:
            detail = f": {exc}" if str(exc) else ""
            self.status[name] = {
                "ok": False,
                "error": type(exc).__name__ + detail,
            }
            return []
        self.status[name] = {"ok": True, "error": ""}
        return [
            {**r, "proc": f"{name}/{r['proc']}"}
            for r in rows
            if isinstance(r, dict) and "proc" in r
        ]

    async def sample_once(self) -> list[dict]:
        batches = await asyncio.gather(
            *(self._pull(n, s) for n, s in self.sources.items())
        )
        rows = [row for batch in batches for row in batch]
        if rows:
            await asyncio.to_thread(
                record, self.path, rows, None, self.retention_s
            )
        return rows

    async def run(self) -> None:
        while True:
            try:
                await self.sample_once()
            except asyncio.CancelledError:
                raise
            except Exception:  # a bad sample must never end the sampling
                log.exception("resource sample failed")
            await asyncio.sleep(self.interval_s)
