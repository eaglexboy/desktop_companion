"""One PNG per look version, kept forever under `<deploy_dir>/looks/`, so the
status page's Looks tab can show every look the companion has ever had even
though only the newest few .glb files are retained (see
`appearance.keep_recent_versions`). A look whose file was pruned is rebuilt
from the LookSpec the manifest keeps for every version, rendered, and the
rebuilt file thrown away -- nothing here touches the manifest or the live
assets.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import time
from pathlib import Path

from brain.appearance import blender_gen
from brain.appearance.manifest import AssetManifest, load_manifest
from brain.appearance.spec import LookSpec
from brain.config import Settings

logger = logging.getLogger("deskmate.appearance")

THUMBNAIL_SCRIPT = Path(__file__).resolve().parents[2] / "blender" / "thumbnail.py"
THUMBNAIL_SIZE = 320
RENDER_TIMEOUT_S = 60.0


def looks_dir(settings: Settings) -> Path:
    return settings.deploy_dir / "looks"


def thumbnail_path(settings: Settings, version: int) -> Path:
    return looks_dir(settings) / f"look-v{version}.png"


async def render_thumbnail(settings: Settings, glb: Path, version: int) -> Path | None:
    """Renders `glb` to the version's PNG. Best-effort: a failure is logged and
    returns None, because a missing picture must never fail a look change."""
    target = thumbnail_path(settings, version)
    target.parent.mkdir(parents=True, exist_ok=True)
    scratch = target.with_suffix(".tmp.png")
    args = [
        settings.appearance.blender_bin,
        "--background",
        "--factory-startup",
        "--python",
        str(THUMBNAIL_SCRIPT),
        "--",
        str(glb),
        str(scratch),
        str(THUMBNAIL_SIZE),
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=blender_gen._clean_blender_env(),
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=RENDER_TIMEOUT_S)
    except (TimeoutError, OSError) as exc:
        logger.warning("thumbnail render for v%d failed: %s", version, exc)
        scratch.unlink(missing_ok=True)
        return None
    if proc.returncode != 0 or b"THUMB_OK" not in out or not scratch.is_file():
        logger.warning("thumbnail render for v%d failed (exit %s)", version, proc.returncode)
        scratch.unlink(missing_ok=True)
        return None
    scratch.replace(target)
    return target


async def _rebuild_glb(settings: Settings, look_spec: dict, build_dir: Path) -> Path | None:
    """Compiles a stored LookSpec to a scratch .glb (same generator as a real
    build, but never published)."""
    build_dir.mkdir(parents=True, exist_ok=True)
    spec_path = build_dir / "spec.json"
    spec_path.write_text(LookSpec.model_validate(look_spec).model_dump_json())
    out_glb = build_dir / "look.glb"
    await blender_gen._run_blender(settings, spec_path, out_glb, build_dir)
    return out_glb if out_glb.is_file() else None


async def ensure_thumbnails(settings: Settings, manifest: AssetManifest | None = None) -> int:
    """Renders every version that has no picture yet, newest first. Idempotent
    and safe to re-run; returns how many were rendered. Takes the build lock
    per version, so a look change the user asks for in the meantime waits at
    most one render."""
    manifest = manifest or load_manifest(settings.deploy_dir)
    if manifest is None:
        return 0
    missing = sorted(
        (v for v in manifest.versions if not thumbnail_path(settings, v.version).is_file()),
        key=lambda v: v.version,
        reverse=True,
    )
    rendered = 0
    for entry in missing:
        existing = settings.assets_dir / entry.glb_filename
        try:
            if existing.is_file():
                if await render_thumbnail(settings, existing, entry.version):
                    rendered += 1
                continue
            async with blender_gen._build_lock:
                build_dir = settings.blender_build_dir / f"thumb-{entry.version}-{int(time.time() * 1000)}"
                try:
                    glb = await _rebuild_glb(settings, entry.look_spec, build_dir)
                    if glb is not None and await render_thumbnail(settings, glb, entry.version):
                        rendered += 1
                finally:
                    shutil.rmtree(build_dir, ignore_errors=True)
        except Exception:
            logger.exception("could not render a thumbnail for look v%d", entry.version)
    if rendered:
        logger.info("rendered %d look thumbnail(s)", rendered)
    return rendered
