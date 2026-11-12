"""Entrypoint: wires config, personality, routing, appearance evolution,
voice, presence, and the overlay IPC hub into one running FastAPI app, plus
the background loops (periodic reflection + autonomous-redesign
eligibility, mood decay, the desktop presence bridge, ephemeral-audio GC).

Run directly (`python -m brain.main`) or via the systemd unit in
deploy/systemd/deskmate-brain.service.

HTTP routes also double as a manual test surface --
`curl -X POST localhost:8765/chat -d '{"text": "hi"}'` exercises the whole
router/personality/voice pipeline without a WebSocket client.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, WebSocket
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from brain.app_state import AppState
from brain.appearance.blender_gen import is_build_running
from brain.appearance.build_options import build_options
from brain.appearance.manifest import load_manifest
from brain.appearance.preview import PreviewBusy, clear_previews
from brain.appearance.spec import (
    CatalogError,
    LookSpec,
    validate_against_catalogs,
)
from brain.appearance.thumbnails import ensure_thumbnails
from brain.background import (
    _gc_loop,
    _mood_broadcast_loop,
    _mood_decay_loop,
    _reflection_loop,
)
from brain.config import (
    MOOD_NAMES,
    TRAIT_NAMES,
    Settings,
    get_settings,
    subscribe_if_running,
)
from brain.events import bus, emit
from brain.evolution import AvatarEvolution
from brain.ipc.server import OverlayHub, welcome_config_subset
from brain.llm_pool import LLMPool, LocalLLMClient
from brain.personality.reflect import run_reflection
from brain.personality.state import PersonalityStore
from brain.pipeline import (
    _broadcast_set_position,
    _deliver_pending_greeting,
    _handle_appearance_progress,
    _handle_chat_text,
    _handle_presence_transition,
    _handle_presence_update,
)
from brain.presence.desktop_bridge import DesktopPresenceBridge, PresenceState
from brain.presence.placement import (
    ScreenInfo,
    WindowState,
)
from brain.router import build_router
from brain.skills.agentskills import discover as discover_agent_skills
from brain.skills.builtin import register_builtin
from brain.skills.manage import (
    StateChangeError,
    build_views,
    change_state,
    summarize,
    view_json,
)
from brain.skills.registry import SkillRegistry
from brain.skills.state import SkillStateStore
from brain.voice.lipsync import RhubarbLipSync
from brain.voice.stt import StreamingSTT, build_stt_coordinator
from brain.voice.tts import build_tts_pool
from brain.voice.voice_select import SeedVoiceSelector

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
logger = logging.getLogger("deskmate.main")


def build_app_state(settings: Settings | None = None) -> AppState:
    settings = settings or get_settings()
    settings.ensure_dirs()

    store = PersonalityStore(
        settings.personality_db_path,
        TRAIT_NAMES,
        MOOD_NAMES,
        identity_name=settings.personality.identity_name,
    )
    local_pool = LLMPool([("local", LocalLLMClient(settings.llm.local), [])])
    router = build_router(settings)

    desktop_bridge: DesktopPresenceBridge | None = None

    def _get_presence_state() -> PresenceState:
        return (
            desktop_bridge.state
            if desktop_bridge is not None
            else PresenceState.ACTIVE
        )

    async def _on_appearance_progress(payload: dict) -> None:
        await _handle_appearance_progress(state_holder["state"], payload)

    evolution = AvatarEvolution(
        settings,
        store,
        local_pool,
        get_presence_state=_get_presence_state,
        on_progress=_on_appearance_progress,
    )
    evolution.wire_tools(router)

    tts_pool = build_tts_pool(settings)
    stt_coordinator = build_stt_coordinator(settings)
    streaming_stt = StreamingSTT(
        stt_coordinator,
        silence_threshold_ms=settings.voice.stt.silence_threshold_ms,
    )
    lipsync = RhubarbLipSync(
        settings.voice.rhubarb_bin, settings.voice.lipsync_recognizer
    )

    state_holder: dict[str, AppState] = {}

    async def _on_user_text_input(_client_id: str, text: str) -> None:
        await _handle_chat_text(state_holder["state"], text)

    async def _on_interaction_state(
        _client_id: str, interaction_state: str
    ) -> None:
        state_holder["state"].interaction_active = interaction_state == "active"

    async def _on_asset_swap_result(
        ok: bool, version: int, error: str | None
    ) -> None:
        if ok:
            logger.info(
                "overlay reported asset_swap_result ok for version %s", version
            )
        else:
            logger.warning(
                "overlay reported asset_swap_result failure for version %s: %s",
                version,
                error,
            )
            state_holder["state"].evolution.handle_asset_swap_failure(version)

    async def _on_speech_finished(_client_id: str, utterance_id: str) -> None:
        logger.debug("speech_finished: %s", utterance_id)

    async def _on_overlay_connected(_client_id: str) -> None:
        last = state_holder["state"].last_presence
        if last is not None:
            await _broadcast_set_position(state_holder["state"], *last)
        _deliver_pending_greeting(state_holder["state"])

    hub = OverlayHub(
        settings,
        get_manifest=lambda: load_manifest(settings.deploy_dir),
        streaming_stt=streaming_stt,
        on_user_text_input=_on_user_text_input,
        on_interaction_state=_on_interaction_state,
        on_asset_swap_result=_on_asset_swap_result,
        on_speech_finished=_on_speech_finished,
        on_client_connected=_on_overlay_connected,
    )

    if settings.presence.dbus_service_name:

        async def _on_presence_update(
            window: WindowState | None, screens: list[ScreenInfo]
        ) -> None:
            await _handle_presence_update(
                state_holder["state"], window, screens
            )

        async def _on_presence_transition(
            old_state: PresenceState, new_state: PresenceState
        ) -> None:
            await _handle_presence_transition(
                state_holder["state"], old_state, new_state
            )

        desktop_bridge = DesktopPresenceBridge(
            settings.presence.dbus_service_name,
            settings.presence.dbus_object_path,
            settings.presence.idle_after_s,
            _on_presence_update,
            on_transition=_on_presence_transition,
        )

    skills = SkillRegistry(
        disabled=set(settings.skills.disabled),
        max_actions=settings.skills.max_actions_per_reply,
    )
    register_builtin(skills)

    state = AppState(
        settings=settings,
        store=store,
        router=router,
        evolution=evolution,
        local_pool=local_pool,
        tts_pool=tts_pool,
        streaming_stt=streaming_stt,
        lipsync=lipsync,
        voice_selector=SeedVoiceSelector(
            settings, store, evolution.current_look_spec
        ),
        hub=hub,
        desktop_bridge=desktop_bridge,
        skills=skills,
        skill_state=SkillStateStore(settings.personality_db_path),
    )
    state_holder["state"] = state
    return state


# --- app lifecycle ---------------------------------------------------------------


async def _apply_reloaded_settings(
    state: AppState, new_settings: Settings
) -> None:
    """After a config.toml reload: keep what the hub hands to *new* clients
    current, and push the overlay-relevant subset (`config_update`, the same
    shape as `welcome.config`) to connected clients -- but only when it
    actually changed, so editing an unrelated key doesn't spam the overlay."""
    changed = welcome_config_subset(new_settings) != welcome_config_subset(
        state.hub.settings
    )
    state.hub.update_settings(new_settings)
    if changed:
        await state.hub.broadcast(
            "config_update", welcome_config_subset(new_settings)
        )


