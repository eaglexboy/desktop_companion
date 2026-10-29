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
import json
import logging
import random
import re
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, WebSocket
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from brain.appearance.blender_gen import is_build_running
from brain.appearance.manifest import load_manifest
from brain.appearance.spec import CatalogError, LookSpec, validate_against_catalogs
from brain.config import (
    MOOD_NAMES,
    TRAIT_NAMES,
    Settings,
    get_settings,
    subscribe_if_running,
)
from brain.events import begin_turn, bus, emit
from brain.evolution import AvatarEvolution
from brain.ipc.server import OverlayHub, project_manifest, welcome_config_subset
from brain.llm_pool import LLMPool, LocalLLMClient
from brain.personality.prompts import build_system_prompt, rank_memory_notes
from brain.personality.reflect import maybe_trigger_autonomous_redesign, run_reflection
from brain.personality.state import PersonalityStore
from brain.presence.desktop_bridge import DesktopPresenceBridge, PresenceState
from brain.presence.placement import (
    Placement,
    ScreenInfo,
    WindowState,
    choose_position,
    is_own_window,
)
from brain.router import ReplyFormat, Router, build_router
from brain.skills.agentskills import discover as discover_agent_skills
from brain.skills.builtin import register_builtin
from brain.skills.manage import (
    StateChangeError,
    build_views,
    change_state,
    summarize,
    view_json,
)
from brain.skills.registry import SkillCall, SkillRegistry
from brain.skills.reply import (
    FORMAT_REMINDER,
    REPAIR_PROMPT,
    is_structured,
    parse_reply,
)
from brain.skills.state import SkillStateStore
from brain.voice.delivery import pick_delivery
from brain.voice.lipsync import RhubarbLipSync, extract_visemes_if_needed
from brain.voice.plaintext import to_plain_speech
from brain.voice.stereo import ensure_stereo_wav
from brain.voice.stt import StreamingSTT, build_stt_coordinator
from brain.voice.tts import TTSPool, build_tts_pool
from brain.voice.voice_select import SeedVoiceSelector

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("deskmate.main")

_GC_INTERVAL_S = 300.0
_GC_TTL_S = 600.0
_LONG_REPLY_CHARS = 280  # pick_delivery's is_long_or_code heuristic


@dataclass
class AppState:
    settings: Settings
    store: PersonalityStore
    router: Router
    evolution: AvatarEvolution
    local_pool: LLMPool
    tts_pool: TTSPool
    streaming_stt: StreamingSTT
    lipsync: RhubarbLipSync
    voice_selector: SeedVoiceSelector
    hub: OverlayHub
    desktop_bridge: DesktopPresenceBridge | None
    skills: SkillRegistry
    skill_state: SkillStateStore
    interaction_active: bool = False
    # Last window/screen telemetry from the sensing adapter, kept so a
    # (re)connecting overlay can be placed immediately instead of waiting
    # for the next window change.
    last_presence: tuple[WindowState | None, list[ScreenInfo]] | None = None
    last_placement: Placement | None = None
    background_tasks: list[asyncio.Task] = field(default_factory=list)


