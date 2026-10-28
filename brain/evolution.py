"""Avatar Appearance Evolution glue: wires together the router's
`change_appearance`/`revert_appearance` tool stubs, personality's
reflection-cycle autonomous-redesign eligibility gate, the Blender
compile/publish/stage pipeline, and desktop presence transitions.

The router's tool handlers and `reflect.py`'s `on_eligible` callback both
need an actual implementation somewhere; this module exists so
`brain/main.py` just instantiates `AvatarEvolution` and wires its methods
in as callbacks, rather than inlining this logic into main.py directly.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections.abc import Awaitable, Callable, Coroutine
from pathlib import Path
from typing import Any

from brain.appearance.blender_gen import (
    CompileResult,
    cancel_running_build,
    compile_and_publish,
    reveal_staged,
)
from brain.appearance.manifest import (
    load_manifest,
    mark_version_status,
    revert_to_version,
    save_manifest,
)
from brain.appearance.spec import (
    LookSpec,
    buildable_template_ids,
    ensure_catalogs_seeded,
    example_look_spec,
    load_templates_catalog,
    load_wardrobe_catalog,
    look_summary,
    merge_delta,
    validate_against_catalogs,
)
from brain.config import Settings
from brain.events import emit, llm_purpose
from brain.llm_pool import LLMPool
from brain.personality.state import PersonalityStore
from brain.presence.desktop_bridge import PresenceState
from brain.router import Router

logger = logging.getLogger("deskmate.evolution")

_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)


def _extract_json_object(raw: str) -> str:
    match = _JSON_OBJECT_RE.search(raw)
    return match.group(0) if match else raw


_DELTA_SYNTHESIS_PROMPT = """\
You are the appearance-design process behind a desktop companion agent's 3D \
avatar. Propose a small change to its look as a JSON delta -- only the \
fields that should change, to be merged onto the current look. Never invent \
values outside the catalogs below.

Current look:
{current_look_json}

Available base templates: {template_ids}
Available outfits: {outfits}
Available footwear: {footwear}
Available accessories per socket: {accessories}
{budget_block}
Context for this change: {trigger_context}

