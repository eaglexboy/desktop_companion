"""The skills that ship with the companion. Each wraps something the brain
already does; none can reach beyond that (no shell, no files)."""

from __future__ import annotations

from typing import Any

from brain.skills.registry import Param, Skill, SkillRegistry

GESTURES = ("greet", "agree", "disagree", "celebrate", "shrug", "angry", "hip_thrust")


def gesture_intensity(state: Any) -> float:
    """How animated a gesture should be, 0 (subdued) to 1 (eager): 0.5 at its resting mood, more
    when the companion is happy and energetic, less when it is low or stressed. The overlay plays
    the matching variant of a clip (a smaller or a bigger greeting wave)."""
    try:
        mood = state.store.get_mood()
        lift = 0.6 * (mood.get("valence", 50.0) - 50.0) + 0.4 * (mood.get("energy", 50.0) - 50.0) - 0.3 * (mood.get("stress", 50.0) - 50.0)
    except Exception:  # noqa: BLE001 -- a gesture must never fail for want of a mood
        return 0.5
    return max(0.0, min(1.0, 0.5 + lift / 100.0))


async def _gesture(args: dict[str, Any], state: Any) -> str:
    name = args["name"]
    # `name` is what the overlay reads; `clip` is the documented protocol field.
    await state.hub.broadcast("play_gesture", {"name": name, "clip": name, "loop": False, "intensity": gesture_intensity(state)})
    return f"played {name}"


async def _remember(args: dict[str, Any], state: Any) -> str:
    state.store.add_memory_note(args["note"])
    return "saved"


async def _change_look(args: dict[str, Any], state: Any) -> str:
    return await state.evolution.change_appearance(args["request"])


async def _revert_look(args: dict[str, Any], state: Any) -> str:
    version = str(args.get("version", "")).strip()
    return await state.evolution.revert_appearance(f"version {version}" if version.isdigit() else "")


async def _unlock_look(_args: dict[str, Any], state: Any) -> str:
    return await state.evolution.unlock_appearance()


def register_builtin(registry: SkillRegistry) -> None:
    registry.register(
        Skill(
            "gesture",
            "do a quick body reaction on screen",
            _gesture,
            (Param("name", "which reaction", enum=GESTURES),),
        )
    )
    registry.register(
        Skill(
            "remember",
            "keep a short note about the user for later conversations",
            _remember,
            (Param("note", "one short fact worth remembering"),),
        )
    )
    registry.register(
        Skill(
            "change_look",
            "redesign your own appearance (takes a minute to build); say what you want to look like. "
            "Declined while you wear an imported model you were asked to keep",
            _change_look,
            (Param("request", "the look the user asked for"),),
        )
    )
    registry.register(
        Skill(
            "revert_look",
            "go back to your previous appearance, or to a numbered look from the status page's Looks tab",
            _revert_look,
            (Param("version", "the look number to go back to; leave out for the previous look", required=False),),
        )
    )
    registry.register(
        Skill(
            "unlock_look",
            "free yourself to change your look again; use when the user says you may change your look "
            "(you keep an imported model until they do)",
            _unlock_look,
        )
    )
