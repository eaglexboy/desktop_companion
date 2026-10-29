"""Orchestrates one appearance-generation run end to end: writes a
validated LookSpec to scratch, runs blender/generator.py headless against
it, verifies the ground-truth `<out>.build.json`, then either publishes it
live (atomic same-filesystem commit into `<deploy_dir>/assets/` +
`data/current/` + `manifest.json`) or holds it in
`<deploy_dir>/assets/staged/` for a later `reveal_staged()` call, depending
on whether the user is present right now (staged reveal).

All the actual mesh/rig/shape-key/animation work happens in
blender/generator.py, run as a separate `blender --background` subprocess --
never imported in-process (`bpy` only exists inside Blender's own Python).

Writing `agent_state.staged_version` / clearing it on reveal is
deliberately NOT done here -- that's SQLite state owned by
brain/personality/state.py; wiring the two together is glue code that
lives elsewhere. Callers are responsible for persisting the
`CompileResult.version` this module returns when staging, and for passing
it back into `reveal_staged()` later.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from blender.viseme_shapekeys import VISEME_CODES
from brain.appearance.manifest import (
    AssetManifest,
    append_version,
    atomic_write,
    glb_filename_for,
    load_manifest,
    save_manifest,
)
from brain.appearance.spec import LookSpec
from brain.config import Settings

logger = logging.getLogger("deskmate.appearance")

GENERATOR_SCRIPT = Path(__file__).resolve().parents[2] / "blender" / "generator.py"
BUILD_TIMEOUT_S = 120.0
GLTF_MAGIC = b"glTF"

_build_lock = asyncio.Lock()
_current_proc: asyncio.subprocess.Process | None = None


class GenerationError(RuntimeError):
    """Raised for any failure short of a timeout: non-zero exit, a
    DESKMATE_BUILD_FAIL line, a missing/invalid build.json, or output that
    fails ground-truth verification."""


class BuildTimeoutError(GenerationError):
    """The 120s subprocess timeout expired; the process was SIGKILLed and
    scratch artifacts discarded."""


class BuildCancelledError(GenerationError):
    """cancel_running_build() SIGTERMed an in-flight autonomous build to
    make way for a higher-priority user request."""


@dataclass
class CompileResult:
    outcome: Literal["published", "staged"]
    version: int
    glb_path: Path
    manifest: AssetManifest | None  # None when staged -- manifest.json is untouched


def _clean_blender_env() -> dict[str, str]:
    """Blender's own glTF export add-on shells out to a `python3` it finds
    on PATH for an internal step, and picks up whatever venv the *brain*
    daemon happens to be running in if that venv's bin/ is ahead on PATH --
    which lacks numpy and makes the export silently fail with exit code 0.
    So: always launch Blender with a venv-free PATH/env, independent of
    however this process itself was started. This fix is load-bearing --
    do not remove it without reproducing that failure mode first.
    """
    env = os.environ.copy()
    venv = env.pop("VIRTUAL_ENV", None)
    env.pop("PYTHONHOME", None)
    env.pop("PYTHONPATH", None)
    if venv:
        parts = [p for p in env.get("PATH", "").split(os.pathsep) if p and not p.startswith(venv)]
        env["PATH"] = os.pathsep.join(parts)
    return env


def _discard_scratch(build_dir: Path) -> None:
    shutil.rmtree(build_dir, ignore_errors=True)


async def _run_blender(settings: Settings, spec_path: Path, out_glb: Path, build_dir: Path) -> dict:
    """Runs blender headless, enforces the 120s timeout (SIGKILL + scratch
    discard on expiry), and parses the DESKMATE_BUILD_OK/FAIL stdout
    contract. Returns the parsed build.json dict on success."""
    global _current_proc
    args = [
        settings.appearance.blender_bin,
        "--background",
        "--factory-startup",
        "--python",
        str(GENERATOR_SCRIPT),
        "--",
        "--spec",
        str(spec_path),
        "--out",
        str(out_glb),
        "--deploy-dir",
        str(settings.deploy_dir),
    ]
    logger.info("running blender headless: %s", " ".join(args))
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=_clean_blender_env(),
    )
    _current_proc = proc
    try:
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=BUILD_TIMEOUT_S)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            _discard_scratch(build_dir)
            raise BuildTimeoutError(f"blender build exceeded {BUILD_TIMEOUT_S}s timeout, process killed") from None
    finally:
        _current_proc = None

    stdout_text = stdout.decode(errors="replace")
    if proc.returncode == -15:  # SIGTERM, from cancel_running_build()
        raise BuildCancelledError("build cancelled (SIGTERM) to prioritize a higher-priority request")
    if "DESKMATE_BUILD_OK" not in stdout_text:
        last_line = next((line for line in reversed(stdout_text.strip().splitlines()) if line.strip()), "")
        reason = last_line or stderr.decode(errors="replace").strip() or f"exit code {proc.returncode}"
        raise GenerationError(f"blender build failed: {reason}")

    build_json_path = out_glb.with_suffix(".build.json")
    if not build_json_path.is_file():
        raise GenerationError(f"blender exited OK but {build_json_path.name} is missing")
    build_json = json.loads(build_json_path.read_text())
    if build_json.get("status") != "ok":
        raise GenerationError(f"build.json status != 'ok': {build_json}")
    return build_json


def _verify_build_output(out_glb: Path, build_json: dict, has_face: bool) -> None:
    """Ground-truth verification: glTF binary magic bytes, all 9 canonical
    viseme shape keys if has_face, and the core clips (idle_*, move, think)."""
    if not out_glb.is_file():
        raise GenerationError(f"{out_glb} was not produced despite a DESKMATE_BUILD_OK exit")
    magic = out_glb.read_bytes()[:4]
    if magic != GLTF_MAGIC:
        raise GenerationError(f"{out_glb} does not start with glTF binary magic bytes (got {magic!r})")

    if has_face:
        expected = {f"viseme_{code}" for code in VISEME_CODES}
        got = set(build_json.get("viseme_shape_keys", []))
        missing = expected - got
        if missing:
            raise GenerationError(f"build.json is missing viseme shape keys: {sorted(missing)}")

    clip_names = {clip["name"] for clip in build_json.get("animation_clips", [])}
    if not any(name.startswith("idle_") for name in clip_names):
        raise GenerationError(f"build.json has no idle_* clips: {sorted(clip_names)}")
    for required in ("move", "think"):
        if required not in clip_names:
            raise GenerationError(f"build.json is missing required core clip {required!r}: {sorted(clip_names)}")


_glb_filename = glb_filename_for


def _publish_version(
    settings: Settings, out_glb: Path, build_json: dict, look_spec_dict: dict, existing_manifest: AssetManifest | None
) -> CompileResult:
    """Same-filesystem atomic commit: copy into assets/, copy into
    data/current/, then the manifest commit point -- in that order, so a
    crash between any two steps never leaves manifest.json pointing at a
    file that doesn't exist yet."""
    data = out_glb.read_bytes()
    manifest = append_version(existing_manifest, build_json, look_spec_dict)
    version = manifest.current_version
    filename = _glb_filename(version)

    atomic_write(settings.assets_dir / filename, data)
    atomic_write(settings.current_dir / filename, data)
    save_manifest(settings.deploy_dir, manifest)

    _prune_old_versions(settings, manifest)
    return CompileResult(outcome="published", version=version, glb_path=settings.current_dir / filename, manifest=manifest)


