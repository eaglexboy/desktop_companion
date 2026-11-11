"""
The chat and speech pipeline shared by POST /chat and the overlay socket: sentence
splitting, speaking a reply, handling typed or spoken text, presence updates and
the return greeting.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import time
from pathlib import Path

from brain.app_state import AppState
from brain.appearance.manifest import load_manifest
from brain.events import begin_turn, emit
from brain.ipc.server import project_manifest
from brain.personality.prompts import build_system_prompt, rank_memory_notes
from brain.presence.desktop_bridge import PresenceState
from brain.presence.placement import (
    ScreenInfo,
    WindowState,
    choose_position,
    is_own_window,
)
from brain.router import ReplyFormat
from brain.skills.builtin import gesture_intensity
from brain.skills.registry import SkillCall
from brain.skills.reply import (
    FORMAT_REMINDER,
    REPAIR_PROMPT,
    is_structured,
    parse_reply,
)
from brain.voice.delivery import pick_delivery
from brain.voice.lipsync import extract_visemes_if_needed
from brain.voice.plaintext import to_plain_speech
from brain.voice.stereo import ensure_stereo_wav

logger = logging.getLogger("deskmate.main")

_LONG_REPLY_CHARS = 280  # pick_delivery's is_long_or_code heuristic


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


async def _speak(
    state: AppState, text: str, *, utterance_id: str | None = None
) -> None:
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
    delivery = pick_delivery(
        state.store.get_traits(), state.store.get_mood(), _is_long_or_code(text)
    )
    has_face = state.evolution.current_look_spec().has_face
    # Chosen once per utterance (never mid-sentence); a failure here must not
    # stop speech -- the pool just uses each engine's own default voice.
    remote_params = None
    try:
        selection = state.voice_selector.select()
        if selection is not None:
            remote_params = {selection.instance: {"seed": selection.seed}}
    except Exception:
        logger.exception(
            "voice selection failed for utterance %r; using default voice",
            utterance_id,
        )

    async def lipsync(wav_path: Path, sentence: str, chunk_seq: int) -> list:
        # Lipsync is a separate failure domain from synthesis: rhubarb being
        # unavailable (or erroring on this particular clip) must not throw away
        # perfectly good audio that already exists on disk -- degrade to no
        # visemes (a static mouth) rather than skipping the whole broadcast.
        try:
            return await extract_visemes_if_needed(
                state.lipsync, wav_path, has_face=has_face, dialog_text=sentence
            )
        except Exception:
            logger.exception(
                "lipsync failed for utterance %r chunk %d; broadcasting with no visemes",
                utterance_id,
                chunk_seq,
            )
            return []

    async def broadcast_when_ready(
        chunk_seq: int,
        sentence: str,
        wav_path: Path,
        lip_task: asyncio.Task,
        previous: asyncio.Task | None,
    ) -> None:
        visemes = await lip_task
        if previous is not None:
            await previous  # keep chunks in order even if a later lip-sync finishes first
        # After lip-sync (rhubarb reads the mono original). Audio that stays
        # mono still plays, so a failure here must not drop the chunk.
        try:
            await asyncio.to_thread(ensure_stereo_wav, wav_path)
        except Exception:
            logger.exception(
                "stereo conversion failed for utterance %r chunk %d; sending as-is",
                utterance_id,
                chunk_seq,
            )
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
                "visemes": [
                    {
                        "start_s": v.start_s,
                        "end_s": v.end_s,
                        "shape_key": v.shape_key,
                    }
                    for v in visemes
                ],
            },
        )

    previous: asyncio.Task | None = None
    for chunk_seq, sentence in enumerate(sentences):
        try:
            wav_path = (
                state.settings.voice_cache_dir
                / f"{utterance_id}_{chunk_seq}.wav"
            )
            started = time.monotonic()
            backend = await state.tts_pool.synthesize(
                sentence, wav_path, remote_params=remote_params
            )
            seed = (remote_params or {}).get(backend, {}).get("seed")
            emit(
                "speech.chunk",
                {
                    "utterance_id": utterance_id,
                    "seq": chunk_seq + 1,
                    "total": len(sentences),
                    "backend": backend,
                    "seed": seed,
                    "synth_s": round(time.monotonic() - started, 2),
                    "chars": len(sentence),
                },
            )
            logger.info(
                "speech %s chunk %d/%d: backend=%s seed=%s synth=%.1fs chars=%d",
                utterance_id,
                chunk_seq + 1,
                len(sentences),
                backend,
                seed,
                time.monotonic() - started,
                len(sentence),
            )
            if backend == "local" and state.tts_pool.has_remote:
                logger.warning(
                    "voice fallback: speech %s chunk %d/%d was spoken by local Piper, not the remote voice",
                    utterance_id,
                    chunk_seq + 1,
                    len(sentences),
                )
        except Exception:
            # A voice-pipeline failure must never take down chat itself -- the
            # reply was already broadcast as text via chat_response before
            # this is ever called. A failed sentence is skipped; the rest of
            # the reply is still spoken.
            logger.exception(
                "TTS synthesis failed for utterance %r chunk %d",
                utterance_id,
                chunk_seq,
            )
            continue

        lip_task = asyncio.create_task(lipsync(wav_path, sentence, chunk_seq))
        previous = asyncio.create_task(
            broadcast_when_ready(
                chunk_seq, sentence, wav_path, lip_task, previous
            )
        )

    if previous is not None:
        try:
            await previous
        except Exception:
            logger.exception(
                "broadcasting speech for utterance %r failed", utterance_id
            )


# How much recent chat the model sees each turn: 6 exchanges is enough to
# follow a thread ("yes, that one") without bloating the prompt of a small
# local model; a gap longer than an hour starts a fresh conversation.
CONVERSATION_HISTORY_MESSAGES = 12
CONVERSATION_HISTORY_MAX_AGE_S = 3600.0


async def _handle_chat_text(
    state: AppState, text: str, *, speak: bool = True
) -> str:
    """Shared by POST /chat and any client->daemon text (typed
    user_text_input, or a finalized audio_stream_end transcription --
    OverlayHub's on_user_text_input callback doesn't distinguish the two,
    deliberately: whether the user typed or spoke, it's the same event)."""
    store = state.store
    begin_turn()
    emit("turn.start", {"text": text, "speak": speak})
    skills_block = (
        state.skills.prompt_block() if state.settings.skills.enabled else ""
    )
    system_prompt = build_system_prompt(
        agent_name=store.get_identity_name(),
        traits=store.get_traits(),
        mood=store.get_mood(),
        memory_notes=[
            n.note
            for n in rank_memory_notes(
                store.recent_memory_notes(limit=20), text
            )[:10]
        ],
        look_summary=state.evolution.current_look_summary(),
        skills_block=skills_block,
    )
    emit("turn.prompt", {"system_prompt": system_prompt})
    history = store.recent_conversation(
        CONVERSATION_HISTORY_MESSAGES, CONVERSATION_HISTORY_MAX_AGE_S
    )
    store.log_interaction("user", text)
    reply_format = (
        ReplyFormat(FORMAT_REMINDER, REPAIR_PROMPT, is_structured)
        if skills_block
        else None
    )
    if reply_format:
        # Past replies are stored as plain dialogue; show them in the reply
        # shape the model is asked to produce so it doesn't drift to plain text.
        history = [
            (
                {
                    "role": m["role"],
                    "content": json.dumps({"say": m["content"], "do": []}),
                }
                if m["role"] == "assistant"
                else m
            )
            for m in history
        ]
    result = await state.router.handle(
        text,
        system_prompt=system_prompt,
        history=history,
        reply_format=reply_format,
    )
    # What the model actually returned, before it is parsed and cleaned for
    # speech -- kept (event + database) so the status page can show what the
    # cleanup changed.
    raw_text = result.text
    emit("reply.raw", {"text": raw_text, "provider": result.source})
    actions: list[SkillCall] = []
    forced_look = False
    if state.settings.skills.enabled and not result.source.startswith("tool:"):
        # The model answers {"say": ..., "do": [...]}: only "say" is dialogue.
        # A reply that isn't in that shape is treated whole as dialogue, with no actions.
        parsed = parse_reply(result.text)
        emit(
            "reply.parsed",
            {
                "structured": parsed.structured,
                "say": parsed.say,
                "actions": [
                    {"skill": a.skill, "args": a.args} for a in parsed.actions
                ],
                "raw": None if parsed.structured else result.text,
            },
        )
        result.text, actions = parsed.say, parsed.actions
        # A model that says "okay, changing" without the action is not left to forget it: a message
        # that is only a request to become an installed model (the user's own words, never the
        # model's) makes the change itself.
        # What it said instead (that it has no such model, for one: it is not told which are installed)
        # is replaced by the answer of the change itself.
        if not any(
            a.skill == "change_look" for a in actions
        ) and state.evolution.is_direct_look_request(text):
            actions = [*actions, SkillCall("change_look", {"request": text})]
            forced_look = True
    # The companion speaks this aloud: strip any Markdown/emoji the model used anyway.
    result.text = to_plain_speech(result.text) if result.text else ""
    if actions:
        ran = await state.skills.run_all(actions, state)
        if (
            forced_look
            and ran
            and ran[-1]["ok"]
            and isinstance(ran[-1]["result"], str)
        ):
            result.text = to_plain_speech(ran[-1]["result"])
    if not result.text:
        emit(
            "turn.reply",
            {
                "text": "",
                "provider": result.source,
                "escalated": result.escalated,
                "reason": result.reason,
            },
        )
        return f"utt_{int(time.time() * 1000)}"
    store.log_interaction(
        "assistant",
        result.text,
        provider=result.source,
        escalated=result.escalated,
        raw_text=raw_text,
    )
    emit(
        "turn.reply",
        {
            "text": result.text,
            "provider": result.source,
            "escalated": result.escalated,
            "reason": result.reason,
        },
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


async def _handle_presence_update(
    state: AppState, window: WindowState | None, screens: list[ScreenInfo]
) -> None:
    avatar_size = (
        state.settings.presence.avatar_width_px,
        state.settings.presence.avatar_height_px,
    )
    if (
        window is not None
        and state.last_placement is not None
        and is_own_window(window, state.last_placement, avatar_size)
    ):
        # The sensing adapter reported the overlay's own window (e.g. while the
        # input bar has focus). Treating it as a user window would make the
        # avatar flee its own corner and flap between corners forever.
        return
    state.last_presence = (window, screens)
    await _broadcast_set_position(state, window, screens)


async def _broadcast_set_position(
    state: AppState, window: WindowState | None, screens: list[ScreenInfo]
) -> None:
    if not screens:
        return
    avatar_size = (
        state.settings.presence.avatar_width_px,
        state.settings.presence.avatar_height_px,
    )
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
        {
            "x": placement.x,
            "y": placement.y,
            "screen": placement.screen_name,
            "hidden": placement.hidden,
            "window": (
                None
                if window is None
                else {
                    "w": window.rect.w,
                    "h": window.rect.h,
                    "fullscreen": window.fullscreen,
                    "screen": window.screen_name,
                }
            ),
        },
    )
    await state.hub.broadcast(
        "set_position",
        {
            "x": placement.x,
            "y": placement.y,
            "screen_name": placement.screen_name,
            "duration_ms": 900,  # same-screen slide; a monitor change is a cross-screen run in the overlay
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
    glb_url = (
        f"/assets/{current_entry.glb_filename}"
        if current_entry is not None
        else ""
    )
    await state.hub.broadcast(
        "hot_reload_asset", {"manifest": manifest_payload, "glb_url": glb_url}
    )


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


async def _handle_presence_transition(
    state: AppState, old_state: PresenceState, new_state: PresenceState
) -> None:
    returned = (
        old_state == PresenceState.AWAY and new_state == PresenceState.ACTIVE
    )
    # Pocket goes cold while the machine is away and its first request then
    # timed out (the greeting fell back to Piper). Warm it in parallel with the
    # transition work below; the greeting waits for it.
    warm_task = (
        asyncio.create_task(
            state.tts_pool.warm_up(
                state.settings.voice_cache_dir / "_warmup.wav"
            )
        )
        if returned
        and state.settings.presence.greet_on_return
        and state.tts_pool.has_remote
        else None
    )
    had_staged = state.store.get_agent_state("staged_version") is not None
    await state.evolution.on_presence_transition(old_state, new_state)
    still_staged = state.store.get_agent_state("staged_version") is not None

    revealed = (
        had_staged and not still_staged and new_state == PresenceState.ACTIVE
    )
    if revealed:
        # A staged build was just revealed by the call above; reuse the
        # normal speech pipeline to deliver a verbal reveal greeting
        # rather than a separate one-off mechanism.
        await _broadcast_hot_reload(state)

    greeting = _return_greeting(state, revealed=revealed) if returned else None
    emit(
        "presence.transition",
        {
            "from": old_state.value,
            "to": new_state.value,
            "greeted": greeting is not None,
            "revealed": revealed,
        },
    )
    if greeting is None:
        return
    # A broadcast reaches only whoever is connected right now, and on a wake
    # from suspend the overlay can reconnect a moment after the brain notices
    # the return. Wait briefly; failing that, hold the greeting for the next
    # connection instead of speaking to nobody.
    if not await _wait_for_overlay(state, _GREETING_CLIENT_WAIT_S):
        state.pending_greeting = (greeting, time.time())
        logger.info(
            "no overlay connected after %.0fs; greeting held until one connects",
            _GREETING_CLIENT_WAIT_S,
        )
        return
    if warm_task is not None:
        await warm_task
    await _deliver_greeting(state, greeting)


# How long a return greeting waits for the overlay to (re)connect, and how long
# a held one stays worth saying once it does.
_GREETING_CLIENT_WAIT_S = 10.0
_GREETING_STALE_S = 300.0


async def _wait_for_overlay(state: AppState, timeout_s: float) -> bool:
    deadline = time.monotonic() + timeout_s
    while not state.hub.connected_client_ids:
        if time.monotonic() >= deadline:
            return False
        await asyncio.sleep(0.25)
    return True


def _deliver_pending_greeting(state: AppState) -> asyncio.Task | None:
    pending, state.pending_greeting = state.pending_greeting, None
    if pending is None:
        return None
    greeting, created_at = pending
    if time.time() - created_at > _GREETING_STALE_S:
        return None

    async def _run() -> None:
        # Pocket may have gone cold again while the greeting was held.
        if state.tts_pool.has_remote:
            await state.tts_pool.warm_up(
                state.settings.voice_cache_dir / "_warmup.wav"
            )
        await _deliver_greeting(state, greeting)

    task = asyncio.create_task(_run())
    state.background_tasks.append(task)
    task.add_done_callback(
        lambda t: (
            state.background_tasks.remove(t)
            if t in state.background_tasks
            else None
        )
    )
    return task


async def _deliver_greeting(state: AppState, greeting: str) -> None:
    # Stamped on delivery, so a greeting nobody received does not use up the cooldown.
    state.store.set_agent_state("last_greeting_timestamp", str(time.time()))
    # Same delivery as a model reply: stored in the conversation (so chat and
    # the model's history see it) and sent as chat_response before the voice.
    state.store.log_interaction(
        "assistant", greeting, provider="presence", escalated=False
    )
    utterance_id = f"utt_{int(time.time() * 1000)}"
    await state.hub.broadcast(
        "chat_response",
        {
            "utterance_id": utterance_id,
            "text": greeting,
            "provider": "presence",
            "escalated": False,
            "rationale": "",
        },
    )
    await state.hub.broadcast(
        "play_gesture",
        {
            "name": "greet",
            "clip": "greet",
            "loop": False,
            "intensity": gesture_intensity(state),
        },
    )
    await _speak(state, greeting, utterance_id=utterance_id)


def _return_greeting(state: AppState, *, revealed: bool) -> str | None:
    """Text to greet the user with on returning from User_Away, or None when
    greeting is disabled or still inside the cooldown. A just-revealed look
    always gets its show-off line (it is news, not a pleasantry), but still
    counts toward the cooldown."""
    cfg = state.settings.presence
    if not cfg.greet_on_return:
        return None
    last_raw = state.store.get_agent_state("last_greeting_timestamp")
    if (
        not revealed
        and last_raw is not None
        and time.time() - float(last_raw) < cfg.greet_cooldown_s
    ):
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