Respond with ONLY compact JSON representing the delta (a partial LookSpec; \
omit anything unchanged). Example shape:
{{"wardrobe": {{"outfit": "wizard_robe"}}, "accessories": {{"socket_eyes": "glasses"}}, "palette": {{"primary": "#1E1E2E"}}}}
"""

_RETRY_SUFFIX = "\n\nYour previous answer was invalid: {error}\nRespond again with ONLY corrected compact JSON, same shape as before."

_AUTONOMOUS_BUDGET_BLOCK = "Change at most 1 to 3 attributes total. {base_template_clause}\n"
_BASE_TEMPLATE_ALLOWED_CLAUSE = "You may change \"base_template\" if a new one truly fits."
_BASE_TEMPLATE_FORBIDDEN_CLAUSE = 'Do NOT include "base_template" in your delta.'

# Stages of one redesign, in order. `done`/`staged`/`failed` are terminal.
# Mirrored 1:1 by overlay/qml/Main.qml's appearance status pill.
_PROGRESS_LABELS = {
    "designing": "Designing a new look...",
    "building": "Building the 3D model...",
    "done": "New look ready!",
    "staged": "New look built, revealing it when you're back",
    "failed": "Couldn't change my look",
}
ProgressHook = Callable[[dict[str, Any]], Awaitable[None]]

_MAX_AUTONOMOUS_LEAVES = 3
_BASE_TEMPLATE_OPENNESS_THRESHOLD = 85.0
_BASE_TEMPLATE_MIN_DAYS = 7


def _flatten(delta: dict, prefix: tuple = ()) -> list[tuple[tuple, object]]:
    leaves: list[tuple[tuple, object]] = []
    for key, value in delta.items():
        path = (*prefix, key)
        if isinstance(value, dict):
            leaves.extend(_flatten(value, path))
        else:
            leaves.append((path, value))
    return leaves


def _unflatten(leaves: list[tuple[tuple, object]]) -> dict:
    result: dict = {}
    for path, value in leaves:
        node = result
        for key in path[:-1]:
            node = node.setdefault(key, {})
        node[path[-1]] = value
    return result


def cap_delta_budget(delta: dict, max_leaves: int) -> dict:
    """Hard safety cap behind the LLM's own instruction-following (same
    philosophy as reflect.py's trait/mood clamps): truncates to the first
    `max_leaves` leaf fields (dict insertion order), rather than rejecting
    the whole delta outright."""
    leaves = _flatten(delta)
    if len(leaves) <= max_leaves:
        return delta
    return _unflatten(leaves[:max_leaves])


def base_template_change_allowed(store: PersonalityStore, openness: float) -> bool:
    """Base template changes require openness >= 85 AND the current
    base_template to have been held for >= 7 days
    (`agent_state.base_template_since`, unset means "can't prove it's been
    long enough" -- never assumed true by absence)."""
    if openness < _BASE_TEMPLATE_OPENNESS_THRESHOLD:
        return False
    since_raw = store.get_agent_state("base_template_since")
    if since_raw is None:
        return False
    return (time.time() - float(since_raw)) >= _BASE_TEMPLATE_MIN_DAYS * 86400


def trigger_b_bias_lines(traits: dict[str, float], mood: dict[str, float]) -> list[str]:
    """Only covers the trait/mood extremes with concrete guidance behind
    them -- no invented phrasing for axes/bands without a real rule (e.g.
    low boldness, low formality, valence)."""
    lines = []
    openness = traits.get("openness", 50.0)
    if openness >= 70:
        lines.append(
            "High openness: feel free to try a new wardrobe style, a new accessory socket, "
            "or even an alternate base template if it truly fits."
        )
    elif openness <= 30:
        lines.append("Low openness: prefer slight palette tweaks or subtle accessory shifts over bold changes.")
    if traits.get("boldness", 50.0) >= 70:
        lines.append("High boldness: favor vibrant, saturated palettes and striking, expressive accessories.")
    if traits.get("formality", 50.0) >= 70:
        lines.append(
            "High formality: favor structured, tailored outfits (e.g. a lab coat or vest) "
            "over casual or whimsical styles."
        )
    if mood.get("energy", 50.0) >= 70:
        lines.append("High energy mood: lean toward brighter, more energetic accent colors.")
    if mood.get("stress", 50.0) >= 70:
        lines.append("Elevated stress mood: lean toward cozy, relaxed attire.")
    return lines


class AvatarEvolution:
    """Owns the real logic behind the two router tool stubs, the reflection
    loop's autonomous-redesign eligibility callback, the presence-transition
    reveal hook, boot-time staged-asset reconciliation, and asset-swap-failure
    rollback. Constructed once and wired into the Router/reflect.py/
    DesktopPresenceBridge by whatever assembles the daemon."""

    def __init__(
        self,
        settings: Settings,
        store: PersonalityStore,
        local_pool: LLMPool,
        *,
        get_presence_state: Callable[[], PresenceState] = lambda: PresenceState.ACTIVE,
        clock: Callable[[], float] = time.time,
        on_progress: ProgressHook | None = None,
    ) -> None:
        self._settings = settings
        self._store = store
        self._local_pool = local_pool
        self._get_presence_state = get_presence_state
        self._clock = clock
        self._on_progress = on_progress
        self._background_tasks: set[asyncio.Task] = set()
        ensure_catalogs_seeded(settings.deploy_dir)

    def wire_tools(self, router: Router) -> None:
        """Overwrites router.py's `change_appearance`/`revert_appearance`
        stubs (registered by `build_router()`) with the real handlers on
        this instance -- `Router.register_tool()` just does a dict
        assignment keyed by name, so re-registering under the same name
        cleanly replaces the stub. Called once by whatever assembles the
        daemon."""
        router.register_tool(
            "change_appearance",
            "Change the companion's visual appearance based on a free-form request.",
            {"type": "object", "properties": {"request": {"type": "string"}}, "required": ["request"]},
            self.change_appearance,
        )
        router.register_tool(
            "revert_appearance",
            "Revert the companion's appearance to its previous look.",
            {"type": "object", "properties": {}},
            self.revert_appearance,
        )

    # --- current look -----------------------------------------------------

    def current_look_spec(self) -> LookSpec:
        look = self._stored_look_spec()
        if look.base_template not in buildable_template_ids(self._settings.deploy_dir) and not look.base_template.startswith(
            "custom:"
        ):
            # An earlier build asked for a body that has no model installed;
            # the generator drew a blob instead. Report what is really on
            # screen, so deltas merge onto something that can be rebuilt.
            return look.model_copy(update={"base_template": "primitive_blob"})
        return look

    def _stored_look_spec(self) -> LookSpec:
        manifest = load_manifest(self._settings.deploy_dir)
        if manifest is not None:
            current = manifest.current()
            if current is not None:
                return LookSpec.model_validate(current.look_spec)
        # default_look_spec is a path relative to the repo root (same
        # convention blender_gen.py's GENERATOR_SCRIPT uses) -- not
        # workspace_dir/deploy_dir, which are pure runtime data locations.
        repo_root = Path(__file__).resolve().parent.parent
        default_path = repo_root / self._settings.appearance.default_look_spec
        if default_path.is_file():
            return LookSpec.model_validate(json.loads(default_path.read_text()))
        return example_look_spec()

    def current_look_summary(self) -> str:
        return look_summary(self.current_look_spec())

    # --- delta synthesis (shared by Trigger A and Trigger B) --------------

    async def _synthesize_delta(
        self,
        current: LookSpec,
        *,
        trigger_context: str,
        budget_block: str = "",
        allow_base_template_change: bool = True,
    ) -> dict | None:
        if self._local_pool.is_empty:
            logger.warning("no local LLM pool configured, cannot synthesize an appearance delta")
            return None

        catalogs_dir = self._settings.catalogs_dir
        templates = load_templates_catalog(catalogs_dir)["templates"]
        wardrobe_catalog = load_wardrobe_catalog(catalogs_dir)
        prompt = _DELTA_SYNTHESIS_PROMPT.format(
            current_look_json=current.model_dump_json(),
            template_ids=", ".join(buildable_template_ids(self._settings.deploy_dir)),
            outfits=", ".join(wardrobe_catalog["outfits"]),
            footwear=", ".join(wardrobe_catalog["footwear"]),
            accessories=wardrobe_catalog["accessories"],
            budget_block=budget_block,
            trigger_context=trigger_context,
        )

        last_error: Exception | None = None
        for attempt in range(2):
            try:
                with llm_purpose("appearance"):
                    _, raw = await self._local_pool.chat([{"role": "user", "content": prompt}], max_tokens=400)
                delta = json.loads(_extract_json_object(raw))
                if not isinstance(delta, dict):
                    raise TypeError(f"expected a JSON object, got {type(delta).__name__}")
                if not allow_base_template_change:
                    delta.pop("base_template", None)
                if budget_block:
                    delta = cap_delta_budget(delta, _MAX_AUTONOMOUS_LEAVES)
                merged = merge_delta(current, delta)
                validate_against_catalogs(merged, self._settings.deploy_dir)
                return delta
            except Exception as exc:  # noqa: BLE001 -- a bad delta must never crash the caller
                last_error = exc
                if attempt == 0:
                    prompt = prompt + _RETRY_SUFFIX.format(error=exc)

        logger.warning("delta synthesis failed twice (%s), giving up for this trigger", last_error)
        return None

    def _stamp_redesign_metadata(self, previous: LookSpec, new: LookSpec) -> None:
        self._store.set_agent_state("last_redesign_timestamp", str(self._clock()))
        if new.base_template != previous.base_template:
            self._store.set_agent_state("base_template_since", str(self._clock()))

    def _is_user_active(self) -> bool:
        return self._get_presence_state() == PresenceState.ACTIVE

    async def _progress(self, stage: str, source: str, **extra: Any) -> None:
        """Narrates one redesign stage to the event bus (status page) and the
        `on_progress` hook (overlay). Observation only: a failing hook must
        never break the redesign it is reporting on."""
        payload = {"stage": stage, "label": _PROGRESS_LABELS[stage], "source": source, **extra}
        emit(f"appearance.{stage}", payload)
        logger.info("appearance %s (%s)%s", stage, source, f": {extra['error']}" if "error" in extra else "")
        if self._on_progress is None:
            return
        try:
            await self._on_progress(payload)
        except Exception:  # noqa: BLE001
            logger.exception("appearance on_progress hook failed for stage %r", stage)

    async def _finish(self, result: CompileResult, source: str) -> None:
        stage = "done" if result.outcome == "published" else "staged"
        await self._progress(stage, source, version=result.version)

    # --- Trigger A: user-prompted ------------------------------------------

    def _schedule_background(self, coro: Coroutine[Any, Any, Any]) -> asyncio.Task:
        task = asyncio.ensure_future(coro)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        return task

    async def change_appearance(self, request: str) -> str:
        """The real `change_appearance` tool handler (replaces router.py's
        stub): reply immediately, do the actual synthesis+compile in the
        background, cancelling any in-flight autonomous build first (user
        intent always wins)."""
        cancel_running_build()
        self._schedule_background(self._run_trigger_a(request))
        return "I like that idea. Let me put something together..."

    async def _run_trigger_a(self, request: str) -> CompileResult | None:
        current = self.current_look_spec()
        await self._progress("designing", "user", request=request)
        delta = await self._synthesize_delta(current, trigger_context=request)
        if delta is None:
            # No spoken follow-up (the caller already replied with "let me
            # put something together"); the failed stage is what tells the
            # user, via the overlay's status pill.
            logger.info("Trigger A: could not synthesize a valid delta for request=%r", request)
            await self._progress("failed", "user", error="couldn't come up with a valid design")
            return None

        merged = merge_delta(current, delta)
        await self._progress("building", "user", summary=look_summary(merged))
        try:
            result = await compile_and_publish(self._settings, merged, is_user_present=lambda: True)
        except Exception as exc:
            # This runs as a detached background task (see
            # change_appearance()) -- an uncaught exception here would
            # otherwise only surface via asyncio's default handler.
            logger.exception("Trigger A: compile_and_publish failed for request=%r", request)
            await self._progress("failed", "user", error=str(exc)[:200])
            return None
        await self._finish(result, "user")
        if result.outcome == "published":
            self._stamp_redesign_metadata(current, merged)
            self._store.add_memory_note(f"Changed appearance at the user's request: {look_summary(merged)}.")
        return result

    async def generate_from_full_spec(self, look_spec: LookSpec) -> CompileResult | None:
        """Backs `POST /appearance/generate`: the caller already
        supplied a complete `LookSpec` directly (no delta-synthesis LLM call
        needed at all, unlike Trigger A's free-text `change_appearance()`).
        Catalog validation is the REST endpoint's own job (so a bad spec
        gets a synchronous 422 instead of a silent background failure);
        this assumes it already passed. Treated as always-present/immediate-
        reveal, same as Trigger A -- a direct API call implies an operator
        is watching for the result."""
        current = self.current_look_spec()
        await self._progress("building", "api", summary=look_summary(look_spec))
        try:
            result = await compile_and_publish(self._settings, look_spec, is_user_present=lambda: True)
        except Exception as exc:
            logger.exception("generate_from_full_spec: compile_and_publish failed")
            await self._progress("failed", "api", error=str(exc)[:200])
            return None
        await self._finish(result, "api")
        if result.outcome == "published":
            self._stamp_redesign_metadata(current, look_spec)
            self._store.add_memory_note(f"Appearance set directly via the API: {look_summary(look_spec)}.")
        return result

    def schedule_generate_from_full_spec(self, look_spec: LookSpec) -> None:
        """Fire-and-forget entry point for `POST /appearance/generate`,
        which returns `202 Accepted` before the compile finishes -- same
        background-task bookkeeping as `change_appearance()`, exposed
        publicly since the REST layer has no business reaching into
        `_background_tasks` itself."""
        self._schedule_background(self.generate_from_full_spec(look_spec))

    # --- Trigger A: revert --------------------------------------------------

    async def revert_appearance(self, _request: str = "") -> str:
        """The real `revert_appearance` tool handler. Zero Blender
        recompilation -- a manifest pointer rewrite only."""
        manifest = load_manifest(self._settings.deploy_dir)
        if manifest is None or manifest.current_version <= 1:
            return "There's no previous look for me to revert to yet."

        target_version = manifest.current_version - 1
        target_entry = manifest.get_version(target_version)
        if target_entry is None or not (self._settings.current_dir / target_entry.glb_filename).is_file():
            return "I can't revert that far back -- that look isn't kept around anymore."

        reverted = revert_to_version(manifest, target_version)
        save_manifest(self._settings.deploy_dir, reverted)
        self._store.set_agent_state("last_redesign_timestamp", str(self._clock()))
        return f"Done -- back to how I looked before: {look_summary(LookSpec.model_validate(target_entry.look_spec))}."

    # --- Trigger B: autonomous evolution ------------------------------------

    async def trigger_b_on_eligible(self) -> None:
        """The real `on_eligible` callback for
        `reflect.maybe_trigger_autonomous_redesign()`."""
        current = self.current_look_spec()
        traits = self._store.get_traits()
        mood = self._store.get_mood()
        allow_base_template = base_template_change_allowed(self._store, traits.get("openness", 50.0))

        bias_lines = trigger_b_bias_lines(traits, mood)
        base_clause = _BASE_TEMPLATE_ALLOWED_CLAUSE if allow_base_template else _BASE_TEMPLATE_FORBIDDEN_CLAUSE
        budget_block = _AUTONOMOUS_BUDGET_BLOCK.format(base_template_clause=base_clause)
        if bias_lines:
            budget_block = "\n".join(bias_lines) + "\n" + budget_block

        await self._progress("designing", "autonomous")
        delta = await self._synthesize_delta(
            current,
            trigger_context="An autonomous personality-driven reflection cycle.",
            budget_block=budget_block,
            allow_base_template_change=allow_base_template,
        )
        if delta is None:
            logger.info("Trigger B: could not synthesize a valid delta this cycle")
            await self._progress("failed", "autonomous", error="couldn't come up with a valid design")
            return

        merged = merge_delta(current, delta)
        await self._progress("building", "autonomous", summary=look_summary(merged))
        try:
            result = await compile_and_publish(self._settings, merged, is_user_present=self._is_user_active)
        except Exception as exc:
            # Called directly from reflect.maybe_trigger_autonomous_redesign()
            # -- an uncaught exception here would propagate into the
            # reflection loop itself, not just this one cycle.
            logger.exception("Trigger B: compile_and_publish failed")
            await self._progress("failed", "autonomous", error=str(exc)[:200])
            return

        await self._finish(result, "autonomous")
        if result.outcome == "published":
            self._stamp_redesign_metadata(current, merged)
            self._store.add_memory_note(
                f"Autonomously changed appearance during a reflection cycle: {look_summary(merged)}."
            )
        elif result.outcome == "staged":
            # last_redesign_timestamp is deliberately NOT stamped here --
            # it's stamped at reveal time, not at staging time, so the
            # cooldown can't quietly complete during a long Away period
            # without the change ever actually being revealed.
            self._store.set_agent_state("staged_version", str(result.version))

    # --- presence-transition reveal hook ------------------------------------

    async def on_presence_transition(self, old_state: PresenceState, new_state: PresenceState) -> None:
        """Wired as `DesktopPresenceBridge`'s `on_transition` callback.
        Reveals a staged build exactly on an Away|Idle -> Active edge, never
        on boot (boot reconciliation is a separate method below)."""
        if new_state != PresenceState.ACTIVE or old_state == PresenceState.ACTIVE:
            return

        staged_raw = self._store.get_agent_state("staged_version")
        if staged_raw is None:
            return

        version = int(staged_raw)
        current = self.current_look_spec()
        try:
            result = reveal_staged(self._settings, version)
        except Exception:
            # Must never crash the caller -- this runs inside a
            # DesktopPresenceBridge watcher task, and an uncaught
            # exception here would take that whole task down.
            # The staged file can legitimately go missing between boot
            # reconciliation and an observed transition (e.g. manual
            # cleanup mid-session); clear the stale pointer and move on.
            logger.exception("failed to reveal staged version %s on presence transition; clearing it", version)
            self._store.set_agent_state("staged_version", None)
            return

        self._store.set_agent_state("staged_version", None)
        revealed_entry = result.manifest.get_version(version) if result.manifest is not None else None
        if revealed_entry is not None:
            new_look = LookSpec.model_validate(revealed_entry.look_spec)
            self._stamp_redesign_metadata(current, new_look)
            self._store.add_memory_note(
                f"Revealed an appearance change that was staged while away: {look_summary(new_look)}."
            )

    def reconcile_staged_on_boot(self) -> None:
        """Boot-time crash reconciliation: if `staged_version` is set but
        the file is gone (e.g. the daemon crashed mid-build, or scratch
        was cleaned by hand),
        clear it. Otherwise leave it exactly as-is and wait for a real
        presence transition -- never reveal on boot."""
        staged_raw = self._store.get_agent_state("staged_version")
        if staged_raw is None:
            return
        glb_path = self._settings.staged_dir / f"look-v{staged_raw}.glb"
        if not glb_path.is_file():
            logger.warning("agent_state.staged_version=%s but %s is missing; clearing", staged_raw, glb_path)
            self._store.set_agent_state("staged_version", None)

    # --- asset-swap-failure rollback ----------------------------------------

    def handle_asset_swap_failure(self, failed_version: int) -> None:
        """On `asset_swap_result: {ok: false}`, mark the version corrupted and
        roll the manifest pointer back one. If there's no earlier version
        to fall back to (a fresh install's very first build failing), marks
        it corrupted and logs critical without crashing -- there's no
        "version 0 / no avatar" sentinel in the manifest schema to fall
        back to instead."""
        manifest = load_manifest(self._settings.deploy_dir)
        if manifest is None:
            logger.critical("asset_swap_result ok:false for version %s but no manifest exists", failed_version)
            return
        manifest = mark_version_status(manifest, failed_version, "corrupted")
        if failed_version <= 1:
            logger.critical(
                "version %s failed to swap and there is no earlier version to roll back to "
                "(fresh-install first build) -- leaving current_version pointed at the "
                "corrupted asset rather than crashing",
                failed_version,
            )
            save_manifest(self._settings.deploy_dir, manifest)
            return
        manifest = revert_to_version(manifest, failed_version - 1)
        save_manifest(self._settings.deploy_dir, manifest)
