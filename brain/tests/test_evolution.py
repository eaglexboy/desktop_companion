"""Synthetic end-to-end harness for brain.evolution.AvatarEvolution: a
fake clock, fake LLM delta responses, and a fake `compile_and_publish` (it
still exercises the REAL manifest.py/blender_gen.py staging+reveal
mechanics -- only the "run Blender" step itself is faked) drive one full
user-requested-change cycle and one full autonomous stage -> reveal cycle
in-process, asserting manifest/agent_state end states match the documented
lifecycle.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, cast
from pathlib import Path

import pytest

import brain.evolution as evolution_module
from brain.appearance.blender_gen import CompileResult
from brain.appearance.manifest import (
    AssetManifest,
    AssetVersionEntry,
    append_version,
    load_manifest,
    save_manifest,
)
from brain.appearance.spec import LookSpec, PaletteSpec, WardrobeSpec
from brain.config import MOOD_NAMES, TRAIT_NAMES, Settings
from brain.evolution import (
    AvatarEvolution,
    base_template_change_allowed,
    cap_delta_budget,
    trigger_b_bias_lines,
)
from brain.llm_pool import LLMClient, LLMPool, Message
from brain.personality.reflect import maybe_trigger_autonomous_redesign
from brain.personality.state import PersonalityStore
from brain.presence.desktop_bridge import PresenceState
from brain.router import build_router


class _ScriptedClient(LLMClient):
    """Returns each reply in `replies` in order, repeating the last one if
    called more times than there are scripted replies (so a retry-loop
    test only needs to script the two attempts it cares about)."""

    def __init__(self, *replies: str) -> None:
        self._replies = list(replies)
        self.calls: list[str] = []

    async def chat(
        self, messages: list[Message], *, max_tokens: int = 1024, **kwargs
    ) -> str:
        self.calls.append(messages[-1]["content"])
        if len(self._replies) > 1:
            return self._replies.pop(0)
        return self._replies[0]


def _pool(*replies: str) -> LLMPool:
    return LLMPool([("local-1", _ScriptedClient(*replies), [])])


def _settings(tmp_path: Path) -> Settings:
    return Settings.model_validate(
        {
            "storage": {
                "workspace_dir": str(tmp_path / "workspace"),
                "deploy_dir": str(tmp_path / "deploy"),
            },
            "llm": {
                "local": {
                    "base_url": "http://127.0.0.1:11434/v1",
                    "model": "test-model",
                }
            },
            "appearance": {"keep_recent_versions": 5},
        }
    )


def _store(tmp_path: Path) -> PersonalityStore:
    return PersonalityStore(tmp_path / "p.db", TRAIT_NAMES, MOOD_NAMES)


def _minimal_look(**overrides) -> LookSpec:
    fields: dict = {
        "base_template": "primitive_blob",
        "has_face": True,
        "palette": PaletteSpec(
            primary="#4A90D9",
            secondary="#FFFFFF",
            accent="#F5A623",
            emissive="#7FD1FF",
        ),
        "wardrobe": WardrobeSpec(),
    }
    fields.update(overrides)
    return LookSpec.model_validate(fields)


def _build_json(**overrides) -> dict:
    payload = {
        "glb_filename": "look.glb",
        "sha256": "a" * 64,
        "created_at": 1.0,
        "status": "ok",
        "seed": 42,
        "evaluated_triangles": 100,
        "size_bytes": 20,
        "has_face": True,
        "speech_indicator_mode": "viseme_shapekeys",
        "capabilities": {
            "has_head": True,
            "has_arms": False,
            "has_legs": False,
            "has_face": True,
        },
        "viseme_shape_keys": [f"viseme_{c}" for c in "ABCDEFGHX"],
        "animation_clips": [
            {"name": "idle_lookaround", "loop": True, "duration_s": 4.0}
        ],
        "bounds": {"height_m": 2.0, "width_m": 1.0, "depth_m": 1.0},
        "warnings": [],
    }
    payload.update(overrides)
    return payload


def _manifest(settings: Settings) -> AssetManifest:
    """`load_manifest` is Optional; tests that dereference it expect one to exist."""
    manifest = load_manifest(settings.deploy_dir)
    assert manifest is not None
    return manifest


def _current_entry(manifest: AssetManifest) -> AssetVersionEntry:
    entry = manifest.current()
    assert entry is not None
    return entry


def _version_entry(manifest: AssetManifest, version: int) -> AssetVersionEntry:
    entry = manifest.get_version(version)
    assert entry is not None
    return entry


def _install_fake_compile_and_publish(monkeypatch) -> None:
    """Fakes only the "run Blender" step; reuses the real manifest.py
    append_version/save_manifest and writes real files to staged_dir/
    current_dir so reveal_staged() (itself real, unfaked) and
    revert_appearance()'s file-exists check both work against genuine
    on-disk state."""

    async def fake_compile_and_publish(
        settings, look_spec, *, is_user_present, on_status=None
    ):
        settings.ensure_dirs()
        existing_manifest = load_manifest(settings.deploy_dir)
        next_version = (
            existing_manifest.latest_version_number()
            if existing_manifest
            else 0
        ) + 1
        filename = f"look-v{next_version}.glb"
        build_json = _build_json(glb_filename=filename)
        look_spec_dict = look_spec.model_dump(mode="json")

        if is_user_present():
            manifest = append_version(
                existing_manifest, build_json, look_spec_dict
            )
            assert manifest.current_version == next_version
            (settings.assets_dir / filename).write_bytes(b"glTF-fake")
            (settings.current_dir / filename).write_bytes(b"glTF-fake")
            save_manifest(settings.deploy_dir, manifest)
            return CompileResult(
                outcome="published",
                version=next_version,
                glb_path=settings.current_dir / filename,
                manifest=manifest,
            )

        settings.staged_dir.mkdir(parents=True, exist_ok=True)
        (settings.staged_dir / filename).write_bytes(b"glTF-fake")
        (settings.staged_dir / f"look-v{next_version}.meta.json").write_text(
            json.dumps({"build_json": build_json, "look_spec": look_spec_dict})
        )
        return CompileResult(
            outcome="staged",
            version=next_version,
            glb_path=settings.staged_dir / filename,
            manifest=None,
        )

    monkeypatch.setattr(
        evolution_module, "compile_and_publish", fake_compile_and_publish
    )


# --- pure helpers --------------------------------------------------------------


def test_cap_delta_budget_truncates_to_first_n_leaves():
    delta = {
        "wardrobe": {"outfit": "hoodie", "footwear": "boots"},
        "palette": {"primary": "#111111"},
    }
    capped = cap_delta_budget(delta, 2)
    leaves = []

    def walk(d, prefix=()):
        for k, v in d.items():
            if isinstance(v, dict):
                walk(v, (*prefix, k))
            else:
                leaves.append((*prefix, k))

    walk(capped)
    assert len(leaves) == 2


def test_cap_delta_budget_leaves_small_delta_untouched():
    delta = {"wardrobe": {"outfit": "hoodie"}}
    assert cap_delta_budget(delta, 3) == delta


def test_base_template_change_allowed_requires_high_openness_and_elapsed_days(
    tmp_path,
):
    import time

    store = _store(tmp_path)
    assert (
        base_template_change_allowed(store, openness=90) is False
    )  # base_template_since never set

    store.set_agent_state("base_template_since", str(time.time()))
    assert (
        base_template_change_allowed(store, openness=90) is False
    )  # not 7 days yet

    store.set_agent_state("base_template_since", str(time.time() - 8 * 86400))
    assert base_template_change_allowed(store, openness=90) is True
    assert (
        base_template_change_allowed(store, openness=50) is False
    )  # openness too low


def test_trigger_b_bias_lines_only_covers_documented_extremes():
    lines = trigger_b_bias_lines(
        {"openness": 50, "boldness": 50, "formality": 50},
        {"energy": 50, "stress": 50},
    )
    assert lines == []

    lines = trigger_b_bias_lines(
        {"openness": 80, "boldness": 80, "formality": 50},
        {"energy": 80, "stress": 50},
    )
    assert (
        len(lines) == 3
    )  # openness, boldness, energy -- not formality or stress


# --- Trigger A: synthetic end-to-end -------------------------------------------


@pytest.mark.asyncio
async def test_trigger_a_full_cycle_publishes_and_updates_agent_state(
    tmp_path, monkeypatch
):
    _install_fake_compile_and_publish(monkeypatch)
    settings = _settings(tmp_path)
    store = _store(tmp_path)
    delta_reply = json.dumps({"wardrobe": {"outfit": "wizard_robe"}})
    evolution = AvatarEvolution(settings, store, _pool(delta_reply))

    result = await evolution._run_trigger_a("put on a wizard robe")

    assert result is not None
    assert result.outcome == "published"
    manifest = _manifest(settings)
    assert manifest.current_version == 1
    assert (
        _current_entry(manifest).look_spec["wardrobe"]["outfit"]
        == "wizard_robe"
    )
    assert store.get_agent_state("last_redesign_timestamp") is not None
    assert store.get_agent_state("staged_version") is None


@pytest.mark.asyncio
async def test_trigger_a_reports_progress_stages_in_order(
    tmp_path, monkeypatch
):
    _install_fake_compile_and_publish(monkeypatch)
    stages: list[dict] = []

    async def hook(payload: dict) -> None:
        stages.append(payload)

    delta_reply = json.dumps({"wardrobe": {"outfit": "wizard_robe"}})
    evolution = AvatarEvolution(
        _settings(tmp_path),
        _store(tmp_path),
        _pool(delta_reply),
        on_progress=hook,
    )

    await evolution._run_trigger_a("put on a wizard robe")

    assert [p["stage"] for p in stages] == ["designing", "building", "done"]
    assert all(p["source"] == "user" for p in stages)
    assert stages[-1]["version"] == 1


@pytest.mark.asyncio
async def test_trigger_a_reports_failure_when_no_valid_delta(
    tmp_path, monkeypatch
):
    _install_fake_compile_and_publish(monkeypatch)
    stages: list[dict] = []

    async def hook(payload: dict) -> None:
        stages.append(payload)

    evolution = AvatarEvolution(
        _settings(tmp_path),
        _store(tmp_path),
        _pool("not json"),
        on_progress=hook,
    )

    assert await evolution._run_trigger_a("become a humanoid") is None
    assert [p["stage"] for p in stages] == ["designing", "failed"]


@pytest.mark.asyncio
async def test_trigger_a_does_not_rebuild_an_identical_look(
    tmp_path, monkeypatch
):
    _install_fake_compile_and_publish(monkeypatch)
    stages: list[dict] = []

    async def hook(payload: dict) -> None:
        stages.append(payload)

    settings = _settings(tmp_path)
    evolution = AvatarEvolution(
        settings,
        _store(tmp_path),
        cast(Any, None),
        on_progress=hook,
    )
    same = json.dumps({"seed": evolution.current_look_spec().seed})
    evolution = AvatarEvolution(
        settings, _store(tmp_path), _pool(same), on_progress=hook
    )

    assert await evolution._run_trigger_a("be what you already are") is None
    assert [p["stage"] for p in stages] == ["designing", "unchanged"]
    assert load_manifest(settings.deploy_dir) is None  # nothing was published


@pytest.mark.asyncio
async def test_a_raising_progress_hook_never_breaks_the_redesign(
    tmp_path, monkeypatch
):
    _install_fake_compile_and_publish(monkeypatch)

    async def hook(_payload: dict) -> None:
        raise RuntimeError("overlay gone")

    delta_reply = json.dumps({"wardrobe": {"outfit": "hoodie"}})
    evolution = AvatarEvolution(
        _settings(tmp_path),
        _store(tmp_path),
        _pool(delta_reply),
        on_progress=hook,
    )

    result = await evolution._run_trigger_a("put on a hoodie")
    assert result is not None and result.outcome == "published"


@pytest.mark.asyncio
async def test_trigger_a_tool_handler_replies_immediately_and_schedules_background_work(
    tmp_path, monkeypatch
):
    _install_fake_compile_and_publish(monkeypatch)
    settings = _settings(tmp_path)
    store = _store(tmp_path)
    delta_reply = json.dumps({"wardrobe": {"outfit": "hoodie"}})
    evolution = AvatarEvolution(settings, store, _pool(delta_reply))

    reply = await evolution.change_appearance("put on a hoodie")
    assert "let me put something together" in reply.lower()
    assert (
        load_manifest(settings.deploy_dir) is None
    )  # background work hasn't run yet

    assert len(evolution._background_tasks) == 1
    await next(iter(evolution._background_tasks))
    assert _manifest(settings).current_version == 1


@pytest.mark.asyncio
async def test_trigger_a_retries_once_on_invalid_delta_then_succeeds(
    tmp_path, monkeypatch
):
    _install_fake_compile_and_publish(monkeypatch)
    settings = _settings(tmp_path)
    store = _store(tmp_path)
    bad_reply = json.dumps({"wardrobe": {"outfit": "not_a_real_outfit"}})
    good_reply = json.dumps({"wardrobe": {"outfit": "hoodie"}})
    client = _ScriptedClient(bad_reply, good_reply)
    evolution = AvatarEvolution(
        settings, store, LLMPool([("local-1", client, [])])
    )

    result = await evolution._run_trigger_a("put on a hoodie")
    assert result is not None and result.outcome == "published"
    assert len(client.calls) == 2  # first attempt + one retry


@pytest.mark.asyncio
async def test_trigger_a_gives_up_silently_after_two_bad_deltas(
    tmp_path, monkeypatch
):
    _install_fake_compile_and_publish(monkeypatch)
    settings = _settings(tmp_path)
    store = _store(tmp_path)
    bad_reply = json.dumps({"wardrobe": {"outfit": "not_a_real_outfit"}})
    evolution = AvatarEvolution(settings, store, _pool(bad_reply))

    result = await evolution._run_trigger_a("put on something weird")
    assert result is None
    assert load_manifest(settings.deploy_dir) is None


# --- Trigger A: revert ----------------------------------------------------------


@pytest.mark.asyncio
async def test_revert_appearance_with_no_history_is_friendly(tmp_path):
    settings = _settings(tmp_path)
    store = _store(tmp_path)
    evolution = AvatarEvolution(settings, store, _pool("{}"))

    reply = await evolution.revert_appearance()
    assert "no previous look" in reply.lower()


@pytest.mark.asyncio
async def test_revert_appearance_rewrites_manifest_pointer_only(
    tmp_path, monkeypatch
):
    _install_fake_compile_and_publish(monkeypatch)
    settings = _settings(tmp_path)
    store = _store(tmp_path)
    evolution = AvatarEvolution(
        settings,
        store,
        _pool(
            json.dumps({"wardrobe": {"outfit": "hoodie"}}),
            json.dumps({"wardrobe": {"outfit": "vest"}}),
        ),
    )

    await evolution._run_trigger_a("put on a hoodie")
    await evolution._run_trigger_a("put on a vest")
    assert _manifest(settings).current_version == 2

    reply = await evolution.revert_appearance()
    assert "hoodie" in reply.lower() or "back to how i looked" in reply.lower()
    manifest = _manifest(settings)
    assert manifest.current_version == 1
    assert len(manifest.versions) == 2  # revert doesn't delete history


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("go back to version 12", 12),
        ("restore look #7 please", 7),
        ("revert to v3", 3),
        ("Version number 5", 5),
        ("change back", None),
        ("go back 5 minutes", None),
    ],
)
def test_requested_version_parsing(text, expected):
    assert evolution_module._requested_version(text) == expected


@pytest.mark.asyncio
async def test_revert_to_named_version_rewrites_pointer_when_file_kept(
    tmp_path, monkeypatch
):
    _install_fake_compile_and_publish(monkeypatch)
    settings = _settings(tmp_path)
    evolution = AvatarEvolution(
        settings,
        _store(tmp_path),
        _pool(
            *(
                json.dumps({"wardrobe": {"outfit": o}})
                for o in ("hoodie", "vest", "tshirt_jeans")
            )
        ),
    )
    for request in ("a", "b", "c"):
        await evolution._run_trigger_a(request)
    assert _manifest(settings).current_version == 3

    reply = await evolution.revert_appearance("go back to version 1")
    assert "version 1" in reply
    assert _manifest(settings).current_version == 1
    assert "already" in (await evolution.revert_appearance("version 1")).lower()
    assert "numbered 99" in await evolution.revert_appearance("version 99")


@pytest.mark.asyncio
async def test_revert_to_pruned_version_rebuilds_it_as_a_new_version(
    tmp_path, monkeypatch
):
    _install_fake_compile_and_publish(monkeypatch)
    settings = _settings(tmp_path)
    evolution = AvatarEvolution(
        settings,
        _store(tmp_path),
        _pool(
            json.dumps({"wardrobe": {"outfit": "hoodie"}}),
            json.dumps({"wardrobe": {"outfit": "vest"}}),
        ),
    )
    await evolution._run_trigger_a("a")
    await evolution._run_trigger_a("b")
    (settings.current_dir / "look-v1.glb").unlink()

    reply = await evolution.revert_appearance("restore version 1")
    assert "put it back together" in reply
    await asyncio.gather(*evolution._background_tasks)
    manifest = _manifest(settings)
    assert manifest.current_version == 3  # numbers never go backwards
    assert (
        _version_entry(manifest, 3).look_spec
        == _version_entry(manifest, 1).look_spec
    )


# --- the look lock (an imported model is kept until the user frees it) ----------


def _evolution_on_custom_look(tmp_path, monkeypatch, *replies: str):
    """An AvatarEvolution whose current look is a custom (imported) model, built
    through the real manifest mechanics after one generated look."""
    _install_fake_compile_and_publish(monkeypatch)
    settings = _settings(tmp_path)
    custom_dir = settings.deploy_dir / "assets" / "custom_templates"
    custom_dir.mkdir(parents=True)
    for name in (
        "miss",
        "other",
    ):  # the catalog check wants the model files to exist
        (custom_dir / f"{name}.glb").write_bytes(b"glTF")
    evolution = AvatarEvolution(settings, _store(tmp_path), _pool(*replies))
    return settings, evolution


async def _publish(evolution, **look):
    result = await evolution.generate_from_full_spec(_minimal_look(**look))
    assert result is not None and result.outcome == "published"
    return result


@pytest.mark.asyncio
async def test_the_companion_is_told_which_models_it_has_and_that_a_worn_one_keeps_its_colours(
    tmp_path, monkeypatch
):
    _settings_, evolution = _evolution_on_custom_look(tmp_path, monkeypatch)
    await _publish(evolution, base_template="custom:miss")
    told = evolution.look_context()
    assert "You are wearing the installed model miss" in told
    assert "keeps its own colours" in told
    assert "miss, other" in told  # the models it can become, by name
    assert "custom:" not in told.split("Installed models")[1]


@pytest.mark.asyncio
async def test_a_custom_model_locks_the_look_and_a_request_to_change_it_is_declined(
    tmp_path, monkeypatch
):
    settings, evolution = _evolution_on_custom_look(
        tmp_path, monkeypatch, json.dumps({"wardrobe": {"outfit": "vest"}})
    )
    await _publish(evolution)  # v1 generated
    assert evolution.look_locked() is False
    await _publish(evolution, base_template="custom:miss")  # v2 imported
    assert evolution.look_locked() is True

    reply = await evolution.change_appearance("put on a vest")
    assert "keeping this look" in reply
    await asyncio.gather(*evolution._background_tasks)
    assert _manifest(settings).current_version == 2  # nothing was built


@pytest.mark.asyncio
async def test_saying_it_may_change_its_look_unlocks_it_and_the_next_request_goes_through(
    tmp_path, monkeypatch
):
    settings, evolution = _evolution_on_custom_look(
        tmp_path, monkeypatch, json.dumps({"wardrobe": {"outfit": "vest"}})
    )
    await _publish(evolution, base_template="custom:miss")
    assert evolution.look_locked() is True

    reply = await evolution.change_appearance(
        "You can change your look if you want"
    )
    assert "free to change" in reply
    assert evolution.look_locked() is False

    await evolution.change_appearance("put on a vest")
    await asyncio.gather(*evolution._background_tasks)
    assert _manifest(settings).current_version == 2
    assert (
        evolution.look_locked() is False
    )  # still the same model: the unlock covers it


@pytest.mark.asyncio
async def test_an_unlock_covers_only_the_model_it_was_given_for(
    tmp_path, monkeypatch
):
    _, evolution = _evolution_on_custom_look(tmp_path, monkeypatch, "{}")
    await _publish(evolution, base_template="custom:miss")
    await evolution.unlock_appearance()
    await _publish(evolution, base_template="custom:other")
    assert (
        evolution.look_locked() is True
    )  # a different imported model is locked again
    await _publish(evolution)  # a generated look
    await _publish(evolution, base_template="custom:miss")
    assert (
        evolution.look_locked() is True
    )  # and so is the first one, once it has been left


@pytest.mark.asyncio
async def test_asking_for_a_generated_look_goes_back_to_the_last_one(
    tmp_path, monkeypatch
):
    settings, evolution = _evolution_on_custom_look(tmp_path, monkeypatch, "{}")
    await _publish(
        evolution,
        palette=PaletteSpec(
            primary="#111111",
            secondary="#FFFFFF",
            accent="#F5A623",
            emissive="#7FD1FF",
        ),
    )
    await _publish(evolution, base_template="custom:miss")

    reply = await evolution.change_appearance("go back to a generated look")
    await asyncio.gather(*evolution._background_tasks)
    assert "version 1" in reply
    manifest = _manifest(settings)
    assert (
        _current_entry(manifest).look_spec["base_template"] == "primitive_blob"
    )
    assert evolution.look_locked() is False


@pytest.mark.asyncio
async def test_no_generated_look_to_go_back_to_is_said_plainly(
    tmp_path, monkeypatch
):
    _, evolution = _evolution_on_custom_look(tmp_path, monkeypatch, "{}")
    await _publish(evolution, base_template="custom:miss")
    reply = await evolution.change_appearance("switch to a procedural body")
    assert "don't have a generated look" in reply
    assert evolution.look_locked() is True


@pytest.mark.asyncio
async def test_the_companion_does_not_redesign_itself_while_the_look_is_locked(
    tmp_path, monkeypatch
):
    settings, evolution = _evolution_on_custom_look(
        tmp_path, monkeypatch, json.dumps({"wardrobe": {"outfit": "vest"}})
    )
    await _publish(evolution, base_template="custom:miss")
    await evolution.trigger_b_on_eligible()
    assert _manifest(settings).current_version == 1  # no autonomous redesign

    await evolution.unlock_appearance()
    await evolution.trigger_b_on_eligible()
    assert _manifest(settings).current_version == 2


@pytest.mark.asyncio
async def test_restoring_a_numbered_look_is_still_allowed_while_locked(
    tmp_path, monkeypatch
):
    settings, evolution = _evolution_on_custom_look(tmp_path, monkeypatch, "{}")
    await _publish(evolution)
    await _publish(evolution, base_template="custom:miss")
    await evolution.revert_appearance("go back to version 1")
    assert _manifest(settings).current_version == 1
    assert evolution.look_locked() is False


@pytest.mark.parametrize(
    ("text", "unlock", "generated"),
    [
        ("You can change your look if you want", True, False),
        ("you're free to change your look", True, False),
        ("feel free to change your appearance", True, False),
        ("it's okay to change your look", True, False),
        ("change to a generated look", False, True),
        ("go back to your procedural body", False, True),
        ("generate a pirate look", False, False),
        ("put on a vest", False, False),
    ],
)
def test_lock_phrases(text, unlock, generated):
    assert evolution_module._wants_unlock(text) is unlock
    assert evolution_module._wants_generated(text) is generated


# --- Trigger B: synthetic stage -> reveal cycle --------------------------------


@pytest.mark.asyncio
async def test_trigger_b_stages_when_user_away_then_reveals_on_return(
    tmp_path, monkeypatch
):
    _install_fake_compile_and_publish(monkeypatch)
    settings = _settings(tmp_path)
    store = _store(tmp_path)
    delta_reply = json.dumps({"wardrobe": {"outfit": "lab_coat"}})

    presence_state = PresenceState.AWAY
    evolution = AvatarEvolution(
        settings,
        store,
        _pool(delta_reply),
        get_presence_state=lambda: presence_state,
    )

    await evolution.trigger_b_on_eligible()

    # staged, not published: live manifest untouched, staged_version recorded
    assert load_manifest(settings.deploy_dir) is None
    assert store.get_agent_state("staged_version") == "1"
    assert (
        store.get_agent_state("last_redesign_timestamp") is None
    )  # not stamped yet -- only on reveal

    # user returns -> Away/Idle -> Active transition
    await evolution.on_presence_transition(
        PresenceState.AWAY, PresenceState.ACTIVE
    )

    manifest = _manifest(settings)
    assert manifest is not None
    assert manifest.current_version == 1
    assert (
        _current_entry(manifest).look_spec["wardrobe"]["outfit"] == "lab_coat"
    )
    assert store.get_agent_state("staged_version") is None
    assert store.get_agent_state("last_redesign_timestamp") is not None


@pytest.mark.asyncio
async def test_trigger_b_publishes_immediately_when_user_already_active(
    tmp_path, monkeypatch
):
    _install_fake_compile_and_publish(monkeypatch)
    settings = _settings(tmp_path)
    store = _store(tmp_path)
    delta_reply = json.dumps({"wardrobe": {"outfit": "vest"}})
    evolution = AvatarEvolution(
        settings,
        store,
        _pool(delta_reply),
        get_presence_state=lambda: PresenceState.ACTIVE,
    )

    await evolution.trigger_b_on_eligible()

    manifest = _manifest(settings)
    assert manifest.current_version == 1
    assert store.get_agent_state("staged_version") is None
    assert store.get_agent_state("last_redesign_timestamp") is not None


@pytest.mark.asyncio
async def test_on_presence_transition_ignores_non_reveal_edges(
    tmp_path, monkeypatch
):
    _install_fake_compile_and_publish(monkeypatch)
    settings = _settings(tmp_path)
    store = _store(tmp_path)
    evolution = AvatarEvolution(
        settings,
        store,
        _pool("{}"),
        get_presence_state=lambda: PresenceState.AWAY,
    )
    store.set_agent_state("staged_version", "1")  # pretend something is staged

    await evolution.on_presence_transition(
        PresenceState.ACTIVE, PresenceState.AWAY
    )
    await evolution.on_presence_transition(
        PresenceState.ACTIVE, PresenceState.IDLE
    )
    await evolution.on_presence_transition(
        PresenceState.ACTIVE, PresenceState.ACTIVE
    )

    # none of these are an Away|Idle -> Active edge; staged_version must survive untouched
    assert store.get_agent_state("staged_version") == "1"


@pytest.mark.asyncio
async def test_boot_reconciliation_clears_staged_version_when_file_missing(
    tmp_path,
):
    settings = _settings(tmp_path)
    settings.ensure_dirs()
    store = _store(tmp_path)
    store.set_agent_state("staged_version", "7")  # no look-v7.glb on disk
    evolution = AvatarEvolution(settings, store, _pool("{}"))

    evolution.reconcile_staged_on_boot()
    assert store.get_agent_state("staged_version") is None


@pytest.mark.asyncio
async def test_boot_reconciliation_leaves_staged_version_when_file_present(
    tmp_path,
):
    settings = _settings(tmp_path)
    settings.ensure_dirs()
    (settings.staged_dir / "look-v3.glb").write_bytes(b"glTF-fake")
    store = _store(tmp_path)
    store.set_agent_state("staged_version", "3")
    evolution = AvatarEvolution(settings, store, _pool("{}"))

    evolution.reconcile_staged_on_boot()
    assert store.get_agent_state("staged_version") == "3"


# --- asset-swap-failure rollback ------------------------------------------------


@pytest.mark.asyncio
async def test_handle_asset_swap_failure_rolls_back_and_marks_corrupted(
    tmp_path, monkeypatch
):
    _install_fake_compile_and_publish(monkeypatch)
    settings = _settings(tmp_path)
    store = _store(tmp_path)
    evolution = AvatarEvolution(
        settings,
        store,
        _pool(
            json.dumps({"wardrobe": {"outfit": "hoodie"}}),
            json.dumps({"wardrobe": {"outfit": "vest"}}),
        ),
    )
    await evolution._run_trigger_a("put on a hoodie")
    await evolution._run_trigger_a("put on a vest")
    assert _manifest(settings).current_version == 2

    evolution.handle_asset_swap_failure(2)

    manifest = _manifest(settings)
    assert manifest.current_version == 1
    assert _version_entry(manifest, 2).status == "corrupted"


def test_handle_asset_swap_failure_on_fresh_install_does_not_crash(
    tmp_path, monkeypatch, caplog
):
    _install_fake_compile_and_publish(monkeypatch)
    settings = _settings(tmp_path)
    settings.ensure_dirs()
    store = _store(tmp_path)
    manifest = append_version(
        None, _build_json(), {"base_template": "primitive_blob"}
    )
    save_manifest(settings.deploy_dir, manifest)
    evolution = AvatarEvolution(settings, store, _pool("{}"))

    evolution.handle_asset_swap_failure(1)  # no N-1 to fall back to

    result_manifest = _manifest(settings)
    assert result_manifest.current_version == 1  # unchanged, just marked
    assert _version_entry(result_manifest, 1).status == "corrupted"


# --- full integration with reflect.py's real eligibility gate -----------------


@pytest.mark.asyncio
async def test_full_trigger_b_wired_through_real_reflect_eligibility_gate(
    tmp_path, monkeypatch
):
    """Confirms AvatarEvolution.trigger_b_on_eligible plugs directly into
    reflect.maybe_trigger_autonomous_redesign's on_eligible parameter."""
    _install_fake_compile_and_publish(monkeypatch)
    settings = _settings(tmp_path)
    store = _store(tmp_path)
    delta_reply = json.dumps({"wardrobe": {"outfit": "armor_light"}})
    evolution = AvatarEvolution(
        settings,
        store,
        _pool(delta_reply),
        get_presence_state=lambda: PresenceState.ACTIVE,
    )

    fired = await maybe_trigger_autonomous_redesign(
        store,
        is_build_running=lambda: False,
        is_user_interacting=lambda: False,
        redesign_cooldown_hours_min=0.0,
        redesign_cooldown_hours_max=0.0,
        autonomous_redesign_probability=1.0,
        on_eligible=evolution.trigger_b_on_eligible,
    )

    assert fired is True
    manifest = _manifest(settings)
    assert (
        manifest is not None
        and _current_entry(manifest).look_spec["wardrobe"]["outfit"]
        == "armor_light"
    )


