"""Skills: the things the companion can *do* when asked, as opposed to say.

The model replies with a structured object (see brain/skills/reply.py): `say`
is dialogue and is spoken; `do` is a list of skill calls. A skill is only ever
run if it is registered here, enabled in config, and its arguments pass this
module's validation -- the model proposes, the registry disposes. Anything
else is dropped and reported on the event stream, never executed.

Adding a skill = `registry.register(Skill(...))` (see brain/skills/builtin.py).
Handlers are async, get validated args plus the app state, and return a short
result string (shown on the status page; never spoken).
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from brain.events import emit

logger = logging.getLogger("deskmate.skills")

SKILL_TIMEOUT_S = 8.0
MAX_STRING_ARG = 300

Handler = Callable[[dict[str, Any], Any], Awaitable[str | None]]


class SkillError(ValueError):
    """A proposed call that must not run (unknown skill, bad arguments)."""


@dataclass(frozen=True)
class Param:
    name: str
    description: str
    enum: tuple[str, ...] | None = None  # string restricted to these values
    required: bool = True


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    handler: Handler
    params: tuple[Param, ...] = ()

    def signature(self) -> str:
        parts = []
        for p in self.params:
            kind = "|".join(p.enum) if p.enum else "text"
            parts.append(f"{p.name}: {kind}" + ("" if p.required else " (optional)"))
        return f"{self.name}({', '.join(parts)})"


@dataclass
class SkillCall:
    skill: str
    args: dict[str, Any] = field(default_factory=dict)


class SkillRegistry:
    def __init__(self, *, disabled: set[str] | None = None, max_actions: int = 3) -> None:
        self._skills: dict[str, Skill] = {}
        self._disabled = disabled or set()
        self.max_actions = max_actions
        # In-memory usage counters for the status page (reset on restart).
        self._stats: dict[str, dict[str, Any]] = {}

    def register(self, skill: Skill) -> None:
        self._skills[skill.name] = skill

    @property
    def enabled(self) -> list[Skill]:
        return [s for n, s in self._skills.items() if n not in self._disabled]

    def get(self, name: str) -> Skill | None:
        skill = self._skills.get(name)
        return None if skill is None or name in self._disabled else skill

    def _stat(self, name: str) -> dict[str, Any]:
        return self._stats.setdefault(
            name, {"calls": 0, "failures": 0, "refused": 0, "last_used": None, "last_error": None}
        )

    def describe(self) -> list[dict[str, Any]]:
        """Everything registered, enabled or not, with usage counters, for the
        status page. Read-only."""
        out = []
        for name, skill in self._skills.items():
            out.append(
                {
                    "name": name,
                    "enabled": name not in self._disabled,
                    "signature": skill.signature(),
                    "description": skill.description,
                    "params": [
                        {"name": p.name, "description": p.description, "enum": list(p.enum) if p.enum else None,
                         "required": p.required}
                        for p in skill.params
                    ],
                    **self._stat(name),
                }
            )
        return out

    def prompt_block(self) -> str:
        """The paragraph that teaches the model the reply format and the
        skills it may use. Empty when there are no enabled skills (the model
        then just talks, and the reply is treated as plain dialogue)."""
        skills = self.enabled
        if not skills:
            return ""
        lines = "\n".join(f"- {s.signature()}: {s.description}" for s in skills)
        return (
            'Reply with ONLY a JSON object: {"say": "<what you say out loud>", "do": [{"skill": "<name>", '
            '"args": {...}}]}. "say" is spoken aloud, so write it as plain talk. "do" is optional and lists '
            "skills to run; use it only when the user asks for something a skill covers, or it clearly fits. "
            'Never say you did something unless it is in "do", and never invent skills. Skills you have:\n' + lines
        )

    def validate(self, call: SkillCall) -> dict[str, Any]:
        skill = self.get(call.skill)
        if skill is None:
            raise SkillError(f"unknown or disabled skill {call.skill!r}")
        if not isinstance(call.args, dict):
            raise SkillError("args must be an object")
        known = {p.name for p in skill.params}
        extra = set(call.args) - known
        if extra:
            raise SkillError(f"unexpected arguments: {sorted(extra)}")
        clean: dict[str, Any] = {}
        for p in skill.params:
            value = call.args.get(p.name)
            if value is None or value == "":
                if p.required:
                    raise SkillError(f"missing argument {p.name!r}")
                continue
            if not isinstance(value, str):
                raise SkillError(f"argument {p.name!r} must be text")
            value = value.strip()
            if p.enum is not None and value not in p.enum:
                raise SkillError(f"argument {p.name!r} must be one of {list(p.enum)}")
            if len(value) > MAX_STRING_ARG:
                raise SkillError(f"argument {p.name!r} is too long")
            clean[p.name] = value
        return clean

    async def run_all(self, calls: list[SkillCall], state: Any) -> list[dict[str, Any]]:
        """Runs the proposed calls in order, at most `max_actions` of them.
        One failing skill never stops the others or the reply."""
        results = []
        for call in calls[: self.max_actions]:
            results.append(await self._run_one(call, state))
        for call in calls[self.max_actions :]:
            emit("skill.rejected", {"skill": call.skill, "reason": f"over the limit of {self.max_actions} per reply"})
        return results

    async def _run_one(self, call: SkillCall, state: Any) -> dict[str, Any]:
        try:
            args = self.validate(call)
        except SkillError as exc:
            if call.skill in self._skills:
                stat = self._stat(call.skill)
                stat["refused"] += 1
                stat["last_error"] = str(exc)
            emit("skill.rejected", {"skill": call.skill, "args": call.args if isinstance(call.args, dict) else {}, "reason": str(exc)})
            return {"skill": call.skill, "ok": False, "error": str(exc)}
        skill = self._skills[call.skill]
        started = time.monotonic()
        try:
            result = await asyncio.wait_for(skill.handler(args, state), SKILL_TIMEOUT_S)
            ok, error = True, None
        except Exception as exc:  # noqa: BLE001 -- a skill must never break the reply
            logger.exception("skill %s failed", call.skill)
            result, ok, error = None, False, f"{type(exc).__name__}: {exc}"
        ms = round((time.monotonic() - started) * 1000)
        stat = self._stat(call.skill)
        stat["calls"] += 1
        stat["last_used"] = time.time()
        if not ok:
            stat["failures"] += 1
            stat["last_error"] = error
        emit("skill.call", {"skill": call.skill, "args": args, "ok": ok, "ms": ms, "result": result, "error": error})
        return {"skill": call.skill, "ok": ok, "result": result, "error": error}
