"""Splits a model reply into dialogue and actions.

Small local models don't always follow a format. So the parse is deliberately
forgiving in one direction only: if the reply isn't a usable `{"say": ...}`
object, the *whole text* is dialogue and no actions run. Never the other way
round -- the model's text is never guessed into an action.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from brain.skills.registry import SkillCall

_FENCE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL | re.IGNORECASE)


@dataclass
class ParsedReply:
    say: str
    actions: list[SkillCall] = field(default_factory=list)
    structured: bool = False


def _first_object(text: str) -> dict[str, Any] | None:
    start = text.find("{")
    if start < 0:
        return None
    try:
        obj, _end = json.JSONDecoder().raw_decode(text[start:])
    except ValueError:
        return None
    return obj if isinstance(obj, dict) else None


def parse_reply(raw: str) -> ParsedReply:
    text = raw.strip()
    fenced = _FENCE.match(text)
    if fenced:
        text = fenced.group(1)
    obj = _first_object(text)
    if obj is None or not isinstance(obj.get("say"), str):
        return ParsedReply(say=raw.strip(), structured=False)

    actions: list[SkillCall] = []
    do = obj.get("do")
    if isinstance(do, list):
        for item in do:
            if isinstance(item, dict) and isinstance(item.get("skill"), str):
                args = item.get("args")
                actions.append(SkillCall(item["skill"], args if isinstance(args, dict) else {}))
    return ParsedReply(say=obj["say"].strip(), actions=actions, structured=True)
