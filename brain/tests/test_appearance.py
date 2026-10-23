"""Tests for brain.appearance.blender_gen -- compile_and_publish's
orchestration (timeout handling, Option B staging vs. immediate publish,
atomic commit, ground-truth verification, cancellation). These stand in a
fake executable for the real `blender` binary (a tiny Python script that
mimics the DESKMATE_BUILD_OK/FAIL stdout contract and writes a fake
build.json) so the tests are fast and hermetic -- the real pipeline is
already covered end-to-end against the actual Blender binary in
brain/tests/test_generator.py.
"""

from __future__ import annotations

import errno
import os
import stat
import textwrap
from pathlib import Path

import pytest

from brain.appearance import blender_gen
from brain.appearance.blender_gen import (
    BuildCancelledError,
    BuildTimeoutError,
    GenerationError,
    _verify_build_output,
    cancel_running_build,
    compile_and_publish,
    reveal_staged,
)
from brain.appearance.manifest import load_manifest
from brain.appearance.spec import LookSpec, PaletteSpec, WardrobeSpec
from brain.config import Settings

VALID_PALETTE = PaletteSpec(primary="#4A90D9", secondary="#FFFFFF", accent="#F5A623", emissive="#7FD1FF")


def _look_spec(**overrides) -> LookSpec:
    fields: dict = {
        "base_template": "primitive_blob",
        "has_face": True,
        "palette": VALID_PALETTE,
        "wardrobe": WardrobeSpec(),
    }
    fields.update(overrides)
    return LookSpec.model_validate(fields)


def _settings(tmp_path: Path, blender_bin: str) -> Settings:
    return Settings.model_validate(
        {
            "storage": {"workspace_dir": str(tmp_path / "workspace"), "deploy_dir": str(tmp_path / "deploy")},
            "llm": {"local": {"base_url": "http://127.0.0.1:11434/v1", "model": "test-model"}},
            "appearance": {"blender_bin": blender_bin, "keep_recent_versions": 2},
        }
    )


_FAKE_BLENDER_OK = textwrap.dedent(
    """\
    #!/usr/bin/env python3
    import json, sys
    argv = sys.argv
    def arg(flag):
        return argv[argv.index(flag) + 1]
    out = arg("--out")
    with open(out, "wb") as f:
        f.write(b"glTF" + b"\\x00" * 16)
    build_json = {
        "glb_filename": out.split("/")[-1], "sha256": "a" * 64, "created_at": 1.0, "status": "ok",
        "seed": 42, "evaluated_triangles": 100, "size_bytes": 20, "has_face": True,
        "speech_indicator_mode": "viseme_shapekeys",
        "capabilities": {"has_head": True, "has_arms": False, "has_legs": False, "has_face": True},
        "viseme_shape_keys": [f"viseme_{c}" for c in "ABCDEFGHX"],
        "animation_clips": [
            {"name": "idle_lookaround", "loop": True, "duration_s": 4.0},
            {"name": "move", "loop": True, "duration_s": 1.0},
            {"name": "think", "loop": True, "duration_s": 3.0},
        ],
        "bounds": {"height_m": 2.0, "width_m": 1.0, "depth_m": 1.0},
        "warnings": [],
    }
    build_json_path = out.rsplit(".", 1)[0] + ".build.json"
    with open(build_json_path, "w") as f:
        json.dump(build_json, f)
    print("DESKMATE_BUILD_OK " + out)
    sys.exit(0)
    """
)

_FAKE_BLENDER_FAIL = textwrap.dedent(
    """\
    #!/usr/bin/env python3
    print("DESKMATE_BUILD_FAIL budget_exceeded: triangles=99999 (max 30000)")
    import sys
    sys.exit(1)
    """
)

_FAKE_BLENDER_HANGS = textwrap.dedent(
    """\
    #!/usr/bin/env python3
    import time
    time.sleep(999)
    """
)


def _write_fake_blender(tmp_path: Path, script: str, name: str = "fake_blender") -> str:
    path = tmp_path / name
    path.write_text(script)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return str(path)


