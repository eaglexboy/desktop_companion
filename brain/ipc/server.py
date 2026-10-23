"""The presentation client's WebSocket hub (`WS /ws/overlay`): Origin/Host
validation, the hello/welcome handshake, duplicate-client-id eviction,
typed Pydantic envelopes for every event, and binary audio-frame routing
into `StreamingSTT`.

This module only owns the WebSocket hub itself -- `brain/main.py` mounts
`OverlayHub.handle_connection` at `/ws/overlay` on the real FastAPI app,
wires the daemon-side callbacks (`on_user_text_input`,
`on_interaction_state`, `on_asset_swap_result`, `on_speech_finished`), and
calls `OverlayHub.broadcast(...)` from the rest of the daemon (chat
replies, appearance reveals, mood updates, ...). Everything here is
independently testable via Starlette's `TestClient`
(`fastapi.testclient.TestClient`), which supports real WebSocket handshakes
without a running server or event loop of its own.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from fastapi import WebSocket
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.websockets import WebSocketDisconnect, WebSocketState

from brain.appearance.manifest import AssetManifest, AssetVersionEntry
from brain.config import Settings
from brain.voice.stt import StreamingSTT

logger = logging.getLogger("deskmate.ipc")

PROTOCOL_VERSION = 1
_HELLO_TIMEOUT_S = 10.0
_CLOSE_POLICY_VIOLATION = 1008
_CLOSE_CLIENT_ID_REPLACED = 4409


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --- generic envelope + per-type payload models -------------------------------


class Envelope(_Strict):
    type: str
    timestamp: float
    payload: dict[str, Any]


class HelloPayload(_Strict):
    protocol_version: int
    client_type: str
    client_id: str
    capabilities: list[str] = Field(default_factory=list)


class UserTextInputPayload(_Strict):
    text: str
    client_id: str


class AudioStreamStartPayload(_Strict):
    stream_id: str
    sample_rate: int = 16000
    encoding: str = "pcm16le"
    channels: int = 1


class AudioStreamChunkPayload(_Strict):
    stream_id: str
    seq: int


class AudioStreamEndPayload(_Strict):
    stream_id: str


class AssetSwapResultPayload(_Strict):
    ok: bool
    version: int
    error: str | None = None


class SpeechFinishedPayload(_Strict):
    utterance_id: str


class InteractionStatePayload(_Strict):
    state: Literal["active", "idle"]
    client_id: str


# Reserved for a future agent/skill orchestration feature: parsed as a
# recognized event (the outer Envelope still validates), but the payload
# shape is never checked and never dispatched anywhere -- a `None` registry
# entry below means exactly that.
_RESERVED_NOOP_TYPES = ("agent_permission_response",)

# Client -> daemon payload models, keyed by Envelope.type. "hello" is
# handled separately (it's only valid before the handshake completes, not
# during normal operation), so it's deliberately not in this registry.
_CLIENT_EVENT_MODELS: dict[str, type[_Strict] | None] = {
    "user_text_input": UserTextInputPayload,
    "audio_stream_start": AudioStreamStartPayload,
    "audio_stream_chunk": AudioStreamChunkPayload,
    "audio_stream_end": AudioStreamEndPayload,
    "asset_swap_result": AssetSwapResultPayload,
    "speech_finished": SpeechFinishedPayload,
    "interaction_state": InteractionStatePayload,
    **dict.fromkeys(_RESERVED_NOOP_TYPES),
}

# Full AssetVersionEntry field set actually sent over the wire -- `seed`,
# `evaluated_triangles`, and `size_bytes` are internal-only, never sent to
# the client. `look_spec`/`warnings` are the two fields that differ between
# welcome (included) and hot_reload_asset (stripped) -- see
# `project_manifest()` below.
_WIRE_VERSION_FIELDS = (
    "version",
    "glb_filename",
    "sha256",
    "created_at",
    "status",
    "has_face",
    "speech_indicator_mode",
    "capabilities",
    "viseme_shape_keys",
    "animation_clips",
    "bounds",
)


def _project_version_entry(entry: AssetVersionEntry, *, include_look_spec_and_warnings: bool) -> dict[str, Any]:
    data = entry.model_dump(mode="json", include=set(_WIRE_VERSION_FIELDS))
    if include_look_spec_and_warnings:
        data["look_spec"] = entry.look_spec
        data["warnings"] = entry.warnings
    return data


def project_manifest(manifest: AssetManifest, *, include_look_spec_and_warnings: bool) -> dict[str, Any]:
    """The `welcome` vs. `hot_reload_asset` payload-shape difference is
    deliberate, not an oversight."""
    return {
        "current_version": manifest.current_version,
        "versions": [
            _project_version_entry(v, include_look_spec_and_warnings=include_look_spec_and_warnings)
            for v in manifest.versions
        ],
    }


def welcome_config_subset(settings: Settings) -> dict[str, Any]:
    """Only the fields the overlay actually needs -- not a dump of the
    whole PresenceConfig/ShortcutsConfig, most of which (dbus_service_name,
    idle_after_s, margin_px, ...) is brain-internal and none of the
    client's business."""
    return {
        "presence": {
            "avatar_width_px": settings.presence.avatar_width_px,
            "avatar_height_px": settings.presence.avatar_height_px,
            "show_speech_bubble": settings.presence.show_speech_bubble,
        },
        "shortcuts": settings.shortcuts.model_dump(),
        "voice": {"volume": settings.voice.tts.volume},
    }