def build_app_state(settings: Settings | None = None) -> AppState:
    settings = settings or get_settings()
    settings.ensure_dirs()

    store = PersonalityStore(
        settings.personality_db_path, TRAIT_NAMES, MOOD_NAMES, identity_name=settings.personality.identity_name
    )
    local_pool = LLMPool([("local", LocalLLMClient(settings.llm.local), [])])
    router = build_router(settings)

    desktop_bridge: DesktopPresenceBridge | None = None

    def _get_presence_state() -> PresenceState:
        return desktop_bridge.state if desktop_bridge is not None else PresenceState.ACTIVE

    async def _on_appearance_progress(payload: dict) -> None:
        await _handle_appearance_progress(state_holder["state"], payload)

    evolution = AvatarEvolution(
        settings, store, local_pool, get_presence_state=_get_presence_state, on_progress=_on_appearance_progress
    )
    evolution.wire_tools(router)

    tts_pool = build_tts_pool(settings)
    stt_coordinator = build_stt_coordinator(settings)
    streaming_stt = StreamingSTT(stt_coordinator, silence_threshold_ms=settings.voice.stt.silence_threshold_ms)
    lipsync = RhubarbLipSync(settings.voice.rhubarb_bin, settings.voice.lipsync_recognizer)

    state_holder: dict[str, AppState] = {}

    async def _on_user_text_input(_client_id: str, text: str) -> None:
        await _handle_chat_text(state_holder["state"], text)

    async def _on_interaction_state(_client_id: str, interaction_state: str) -> None:
        state_holder["state"].interaction_active = interaction_state == "active"

    async def _on_asset_swap_result(ok: bool, version: int, error: str | None) -> None:
        if not ok:
            logger.warning("overlay reported asset_swap_result failure for version %s: %s", version, error)
            state_holder["state"].evolution.handle_asset_swap_failure(version)

    async def _on_speech_finished(_client_id: str, utterance_id: str) -> None:
        logger.debug("speech_finished: %s", utterance_id)

    async def _on_overlay_connected(_client_id: str) -> None:
        last = state_holder["state"].last_presence
        if last is not None:
            await _broadcast_set_position(state_holder["state"], *last)

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
        async def _on_presence_update(window: WindowState | None, screens: list[ScreenInfo]) -> None:
            await _handle_presence_update(state_holder["state"], window, screens)

        async def _on_presence_transition(old_state: PresenceState, new_state: PresenceState) -> None:
            await _handle_presence_transition(state_holder["state"], old_state, new_state)

        desktop_bridge = DesktopPresenceBridge(
            settings.presence.dbus_service_name,
            settings.presence.dbus_object_path,
            settings.presence.idle_after_s,
            _on_presence_update,
            on_transition=_on_presence_transition,
        )

    skills = SkillRegistry(
        disabled=set(settings.skills.disabled), max_actions=settings.skills.max_actions_per_reply
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
        voice_selector=SeedVoiceSelector(settings, store, evolution.current_look_spec),
        hub=hub,
        desktop_bridge=desktop_bridge,
        skills=skills,
        skill_state=SkillStateStore(settings.personality_db_path),
    )
    state_holder["state"] = state
    return state


# --- shared chat/speech pipeline (used by both POST /chat and the WS
# user_text_input/audio-transcription paths) -----------------------------------


def _is_long_or_code(text: str) -> bool:
    return len(text) > _LONG_REPLY_CHARS or "```" in text


# Sentence terminators: a run of non-terminator characters followed by one or
# more of . ! ? -- the same boundary rule the TalkWithMe reference client uses
# to keep each TTS request short enough to finish inside a remote timeout.
_SENTENCE_RE = re.compile(r"[^.!?]*[.!?]+")


def split_sentences(text: str) -> list[str]:
    """Splits a reply into speakable sentences; a trailing fragment with no
    terminator is kept as its own final sentence. Fragments with no word
    characters (a stray "..." or "!") are dropped -- there is nothing to say."""
    sentences: list[str] = []
    last_end = 0
    for match in _SENTENCE_RE.finditer(text):
        sentences.append(match.group(0).strip())
        last_end = match.end()
    sentences.append(text[last_end:].strip())
    return [s for s in sentences if re.search(r"\w", s)]


