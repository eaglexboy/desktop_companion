"""status_page: read-only collectors, the Host-header guard, and log-source whitelisting."""

from __future__ import annotations

import json
import sqlite3
import struct

import httpx
import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from brain.config import Settings
from status_page import app as status_app

TOML_SETTINGS = {
    "storage": {
        "workspace_dir": "/tmp/dm-status-ws",
        "deploy_dir": "/tmp/dm-status-deploy",
    },
    "llm": {"local": {"base_url": "http://127.0.0.1:1/v1", "model": "m"}},
    "status": {
        "port": 8780,
        "units": ["deskmate-brain"],
        "extra_logs": {"tts": "/tmp/does-not-matter.log"},
    },
}


@pytest.fixture
def settings(tmp_path, monkeypatch) -> Settings:
    data = {
        **TOML_SETTINGS,
        "storage": {
            "workspace_dir": str(tmp_path / "ws"),
            "deploy_dir": str(tmp_path),
        },
    }
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
    assert "Deskmate Admin" in r.text


def test_unknown_log_source_is_404_not_a_path(settings):
    assert _client().get("/api/logs/..%2Fetc%2Fpasswd").status_code == 404


def test_log_sources_come_only_from_config(settings):
    assert set(status_app._log_sources(settings)) == {"brain", "tts"}


def test_db_rows_reads_without_writing(settings, tmp_path):
    conn = sqlite3.connect(settings.personality_db_path)
    conn.executescript("""
        CREATE TABLE interactions(id INTEGER PRIMARY KEY, ts REAL, role TEXT, text TEXT, provider TEXT, escalated INT, reflected INT);
        CREATE TABLE memories(id INTEGER PRIMARY KEY, ts REAL, note TEXT);
        CREATE TABLE agent_state(key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE traits(name TEXT PRIMARY KEY, value REAL);
        CREATE TABLE mood(name TEXT PRIMARY KEY, value REAL);
        INSERT INTO interactions(ts, role, text) VALUES (1, 'user', 'hi'), (2, 'assistant', 'hello');
        INSERT INTO mood VALUES ('energy', 60);
        """)
    conn.commit()
    conn.close()
    rows = status_app._db_rows(settings)
    assert rows["available"] and [m["text"] for m in rows["interactions"]] == [
        "hi",
        "hello",
    ]
    assert rows["mood"] == {"energy": 60}
    with pytest.raises(
        sqlite3.OperationalError
    ):  # the page's own connection mode must refuse writes
        ro = sqlite3.connect(
            f"file:{settings.personality_db_path}?mode=ro", uri=True
        )
        ro.execute("INSERT INTO mood VALUES ('x', 1)")


def test_missing_db_is_reported_not_raised(settings):
    assert status_app._db_rows(settings)["available"] is False


def test_every_asset_the_index_references_is_served(settings):
    import re

    client = _client()
    html = client.get("/").text
    assets = re.findall(r'(?:href|src)="(/static/[^"]+)"', html)
    assert any(a.endswith("main.js") for a in assets) and any(
        a.endswith(".css") for a in assets
    )
    for url in assets:
        assert client.get(url).status_code == 200, url


def test_every_js_module_import_resolves(settings):
    """A rename that leaves a dangling `import ... from "./x.js"` would blank the whole page."""
    import re
    from pathlib import Path

    js_dir = Path(status_app.STATIC_DIR) / "js"
    for path in js_dir.glob("*.js"):
        for target in re.findall(r'from "\./([\w.-]+\.js)"', path.read_text()):
            assert (
                js_dir / target
            ).is_file(), f"{path.name} imports missing {target}"


def test_static_mount_does_not_serve_outside_its_directory(settings):
    assert _client().get("/static/../app.py").status_code in (400, 404)


def test_static_is_covered_by_the_host_guard(settings):
    assert (
        _client()
        .get("/static/js/main.js", headers={"host": "evil.example:8780"})
        .status_code
        == 403
    )


async def test_backup_state_reports_the_snapshot_age_and_missing_config(
    settings, tmp_path
):
    import os
    import time

    # not configured
    assert (await status_app._backup_state(settings))["configured"] is False

    target = tmp_path / "bk"
    target.mkdir()
    configured = settings.model_copy(deep=True)
    configured.storage.backup_dir = str(target)
    assert (await status_app._backup_state(configured))[
        "last_ts"
    ] is None  # nothing backed up yet

    snapshot = target / "state.db"
    snapshot.write_bytes(b"x")
    os.utime(snapshot, (time.time() - 3600, time.time() - 3600))
    state = await status_app._backup_state(configured)
    assert 3500 < time.time() - state["last_ts"] < 3700


async def test_backup_state_parses_the_timers_next_run(settings, monkeypatch):
    outputs = {"is-active": "active", "show": "@1793690261\n"}

    async def fake_run(*argv):
        return 0, outputs["show" if "show" in argv else "is-active"]

    monkeypatch.setattr(status_app, "_run", fake_run)
    assert (await status_app._backup_state(settings))["next"] == 1793690261.0

    outputs["show"] = "@0\n"  # nothing scheduled
    assert (await status_app._backup_state(settings))["next"] is None