async def _render_missing_look_thumbnails(state: AppState) -> None:
    """Gives every look version a picture for the status page's Looks tab,
    including ones whose .glb is long pruned (rebuilt from the stored spec)."""
    try:
        await ensure_thumbnails(state.settings)
    except Exception:
        logger.exception("look thumbnail backfill failed")


@asynccontextmanager
async def lifespan(app: FastAPI):
    state = build_app_state()
    app.state.deskmate = state

    # The config watcher calls back on its own thread; hop onto the event loop.
    loop = asyncio.get_running_loop()

    def _on_settings_reloaded(new_settings: Settings) -> None:
        try:
            loop.call_soon_threadsafe(
                lambda: asyncio.create_task(
                    _apply_reloaded_settings(state, new_settings)
                )
            )
        except RuntimeError:  # loop already closed (shutdown race)
            pass

    unsubscribe_settings = subscribe_if_running(_on_settings_reloaded)

    state.evolution.reconcile_staged_on_boot()
    clear_previews(
        state.settings
    )  # a preview never outlives the process that made it

    background = [
        asyncio.create_task(_render_missing_look_thumbnails(state)),
        asyncio.create_task(_reflection_loop(state)),
        asyncio.create_task(_mood_decay_loop(state)),
        asyncio.create_task(_mood_broadcast_loop(state)),
        asyncio.create_task(_gc_loop(state)),
    ]
    if state.desktop_bridge is not None:
        background.append(asyncio.create_task(state.desktop_bridge.run()))
    state.background_tasks = background

    logger.info("deskmate brain daemon up: %s", state.store.get_identity_name())
    try:
        yield
    finally:
        unsubscribe_settings()
        state.previews.discard_all()
        for task in background:
            task.cancel()
        await asyncio.gather(*background, return_exceptions=True)
        await state.router.aclose()
        await state.local_pool.aclose()
        await state.tts_pool.aclose()
        state.store.close()
        state.skill_state.close()