async def _speak(state: AppState, text: str, *, utterance_id: str | None = None) -> None:
    """Speaks a reply one sentence at a time. Each sentence is its own TTS
    request, lip-sync pass, and `speech` envelope (`chunk_seq`, `final`), sent
    as soon as it is ready -- so the first sentence plays while the next is
    still synthesizing, and no single request has to fit a whole reply inside
    a remote engine's timeout.

    Synthesis is strictly sequential (one engine, one GPU), but each
    sentence's lip-sync runs in the background while the *next* sentence
    synthesizes, and envelopes are broadcast in order as each lip-sync
    finishes. Doing lip-sync inline (as an earlier version did) added its
    full duration to every sentence's latency and left audible gaps."""
    if state.tts_pool.is_empty:
        return
    utterance_id = utterance_id or f"utt_{int(time.time() * 1000)}"
    sentences = split_sentences(text)
    delivery = pick_delivery(state.store.get_traits(), state.store.get_mood(), _is_long_or_code(text))
    has_face = state.evolution.current_look_spec().has_face
    # Chosen once per utterance (never mid-sentence); a failure here must not
    # stop speech -- the pool just uses each engine's own default voice.
    remote_params = None
    try:
        selection = state.voice_selector.select()
        if selection is not None:
            remote_params = {selection.instance: {"seed": selection.seed}}
    except Exception:
        logger.exception("voice selection failed for utterance %r; using default voice", utterance_id)

    async def lipsync(wav_path: Path, sentence: str, chunk_seq: int) -> list:
        # Lipsync is a separate failure domain from synthesis: rhubarb being
        # unavailable (or erroring on this particular clip) must not throw away
        # perfectly good audio that already exists on disk -- degrade to no
        # visemes (a static mouth) rather than skipping the whole broadcast.
        try:
            return await extract_visemes_if_needed(state.lipsync, wav_path, has_face=has_face, dialog_text=sentence)
        except Exception:
            logger.exception("lipsync failed for utterance %r chunk %d; broadcasting with no visemes", utterance_id, chunk_seq)
            return []

    async def broadcast_when_ready(
        chunk_seq: int, sentence: str, wav_path: Path, lip_task: asyncio.Task, previous: asyncio.Task | None
    ) -> None:
        visemes = await lip_task
        if previous is not None:
            await previous  # keep chunks in order even if a later lip-sync finishes first
        # After lip-sync (rhubarb reads the mono original). Audio that stays
        # mono still plays, so a failure here must not drop the chunk.
        try:
            await asyncio.to_thread(ensure_stereo_wav, wav_path)
        except Exception:
            logger.exception("stereo conversion failed for utterance %r chunk %d; sending as-is", utterance_id, chunk_seq)
        await state.hub.broadcast(
            "speech",
            {
                "utterance_id": utterance_id,
                "chunk_seq": chunk_seq,
                "final": chunk_seq == len(sentences) - 1,
                "wav_path": str(wav_path),
                "audio_url": f"/audio/{utterance_id}_{chunk_seq}.wav",
                "text": sentence,
                "delivery": delivery,
                "visemes": [{"start_s": v.start_s, "end_s": v.end_s, "shape_key": v.shape_key} for v in visemes],
            },
        )

    previous: asyncio.Task | None = None
    for chunk_seq, sentence in enumerate(sentences):
        try:
            wav_path = state.settings.voice_cache_dir / f"{utterance_id}_{chunk_seq}.wav"
            started = time.monotonic()
            backend = await state.tts_pool.synthesize(sentence, wav_path, remote_params=remote_params)
            seed = (remote_params or {}).get(backend, {}).get("seed")
            emit(
                "speech.chunk",
                {"utterance_id": utterance_id, "seq": chunk_seq + 1, "total": len(sentences), "backend": backend,
                 "seed": seed, "synth_s": round(time.monotonic() - started, 2), "chars": len(sentence)},
            )
            logger.info(
                "speech %s chunk %d/%d: backend=%s seed=%s synth=%.1fs chars=%d",
                utterance_id, chunk_seq + 1, len(sentences), backend, seed, time.monotonic() - started, len(sentence),
            )
            if backend == "local" and state.tts_pool.has_remote:
                logger.warning(
                    "voice fallback: speech %s chunk %d/%d was spoken by local Piper, not the remote voice",
                    utterance_id, chunk_seq + 1, len(sentences),
                )
        except Exception:
            # A voice-pipeline failure must never take down chat itself -- the
            # reply was already broadcast as text via chat_response before
            # this is ever called. A failed sentence is skipped; the rest of
            # the reply is still spoken.
            logger.exception("TTS synthesis failed for utterance %r chunk %d", utterance_id, chunk_seq)
            continue

        lip_task = asyncio.create_task(lipsync(wav_path, sentence, chunk_seq))
        previous = asyncio.create_task(broadcast_when_ready(chunk_seq, sentence, wav_path, lip_task, previous))

    if previous is not None:
        try:
            await previous
        except Exception:
            logger.exception("broadcasting speech for utterance %r failed", utterance_id)


# How much recent chat the model sees each turn: 6 exchanges is enough to
# follow a thread ("yes, that one") without bloating the prompt of a small
# local model; a gap longer than an hour starts a fresh conversation.
CONVERSATION_HISTORY_MESSAGES = 12
CONVERSATION_HISTORY_MAX_AGE_S = 3600.0