def test_ui_sizes_are_rem_not_px(settings):
    """The UI scales with the viewer's root font size; a stray px length would not."""
    import re
    from pathlib import Path

    css_dir = Path(status_app.STATIC_DIR) / "css"
    px = re.compile(r"(?<![\w.#-])-?\d*\.?\d+px\b")
    offenders = [
        f"{p.name}: {m.group(0)}"
        for p in css_dir.glob("*.css")
        for m in px.finditer(p.read_text())
    ]
    assert not offenders, offenders


def test_scripts_carry_no_size_values(settings):
    """Sizes belong in css/. A size in an inline style string in the JS would dodge the rem rule."""
    import re
    from pathlib import Path

    size_style = re.compile(
        r"(?:width|height|border-radius|margin|padding|font-size|gap)\s*:\s*[\d.]+(?:px|rem|em|%)?\s*[;\"'`]"
    )
    offenders = [
        f"{p.name}: {m.group(0)}"
        for p in (Path(status_app.STATIC_DIR) / "js").glob("*.js")
        for m in size_style.finditer(p.read_text())
    ]
    assert not offenders, offenders


def test_db_rows_pairs_each_saved_reply_with_the_raw_model_output(settings):
    """The status page compares what the model returned with what was saved."""
    conn = sqlite3.connect(settings.personality_db_path)
    conn.executescript("""
        CREATE TABLE interactions(id INTEGER PRIMARY KEY, ts REAL, role TEXT, text TEXT, provider TEXT, escalated INT, reflected INT);
        CREATE TABLE reply_raw(interaction_id INTEGER PRIMARY KEY, raw TEXT NOT NULL);
        CREATE TABLE memories(id INTEGER PRIMARY KEY, ts REAL, note TEXT);
        CREATE TABLE agent_state(key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE traits(name TEXT PRIMARY KEY, value REAL);
        CREATE TABLE mood(name TEXT PRIMARY KEY, value REAL);
        INSERT INTO interactions(id, ts, role, text) VALUES (1, 1, 'user', 'hi'), (2, 2, 'assistant', 'Hello there.'),
                                                            (3, 3, 'assistant', 'older turn');
        INSERT INTO reply_raw VALUES (2, '{"say": "*smiles* Hello there."}');
        """)
    conn.commit()
    conn.close()

    rows = {m["id"]: m for m in status_app._db_rows(settings)["interactions"]}

    assert rows[2]["raw"] == '{"say": "*smiles* Hello there."}'
    assert rows[2]["text"] == "Hello there."
    assert rows[1]["raw"] is None  # user turns have none
    assert (
        rows[3]["raw"] is None
    )  # nor do assistant turns saved before raw replies were recorded


def test_db_rows_still_reads_a_database_without_the_reply_raw_table(settings):
    conn = sqlite3.connect(settings.personality_db_path)
    conn.executescript("""
        CREATE TABLE interactions(id INTEGER PRIMARY KEY, ts REAL, role TEXT, text TEXT, provider TEXT, escalated INT, reflected INT);
        CREATE TABLE memories(id INTEGER PRIMARY KEY, ts REAL, note TEXT);
        CREATE TABLE agent_state(key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE traits(name TEXT PRIMARY KEY, value REAL);
        CREATE TABLE mood(name TEXT PRIMARY KEY, value REAL);
        INSERT INTO interactions(ts, role, text) VALUES (1, 'assistant', 'hello');
        """)
    conn.commit()
    conn.close()

    rows = status_app._db_rows(settings)

    assert rows["available"] and [m["text"] for m in rows["interactions"]] == [
        "hello"
    ]


def test_looks_lists_versions_newest_first_with_picture_flag(settings):
    import json

    def entry(version, outfit):
        return {
            "version": version,
            "glb_filename": f"look-v{version}.glb",
            "created_at": 1000.0 + version,
            "status": "ok",
            "look_spec": {
                "base_template": "standard_humanoid_female",
                "wardrobe": {"outfit": outfit, "footwear": None},
                "accessories": {
                    "socket_eyes": "glasses",
                    "socket_head_top": None,
                },
            },
        }

    (settings.deploy_dir / "manifest.json").write_text(
        json.dumps(
            {
                "current_version": 2,
                "versions": [entry(1, "hoodie"), entry(2, "vest")],
            }
        )
    )
    (settings.deploy_dir / "looks").mkdir()
    (settings.deploy_dir / "looks" / "look-v2.png").write_bytes(b"\x89PNG")

    data = _client().get("/api/looks").json()
    assert data["current"] == 2
    assert [look["version"] for look in data["looks"]] == [2, 1]
    assert (
        data["looks"][0]["has_picture"] and not data["looks"][1]["has_picture"]
    )
    assert (
        data["looks"][0]["summary"] == "standard humanoid female, vest, glasses"
    )
    assert _client().get("/api/looks/2.png").status_code == 200
    assert _client().get("/api/looks/1.png").status_code == 404


def test_looks_empty_without_manifest(settings):
    assert _client().get("/api/looks").json() == {
        "current": None,
        "locked": False,
        "looks": [],
    }


