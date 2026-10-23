"""Unit tests for brain.appearance.manifest -- pure Python, no Blender needed."""

from __future__ import annotations

import errno
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from brain.appearance.manifest import (
    append_version,
    atomic_write,
    load_manifest,
    mark_version_status,
    revert_to_version,
    save_manifest,
)


def _build_json(**overrides) -> dict:
    payload = {
        "glb_filename": "look-v1.glb",
        "sha256": "a" * 64,
        "created_at": 1718000000.0,
        "status": "ok",
        "seed": 42,
        "evaluated_triangles": 3904,
        "size_bytes": 358556,
        "has_face": True,
        "speech_indicator_mode": "viseme_shapekeys",
        "capabilities": {"has_head": True, "has_arms": False, "has_legs": False, "has_face": True},
        "viseme_shape_keys": [f"viseme_{c}" for c in "ABCDEFGHX"],
        "animation_clips": [{"name": "idle_lookaround", "loop": True, "duration_s": 4.0}],
        "bounds": {"height_m": 2.45, "width_m": 2.0, "depth_m": 2.31},
        "warnings": [],
    }
    payload.update(overrides)
    return payload


LOOK_SPEC = {"base_template": "primitive_blob", "has_face": True}


def test_append_version_starts_at_one_from_empty_manifest():
    manifest = append_version(None, _build_json(), LOOK_SPEC)
    assert manifest.current_version == 1
    assert len(manifest.versions) == 1
    assert manifest.versions[0].version == 1
    assert manifest.versions[0].look_spec == LOOK_SPEC


def test_append_version_increments_and_retains_history():
    m1 = append_version(None, _build_json(glb_filename="look-v1.glb"), LOOK_SPEC)
    m2 = append_version(m1, _build_json(glb_filename="look-v2.glb"), LOOK_SPEC)
    assert m2.current_version == 2
    assert len(m2.versions) == 2
    assert m2.get_version(1).glb_filename == "look-v1.glb"
    assert m2.get_version(2).glb_filename == "look-v2.glb"


def test_append_version_rejects_unknown_fields():
    with pytest.raises(ValidationError):
        append_version(None, _build_json(unexpected_field="nope"), LOOK_SPEC)


def test_revert_to_version_rewrites_pointer_only():
    m1 = append_version(None, _build_json(), LOOK_SPEC)
    m2 = append_version(m1, _build_json(seed=99), LOOK_SPEC)
    reverted = revert_to_version(m2, 1)
    assert reverted.current_version == 1
    # both versions still present -- revert is a pointer rewrite, not a deletion
    assert len(reverted.versions) == 2
    assert reverted.current().seed == 42


def test_revert_to_version_rejects_unknown_version():
    m1 = append_version(None, _build_json(), LOOK_SPEC)
    with pytest.raises(ValueError, match="not present"):
        revert_to_version(m1, 99)


def test_mark_version_status_updates_only_target_version():
    m1 = append_version(None, _build_json(), LOOK_SPEC)
    m2 = append_version(m1, _build_json(), LOOK_SPEC)
    marked = mark_version_status(m2, 2, "corrupted")
    assert marked.get_version(2).status == "corrupted"
    assert marked.get_version(1).status == "ok"


def test_mark_version_status_rejects_unknown_version():
    m1 = append_version(None, _build_json(), LOOK_SPEC)
    with pytest.raises(ValueError, match="not present"):
        mark_version_status(m1, 99, "corrupted")


def test_save_and_load_manifest_round_trips(tmp_path: Path):
    manifest = append_version(None, _build_json(), LOOK_SPEC)
    save_manifest(tmp_path, manifest)
    loaded = load_manifest(tmp_path)
    assert loaded is not None
    assert loaded == manifest


def test_load_manifest_returns_none_when_absent(tmp_path: Path):
    assert load_manifest(tmp_path) is None


def test_save_manifest_leaves_no_tmp_file_behind(tmp_path: Path):
    manifest = append_version(None, _build_json(), LOOK_SPEC)
    save_manifest(tmp_path, manifest)
    assert not any(p.suffix == ".tmp" for p in tmp_path.iterdir())
    assert (tmp_path / "manifest.json").is_file()


def test_atomic_write_str_and_bytes(tmp_path: Path):
    target = tmp_path / "data.json"
    atomic_write(target, json.dumps({"a": 1}))
    assert json.loads(target.read_text()) == {"a": 1}

    target2 = tmp_path / "data.bin"
    atomic_write(target2, b"\x00\x01\x02")
    assert target2.read_bytes() == b"\x00\x01\x02"


def test_atomic_write_exdev_is_surfaced_not_swallowed(tmp_path: Path, monkeypatch):
    """Confirms atomic_write doesn't silently catch EXDEV -- the caller
    (blender_gen.py) is responsible for ensuring the temp file is
    same-filesystem in the first place; this just verifies no error-hiding
    exists at this layer. Simulated via monkeypatch rather than a genuine
    cross-filesystem split, which depends on the deployment's own storage
    layout and can't be relied on to exist in every test environment."""

    def fake_replace(_src, _dst):
        raise OSError(errno.EXDEV, "Invalid cross-device link")

    monkeypatch.setattr("os.replace", fake_replace)
    with pytest.raises(OSError) as exc_info:
        atomic_write(tmp_path / "x.json", "{}")
    assert exc_info.value.errno == errno.EXDEV


def test_append_version_assigns_versioned_filename_regardless_of_build_json():
    # The generator's build.json always carries its scratch output name; the
    # manifest must record the real on-disk name or clients are pointed at a
    # file that never exists.
    m1 = append_version(None, _build_json(glb_filename="look.glb"), LOOK_SPEC)
    m2 = append_version(m1, _build_json(glb_filename="look.glb"), LOOK_SPEC)
    assert m1.get_version(1).glb_filename == "look-v1.glb"
    assert m2.get_version(2).glb_filename == "look-v2.glb"
