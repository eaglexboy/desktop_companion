"""Tests for brain.router: keyword pre-filter dispatch, classify-then-route, tool registry."""

from __future__ import annotations

import pytest

from brain.config import Settings
from brain.llm_pool import LLMClient, LLMPool, Message
from brain.router import Router, build_router


class _FakeClient(LLMClient):
    def __init__(self, behavior) -> None:
        self._behavior = iter(behavior)
        self.calls: list[list[Message]] = []

    async def chat(self, messages: list[Message], *, max_tokens: int = 1024, **kwargs) -> str:
        self.calls.append(messages)
        outcome = next(self._behavior)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _pool(*replies) -> LLMPool:
    return LLMPool([("fake", _FakeClient(list(replies)), [])]) if replies else LLMPool([])


# --- Router construction -----------------------------------------------------


def test_router_raises_if_both_pools_empty() -> None:
    with pytest.raises(RuntimeError, match="at least one"):
        Router(LLMPool([]), LLMPool([]))


def test_router_allows_one_empty_pool() -> None:
    Router(_pool("local reply"), LLMPool([]))  # local only -- must not raise
    Router(LLMPool([]), _pool("cloud reply"))  # cloud only -- must not raise


# --- escalation_threshold short-circuits -------------------------------------


@pytest.mark.asyncio
async def test_always_local_never_touches_classifier_or_cloud() -> None:
    classifier = _FakeClient([RuntimeError("classifier should never be called")])
    router = Router(_pool("local reply"), _pool("should not be used"), classifier=classifier, escalation_threshold="always_local")

    result = await router.handle("anything at all", system_prompt="sys")

    assert result.text == "local reply"
    assert result.escalated is False


@pytest.mark.asyncio
async def test_always_cloud_never_touches_classifier() -> None:
    classifier = _FakeClient([RuntimeError("classifier should never be called")])
    router = Router(_pool("should not be used"), _pool("cloud reply"), classifier=classifier, escalation_threshold="always_cloud")

    result = await router.handle("anything at all", system_prompt="sys")

    assert result.text == "cloud reply"
    assert result.escalated is True


# --- Classifier Fallback Rule: default to LOCAL on error, not fail-safe
# toward the costlier cloud model. --------------------------------------


@pytest.mark.asyncio
async def test_classifier_error_defaults_to_local_not_cloud() -> None:
    classifier = _FakeClient([RuntimeError("classifier timed out")])
    router = Router(_pool("local reply"), _pool("should not be used"), classifier=classifier)

    result = await router.handle("hello", system_prompt="sys")

    assert result.text == "local reply"
    assert result.escalated is False


@pytest.mark.asyncio
async def test_no_classifier_configured_defaults_to_local() -> None:
    router = Router(_pool("local reply"), _pool("should not be used"), classifier=None)

    result = await router.handle("hello", system_prompt="sys")

    assert result.text == "local reply"
    assert result.escalated is False


@pytest.mark.asyncio
async def test_classifier_malformed_json_defaults_to_local() -> None:
    classifier = _FakeClient(["this is not json"])
    router = Router(_pool("local reply"), _pool("should not be used"), classifier=classifier)

    result = await router.handle("hello", system_prompt="sys")

    assert result.text == "local reply"
    assert result.escalated is False


@pytest.mark.asyncio
async def test_classifier_trailing_garbage_after_json_still_parses() -> None:
    """Observed live against a real small local model (phi3.5:3.8b) during
    router-model A/B testing: valid JSON followed by unrelated rambling
    text. A strict whole-string json.loads() would reject this outright."""
    classifier = _FakeClient(['{"escalate": true, "reason": "complex"}\n\nExplangy, as an AI...'])
    router = Router(_pool("should not be used"), _pool("cloud reply"), classifier=classifier)

    result = await router.handle("hello", system_prompt="sys")

    assert result.text == "cloud reply"
    assert result.escalated is True


@pytest.mark.asyncio
async def test_classifier_malformed_value_inside_json_still_defaults_to_local() -> None:
    """A different live-observed quirk (granite4): a stray character INSIDE
    the braces, which no amount of substring extraction can repair -- must
    still fall through to the ordinary Classifier Fallback Rule, not crash."""
    classifier = _FakeClient(['{"escalate": true, "reason": "stray period".}'])
    router = Router(_pool("local reply"), _pool("should not be used"), classifier=classifier)

    result = await router.handle("hello", system_prompt="sys")

    assert result.text == "local reply"
    assert result.escalated is False


@pytest.mark.asyncio
async def test_classifier_says_escalate_true_routes_to_cloud() -> None:
    classifier = _FakeClient(['{"escalate": true, "reason": "complex reasoning"}'])
    router = Router(_pool("should not be used"), _pool("cloud reply"), classifier=classifier)

    result = await router.handle("write me a red-black tree in rust", system_prompt="sys")

    assert result.text == "cloud reply"
    assert result.escalated is True
    assert result.reason == "complex reasoning"


