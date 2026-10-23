"""LLM client abstraction and priority-ordered failover pool."""

from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field

import httpx

from brain.config import LLMCloudConfig, LLMLocalConfig
from brain.events import current_purpose, emit

logger = logging.getLogger(__name__)

Message = dict[str, str]

_FAILURE_COOLDOWN_S = 60.0


class LLMClient(ABC):
    """Common interface every local/cloud LLM integration implements."""

    @abstractmethod
    async def chat(self, messages: list[Message], *, max_tokens: int = 1024, **kwargs) -> str:
        """Send conversation history to the model and return the response text."""

    async def health_check(self) -> bool:
        return True

    async def aclose(self) -> None:
        return None


class LocalLLMClient(LLMClient):
    """Any OpenAI-compatible local server (vLLM, Ollama, llama.cpp, ...)."""

    def __init__(self, config: LLMLocalConfig) -> None:
        self._config = config
        self._http = httpx.AsyncClient(base_url=config.base_url, timeout=config.timeout_s)

    async def chat(self, messages: list[Message], *, max_tokens: int = 1024, **kwargs) -> str:
        payload = {
            "model": self._config.model,
            "messages": messages,
            "max_tokens": max_tokens,
            **self._config.extra_body,
            **kwargs,
        }
        if self._config.reasoning:
            template_kwargs = dict(payload.get("chat_template_kwargs") or {})
            template_kwargs["enable_thinking"] = True
            payload["chat_template_kwargs"] = template_kwargs
            payload["max_tokens"] = max_tokens + self._config.reasoning_extra_tokens
        started = time.monotonic()
        try:
            response = await self._http.post("/chat/completions", json=payload)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            emit("llm.error", {"model": self._config.model, "purpose": current_purpose(), "error": type(exc).__name__})
            raise
        data = response.json()
        message = data["choices"][0]["message"]
        content = message.get("content")
        # Whatever the server returns as reasoning is surfaced (not spoken):
        # the field is `reasoning_content` on older vLLM, `reasoning` on newer.
        reasoning = message.get("reasoning_content") or message.get("reasoning")
        usage = data.get("usage") or {}
        emit(
            "llm.call",
            {
                "model": self._config.model,
                "purpose": current_purpose(),
                "ms": round((time.monotonic() - started) * 1000),
                "prompt_tokens": usage.get("prompt_tokens"),
                "completion_tokens": usage.get("completion_tokens"),
                "reasoning_enabled": self._config.reasoning,
                "has_reasoning": bool(reasoning),
            },
        )
        if reasoning:
            emit("llm.reasoning", {"model": self._config.model, "purpose": current_purpose(), "text": reasoning})
        if not content:
            # Reasoning models can spend their whole max_tokens budget
            # "thinking" and return content: null instead of raising.
            # Treating this as a hard error (not silently returning None)
            # is what makes pool failover and caller error-handling
            # actually fire.
            reasoning = message.get("reasoning") or message.get("reasoning_content")
            raise RuntimeError(
                f"Local LLM at {self._config.base_url} returned empty content "
                f"(reasoning={reasoning!r}). If this is a reasoning model, it likely spent "
                "its whole max_tokens budget thinking before emitting an answer. Fix: add "
                '`extra_body = { chat_template_kwargs = { enable_thinking = false } }` '
                "to this server's [llm.local] config."
            )
        return content

    async def health_check(self) -> bool:
        try:
            response = await self._http.get("/models")
            return response.status_code == 200
        except httpx.HTTPError:
            return False

    async def aclose(self) -> None:
        await self._http.aclose()


class AnthropicCloudClient(LLMClient):
    """Anthropic Messages API. The `anthropic` SDK is imported lazily so
    local-only deployments never need it installed."""

    def __init__(self, config: LLMCloudConfig) -> None:
        from anthropic import AsyncAnthropic  # lazy import -- not a hard dependency

        self._config = config
        self._client = AsyncAnthropic(api_key=config.api_key, base_url=config.base_url, timeout=config.timeout_s)

    async def chat(self, messages: list[Message], *, max_tokens: int = 1024, **kwargs) -> str:
        system_prompt: str | None = None
        chat_messages: list[Message] = []
        for m in messages:
            if m["role"] == "system":
                system_prompt = m["content"]
            else:
                chat_messages.append(m)

        create_kwargs: dict = {
            "model": self._config.model,
            "messages": chat_messages,
            "max_tokens": max_tokens,
            **kwargs,
        }
        if system_prompt is not None:
            create_kwargs["system"] = system_prompt

        response = await self._client.messages.create(**create_kwargs)
        content = "".join(block.text for block in response.content if block.type == "text")
        if not content:
            raise RuntimeError(f"Anthropic provider '{self._config.model}' returned empty content")
        return content

    async def aclose(self) -> None:
        await self._client.close()