@pytest.fixture(autouse=True)
def _reset_lock_between_tests():
    # Guards against one test's fake process leaking into the next if an
    # assertion fails mid-build and leaves _current_proc set.
    yield
    blender_gen._current_proc = None


# --- _verify_build_output (pure, no subprocess) -------------------------------


def test_verify_build_output_rejects_bad_magic_bytes(tmp_path):
    bad = tmp_path / "bad.glb"
    bad.write_bytes(b"NOTGLTF")
    with pytest.raises(GenerationError, match="magic bytes"):
        _verify_build_output(bad, {"animation_clips": []}, has_face=False)


def test_verify_build_output_rejects_missing_viseme_keys(tmp_path):
    glb = tmp_path / "ok.glb"
    glb.write_bytes(b"glTF" + b"\x00" * 10)
    build_json = {"viseme_shape_keys": ["viseme_A"], "animation_clips": [{"name": "idle_x"}, {"name": "move"}, {"name": "think"}]}
    with pytest.raises(GenerationError, match="viseme shape keys"):
        _verify_build_output(glb, build_json, has_face=True)


def test_verify_build_output_rejects_missing_core_clips(tmp_path):
    glb = tmp_path / "ok.glb"
    glb.write_bytes(b"glTF" + b"\x00" * 10)
    build_json = {"viseme_shape_keys": [], "animation_clips": [{"name": "idle_x"}]}
    with pytest.raises(GenerationError, match="core clip"):
        _verify_build_output(glb, build_json, has_face=False)


def test_verify_build_output_accepts_valid_output(tmp_path):
    glb = tmp_path / "ok.glb"
    glb.write_bytes(b"glTF" + b"\x00" * 10)
    build_json = {
        "viseme_shape_keys": [f"viseme_{c}" for c in "ABCDEFGHX"],
        "animation_clips": [{"name": "idle_x"}, {"name": "move"}, {"name": "think"}],
    }
    _verify_build_output(glb, build_json, has_face=True)  # must not raise


# --- compile_and_publish: immediate publish (user present) -------------------


@pytest.mark.asyncio
async def test_publish_writes_manifest_and_glb_atomically(tmp_path):
    bin_path = _write_fake_blender(tmp_path, _FAKE_BLENDER_OK)
    settings = _settings(tmp_path, bin_path)

    result = await compile_and_publish(settings, _look_spec(), is_user_present=lambda: True)

    assert result.outcome == "published"
    assert result.version == 1
    assert result.glb_path.is_file()
    assert result.glb_path.read_bytes()[:4] == b"glTF"

    manifest = load_manifest(settings.deploy_dir)
    assert manifest is not None
    assert manifest.current_version == 1
    assert manifest.get_version(1).look_spec["base_template"] == "primitive_blob"

    assert not any(p.suffix == ".tmp" for p in settings.assets_dir.iterdir())
    assert not any(p.suffix == ".tmp" for p in settings.current_dir.iterdir())
    assert not any(p.suffix == ".tmp" for p in settings.deploy_dir.glob("*.tmp"))


@pytest.mark.asyncio
async def test_publish_prunes_old_versions_beyond_keep_recent(tmp_path):
    bin_path = _write_fake_blender(tmp_path, _FAKE_BLENDER_OK)
    settings = _settings(tmp_path, bin_path)  # keep_recent_versions=2

    for _ in range(3):
        result = await compile_and_publish(settings, _look_spec(), is_user_present=lambda: True)

    assert result.version == 3
    remaining = {p.name for p in settings.assets_dir.glob("look-v*.glb")}
    assert remaining == {"look-v2.glb", "look-v3.glb"}


@pytest.mark.asyncio
async def test_publish_fails_cleanly_on_build_failure(tmp_path):
    bin_path = _write_fake_blender(tmp_path, _FAKE_BLENDER_FAIL)
    settings = _settings(tmp_path, bin_path)

    with pytest.raises(GenerationError, match="budget_exceeded"):
        await compile_and_publish(settings, _look_spec(), is_user_present=lambda: True)

    assert load_manifest(settings.deploy_dir) is None  # live state untouched