@pytest.mark.asyncio
async def test_classifier_escalate_but_no_cloud_pool_falls_back_to_local_with_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    classifier = _FakeClient(['{"escalate": true, "reason": "complex"}'])
    router = Router(_pool("local reply"), LLMPool([]), classifier=classifier)

    with caplog.at_level("WARNING"):
        result = await router.handle("hello", system_prompt="sys")

    assert result.text == "local reply"
    assert result.escalated is False
    assert any("falling back to local" in r.message for r in caplog.records)


# --- Keyword pre-filter + tool registry --------------------------------------


@pytest.mark.asyncio
async def test_register_tool_and_keyword_dispatch_change_appearance() -> None:
    router = Router(_pool("should not be used"), LLMPool([]))
    seen_requests = []

    async def handler(request: str) -> str:
        seen_requests.append(request)
        return "done: wizard robe"

    router.register_tool("change_appearance", "desc", {"type": "object"}, handler)

    result = await router.handle("can you wear a wizard robe today?", system_prompt="sys")

    assert result.text == "done: wizard robe"
    assert result.source == "tool:change_appearance"
    assert result.escalated is False
    assert seen_requests == ["can you wear a wizard robe today?"]


@pytest.mark.asyncio
async def test_register_tool_and_keyword_dispatch_revert_appearance() -> None:
    router = Router(_pool("should not be used"), LLMPool([]))

    async def handler(request: str) -> str:
        return "reverted"

    router.register_tool("revert_appearance", "desc", {"type": "object"}, handler)

    result = await router.handle("please go back to your old look", system_prompt="sys")

    assert result.text == "reverted"
    assert result.source == "tool:revert_appearance"


@pytest.mark.asyncio
async def test_keyword_match_without_registered_tool_falls_through_to_normal_chat() -> None:
    router = Router(_pool("normal chat reply"), LLMPool([]))

    result = await router.handle("wear a hat", system_prompt="sys")

    assert result.text == "normal chat reply"
    assert result.source == "fake"


@pytest.mark.asyncio
async def test_unrelated_message_does_not_trigger_tool_dispatch() -> None:
    router = Router(_pool("normal chat reply"), LLMPool([]))
    called = False

    async def handler(request: str) -> str:
        nonlocal called
        called = True
        return "x"

    router.register_tool("change_appearance", "desc", {"type": "object"}, handler)

    result = await router.handle("what's the weather like today?", system_prompt="sys")

    assert called is False
    assert result.text == "normal chat reply"


# --- History passthrough -----------------------------------------------------


@pytest.mark.asyncio
async def test_history_included_in_message_list() -> None:
    client = _FakeClient(["reply"])
    router = Router(LLMPool([("fake", client, [])]), LLMPool([]))

    await router.handle(
        "follow-up question",
        system_prompt="sys",
        history=[{"role": "user", "content": "earlier"}, {"role": "assistant", "content": "earlier reply"}],
    )

    sent = client.calls[0]
    assert sent[0] == {"role": "system", "content": "sys"}
    assert sent[1] == {"role": "user", "content": "earlier"}
    assert sent[2] == {"role": "assistant", "content": "earlier reply"}
    assert sent[3] == {"role": "user", "content": "follow-up question"}


# --- build_router wiring ------------------------------------------------------


def _settings_with_cloud_providers() -> Settings:
    return Settings.model_validate(
        {
            "storage": {"workspace_dir": "/tmp/ws", "deploy_dir": "/tmp/dep"},
            "llm": {
                "local": {"base_url": "http://127.0.0.1:11434/v1", "model": "local-model"},
                "cloud": {
                    "low_priority": {
                        "provider_type": "generic_openai",
                        "base_url": "https://low.example.com/v1",
                        "model": "m",
                        "api_key": "sk-low",
                        "priority": 5,
                        "enabled": True,
                    },
                    "high_priority": {
                        "provider_type": "anthropic",
                        "base_url": "https://high.example.com",
                        "model": "m",
                        "api_key": "sk-high",
                        "priority": 50,
                        "enabled": True,
                    },
                    "disabled": {
                        "provider_type": "generic_openai",
                        "base_url": "https://disabled.example.com/v1",
                        "model": "m",
                        "api_key": "sk-disabled",
                        "priority": 999,
                        "enabled": False,
                    },
                },
            },
        }
    )


def test_build_router_wires_local_and_sorted_enabled_cloud_pool() -> None:
    settings = _settings_with_cloud_providers()
    router = build_router(settings)

    assert router._local_pool.is_empty is False
    assert router._cloud_pool.is_empty is False
    assert [e.name for e in router._cloud_pool._entries] == ["high_priority", "low_priority"]


def test_build_router_registers_builtin_appearance_tools() -> None:
    router = build_router(_settings_with_cloud_providers())
    assert set(router.tools) == {"change_appearance", "revert_appearance"}


@pytest.mark.asyncio
async def test_build_router_builtin_tools_raise_not_implemented_until_wired() -> None:
    router = build_router(_settings_with_cloud_providers())
    with pytest.raises(NotImplementedError, match="stub"):
        await router.tools["change_appearance"].handler("wear a hat")
    with pytest.raises(NotImplementedError, match="stub"):
        await router.tools["revert_appearance"].handler("")