# --- wiring into the real Router (replacing router.py's stubs) ----------------


@pytest.mark.asyncio
async def test_wire_tools_replaces_router_stubs_end_to_end(
    tmp_path, monkeypatch
):
    """Confirms AvatarEvolution.wire_tools() actually overwrites
    router.py's NotImplementedError stubs, and that a real chat message
    matching the keyword pre-filter reaches the real implementation via
    Router.handle() -- not just a direct method call."""
    _install_fake_compile_and_publish(monkeypatch)
    settings = _settings(tmp_path)
    store = _store(tmp_path)
    delta_reply = json.dumps({"wardrobe": {"outfit": "hoodie"}})
    evolution = AvatarEvolution(settings, store, _pool(delta_reply))

    settings.skills.enabled = False  # the keyword pre-filter only runs when the model can't call tools itself
    router = build_router(settings)
    evolution.wire_tools(router)

    result = await router.handle(
        "please change your look for me", system_prompt="irrelevant"
    )
    assert result.source == "tool:change_appearance"
    assert "let me put something together" in result.text.lower()

    # the background compile scheduled by the real handler should still complete
    assert len(evolution._background_tasks) == 1
    await next(iter(evolution._background_tasks))
    assert _manifest(settings).current_version == 1


def test_keyword_prefilter_is_off_when_skills_are_enabled(tmp_path):
    settings = _settings(tmp_path)
    settings.skills.enabled = True
    assert (
        build_router(settings)._match_tool_keyword(
            "please change your look for me"
        )
        is None
    )


