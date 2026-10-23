"""Standalone, read-only status page for the Deskmate stack.

A separate process from the brain on purpose: when the brain hangs or
crashes, this is the thing you need to still be up. It never writes to
anything -- `state.db` is opened read-only, units are only queried, and logs
are only read. What lives solely in the brain's memory comes from the brain's
own GET /api/status, and a failure to reach it is shown, not hidden.

Run: python -m status_page.app   (from the repo root; see deploy/systemd/deskmate-status.service)
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import sqlite3
import time
from pathlib import Path

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from brain.config import Settings, get_settings

logger = logging.getLogger("deskmate.status")

STATIC_DIR = Path(__file__).with_name("static")
PROBE_TIMEOUT_S = 2.0
LOG_BACKLOG_LINES = 200

app = FastAPI(title="Deskmate status", docs_url=None, redoc_url=None, openapi_url=None)


def _settings() -> Settings:
    return get_settings()


@app.middleware("http")
async def _local_host_only(request: Request, call_next):
    """Reject requests whose Host header isn't this server's own address.
    Browsers can be steered to a localhost service by a hostile page via DNS
    rebinding; the Host header is what gives that away."""
    port = _settings().status.port
    allowed = {f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"}
    if request.headers.get("host", "") not in allowed and _settings().status.host in ("127.0.0.1", "localhost"):
        return JSONResponse({"detail": "bad host"}, status_code=403)
    return await call_next(request)


# --- collectors ----------------------------------------------------------------


async def _run(*args: str, timeout: float = 3.0) -> tuple[int, str]:
    try:
        proc = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
        return proc.returncode or 0, out.decode(errors="replace").strip()
    except (TimeoutError, OSError) as exc:
        return -1, str(exc)


async def _unit_state(unit: str) -> dict:
    _, active = await _run("systemctl", "--user", "is-active", f"{unit}.service")
    _, props = await _run(
        "systemctl", "--user", "show", f"{unit}.service", "-p", "ActiveEnterTimestampMonotonic", "-p", "NRestarts", "-p", "MainPID"
    )
    values = dict(line.split("=", 1) for line in props.splitlines() if "=" in line)
    return {
        "unit": unit,
        "active": active,
        "pid": int(values.get("MainPID", "0") or 0),
        "restarts": int(values.get("NRestarts", "0") or 0),
    }


async def _probe(client: httpx.AsyncClient, name: str, url: str, kind: str, enabled: bool = True) -> dict:
    result = {"name": name, "kind": kind, "url": url, "enabled": enabled, "ok": False, "ms": None, "detail": ""}
    if not enabled:
        result["detail"] = "disabled in config"
        return result
    started = time.perf_counter()
    try:
        response = await client.get(url)
        result["ms"] = round((time.perf_counter() - started) * 1000)
        result["ok"] = response.status_code < 500
        result["detail"] = f"HTTP {response.status_code}"
    except httpx.HTTPError as exc:
        result["detail"] = type(exc).__name__
    return result


def _probe_targets(settings: Settings) -> list[tuple[str, str, str, bool]]:
    targets = [
        ("llm: local", settings.llm.local.base_url.rstrip("/") + "/models", "llm", True),
    ]
    classifier = settings.llm.resolved_classifier()
    targets.append(("llm: classifier", classifier.base_url.rstrip("/") + "/models", "llm", True))
    for name, entry in settings.voice.tts.remote.items():
        path = getattr(entry, "capabilities_endpoint", None) or "/"
        targets.append((f"tts: {name}", entry.base_url.rstrip("/") + path, "tts", entry.enabled))
    return targets


def _db_rows(settings: Settings) -> dict:
    path = settings.personality_db_path
    out: dict = {"available": False, "interactions": [], "memories": [], "agent_state": {}, "traits": {}, "mood": {}}
    if not path.is_file():
        return out
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=1.0)
        conn.row_factory = sqlite3.Row
        try:
            out["interactions"] = [
                dict(r)
                for r in conn.execute(
                    "SELECT id, ts, role, text, provider, escalated FROM interactions ORDER BY id DESC LIMIT 40"
                )
            ][::-1]
            out["memories"] = [dict(r) for r in conn.execute("SELECT ts, note FROM memories ORDER BY id DESC LIMIT 12")]
            out["agent_state"] = {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM agent_state")}
            out["traits"] = {r["name"]: r["value"] for r in conn.execute("SELECT name, value FROM traits")}
            out["mood"] = {r["name"]: r["value"] for r in conn.execute("SELECT name, value FROM mood")}
            out["available"] = True
        finally:
            conn.close()
    except sqlite3.Error as exc:
        out["error"] = str(exc)
    return out


BACKUP_STALE_AFTER_S = 36 * 3600.0   # the timer is daily; allow a missed night plus the random delay


async def _backup_state(settings: Settings) -> dict:
    """Is the daily backup configured, scheduled, and actually producing copies?
    The evidence is the backup's own state.db (its mtime is the last successful
    snapshot, because backup_state.sh moves it into place only after an
    integrity check), plus the timer's state."""
    path = settings.storage.backup_dir
    out: dict = {"configured": bool(path), "path": path, "last_ts": None, "timer": None, "next": None}
    if path:
        snapshot = Path(path) / "state.db"
        try:
            out["last_ts"] = snapshot.stat().st_mtime
        except OSError:
            pass
    _, active = await _run("systemctl", "--user", "is-active", "deskmate-backup.timer")
    out["timer"] = active
    _, shown = await _run("systemctl", "--user", "show", "deskmate-backup.timer", "-p", "NextElapseUSecRealtime", "--value")
    if shown.strip().isdigit() and int(shown) > 0:
        out["next"] = int(shown) / 1_000_000
    out["stale_after_s"] = BACKUP_STALE_AFTER_S
    return out


