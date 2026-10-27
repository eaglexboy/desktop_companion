"""Skill enable/pin state, the context budget, and the importer's source labels and categories."""

from __future__ import annotations

from pathlib import Path

import pytest

from brain.skills import context
from brain.skills.agentskills import discover, display_names, skill_id
from brain.skills.importer import _label_from, install, main
from brain.skills.manage import (
    StateChangeError,
    build_views,
    change_state,
    default_enabled,
    resolve,
    summarize,
)
from brain.skills.state import SkillStateStore

SKILL = "---\nname: {name}\ndescription: {desc}\n---\nbody\n"


def put(root: Path, source: str | None, name: str, desc: str = "Does a thing. Use it when asked.", extra: dict | None = None) -> Path:
    d = (root / source / name) if source else (root / name)
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(SKILL.format(name=name, desc=desc))
    for rel, text in (extra or {}).items():
        (d / rel).parent.mkdir(parents=True, exist_ok=True)
        (d / rel).write_text(text)
    return d


def install_all(tmp_path: Path, **by_name):
    """Installs skills through the real importer so they carry provenance and a tier."""
    root = tmp_path / "skills"
    for source, spec in by_name.items():
        for name, extra in spec.items():
            src = put(tmp_path / "src" / source, None, name, extra=extra)
            assert install(src, root, source=source).ok
    return root


@pytest.fixture
def env(tmp_path):
    root = install_all(
        tmp_path,
        hermes={"pdf": {}, "arxiv": {}, "risky": {"scripts/x.sh": "echo"}},
        anthropics={"pdf": {}},
    )
    store = SkillStateStore(tmp_path / "state.db")
    skills, _ = discover(root)
    yield root, store, skills
    store.close()


def views_of(root, store, skills):
    return build_views(root, skills, store.all())


# --- identity ------------------------------------------------------------------------


def test_ids_are_source_slash_name_and_clashing_names_get_a_static_source_suffix(env):
    root, _store, skills = env
    ids = sorted(skill_id(root, k) for k in skills)
    assert ids == ["anthropics/pdf", "hermes/arxiv", "hermes/pdf", "hermes/risky"]
    names = display_names(root, skills)
    assert names["hermes/arxiv"] == "arxiv"
    assert names["hermes/pdf"] == "pdf@hermes" and names["anthropics/pdf"] == "pdf@anthropics"


def test_resolve_prefers_exact_id_then_alias_and_refuses_ambiguous_bare_names(env):
    root, store, skills = env
    v = views_of(root, store, skills)
    assert resolve(v, "hermes/pdf").id == "hermes/pdf" and resolve(v, "arxiv").id == "hermes/arxiv"
    with pytest.raises(StateChangeError, match="ambiguous"):
        resolve(v, "pdf")
    assert resolve(v, "pdf@anthropics").id == "anthropics/pdf"
    with pytest.raises(StateChangeError, match="no installed skill"):
        resolve(v, "nope")


# --- enabling rules ------------------------------------------------------------------


def test_imports_start_disabled_and_hand_placed_skills_start_enabled(tmp_path, env):
    root, store, skills = env
    assert not any(v.enabled for v in views_of(root, store, skills))
    put(root, None, "mine")
    skills, _ = discover(root)
    mine = next(v for v in views_of(root, store, skills) if v.id == "local/mine")
    assert mine.tier == "local" and mine.enabled and default_enabled("local") and not default_enabled("imported")


def test_enabling_needs_confirmation_but_disabling_does_not(env):
    root, store, skills = env
    kw = {"changed_by": "cli", "max_pinned": 8}
    with pytest.raises(StateChangeError, match="confirmation"):
        change_state(store, views_of(root, store, skills), "arxiv", enabled=True, **kw)
    assert change_state(store, views_of(root, store, skills), "arxiv", enabled=True, confirmed=True, **kw).enabled
    assert not change_state(store, views_of(root, store, skills), "arxiv", enabled=False, **kw).enabled


def test_flagged_skills_need_their_name_typed(env):
    root, store, skills = env
    kw = {"changed_by": "api", "max_pinned": 8, "enabled": True, "confirmed": True}
    with pytest.raises(StateChangeError, match="flagged"):
        change_state(store, views_of(root, store, skills), "risky", confirm_text="yes", **kw)
    assert change_state(store, views_of(root, store, skills), "risky", confirm_text="Risky", **kw).enabled


def test_pinning_needs_an_enabled_skill_respects_the_cap_and_disabling_unpins(env):
    root, store, skills = env
    base = {"changed_by": "cli", "max_pinned": 1}
    with pytest.raises(StateChangeError, match="enabled"):
        change_state(store, views_of(root, store, skills), "arxiv", pinned=True, **base)
    change_state(store, views_of(root, store, skills), "arxiv", enabled=True, confirmed=True, **base)
    change_state(store, views_of(root, store, skills), "hermes/pdf", enabled=True, confirmed=True, **base)
    assert change_state(store, views_of(root, store, skills), "arxiv", pinned=True, **base).pinned
    with pytest.raises(StateChangeError, match="at most 1"):
        change_state(store, views_of(root, store, skills), "hermes/pdf", pinned=True, **base)
    after = change_state(store, views_of(root, store, skills), "arxiv", enabled=False, **base)
    assert not after.enabled and not after.pinned