@pytest.mark.asyncio
async def test_unmet_note_is_recorded_and_the_closest_look_still_builds(
    tmp_path, monkeypatch
):
    _install_fake_compile_and_publish(monkeypatch)
    store = _store(tmp_path)
    delta_reply = json.dumps(
        {"wardrobe": {"outfit": "hoodie"}, "unmet": "no jellyfish body"}
    )
    evolution = AvatarEvolution(_settings(tmp_path), store, _pool(delta_reply))

    result = await evolution._run_trigger_a("become a jellyfish")

    assert result is not None and result.outcome == "published"
    assert any(
        "become a jellyfish" in n.note and "no jellyfish body" in n.note
        for n in store.recent_memory_notes(limit=10)
    )


@pytest.mark.asyncio
async def test_invented_option_fails_clearly_and_is_recorded_as_unmet(
    tmp_path, monkeypatch
):
    _install_fake_compile_and_publish(monkeypatch)
    store = _store(tmp_path)
    stages: list[dict] = []

    async def hook(payload: dict) -> None:
        stages.append(payload)

    bad_reply = json.dumps({"base_template": "jellyfish"})
    evolution = AvatarEvolution(
        _settings(tmp_path), store, _pool(bad_reply), on_progress=hook
    )

    assert await evolution._run_trigger_a("become a jellyfish") is None

    assert stages[-1]["stage"] == "failed"
    assert "don't have the parts" in stages[-1]["error"]
    assert any(
        "jellyfish" in n.note for n in store.recent_memory_notes(limit=10)
    )


