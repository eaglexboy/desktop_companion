"""Tests for brain.ipc.server.OverlayHub, via FastAPI's TestClient (real
WebSocket handshakes, no running server or event loop of our own needed --
close-before-accept and custom 4000+ close codes both round-trip correctly
through it)."""

from __future__ import annotations

import time

import pytest
from fastapi import FastAPI, WebSocket
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from brain.appearance.manifest import append_version
from brain.config import Settings
from brain.ipc.server import PROTOCOL_VERSION, OverlayHub, project_manifest
from brain.voice.stt import StreamingSTT, STTBackend, STTCoordinator

DEFAULT_HOST_HEADER = "127.0.0.1:8765"


def _settings(**overrides) -> Settings:
    fields = {
        "storage": {"workspace_dir": "/tmp/ws", "deploy_dir": "/tmp/deploy"},
        "llm": {"local": {"base_url": "http://x", "model": "m"}},
    }
    fields.update(overrides)
    return Settings.model_validate(fields)


def _app(hub: OverlayHub) -> FastAPI:
    app = FastAPI()

    @app.websocket("/ws/overlay")
    async def ws_endpoint(websocket: WebSocket) -> None:
        await hub.handle_connection(websocket)

    return app


def _hello(client_id: str = "desktop_primary", **overrides) -> dict:
    payload = {
        "protocol_version": PROTOCOL_VERSION,
        "client_type": "overlay",
        "client_id": client_id,
        "capabilities": ["audio_in", "audio_out", "3d_glb"],
    }
    payload.update(overrides)
    return {"type": "hello", "timestamp": time.time(), "payload": payload}


def _build_json(**overrides) -> dict:
    payload = {
        "glb_filename": "look-v1.glb",
        "sha256": "a" * 64,
        "created_at": 1.0,
        "status": "ok",
        "seed": 42,
        "evaluated_triangles": 100,
        "size_bytes": 20,
        "has_face": True,
        "speech_indicator_mode": "viseme_shapekeys",
        "capabilities": {"has_head": True, "has_arms": False, "has_legs": False, "has_face": True},
        "viseme_shape_keys": [f"viseme_{c}" for c in "ABCDEFGHX"],
        "animation_clips": [{"name": "idle_lookaround", "loop": True, "duration_s": 4.0}],
        "bounds": {"height_m": 2.0, "width_m": 1.0, "depth_m": 1.0},
        "warnings": ["a test warning"],
    }
    payload.update(overrides)
    return payload


# --- Origin / Host protection --------------------------------------------------


def test_rejects_disallowed_origin():
    hub = OverlayHub(_settings())
    client = TestClient(_app(hub))
    with pytest.raises(WebSocketDisconnect) as exc_info, client.websocket_connect(
        "/ws/overlay", headers={"host": DEFAULT_HOST_HEADER, "origin": "https://evil.example.com"}
    ):
        pass
    assert exc_info.value.code == 1008


def test_accepts_allowed_origin():
    hub = OverlayHub(_settings(server={"allowed_origins": ["https://trusted.example.com"]}))
    client = TestClient(_app(hub))
    with client.websocket_connect(
        "/ws/overlay", headers={"host": DEFAULT_HOST_HEADER, "origin": "https://trusted.example.com"}
    ) as ws:
        ws.send_json(_hello())
        welcome = ws.receive_json()
        assert welcome["type"] == "welcome"


def test_accepts_missing_origin_as_native_client():
    hub = OverlayHub(_settings())
    client = TestClient(_app(hub))
    with client.websocket_connect("/ws/overlay", headers={"host": DEFAULT_HOST_HEADER}) as ws:
        ws.send_json(_hello())
        assert ws.receive_json()["type"] == "welcome"


def test_rejects_bad_host_header():
    hub = OverlayHub(_settings())
    client = TestClient(_app(hub))
    with pytest.raises(WebSocketDisconnect) as exc_info, client.websocket_connect(
        "/ws/overlay", headers={"host": "attacker.example.com"}
    ):
        pass
    assert exc_info.value.code == 1008


# --- handshake ------------------------------------------------------------------


def test_welcome_contains_expected_fields():
    hub = OverlayHub(_settings())
    client = TestClient(_app(hub))
    with client.websocket_connect("/ws/overlay", headers={"host": DEFAULT_HOST_HEADER}) as ws:
        ws.send_json(_hello())
        welcome = ws.receive_json()
        assert welcome["type"] == "welcome"
        payload = welcome["payload"]
        assert payload["protocol_version"] == PROTOCOL_VERSION
        assert "asset_dir" in payload
        assert payload["manifest"] == {"current_version": 0, "versions": []}
        assert payload["config"]["presence"] == {"avatar_width_px": 240, "avatar_height_px": 240, "show_speech_bubble": True}
        assert payload["config"]["shortcuts"]["toggle_interaction"] == "Meta+Alt+D"
        assert payload["config"]["voice"] == {"volume": 1.0}