def test_aliases_are_validated_unique_and_usable_as_references(env):
    root, store, skills = env
    kw = {"changed_by": "cli", "max_pinned": 8}
    change_state(store, views_of(root, store, skills), "hermes/pdf", alias="docs", **kw)
    assert resolve(views_of(root, store, skills), "docs").id == "hermes/pdf"
    with pytest.raises(StateChangeError, match="already used"):
        change_state(store, views_of(root, store, skills), "anthropics/pdf", alias="docs", **kw)
    with pytest.raises(StateChangeError, match="lowercase"):
        change_state(store, views_of(root, store, skills), "arxiv", alias="Bad Alias", **kw)
    assert change_state(store, views_of(root, store, skills), "docs", alias="", **kw).alias is None


def test_unknown_sources_of_change_are_refused(env):
    root, store, skills = env
    with pytest.raises(StateChangeError):
        change_state(store, views_of(root, store, skills), "arxiv", enabled=False, changed_by="model", max_pinned=8)


def test_state_survives_reopening_the_database(tmp_path):
    db = tmp_path / "s.db"
    a = SkillStateStore(db)
    a.put("x/y", enabled=True, pinned=True, alias="why", changed_by="cli")
    a.close()
    b = SkillStateStore(db)
    s = b.get("x/y")
    assert s is not None and (s.enabled, s.pinned, s.alias) == (True, True, "why")
    b.close()


# --- context budget ------------------------------------------------------------------


def test_stub_is_one_line_first_sentence_and_capped():
    assert context.stub("pdf", "Extract text.\nAlso merges files. More.") == "pdf: Extract text."
    long = context.stub("x", "word " * 100)
    assert len(long) <= context.STUB_MAX_CHARS and long.endswith("…")


def test_budget_is_the_smaller_of_cap_and_fraction_of_the_window():
    assert context.budget_tokens(8192, 0.03, 400) == 245
    assert context.budget_tokens(200_000, 0.03, 400) == 400


def test_skill_tokens_never_exceed_the_budget_however_many_skills_exist():
    """ADR invariant 1: cost is O(budget), not O(N)."""
    stubs = [(f"s/{i}", context.stub(f"skill-{i}", f"Does thing number {i}. And more words follow here.")) for i in range(5000)]
    for budget in (0, 50, 245, 400):
        plan = context.plan_stubs(stubs, budget)
        assert plan.tokens_used <= budget
        assert len(plan.kept) + len(plan.dropped) == len(stubs)  # dropped, never silently lost
        assert plan.kept == stubs[: len(plan.kept)]


def test_summary_reports_the_meter_and_that_nothing_is_in_a_prompt_yet(env):
    from brain.config import SkillsConfig

    root, store, skills = env
    kw = {"changed_by": "cli", "max_pinned": 8}
    change_state(store, views_of(root, store, skills), "arxiv", enabled=True, confirmed=True, **kw)
    change_state(store, views_of(root, store, skills), "arxiv", pinned=True, **kw)
    meter = summarize(SkillsConfig(), 8192, views_of(root, store, skills))
    assert (meter["installed"], meter["enabled"], meter["pinned"], meter["in_prompt"]) == (4, 1, 1, False)
    assert 0 < meter["tokens_used"] <= meter["budget_tokens"] == 245


# --- config --------------------------------------------------------------------------


def test_context_tokens_exists_on_every_backend_and_the_smallest_is_used():
    from brain.config import LLMConfig

    llm = LLMConfig.model_validate({
        "local": {"base_url": "http://x/v1", "model": "m", "context_tokens": 16384},
        "cloud": {
            "big": {"provider_type": "anthropic", "base_url": "u", "model": "m", "api_key": "k", "context_tokens": 200000},
            "small": {"provider_type": "openai", "base_url": "u", "model": "m", "api_key": "k", "context_tokens": 8192},
            "off": {"provider_type": "openai", "base_url": "u", "model": "m", "api_key": "k", "context_tokens": 1024, "enabled": False},
        },
    })
    assert llm.smallest_context_tokens() == 8192  # a disabled provider cannot answer, so it does not shrink the window


# --- importer: labels and categories -------------------------------------------------


def test_source_label_is_the_repo_name_unless_a_different_owner_already_took_it(tmp_path):
    assert _label_from("https://github.com/NousResearch/hermes-agent.git") == "hermes-agent"
    dest = tmp_path / "skills"
    src = put(tmp_path / "src", None, "one")
    assert install(src, dest, source="skills", origin="https://github.com/anthropics/skills").ok
    assert _label_from("https://github.com/anthropics/skills", dest) == "skills"
    assert _label_from("https://github.com/someoneelse/skills", dest) == "someoneelse-skills"


def test_importer_records_the_category_from_the_path_inside_the_repository(tmp_path):
    repo = tmp_path / "repo"
    put(repo / "research", None, "arxiv")
    put(repo, None, "toplevel")
    assert main([str(repo), "--dest", str(tmp_path / "out"), "--source-label", "r"]) == 0
    skills, _ = discover(tmp_path / "out")
    cats = {}
    for k in skills:
        cats[k.name] = next(
            line.split('"')[1] for line in (k.path / ".source").read_text().splitlines() if line.startswith("category")
        )
    assert cats == {"arxiv": "research", "toplevel": ""}