@pytest.mark.asyncio
async def test_a_revert_tells_the_overlay_to_reload_the_look(
    tmp_path, monkeypatch
):
    """Reverting only rewrites the manifest pointer; the overlay learns of a new look from the
    `done` progress event (main.py hot-reloads on it). Without one it kept showing the look
    that had been reverted away from, until it was restarted."""
    _install_fake_compile_and_publish(monkeypatch)
    seen: list[dict] = []

    async def hook(payload: dict) -> None:
        seen.append(payload)

    settings = _settings(tmp_path)
    evolution = AvatarEvolution(
        settings,
        _store(tmp_path),
        _pool(
            *(
                json.dumps({"wardrobe": {"outfit": o}})
                for o in ("hoodie", "vest", "tshirt_jeans")
            )
        ),
        on_progress=hook,
    )
    for request in ("a", "b", "c"):
        await evolution._run_trigger_a(request)
    seen.clear()

    await evolution.revert_appearance()  # one step back, to version 2
    assert [(p["stage"], p["version"]) for p in seen] == [("done", 2)]
    seen.clear()
    await evolution.revert_appearance(
        "go back to version 1"
    )  # a named, kept version
    assert [(p["stage"], p["version"]) for p in seen] == [("done", 1)]


@pytest.mark.asyncio
async def test_asking_for_an_installed_model_by_name_switches_to_it_even_while_the_look_is_kept(
    tmp_path, monkeypatch
):
    settings, evolution = _evolution_on_custom_look(tmp_path, monkeypatch)
    await _publish(evolution, base_template="custom:miss")
    assert evolution.look_locked() is True

    reply = await evolution.change_appearance(
        "Change your look to the Other model"
    )
    assert "Switching to other" in reply
    await asyncio.gather(*evolution._background_tasks)
    assert _manifest(settings).current_version == 2
    assert evolution.current_look_spec().base_template == "custom:other"

    again = await evolution.change_appearance("change to the other model")
    assert (
        "already wearing" in again and "building it again" in again
    )  # the same model: built again
    await asyncio.gather(*evolution._background_tasks)
    assert _manifest(settings).current_version == 3