async def _handle_chat_text(state: AppState, text: str, *, speak: bool = True) -> str:
    """Shared by POST /chat and any client->daemon text (typed
    user_text_input, or a finalized audio_stream_end transcription --
    OverlayHub's on_user_text_input callback doesn't distinguish the two,
    deliberately: whether the user typed or spoke, it's the same event)."""
    store = state.store
    begin_turn()
    emit("turn.start", {"text": text, "speak": speak})
    skills_block = state.skills.prompt_block() if state.settings.skills.enabled else ""
    system_prompt = build_system_prompt(
        agent_name=store.get_identity_name(),
        traits=store.get_traits(),
        mood=store.get_mood(),
        memory_notes=[n.note for n in rank_memory_notes(store.recent_memory_notes(limit=20), text)[:10]],
        look_summary=state.evolution.current_look_summary(),
        skills_block=skills_block,
    )
    emit("turn.prompt", {"system_prompt": system_prompt})
    history = store.recent_conversation(CONVERSATION_HISTORY_MESSAGES, CONVERSATION_HISTORY_MAX_AGE_S)
    store.log_interaction("user", text)
    reply_format = ReplyFormat(FORMAT_REMINDER, REPAIR_PROMPT, is_structured) if skills_block else None
    if reply_format:
        # Past replies are stored as plain dialogue; show them in the reply
        # shape the model is asked to produce so it doesn't drift to plain text.
        history = [
            {"role": m["role"], "content": json.dumps({"say": m["content"], "do": []})} if m["role"] == "assistant" else m
            for m in history
        ]
    result = await state.router.handle(text, system_prompt=system_prompt, history=history, reply_format=reply_format)
    # What the model actually returned, before it is parsed and cleaned for
    # speech -- kept (event + database) so the status page can show what the
    # cleanup changed.
    raw_text = result.text
    emit("reply.raw", {"text": raw_text, "provider": result.source})
    actions: list[SkillCall] = []
    if state.settings.skills.enabled and not result.source.startswith("tool:"):
        # The model answers {"say": ..., "do": [...]}: only "say" is dialogue.
        # A reply that isn't in that shape is treated whole as dialogue, with no actions.
        parsed = parse_reply(result.text)
        emit(
            "reply.parsed",
            {"structured": parsed.structured, "say": parsed.say,
             "actions": [{"skill": a.skill, "args": a.args} for a in parsed.actions],
             "raw": None if parsed.structured else result.text},
        )
        result.text, actions = parsed.say, parsed.actions
    # The companion speaks this aloud: strip any Markdown/emoji the model used anyway.
    result.text = to_plain_speech(result.text) if result.text else ""
    if actions:
        await state.skills.run_all(actions, state)
    if not result.text:
        emit("turn.reply", {"text": "", "provider": result.source, "escalated": result.escalated, "reason": result.reason})
        return f"utt_{int(time.time() * 1000)}"
    store.log_interaction(
        "assistant", result.text, provider=result.source, escalated=result.escalated, raw_text=raw_text
    )
    emit(
        "turn.reply",
        {"text": result.text, "provider": result.source, "escalated": result.escalated, "reason": result.reason},
    )

    utterance_id = f"utt_{int(time.time() * 1000)}"
    await state.hub.broadcast(
        "chat_response",
        {
            "utterance_id": utterance_id,
            "text": result.text,
            "provider": result.source,
            "escalated": result.escalated,
            "rationale": result.reason or "",
        },
    )
    if speak:
        await _speak(state, result.text, utterance_id=utterance_id)
    return utterance_id


async def _handle_presence_update(state: AppState, window: WindowState | None, screens: list[ScreenInfo]) -> None:
    avatar_size = (state.settings.presence.avatar_width_px, state.settings.presence.avatar_height_px)
    if window is not None and state.last_placement is not None and is_own_window(window, state.last_placement, avatar_size):
        # The sensing adapter reported the overlay's own window (e.g. while the
        # input bar has focus). Treating it as a user window would make the
        # avatar flee its own corner and flap between corners forever.
        return
    state.last_presence = (window, screens)
    await _broadcast_set_position(state, window, screens)


async def _broadcast_set_position(state: AppState, window: WindowState | None, screens: list[ScreenInfo]) -> None:
    if not screens:
        return
    avatar_size = (state.settings.presence.avatar_width_px, state.settings.presence.avatar_height_px)
    placement = choose_position(
        window,
        screens,
        avatar_size,
        margin=state.settings.presence.margin_px,
        display_affinity=state.settings.presence.display_affinity,
    )
    state.last_placement = placement
    emit(
        "presence.placement",
        {"x": placement.x, "y": placement.y, "screen": placement.screen_name, "hidden": placement.hidden,
         "window": None if window is None else {"w": window.rect.w, "h": window.rect.h, "fullscreen": window.fullscreen,
                                                  "screen": window.screen_name}},
    )
    await state.hub.broadcast(
        "set_position",
        {
            "x": placement.x,
            "y": placement.y,
            "screen_name": placement.screen_name,
            "duration_ms": 400,  # 200ms fade-out + re-anchor + 200ms fade-in
            "hidden": placement.hidden,
        },
    )