app = FastAPI(title="deskmate brain", lifespan=lifespan)

# Set when the daemon is told to stop: the event stream for the status page ends at once.
_stopping = asyncio.Event()


def _state(app_: FastAPI) -> AppState:
    return app_.state.deskmate


# --- REST surface -----------------------------------------------------------


class ChatRequest(BaseModel):
    text: str
    speak: bool = True


@app.get("/health")
async def health():
    state = _state(app)
    presence = (
        state.desktop_bridge.state
        if state.desktop_bridge is not None
        else PresenceState.ACTIVE
    )
    return {
        "status": "ok",
        "name": state.store.get_identity_name(),
        "active_voice": _describe_active_voice(
            state.settings, state.voice_selector
        ),
        "voice_degraded": state.tts_pool.is_degraded,
        "presence": presence,
        "traits": state.store.get_traits(),
        "mood": state.store.get_mood(),
    }


_STARTED_AT = time.time()


@app.get("/api/skills")
async def api_skills():
    """Read-only view of the skill registry for the status page: every
    registered skill (enabled or not), its parameters, usage counters, and the
    exact block the model is given."""
    state = _state(app)
    enabled = state.settings.skills.enabled
    installed, rejected = discover_agent_skills(state.settings.skills_dir)
    views = build_views(
        state.settings.skills_dir, installed, state.skill_state.all()
    )
    return {
        "enabled": enabled,
        "max_actions_per_reply": state.skills.max_actions,
        "skills": state.skills.describe(),
        "prompt_block": state.skills.prompt_block() if enabled else "",
        # Agent Skills (SKILL.md) installed from other people: catalogued, never run and never put in the prompt
        # until the agent runtime exists. `budget` is what their pinned stubs would cost.
        "installed": [view_json(v) for v in views],
        "rejected": rejected,
        "budget": summarize(
            state.settings.skills,
            state.settings.llm.smallest_context_tokens(),
            views,
        ),
    }


class SkillStateRequest(BaseModel):
    skill: str
    enabled: bool | None = None
    pinned: bool | None = None
    alias: str | None = None
    # The client must really have asked the user (typed or clicked, never spoken). A flagged skill also needs its
    # name typed back in `confirm_text`. The server can only check that these are present.
    confirmed: bool = False
    confirm_text: str = ""


@app.post("/api/skills/state")
async def api_skill_state(req: SkillStateRequest):
    """Enable, disable, pin or alias an installed Agent Skill. Not reachable by the model: no typed action calls
    this, and replies are only ever parsed into registry skills."""
    state = _state(app)
    installed, _ = discover_agent_skills(state.settings.skills_dir)
    views = build_views(
        state.settings.skills_dir, installed, state.skill_state.all()
    )
    try:
        changed = change_state(
            state.skill_state,
            views,
            req.skill,
            enabled=req.enabled,
            pinned=req.pinned,
            alias=req.alias,
            confirmed=req.confirmed,
            confirm_text=req.confirm_text,
            changed_by="api",
            max_pinned=state.settings.skills.max_pinned,
        )
    except StateChangeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    emit(
        "skill.state",
        {
            "skill": changed.skill_id,
            "enabled": changed.enabled,
            "pinned": changed.pinned,
            "by": "api",
        },
    )
    return {
        "skill": changed.skill_id,
        "enabled": changed.enabled,
        "pinned": changed.pinned,
        "alias": changed.alias,
    }


