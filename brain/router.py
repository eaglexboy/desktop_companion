"""Capability-aware LLM router: intent/keyword dispatch, classify-then-route, tool registry."""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal

from brain.config import LLMLocalConfig, Settings
from brain.events import emit, llm_purpose
from brain.llm_pool import LLMClient, LLMPool, LocalLLMClient, build_cloud_client

logger = logging.getLogger(__name__)

# "under 8 words" and _CLASSIFY_MAX_TOKENS=128 (not 64) keep the
# classifier's JSON reply from being truncated mid-string by a model that
# writes a long "reason" field -- a truncated reply is invalid JSON, which,
# per the Classifier Fallback Rule below, would silently default a
# correctly-identified-complex question back to local.
_CAPABILITY_TAGS = ("code", "complex_reasoning", "large_context", "general")

# The classifier doesn't just decide escalate true/false, it also names
# *which kind* of cloud strength the query needs, so handle() can prefer a
# provider whose [llm.cloud.<name>] capabilities list actually matches (see
# LLMPool.chat()'s required_capability parameter) instead of always trying
# cloud providers in a fixed priority order regardless of what the question
# is actually about.
_CLASSIFY_PROMPT = (
    "You are a routing classifier for a desktop AI companion. Decide whether "
    "the following user message needs a more powerful cloud model (complex "
    "reasoning, long-context analysis, code generation) or can be answered "
    "by a fast local model. If it needs a cloud model, also name which kind "
    'of capability it needs. Respond with ONLY compact JSON: {{"escalate": '
    'true|false, "capability": "code"|"complex_reasoning"|"large_context"|"general", '
    '"reason": "<short reason, under 8 words>"}}.\n\nUser message: {user_text}'
)
_CLASSIFY_MAX_TOKENS = 128

# Small local models occasionally wrap or follow the JSON object with
# commentary (observed live: one candidate rambled a paragraph past its own
# JSON answer before hitting its token limit) -- extracting the first
# {...} substring is cheap defense-in-depth against that, independent of
# which model ends up configured. It does NOT help with a malformed value
# *inside* the braces (also observed live, rarely); that case is still
# caught by the try/except below and falls through to the same fallback.
_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)


def _extract_json_object(raw: str) -> str:
    match = _JSON_OBJECT_RE.search(raw)
    return match.group(0) if match else raw

EscalationThreshold = Literal["auto", "always_local", "always_cloud"]


@dataclass
class RouterResult:
    text: str
    source: str
    escalated: bool
    reason: str | None = None
    capability: str | None = None


@dataclass(frozen=True)
class ReplyFormat:
    """A reply shape the router enforces without provider support: the
    reminder rides on the user message, and a reply failing `is_valid` gets one
    repair round-trip."""

    reminder: str
    repair_prompt: str
    is_valid: Callable[[str], bool]


ToolHandler = Callable[[str], Awaitable[Any]]


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: ToolHandler


# Starter keyword pre-filter for the two built-in appearance tools, for
# models without native tool-calling. Inherently fuzzy; expect to tune
# empirically rather than treat this list as exhaustive.
_CHANGE_APPEARANCE_KEYWORDS = (
    "change your look",
    "change your appearance",
    "new look",
    "new outfit",
    "wear a",
    "wear some",
    "put on a",
    "put on some",
    "redesign yourself",
    "try a different look",
    "dress differently",
)
_REVERT_APPEARANCE_KEYWORDS = (
    "go back to your old look",
    "revert your look",
    "undo your look",
    "change back",
    "old appearance",
    "previous look",
)