def test_welcome_carries_the_configured_voice_volume():
    hub = OverlayHub(_settings(voice={"tts": {"volume": 0.4}}))
    client = TestClient(_app(hub))
    with client.websocket_connect("/ws/overlay", headers={"host": DEFAULT_HOST_HEADER}) as ws:
        ws.send_json(_hello())
        assert ws.receive_json()["payload"]["config"]["voice"] == {"volume": 0.4}


def test_update_settings_changes_what_later_clients_are_welcomed_with():
    hub = OverlayHub(_settings())
    hub.update_settings(_settings(voice={"tts": {"volume": 0.25}}))
    assert hub.settings.voice.tts.volume == 0.25
    client = TestClient(_app(hub))
    with client.websocket_connect("/ws/overlay", headers={"host": DEFAULT_HOST_HEADER}) as ws:
        ws.send_json(_hello())
        assert ws.receive_json()["payload"]["config"]["voice"] == {"volume": 0.25}


def test_welcome_includes_real_manifest_with_look_spec_and_warnings():
    manifest = append_version(None, _build_json(), {"base_template": "primitive_blob"})
    hub = OverlayHub(_settings(), get_manifest=lambda: manifest)
    client = TestClient(_app(hub))
    with client.websocket_connect("/ws/overlay", headers={"host": DEFAULT_HOST_HEADER}) as ws:
        ws.send_json(_hello())
        welcome = ws.receive_json()
        entry = welcome["payload"]["manifest"]["versions"][0]
        assert entry["look_spec"] == {"base_template": "primitive_blob"}
        assert entry["warnings"] == ["a test warning"]
        # internal-only fields never cross the wire
        assert "seed" not in entry
        assert "evaluated_triangles" not in entry
        assert "size_bytes" not in entry


def test_rejects_wrong_protocol_version():
    # The server closes only after accept() + reading this message, so the
    # rejection is only observable to the client on the *next* recv/send,
    # not at connect() time (unlike the pre-accept Origin/Host checks above).
    hub = OverlayHub(_settings())
    client = TestClient(_app(hub))
    with pytest.raises(WebSocketDisconnect) as exc_info, client.websocket_connect(
        "/ws/overlay", headers={"host": DEFAULT_HOST_HEADER}
    ) as ws:
        ws.send_json(_hello(protocol_version=99))
        ws.receive_json()
    assert exc_info.value.code == 1008


def test_rejects_non_hello_first_message():
    hub = OverlayHub(_settings())
    client = TestClient(_app(hub))
    with pytest.raises(WebSocketDisconnect) as exc_info, client.websocket_connect(
        "/ws/overlay", headers={"host": DEFAULT_HOST_HEADER}
    ) as ws:
        ws.send_json({"type": "user_text_input", "timestamp": time.time(), "payload": {"text": "hi", "client_id": "x"}})
        ws.receive_json()
    assert exc_info.value.code == 1008


def test_rejects_malformed_hello_payload():
    hub = OverlayHub(_settings())
    client = TestClient(_app(hub))
    with pytest.raises(WebSocketDisconnect) as exc_info, client.websocket_connect(
        "/ws/overlay", headers={"host": DEFAULT_HOST_HEADER}
    ) as ws:
        ws.send_json({"type": "hello", "timestamp": time.time(), "payload": {"client_id": "x"}})
        ws.receive_json()
    assert exc_info.value.code == 1008


# --- duplicate client_id --------------------------------------------------------


def test_duplicate_client_id_closes_older_socket_with_4409():
    hub = OverlayHub(_settings())
    client = TestClient(_app(hub))
    with client.websocket_connect("/ws/overlay", headers={"host": DEFAULT_HOST_HEADER}) as ws1:
        ws1.send_json(_hello("desktop_primary"))
        ws1.receive_json()  # welcome

        with client.websocket_connect("/ws/overlay", headers={"host": DEFAULT_HOST_HEADER}) as ws2:
            ws2.send_json(_hello("desktop_primary"))
            ws2.receive_json()  # welcome
            assert hub.connected_client_ids == ["desktop_primary"]

            with pytest.raises(WebSocketDisconnect) as exc_info:
                ws1.receive_json()
            assert exc_info.value.code == 4409


# --- client -> daemon event dispatch --------------------------------------------


def test_user_text_input_invokes_callback():
    received = []

    async def on_user_text_input(client_id, text):
        received.append((client_id, text))

    hub = OverlayHub(_settings(), on_user_text_input=on_user_text_input)
    client = TestClient(_app(hub))
    with client.websocket_connect("/ws/overlay", headers={"host": DEFAULT_HOST_HEADER}) as ws:
        ws.send_json(_hello("desktop_primary"))
        ws.receive_json()
        ws.send_json(
            {"type": "user_text_input", "timestamp": time.time(), "payload": {"text": "Hello", "client_id": "desktop_primary"}}
        )
        ws.close()

    assert received == [("desktop_primary", "Hello")]