# --- connection bookkeeping -----------------------------------------------------


@dataclass
class _ConnectionState:
    websocket: WebSocket
    client_id: str
    client_type: str
    capabilities: list[str]
    active_stream_id: str | None = field(default=None)


class HandshakeError(ValueError):
    """Raised internally when the hello handshake fails; always results in
    closing the socket with `_CLOSE_POLICY_VIOLATION`, never a crash."""


OnUserText = Callable[[str, str], Awaitable[None]]  # (client_id, text)
OnInteractionState = Callable[[str, str], Awaitable[None]]  # (client_id, state)
OnAssetSwapResult = Callable[[bool, int, str | None], Awaitable[None]]  # (ok, version, error)
OnSpeechFinished = Callable[[str, str], Awaitable[None]]  # (client_id, utterance_id)
OnClientConnected = Callable[[str], Awaitable[None]]  # (client_id) -- after welcome is sent


async def _noop_user_text(_client_id: str, _text: str) -> None:
    return None


async def _noop_interaction_state(_client_id: str, _state: str) -> None:
    return None


async def _noop_asset_swap_result(_ok: bool, _version: int, _error: str | None) -> None:
    return None


async def _noop_speech_finished(_client_id: str, _utterance_id: str) -> None:
    return None


async def _noop_client_connected(_client_id: str) -> None:
    return None