@app.get("/api/overview")
async def overview() -> dict:
    settings = _settings()
    brain_base = f"http://{settings.server.host}:{settings.server.port}"
    async with httpx.AsyncClient(timeout=PROBE_TIMEOUT_S) as client:
        units_task = asyncio.gather(*(_unit_state(u) for u in settings.status.units))
        probes_task = asyncio.gather(*(_probe(client, *t[:3], enabled=t[3]) for t in _probe_targets(settings)))

        async def brain_get(path: str):
            try:
                response = await client.get(brain_base + path)
                response.raise_for_status()
                return response.json()
            except (httpx.HTTPError, ValueError) as exc:
                return {"error": type(exc).__name__}

        health, status, skills = await asyncio.gather(
            brain_get("/health"), brain_get("/api/status"), brain_get("/api/skills")
        )
        units, probes = await asyncio.gather(units_task, probes_task)
    db = await asyncio.to_thread(_db_rows, settings)
    backup = await _backup_state(settings)
    return {
        "now": time.time(),
        "units": units,
        "brain": {"url": brain_base, "health": health, "status": status, "skills": skills},
        "probes": probes,
        "db": db,
        "backup": backup,
        "log_sources": list(_log_sources(settings)),
    }


# --- logs ------------------------------------------------------------------------


def _log_sources(settings: Settings) -> dict[str, list[str]]:
    """name -> argv. Fixed by config: the client only ever names a source,
    never a path or a command."""
    sources: dict[str, list[str]] = {}
    for unit in settings.status.units:
        sources[unit.removeprefix("deskmate-")] = [
            "journalctl", "--user", "-u", f"{unit}.service", "-o", "short-iso", "--no-pager",
        ]
    for name, path in settings.status.extra_logs.items():
        sources[name] = ["tail", "-F", "-n", str(LOG_BACKLOG_LINES), path]
    return sources


def _sse(line: str) -> bytes:
    return f"data: {json.dumps(line)}\n\n".encode()


