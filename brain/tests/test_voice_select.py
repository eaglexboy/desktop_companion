import random
from pathlib import Path

import pytest
from pydantic import ValidationError

from brain.appearance.spec import example_look_spec
from brain.config import Settings, TTSRemoteEntry
from brain.personality.state import PersonalityStore
from brain.voice.tts import TTSBackend, TTSPool
from brain.voice.voice_select import (
    SeedVoiceSelector,
    VoiceSelection,
    parse_voice_id,
    voice_groups_for,
)

SEEDS = {
    "male": [7, 88],
    "female": [5, 33],
    "male_robot": [500],
}


def _settings(tmp_path: Path, *, seeds=SEEDS, active_backend="remote", provider_type="tts_serve") -> Settings:
    return Settings.model_validate(
        {
            "storage": {"workspace_dir": str(tmp_path / "ws"), "deploy_dir": str(tmp_path / "deploy")},
            "llm": {"local": {"base_url": "http://127.0.0.1:11434/v1", "model": "test-model"}},
            "voice": {
                "tts": {
                    "active_backend": active_backend,
                    "remote": {
                        "vox": {
                            "provider_type": provider_type,
                            "base_url": "http://127.0.0.1:7500",
                            "seed_voices": seeds,
                        }
                    },
                }
            },
        }
    )


def _look(base_template="primitive_blob", femininity=0.5):
    look = example_look_spec()
    return look.model_copy(
        update={
            "base_template": base_template,
            "proportions": look.proportions.model_copy(update={"femininity": femininity}),
        }
    )


def _selector(tmp_path, look, **settings_kwargs):
    store = PersonalityStore(tmp_path / "state.db", ("openness",), ("energy",))
    holder = {"look": look}
    selector = SeedVoiceSelector(
        _settings(tmp_path, **settings_kwargs), store, lambda: holder["look"], rng=random.Random(1)
    )
    return selector, store, holder


@pytest.mark.parametrize(
    ("template", "femininity", "expected"),
    [
        ("standard_humanoid_male", 0.9, ["male"]),  # explicit base wins over femininity
        ("standard_humanoid_female", 0.1, ["female"]),
        ("primitive_blob", 0.5, ["female"]),  # >= 0.5 reads female
        ("primitive_blob", 0.2, ["male"]),
        ("primitive_mechanical", 0.2, ["male_robot", "male"]),
        ("primitive_mechanical", 0.8, ["female_robot", "female"]),
    ],
)
def test_voice_groups_for(template, femininity, expected):
    assert voice_groups_for(_look(template, femininity)) == expected


def test_voice_id_round_trip_and_garbage():
    sel = VoiceSelection("vox", 42)
    assert sel.voice_id == "remote:vox:seed:42"
    assert parse_voice_id(sel.voice_id) == sel
    assert parse_voice_id(None) is None
    assert parse_voice_id("piper:default") is None
    assert parse_voice_id("remote:vox:seed:abc") is None


def test_picks_from_matching_group_and_persists(tmp_path):
    selector, store, _ = _selector(tmp_path, _look("standard_humanoid_male"))
    first = selector.select()
    assert first.seed in SEEDS["male"]
    assert store.get_agent_state("current_voice_id") == first.voice_id
    # stable across calls -- the voice must not change between sentences/utterances
    assert all(selector.select() == first for _ in range(20))


def test_reselects_when_redesign_moves_to_another_group(tmp_path):
    selector, store, holder = _selector(tmp_path, _look("standard_humanoid_male"))
    male = selector.select()
    holder["look"] = _look("standard_humanoid_female")
    female = selector.select()
    assert female.seed in SEEDS["female"]
    assert store.get_agent_state("previous_voice_id") == male.voice_id


def test_robot_uses_robot_pool_and_falls_back_to_gendered_pool(tmp_path):
    selector, _store, holder = _selector(tmp_path, _look("primitive_mechanical", 0.1))
    assert selector.select().seed == 500
    holder["look"] = _look("primitive_mechanical", 0.9)  # no female_robot pool -> female
    assert selector.select().seed in SEEDS["female"]


def test_no_selection_when_local_backend_or_no_pools_or_wrong_provider(tmp_path):
    for kwargs in (
        {"active_backend": "local"},
        {"seeds": {}},
        {"provider_type": "generic_openai"},
    ):
        selector, store, _ = _selector(tmp_path / str(len(kwargs)), _look(), **kwargs)
        assert selector.select() is None
        assert store.get_agent_state("current_voice_id") is None


class _Recorder(TTSBackend):
    def __init__(self, fail=False):
        self.calls: list[dict | None] = []
        self.fail = fail

    async def synthesize(self, text, out_wav_path, *, voice_id=None, params=None):
        self.calls.append(params)
        if self.fail:
            raise RuntimeError("down")


async def test_pool_sends_remote_params_only_to_their_instance(tmp_path):
    first, second, local = _Recorder(fail=True), _Recorder(), _Recorder()
    pool = TTSPool([("a", first), ("b", second)], local)
    await pool.synthesize("hi", tmp_path / "x.wav", remote_params={"a": {"seed": 7}})
    assert first.calls == [{"seed": 7}]
    assert second.calls == [None]  # a's seed did not leak to the failover instance


def test_seed_voices_defaults_empty_and_rejects_unknown_group():
    assert TTSRemoteEntry(base_url="http://x").seed_voices.is_empty()
    with pytest.raises(ValidationError):
        TTSRemoteEntry(base_url="http://x", seed_voices={"alien": [1]})


def test_default_seed_used_for_default_femininity_only(tmp_path):
    seeds = {**SEEDS, "default_seed": 33}
    # default femininity (0.5) -> female pool -> the configured default, not random
    selector, _store, _ = _selector(tmp_path / "a", _look("primitive_blob", 0.5), seeds=seeds)
    assert selector.select().seed == 33
    # customized femininity -> random pick from the pool (rng seeded, but any pool member is valid)
    selector, _store, _ = _selector(tmp_path / "b", _look("primitive_blob", 0.9), seeds=seeds)
    assert selector.select().seed in SEEDS["female"]
    # default_seed not in the matching pool -> ignored
    selector, _store, _ = _selector(tmp_path / "c", _look("standard_humanoid_male", 0.5), seeds=seeds)
    assert selector.select().seed in SEEDS["male"]


def test_default_seed_survives_as_stable_voice(tmp_path):
    selector, _store, _ = _selector(tmp_path, _look("primitive_blob", 0.5), seeds={**SEEDS, "default_seed": 5})
    assert [selector.select().seed for _ in range(5)] == [5] * 5
