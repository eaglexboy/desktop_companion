"""Integration tests for brain.main -- the daemon-side golden-path smoke
test: GET /health, POST /chat's full router->personality->voice pipeline,
and a synthetic hello/user_text_input WebSocket flow standing in for a
real overlay client.

Dependencies are faked with hermetic-CI patterns (httpx.MockTransport for
the LLM, a fake TTS backend) -- except lipsync, which runs the real
`rhubarb` binary against a real (silent) WAV: it's fast and deterministic,
so faking it here would just be a weaker copy of that coverage.
"""

from __future__ import annotations

import json
import shutil
import time
import wave
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from brain.appearance.manifest import append_version, save_manifest
from brain.config import Settings
from brain.ipc.server import PROTOCOL_VERSION
from brain.main import app, build_app_state
from brain.voice.tts import TTSBackend

HAS_RHUBARB = shutil.which("rhubarb") is not None


def _settings(tmp_path: Path, **overrides) -> Settings:
    fields = {
        "storage": {"workspace_dir": str(tmp_path / "ws"), "deploy_dir": str(tmp_path / "deploy")},
        "llm": {"local": {"base_url": "http://127.0.0.1:11434/v1", "model": "test-model"}},
        "presence": {"dbus_service_name": ""},  # skip DesktopPresenceBridge -- no D-Bus dependency in these tests
        "voice": {"rhubarb_bin": shutil.which("rhubarb") or "rhubarb"},
    }
    fields.update(overrides)
    return Settings.model_validate(fields)


def _mock_chat_transport(reply_text: str) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": reply_text}}]})

    return httpx.MockTransport(handler)


class _SilentWavTTSBackend(TTSBackend):
    """Writes a real, valid silent WAV -- so lipsync (real rhubarb, not
    faked) has genuine audio to run against, deterministically."""

    async def synthesize(self, text, out_wav_path, *, voice_id=None, params=None) -> None:
        out_wav_path.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(out_wav_path), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(16000)
            wf.writeframes(b"\x00\x00" * 8000)  # 0.5s of silence


def _wire_fakes(state, *, chat_reply: str = "Hello there!") -> None:
    """Reaches into semi-private internals to swap in fakes post-construction
    -- same precedent as other test files in this suite (e.g. pool._remote,
    bridge._locked_hint) -- rather than duplicating build_app_state()'s
    wiring logic with a second, parallel construction path."""
    transport = _mock_chat_transport(chat_reply)
    state.router._local_pool._entries[0].client._http = httpx.AsyncClient(
        base_url="http://127.0.0.1:11434/v1", transport=transport
    )
    state.local_pool._entries[0].client._http = httpx.AsyncClient(
        base_url="http://127.0.0.1:11434/v1", transport=transport
    )
    state.tts_pool._local = _SilentWavTTSBackend()