@pytest.mark.asyncio
async def test_a_request_that_only_mentions_a_model_name_is_not_a_switch(
    tmp_path, monkeypatch
):
    settings, evolution = _evolution_on_custom_look(tmp_path, monkeypatch)
    await _publish(evolution, base_template="custom:miss")

    reply = await evolution.change_appearance("give me other ears and a hat")
    assert "keeping this look" in reply
    await asyncio.gather(*evolution._background_tasks)
    assert _manifest(settings).current_version == 1


def test_a_request_to_become_an_installed_model_is_read_through_typos_and_needs_a_switching_word():
    from brain.evolution import _named_model

    templates = ["custom:avocado", "custom:bmo", "custom:titan_tvman"]
    assert (
        _named_model("Change to the Avacado model", templates)
        == "custom:avocado"
    )
    assert (
        _named_model("turn into titan tvman", templates) == "custom:titan_tvman"
    )
    assert _named_model("avocado?", templates) is None  # a mention
    assert _named_model("do you like avocado", templates) is None
    assert _named_model("give me bmo ears", templates) is None


def test_part_of_a_models_name_is_enough_when_only_one_model_has_it():
    from brain.evolution import _named_model

    templates = [
        "custom:milo_robot_rigged",
        "custom:mech_mike_animated",
        "custom:mech_stan_animated",
        "custom:robot_polygonal_rigged",
    ]
    assert (
        _named_model("Change to Milo model", templates)
        == "custom:milo_robot_rigged"
    )
    assert (
        _named_model("change to stan", templates) == "custom:mech_stan_animated"
    )
    # a misspelling two names are close to ("mile": mike, milo) is settled by the other word
    assert (
        _named_model("Change to Mile robot model", templates)
        == "custom:milo_robot_rigged"
    )
    assert _named_model("change to mile", templates) is None  # mike or milo
    assert (
        _named_model("change to the robot model", templates) is None
    )  # two have it
    assert _named_model("change to the mech model", templates) is None


@pytest.mark.asyncio
async def test_asking_it_to_rebuild_builds_the_current_imported_look_again(
    tmp_path, monkeypatch
):
    settings, evolution = _evolution_on_custom_look(tmp_path, monkeypatch)
    await _publish(evolution, base_template="custom:miss")
    assert evolution.is_direct_look_request("rebuild your look")
    assert not evolution.is_direct_look_request("tell me about your look")

    reply = await evolution.change_appearance("Please rebuild it")
    assert "Rebuilding" in reply
    await asyncio.gather(*evolution._background_tasks)
    assert _manifest(settings).current_version == 2
    assert evolution.current_look_spec().base_template == "custom:miss"
