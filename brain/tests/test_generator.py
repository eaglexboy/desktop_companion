"""Integration tests for blender/generator.py -- all invoke the real `blender`
binary as a subprocess (bpy only exists inside Blender's own interpreter, so
this can't be imported and unit-tested directly). Skipped automatically if
`blender` isn't on PATH.

Note on determinism: Blender's glTF exporter does not produce
byte-identical .glb output across separate process runs once a scene has
more than one mesh object, even with PYTHONHASHSEED pinned and --threads 1
(a single-mesh scene exports byte-identical every time; a two-mesh scene
does not). This is exporter-internal reordering, not something
generator.py controls, so test_seed_determinism below asserts
structural/semantic equality (triangle count, shape keys, clip names,
bounds) rather than a raw SHA256 match.
"""

from __future__ import annotations

import json
import shutil
import struct
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
GENERATOR = REPO_ROOT / "blender" / "generator.py"
SAMPLE_LOOK = REPO_ROOT / "examples" / "sample_look.json"

BLENDER_BIN = shutil.which("blender") or "blender"
pytestmark = pytest.mark.skipif(shutil.which("blender") is None, reason="blender binary not found on PATH")


def _run_generator(*args: str, timeout: float = 90.0) -> subprocess.CompletedProcess:
    return subprocess.run(
        [BLENDER_BIN, "--background", "--factory-startup", "--python", str(GENERATOR), "--", *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _compile(spec: dict, tmp_path: Path, name: str = "look") -> tuple[subprocess.CompletedProcess, Path, dict]:
    spec_path = tmp_path / f"{name}.json"
    spec_path.write_text(json.dumps(spec))
    out_path = tmp_path / f"{name}.glb"
    deploy_dir = tmp_path / "deploy"
    result = _run_generator("--spec", str(spec_path), "--out", str(out_path), "--deploy-dir", str(deploy_dir))
    build_json = json.loads(out_path.with_suffix(".build.json").read_text())
    return result, out_path, build_json


def _minimal_valid_glb_bytes() -> bytes:
    """The smallest glTF binary Blender's importer will actually accept:
    a correct 12-byte header (magic, version=2, total length) followed by
    one minimal JSON chunk. Used to test the import path without needing a
    real authored asset."""
    content = json.dumps({"asset": {"version": "2.0"}}).encode()
    content += b" " * ((-len(content)) % 4)  # glTF chunks must be 4-byte aligned
    chunk_header = struct.pack("<I", len(content)) + b"JSON"
    total_length = 12 + len(chunk_header) + len(content)
    header = b"glTF" + struct.pack("<II", 2, total_length)
    return header + chunk_header + content


def _minimal_spec(**overrides) -> dict:
    spec = {
        "base_template": "primitive_blob",
        "has_face": True,
        "seed": 42,
        "proportions": {"muscularity": 0.5, "femininity": 0.5, "height": 1.0, "build": 1.0},
        "palette": {"primary": "#4A90D9", "secondary": "#FFFFFF", "accent": "#F5A623", "emissive": "#7FD1FF"},
        "surface_material": "matte",
        "wardrobe": {"outfit": "none", "footwear": "none"},
        "accessories": {},
    }
    spec.update(overrides)
    return spec


def test_tier3_generation(tmp_path):
    result, out_path, build_json = _compile(_minimal_spec(), tmp_path)
    assert "DESKMATE_BUILD_OK" in result.stdout
    assert result.returncode == 0
    assert out_path.is_file()
    assert build_json["status"] == "ok"
    assert build_json["evaluated_triangles"] > 0


def test_humanoid_base_templates_build_a_procedural_humanoid_when_no_model_exists(tmp_path):
    """With no standard_humanoid_{female,male}.glb installed, the generator
    builds its own humanoid: a head, two arms and two legs, rigged -- not the
    blob it used to quietly substitute."""
    for base_template in ("standard_humanoid_female", "standard_humanoid_male"):
        result, out_path, build_json = _compile(_minimal_spec(base_template=base_template), tmp_path, base_template)
        assert result.returncode == 0, result.stderr
        assert out_path.is_file()
        assert build_json["status"] == "ok"
        assert build_json["capabilities"] == {"has_head": True, "has_arms": True, "has_legs": True, "has_face": True}
        assert build_json["warnings"] == []


def test_blob_has_no_limbs(tmp_path):
    _, _, build_json = _compile(_minimal_spec(), tmp_path)
    assert build_json["capabilities"]["has_arms"] is False
    assert build_json["capabilities"]["has_legs"] is False


def test_installed_model_is_used_not_discarded(tmp_path):
    """A rigged model dropped in as standard_humanoid_male.glb becomes the
    body: it keeps its own limbs and lip-sync, and is scaled to house height."""
    _, source_glb, _ = _compile(_minimal_spec(base_template="standard_humanoid_female"), tmp_path, "source")
    base_templates_dir = tmp_path / "deploy" / "assets" / "base_templates"
    base_templates_dir.mkdir(parents=True)
    shutil.copy(source_glb, base_templates_dir / "standard_humanoid_male.glb")

    result, _, build_json = _compile(_minimal_spec(base_template="standard_humanoid_male"), tmp_path, "used")
    assert result.returncode == 0, result.stderr
    assert build_json["status"] == "ok"
    assert build_json["capabilities"] == {"has_head": True, "has_arms": True, "has_legs": True, "has_face": True}
    assert len(build_json["viseme_shape_keys"]) == 9
    assert build_json["bounds"]["height_m"] == pytest.approx(2.5, abs=0.01)
    assert not any("procedurally" in w for w in build_json["warnings"])


def test_installed_model_without_a_face_builds_faceless(tmp_path):
    """A model with no viseme shape keys is built with the talk_pulse speech
    indicator, and the build's own has_face says so."""
    _, source_glb, _ = _compile(_minimal_spec(base_template="primitive_mechanical", has_face=False), tmp_path, "source")
    custom_dir = tmp_path / "deploy" / "assets" / "custom_templates"
    custom_dir.mkdir(parents=True)
    shutil.copy(source_glb, custom_dir / "bot.glb")

    result, _, build_json = _compile(_minimal_spec(base_template="custom:bot", has_face=True), tmp_path, "used")
    assert result.returncode == 0, result.stderr
    assert build_json["has_face"] is False
    assert build_json["speech_indicator_mode"] == "talk_pulse"
    assert any("no viseme shape keys" in w for w in build_json["warnings"])


def test_unusable_installed_model_falls_back_to_procedural_with_warning(tmp_path):
    """A model file with no mesh in it must not fail the build."""
    base_templates_dir = tmp_path / "deploy" / "assets" / "base_templates"
    base_templates_dir.mkdir(parents=True)
    (base_templates_dir / "standard_humanoid_male.glb").write_bytes(_minimal_valid_glb_bytes())

    result, out_path, build_json = _compile(_minimal_spec(base_template="standard_humanoid_male"), tmp_path)
    assert result.returncode == 0, result.stderr
    assert build_json["status"] == "ok"
    assert any("building procedurally instead" in w for w in build_json["warnings"])
    assert build_json["capabilities"]["has_legs"] is True
    assert build_json["size_bytes"] == out_path.stat().st_size


def test_inspection_cli(tmp_path):
    # First compile a real .glb, then --inspect it as if it were a Tier-1 custom asset.
    _, glb_path, _ = _compile(_minimal_spec(), tmp_path)
    inspect_out = tmp_path / "capabilities.json"
    result = _run_generator("--inspect", str(glb_path), "--out", str(inspect_out))
    assert result.returncode == 0, result.stderr
    assert "DESKMATE_BUILD_OK" in result.stdout
    payload = json.loads(inspect_out.read_text())
    assert payload["tier"] == "tier_1_custom"
    assert payload["has_face"] is True
    assert set(payload["viseme_shape_keys"]) == {f"viseme_{c}" for c in "ABCDEFGHX"}
    assert "spine" in payload["bone_names"]
    assert payload["triangle_count"] > 0


def test_proportion_baking(tmp_path):
    base = _compile(_minimal_spec(proportions={"muscularity": 0.5, "femininity": 0.5, "height": 1.0, "build": 1.0}), tmp_path, "base")[2]
    tall = _compile(_minimal_spec(proportions={"muscularity": 0.5, "femininity": 0.5, "height": 1.4, "build": 1.0}), tmp_path, "tall")[2]
    assert tall["bounds"]["height_m"] > base["bounds"]["height_m"]


def test_wardrobe_fit(tmp_path):
    spec = _minimal_spec(
        wardrobe={"outfit": "hoodie", "footwear": "sneakers"},
        accessories={"socket_eyes": "glasses", "socket_head_top": "pointy_hat"},
    )
    result, _, build_json = _compile(spec, tmp_path)
    assert result.returncode == 0, result.stderr
    assert build_json["status"] == "ok"
    # accessories/wardrobe aren't named individually in build.json, but a
    # higher triangle count than the bare-body case confirms geometry was
    # actually added, not silently skipped.
    bare = _compile(_minimal_spec(), tmp_path, "bare")[2]
    assert build_json["evaluated_triangles"] > bare["evaluated_triangles"]


def test_faceless_pulse(tmp_path):
    spec = _minimal_spec(base_template="primitive_mechanical", has_face=False)
    result, _, build_json = _compile(spec, tmp_path)
    assert result.returncode == 0, result.stderr
    assert build_json["has_face"] is False
    assert build_json["speech_indicator_mode"] == "talk_pulse"
    assert build_json["viseme_shape_keys"] == []


def test_state_loops(tmp_path):
    # primitive_blob: no arms, no legs -- move/think must use their
    # degraded (hop / spine-pulse) fallbacks, and arm-only gestures must be omitted.
    _, _, blob_json = _compile(_minimal_spec(), tmp_path, "blob")
    clip_names = {c["name"] for c in blob_json["animation_clips"]}
    assert "move" in clip_names
    assert "think" in clip_names
    assert "greet" not in clip_names
    assert "shrug" not in clip_names

    # primitive_mechanical: has arms, no legs -- greet/shrug must be present.
    _, _, mech_json = _compile(_minimal_spec(base_template="primitive_mechanical"), tmp_path, "mech")
    mech_clip_names = {c["name"] for c in mech_json["animation_clips"]}
    assert "greet" in mech_clip_names
    assert "shrug" in mech_clip_names


def test_environment_isolation(tmp_path):
    """--factory-startup must mean the generator doesn't depend on (or get
    polluted by) a user's local Blender preferences/addons -- verified here
    by confirming a clean run from an isolated subprocess environment with
    PYTHONPATH/VIRTUAL_ENV stripped produces the same successful result as a
    normal run, i.e. the script carries everything it needs via its own
    repo-root sys.path.insert()."""
    import os

    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "VIRTUAL_ENV")}
    spec_path = tmp_path / "look.json"
    spec_path.write_text(json.dumps(_minimal_spec()))
    out_path = tmp_path / "look.glb"
    result = subprocess.run(
        [
            BLENDER_BIN,
            "--background",
            "--factory-startup",
            "--python",
            str(GENERATOR),
            "--",
            "--spec",
            str(spec_path),
            "--out",
            str(out_path),
            "--deploy-dir",
            str(tmp_path / "deploy"),
        ],
        capture_output=True,
        text=True,
        timeout=90,
        env=env,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert out_path.is_file()


def test_budget_enforcement(tmp_path, monkeypatch):
    """Rather than authoring a 30k+ triangle spec (expensive and fragile),
    confirms the budget-exceeded path is real by temporarily lowering
    generator.py's own TRIANGLE_BUDGET via an env-free, file-level patch:
    a tiny wrapper script that imports the module and overrides the constant
    before calling main() -- the generator has no CLI flag for this (budgets
    are deliberately fixed per spec), so this is the only way to exercise the
    failure path without an enormous, slow real mesh."""
    wrapper = tmp_path / "budget_wrapper.py"
    wrapper.write_text(
        "import sys\n"
        f"sys.path.insert(0, {str(REPO_ROOT)!r})\n"
        "import blender.generator as g\n"
        "g.TRIANGLE_BUDGET = 10\n"
        "g.main()\n"
    )
    spec_path = tmp_path / "look.json"
    spec_path.write_text(json.dumps(_minimal_spec()))
    out_path = tmp_path / "look.glb"
    result = subprocess.run(
        [
            BLENDER_BIN,
            "--background",
            "--factory-startup",
            "--python",
            str(wrapper),
            "--",
            "--spec",
            str(spec_path),
            "--out",
            str(out_path),
            "--deploy-dir",
            str(tmp_path / "deploy"),
        ],
        capture_output=True,
        text=True,
        timeout=90,
        check=False,
    )
    assert "DESKMATE_BUILD_FAIL budget_exceeded" in result.stdout
    assert result.returncode == 1
    build_json = json.loads(out_path.with_suffix(".build.json").read_text())
    assert build_json["status"] == "fail"
    assert not out_path.is_file()  # export_glb never ran


def test_seed_determinism(tmp_path):
    """See module docstring: Blender's exporter reorders multi-mesh buffer
    data non-deterministically across process runs, so this asserts
    structural/semantic equality rather than a byte-exact SHA256 match."""
    _, _, a = _compile(_minimal_spec(seed=99), tmp_path, "det_a")
    _, _, b = _compile(_minimal_spec(seed=99), tmp_path, "det_b")

    assert a["evaluated_triangles"] == b["evaluated_triangles"]
    assert a["viseme_shape_keys"] == b["viseme_shape_keys"]
    assert a["animation_clips"] == b["animation_clips"]
    assert a["bounds"] == b["bounds"]
    assert a["capabilities"] == b["capabilities"]