class OverlayHub:
    """Owns every live `/ws/overlay` connection. `get_manifest`/`streaming_stt`
    and the four `on_*` callbacks are all injected so this is fully testable
    without a real daemon behind it -- each defaults to a no-op/`None` so
    tests only need to supply what they're exercising."""

    def __init__(
        self,
        settings: Settings,
        *,
        get_manifest: Callable[[], AssetManifest | None] = lambda: None,
        streaming_stt: StreamingSTT | None = None,
        on_user_text_input: OnUserText = _noop_user_text,
        on_interaction_state: OnInteractionState = _noop_interaction_state,
        on_asset_swap_result: OnAssetSwapResult = _noop_asset_swap_result,
        on_speech_finished: OnSpeechFinished = _noop_speech_finished,
        on_client_connected: OnClientConnected = _noop_client_connected,
    ) -> None:
        self._settings = settings
        self._get_manifest = get_manifest
        self._streaming_stt = streaming_stt
        self._on_user_text_input = on_user_text_input
        self._on_interaction_state = on_interaction_state
        self._on_asset_swap_result = on_asset_swap_result
        self._on_speech_finished = on_speech_finished
        self._on_client_connected = on_client_connected
        self._connections: dict[str, _ConnectionState] = {}

    @property
    def connected_client_ids(self) -> list[str]:
        return list(self._connections)

    def _validate_origin_and_host(self, websocket: WebSocket) -> str | None:
        """Returns a rejection reason string, or None if the connection
        should proceed. Checked before `accept()`."""
        origin = websocket.headers.get("origin")
        if origin is not None and origin not in self._settings.server.allowed_origins:
            return f"Origin {origin!r} not in allowed_origins"

        host = websocket.headers.get("host")
        port = self._settings.server.port
        allowed_hosts = (f"127.0.0.1:{port}", f"localhost:{port}")
        if host not in allowed_hosts:
            return f"Host {host!r} not in {allowed_hosts} (DNS-rebinding guard)"
        return None

    async def handle_connection(self, websocket: WebSocket) -> None:
        """The full per-connection lifecycle. Never raises -- every failure
        path closes the socket with a reason logged, matching this
        codebase's "a bad client must never crash the daemon" convention."""
        rejection = self._validate_origin_and_host(websocket)
        if rejection is not None:
            logger.warning("rejecting /ws/overlay connection: %s", rejection)
            await websocket.close(code=_CLOSE_POLICY_VIOLATION)
            return

        await websocket.accept()

        try:
            hello = await self._complete_handshake(websocket)
        except HandshakeError as exc:
            logger.warning("handshake failed: %s", exc)
            await _safe_close(websocket, _CLOSE_POLICY_VIOLATION)
            return

        existing = self._connections.get(hello.client_id)
        if existing is not None:
            logger.info("client_id %r reconnected; closing the previous socket", hello.client_id)
            await _safe_close(existing.websocket, _CLOSE_CLIENT_ID_REPLACED)
            self._connections.pop(hello.client_id, None)

        state = _ConnectionState(
            websocket=websocket, client_id=hello.client_id, client_type=hello.client_type, capabilities=hello.capabilities
        )
        self._connections[hello.client_id] = state
        await self._send_welcome(websocket)
        logger.info("client %r connected (%d total)", hello.client_id, len(self._connections))
        # Lets the daemon push state the client can't know yet (e.g. the
        # current avatar placement) -- a failure here must not drop the
        # connection that just completed its handshake.
        try:
            await self._on_client_connected(hello.client_id)
        except Exception:
            logger.exception("on_client_connected callback failed for %r", hello.client_id)

        try:
            await self._receive_loop(state)
        except WebSocketDisconnect:
            pass
        finally:
            if self._connections.get(hello.client_id) is state:
                self._connections.pop(hello.client_id, None)
            logger.info("client %r disconnected (%d remaining)", hello.client_id, len(self._connections))

    async def _complete_handshake(self, websocket: WebSocket) -> HelloPayload:
        try:
            raw = await asyncio.wait_for(websocket.receive_text(), timeout=_HELLO_TIMEOUT_S)
        except TimeoutError as exc:
            raise HandshakeError("no hello message received within timeout") from exc
        except WebSocketDisconnect as exc:
            raise HandshakeError("client disconnected before sending hello") from exc

        try:
            envelope = Envelope.model_validate(json.loads(raw))
        except (ValidationError, ValueError) as exc:
            raise HandshakeError(f"malformed envelope: {exc}") from exc

        if envelope.type != "hello":
            raise HandshakeError(f"expected 'hello' as the first message, got {envelope.type!r}")

        try:
            hello = HelloPayload.model_validate(envelope.payload)
        except ValidationError as exc:
            raise HandshakeError(f"malformed hello payload: {exc}") from exc

        if hello.protocol_version != PROTOCOL_VERSION:
            raise HandshakeError(
                f"unsupported protocol_version {hello.protocol_version} (server supports {PROTOCOL_VERSION})"
            )
        return hello

    async def _send_welcome(self, websocket: WebSocket) -> None:
        manifest = self._get_manifest()
        manifest_payload = (
            project_manifest(manifest, include_look_spec_and_warnings=True)
            if manifest is not None
            else {"current_version": 0, "versions": []}
        )
        await websocket.send_json(
            _envelope(
                "welcome",
                {
                    "protocol_version": PROTOCOL_VERSION,
                    "asset_dir": str(self._settings.current_dir),
                    "manifest": manifest_payload,
                    "config": welcome_config_subset(self._settings),
                },
            )
        )

    async def _receive_loop(self, state: _ConnectionState) -> None:
        while True:
            message = await state.websocket.receive()
            if message["type"] == "websocket.disconnect":
                raise WebSocketDisconnect(message.get("code", 1000), message.get("reason"))
            if "bytes" in message and message["bytes"] is not None:
                self._handle_binary_frame(state, message["bytes"])
                continue
            raw = message.get("text")
            if raw is None:
                continue
            try:
                envelope = Envelope.model_validate(json.loads(raw))
            except (ValidationError, ValueError) as exc:
                logger.warning("client %r sent a malformed envelope: %s", state.client_id, exc)
                continue
            try:
                await self._dispatch(state, envelope)
            except Exception:
                logger.exception("error handling %r from client %r", envelope.type, state.client_id)

    def _handle_binary_frame(self, state: _ConnectionState, data: bytes) -> None:
        """Raw PCM16LE audio frames carry no header of their own -- routed
        by whichever stream_id was most recently announced via
        audio_stream_start on this connection."""
        if self._streaming_stt is None or state.active_stream_id is None:
            logger.warning(
                "client %r sent a binary audio frame with no active stream (missing audio_stream_start?)",
                state.client_id,
            )
            return
        self._streaming_stt.feed_chunk(state.active_stream_id, data)

    async def _dispatch(self, state: _ConnectionState, envelope: Envelope) -> None:
        if envelope.type in _RESERVED_NOOP_TYPES:
            logger.debug("ignoring reserved event %r from client %r", envelope.type, state.client_id)
            return
        model = _CLIENT_EVENT_MODELS.get(envelope.type)
        if model is None:
            logger.warning("client %r sent an unrecognized event type %r; ignoring", state.client_id, envelope.type)
            return

        payload = model.model_validate(envelope.payload)
        if isinstance(payload, UserTextInputPayload):
            await self._on_user_text_input(state.client_id, payload.text)
        elif isinstance(payload, InteractionStatePayload):
            await self._on_interaction_state(state.client_id, payload.state)
        elif isinstance(payload, AssetSwapResultPayload):
            await self._on_asset_swap_result(payload.ok, payload.version, payload.error)
        elif isinstance(payload, SpeechFinishedPayload):
            await self._on_speech_finished(state.client_id, payload.utterance_id)
        elif isinstance(payload, AudioStreamStartPayload):
            state.active_stream_id = payload.stream_id
            if self._streaming_stt is not None:
                self._streaming_stt.start_stream(payload.stream_id, sample_rate=payload.sample_rate)
        elif isinstance(payload, AudioStreamChunkPayload):
            pass  # the JSON header is purely sequencing metadata; the real bytes arrive as the next binary frame
        elif isinstance(payload, AudioStreamEndPayload):
            await self._finalize_audio_stream(state, payload.stream_id)

    async def _finalize_audio_stream(self, state: _ConnectionState, stream_id: str) -> None:
        if self._streaming_stt is None:
            return
        if state.active_stream_id == stream_id:
            state.active_stream_id = None
        try:
            _backend_name, text = await self._streaming_stt.end_stream(stream_id)
        except Exception:
            logger.exception("failed to finalize audio stream %r for client %r", stream_id, state.client_id)
            return
        if text:
            await self._on_user_text_input(state.client_id, text)

    @property
    def settings(self) -> Settings:
        return self._settings

    def update_settings(self, settings: Settings) -> None:
        """Swap in reloaded settings, so a client connecting later gets the
        current `welcome.config` and not the one from daemon startup."""
        self._settings = settings

    async def broadcast(self, event_type: str, payload: dict[str, Any]) -> None:
        """Pushed from elsewhere in the daemon (chat replies, appearance
        reveals, mood updates, ...)."""
        envelope = _envelope(event_type, payload)
        dead_client_ids = []
        for client_id, state in self._connections.items():
            try:
                await state.websocket.send_json(envelope)
            except Exception:  # noqa: BLE001 -- one dead socket must never stop the rest of the broadcast
                dead_client_ids.append(client_id)
        for client_id in dead_client_ids:
            self._connections.pop(client_id, None)


def _envelope(event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    return {"type": event_type, "timestamp": time.time(), "payload": payload}


async def _safe_close(websocket: WebSocket, code: int) -> None:
    if websocket.client_state != WebSocketState.DISCONNECTED:
        try:
            await websocket.close(code=code)
        except Exception:
            logger.debug("ignoring error closing an already-gone socket", exc_info=True)