async def _broadcast_hot_reload(state: AppState) -> None:
    manifest = load_manifest(state.settings.deploy_dir)
    manifest_payload = (
        project_manifest(manifest, include_look_spec_and_warnings=False)
        if manifest is not None
        else {"current_version": 0, "versions": []}
    )
    current_entry = manifest.current() if manifest is not None else None
    glb_url = f"/assets/{current_entry.glb_filename}" if current_entry is not None else ""
    await state.hub.broadcast("hot_reload_asset", {"manifest": manifest_payload, "glb_url": glb_url})


async def _handle_appearance_progress(state: AppState, payload: dict) -> None:
    """Pushes redesign progress to the overlay's status pill. A freshly
    published look is also hot-reloaded here -- nothing else broadcasts it
    outside the staged-reveal path. Autonomous (reflection-driven) redesigns
    stay invisible until they land: no pill for a change nobody asked for."""
    stage = payload["stage"]
    if stage == "done":
        await _broadcast_hot_reload(state)
    if payload["source"] == "autonomous" and stage != "done":
        return
    await state.hub.broadcast("appearance_progress", payload)


async def _handle_presence_transition(state: AppState, old_state: PresenceState, new_state: PresenceState) -> None:
    had_staged = state.store.get_agent_state("staged_version") is not None
    await state.evolution.on_presence_transition(old_state, new_state)
    still_staged = state.store.get_agent_state("staged_version") is not None

    revealed = had_staged and not still_staged and new_state == PresenceState.ACTIVE
    if revealed:
        # A staged build was just revealed by the call above; reuse the
        # normal speech pipeline to deliver a verbal reveal greeting
        # rather than a separate one-off mechanism.
        await _broadcast_hot_reload(state)

    returned = old_state == PresenceState.AWAY and new_state == PresenceState.ACTIVE
    greeting = _return_greeting(state, revealed=revealed) if returned else None
    emit(
        "presence.transition",
        {"from": old_state.value, "to": new_state.value, "greeted": greeting is not None, "revealed": revealed},
    )
    if greeting is None:
        return
    state.store.set_agent_state("last_greeting_timestamp", str(time.time()))
    await state.hub.broadcast("play_gesture", {"name": "greet", "clip": "greet", "loop": False})
    await _speak(state, greeting)


def _return_greeting(state: AppState, *, revealed: bool) -> str | None:
    """Text to greet the user with on returning from User_Away, or None when
    greeting is disabled or still inside the cooldown. A just-revealed look
    always gets its show-off line (it is news, not a pleasantry), but still
    counts toward the cooldown."""
    cfg = state.settings.presence
    if not cfg.greet_on_return:
        return None
    last_raw = state.store.get_agent_state("last_greeting_timestamp")
    if not revealed and last_raw is not None and time.time() - float(last_raw) < cfg.greet_cooldown_s:
        return None
    if revealed:
        return "Welcome back! While you were away, I decided to try out a new look. What do you think?"
    hour = time.localtime().tm_hour
    part = "morning" if hour < 12 else "afternoon" if hour < 18 else "evening"
    return random.choice(
        [
            f"Welcome back! Good {part}.",
            "Hey, you're back!",
            f"Good {part}! Ready when you are.",
        ]
    )


# --- background loops -----------------------------------------------------------


async def _reflection_loop(state: AppState) -> None:
    """One combined cycle: autonomous-redesign eligibility is evaluated in
    the same reflect.py pass as personality reflection itself, not on a
    separate timer."""
    while True:
        await asyncio.sleep(state.settings.personality.reflection_interval_s)
        try:
            await run_reflection(state.store, state.local_pool)
        except Exception:
            logger.exception("reflection loop iteration failed")
        try:
            await maybe_trigger_autonomous_redesign(
                state.store,
                is_build_running=is_build_running,
                is_user_interacting=lambda: state.interaction_active,
                redesign_cooldown_hours_min=state.settings.appearance.redesign_cooldown_hours_min,
                redesign_cooldown_hours_max=state.settings.appearance.redesign_cooldown_hours_max,
                autonomous_redesign_probability=state.settings.appearance.autonomous_redesign_probability,
                on_eligible=state.evolution.trigger_b_on_eligible,
            )
        except Exception:
            logger.exception("Trigger-B eligibility check failed")