class Router:
    """Classify-then-route between a local and a cloud LLMPool, plus an
    open tool-registration hook for future agent/skill orchestration."""

    def __init__(
        self,
        local_pool: LLMPool,
        cloud_pool: LLMPool,
        *,
        classifier: LLMClient | None = None,
        escalation_threshold: EscalationThreshold = "auto",
    ) -> None:
        if local_pool.is_empty and cloud_pool.is_empty:
            raise RuntimeError("Router requires at least one local or cloud LLM provider")
        self._local_pool = local_pool
        self._cloud_pool = cloud_pool
        self._classifier = classifier
        self._escalation_threshold = escalation_threshold
        self._tools: dict[str, ToolSpec] = {}

    def register_tool(
        self, name: str, description: str, parameters: dict[str, Any], handler: ToolHandler
    ) -> None:
        self._tools[name] = ToolSpec(name, description, parameters, handler)

    @property
    def tools(self) -> dict[str, ToolSpec]:
        return dict(self._tools)

    def _match_tool_keyword(self, user_text: str) -> str | None:
        lowered = user_text.lower()
        if any(kw in lowered for kw in _CHANGE_APPEARANCE_KEYWORDS):
            return "change_appearance"
        if any(kw in lowered for kw in _REVERT_APPEARANCE_KEYWORDS):
            return "revert_appearance"
        return None

    async def _classify(self, user_text: str) -> tuple[bool, str, str]:
        """Returns (escalate, capability, reason). capability is only
        meaningful when escalate is True; it's always "general" otherwise."""
        if self._escalation_threshold == "always_local":
            return False, "general", "escalation_threshold=always_local"
        if self._escalation_threshold == "always_cloud":
            return True, "general", "escalation_threshold=always_cloud"
        if self._classifier is None:
            return False, "general", "no classifier configured, defaulting to local"
        started = time.monotonic()

        # Classifier Fallback Rule: on timeout or error, default to LOCAL
        # execution (escalated=false) rather than fail-safe toward the
        # stronger (and costlier) cloud model.
        try:
            with llm_purpose("classify"):
                raw = await self._classifier.chat(
                    [{"role": "user", "content": _CLASSIFY_PROMPT.format(user_text=user_text)}],
                    max_tokens=_CLASSIFY_MAX_TOKENS,
                )
            verdict = json.loads(_extract_json_object(raw))
            escalate = bool(verdict.get("escalate", False))
            capability = str(verdict.get("capability", "general")) or "general"
            if capability not in _CAPABILITY_TAGS:
                capability = "general"
            reason = str(verdict.get("reason", ""))
            emit(
                "router.classified",
                {"escalate": escalate, "capability": capability, "reason": reason, "raw": raw[:300],
                 "ms": round((time.monotonic() - started) * 1000)},
            )
            return escalate, capability, reason
        except Exception as exc:  # noqa: BLE001 -- flaky classification must never crash chat
            logger.warning("Classifier failed (%s); defaulting to local execution", exc)
            emit("router.classify_failed", {"error": str(exc)[:300], "ms": round((time.monotonic() - started) * 1000)})
            return False, "general", f"classifier error: {exc}"

    async def handle(
        self,
        user_text: str,
        *,
        system_prompt: str,
        history: list[dict[str, str]] | None = None,
        reply_format: ReplyFormat | None = None,
    ) -> RouterResult:
        tool_name = self._match_tool_keyword(user_text)
        if tool_name is not None and tool_name in self._tools:
            emit("router.keyword", {"tool": tool_name})
            tool = self._tools[tool_name]
            result = await tool.handler(user_text)
            return RouterResult(
                text=str(result), source=f"tool:{tool_name}", escalated=False, reason="keyword pre-filter match"
            )

        escalate, capability, reason = await self._classify(user_text)
        pool = self._cloud_pool if escalate else self._local_pool
        wanted_escalation = escalate
        if escalate and pool.is_empty:
            logger.warning("Classifier wants to escalate but no cloud pool is configured; falling back to local")
            pool = self._local_pool
            escalate = False

        messages = [{"role": "system", "content": system_prompt}]
        messages.extend(history or [])
        user_content = f"{user_text}\n\n{reply_format.reminder}" if reply_format else user_text
        messages.append({"role": "user", "content": user_content})

        emit(
            "router.route",
            {"pool": "cloud" if escalate else "local", "wanted_escalation": wanted_escalation,
             "fell_back_to_local": wanted_escalation and not escalate, "capability": capability if escalate else None,
             "reason": reason},
        )
        if escalate:
            # Capability is a preference, not a hard filter -- LLMPool.chat()
            # tries a matching-capability provider first, then falls back to
            # every other enabled provider in priority order if none match
            # (see llm_pool.py's LLMPool docstring).
            with llm_purpose("reply"):
                source, reply = await pool.chat(messages, max_tokens=1024, required_capability=capability)
        else:
            with llm_purpose("reply"):
                source, reply = await pool.chat(messages, max_tokens=1024)
        if reply_format and not reply_format.is_valid(reply):
            # Provider-agnostic repair: one re-ask with the bad reply in context.
            # If it is still malformed, the original is kept (the caller falls
            # back to treating it as plain dialogue).
            emit("reply.repair", {"source": source, "bad": reply[:300]})
            repair = messages + [
                {"role": "assistant", "content": reply},
                {"role": "user", "content": reply_format.repair_prompt},
            ]
            try:
                with llm_purpose("reply"):
                    source2, fixed = await pool.chat(repair, max_tokens=1024)
                if reply_format.is_valid(fixed):
                    source, reply = source2, fixed
            except Exception as exc:  # noqa: BLE001 -- a failed repair must never lose the reply we already have
                logger.warning("Reply repair failed (%s); keeping the original reply", exc)
        return RouterResult(text=reply, source=source, escalated=escalate, reason=reason, capability=capability if escalate else None)

    async def aclose(self) -> None:
        await self._local_pool.aclose()
        await self._cloud_pool.aclose()


def build_router(settings: Settings) -> Router:
    """Wire a real Router from Settings: one local client, a priority-ordered
    cloud pool, a cascaded classifier client, and the two built-in appearance
    tools registered as stubs -- real callers overwrite these registrations
    once the appearance pipeline is wired up (see
    AvatarEvolution.wire_tools())."""
    local_pool = LLMPool([("local", LocalLLMClient(settings.llm.local), [])])

    cloud_items = sorted(
        ((name, cfg) for name, cfg in settings.llm.cloud.items() if cfg.enabled),
        key=lambda item: -item[1].priority,
    )
    cloud_pool = LLMPool([(name, build_cloud_client(cfg), cfg.capabilities) for name, cfg in cloud_items])

    classifier_cfg = settings.llm.resolved_classifier()
    classifier_client = LocalLLMClient(
        LLMLocalConfig(
            base_url=classifier_cfg.base_url,
            model=classifier_cfg.model,
            timeout_s=classifier_cfg.timeout_s,
            extra_body=classifier_cfg.extra_body,
        )
    )

    router = Router(
        local_pool,
        cloud_pool,
        classifier=classifier_client,
        escalation_threshold=settings.llm.routing.escalation_threshold,
    )

    async def _change_appearance(request: str) -> str:
        raise NotImplementedError("change_appearance is a stub -- not yet wired to the appearance pipeline")

    async def _revert_appearance(_request: str = "") -> str:
        raise NotImplementedError("revert_appearance is a stub -- not yet wired to the appearance pipeline")

    router.register_tool(
        "change_appearance",
        "Change the companion's visual appearance based on a free-form request.",
        {"type": "object", "properties": {"request": {"type": "string"}}, "required": ["request"]},
        _change_appearance,
    )
    router.register_tool(
        "revert_appearance",
        "Revert the companion's appearance to its previous look.",
        {"type": "object", "properties": {}},
        _revert_appearance,
    )
    return router