def test_looks_reports_the_lock_on_an_imported_model_until_it_is_unlocked(
    settings,
):
    import json

    entry = {
        "version": 1,
        "glb_filename": "look-v1.glb",
        "created_at": 1.0,
        "status": "ok",
        "look_spec": {
            "base_template": "custom:miss",
            "wardrobe": {},
            "accessories": {},
        },
    }
    (settings.deploy_dir / "manifest.json").write_text(
        json.dumps({"current_version": 1, "versions": [entry]})
    )
    assert (
        _client().get("/api/looks").json()["locked"] is True
    )  # no personality db yet: locked

    conn = sqlite3.connect(settings.personality_db_path)
    conn.execute("CREATE TABLE agent_state (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute(
        "INSERT INTO agent_state VALUES ('look_unlocked_for', 'custom:miss')"
    )
    conn.commit()
    conn.close()
    assert _client().get("/api/looks").json()["locked"] is False

    entry["look_spec"]["base_template"] = "primitive_blob"
    (settings.deploy_dir / "manifest.json").write_text(
        json.dumps({"current_version": 1, "versions": [entry]})
    )
    assert _client().get("/api/looks").json()["locked"] is False


def test_animations_lists_libraries_and_what_each_body_gets(settings):
    import json
    import struct

    from blender.animation_library import REQUIRED_BONES

    def glb(doc: dict) -> bytes:
        body = json.dumps(doc).encode()
        body += b" " * (-len(body) % 4)
        return (
            b"glTF"
            + struct.pack("<II", 2, 20 + len(body))
            + struct.pack("<I", len(body))
            + b"JSON"
            + body
        )

    core = sorted(REQUIRED_BONES)
    library = settings.deploy_dir / "assets" / "animation_library"
    library.mkdir(parents=True)
    (library / "pack.glb").write_bytes(
        glb(
            {
                "asset": {"version": "2.0"},
                "nodes": [{"name": n} for n in core],
                "skins": [{"joints": list(range(len(core)))}],
                "animations": [{"name": "Idle_Loop"}],
            }
        )
    )
    custom = settings.deploy_dir / "assets" / "custom_templates"
    custom.mkdir()
    (custom / "hero.glb").write_bytes(b"glTF")
    (custom / "hero.caps.json").write_text(
        json.dumps(
            {
                "bone_names": core,
                "has_head": True,
                "has_arms": True,
                "has_legs": True,
                "has_face": False,
            }
        )
    )

    data = _client().get("/api/animations").json()

    assert data["libraries"] == [
        {"name": "pack", "clips": ["Idle_Loop"], "joints": len(core)}
    ]
    hero = next(b for b in data["bodies"] if b["body"] == "custom:hero")
    assert hero["libraries"]["pack"]["fits"] is True and hero["libraries"][
        "pack"
    ]["roles"] == ["idle"]


def test_animations_is_empty_without_a_deploy_directory(settings):
    data = _client().get("/api/animations").json()
    assert data["libraries"] == []


# --- the Build page: the only writing page (docs/ADR_BUILD_YOUR_LOOK.md) -----------

WRITE_HEADERS = {
    "origin": "http://127.0.0.1:8780",
    "x-deskmate-admin": "1",
    "content-type": "application/json",
}


class FakeBrain:
    """Replaces the call to the brain, so no test ever reaches a live one."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict | None]] = []
        self.status = 202
        self.body: dict = {"status": "building"}

    async def __call__(
        self, method: str, path: str, body: dict | None = None
    ) -> httpx.Response:
        self.calls.append((method, path, body))
        return httpx.Response(self.status, json=self.body)


@pytest.fixture
def brain(settings, monkeypatch) -> FakeBrain:
    fake = FakeBrain()
    monkeypatch.setattr(status_app, "_brain", fake)
    return fake


def test_nothing_but_the_build_routes_can_change_anything(settings):
    """Every route that is not a GET lives under a writing page's prefix."""
    for route in status_app.app.routes:
        methods = getattr(route, "methods", None) or set()
        path = getattr(route, "path", "")
        if methods - {"GET", "HEAD"}:
            assert path.startswith(status_app.WRITE_PREFIXES), path


def test_a_write_to_any_other_path_is_refused(settings):
    client = _client()
    for method, path in (
        ("POST", "/api/looks"),
        ("DELETE", "/"),
        ("PUT", "/api/overview"),
        ("POST", "/api/logs/x"),
    ):
        assert (
            client.request(method, path, headers=WRITE_HEADERS).status_code
            == 405
        ), (method, path)


def test_a_write_needs_the_pages_own_origin_and_header(settings, brain):
    client = _client()
    spec = {"base_template": "primitive_blob"}
    ok = client.post("/api/build/apply", json=spec, headers=WRITE_HEADERS)
    assert ok.status_code == 202 and brain.calls[-1] == (
        "POST",
        "/appearance/generate",
        spec,
    )
    calls = len(brain.calls)

    def refused(**changes: str | None) -> int:
        headers = {**WRITE_HEADERS, **changes}
        headers = {k: v for k, v in headers.items() if v is not None}
        return client.post(
            "/api/build/apply", json=spec, headers=headers
        ).status_code

    assert refused(origin="http://evil.example") == 403
    assert refused(origin=None) == 403
    assert refused(**{"x-deskmate-admin": None}) == 403
    assert refused(**{"content-type": "text/plain"}) in (403, 415, 422)
    assert (
        client.post(
            "/api/build/apply",
            json=spec,
            headers={**WRITE_HEADERS, "host": "evil.example:8780"},
        ).status_code
        == 403
    )
    assert len(brain.calls) == calls  # none of them reached the brain


