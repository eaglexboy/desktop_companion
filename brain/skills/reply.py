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


# Appended to the last user message of every skills-enabled turn, and used as the
# repair prompt when a reply still isn't JSON. Restating the format right where
# the model starts writing is what small models actually obey, and it needs
# nothing from the provider (no response_format / guided decoding).
FORMAT_REMINDER = (
    'Format: reply with ONLY the JSON object {"say": "...", "do": []}, even for one word or a vocal cue. '
    'Put any *sigh* or *laughs* inside the "say" string. No text outside the JSON.'
)
REPAIR_PROMPT = (
    "That reply was not the required JSON object. Resend the same reply, unchanged in wording, as "
    '{"say": "<your reply>", "do": []} and nothing else.'
)


@dataclass
class ParsedReply:
    say: str
    actions: list[SkillCall] = field(default_factory=list)
    structured: bool = False


# A reply cut off inside the object: `{"say": "Hi", "do": []` (the closing brace never came) or
# `{"say": "Hi, there` (cut off in the middle of the sentence).
_TRUNCATED_SAY = re.compile(r'^\s*\{\s*"say"\s*:\s*"((?:[^"\\]|\\.)*)', re.DOTALL)


def _first_object(text: str) -> dict[str, Any] | None:
    start = text.find("{")
    if start < 0:
        return None
    for tail in (
        "",
        "}",
    ):  # the second: the object was complete but for its closing brace
        try:
            obj, _end = json.JSONDecoder().raw_decode(text[start:] + tail)
        except ValueError:
            continue
        return obj if isinstance(obj, dict) else None
    return None


def _truncated_say(text: str) -> str | None:
    """The dialogue of a reply that was cut off before it was a whole object. Only the "say" string is
    taken (what was cut off is never run as an action)."""
    match = _TRUNCATED_SAY.match(text)
    if not match:
        return None
    try:
        return json.loads(f'"{match.group(1)}"').strip()
    except ValueError:  # cut off inside an escape
        return match.group(1).rstrip("\\").strip()


def parse_reply(raw: str) -> ParsedReply:
    text = raw.strip()
    fenced = _FENCE.match(text)
    if fenced:
        text = fenced.group(1)
    obj = _first_object(text)
    if obj is None or not isinstance(obj.get("say"), str):
        cut = _truncated_say(text)
        if cut:
            return ParsedReply(say=cut, structured=True)
        return ParsedReply(say=raw.strip(), structured=False)

    actions: list[SkillCall] = []
    do = obj.get("do")
    if isinstance(do, list):
        for item in do:
            if isinstance(item, dict) and isinstance(item.get("skill"), str):
                args = item.get("args")
                actions.append(
                    SkillCall(item["skill"], args if isinstance(args, dict) else {})
                )
    return ParsedReply(say=obj["say"].strip(), actions=actions, structured=True)


def is_structured(raw: str) -> bool:
    return parse_reply(raw).structured
