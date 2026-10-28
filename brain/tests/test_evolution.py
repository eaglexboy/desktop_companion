"""Synthetic end-to-end harness for brain.evolution.AvatarEvolution: a
fake clock, fake LLM delta responses, and a fake `compile_and_publish` (it
still exercises the REAL manifest.py/blender_gen.py staging+reveal
mechanics -- only the "run Blender" step itself is faked) drive one full
user-requested-change cycle and one full autonomous stage -> reveal cycle
in-process, asserting manifest/agent_state end states match the documented
lifecycle.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import brain.evolution as evolution_module
from brain.appearance.blender_gen import CompileResult
from brain.appearance.manifest import AssetManifest, AssetVersionEntry, append_version, load_manifest, save_manifest
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

    async def chat(self, messages: list[Message], *, max_tokens: int = 1024, **kwargs) -> str:
        self.calls.append(messages[-1]["content"])
        if len(self._replies) > 1:
            return self._replies.pop(0)
        return self._replies[0]


def _pool(*replies: str) -> LLMPool:
    return LLMPool([("local-1", _ScriptedClient(*replies), [])])


def _settings(tmp_path: Path) -> Settings:
    return Settings.model_validate(
        {
            "storage": {"workspace_dir": str(tmp_path / "workspace"), "deploy_dir": str(tmp_path / "deploy")},
            "llm": {"local": {"base_url": "http://127.0.0.1:11434/v1", "model": "test-model"}},
            "appearance": {"keep_recent_versions": 5},
        }
    )


def _store(tmp_path: Path) -> PersonalityStore:
    return PersonalityStore(tmp_path / "p.db", TRAIT_NAMES, MOOD_NAMES)


def _minimal_look(**overrides) -> LookSpec:
    fields: dict = {
        "base_template": "primitive_blob",
        "has_face": True,
        "palette": PaletteSpec(primary="#4A90D9", secondary="#FFFFFF", accent="#F5A623", emissive="#7FD1FF"),
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
        "capabilities": {"has_head": True, "has_arms": False, "has_legs": False, "has_face": True},
        "viseme_shape_keys": [f"viseme_{c}" for c in "ABCDEFGHX"],
        "animation_clips": [{"name": "idle_lookaround", "loop": True, "duration_s": 4.0}],
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

    async def fake_compile_and_publish(settings, look_spec, *, is_user_present, on_status=None):
        settings.ensure_dirs()
        existing_manifest = load_manifest(settings.deploy_dir)
        next_version = (existing_manifest.latest_version_number() if existing_manifest else 0) + 1
        filename = f"look-v{next_version}.glb"
        build_json = _build_json(glb_filename=filename)
        look_spec_dict = look_spec.model_dump(mode="json")

        if is_user_present():
            manifest = append_version(existing_manifest, build_json, look_spec_dict)
            assert manifest.current_version == next_version
            (settings.assets_dir / filename).write_bytes(b"glTF-fake")
            (settings.current_dir / filename).write_bytes(b"glTF-fake")
            save_manifest(settings.deploy_dir, manifest)
            return CompileResult(
                outcome="published", version=next_version, glb_path=settings.current_dir / filename, manifest=manifest
            )

        settings.staged_dir.mkdir(parents=True, exist_ok=True)
        (settings.staged_dir / filename).write_bytes(b"glTF-fake")
        (settings.staged_dir / f"look-v{next_version}.meta.json").write_text(
            json.dumps({"build_json": build_json, "look_spec": look_spec_dict})
        )
        return CompileResult(
            outcome="staged", version=next_version, glb_path=settings.staged_dir / filename, manifest=None
        )

    monkeypatch.setattr(evolution_module, "compile_and_publish", fake_compile_and_publish)


# --- pure helpers --------------------------------------------------------------


def test_cap_delta_budget_truncates_to_first_n_leaves():
    delta = {"wardrobe": {"outfit": "hoodie", "footwear": "boots"}, "palette": {"primary": "#111111"}}
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


def test_base_template_change_allowed_requires_high_openness_and_elapsed_days(tmp_path):
    import time

    store = _store(tmp_path)
    assert base_template_change_allowed(store, openness=90) is False  # base_template_since never set

    store.set_agent_state("base_template_since", str(time.time()))
    assert base_template_change_allowed(store, openness=90) is False  # not 7 days yet

    store.set_agent_state("base_template_since", str(time.time() - 8 * 86400))
    assert base_template_change_allowed(store, openness=90) is True
    assert base_template_change_allowed(store, openness=50) is False  # openness too low


def test_trigger_b_bias_lines_only_covers_documented_extremes():
    lines = trigger_b_bias_lines({"openness": 50, "boldness": 50, "formality": 50}, {"energy": 50, "stress": 50})
    assert lines == []

    lines = trigger_b_bias_lines({"openness": 80, "boldness": 80, "formality": 50}, {"energy": 80, "stress": 50})
    assert len(lines) == 3  # openness, boldness, energy -- not formality or stress


# --- Trigger A: synthetic end-to-end -------------------------------------------


@pytest.mark.asyncio
async def test_trigger_a_full_cycle_publishes_and_updates_agent_state(tmp_path, monkeypatch):
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
    assert _current_entry(manifest).look_spec["wardrobe"]["outfit"] == "wizard_robe"
    assert store.get_agent_state("last_redesign_timestamp") is not None
    assert store.get_agent_state("staged_version") is None


@pytest.mark.asyncio
async def test_trigger_a_reports_progress_stages_in_order(tmp_path, monkeypatch):
    _install_fake_compile_and_publish(monkeypatch)
    stages: list[dict] = []

    async def hook(payload: dict) -> None:
        stages.append(payload)

    delta_reply = json.dumps({"wardrobe": {"outfit": "wizard_robe"}})
    evolution = AvatarEvolution(_settings(tmp_path), _store(tmp_path), _pool(delta_reply), on_progress=hook)

    await evolution._run_trigger_a("put on a wizard robe")

    assert [p["stage"] for p in stages] == ["designing", "building", "done"]
    assert all(p["source"] == "user" for p in stages)
    assert stages[-1]["version"] == 1


@pytest.mark.asyncio
async def test_trigger_a_reports_failure_when_no_valid_delta(tmp_path, monkeypatch):
    _install_fake_compile_and_publish(monkeypatch)
    stages: list[dict] = []

    async def hook(payload: dict) -> None:
        stages.append(payload)

    evolution = AvatarEvolution(_settings(tmp_path), _store(tmp_path), _pool("not json"), on_progress=hook)

    assert await evolution._run_trigger_a("become a humanoid") is None
    assert [p["stage"] for p in stages] == ["designing", "failed"]


@pytest.mark.asyncio
async def test_a_raising_progress_hook_never_breaks_the_redesign(tmp_path, monkeypatch):
    _install_fake_compile_and_publish(monkeypatch)

    async def hook(_payload: dict) -> None:
        raise RuntimeError("overlay gone")

    delta_reply = json.dumps({"wardrobe": {"outfit": "hoodie"}})
    evolution = AvatarEvolution(_settings(tmp_path), _store(tmp_path), _pool(delta_reply), on_progress=hook)

    result = await evolution._run_trigger_a("put on a hoodie")
    assert result is not None and result.outcome == "published"


@pytest.mark.asyncio
async def test_trigger_a_tool_handler_replies_immediately_and_schedules_background_work(tmp_path, monkeypatch):
    _install_fake_compile_and_publish(monkeypatch)
    settings = _settings(tmp_path)
    store = _store(tmp_path)
    delta_reply = json.dumps({"wardrobe": {"outfit": "hoodie"}})
    evolution = AvatarEvolution(settings, store, _pool(delta_reply))

    reply = await evolution.change_appearance("put on a hoodie")
    assert "let me put something together" in reply.lower()
    assert load_manifest(settings.deploy_dir) is None  # background work hasn't run yet

    assert len(evolution._background_tasks) == 1
    await next(iter(evolution._background_tasks))
    assert _manifest(settings).current_version == 1


@pytest.mark.asyncio
async def test_trigger_a_retries_once_on_invalid_delta_then_succeeds(tmp_path, monkeypatch):
    _install_fake_compile_and_publish(monkeypatch)
    settings = _settings(tmp_path)
    store = _store(tmp_path)
    bad_reply = json.dumps({"wardrobe": {"outfit": "not_a_real_outfit"}})
    good_reply = json.dumps({"wardrobe": {"outfit": "hoodie"}})
    client = _ScriptedClient(bad_reply, good_reply)
    evolution = AvatarEvolution(settings, store, LLMPool([("local-1", client, [])]))

    result = await evolution._run_trigger_a("put on a hoodie")
    assert result is not None and result.outcome == "published"
    assert len(client.calls) == 2  # first attempt + one retry


@pytest.mark.asyncio
async def test_trigger_a_gives_up_silently_after_two_bad_deltas(tmp_path, monkeypatch):
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
async def test_revert_appearance_rewrites_manifest_pointer_only(tmp_path, monkeypatch):
    _install_fake_compile_and_publish(monkeypatch)
    settings = _settings(tmp_path)
    store = _store(tmp_path)
    evolution = AvatarEvolution(
        settings, store, _pool(json.dumps({"wardrobe": {"outfit": "hoodie"}}), json.dumps({"wardrobe": {"outfit": "vest"}}))
    )

    await evolution._run_trigger_a("put on a hoodie")
    await evolution._run_trigger_a("put on a vest")
    assert _manifest(settings).current_version == 2

    reply = await evolution.revert_appearance()
    assert "hoodie" in reply.lower() or "back to how i looked" in reply.lower()
    manifest = _manifest(settings)
    assert manifest.current_version == 1
    assert len(manifest.versions) == 2  # revert doesn't delete history


# --- Trigger B: synthetic stage -> reveal cycle --------------------------------


@pytest.mark.asyncio
async def test_trigger_b_stages_when_user_away_then_reveals_on_return(tmp_path, monkeypatch):
    _install_fake_compile_and_publish(monkeypatch)
    settings = _settings(tmp_path)
    store = _store(tmp_path)
    delta_reply = json.dumps({"wardrobe": {"outfit": "lab_coat"}})

    presence_state = PresenceState.AWAY
    evolution = AvatarEvolution(settings, store, _pool(delta_reply), get_presence_state=lambda: presence_state)

    await evolution.trigger_b_on_eligible()

    # staged, not published: live manifest untouched, staged_version recorded
    assert load_manifest(settings.deploy_dir) is None
    assert store.get_agent_state("staged_version") == "1"
    assert store.get_agent_state("last_redesign_timestamp") is None  # not stamped yet -- only on reveal

    # user returns -> Away/Idle -> Active transition
    await evolution.on_presence_transition(PresenceState.AWAY, PresenceState.ACTIVE)

    manifest = _manifest(settings)
    assert manifest is not None
    assert manifest.current_version == 1
    assert _current_entry(manifest).look_spec["wardrobe"]["outfit"] == "lab_coat"
    assert store.get_agent_state("staged_version") is None
    assert store.get_agent_state("last_redesign_timestamp") is not None


@pytest.mark.asyncio
async def test_trigger_b_publishes_immediately_when_user_already_active(tmp_path, monkeypatch):
    _install_fake_compile_and_publish(monkeypatch)
    settings = _settings(tmp_path)
    store = _store(tmp_path)
    delta_reply = json.dumps({"wardrobe": {"outfit": "vest"}})
    evolution = AvatarEvolution(settings, store, _pool(delta_reply), get_presence_state=lambda: PresenceState.ACTIVE)

    await evolution.trigger_b_on_eligible()

    manifest = _manifest(settings)
    assert manifest.current_version == 1
    assert store.get_agent_state("staged_version") is None
    assert store.get_agent_state("last_redesign_timestamp") is not None


@pytest.mark.asyncio
async def test_on_presence_transition_ignores_non_reveal_edges(tmp_path, monkeypatch):
    _install_fake_compile_and_publish(monkeypatch)
    settings = _settings(tmp_path)
    store = _store(tmp_path)
    evolution = AvatarEvolution(settings, store, _pool("{}"), get_presence_state=lambda: PresenceState.AWAY)
    store.set_agent_state("staged_version", "1")  # pretend something is staged

    await evolution.on_presence_transition(PresenceState.ACTIVE, PresenceState.AWAY)
    await evolution.on_presence_transition(PresenceState.ACTIVE, PresenceState.IDLE)
    await evolution.on_presence_transition(PresenceState.ACTIVE, PresenceState.ACTIVE)

    # none of these are an Away|Idle -> Active edge; staged_version must survive untouched
    assert store.get_agent_state("staged_version") == "1"


@pytest.mark.asyncio
async def test_boot_reconciliation_clears_staged_version_when_file_missing(tmp_path):
    settings = _settings(tmp_path)
    settings.ensure_dirs()
    store = _store(tmp_path)
    store.set_agent_state("staged_version", "7")  # no look-v7.glb on disk
    evolution = AvatarEvolution(settings, store, _pool("{}"))

    evolution.reconcile_staged_on_boot()
    assert store.get_agent_state("staged_version") is None


@pytest.mark.asyncio
async def test_boot_reconciliation_leaves_staged_version_when_file_present(tmp_path):
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
async def test_handle_asset_swap_failure_rolls_back_and_marks_corrupted(tmp_path, monkeypatch):
    _install_fake_compile_and_publish(monkeypatch)
    settings = _settings(tmp_path)
    store = _store(tmp_path)
    evolution = AvatarEvolution(
        settings, store, _pool(json.dumps({"wardrobe": {"outfit": "hoodie"}}), json.dumps({"wardrobe": {"outfit": "vest"}}))
    )
    await evolution._run_trigger_a("put on a hoodie")
    await evolution._run_trigger_a("put on a vest")
    assert _manifest(settings).current_version == 2

    evolution.handle_asset_swap_failure(2)

    manifest = _manifest(settings)
    assert manifest.current_version == 1
    assert _version_entry(manifest, 2).status == "corrupted"


def test_handle_asset_swap_failure_on_fresh_install_does_not_crash(tmp_path, monkeypatch, caplog):
    _install_fake_compile_and_publish(monkeypatch)
    settings = _settings(tmp_path)
    settings.ensure_dirs()
    store = _store(tmp_path)
    manifest = append_version(None, _build_json(), {"base_template": "primitive_blob"})
    save_manifest(settings.deploy_dir, manifest)
    evolution = AvatarEvolution(settings, store, _pool("{}"))

    evolution.handle_asset_swap_failure(1)  # no N-1 to fall back to

    result_manifest = _manifest(settings)
    assert result_manifest.current_version == 1  # unchanged, just marked
    assert _version_entry(result_manifest, 1).status == "corrupted"


# --- full integration with reflect.py's real eligibility gate -----------------


@pytest.mark.asyncio
async def test_full_trigger_b_wired_through_real_reflect_eligibility_gate(tmp_path, monkeypatch):
    """Confirms AvatarEvolution.trigger_b_on_eligible plugs directly into
    reflect.maybe_trigger_autonomous_redesign's on_eligible parameter."""
    _install_fake_compile_and_publish(monkeypatch)
    settings = _settings(tmp_path)
    store = _store(tmp_path)
    delta_reply = json.dumps({"wardrobe": {"outfit": "armor_light"}})
    evolution = AvatarEvolution(settings, store, _pool(delta_reply), get_presence_state=lambda: PresenceState.ACTIVE)

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
    assert manifest is not None and _current_entry(manifest).look_spec["wardrobe"]["outfit"] == "armor_light"


# --- wiring into the real Router (replacing router.py's stubs) ----------------


@pytest.mark.asyncio
async def test_wire_tools_replaces_router_stubs_end_to_end(tmp_path, monkeypatch):
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

    result = await router.handle("please change your look for me", system_prompt="irrelevant")
    assert result.source == "tool:change_appearance"
    assert "let me put something together" in result.text.lower()

    # the background compile scheduled by the real handler should still complete
    assert len(evolution._background_tasks) == 1
    await next(iter(evolution._background_tasks))
    assert _manifest(settings).current_version == 1


def test_keyword_prefilter_is_off_when_skills_are_enabled(tmp_path):
    settings = _settings(tmp_path)
    settings.skills.enabled = True
    assert build_router(settings)._match_tool_keyword("please change your look for me") is None


def test_current_look_reports_blob_when_stored_template_has_no_model(tmp_path):
    settings = _settings(tmp_path)
    evolution = AvatarEvolution(settings, _store(tmp_path), _pool("{}"))
    spec = evolution.current_look_spec().model_copy(update={"base_template": "standard_humanoid_female"})
    save_manifest(settings.deploy_dir, append_version(None, _build_json(), spec.model_dump(mode="json")))

    assert evolution.current_look_spec().base_template == "primitive_blob"
