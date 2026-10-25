"""status_page: read-only collectors, the Host-header guard, and log-source whitelisting."""

from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

from brain.config import Settings
from status_page import app as status_app

TOML_SETTINGS = {
    "storage": {"workspace_dir": "/tmp/dm-status-ws", "deploy_dir": "/tmp/dm-status-deploy"},
    "llm": {"local": {"base_url": "http://127.0.0.1:1/v1", "model": "m"}},
    "status": {"port": 8780, "units": ["deskmate-brain"], "extra_logs": {"tts": "/tmp/does-not-matter.log"}},
}


@pytest.fixture
def settings(tmp_path, monkeypatch) -> Settings:
    data = {**TOML_SETTINGS, "storage": {"workspace_dir": str(tmp_path / "ws"), "deploy_dir": str(tmp_path)}}
    s = Settings.model_validate(data)
    monkeypatch.setattr(status_app, "_settings", lambda: s)
    return s


def _client() -> TestClient:
    return TestClient(status_app.app, base_url="http://127.0.0.1:8780")


def test_rejects_foreign_host_header(settings):
    r = _client().get("/", headers={"host": "evil.example:8780"})
    assert r.status_code == 403


def test_index_served_for_local_host(settings):
    r = _client().get("/")
    assert r.status_code == 200
    assert "Deskmate status" in r.text


def test_unknown_log_source_is_404_not_a_path(settings):
    assert _client().get("/api/logs/..%2Fetc%2Fpasswd").status_code == 404


def test_log_sources_come_only_from_config(settings):
    assert set(status_app._log_sources(settings)) == {"brain", "tts"}


def test_db_rows_reads_without_writing(settings, tmp_path):
    conn = sqlite3.connect(settings.personality_db_path)
    conn.executescript(
        """
        CREATE TABLE interactions(id INTEGER PRIMARY KEY, ts REAL, role TEXT, text TEXT, provider TEXT, escalated INT, reflected INT);
        CREATE TABLE memories(id INTEGER PRIMARY KEY, ts REAL, note TEXT);
        CREATE TABLE agent_state(key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE traits(name TEXT PRIMARY KEY, value REAL);
        CREATE TABLE mood(name TEXT PRIMARY KEY, value REAL);
        INSERT INTO interactions(ts, role, text) VALUES (1, 'user', 'hi'), (2, 'assistant', 'hello');
        INSERT INTO mood VALUES ('energy', 60);
        """
    )
    conn.commit()
    conn.close()
    rows = status_app._db_rows(settings)
    assert rows["available"] and [m["text"] for m in rows["interactions"]] == ["hi", "hello"]
    assert rows["mood"] == {"energy": 60}
    with pytest.raises(sqlite3.OperationalError):  # the page's own connection mode must refuse writes
        ro = sqlite3.connect(f"file:{settings.personality_db_path}?mode=ro", uri=True)
        ro.execute("INSERT INTO mood VALUES ('x', 1)")


def test_missing_db_is_reported_not_raised(settings):
    assert status_app._db_rows(settings)["available"] is False


def test_every_asset_the_index_references_is_served(settings):
    import re

    client = _client()
    html = client.get("/").text
    assets = re.findall(r'(?:href|src)="(/static/[^"]+)"', html)
    assert any(a.endswith("main.js") for a in assets) and any(a.endswith(".css") for a in assets)
    for url in assets:
        assert client.get(url).status_code == 200, url


def test_every_js_module_import_resolves(settings):
    """A rename that leaves a dangling `import ... from "./x.js"` would blank the whole page."""
    import re
    from pathlib import Path

    js_dir = Path(status_app.STATIC_DIR) / "js"
    for path in js_dir.glob("*.js"):
        for target in re.findall(r'from "\./([\w.-]+\.js)"', path.read_text()):
            assert (js_dir / target).is_file(), f"{path.name} imports missing {target}"


def test_static_mount_does_not_serve_outside_its_directory(settings):
    assert _client().get("/static/../app.py").status_code in (400, 404)


def test_static_is_covered_by_the_host_guard(settings):
    assert _client().get("/static/js/main.js", headers={"host": "evil.example:8780"}).status_code == 403