@pytest.fixture
def client(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    built_state = build_app_state(settings)
    _wire_fakes(built_state)

    monkeypatch.setattr("brain.main.build_app_state", lambda *a, **kw: built_state)
    with TestClient(app) as c:
        yield c, built_state


def _hello(client_id: str = "desktop_primary") -> dict:
    return {
        "type": "hello",
        "timestamp": time.time(),
        "payload": {
            "protocol_version": PROTOCOL_VERSION,
            "client_type": "overlay",
            "client_id": client_id,
            "capabilities": ["audio_in", "audio_out", "3d_glb"],
        },
    }


# --- GET /health -----------------------------------------------------------------


def test_health_returns_200_with_documented_fields(client):
    c, _state = client
    response = c.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["name"] == "Deskmate"
    assert body["active_voice"] == "piper:default"
    assert body["voice_degraded"] is False
    assert body["presence"] == "User_Active"
    assert "openness" in body["traits"]
    assert "energy" in body["mood"]


# --- POST /chat: the Golden Path's chat_response + speech steps ---------------


@pytest.mark.skipif(not HAS_RHUBARB, reason="rhubarb binary not found on PATH")
def test_chat_broadcasts_chat_response_and_speech_with_visemes(client):
    c, _state = client
    with c.websocket_connect("/ws/overlay", headers={"host": "127.0.0.1:8765"}) as ws:
        ws.send_json(_hello())
        ws.receive_json()  # welcome

        response = c.post("/chat", json={"text": "hi there", "speak": True})
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "ok"
        utterance_id = body["utterance_id"]

        chat_response = ws.receive_json()
        assert chat_response["type"] == "chat_response"
        assert chat_response["payload"]["utterance_id"] == utterance_id
        assert chat_response["payload"]["text"] == "Hello there!"
        assert chat_response["payload"]["provider"] == "local"

        speech = ws.receive_json()
        assert speech["type"] == "speech"
        assert speech["payload"]["utterance_id"] == utterance_id
        assert speech["payload"]["chunk_seq"] == 0
        assert speech["payload"]["final"] is True
        assert speech["payload"]["audio_url"] == f"/audio/{utterance_id}_0.wav"
        assert speech["payload"]["delivery"] in ("deadpan", "animated", "subdued", "explanatory")
        # real rhubarb ran against a real silent WAV -- at least the rest viseme
        assert len(speech["payload"]["visemes"]) >= 1
        assert all(v["shape_key"].startswith("viseme_") for v in speech["payload"]["visemes"])
        # The fake TTS wrote mono; by the time the chunk is announced the file
        # the overlay will fetch is stereo (same track on both channels).
        with wave.open(speech["payload"]["wav_path"], "rb") as announced:
            assert announced.getnchannels() == 2
            assert announced.getnframes() == 8000


def test_chat_speak_false_skips_speech_broadcast(client):
    c, _state = client
    with c.websocket_connect("/ws/overlay", headers={"host": "127.0.0.1:8765"}) as ws:
        ws.send_json(_hello())
        ws.receive_json()  # welcome

        response = c.post("/chat", json={"text": "hi there", "speak": False})
        assert response.status_code == 200

        chat_response = ws.receive_json()
        assert chat_response["type"] == "chat_response"

        # no speech event should follow -- send a second, distinguishable
        # chat to confirm the next message really is its chat_response, not
        # a delayed speech broadcast from the first.
        c.post("/chat", json={"text": "second message", "speak": False})
        second = ws.receive_json()
        assert second["type"] == "chat_response"
        assert second["payload"]["utterance_id"] != chat_response["payload"]["utterance_id"]


def test_chat_logs_interactions_to_personality_store(client):
    c, state = client
    c.post("/chat", json={"text": "remember this", "speak": False})
    pending = state.store.pending_interactions()
    texts = [row["text"] for row in pending]
    assert "remember this" in texts
    assert "Hello there!" in texts


# --- synthetic WS golden path: hello -> welcome -> user_text_input -> chat_response + speech ---


@pytest.mark.skipif(not HAS_RHUBARB, reason="rhubarb binary not found on PATH")
def test_golden_path_user_text_input_over_websocket(client):
    c, _state = client
    with c.websocket_connect("/ws/overlay", headers={"host": "127.0.0.1:8765"}) as ws:
        ws.send_json(_hello())
        welcome = ws.receive_json()
        assert welcome["type"] == "welcome"

        ws.send_json(
            {"type": "user_text_input", "timestamp": time.time(), "payload": {"text": "hello", "client_id": "desktop_primary"}}
        )

        chat_response = ws.receive_json()
        assert chat_response["type"] == "chat_response"
        assert chat_response["payload"]["text"] == "Hello there!"

        speech = ws.receive_json()
        assert speech["type"] == "speech"
        assert len(speech["payload"]["visemes"]) >= 1


# --- POST /appearance/generate ---------------------------------------------------


def _valid_look_spec_payload() -> dict:
    return {
        "base_template": "primitive_blob",
        "has_face": True,
        "palette": {"primary": "#4A90D9", "secondary": "#FFFFFF", "accent": "#F5A623", "emissive": "#7FD1FF"},
        "wardrobe": {"outfit": "none", "footwear": "none"},
    }


def test_appearance_generate_accepts_valid_spec(client, monkeypatch):
    c, _state = client

    async def fake_compile_and_publish(settings, look_spec, *, is_user_present, on_status=None):
        from brain.appearance.blender_gen import CompileResult

        return CompileResult(outcome="published", version=1, glb_path=Path("/tmp/x.glb"), manifest=None)

    import brain.evolution as evolution_module

    monkeypatch.setattr(evolution_module, "compile_and_publish", fake_compile_and_publish)

    response = c.post("/appearance/generate", json=_valid_look_spec_payload())
    assert response.status_code == 202
    assert response.json() == {"status": "building", "task": "appearance_generation"}


def test_appearance_generate_rejects_bad_catalog_value_with_422(client):
    c, _state = client
    bad_spec = _valid_look_spec_payload()
    # Not a schema-level violation (socket_eyes accepts any string) -- only
    # catalog validation (validate_against_catalogs, called synchronously
    # by the endpoint before accepting) can catch this one.
    bad_spec["accessories"] = {"socket_eyes": "not_a_real_accessory_item"}
    response = c.post("/appearance/generate", json=bad_spec)
    assert response.status_code == 422


def test_appearance_generate_rejects_malformed_spec_with_422_from_pydantic(client):
    c, _state = client
    response = c.post("/appearance/generate", json={"base_template": "primitive_blob"})  # missing required fields
    assert response.status_code == 422


# --- POST /appearance/revert ------------------------------------------------------


def test_appearance_revert_with_no_history_reports_error_status(client):
    c, _state = client
    response = c.post("/appearance/revert")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "error"


def test_appearance_revert_rewrites_manifest_pointer(client):
    c, state = client
    build_json = {
        "glb_filename": "look-v1.glb", "sha256": "a" * 64, "created_at": 1.0, "status": "ok", "seed": 42,
        "evaluated_triangles": 100, "size_bytes": 20, "has_face": True, "speech_indicator_mode": "viseme_shapekeys",
        "capabilities": {"has_head": True, "has_arms": False, "has_legs": False, "has_face": True},
        "viseme_shape_keys": [], "animation_clips": [], "bounds": {"height_m": 1, "width_m": 1, "depth_m": 1},
        "warnings": [],
    }
    manifest = append_version(None, build_json, _valid_look_spec_payload())
    manifest = append_version(manifest, {**build_json, "glb_filename": "look-v2.glb"}, _valid_look_spec_payload())
    save_manifest(state.settings.deploy_dir, manifest)
    state.settings.current_dir.mkdir(parents=True, exist_ok=True)
    (state.settings.current_dir / "look-v1.glb").write_bytes(b"glTF-fake")

    response = c.post("/appearance/revert")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "reverted_to_version": 1}