def test_interaction_state_invokes_callback():
    received = []

    async def on_interaction_state(client_id, state):
        received.append((client_id, state))

    hub = OverlayHub(_settings(), on_interaction_state=on_interaction_state)
    client = TestClient(_app(hub))
    with client.websocket_connect("/ws/overlay", headers={"host": DEFAULT_HOST_HEADER}) as ws:
        ws.send_json(_hello("desktop_primary"))
        ws.receive_json()
        ws.send_json(
            {"type": "interaction_state", "timestamp": time.time(), "payload": {"state": "active", "client_id": "desktop_primary"}}
        )
        ws.close()

    assert received == [("desktop_primary", "active")]


def test_asset_swap_result_invokes_callback():
    received = []

    async def on_asset_swap_result(ok, version, error):
        received.append((ok, version, error))

    hub = OverlayHub(_settings(), on_asset_swap_result=on_asset_swap_result)
    client = TestClient(_app(hub))
    with client.websocket_connect("/ws/overlay", headers={"host": DEFAULT_HOST_HEADER}) as ws:
        ws.send_json(_hello("desktop_primary"))
        ws.receive_json()
        ws.send_json(
            {"type": "asset_swap_result", "timestamp": time.time(), "payload": {"ok": False, "version": 3, "error": "boom"}}
        )
        ws.close()

    assert received == [(False, 3, "boom")]


def test_speech_finished_invokes_callback():
    received = []

    async def on_speech_finished(client_id, utterance_id):
        received.append((client_id, utterance_id))

    hub = OverlayHub(_settings(), on_speech_finished=on_speech_finished)
    client = TestClient(_app(hub))
    with client.websocket_connect("/ws/overlay", headers={"host": DEFAULT_HOST_HEADER}) as ws:
        ws.send_json(_hello("desktop_primary"))
        ws.receive_json()
        ws.send_json({"type": "speech_finished", "timestamp": time.time(), "payload": {"utterance_id": "utt_1"}})
        ws.close()

    assert received == [("desktop_primary", "utt_1")]


# --- reserved V2+ events: recognized, no-op, never crash -----------------------


def test_reserved_event_is_a_recognized_noop():
    hub = OverlayHub(_settings())
    client = TestClient(_app(hub))
    with client.websocket_connect("/ws/overlay", headers={"host": DEFAULT_HOST_HEADER}) as ws:
        ws.send_json(_hello("desktop_primary"))
        ws.receive_json()
        ws.send_json(
            {
                "type": "agent_permission_response",
                "timestamp": time.time(),
                "payload": {"task_id": "t_1", "decision": "allow_once"},
            }
        )
        # connection must still be alive and usable afterward
        ws.send_json({"type": "speech_finished", "timestamp": time.time(), "payload": {"utterance_id": "utt_1"}})
        ws.close()


def test_unknown_event_type_does_not_crash_connection():
    hub = OverlayHub(_settings())
    client = TestClient(_app(hub))
    with client.websocket_connect("/ws/overlay", headers={"host": DEFAULT_HOST_HEADER}) as ws:
        ws.send_json(_hello("desktop_primary"))
        ws.receive_json()
        ws.send_json({"type": "something_the_server_has_never_heard_of", "timestamp": time.time(), "payload": {}})
        ws.send_json({"type": "speech_finished", "timestamp": time.time(), "payload": {"utterance_id": "utt_1"}})
        ws.close()


def test_malformed_payload_does_not_crash_connection():
    received = []

    async def on_speech_finished(client_id, utterance_id):
        received.append(utterance_id)

    hub = OverlayHub(_settings(), on_speech_finished=on_speech_finished)
    client = TestClient(_app(hub))
    with client.websocket_connect("/ws/overlay", headers={"host": DEFAULT_HOST_HEADER}) as ws:
        ws.send_json(_hello("desktop_primary"))
        ws.receive_json()
        ws.send_json({"type": "speech_finished", "timestamp": time.time(), "payload": {"wrong_field": "x"}})
        ws.send_json({"type": "speech_finished", "timestamp": time.time(), "payload": {"utterance_id": "utt_ok"}})
        ws.close()

    assert received == ["utt_ok"]


# --- audio streaming -------------------------------------------------------------


class _FakeSTTBackend(STTBackend):
    def __init__(self, text: str) -> None:
        self._text = text

    async def transcribe(self, wav_path, *, language=None) -> str:
        return self._text