class GenericOpenAICloudClient(LLMClient):
    """Any OpenAI-compatible cloud endpoint (OpenAI itself, OpenRouter, etc.)."""

    def __init__(self, config: LLMCloudConfig) -> None:
        self._config = config
        self._http = httpx.AsyncClient(
            base_url=config.base_url,
            timeout=config.timeout_s,
            headers={"Authorization": f"Bearer {config.api_key}"},
        )

    async def chat(self, messages: list[Message], *, max_tokens: int = 1024, **kwargs) -> str:
        payload = {"model": self._config.model, "messages": messages, "max_tokens": max_tokens, **kwargs}
        response = await self._http.post("/chat/completions", json=payload)
        response.raise_for_status()
        data = response.json()
        content = data["choices"][0]["message"].get("content")
        if not content:
            raise RuntimeError(f"Cloud LLM at {self._config.base_url} returned empty content")
        return content

    async def aclose(self) -> None:
        await self._http.aclose()


_CLOUD_CLIENT_REGISTRY: dict[str, Callable[[LLMCloudConfig], LLMClient]] = {
    "anthropic": AnthropicCloudClient,
    "generic_openai": GenericOpenAICloudClient,
}


def build_cloud_client(config: LLMCloudConfig) -> LLMClient:
    try:
        factory = _CLOUD_CLIENT_REGISTRY[config.provider_type]
    except KeyError:
        raise ValueError(
            f"Unknown provider_type '{config.provider_type}'. Known: {sorted(_CLOUD_CLIENT_REGISTRY)}"
        ) from None
    return factory(config)


@dataclass
class _PoolEntry:
    name: str
    client: LLMClient
    capabilities: tuple[str, ...] = field(default=())
    cooldown_until: float = field(default=0.0)

    def is_cooled_down(self, now: float) -> bool:
        return now >= self.cooldown_until


class LLMPool:
    """A priority-ordered list of LLMClients with failover and optional
    capability-biased selection (provider selection by capability tags).

    Policy:
      - timeout -> 1 immediate retry against the same provider; if that also
        fails, 60s cooldown and fail over to the next provider.
      - HTTP 429/401/403, or any other error -> no retry, immediate 60s
        cooldown, fail over to the next provider.
      - every candidate currently in cooldown -> raise immediately rather
        than hammering providers mid-cooldown.
      - all candidates exhausted -> raise.
      - chat(required_capability=...): providers whose `capabilities` list
        contains the requested tag are tried first (priority order within
        that group), THEN every other provider as a fallback (priority order
        within that group too) -- a capability tag is a *preference*, not a
        hard filter, so the pool still degrades gracefully to "try anything"
        rather than refusing outright when nothing is tagged for the need.
    """

    def __init__(
        self,
        entries: list[tuple[str, LLMClient, list[str]]],
        *,
        cooldown_s: float = _FAILURE_COOLDOWN_S,
    ) -> None:
        """entries: (name, client, capabilities) triples. Pass an empty
        capabilities list for pools that don't need capability matching
        (e.g. the local pool, which is always a single entry anyway)."""
        self._entries = [_PoolEntry(name, client, tuple(capabilities)) for name, client, capabilities in entries]
        self._cooldown_s = cooldown_s

    @property
    def is_empty(self) -> bool:
        return not self._entries

    async def chat(
        self, messages: list[Message], *, required_capability: str | None = None, **kwargs
    ) -> tuple[str, str]:
        now = time.monotonic()
        available = [e for e in self._entries if e.is_cooled_down(now)]
        if not available:
            raise RuntimeError("All LLM providers are in cooldown")

        if required_capability is not None:
            matching = [e for e in available if required_capability in e.capabilities]
            rest = [e for e in available if required_capability not in e.capabilities]
            candidates = matching + rest
        else:
            candidates = available

        last_error: Exception | None = None
        for index, entry in enumerate(candidates):
            try:
                reply = await self._attempt(entry, messages, kwargs)
                return entry.name, reply
            except Exception as exc:  # noqa: BLE001 -- deliberately blanket, see policy above
                last_error = exc
                entry.cooldown_until = time.monotonic() + self._cooldown_s
                self._log_failover(entry, candidates, index, str(exc))

        raise RuntimeError(f"All LLM providers failed: {last_error}") from last_error

    async def _attempt(self, entry: _PoolEntry, messages: list[Message], kwargs: dict) -> str:
        try:
            return await entry.client.chat(messages, **kwargs)
        except httpx.TimeoutException:
            # Exactly one immediate retry on timeout; whatever this raises
            # (including a second timeout) propagates to the caller, which
            # treats it the same as any other failure (cooldown + failover).
            return await entry.client.chat(messages, **kwargs)

    def _log_failover(self, entry: _PoolEntry, candidates: list[_PoolEntry], index: int, reason: str) -> None:
        if index + 1 < len(candidates):
            next_name = candidates[index + 1].name
            logger.warning("LLM provider '%s' failed (%s); failing over to '%s'", entry.name, reason, next_name)
        else:
            logger.warning("LLM provider '%s' failed (%s); no more providers to try", entry.name, reason)

    async def aclose(self) -> None:
        for entry in self._entries:
            await entry.client.aclose()