# --- POST /reflect -----------------------------------------------------------------


def test_reflect_with_nothing_pending(client):
    c, _state = client
    response = c.post("/reflect")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "notes_created": 0, "trait_deltas": {}}


def test_reflect_applies_and_reports_real_deltas(client):
    c, state = client
    state.store.log_interaction("user", "I love trying new things")

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.dumps(
            {"trait_deltas": {"openness": 5}, "mood_deltas": {}, "memory_notes": ["likes novelty"]}
        )
        return httpx.Response(200, json={"choices": [{"message": {"content": payload}}]})

    state.local_pool._entries[0].client._http = httpx.AsyncClient(
        base_url="http://127.0.0.1:11434/v1", transport=httpx.MockTransport(handler)
    )

    response = c.post("/reflect")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["notes_created"] == 1
    assert body["trait_deltas"] == {"openness": 5.0}


# --- GET /audio/{filename}, GET /assets/{filename}: path-traversal guards -----


def test_get_audio_rejects_path_traversal(client):
    c, _state = client
    assert c.get("/audio/..%2F..%2Fetc%2Fpasswd.wav").status_code == 404
    assert c.get("/audio/notawav.txt").status_code == 404
    assert c.get("/audio/nonexistent.wav").status_code == 404


def test_get_audio_serves_a_real_file(client):
    c, state = client
    wav_path = state.settings.voice_cache_dir / "real.wav"
    wav_path.parent.mkdir(parents=True, exist_ok=True)
    wav_path.write_bytes(b"RIFF-fake-wav-bytes")
    response = c.get("/audio/real.wav")
    assert response.status_code == 200
    assert response.content == b"RIFF-fake-wav-bytes"


def test_get_asset_rejects_path_traversal(client):
    c, _state = client
    assert c.get("/assets/..%2F..%2Fetc%2Fpasswd").status_code == 404
    assert c.get("/assets/nonexistent.glb").status_code == 404


def test_get_asset_serves_a_real_file(client):
    c, state = client
    state.settings.current_dir.mkdir(parents=True, exist_ok=True)
    glb_path = state.settings.current_dir / "look-v1.glb"
    glb_path.write_bytes(b"glTF-fake-bytes")
    response = c.get("/assets/look-v1.glb")
    assert response.status_code == 200
    assert response.content == b"glTF-fake-bytes"