@app.get("/api/events")
async def api_events(request: Request, since: int = 0):
    """Server-sent stream of what the brain is doing (see brain/events.py):
    the retained backlog after `since` (or Last-Event-ID), then live events.
    Read-only; for the status page."""
    last_id = request.headers.get("last-event-id")
    if last_id and last_id.isdigit():
        since = max(since, int(last_id))

    async def stream():
        with bus.subscribe() as queue:
            sent = since
            for event in bus.since(since):
                sent = event["seq"]
                yield f"id: {sent}\ndata: {json.dumps(event)}\n\n"
            stopping = asyncio.ensure_future(_stopping.wait())
            try:
                while not _stopping.is_set():
                    getting = asyncio.ensure_future(queue.get())
                    done, _ = await asyncio.wait(
                        {getting, stopping},
                        timeout=15.0,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    if getting not in done:
                        getting.cancel()
                        if stopping in done:
                            return  # the daemon is stopping: end the stream so it can
                        if await request.is_disconnected():
                            return
                        yield ": keepalive\n\n"
                        continue
                    event = getting.result()
                    if (
                        event["seq"] <= sent
                    ):  # already replayed from the backlog
                        continue
                    sent = event["seq"]
                    yield f"id: {sent}\ndata: {json.dumps(event)}\n\n"
            finally:
                stopping.cancel()

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store"},
    )


@app.get("/api/status")
async def api_status():
    """Read-only runtime state that only exists in this process's memory, for
    the standalone status page (status_page/). Everything on disk it reads
    for itself."""
    state = _state(app)
    placement = state.last_placement
    window, screens = (
        state.last_presence if state.last_presence is not None else (None, [])
    )
    manifest = load_manifest(state.settings.deploy_dir)
    return {
        "started_at": _STARTED_AT,
        "overlay_clients": state.hub.connected_client_ids,
        "interaction_active": state.interaction_active,
        "build_running": is_build_running(),
        "placement": (
            None
            if placement is None
            else {
                "x": placement.x,
                "y": placement.y,
                "screen": placement.screen_name,
                "hidden": placement.hidden,
            }
        ),
        "window": (
            None
            if window is None
            else {
                "x": window.rect.x,
                "y": window.rect.y,
                "w": window.rect.w,
                "h": window.rect.h,
                "screen": window.screen_name,
                "fullscreen": window.fullscreen,
            }
        ),
        "screens": [
            {
                "name": s.name,
                "x": s.rect.x,
                "y": s.rect.y,
                "w": s.rect.w,
                "h": s.rect.h,
            }
            for s in screens
        ],
        "look": {
            "current_version": (
                manifest.current_version if manifest is not None else None
            ),
            "versions": len(manifest.versions) if manifest is not None else 0,
            "summary": state.evolution.current_look_summary(),
        },
    }


def _describe_active_voice(
    settings: Settings, selector: SeedVoiceSelector
) -> str:
    """The seed voice the companion has persisted (agent_state.current_voice_id,
    written by SeedVoiceSelector) when remote seed voices are in use;
    otherwise derived from the real active TTS config."""
    tts_config = settings.voice.tts
    if tts_config.active_backend == "remote":
        stored = selector.current_voice_id()
        if stored and any(
            stored.startswith(f"remote:{name}:seed:")
            for name in tts_config.remote
        ):
            return stored
        enabled = tts_config.enabled_remote_instances()
        if enabled:
            name = next(
                (n for n, e in tts_config.remote.items() if e is enabled[0]),
                "remote",
            )
            return f"remote:{name}"
    return f"piper:{Path(tts_config.local.voice_model_path).stem}"


@app.post("/chat")
async def chat(req: ChatRequest):
    state = _state(app)
    utterance_id = await _handle_chat_text(state, req.text, speak=req.speak)
    return {"status": "ok", "utterance_id": utterance_id}


@app.get("/appearance/options")
async def appearance_options():
    """What the Build page may offer: per usable body, which controls apply and
    their choices, with the reason when one does not (docs/ADR_BUILD_YOUR_LOOK.md).
    Read-only; reads the deploy directory and runs nothing."""
    state = _state(app)
    return await asyncio.to_thread(build_options, state.settings.deploy_dir)