def test_a_write_must_be_a_small_json_object(settings, brain):
    client = _client()
    assert (
        client.post(
            "/api/build/apply", content=b"[1]", headers=WRITE_HEADERS
        ).status_code
        == 400
    )
    assert (
        client.post(
            "/api/build/apply", content=b"nope", headers=WRITE_HEADERS
        ).status_code
        == 400
    )
    assert (
        client.post(
            "/api/build/apply",
            content=b"{" + b" " * 20_000 + b"}",
            headers=WRITE_HEADERS,
        ).status_code
        == 413
    )
    assert not brain.calls


def test_the_brains_refusal_reaches_the_page(settings, brain):
    brain.status, brain.body = 422, {
        "detail": "wardrobe.outfit 'x' is not available"
    }
    r = _client().post(
        "/api/build/preview",
        json={"base_template": "primitive_blob"},
        headers=WRITE_HEADERS,
    )
    assert r.status_code == 422 and "not available" in r.json()["detail"]


def test_an_unreachable_brain_is_a_502_not_a_crash(settings, monkeypatch):
    monkeypatch.setattr(
        status_app, "_brain_base", lambda _s: "http://127.0.0.1:1"
    )
    r = _client().post("/api/build/apply", json={}, headers=WRITE_HEADERS)
    assert r.status_code == 502 and "not reachable" in r.json()["detail"]
    assert _client().get("/api/build/options").status_code == 502


def test_a_preview_id_is_a_hex_token_and_never_a_path(settings, brain):
    client = _client()
    for bad in ("%2e%2e", "abc", "%2e%2e%2fsecret", "G" * 32):
        assert client.get(f"/api/build/preview/{bad}").status_code == 404
        assert (
            client.delete(
                f"/api/build/preview/{bad}", headers=WRITE_HEADERS
            ).status_code
            == 404
        )
    assert not brain.calls
    good = "0123456789abcdef" * 2
    client.get(f"/api/build/preview/{good}")
    assert brain.calls[-1] == ("GET", f"/appearance/preview/{good}", None)


def test_the_picture_comes_from_the_brain_or_is_404(settings, monkeypatch):
    good = "0123456789abcdef" * 2

    async def served(method, path, body=None):
        return httpx.Response(200, content=b"\x89PNG")

    monkeypatch.setattr(status_app, "_brain", served)
    r = _client().get(f"/api/build/preview/{good}/picture")
    assert (
        r.status_code == 200
        and r.content == b"\x89PNG"
        and r.headers["content-type"] == "image/png"
    )

    async def missing(method, path, body=None):
        return httpx.Response(404, json={"detail": "no"})

    monkeypatch.setattr(status_app, "_brain", missing)
    assert (
        _client().get(f"/api/build/preview/{good}/picture").status_code == 404
    )


def _install_body(settings: Settings, name: str = "hero") -> None:
    from blender.animation_library import REQUIRED_BONES

    def glb(doc: dict) -> bytes:
        body = json.dumps(doc).encode()
        body += b" " * (-len(body) % 4)
        return (
            b"glTF"
            + struct.pack("<II", 2, 20 + len(body))
            + struct.pack("<I", len(body))
            + b"JSON"
            + body
        )

    bones = sorted(REQUIRED_BONES)
    custom = settings.deploy_dir / "assets" / "custom_templates"
    custom.mkdir(parents=True, exist_ok=True)
    (custom / f"{name}.glb").write_bytes(b"glTF-body")
    (custom / f"{name}.caps.json").write_text(
        json.dumps(
            {
                "bone_names": bones,
                "supported_sockets": [],
                "capabilities": {"has_head": True},
            }
        )
    )
    library = settings.deploy_dir / "assets" / "animation_library"
    library.mkdir(parents=True, exist_ok=True)
    (library / "pack.glb").write_bytes(
        glb(
            {
                "asset": {"version": "2.0"},
                "nodes": [{"name": n} for n in bones],
                "skins": [{"joints": list(range(len(bones)))}],
                "animations": [{"name": "Idle_Loop"}],
            }
        )
    )


def test_the_viewer_gets_an_installed_body_and_its_clips_by_id(settings):
    _install_body(settings)
    client = _client()
    body = client.get("/api/build/model/body", params={"body": "custom:hero"})
    assert body.status_code == 200 and body.content == b"glTF-body"
    assert body.headers["content-type"] == "model/gltf-binary"
    assert (
        client.get(
            "/api/build/model/library",
            params={"body": "custom:hero", "library": "pack"},
        ).status_code
        == 200
    )
    clips = client.get(
        "/api/build/animations", params={"body": "custom:hero"}
    ).json()
    assert clips["model"] is True
    only = clips["libraries"][0]["clips"]
    assert [(c["name"], c["roles"]) for c in only] == [("Idle_Loop", ["idle"])]


def test_the_viewer_cannot_be_pointed_at_a_path(settings, tmp_path):
    _install_body(settings)
    (tmp_path.parent / "secret.glb").write_bytes(b"secret")
    client = _client()
    for params in (
        {"body": "custom:../../secret"},
        {"body": "../secret"},
        {"body": "/etc/passwd"},
        {"body": "primitive_blob"},
        {"body": "custom:nothing"},
    ):
        assert (
            client.get("/api/build/model/body", params=params).status_code
            == 404
        ), params
    for library in ("../../../secret", "nope", "/etc/passwd"):
        r = client.get(
            "/api/build/model/library",
            params={"body": "custom:hero", "library": library},
        )
        assert r.status_code == 404, library
    r = client.get(
        "/api/build/model/outfit",
        params={"body": "custom:hero", "outfit": "../../../secret"},
    )
    assert r.status_code == 404