# --- compile_and_publish: Option B staging (user away) ------------------------


@pytest.mark.asyncio
async def test_staged_build_does_not_touch_live_manifest(tmp_path):
    bin_path = _write_fake_blender(tmp_path, _FAKE_BLENDER_OK)
    settings = _settings(tmp_path, bin_path)

    result = await compile_and_publish(settings, _look_spec(), is_user_present=lambda: False)

    assert result.outcome == "staged"
    assert result.version == 1
    assert result.glb_path.is_file()
    assert load_manifest(settings.deploy_dir) is None
    assert not (settings.current_dir / "look-v1.glb").exists()


@pytest.mark.asyncio
async def test_reveal_staged_publishes_without_recompiling(tmp_path):
    bin_path = _write_fake_blender(tmp_path, _FAKE_BLENDER_OK)
    settings = _settings(tmp_path, bin_path)

    staged = await compile_and_publish(settings, _look_spec(), is_user_present=lambda: False)
    assert staged.outcome == "staged"

    revealed = reveal_staged(settings, staged.version)

    assert revealed.outcome == "published"
    assert revealed.version == 1
    assert (settings.current_dir / "look-v1.glb").is_file()
    assert load_manifest(settings.deploy_dir).current_version == 1
    # staged scratch cleaned up once published
    assert not (settings.staged_dir / "look-v1.glb").exists()
    assert not (settings.staged_dir / "look-v1.meta.json").exists()


def test_reveal_staged_raises_if_nothing_staged(tmp_path):
    settings = _settings(tmp_path, "unused")
    settings.ensure_dirs()
    with pytest.raises(GenerationError, match="no staged build"):
        reveal_staged(settings, 1)


# --- timeout + cancellation ----------------------------------------------------


@pytest.mark.asyncio
async def test_timeout_kills_process_and_discards_scratch(tmp_path, monkeypatch):
    monkeypatch.setattr(blender_gen, "BUILD_TIMEOUT_S", 0.3)
    bin_path = _write_fake_blender(tmp_path, _FAKE_BLENDER_HANGS)
    settings = _settings(tmp_path, bin_path)

    with pytest.raises(BuildTimeoutError):
        await compile_and_publish(settings, _look_spec(), is_user_present=lambda: True)

    assert list(settings.workspace_dir.glob("blender_build/*")) == []


@pytest.mark.asyncio
async def test_cancel_running_build_raises_build_cancelled(tmp_path):
    import asyncio

    bin_path = _write_fake_blender(tmp_path, _FAKE_BLENDER_HANGS)
    settings = _settings(tmp_path, bin_path)

    task = asyncio.create_task(compile_and_publish(settings, _look_spec(), is_user_present=lambda: True))
    for _ in range(50):
        if blender_gen._current_proc is not None:
            break
        await asyncio.sleep(0.05)
    assert cancel_running_build() is True

    with pytest.raises(BuildCancelledError):
        await task


def test_cancel_running_build_returns_false_when_idle():
    assert cancel_running_build() is False


# --- EXDEV surfacing (same guarantee as test_manifest.py, exercised through
# the actual publish path this time) -------------------------------------------


@pytest.mark.asyncio
async def test_publish_surfaces_exdev_rather_than_swallowing_it(tmp_path, monkeypatch):
    bin_path = _write_fake_blender(tmp_path, _FAKE_BLENDER_OK)
    settings = _settings(tmp_path, bin_path)

    real_replace = os.replace

    def flaky_replace(src, dst):
        if str(dst).endswith(".glb"):
            raise OSError(errno.EXDEV, "Invalid cross-device link")
        real_replace(src, dst)

    monkeypatch.setattr(os, "replace", flaky_replace)
    with pytest.raises(OSError) as exc_info:
        await compile_and_publish(settings, _look_spec(), is_user_present=lambda: True)
    assert exc_info.value.errno == errno.EXDEV