@app.post("/appearance/preview", status_code=202)
async def appearance_preview(look_spec: LookSpec):
    """Starts a throwaway build of `look_spec` and a picture of it. Nothing is
    published and the live look is untouched (docs/ADR_BUILD_YOUR_LOOK.md)."""
    state = _state(app)
    try:
        validate_against_catalogs(look_spec, state.settings.deploy_dir)
    except CatalogError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    try:
        preview = state.previews.start(state.settings, look_spec)
    except PreviewBusy as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    return preview.public()


@app.get("/appearance/preview/{preview_id}")
async def appearance_preview_status(preview_id: str):
    preview = _state(app).previews.get(preview_id)
    if preview is None:
        raise HTTPException(status_code=404, detail="no such preview")
    return preview.public()


@app.get("/appearance/preview/{preview_id}/picture")
async def appearance_preview_picture(preview_id: str):
    preview = _state(app).previews.get(preview_id)
    picture = preview.picture if preview is not None else None
    if picture is None or not picture.is_file():
        raise HTTPException(
            status_code=404, detail="no picture for that preview"
        )
    return FileResponse(
        picture, media_type="image/png", headers={"Cache-Control": "no-store"}
    )


@app.delete("/appearance/preview/{preview_id}")
async def appearance_preview_discard(preview_id: str):
    if not _state(app).previews.discard(preview_id):
        raise HTTPException(status_code=404, detail="no such preview")
    return {"status": "discarded"}


@app.post("/appearance/generate", status_code=202)
async def appearance_generate(look_spec: LookSpec):
    state = _state(app)
    try:
        validate_against_catalogs(look_spec, state.settings.deploy_dir)
    except CatalogError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None

    state.evolution.schedule_generate_from_full_spec(look_spec)
    return {"status": "building", "task": "appearance_generation"}


@app.post("/appearance/revert")
async def appearance_revert():
    state = _state(app)
    before = load_manifest(state.settings.deploy_dir)
    before_version = before.current_version if before is not None else None
    message = await state.evolution.revert_appearance()
    after = load_manifest(state.settings.deploy_dir)
    if after is not None and after.current_version != before_version:
        return {"status": "ok", "reverted_to_version": after.current_version}
    return {"status": "error", "message": message}


@app.post("/reflect")
async def reflect():
    state = _state(app)
    result = await run_reflection(state.store, state.local_pool)
    return {
        "status": "ok",
        "notes_created": result.notes_created,
        "trait_deltas": result.trait_deltas,
    }


@app.get("/audio/{filename}")
async def get_audio(filename: str):
    state = _state(app)
    if "/" in filename or "\\" in filename or not filename.endswith(".wav"):
        raise HTTPException(status_code=404)
    path = (state.settings.voice_cache_dir / filename).resolve()
    if (
        path.parent != state.settings.voice_cache_dir.resolve()
        or not path.is_file()
    ):
        raise HTTPException(status_code=404)
    return FileResponse(path, media_type="audio/wav")


@app.get("/assets/{filename}")
async def get_asset(filename: str):
    state = _state(app)
    if "/" in filename or "\\" in filename:
        raise HTTPException(status_code=404)
    path = (state.settings.current_dir / filename).resolve()
    if (
        path.parent != state.settings.current_dir.resolve()
        or not path.is_file()
    ):
        raise HTTPException(status_code=404)
    return FileResponse(path, media_type="model/gltf-binary")


@app.websocket("/ws/overlay")
async def ws_overlay(websocket: WebSocket):
    await _state(app).hub.handle_connection(websocket)


def run() -> None:
    import uvicorn

    class _Server(uvicorn.Server):
        async def shutdown(self, sockets=None) -> None:
            # uvicorn waits for every open connection to close before it stops, and the overlay's
            # socket and the status page's event stream never do: closing them is what lets it stop
            # at once instead of at systemd's timeout.
            # (run as `python -m brain.main` this file is `__main__`, and uvicorn serves the
            # `brain.main` it imports itself: the app and the flag to set are that one's)
            served = importlib.import_module("brain.main")
            served._stopping.set()
            state = getattr(served.app.state, "deskmate", None)
            if state is not None:
                await state.hub.close_all()
            await super().shutdown(sockets=sockets)

    settings = get_settings()
    _Server(
        uvicorn.Config(
            "brain.main:app",
            host=settings.server.host,
            port=settings.server.port,
            log_level="info",
            # whatever is still open after this is closed, so a stop is never left to be killed
            timeout_graceful_shutdown=10,
        )
    ).run()


if __name__ == "__main__":
    run()