def test_a_held_body_is_not_given_to_the_viewer(settings):
    from brain.appearance import models

    _install_body(settings)
    models.set_hold(settings.deploy_dir, "custom:hero", "licence not checked")
    assert (
        _client()
        .get("/api/build/model/body", params={"body": "custom:hero"})
        .status_code
        == 404
    )


def test_start_from_reads_a_versions_stored_spec(settings):
    (settings.deploy_dir).mkdir(parents=True, exist_ok=True)
    (settings.deploy_dir / "manifest.json").write_text(
        json.dumps(
            {
                "current_version": 2,
                "versions": [
                    {
                        "version": 1,
                        "look_spec": {"base_template": "primitive_blob"},
                    },
                    {
                        "version": 2,
                        "look_spec": {"base_template": "primitive_mechanical"},
                    },
                ],
            }
        )
    )
    client = _client()
    assert (
        client.get("/api/build/spec").json()["look_spec"]["base_template"]
        == "primitive_mechanical"
    )
    assert (
        client.get("/api/build/spec", params={"version": 1}).json()[
            "look_spec"
        ]["base_template"]
        == "primitive_blob"
    )
    assert (
        client.get("/api/build/spec", params={"version": 9}).status_code == 404
    )


def test_the_import_map_points_at_files_that_are_served(settings):
    """The viewer imports `three` by name; a wrong map would blank only the Build page."""
    import re

    client = _client()
    html = client.get("/").text
    block = re.search(
        r'<script type="importmap">(.*?)</script>', html, re.DOTALL
    )
    assert block, "the index has no import map"
    imports = json.loads(block.group(1))["imports"]
    assert client.get(imports["three"]).status_code == 200
    addons = imports["three/addons/"]
    for module in (
        "loaders/GLTFLoader.js",
        "controls/OrbitControls.js",
        "utils/BufferGeometryUtils.js",
    ):
        assert client.get(addons + module).status_code == 200, module
    assert (
        client.get("/static/vendor/three/LICENSE").status_code == 200
    )  # shipped with the code


def test_the_navigation_is_a_sidebar_of_named_groups_and_every_page_is_in_one(
    settings,
):
    import re

    html = _client().get("/").text
    side = html[html.index('id="nav"') : html.index("</aside>")]
    groups = re.findall(r'class="group-name"[^>]*>([^<]+)<', side)
    assert groups == ["Overview", "Companion", "Appearance", "Models"]
    tabs = re.findall(r'data-tab="([a-z]+)"', side)
    panels = re.findall(r'class="tab" id="tab-([a-z]+)"', html)
    assert sorted(tabs) == sorted(
        panels
    )  # a page that is not in the menu cannot be reached


def test_the_build_page_is_in_the_navigation(settings):
    from pathlib import Path

    html = _client().get("/").text
    assert 'data-tab="build"' in html and 'id="tab-build"' in html
    assert (
        '"build"'
        in (Path(status_app.STATIC_DIR) / "js" / "tabs.js").read_text()
    )