@app.get("/api/logs/{source}")
async def stream_logs(source: str, request: Request):
    argv = _log_sources(_settings()).get(source)
    if argv is None:
        raise HTTPException(404, "unknown log source")
    if argv[0] == "journalctl":
        argv = [*argv, "-n", str(LOG_BACKLOG_LINES), "-f"]
    if shutil.which(argv[0]) is None:
        raise HTTPException(500, f"{argv[0]} not available")

    async def events():
        proc = await asyncio.create_subprocess_exec(
            *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
        )
        assert proc.stdout is not None
        try:
            while True:
                try:
                    raw = await asyncio.wait_for(proc.stdout.readline(), 15.0)
                except TimeoutError:
                    yield b": keepalive\n\n"
                    if await request.is_disconnected():
                        break
                    continue
                if not raw:
                    break
                yield _sse(raw.decode(errors="replace").rstrip("\n"))
        finally:
            if proc.returncode is None:
                proc.terminate()
                await proc.wait()

    return StreamingResponse(events(), media_type="text/event-stream", headers={"Cache-Control": "no-store"})


# --- the brain's live event stream -------------------------------------------------


@app.get("/api/events")
async def proxy_events(request: Request, since: int = 0):
    """Relays the brain's SSE stream (GET /api/events) to the browser. The page
    talks only to this service; if the brain is down the stream simply ends and
    the browser's EventSource retries."""
    settings = _settings()
    url = f"http://{settings.server.host}:{settings.server.port}/api/events"
    headers = {}
    if last := request.headers.get("last-event-id"):
        headers["Last-Event-ID"] = last

    async def relay():
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(5.0, read=None)) as client:
                async with client.stream("GET", url, params={"since": since}, headers=headers) as upstream:
                    async for chunk in upstream.aiter_raw():
                        yield chunk
        except httpx.HTTPError:
            yield b"event: brain-down\ndata: {}\n\n"

    return StreamingResponse(relay(), media_type="text/event-stream", headers={"Cache-Control": "no-store"})


# --- mood / trait history ----------------------------------------------------------

MAX_POINTS_PER_SERIES = 600


def _history(settings: Settings, kind: str, hours: float) -> dict:
    path = settings.personality_db_path
    since = time.time() - hours * 3600.0
    series: dict[str, list[list[float]]] = {}
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=1.0)
        try:
            rows = conn.execute(
                "SELECT ts, name, value FROM personality_history WHERE kind = ? AND ts >= ? ORDER BY ts", (kind, since)
            ).fetchall()
            # The newest point *before* the window anchors the left edge so a
            # quiet stretch still draws a line instead of nothing.
            anchors = conn.execute(
                "SELECT name, value FROM personality_history WHERE kind = ? AND ts < ? AND ts = "
                "(SELECT MAX(ts) FROM personality_history h WHERE h.kind = ? AND h.name = personality_history.name AND h.ts < ?)",
                (kind, since, kind, since),
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return {"kind": kind, "since": since, "series": {}}
    for name, value in anchors:
        series.setdefault(name, []).append([since, value])
    for ts, name, value in rows:
        series.setdefault(name, []).append([ts, value])
    for name, points in series.items():
        if len(points) > MAX_POINTS_PER_SERIES:
            step = len(points) / MAX_POINTS_PER_SERIES
            series[name] = [points[int(i * step)] for i in range(MAX_POINTS_PER_SERIES)] + [points[-1]]
    return {"kind": kind, "since": since, "now": time.time(), "series": series}


@app.get("/api/history")
async def history(kind: str = "mood", hours: float = 24.0) -> dict:
    if kind not in ("mood", "traits"):
        raise HTTPException(400, "kind must be mood or traits")
    return await asyncio.to_thread(_history, _settings(), kind, min(max(hours, 0.1), 24 * 30))


@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    return (STATIC_DIR / "index.html").read_text()


# css/ and js/ (ES modules). Mounted last so it never shadows an /api route; the
# Host-header middleware above covers it too.
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


def main() -> None:
    settings = _settings()
    logging.basicConfig(level=logging.INFO)
    uvicorn.run(app, host=settings.status.host, port=settings.status.port, log_level="warning")


if __name__ == "__main__":
    main()
