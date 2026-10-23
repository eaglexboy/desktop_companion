"""Tests for brain.llm_pool: client adapters and the failover pool policy."""

from __future__ import annotations

import httpx
import pytest

from brain.config import LLMCloudConfig, LLMLocalConfig
from brain.llm_pool import (
    GenericOpenAICloudClient,
    LLMClient,
    LLMPool,
    LocalLLMClient,
    Message,
    build_cloud_client,
)

_DEFAULT_LOCAL_CONFIG = LLMLocalConfig(base_url="http://127.0.0.1:11434/v1", model="test-model")
_DEFAULT_CLOUD_CONFIG = LLMCloudConfig(
    provider_type="generic_openai",
    base_url="https://cloud.example.com/v1",
    model="cloud-model",
    api_key="sk-test",
)


def _local_config(**overrides) -> LLMLocalConfig:
    return _DEFAULT_LOCAL_CONFIG.model_copy(update=overrides)


def _cloud_config(**overrides) -> LLMCloudConfig:
    return _DEFAULT_CLOUD_CONFIG.model_copy(update=overrides)


def _mock_transport(handler) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


class _FakeClient(LLMClient):
    """A minimal LLMClient stand-in for pool-policy tests (no real HTTP)."""

    def __init__(self, name: str, behavior) -> None:
        self.name = name
        self._behavior = iter(behavior)
        self.call_count = 0

    async def chat(self, messages: list[Message], *, max_tokens: int = 1024, **kwargs) -> str:
        self.call_count += 1
        outcome = next(self._behavior)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


# --- LocalLLMClient ---------------------------------------------------------