def test_split_sentences_splits_on_terminators_and_keeps_trailing_fragment():
    from brain.main import split_sentences

    assert split_sentences("Hi there! How are you? Fine.") == ["Hi there!", "How are you?", "Fine."]
    assert split_sentences("One. Two without terminator") == ["One.", "Two without terminator"]
    assert split_sentences("Wait... what?!") == ["Wait...", "what?!"]
    assert split_sentences("...") == []
    assert split_sentences("") == []


@pytest.mark.skipif(not HAS_RHUBARB, reason="rhubarb binary not installed")
def test_multi_sentence_reply_is_spoken_as_ordered_chunks(client):
    c, state = client
    _wire_fakes(state, chat_reply="First one. Second one! Third?")
    with c.websocket_connect("/ws/overlay", headers={"host": "127.0.0.1:8765"}) as ws:
        ws.send_json(_hello())
        ws.receive_json()  # welcome

        utterance_id = c.post("/chat", json={"text": "hi", "speak": True}).json()["utterance_id"]
        assert ws.receive_json()["type"] == "chat_response"

        chunks = [ws.receive_json()["payload"] for _ in range(3)]

    assert [ch["chunk_seq"] for ch in chunks] == [0, 1, 2]
    assert [ch["final"] for ch in chunks] == [False, False, True]
    assert [ch["text"] for ch in chunks] == ["First one.", "Second one!", "Third?"]
    assert [ch["audio_url"] for ch in chunks] == [f"/audio/{utterance_id}_{n}.wav" for n in range(3)]
    assert {ch["utterance_id"] for ch in chunks} == {utterance_id}


def test_overlay_gets_current_placement_right_after_handshake(client):
    from brain.presence.placement import Rect, ScreenInfo

    c, state = client
    state.last_presence = (None, [ScreenInfo("DP-1", Rect(0, 0, 1920, 1080))])
    with c.websocket_connect("/ws/overlay", headers={"host": "127.0.0.1:8765"}) as ws:
        ws.send_json(_hello())
        assert ws.receive_json()["type"] == "welcome"
        position = ws.receive_json()
        assert position["type"] == "set_position"
        assert position["payload"]["screen_name"] == "DP-1"
        assert position["payload"]["hidden"] is False


def test_no_set_position_after_handshake_when_no_presence_telemetry_yet(client):
    c, state = client
    assert state.last_presence is None
    with c.websocket_connect("/ws/overlay", headers={"host": "127.0.0.1:8765"}) as ws:
        ws.send_json(_hello())
        assert ws.receive_json()["type"] == "welcome"
        # nothing else is queued for a client that connected before any telemetry
        c.post("/chat", json={"text": "hi", "speak": False})
        assert ws.receive_json()["type"] == "chat_response"


async def test_own_overlay_window_telemetry_does_not_make_the_avatar_flap(client):
    from brain.main import _handle_presence_update
    from brain.presence.placement import Rect, ScreenInfo, WindowState

    _c, state = client
    screens = [ScreenInfo("DP-3", Rect(2560, 0, 2560, 1440)), ScreenInfo("DP-2", Rect(0, 0, 2560, 1440))]
    broadcasts: list[dict] = []

    async def fake_broadcast(event_type, payload):
        if event_type == "set_position":
            broadcasts.append(payload)

    state.hub.broadcast = fake_broadcast

    # No window: avatar goes to the primary screen's bottom-right corner.
    await _handle_presence_update(state, None, screens)
    assert len(broadcasts) == 1
    placed = state.last_placement

    # The adapter now reports the overlay's own window at exactly that spot
    # (focused input bar). It must be ignored -- no new placement, no flip.
    size = (state.settings.presence.avatar_width_px, state.settings.presence.avatar_height_px)
    own = WindowState(Rect(placed.x, placed.y, *size), placed.screen_name, False, 1)
    for _ in range(5):
        await _handle_presence_update(state, own, screens)
    assert len(broadcasts) == 1
    assert state.last_placement == placed
    assert state.last_presence == (None, screens)  # the bogus report was not remembered either

    # A genuine user window still moves the avatar.
    user = WindowState(Rect(2560, 0, 2560, 1396), "DP-3", False, 1)
    await _handle_presence_update(state, user, screens)
    assert len(broadcasts) == 2


