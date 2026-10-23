"""AssetManifest: the versioned deployment record wrapping blender/generator.py's
per-build `<out>.build.json` ground truth into `<deploy_dir>/manifest.json`.
Each `versions[]` entry mirrors `<out>.build.json` verbatim plus
`version: N` and the full serialized `look_spec` -- the latter is what
makes a user revert ("go back to your old look") free: zero Blender
recompilation, because recent `.glb` files and LookSpecs are retained, not
re-derived.

This module is pure (Pydantic + plain file I/O only, no `bpy`) and is fully
unit-testable without Blender -- unlike blender/generator.py or
blender_gen.py's subprocess orchestration.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

MANIFEST_FILENAME = "manifest.json"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AnimationClipEntry(_Strict):
    name: str
    loop: bool
    duration_s: float


class AssetVersionEntry(_Strict):
    """Mirrors <out>.build.json exactly, plus `version` and `look_spec`."""

    version: int
    glb_filename: str
    sha256: str
    created_at: float
    status: Literal["ok", "fail", "corrupted"]
    seed: int
    evaluated_triangles: int
    size_bytes: int
    has_face: bool
    speech_indicator_mode: Literal["viseme_shapekeys", "talk_pulse"]
    capabilities: dict[str, bool]
    viseme_shape_keys: list[str]
    animation_clips: list[AnimationClipEntry]
    bounds: dict[str, float]
    warnings: list[str] = []
    look_spec: dict


class AssetManifest(_Strict):
    current_version: int
    versions: list[AssetVersionEntry]

    def get_version(self, version: int) -> AssetVersionEntry | None:
        return next((v for v in self.versions if v.version == version), None)

    def current(self) -> AssetVersionEntry | None:
        return self.get_version(self.current_version)

    def latest_version_number(self) -> int:
        return max((v.version for v in self.versions), default=0)


def atomic_write(target_path: Path, data: bytes | str) -> None:
    """Same-directory `.tmp` + fsync + os.replace -- an EXDEV-safety
    primitive. Writing the temp file inside target_path's own directory
    (rather than e.g. a shared system tmp dir) is the actual guarantee:
    os.replace is only atomic within a single filesystem, and a temp file
    elsewhere could land on a different one."""
    temp_path = target_path.with_suffix(f"{target_path.suffix}.tmp")
    mode = "wb" if isinstance(data, bytes) else "w"
    encoding = None if isinstance(data, bytes) else "utf-8"
    with open(temp_path, mode, encoding=encoding) as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(temp_path, target_path)


def manifest_path(deploy_dir: Path) -> Path:
    return deploy_dir / MANIFEST_FILENAME


def load_manifest(deploy_dir: Path) -> AssetManifest | None:
    path = manifest_path(deploy_dir)
    if not path.is_file():
        return None
    return AssetManifest.model_validate_json(path.read_text())


def save_manifest(deploy_dir: Path, manifest: AssetManifest) -> None:
    atomic_write(manifest_path(deploy_dir), manifest.model_dump_json(indent=2))


def glb_filename_for(version: int) -> str:
    """The one place the on-disk, versioned asset name is defined. The
    generator's own build.json carries only its scratch output name (always
    the same, e.g. "look.glb"), so the manifest -- which owns versioning --
    must assign the real filename, or it would point at a file that never
    exists."""
    return f"look-v{version}.glb"


def append_version(
    manifest: AssetManifest | None, build_json: dict, look_spec: dict
) -> AssetManifest:
    """Builds the next AssetManifest from a prior one (or None, for a fresh
    install) plus a successful <out>.build.json and the LookSpec that
    produced it. Pure -- does not touch disk; call save_manifest() to commit."""
    next_version = (manifest.latest_version_number() if manifest else 0) + 1
    entry = AssetVersionEntry(
        **{**build_json, "glb_filename": glb_filename_for(next_version)}, version=next_version, look_spec=look_spec
    )
    versions = [*manifest.versions, entry] if manifest else [entry]
    return AssetManifest(current_version=next_version, versions=versions)


def revert_to_version(manifest: AssetManifest, target_version: int) -> AssetManifest:
    """Fast user undo and the automatic rollback-on-swap-failure path -- a
    pure pointer rewrite, no Blender recompilation, since the target
    version's .glb and LookSpec are already retained in `versions[]`."""
    if manifest.get_version(target_version) is None:
        raise ValueError(f"version {target_version} is not present in this manifest")
    return manifest.model_copy(update={"current_version": target_version})


def mark_version_status(manifest: AssetManifest, version: int, status: Literal["ok", "fail", "corrupted"]) -> AssetManifest:
    """Used by the swap-failure rollback path to mark a version `corrupted`
    after reverting `current_version` away from it."""
    if manifest.get_version(version) is None:
        raise ValueError(f"version {version} is not present in this manifest")
    updated_versions = [
        v.model_copy(update={"status": status}) if v.version == version else v for v in manifest.versions
    ]
    return manifest.model_copy(update={"versions": updated_versions})