def test_the_form_logic_fits_a_spec_to_a_body_without_a_browser(
    settings, tmp_path
):
    """build-spec.js is pure, so node can run it: a body that offers less drops what it cannot take."""
    import shutil
    import subprocess
    from pathlib import Path

    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    module = Path(status_app.STATIC_DIR) / "js" / "build-spec.js"
    script = tmp_path / "check.mjs"
    script.write_text(f"""
import {{ defaultSpec, fitToBody, fromStored, sameSpec, summary, defaultClip, SOCKETS, usedColors, colorReason, surfaceReason, slotsFor, isDirty, startGroups, filterGroups, startText, previewOnlySockets }} from {json.dumps(module.as_uri())};
const control = (choices, available = true) => ({{ available, choices, reason: available ? null : "no" }});
const accessories = Object.fromEntries(SOCKETS.map((s) => [s, control([], false)]));
const procedural = {{
  id: "primitive_blob", has_face: false,
  controls: {{ outfit: control(["none", "hoodie"]), footwear: control(["none", "boots"]), proportions: control(["height", "build"]),
    accessories: {{ ...accessories, socket_eyes: control(["glasses"]) }} }},
}};
const installed = {{
  id: "custom:hero", has_face: true,
  controls: {{ outfit: control(["none"], false), footwear: control(["none"], false), proportions: control([], false),
    accessories }},
}};
const out = {{}};
let spec = defaultSpec(procedural);
spec.wardrobe.outfit = "hoodie"; spec.wardrobe.footwear = "boots"; spec.accessories.socket_eyes = "glasses";
out.kept = summary(spec);
const moved = fitToBody(spec, installed);
out.moved = [moved.base_template, moved.has_face, moved.wardrobe.outfit, moved.wardrobe.footwear, moved.accessories.socket_eyes];
out.colours = moved.palette.primary === spec.palette.primary;
out.same = sameSpec(spec, JSON.parse(JSON.stringify(spec)));
out.differs = sameSpec(spec, moved);
const old = fromStored({{ base_template: "primitive_blob", palette: {{ primary: "#000000" }} }}, procedural);
out.old = [old.palette.primary, old.palette.accent, old.proportions.height, old.surface_material];
out.clip = defaultClip([{{ name: "a", clips: [{{ name: "Walk", roles: ["walk"] }}, {{ name: "Idle", roles: ["idle"] }}] }}]);
out.noclip = defaultClip([]);
const colors = {{ body: ["primary"], surface: true, outfit: ["secondary"], footwear: ["secondary", "accent"],
  accessories: Object.fromEntries(SOCKETS.map((s) => [s, s === "socket_eyes" ? ["accent", "emissive"] : ["accent"]])),
  models: {{ socket_neck: ["scarf_model"] }},
  reasons: {{ installed: "own colors", unused: "nothing uses it", surface: "no surface" }} }};
const withColors = {{ ...procedural, colors }};
const plain = defaultSpec(withColors);
out.used0 = [...usedColors(plain, withColors)];
const dressed = structuredClone(plain);
dressed.wardrobe.outfit = "hoodie"; dressed.accessories.socket_eyes = "glasses"; dressed.accessories.socket_neck = "scarf_model";
out.used1 = [...usedColors(dressed, withColors)].sort();
out.reason = [colorReason("emissive", plain, withColors), colorReason("emissive", dressed, withColors)];
out.installedReason = colorReason("primary", plain, {{ colors: {{ ...colors, body: [] }} }});
const hairy = {{ ...withColors, colors: {{ ...colors, body: ["hair"], reasons: {{ ...colors.reasons, hair: "no hair" }} }} }};
out.hair = [plain.palette.hair, colorReason("hair", plain, {{ ...withColors, colors: hairy.colors }}), colorReason("hair", plain, hairy), colorReason("primary", plain, hairy)];
const styled = {{ ...withColors, colors: {{ ...colors, hair_models: {{ socket_head_top: ["Hair_Long"] }}, reasons: {{ ...colors.reasons, hairstyle: "pick a hair model" }} }} }};
const longHair = structuredClone(plain); longHair.accessories.socket_head_top = "Hair_Long";
out.hairstyle = [colorReason("hairstyle", plain, styled), colorReason("hairstyle", longHair, styled), usedColors(longHair, styled).has("hairstyle"), plain.palette.hairstyle];
out.hairless = colorReason("hair", plain, {{ ...withColors, colors: {{ ...hairy.colors, body: ["primary"] }} }});
out.surface = [surfaceReason(withColors), surfaceReason({{ colors: {{ ...colors, surface: false }} }})];
out.slots = slotsFor(withColors).usable;
out.missing = slotsFor(withColors).missing.length;
out.dirty = [isDirty(plain, null), isDirty(plain, structuredClone(plain)), isDirty(dressed, plain), isDirty(null, null)];
const looks = {{ current: 3, looks: [{{ version: 3, summary: "custom ranger antenna" }}, {{ version: 2, summary: "primitive blob wizard robe" }}] }};
const groups = startGroups([{{ id: "primitive_blob", kind: "procedural" }}, {{ id: "custom:knight", kind: "installed" }}], looks, {{ procedural: "built-in", installed: "installed model" }});
out.groups = groups.map((g) => [g.id, g.items.length]);
out.first = groups[0].items[0];
out.filtered = filterGroups(groups, "ROBE blob").map((g) => [g.id, g.items.map((i) => i.value)]);
out.byAccessory = filterGroups(groups, "antenna").map((g) => g.items.map((i) => i.value));
out.none = filterGroups(groups, "zzz").length;
out.all = filterGroups(groups, "  ") === groups;
out.nolooks = startGroups([{{ id: "primitive_blob", kind: "procedural" }}], {{ current: null, looks: [] }}, {{ procedural: "built-in" }}).map((g) => g.id);
out.label = [startText(groups, "v:2"), startText(groups, "nope")];
out.previewOnly = [
  previewOnlySockets({{ accessories: {{ socket_head_top: "Hair", socket_eyes: "glasses", socket_neck: null }} }}, new Set(["socket_head_top"])),
  previewOnlySockets({{ accessories: {{}} }}, new Set()),
];
console.log(JSON.stringify(out));
""")
    result = subprocess.run(
        [node, str(script)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout)
    assert out["kept"] == "primitive blob, hoodie, boots, glasses"
    assert out["moved"] == ["custom:hero", True, "none", "none", None]
    assert (
        out["colours"] is True
        and out["same"] is True
        and out["differs"] is False
    )
    assert out["old"] == ["#000000", "#f5a623", 1, "matte"]
    assert (
        out["clip"] == {"library": "a", "clip": "Idle"}
        and out["noclip"] is None
    )
    assert out["used0"] == ["primary"] and out["used1"] == [
        "accent",
        "emissive",
        "primary",
        "secondary",
    ]
    assert out["reason"] == [
        "nothing uses it",
        "",
    ]  # a color is enabled once something chosen uses it
    assert out["installedReason"] == "own colors"
    # hair is white (as modelled) until chosen, is usable only on a body that has hair, and a body
    # whose only color is its hair still says its other colors are its own
    assert out["hair"] == ["#ffffff", "", "", "own colors"]
    assert out["hairless"] == "no hair"
    # a hair or beard model has its own color control, on only while one is chosen
    assert out["hairstyle"] == ["pick a hair model", "", True, "#ffffff"]
    assert out["surface"] == ["", "no surface"]
    assert out["slots"] == ["socket_eyes"] and out["missing"] == 7
    assert out["groups"] == [["had", 2], ["new", 2]]
    assert out["first"] == {
        "value": "v:3",
        "text": "Version 3 (current) — custom ranger antenna",
    }  # the current look is listed once
    assert out["filtered"] == [
        ["had", ["v:2"]]
    ]  # every word, any order, any case
    assert out["byAccessory"] == [
        ["v:3"]
    ]  # the filter reaches what a look is made of
    assert out["none"] == 0 and out["all"] is True and out["nolooks"] == ["new"]
    assert out["label"] == ["Version 2 — primitive blob wizard robe", ""]
    assert out["previewOnly"] == [
        ["socket_eyes"],
        [],
    ]  # what the viewer did not draw, and only if one is chosen
    assert out["dirty"] == [
        True,
        False,
        True,
        False,
    ]  # a new look is always applicable; an unchanged one is not


def test_the_viewer_gets_an_accessory_model_by_socket_and_item_only(settings):
    hair = (
        settings.deploy_dir
        / "assets"
        / "wardrobe"
        / "socket_head_top"
        / "Bob.glb"
    )
    hair.parent.mkdir(parents=True)
    hair.write_bytes(b"glTF-hair")
    client = _client()
    ok = client.get(
        "/api/build/model/accessory",
        params={"socket": "socket_head_top", "item": "Bob"},
    )
    assert ok.status_code == 200 and ok.content == b"glTF-hair"
    for params in (
        {"socket": "socket_neck", "item": "Bob"},
        {"socket": "socket_head_top", "item": "../socket_head_top/Bob"},
        {"socket": "socket_head_top", "item": "antenna"},
        {"socket": "..", "item": "Bob"},
    ):
        assert (
            client.get("/api/build/model/accessory", params=params).status_code
            == 404
        ), params


# --- stopping: open streams must not hold the process up --------------------------------


async def test_a_wait_inside_a_stream_ends_when_the_app_is_told_to_stop(
    monkeypatch,
):
    import asyncio

    stopping = asyncio.Event()
    monkeypatch.setattr(status_app, "_stopping", stopping)
    never = asyncio.Event()
    waiting = asyncio.ensure_future(status_app._unless_stopping(never.wait()))
    await asyncio.sleep(0)
    assert not waiting.done()  # nothing is stopping: it keeps waiting
    stopping.set()
    assert (
        await asyncio.wait_for(waiting, 1) is None
    )  # told to stop: it gives up at once
    assert all(
        t.done() for t in asyncio.all_tasks() if t is not asyncio.current_task()
    )  # and leaves nothing behind


async def test_a_wait_inside_a_stream_passes_its_result_and_its_errors_through(
    monkeypatch,
):
    import asyncio

    monkeypatch.setattr(status_app, "_stopping", asyncio.Event())

    async def value():
        return b"line"

    async def boom():
        raise ValueError("no")

    assert await status_app._unless_stopping(value()) == b"line"
    with pytest.raises(ValueError):
        await status_app._unless_stopping(boom())


# --- the interface's shared helpers, and its type check ----------------------------------


def _node(tmp_path, source: str) -> dict:
    """Runs a small ES module with node and returns the JSON it prints."""
    import shutil
    import subprocess

    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    script = tmp_path / "check.mjs"
    script.write_text(source)
    result = subprocess.run(
        [node, str(script)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_the_html_helper_escapes_by_default(tmp_path):
    from pathlib import Path

    module = (Path(status_app.STATIC_DIR) / "js" / "html.js").as_uri()
    out = _node(
        tmp_path,
        f"""
import {{ html, raw, attrs, escape }} from {json.dumps(module)};
const evil = '<img src=x onerror="alert(1)">&';
const names = ["a<b", html`<i>ok</i>`, 0, null, undefined, false];
console.log(JSON.stringify({{
  text: String(html`<b>${{evil}}</b>`),
  nested: String(html`<ul>${{names.map((n) => html`<li>${{n}}</li>`)}}</ul>`),
  raw: String(html`<p>${{raw("<br>")}}</p>`),
  nothing: String(html`[${{null}}|${{undefined}}|${{false}}|${{0}}|${{""}}]`),
  attrs: String(attrs({{ disabled: true, title: 'a"b', hidden: false, gone: null, none: undefined, n: 0 }})),
  attrInTag: String(html`<a ${{attrs({{ title: evil }})}}>x</a>`),
  escape: escape(`&<>"`),
}}));
""",
    )
    assert (
        out["text"]
        == "<b>&lt;img src=x onerror=&quot;alert(1)&quot;&gt;&amp;</b>"
    )
    assert (
        out["nested"]
        == "<ul><li>a&lt;b</li><li><i>ok</i></li><li>0</li><li></li><li></li><li></li></ul>"
    )
    assert out["raw"] == "<p><br></p>"
    assert (
        out["nothing"] == "[|||0|]"
    )  # nothing for null, undefined and false; a zero is a zero
    assert out["attrs"] == ' disabled title="a&quot;b" n="0"'
    assert (
        "onerror=&quot;" in out["attrInTag"] and '"<img' not in out["attrInTag"]
    )
    assert out["escape"] == "&amp;&lt;&gt;&quot;"


def test_the_store_tells_its_subscribers_once_per_batch_of_changes(tmp_path):
    from pathlib import Path

    module = (Path(status_app.STATIC_DIR) / "js" / "store.js").as_uri()
    out = _node(
        tmp_path,
        f"""
import {{ createStore }} from {json.dumps(module)};
const store = createStore({{ n: 0 }});
const seen = [];
const stop = store.subscribe((s) => seen.push(s.n));   // runs once at once
store.update((s) => {{ s.n = 1; }});
store.update((s) => {{ s.n = 2; }});                      // two changes, one notification
await Promise.resolve();
stop();
store.update((s) => {{ s.n = 3; }});                      // nobody is listening now
await Promise.resolve();
console.log(JSON.stringify({{ seen, final: store.state.n }}));
""",
    )
    assert out == {"seen": [0, 2], "final": 3}


def test_the_interface_javascript_passes_the_type_checker():
    """`npm run check` in status_page/ (strict JSDoc types, no build); needs the dev tools installed there."""
    import subprocess
    from pathlib import Path

    root = Path(status_app.__file__).parent
    tsc = root / "node_modules" / ".bin" / "tsc"
    if not tsc.is_file():
        pytest.skip(
            "the type checker is not installed (npm install in status_page/)"
        )
    result = subprocess.run(
        [str(tsc), "-p", str(root / "tsconfig.json")],
        capture_output=True,
        text=True,
        cwd=root,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


# --- the Model settings page: the second writing page (docs/ADR_MODEL_SETTINGS_PAGE.md) -------------


def test_model_settings_routes_relay_to_the_brain(settings, brain):
    client = _client()
    body = {"body": "custom:toy", "settings": {"format": 1}}
    for method, path, target in (
        (
            "POST",
            "/api/model-settings/validate",
            "/appearance/model-settings/validate",
        ),
        ("PUT", "/api/model-settings/save", "/appearance/model-settings"),
        ("PUT", "/api/model-settings/hold", "/appearance/model-settings/hold"),
        (
            "PUT",
            "/api/model-settings/credits",
            "/appearance/model-settings/credits",
        ),
        ("PUT", "/api/model-settings/face", "/appearance/model-settings/face"),
        (
            "POST",
            "/api/model-settings/alignment",
            "/appearance/model-settings/alignment",
        ),
        (
            "POST",
            "/api/model-settings/rebuild",
            "/appearance/model-settings/rebuild",
        ),
    ):
        brain.calls.clear()
        r = client.request(method, path, json=body, headers=WRITE_HEADERS)
        assert r.status_code == 202, (path, r.text)
        assert brain.calls == [(method, target, body)]
    brain.calls.clear()
    assert (
        client.delete(
            "/api/model-settings/stale?body=custom%3Atoy", headers=WRITE_HEADERS
        ).status_code
        == 202
    )
    assert brain.calls == [
        ("DELETE", "/appearance/model-settings/stale?body=custom%3Atoy", None)
    ]
    brain.calls.clear()
    client.get("/api/model-settings/get?body=custom:a%26b")
    assert brain.calls == [
        ("GET", "/appearance/model-settings?body=custom%3Aa%26b", None)
    ]


def test_model_settings_writes_need_the_guards_and_fit_the_size_limit(
    settings, brain
):
    client = _client()
    no_header = {
        k: v for k, v in WRITE_HEADERS.items() if k != "x-deskmate-admin"
    }
    foreign = {**WRITE_HEADERS, "origin": "http://evil.example"}
    for headers in (no_header, foreign):
        assert (
            client.put(
                "/api/model-settings/save", json={"body": "x"}, headers=headers
            ).status_code
            == 403
        )
    assert (
        client.put(
            "/api/model-settings/save",
            content=b"x",
            headers={**WRITE_HEADERS, "content-type": "text/plain"},
        ).status_code
        == 415
    )
    # bigger than a Build body is fine here, bigger than a settings file is not
    big = (
        b'{"body": "custom:toy", "settings": {"format": 1}, "pad": "'
        + b"a" * 30_000
        + b'"}'
    )
    assert (
        client.put(
            "/api/model-settings/save", content=big, headers=WRITE_HEADERS
        ).status_code
        == 202
    )
    huge = b"{" + b" " * 70_000 + b"}"
    assert (
        client.put(
            "/api/model-settings/save", content=huge, headers=WRITE_HEADERS
        ).status_code
        == 413
    )
    assert (
        client.put(
            "/api/model-settings/save", content=b"[1]", headers=WRITE_HEADERS
        ).status_code
        == 400
    )


def test_nothing_outside_the_two_writing_pages_can_change_anything(settings):
    for route in status_app.app.routes:
        if isinstance(route, APIRoute) and (route.methods or set()) - {
            "GET",
            "HEAD",
        }:
            assert route.path.startswith(
                ("/api/build/", "/api/model-settings/")
            ), route.path
    assert (
        _client()
        .put("/api/model-settings", json={}, headers=WRITE_HEADERS)
        .status_code
        == 405
    )