@pytest.mark.asyncio
async def test_local_llm_client_success() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": "hello there"}}]})

    client = LocalLLMClient(_local_config())
    client._http = httpx.AsyncClient(
        base_url="http://127.0.0.1:11434/v1", transport=_mock_transport(handler)
    )
    try:
        reply = await client.chat([{"role": "user", "content": "hi"}])
        assert reply == "hello there"
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_local_llm_client_empty_content_raises_with_reasoning_hint() -> None:
    """A reasoning model can spend its whole max_tokens budget thinking and
    return content: null instead of raising."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": None, "reasoning": "Thinking...\n1. ..."}}]},
        )

    client = LocalLLMClient(_local_config())
    client._http = httpx.AsyncClient(
        base_url="http://127.0.0.1:11434/v1", transport=_mock_transport(handler)
    )
    try:
        with pytest.raises(RuntimeError, match="enable_thinking"):
            await client.chat([{"role": "user", "content": "hi"}])
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_local_llm_client_extra_body_merged_into_request() -> None:
    seen_payload = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        seen_payload.update(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    config = _local_config(extra_body={"chat_template_kwargs": {"enable_thinking": False}})
    client = LocalLLMClient(config)
    client._http = httpx.AsyncClient(
        base_url="http://127.0.0.1:11434/v1", transport=_mock_transport(handler)
    )
    try:
        await client.chat([{"role": "user", "content": "hi"}])
        assert seen_payload["chat_template_kwargs"] == {"enable_thinking": False}
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_local_llm_client_health_check_true_and_false() -> None:
    def ok_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": []})

    client = LocalLLMClient(_local_config())
    client._http = httpx.AsyncClient(base_url="http://x/v1", transport=_mock_transport(ok_handler))
    assert await client.health_check() is True
    await client.aclose()

    def fail_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    client = LocalLLMClient(_local_config())
    client._http = httpx.AsyncClient(base_url="http://x/v1", transport=_mock_transport(fail_handler))
    assert await client.health_check() is False
    await client.aclose()


# --- GenericOpenAICloudClient ------------------------------------------------


@pytest.mark.asyncio
async def test_generic_openai_cloud_client_sends_bearer_token() -> None:
    seen_headers: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen_headers.update(request.headers)
        return httpx.Response(200, json={"choices": [{"message": {"content": "cloud reply"}}]})

    config = _cloud_config(api_key="sk-secret-value")
    client = GenericOpenAICloudClient(config)
    client._http = httpx.AsyncClient(
        base_url=config.base_url, headers={"Authorization": f"Bearer {config.api_key}"}, transport=_mock_transport(handler)
    )
    try:
        reply = await client.chat([{"role": "user", "content": "hi"}])
        assert reply == "cloud reply"
        assert seen_headers["authorization"] == "Bearer sk-secret-value"
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_generic_openai_cloud_client_empty_content_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": ""}}]})

    client = GenericOpenAICloudClient(_cloud_config())
    client._http = httpx.AsyncClient(base_url="https://x", transport=_mock_transport(handler))
    try:
        with pytest.raises(RuntimeError):
            await client.chat([{"role": "user", "content": "hi"}])
    finally:
        await client.aclose()


# --- build_cloud_client registry dispatch -----------------------------------


def test_build_cloud_client_dispatches_generic_openai() -> None:
    client = build_cloud_client(_cloud_config(provider_type="generic_openai"))
    assert isinstance(client, GenericOpenAICloudClient)


def test_build_cloud_client_dispatches_anthropic() -> None:
    from brain.llm_pool import AnthropicCloudClient

    client = build_cloud_client(_cloud_config(provider_type="anthropic"))
    assert isinstance(client, AnthropicCloudClient)


def test_build_cloud_client_unknown_provider_type_raises() -> None:
    with pytest.raises(ValueError, match="Unknown provider_type"):
        build_cloud_client(_cloud_config(provider_type="totally_unknown"))


# --- LLMPool failover policy -------------------------------------------------


@pytest.mark.asyncio
async def test_pool_is_empty() -> None:
    assert LLMPool([]).is_empty is True
    assert LLMPool([("a", _FakeClient("a", ["ok"]), [])]).is_empty is False


# --- LLMPool capability-based selection -------------------------------------


@pytest.mark.asyncio
async def test_pool_prefers_matching_capability_over_higher_priority() -> None:
    """general_purpose is listed first (higher priority in a real config),
    but a code question should still prefer the code-tagged provider."""
    general_purpose = _FakeClient("general_purpose", ["general reply"])
    code_specialist = _FakeClient("code_specialist", ["code reply"])
    pool = LLMPool(
        [
            ("general_purpose", general_purpose, ["general", "complex_reasoning"]),
            ("code_specialist", code_specialist, ["code"]),
        ]
    )

    name, reply = await pool.chat([{"role": "user", "content": "hi"}], required_capability="code")

    assert (name, reply) == ("code_specialist", "code reply")
    assert code_specialist.call_count == 1
    assert general_purpose.call_count == 0  # never touched -- it matched first in the list, lost on capability


@pytest.mark.asyncio
async def test_pool_falls_back_to_full_priority_order_when_no_capability_matches() -> None:
    a = _FakeClient("a", ["reply-a"])
    b = _FakeClient("b", ["reply-b"])
    pool = LLMPool([("a", a, ["code"]), ("b", b, ["large_context"])])

    name, reply = await pool.chat([{"role": "user", "content": "hi"}], required_capability="complex_reasoning")

    assert (name, reply) == ("a", "reply-a")  # neither matches -- falls back to plain priority order


@pytest.mark.asyncio
async def test_pool_required_capability_none_behaves_exactly_as_before() -> None:
    a = _FakeClient("a", ["reply-a"])
    b = _FakeClient("b", ["reply-b"])
    pool = LLMPool([("a", a, ["code"]), ("b", b, ["large_context"])])

    name, reply = await pool.chat([{"role": "user", "content": "hi"}])  # no required_capability at all

    assert (name, reply) == ("a", "reply-a")


@pytest.mark.asyncio
async def test_pool_capability_filter_does_not_leak_into_client_chat_kwargs() -> None:
    """required_capability is consumed by LLMPool itself -- it must never end
    up forwarded into the underlying LLM API request payload."""
    seen_kwargs: dict = {}

    class _RecordingClient(LLMClient):
        async def chat(self, messages, *, max_tokens: int = 1024, **kwargs) -> str:
            seen_kwargs.update(kwargs)
            return "ok"

    pool = LLMPool([("a", _RecordingClient(), ["code"])])
    await pool.chat([{"role": "user", "content": "hi"}], required_capability="code", temperature=0.5)

    assert "required_capability" not in seen_kwargs
    assert seen_kwargs == {"temperature": 0.5}


@pytest.mark.asyncio
async def test_pool_capability_matching_providers_tried_in_priority_order_among_themselves() -> None:
    """Two providers both tagged "code" -- the earlier one (higher priority,
    by construction order from build_router's sort) should still win first."""
    higher = _FakeClient("higher", [RuntimeError("down")])
    lower = _FakeClient("lower", ["lower reply"])
    pool = LLMPool([("higher", higher, ["code"]), ("lower", lower, ["code"])])

    name, reply = await pool.chat([{"role": "user", "content": "hi"}], required_capability="code")

    assert (name, reply) == ("lower", "lower reply")
    assert higher.call_count == 1  # tried first (matching + higher priority), then failed over


@pytest.mark.asyncio
async def test_pool_first_provider_succeeds() -> None:
    a = _FakeClient("a", ["reply-a"])
    pool = LLMPool([("a", a, [])])
    name, reply = await pool.chat([{"role": "user", "content": "hi"}])
    assert (name, reply) == ("a", "reply-a")


@pytest.mark.asyncio
async def test_pool_generic_failure_fails_over_no_retry() -> None:
    a = _FakeClient("a", [RuntimeError("boom")])
    b = _FakeClient("b", ["reply-b"])
    pool = LLMPool([("a", a, []), ("b", b, [])])

    name, reply = await pool.chat([{"role": "user", "content": "hi"}])

    assert (name, reply) == ("b", "reply-b")
    assert a.call_count == 1  # no retry for a generic error


@pytest.mark.asyncio
async def test_pool_timeout_gets_one_retry_then_succeeds() -> None:
    a = _FakeClient("a", [httpx.TimeoutException("slow"), "reply-a-after-retry"])
    pool = LLMPool([("a", a, [])])

    name, reply = await pool.chat([{"role": "user", "content": "hi"}])

    assert (name, reply) == ("a", "reply-a-after-retry")
    assert a.call_count == 2


@pytest.mark.asyncio
async def test_pool_timeout_twice_fails_over_after_exactly_one_retry() -> None:
    a = _FakeClient("a", [httpx.TimeoutException("slow"), httpx.TimeoutException("still slow")])
    b = _FakeClient("b", ["reply-b"])
    pool = LLMPool([("a", a, []), ("b", b, [])])

    name, reply = await pool.chat([{"role": "user", "content": "hi"}])

    assert (name, reply) == ("b", "reply-b")
    assert a.call_count == 2  # original attempt + exactly one retry, no third try


@pytest.mark.asyncio
async def test_pool_http_429_no_retry_immediate_cooldown() -> None:
    error_429 = httpx.HTTPStatusError(
        "rate limited", request=httpx.Request("POST", "http://x"), response=httpx.Response(429)
    )
    a = _FakeClient("a", [error_429])
    b = _FakeClient("b", ["reply-b"])
    pool = LLMPool([("a", a, []), ("b", b, [])])

    name, reply = await pool.chat([{"role": "user", "content": "hi"}])

    assert (name, reply) == ("b", "reply-b")
    assert a.call_count == 1  # no retry for 429


@pytest.mark.asyncio
async def test_pool_cooldown_skips_recently_failed_provider_on_next_call() -> None:
    a = _FakeClient("a", [RuntimeError("boom"), "should not be reached"])
    b = _FakeClient("b", ["reply-b-1", "reply-b-2"])
    pool = LLMPool([("a", a, []), ("b", b, [])])

    await pool.chat([{"role": "user", "content": "first"}])
    name, reply = await pool.chat([{"role": "user", "content": "second"}])

    assert (name, reply) == ("b", "reply-b-2")
    assert a.call_count == 1  # still cooling down, never retried on the second call


@pytest.mark.asyncio
async def test_pool_all_candidates_cooling_down_raises_immediately_without_calling_anyone() -> None:
    a = _FakeClient("a", [RuntimeError("boom")])
    pool = LLMPool([("a", a, [])], cooldown_s=9999.0)

    # First call: nothing is in cooldown yet, so "a" is legitimately attempted and fails.
    with pytest.raises(RuntimeError, match="All LLM providers failed"):
        await pool.chat([{"role": "user", "content": "first"}])
    # Second call: "a" is now in its (effectively permanent, given the huge
    # cooldown_s) cooldown window -- must raise immediately without calling it again.
    with pytest.raises(RuntimeError, match="in cooldown"):
        await pool.chat([{"role": "user", "content": "second"}])

    assert a.call_count == 1  # second call never touched the client at all


@pytest.mark.asyncio
async def test_pool_all_providers_fail_raises_with_last_error() -> None:
    a = _FakeClient("a", [RuntimeError("fail-a")])
    b = _FakeClient("b", [RuntimeError("fail-b")])
    pool = LLMPool([("a", a, []), ("b", b, [])])

    with pytest.raises(RuntimeError, match="All LLM providers failed"):
        await pool.chat([{"role": "user", "content": "hi"}])


@pytest.mark.asyncio
async def test_pool_emits_exact_failover_log_line(caplog: pytest.LogCaptureFixture) -> None:
    a = _FakeClient("a", [RuntimeError("connection refused")])
    b = _FakeClient("b", ["reply-b"])
    pool = LLMPool([("a", a, []), ("b", b, [])])

    with caplog.at_level("WARNING"):
        await pool.chat([{"role": "user", "content": "hi"}])

    assert any(
        "LLM provider 'a' failed (connection refused); failing over to 'b'" in record.message
        for record in caplog.records
    )


@pytest.mark.asyncio
async def test_pool_aclose_closes_every_client() -> None:
    closed = []

    class _TrackingClient(_FakeClient):
        async def aclose(self) -> None:
            closed.append(self.name)

    pool = LLMPool([("a", _TrackingClient("a", ["x"]), []), ("b", _TrackingClient("b", ["y"]), [])])
    await pool.aclose()
    assert closed == ["a", "b"]