async def _mood_decay_loop(state: AppState) -> None:
    while True:
        await asyncio.sleep(state.settings.personality.mood_decay_interval_s)
        try:
            state.store.decay_mood(state.settings.personality.mood_half_life_s)
        except Exception:
            logger.exception("mood decay loop iteration failed")


async def _gc_loop(state: AppState) -> None:
    """Sweeps ephemeral synthesized-speech WAVs older than _GC_TTL_S --
    these are write-once playback artifacts (GET /audio/{utterance_id}.wav),
    never read again once the overlay has played them."""
    while True:
        await asyncio.sleep(_GC_INTERVAL_S)
        try:
            cutoff = time.time() - _GC_TTL_S
            for wav_path in state.settings.voice_cache_dir.glob("*.wav"):
                try:
                    if wav_path.stat().st_mtime < cutoff:
                        wav_path.unlink()
                except OSError:
                    pass  # already gone, or a permissions race -- not worth logging every tick
        except Exception:
            logger.exception("voice cache GC iteration failed")


# --- app lifecycle ---------------------------------------------------------------


async def _apply_reloaded_settings(state: AppState, new_settings: Settings) -> None:
    """After a config.toml reload: keep what the hub hands to *new* clients
    current, and push the overlay-relevant subset (`config_update`, the same
    shape as `welcome.config`) to connected clients -- but only when it
    actually changed, so editing an unrelated key doesn't spam the overlay."""
    changed = welcome_config_subset(new_settings) != welcome_config_subset(state.hub.settings)
    state.hub.update_settings(new_settings)
    if changed:
        await state.hub.broadcast("config_update", welcome_config_subset(new_settings))


@asynccontextmanager
async def lifespan(app: FastAPI):
    state = build_app_state()
    app.state.deskmate = state

    # The config watcher calls back on its own thread; hop onto the event loop.
    loop = asyncio.get_running_loop()

    def _on_settings_reloaded(new_settings: Settings) -> None:
        try:
            loop.call_soon_threadsafe(
                lambda: asyncio.create_task(_apply_reloaded_settings(state, new_settings))
            )
        except RuntimeError:  # loop already closed (shutdown race)
            pass

    unsubscribe_settings = subscribe_if_running(_on_settings_reloaded)

    state.evolution.reconcile_staged_on_boot()

    background = [
        asyncio.create_task(_reflection_loop(state)),
        asyncio.create_task(_mood_decay_loop(state)),
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
        for task in background:
            task.cancel()
        await asyncio.gather(*background, return_exceptions=True)
        await state.router.aclose()
        await state.local_pool.aclose()
        await state.tts_pool.aclose()
        state.store.close()
        state.skill_state.close()


app = FastAPI(title="deskmate brain", lifespan=lifespan)


def _state(app_: FastAPI) -> AppState:
    return app_.state.deskmate


# --- REST surface -----------------------------------------------------------


class ChatRequest(BaseModel):
    text: str
    speak: bool = True


@app.get("/health")
async def health():
    state = _state(app)
    presence = state.desktop_bridge.state if state.desktop_bridge is not None else PresenceState.ACTIVE
    return {
        "status": "ok",
        "name": state.store.get_identity_name(),
        "active_voice": _describe_active_voice(state.settings, state.voice_selector),
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
    views = build_views(state.settings.skills_dir, installed, state.skill_state.all())
    return {
        "enabled": enabled,
        "max_actions_per_reply": state.skills.max_actions,
        "skills": state.skills.describe(),
        "prompt_block": state.skills.prompt_block() if enabled else "",
        # Agent Skills (SKILL.md) installed from other people: catalogued, never run and never put in the prompt
        # until the agent runtime exists. `budget` is what their pinned stubs would cost.
        "installed": [view_json(v) for v in views],
        "rejected": rejected,
        "budget": summarize(state.settings.skills, state.settings.llm.smallest_context_tokens(), views),
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
    views = build_views(state.settings.skills_dir, installed, state.skill_state.all())
    try:
        changed = change_state(
            state.skill_state, views, req.skill, enabled=req.enabled, pinned=req.pinned, alias=req.alias,
            confirmed=req.confirmed, confirm_text=req.confirm_text, changed_by="api",
            max_pinned=state.settings.skills.max_pinned,
        )
    except StateChangeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    emit("skill.state", {"skill": changed.skill_id, "enabled": changed.enabled, "pinned": changed.pinned, "by": "api"})
    return {"skill": changed.skill_id, "enabled": changed.enabled, "pinned": changed.pinned, "alias": changed.alias}


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
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), 15.0)
                except TimeoutError:
                    if await request.is_disconnected():
                        return
                    yield ": keepalive\n\n"
                    continue
                if event["seq"] <= sent:  # already replayed from the backlog
                    continue
                sent = event["seq"]
                yield f"id: {sent}\ndata: {json.dumps(event)}\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-store"})