async def test_backup_state_reports_the_snapshot_age_and_missing_config(settings, tmp_path):
    import os
    import time

    # not configured
    assert (await status_app._backup_state(settings))["configured"] is False

    target = tmp_path / "bk"
    target.mkdir()
    configured = settings.model_copy(deep=True)
    configured.storage.backup_dir = str(target)
    assert (await status_app._backup_state(configured))["last_ts"] is None  # nothing backed up yet

    snapshot = target / "state.db"
    snapshot.write_bytes(b"x")
    os.utime(snapshot, (time.time() - 3600, time.time() - 3600))
    state = await status_app._backup_state(configured)
    assert 3500 < time.time() - state["last_ts"] < 3700


def test_ui_sizes_are_rem_not_px(settings):
    """The UI scales with the viewer's root font size; a stray px length would not."""
    import re
    from pathlib import Path

    css_dir = Path(status_app.STATIC_DIR) / "css"
    px = re.compile(r"(?<![\w.#-])-?\d*\.?\d+px\b")
    offenders = [f"{p.name}: {m.group(0)}" for p in css_dir.glob("*.css") for m in px.finditer(p.read_text())]
    assert not offenders, offenders


def test_scripts_carry_no_size_values(settings):
    """Sizes belong in css/. A size in an inline style string in the JS would dodge the rem rule."""
    import re
    from pathlib import Path

    size_style = re.compile(r"(?:width|height|border-radius|margin|padding|font-size|gap)\s*:\s*[\d.]+(?:px|rem|em|%)?\s*[;\"'`]")
    offenders = [f"{p.name}: {m.group(0)}" for p in (Path(status_app.STATIC_DIR) / "js").glob("*.js") for m in size_style.finditer(p.read_text())]
    assert not offenders, offenders


def test_db_rows_pairs_each_saved_reply_with_the_raw_model_output(settings):
    """The status page compares what the model returned with what was saved."""
    conn = sqlite3.connect(settings.personality_db_path)
    conn.executescript(
        """
        CREATE TABLE interactions(id INTEGER PRIMARY KEY, ts REAL, role TEXT, text TEXT, provider TEXT, escalated INT, reflected INT);
        CREATE TABLE reply_raw(interaction_id INTEGER PRIMARY KEY, raw TEXT NOT NULL);
        CREATE TABLE memories(id INTEGER PRIMARY KEY, ts REAL, note TEXT);
        CREATE TABLE agent_state(key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE traits(name TEXT PRIMARY KEY, value REAL);
        CREATE TABLE mood(name TEXT PRIMARY KEY, value REAL);
        INSERT INTO interactions(id, ts, role, text) VALUES (1, 1, 'user', 'hi'), (2, 2, 'assistant', 'Hello there.'),
                                                            (3, 3, 'assistant', 'older turn');
        INSERT INTO reply_raw VALUES (2, '{"say": "*smiles* Hello there."}');
        """
    )
    conn.commit()
    conn.close()

    rows = {m["id"]: m for m in status_app._db_rows(settings)["interactions"]}

    assert rows[2]["raw"] == '{"say": "*smiles* Hello there."}'
    assert rows[2]["text"] == "Hello there."
    assert rows[1]["raw"] is None  # user turns have none
    assert rows[3]["raw"] is None  # nor do assistant turns saved before raw replies were recorded


def test_db_rows_still_reads_a_database_without_the_reply_raw_table(settings):
    conn = sqlite3.connect(settings.personality_db_path)
    conn.executescript(
        """
        CREATE TABLE interactions(id INTEGER PRIMARY KEY, ts REAL, role TEXT, text TEXT, provider TEXT, escalated INT, reflected INT);
        CREATE TABLE memories(id INTEGER PRIMARY KEY, ts REAL, note TEXT);
        CREATE TABLE agent_state(key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE traits(name TEXT PRIMARY KEY, value REAL);
        CREATE TABLE mood(name TEXT PRIMARY KEY, value REAL);
        INSERT INTO interactions(ts, role, text) VALUES (1, 'assistant', 'hello');
        """
    )
    conn.commit()
    conn.close()

    rows = status_app._db_rows(settings)

    assert rows["available"] and [m["text"] for m in rows["interactions"]] == ["hello"]
