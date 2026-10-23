"""Skills: the reply parser's forgiving-in-one-direction rule, the registry's validation, and the chat wiring."""

from __future__ import annotations

import pytest

from brain.skills.builtin import register_builtin
from brain.skills.registry import Param, Skill, SkillCall, SkillError, SkillRegistry
from brain.skills.reply import parse_reply


class FakeHub:
    def __init__(self):
        self.sent = []

    async def broadcast(self, kind, payload):
        self.sent.append((kind, payload))


class FakeStore:
    def __init__(self):
        self.notes = []

    def add_memory_note(self, note):
        self.notes.append(note)


class FakeState:
    def __init__(self):
        self.hub, self.store = FakeHub(), FakeStore()


# --- parse_reply -----------------------------------------------------------------


def test_structured_reply_splits_dialogue_from_actions():
    r = parse_reply('{"say": "Hi there!", "do": [{"skill": "gesture", "args": {"name": "greet"}}]}')
    assert r.structured and r.say == "Hi there!"
    assert [(a.skill, a.args) for a in r.actions] == [("gesture", {"name": "greet"})]


def test_code_fenced_json_and_surrounding_chatter_still_parse():
    r = parse_reply('Sure!\n```json\n{"say": "ok", "do": []}\n```')
    assert r.structured and r.say == "ok" and r.actions == []


def test_plain_text_is_all_dialogue_with_no_actions():
    r = parse_reply("Just chatting, no JSON here.")
    assert not r.structured and r.say == "Just chatting, no JSON here." and r.actions == []


def test_json_without_say_is_not_guessed_into_actions():
    r = parse_reply('{"do": [{"skill": "change_look", "args": {"request": "x"}}]}')
    assert not r.structured and r.actions == []


def test_a_code_sample_in_dialogue_does_not_become_actions():
    r = parse_reply('Here you go: {"name": "value"} is a dict.')
    assert not r.structured and r.actions == []


def test_malformed_do_entries_are_skipped():
    r = parse_reply('{"say": "x", "do": ["gesture", {"args": {}}, {"skill": "remember", "args": "nope"}]}')
    assert r.structured and [a.skill for a in r.actions] == ["remember"] and r.actions[0].args == {}


# --- registry --------------------------------------------------------------------


def _registry(**kw) -> SkillRegistry:
    reg = SkillRegistry(**kw)
    register_builtin(reg)
    return reg


def test_prompt_block_lists_enabled_skills_only():
    block = _registry(disabled={"change_look"}).prompt_block()
    assert "gesture(name: greet|agree|disagree|celebrate|shrug)" in block
    assert "change_look" not in block
    assert SkillRegistry().prompt_block() == ""


def test_validation_rejects_unknown_skills_and_bad_arguments():
    reg = _registry()
    with pytest.raises(SkillError):
        reg.validate(SkillCall("rm_rf", {}))
    with pytest.raises(SkillError):
        reg.validate(SkillCall("gesture", {"name": "backflip"}))
    with pytest.raises(SkillError):
        reg.validate(SkillCall("gesture", {"name": "greet", "extra": "x"}))
    with pytest.raises(SkillError):
        reg.validate(SkillCall("remember", {}))
    with pytest.raises(SkillError):
        reg.validate(SkillCall("remember", {"note": "x" * 1000}))
    assert reg.validate(SkillCall("gesture", {"name": " shrug "})) == {"name": "shrug"}


async def test_run_all_executes_valid_calls_and_refuses_the_rest():
    reg, state = _registry(max_actions=2), FakeState()
    calls = [
        SkillCall("gesture", {"name": "greet"}),
        SkillCall("nope", {}),
        SkillCall("remember", {"note": "likes tea"}),  # third: over the limit of 2
    ]
    results = await reg.run_all(calls, state)
    assert [r["ok"] for r in results] == [True, False]
    assert state.hub.sent == [("play_gesture", {"name": "greet", "clip": "greet", "loop": False})]
    assert state.store.notes == []


async def test_a_failing_skill_does_not_break_the_others():
    reg = SkillRegistry()

    async def boom(_a, _s):
        raise RuntimeError("bad")

    async def fine(_a, _s):
        return "done"

    reg.register(Skill("boom", "x", boom))
    reg.register(Skill("fine", "y", fine, (Param("q", "z", required=False),)))
    results = await reg.run_all([SkillCall("boom", {}), SkillCall("fine", {})], FakeState())
    assert [r["ok"] for r in results] == [False, True]
    assert "RuntimeError" in results[0]["error"]


def test_skills_config_parses_the_reserved_worker_key():
    from brain.config import Settings

    s = Settings.model_validate(
        {
            "storage": {"workspace_dir": "/tmp/a", "deploy_dir": "/tmp/b"},
            "llm": {"local": {"base_url": "http://x/v1", "model": "m"}},
            "skills": {"max_script_workers": 2, "disabled": ["change_look"]},
        }
    )
    assert s.skills.enabled and s.skills.disabled == ["change_look"] and s.skills.max_script_workers == 2


async def test_describe_reports_every_skill_with_usage_counters():
    reg, state = _registry(disabled={"change_look"}), FakeState()
    await reg.run_all([SkillCall("gesture", {"name": "greet"}), SkillCall("gesture", {"name": "nope"})], state)
    by_name = {s["name"]: s for s in reg.describe()}
    assert set(by_name) == {"gesture", "remember", "change_look", "revert_look"}
    assert by_name["change_look"]["enabled"] is False and by_name["gesture"]["enabled"] is True
    assert (by_name["gesture"]["calls"], by_name["gesture"]["refused"]) == (1, 1)
    assert by_name["gesture"]["params"][0]["enum"] == ["greet", "agree", "disagree", "celebrate", "shrug"]
    assert by_name["gesture"]["last_used"] is not None and by_name["remember"]["last_used"] is None