@app.get("/api/status")
async def api_status():
    """Read-only runtime state that only exists in this process's memory, for
    the standalone status page (status_page/). Everything on disk it reads
    for itself."""
    state = _state(app)
    placement = state.last_placement
    window, screens = state.last_presence if state.last_presence is not None else (None, [])
    manifest = load_manifest(state.settings.deploy_dir)
    return {
        "started_at": _STARTED_AT,
        "overlay_clients": state.hub.connected_client_ids,
        "interaction_active": state.interaction_active,
        "build_running": is_build_running(),
        "placement": None
        if placement is None
        else {"x": placement.x, "y": placement.y, "screen": placement.screen_name, "hidden": placement.hidden},
        "window": None
        if window is None
        else {
            "x": window.rect.x,
            "y": window.rect.y,
            "w": window.rect.w,
            "h": window.rect.h,
            "screen": window.screen_name,
            "fullscreen": window.fullscreen,
        },
        "screens": [
            {"name": s.name, "x": s.rect.x, "y": s.rect.y, "w": s.rect.w, "h": s.rect.h} for s in screens
        ],
        "look": {
            "current_version": manifest.current_version if manifest is not None else None,
            "versions": len(manifest.versions) if manifest is not None else 0,
            "summary": state.evolution.current_look_summary(),
        },
    }


def _describe_active_voice(settings: Settings, selector: SeedVoiceSelector) -> str:
    """The seed voice the companion has persisted (agent_state.current_voice_id,
    written by SeedVoiceSelector) when remote seed voices are in use;
    otherwise derived from the real active TTS config."""
    tts_config = settings.voice.tts
    if tts_config.active_backend == "remote":
        stored = selector.current_voice_id()
        if stored and any(stored.startswith(f"remote:{name}:seed:") for name in tts_config.remote):
            return stored
        enabled = tts_config.enabled_remote_instances()
        if enabled:
            name = next((n for n, e in tts_config.remote.items() if e is enabled[0]), "remote")
            return f"remote:{name}"
    return f"piper:{Path(tts_config.local.voice_model_path).stem}"


@app.post("/chat")
async def chat(req: ChatRequest):
    state = _state(app)
    utterance_id = await _handle_chat_text(state, req.text, speak=req.speak)
    return {"status": "ok", "utterance_id": utterance_id}


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
    return {"status": "ok", "notes_created": result.notes_created, "trait_deltas": result.trait_deltas}


@app.get("/audio/{filename}")
async def get_audio(filename: str):
    state = _state(app)
    if "/" in filename or "\\" in filename or not filename.endswith(".wav"):
        raise HTTPException(status_code=404)
    path = (state.settings.voice_cache_dir / filename).resolve()
    if path.parent != state.settings.voice_cache_dir.resolve() or not path.is_file():
        raise HTTPException(status_code=404)
    return FileResponse(path, media_type="audio/wav")


@app.get("/assets/{filename}")
async def get_asset(filename: str):
    state = _state(app)
    if "/" in filename or "\\" in filename:
        raise HTTPException(status_code=404)
    path = (state.settings.current_dir / filename).resolve()
    if path.parent != state.settings.current_dir.resolve() or not path.is_file():
        raise HTTPException(status_code=404)
    return FileResponse(path, media_type="model/gltf-binary")


@app.websocket("/ws/overlay")
async def ws_overlay(websocket: WebSocket):
    await _state(app).hub.handle_connection(websocket)


def run() -> None:
    import uvicorn

    settings = get_settings()
    uvicorn.run("brain.main:app", host=settings.server.host, port=settings.server.port, log_level="info")


if __name__ == "__main__":
    run()