def _prune_old_versions(settings: Settings, manifest: AssetManifest) -> None:
    """Keeps only `appearance.keep_recent_versions` most recent .glb files in
    both assets/ and data/current/. Prunes immediately after a successful
    local publish, rather than waiting for the presentation client to
    confirm it actually swapped to the new asset first -- a simpler policy
    than gating pruning on that round-trip."""
    keep = settings.appearance.keep_recent_versions
    sorted_versions = sorted((v.version for v in manifest.versions), reverse=True)
    for old_version in sorted_versions[keep:]:
        for directory in (settings.assets_dir, settings.current_dir):
            (directory / _glb_filename(old_version)).unlink(missing_ok=True)


def _stage_version(settings: Settings, out_glb: Path, build_json: dict, look_spec_dict: dict, version: int) -> CompileResult:
    """Option B: the live manifest/data/current are untouched; the compiled
    asset and the metadata needed to publish it later without recompiling
    are held under assets/staged/ instead."""
    settings.staged_dir.mkdir(parents=True, exist_ok=True)
    filename = _glb_filename(version)
    atomic_write(settings.staged_dir / filename, out_glb.read_bytes())
    meta = {"build_json": build_json, "look_spec": look_spec_dict}
    atomic_write(settings.staged_dir / f"look-v{version}.meta.json", json.dumps(meta))
    return CompileResult(outcome="staged", version=version, glb_path=settings.staged_dir / filename, manifest=None)


