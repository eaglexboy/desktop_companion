"""The skills that ship with the companion. Each wraps something the brain
already does; none can reach beyond that (no shell, no files)."""

from __future__ import annotations

from typing import Any

from brain.skills.registry import Param, Skill, SkillRegistry

GESTURES = ("greet", "agree", "disagree", "celebrate", "shrug")


async def _gesture(args: dict[str, Any], state: Any) -> str:
    name = args["name"]
    # `name` is what the overlay reads; `clip` is the documented protocol field.
    await state.hub.broadcast("play_gesture", {"name": name, "clip": name, "loop": False})
    return f"played {name}"


async def _remember(args: dict[str, Any], state: Any) -> str:
    state.store.add_memory_note(args["note"])
    return "saved"


async def _change_look(args: dict[str, Any], state: Any) -> str:
    return await state.evolution.change_appearance(args["request"])


async def _revert_look(_args: dict[str, Any], state: Any) -> str:
    return await state.evolution.revert_appearance()


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
            "redesign your own appearance (takes a minute to build); say what you want to look like",
            _change_look,
            (Param("request", "the look the user asked for"),),
        )
    )
    registry.register(Skill("revert_look", "go back to your previous appearance", _revert_look))