def test_audio_stream_routes_binary_frames_and_finalizes_on_end():
    received = []

    async def on_user_text_input(client_id, text):
        received.append((client_id, text))

    coordinator = STTCoordinator("local", _FakeSTTBackend("transcribed speech"), None)
    streaming = StreamingSTT(coordinator, silence_threshold_ms=800)
    hub = OverlayHub(_settings(), streaming_stt=streaming, on_user_text_input=on_user_text_input)
    client = TestClient(_app(hub))

    with client.websocket_connect("/ws/overlay", headers={"host": DEFAULT_HOST_HEADER}) as ws:
        ws.send_json(_hello("desktop_primary"))
        ws.receive_json()
        ws.send_json(
            {"type": "audio_stream_start", "timestamp": time.time(), "payload": {"stream_id": "s_1", "sample_rate": 16000, "encoding": "pcm16le", "channels": 1}}
        )
        ws.send_json({"type": "audio_stream_chunk", "timestamp": time.time(), "payload": {"stream_id": "s_1", "seq": 0}})
        ws.send_bytes(b"\x00\x00" * 160)
        ws.send_json({"type": "audio_stream_chunk", "timestamp": time.time(), "payload": {"stream_id": "s_1", "seq": 1}})
        ws.send_bytes(b"\x00\x00" * 160)
        ws.send_json({"type": "audio_stream_end", "timestamp": time.time(), "payload": {"stream_id": "s_1"}})
        ws.close()

    assert received == [("desktop_primary", "transcribed speech")]


def test_binary_frame_with_no_active_stream_is_ignored_not_fatal():
    coordinator = STTCoordinator("local", _FakeSTTBackend("x"), None)
    streaming = StreamingSTT(coordinator, silence_threshold_ms=800)
    hub = OverlayHub(_settings(), streaming_stt=streaming)
    client = TestClient(_app(hub))

    with client.websocket_connect("/ws/overlay", headers={"host": DEFAULT_HOST_HEADER}) as ws:
        ws.send_json(_hello("desktop_primary"))
        ws.receive_json()
        ws.send_bytes(b"\x00\x00" * 160)  # no audio_stream_start first
        ws.send_json({"type": "speech_finished", "timestamp": time.time(), "payload": {"utterance_id": "still_alive"}})
        ws.close()


# --- broadcast -------------------------------------------------------------------
#
# Unit-tested directly against OverlayHub's internal connection registry
# rather than through a real TestClient WebSocket round-trip: the real
# server-side WebSocket objects live inside TestClient's own background
# thread/event loop, and `TestClient.portal` is None outside an active
# request, so there's no supported way to schedule a coroutine onto that
# loop from a sync test. broadcast()'s actual logic (iterate connections,
# send, prune dead ones) doesn't need a real socket to verify.


class _FakeWebSocket:
    def __init__(self, *, fails: bool = False) -> None:
        self.sent: list[dict] = []
        self.fails = fails

    async def send_json(self, data: dict) -> None:
        if self.fails:
            raise RuntimeError("socket is dead")
        self.sent.append(data)


@pytest.mark.asyncio
async def test_broadcast_reaches_all_connected_clients():
    from brain.ipc.server import _ConnectionState

    hub = OverlayHub(_settings())
    ws1, ws2 = _FakeWebSocket(), _FakeWebSocket()
    hub._connections["primary"] = _ConnectionState(websocket=ws1, client_id="primary", client_type="overlay", capabilities=[])
    hub._connections["secondary"] = _ConnectionState(websocket=ws2, client_id="secondary", client_type="overlay", capabilities=[])

    await hub.broadcast("mood_update", {"energy": 60.0, "valence": 50.0, "stress": 20.0})

    assert ws1.sent[0]["type"] == "mood_update"
    assert ws1.sent[0]["payload"]["energy"] == 60.0
    assert ws2.sent[0]["type"] == "mood_update"


@pytest.mark.asyncio
async def test_broadcast_prunes_dead_connections_without_failing_the_rest():
    from brain.ipc.server import _ConnectionState

    hub = OverlayHub(_settings())
    alive, dead = _FakeWebSocket(), _FakeWebSocket(fails=True)
    hub._connections["alive"] = _ConnectionState(websocket=alive, client_id="alive", client_type="overlay", capabilities=[])
    hub._connections["dead"] = _ConnectionState(websocket=dead, client_id="dead", client_type="overlay", capabilities=[])

    await hub.broadcast("status_update", {"state": "idle"})

    assert len(alive.sent) == 1
    assert hub.connected_client_ids == ["alive"]  # the dead one was pruned


def test_hot_reload_asset_strips_look_spec_and_warnings():
    manifest = append_version(None, _build_json(), {"base_template": "primitive_blob"})
    stripped = project_manifest(manifest, include_look_spec_and_warnings=False)
    entry = stripped["versions"][0]
    assert "look_spec" not in entry
    assert "warnings" not in entry
    assert entry["glb_filename"] == "look-v1.glb"