async def compile_and_publish(
    settings: Settings,
    look_spec: LookSpec,
    *,
    is_user_present: Callable[[], bool],
    on_status: Callable[[str], None] | None = None,
) -> CompileResult:
    """Compiles `look_spec` to scratch, verifies it, then either publishes it
    live or stages it, depending on `is_user_present()` evaluated once the
    build is ready to commit (not before compiling -- presence may change
    during the ~seconds-to-tens-of-seconds build). Only one compile runs at a
    time (`_build_lock`); a second caller blocks until the first finishes
    (or is cancelled via cancel_running_build())."""
    async with _build_lock:
        if on_status:
            on_status("rebuilding")
        settings.ensure_dirs()
        build_dir = settings.blender_build_dir / f"look-{int(time.time() * 1000)}"
        build_dir.mkdir(parents=True, exist_ok=True)
        spec_path = build_dir / "spec.json"
        look_spec_dict = look_spec.model_dump(mode="json")
        spec_path.write_text(json.dumps(look_spec_dict))
        out_glb = build_dir / "look.glb"

        try:
            build_json = await _run_blender(settings, spec_path, out_glb, build_dir)
            # the build's own has_face is authoritative: an imported model without
            # visemes is built faceless even if the spec asked for a face
            _verify_build_output(out_glb, build_json, build_json.get("has_face", look_spec.has_face))

            existing_manifest = load_manifest(settings.deploy_dir)
            if is_user_present():
                result = _publish_version(settings, out_glb, build_json, look_spec_dict, existing_manifest)
            else:
                next_version = (existing_manifest.latest_version_number() if existing_manifest else 0) + 1
                result = _stage_version(settings, out_glb, build_json, look_spec_dict, next_version)
        finally:
            _discard_scratch(build_dir)
            if on_status:
                on_status("idle")

        return result


def reveal_staged(settings: Settings, version: int) -> CompileResult:
    """The presence-transition hook (wired to Away|Idle -> Active edges,
    never on boot). Publishes an already-compiled staged version with zero
    recompilation, using the sidecar metadata compile_and_publish() wrote
    alongside it. Caller is responsible for reading
    `agent_state.staged_version` beforehand and clearing it after this
    returns successfully."""
    filename = _glb_filename(version)
    staged_glb = settings.staged_dir / filename
    meta_path = settings.staged_dir / f"look-v{version}.meta.json"
    if not staged_glb.is_file() or not meta_path.is_file():
        raise GenerationError(f"no staged build found for version {version} under {settings.staged_dir}")

    meta = json.loads(meta_path.read_text())
    existing_manifest = load_manifest(settings.deploy_dir)
    result = _publish_version(settings, staged_glb, meta["build_json"], meta["look_spec"], existing_manifest)
    staged_glb.unlink(missing_ok=True)
    meta_path.unlink(missing_ok=True)
    return result


def cancel_running_build() -> bool:
    """SIGTERMs an in-flight Blender subprocess -- used when a user-requested
    redesign needs to cancel an autonomous build already in progress.
    Returns True if a build was actually running and signalled, False if
    there was nothing to cancel."""
    if _current_proc is None:
        return False
    _current_proc.terminate()
    return True


def is_build_running() -> bool:
    """`reflect.maybe_trigger_autonomous_redesign()`'s gate needs to know
    this (an autonomous redesign is skipped entirely while a build --
    autonomous or user-prompted -- is already in flight); exposes
    `_build_lock`'s state without exposing the lock itself."""
    return _build_lock.locked()
