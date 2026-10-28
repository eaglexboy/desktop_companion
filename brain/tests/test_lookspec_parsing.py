"""Tests for brain.appearance.spec: LookSpec schema validation, catalog
validation (without invoking Blender), and delta merging."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from brain.appearance.spec import (
    CATALOGS_SEED_DIR,
    CatalogError,
    LookSpec,
    available_custom_templates,
    ensure_catalogs_seeded,
    example_look_spec,
    look_summary,
    merge_delta,
    validate_against_catalogs,
)

VALID_PALETTE = {"primary": "#4A90D9", "secondary": "#FFFFFF", "accent": "#F5A623", "emissive": "#7FD1FF"}


def _minimal_spec(**overrides) -> dict:
    spec = {
        "base_template": "primitive_blob",
        "has_face": True,
        "palette": dict(VALID_PALETTE),
        "wardrobe": {"outfit": "none", "footwear": "none"},
    }
    spec.update(overrides)
    return spec


# --- schema validation -------------------------------------------------------


def test_minimal_valid_spec_parses_with_defaults():
    spec = LookSpec.model_validate(_minimal_spec())
    assert spec.seed == 42
    assert spec.surface_material == "matte"
    assert spec.proportions.height == 1.0
    assert spec.accessories.socket_eyes is None


def test_rejects_bad_hex_color():
    with pytest.raises(ValidationError):
        LookSpec.model_validate(_minimal_spec(palette={**VALID_PALETTE, "primary": "not-a-color"}))


def test_rejects_palette_missing_required_field():
    with pytest.raises(ValidationError):
        LookSpec.model_validate(_minimal_spec(palette={"primary": "#4A90D9"}))


def test_rejects_out_of_range_proportions():
    with pytest.raises(ValidationError):
        LookSpec.model_validate(_minimal_spec(proportions={"height": 5.0}))
    with pytest.raises(ValidationError):
        LookSpec.model_validate(_minimal_spec(proportions={"muscularity": -0.5}))


def test_rejects_unknown_surface_material():
    with pytest.raises(ValidationError):
        LookSpec.model_validate(_minimal_spec(surface_material="shiny"))


def test_rejects_unknown_outfit_enum_value():
    with pytest.raises(ValidationError):
        LookSpec.model_validate(_minimal_spec(wardrobe={"outfit": "tuxedo", "footwear": "none"}))


def test_rejects_unknown_accessory_socket_key():
    with pytest.raises(ValidationError):
        LookSpec.model_validate(_minimal_spec(accessories={"socket_tail": "wand"}))


def test_rejects_unknown_top_level_field():
    with pytest.raises(ValidationError):
        LookSpec.model_validate(_minimal_spec(extra_field="nope"))


def test_missing_required_top_level_field_rejected():
    spec = _minimal_spec()
    del spec["wardrobe"]
    with pytest.raises(ValidationError):
        LookSpec.model_validate(spec)


def test_example_look_spec_is_itself_valid():
    spec = example_look_spec()
    assert isinstance(spec, LookSpec)


def test_repo_sample_look_json_round_trips():
    repo_root = Path(__file__).resolve().parent.parent.parent
    sample = json.loads((repo_root / "examples" / "sample_look.json").read_text())
    spec = LookSpec.model_validate(sample)
    assert spec.base_template == "primitive_blob"


# --- catalog validation (no Blender) -----------------------------------------


def _deploy_with_catalogs(tmp_path: Path) -> Path:
    deploy_dir = tmp_path / "deploy"
    ensure_catalogs_seeded(deploy_dir)
    return deploy_dir


def test_ensure_catalogs_seeded_copies_seed_files_once(tmp_path):
    deploy_dir = _deploy_with_catalogs(tmp_path)
    catalogs_dir = deploy_dir / "assets" / "catalogs"
    assert (catalogs_dir / "templates_catalog.json").is_file()
    assert (catalogs_dir / "wardrobe_catalog.json").is_file()

    # never overwrites an existing (possibly hand-edited) catalog
    (catalogs_dir / "templates_catalog.json").write_text('{"templates": {"custom_only": {}}}')
    ensure_catalogs_seeded(deploy_dir)
    assert json.loads((catalogs_dir / "templates_catalog.json").read_text()) == {"templates": {"custom_only": {}}}


def test_validate_against_catalogs_accepts_known_tier3_template(tmp_path):
    deploy_dir = _deploy_with_catalogs(tmp_path)
    spec = LookSpec.model_validate(_minimal_spec())
    validate_against_catalogs(spec, deploy_dir)  # must not raise


def test_available_custom_templates_lists_dropped_in_models(tmp_path):
    deploy_dir = _deploy_with_catalogs(tmp_path)
    assert available_custom_templates(deploy_dir) == []
    custom_dir = deploy_dir / "assets" / "custom_templates"
    custom_dir.mkdir(parents=True)
    (custom_dir / "robot.glb").write_bytes(b"glTF")
    (custom_dir / "cat.blend").write_bytes(b"")
    (custom_dir / "notes.txt").write_text("not a model")
    assert available_custom_templates(deploy_dir) == ["custom:cat", "custom:robot"]


def test_validate_against_catalogs_accepts_both_humanoid_genders(tmp_path):
    deploy_dir = _deploy_with_catalogs(tmp_path)
    for base_template in ("standard_humanoid_female", "standard_humanoid_male"):
        spec = LookSpec.model_validate(_minimal_spec(base_template=base_template))
        validate_against_catalogs(spec, deploy_dir)  # must not raise


def test_validate_against_catalogs_rejects_unknown_base_template(tmp_path):
    deploy_dir = _deploy_with_catalogs(tmp_path)
    spec = LookSpec.model_validate(_minimal_spec(base_template="totally_made_up"))
    with pytest.raises(CatalogError, match="base_template"):
        validate_against_catalogs(spec, deploy_dir)


def test_validate_against_catalogs_rejects_unknown_outfit(tmp_path):
    deploy_dir = _deploy_with_catalogs(tmp_path)
    # bypass LookSpec's own enum check by constructing the nested model directly
    # isn't possible (Literal enforces it) -- so this exercises the catalog
    # check against a *valid enum value that just isn't in this catalog file*,
    # by shrinking the catalog instead.
    catalogs_dir = deploy_dir / "assets" / "catalogs"
    wardrobe_catalog = json.loads((catalogs_dir / "wardrobe_catalog.json").read_text())
    wardrobe_catalog["outfits"] = ["none"]
    (catalogs_dir / "wardrobe_catalog.json").write_text(json.dumps(wardrobe_catalog))

    spec = LookSpec.model_validate(_minimal_spec(wardrobe={"outfit": "hoodie", "footwear": "none"}))
    with pytest.raises(CatalogError, match="outfit"):
        validate_against_catalogs(spec, deploy_dir)


def test_validate_against_catalogs_rejects_unknown_accessory_item(tmp_path):
    deploy_dir = _deploy_with_catalogs(tmp_path)
    spec = LookSpec.model_validate(_minimal_spec(accessories={"socket_eyes": "monocle"}))
    with pytest.raises(CatalogError, match="socket_eyes"):
        validate_against_catalogs(spec, deploy_dir)


def test_validate_against_catalogs_accepts_known_accessory_item(tmp_path):
    deploy_dir = _deploy_with_catalogs(tmp_path)
    spec = LookSpec.model_validate(_minimal_spec(accessories={"socket_eyes": "glasses"}))
    validate_against_catalogs(spec, deploy_dir)  # must not raise


def test_validate_against_catalogs_custom_template_requires_file_on_disk(tmp_path):
    deploy_dir = _deploy_with_catalogs(tmp_path)
    spec = LookSpec.model_validate(_minimal_spec(base_template="custom:cyber_cat"))
    with pytest.raises(CatalogError, match="custom"):
        validate_against_catalogs(spec, deploy_dir)

    custom_dir = deploy_dir / "assets" / "custom_templates"
    custom_dir.mkdir(parents=True)
    (custom_dir / "cyber_cat.glb").write_bytes(b"glTF")
    validate_against_catalogs(spec, deploy_dir)  # must not raise now


def test_catalogs_seed_dir_has_both_files_committed():
    assert (CATALOGS_SEED_DIR / "templates_catalog.json").is_file()
    assert (CATALOGS_SEED_DIR / "wardrobe_catalog.json").is_file()
    templates = json.loads((CATALOGS_SEED_DIR / "templates_catalog.json").read_text())["templates"]
    assert "primitive_blob" in templates
    assert "primitive_mechanical" in templates
    assert "standard_humanoid_female" in templates
    assert "standard_humanoid_male" in templates


# --- merge_delta --------------------------------------------------------------


def test_merge_delta_applies_partial_nested_changes():
    current = LookSpec.model_validate(_minimal_spec())
    delta = {
        "wardrobe": {"outfit": "wizard_robe"},
        "accessories": {"socket_eyes": "glasses", "socket_head_top": "pointy_hat"},
        "palette": {"primary": "#1E1E2E", "accent": "#F38BA8"},
    }
    merged = merge_delta(current, delta)

    assert merged.wardrobe.outfit == "wizard_robe"
    assert merged.wardrobe.footwear == "none"  # untouched
    assert merged.accessories.socket_eyes == "glasses"
    assert merged.accessories.socket_head_top == "pointy_hat"
    assert merged.palette.primary == "#1E1E2E"
    assert merged.palette.secondary == VALID_PALETTE["secondary"]  # untouched
    assert merged.palette.accent == "#F38BA8"


def test_merge_delta_result_is_still_validated():
    current = LookSpec.model_validate(_minimal_spec())
    with pytest.raises(ValidationError):
        merge_delta(current, {"surface_material": "not_a_real_material"})


def test_merge_delta_does_not_mutate_current():
    current = LookSpec.model_validate(_minimal_spec())
    merge_delta(current, {"wardrobe": {"outfit": "hoodie"}})
    assert current.wardrobe.outfit == "none"


# --- look_summary --------------------------------------------------------------


def test_look_summary_bare_spec():
    spec = LookSpec.model_validate(_minimal_spec())
    assert look_summary(spec) == "[Appearance: primitive_blob, palette #4A90D9/#F5A623]"


def test_look_summary_includes_wardrobe_and_accessories():
    spec = LookSpec.model_validate(
        _minimal_spec(
            wardrobe={"outfit": "wizard_robe", "footwear": "boots"},
            accessories={"socket_eyes": "glasses", "socket_head_top": "pointy_hat"},
        )
    )
    summary = look_summary(spec)
    assert summary.startswith("[Appearance: primitive_blob, wearing wizard_robe, boots")
    assert "pointy_hat" in summary  # socket_head_top sorts before socket_eyes
    assert "glasses" in summary
    assert summary.endswith("palette #4A90D9/#F5A623]")