@pytest.mark.skipif(not HAS_RHUBARB, reason="rhubarb binary not installed")
async def test_speak_logs_backend_and_warns_when_piper_fallback_was_used(client, caplog):
    from brain.main import _speak

    _c, state = client

    class _Failing(TTSBackend):
        async def synthesize(self, text, out_wav_path, *, voice_id=None, params=None) -> None:
            raise RuntimeError("remote down")

    # one remote that always fails, with the (silent-WAV) fake as the Piper fallback
    from brain.voice.tts import TTSPool

    state.tts_pool = TTSPool([("vox", _Failing())], _SilentWavTTSBackend())
    state.hub.broadcast = _noop_broadcast
    with caplog.at_level("INFO"):
        await _speak(state, "One. Two.", utterance_id="utt_log")
    messages = [r.getMessage() for r in caplog.records]
    assert any("speech utt_log chunk 1/2: backend=local" in m for m in messages)
    assert any("voice fallback" in m and "utt_log" in m for m in messages)


async def _noop_broadcast(event_type, payload):
    return None


async def test_lipsync_overlaps_next_sentence_and_chunks_stay_in_order(client, monkeypatch):
    import asyncio

    import brain.main as main_module

    _c, state = client
    events: list[str] = []
    broadcast_order: list[int] = []

    class _RecordingTTS(TTSBackend):
        async def synthesize(self, text, out_wav_path, *, voice_id=None, params=None) -> None:
            events.append(f"synth:{text}")
            await asyncio.sleep(0.01)
            out_wav_path.parent.mkdir(parents=True, exist_ok=True)
            out_wav_path.write_bytes(b"RIFF")

    # sentence 0's lip-sync is slow, sentence 1's is instant: 1 finishes first
    delays = {"A.": 0.3, "B.": 0.0}

    async def slow_lipsync(_lipsync, wav_path, *, has_face, dialog_text=None):
        events.append(f"lip-start:{dialog_text}")
        await asyncio.sleep(delays[dialog_text])
        events.append(f"lip-end:{dialog_text}")
        return []

    async def record_broadcast(event_type, payload):
        if event_type == "speech":
            broadcast_order.append(payload["chunk_seq"])

    monkeypatch.setattr(main_module, "extract_visemes_if_needed", slow_lipsync)
    from brain.voice.tts import TTSPool

    state.tts_pool = TTSPool([], _RecordingTTS())
    state.hub.broadcast = record_broadcast
    await main_module._speak(state, "A. B.", utterance_id="utt_pipe")

    # synthesis of "B." started while "A."'s lip-sync was still running ...
    assert events.index("synth:B.") < events.index("lip-end:A.")
    # ... and even though B's lip-sync finished first, chunk 0 was broadcast first.
    assert events.index("lip-end:B.") < events.index("lip-end:A.")
    assert broadcast_order == [0, 1]


# --- live config reload: voice volume pushed to connected overlays ---------------


def test_reload_with_changed_volume_pushes_config_update(client):
    import asyncio

    from brain.main import _apply_reloaded_settings

    c, state = client
    with c.websocket_connect("/ws/overlay", headers={"host": "127.0.0.1:8765"}) as ws:
        ws.send_json(_hello())
        assert ws.receive_json()["payload"]["config"]["voice"] == {"volume": 1.0}

        reloaded = state.settings.model_copy(deep=True)
        reloaded.voice.tts.volume = 0.3
        c.portal.call(_apply_reloaded_settings, state, reloaded)

        update = ws.receive_json()
        assert update["type"] == "config_update"
        assert update["payload"]["voice"] == {"volume": 0.3}
        assert update["payload"]["presence"]["avatar_width_px"] == reloaded.presence.avatar_width_px
    assert state.hub.settings.voice.tts.volume == 0.3


def test_reload_without_overlay_relevant_change_sends_nothing(client):
    from brain.main import _apply_reloaded_settings

    c, state = client
    with c.websocket_connect("/ws/overlay", headers={"host": "127.0.0.1:8765"}) as ws:
        ws.send_json(_hello())
        ws.receive_json()  # welcome

        reloaded = state.settings.model_copy(deep=True)
        reloaded.voice.tts.rate = 0.8  # not part of the overlay's config subset
        c.portal.call(_apply_reloaded_settings, state, reloaded)
        # Prove nothing was queued: the next thing the socket delivers is the
        # reply to a /chat we make afterwards, not a config_update.
        c.post("/chat", json={"text": "hi", "speak": False})
        assert ws.receive_json()["type"] != "config_update"
