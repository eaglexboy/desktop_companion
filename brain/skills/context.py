"""What third-party skills cost in the prompt, and what fits.

The cost of skills must not grow with how many are installed (ADR "Skill
Activation & Context Budget", invariant 1): only pinned skills get a one-line
stub, and the stubs together must fit a budget derived from the context window.
In V1 imported skills are inert, so nothing here is put in a prompt yet; this is
the builder and the meter, so the numbers are real before the runtime exists.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

STUB_MAX_CHARS = 160
CHARS_PER_TOKEN = 4  # deliberately coarse and slightly pessimistic for English; no tokenizer dependency

_SENTENCE_END = re.compile(r"(?<=[.!?])\s")


def estimate_tokens(text: str) -> int:
    return math.ceil(len(text) / CHARS_PER_TOKEN) if text else 0


def stub(display_name: str, description: str) -> str:
    """`name: first sentence of the description`, one line, at most STUB_MAX_CHARS."""
    one_line = " ".join(description.split())
    first = _SENTENCE_END.split(one_line, maxsplit=1)[0]
    line = f"{display_name}: {first}"
    return line if len(line) <= STUB_MAX_CHARS else line[: STUB_MAX_CHARS - 1].rstrip() + "…"


def budget_tokens(context_tokens: int, fraction: float, cap: int) -> int:
    """`min(cap, fraction x context window)`."""
    return max(0, min(cap, int(context_tokens * fraction)))


@dataclass(frozen=True)
class StubPlan:
    kept: list[tuple[str, str]]     # (skill id, stub text) that fit, in the order given
    dropped: list[str]              # skill ids that did not fit
    tokens_used: int
    budget: int


def plan_stubs(stubs: list[tuple[str, str]], budget: int) -> StubPlan:
    """Keeps stubs in the order given until the budget is spent; the rest are
    dropped (and reported), never truncated. Each kept stub counts a newline."""
    kept: list[tuple[str, str]] = []
    dropped: list[str] = []
    used = 0
    for sid, text in stubs:
        cost = estimate_tokens(text + "\n")
        if used + cost <= budget:
            kept.append((sid, text))
            used += cost
        else:
            dropped.append(sid)
    return StubPlan(kept, dropped, used, budget)