def test_build_router_classifier_cascades_from_llm_local_by_default() -> None:
    settings = Settings.model_validate(
        {
            "storage": {"workspace_dir": "/tmp/ws", "deploy_dir": "/tmp/dep"},
            "llm": {"local": {"base_url": "http://127.0.0.1:11434/v1", "model": "local-model"}},
        }
    )
    router = build_router(settings)
    assert router._classifier is not None


def test_build_router_threads_capabilities_from_config_into_cloud_pool() -> None:
    settings = _settings_with_cloud_providers()
    router = build_router(settings)
    entries_by_name = {e.name: e for e in router._cloud_pool._entries}
    assert entries_by_name["low_priority"].capabilities == ()
    # "high_priority" doesn't set capabilities in _settings_with_cloud_providers either --
    # add a capability-tagged settings builder below for the real routing tests.


# --- Capability-based engine routing -- simulates several cloud engines
# with different declared strengths and confirms the classifier's chosen
# capability actually steers which one gets used, not just priority order. ---


def _settings_with_capability_tagged_providers() -> Settings:
    return Settings.model_validate(
        {
            "storage": {"workspace_dir": "/tmp/ws", "deploy_dir": "/tmp/dep"},
            "llm": {
                "local": {"base_url": "http://127.0.0.1:11434/v1", "model": "local-model"},
                "cloud": {
                    "general_purpose": {
                        "provider_type": "generic_openai",
                        "base_url": "https://general.example.com/v1",
                        "model": "m",
                        "api_key": "sk-general",
                        "priority": 100,  # highest priority -- would always win without capability matching
                        "capabilities": ["general", "complex_reasoning"],
                        "enabled": True,
                    },
                    "code_specialist": {
                        "provider_type": "generic_openai",
                        "base_url": "https://code.example.com/v1",
                        "model": "m",
                        "api_key": "sk-code",
                        "priority": 10,
                        "capabilities": ["code"],
                        "enabled": True,
                    },
                    "large_context_specialist": {
                        "provider_type": "generic_openai",
                        "base_url": "https://bigctx.example.com/v1",
                        "model": "m",
                        "api_key": "sk-bigctx",
                        "priority": 5,
                        "capabilities": ["large_context"],
                        "enabled": True,
                    },
                },
            },
        }
    )


class _FakeClassifier(LLMClient):
    """Returns a fixed classify verdict regardless of the actual prompt text --
    lets these tests drive the routing logic directly without depending on a
    real model's judgment."""

    def __init__(self, verdict_json: str) -> None:
        self._verdict_json = verdict_json

    async def chat(self, messages: list[Message], *, max_tokens: int = 1024, **kwargs) -> str:
        return self._verdict_json


def _router_with_capability_engines(verdict_json: str) -> Router:
    settings = _settings_with_capability_tagged_providers()
    router = build_router(settings)
    router._classifier = _FakeClassifier(verdict_json)
    # Swap in fake clients on every real pool entry (local included) so no
    # network call happens, regardless of which path a given test exercises.
    for entry in (*router._cloud_pool._entries, *router._local_pool._entries):
        entry.client = _FakeClient([f"{entry.name} reply"])
    return router


@pytest.mark.asyncio
async def test_routes_code_question_to_code_specialist_despite_lower_priority() -> None:
    router = _router_with_capability_engines('{"escalate": true, "capability": "code", "reason": "needs code"}')

    result = await router.handle("write me a quicksort in rust", system_prompt="sys")

    assert result.text == "code_specialist reply"
    assert result.capability == "code"
    assert result.escalated is True


@pytest.mark.asyncio
async def test_routes_large_context_question_to_large_context_specialist() -> None:
    router = _router_with_capability_engines(
        '{"escalate": true, "capability": "large_context", "reason": "needs big context"}'
    )

    result = await router.handle("summarize this 200 page document", system_prompt="sys")

    assert result.text == "large_context_specialist reply"
    assert result.capability == "large_context"


@pytest.mark.asyncio
async def test_routes_general_question_to_highest_priority_general_provider() -> None:
    router = _router_with_capability_engines(
        '{"escalate": true, "capability": "general", "reason": "general reasoning"}'
    )

    result = await router.handle("explain quantum entanglement simply", system_prompt="sys")

    assert result.text == "general_purpose reply"
    assert result.capability == "general"


@pytest.mark.asyncio
async def test_unrecognized_capability_from_classifier_falls_back_to_general() -> None:
    """A classifier might hallucinate a capability tag no provider declares --
    must not crash, must fall back to ordinary priority order."""
    router = _router_with_capability_engines(
        '{"escalate": true, "capability": "quantum_computing", "reason": "??"}'
    )

    result = await router.handle("hello", system_prompt="sys")

    assert result.text == "general_purpose reply"  # highest priority, since nothing is tagged "quantum_computing"
    assert result.capability == "general"  # normalized, not the hallucinated tag


@pytest.mark.asyncio
async def test_local_question_never_touches_capability_routing() -> None:
    router = _router_with_capability_engines('{"escalate": false, "capability": "general", "reason": "simple"}')

    result = await router.handle("hi there", system_prompt="sys")

    assert result.escalated is False
    assert result.capability is None
