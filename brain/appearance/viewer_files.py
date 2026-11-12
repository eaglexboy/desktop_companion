"""The files the Build page's 3D viewer may be given, and what it may play
(docs/ADR_BUILD_YOUR_LOOK.md, decision 4).

The browser names a body, an outfit or a library by id; this module is the only
thing that turns an id into a file, and only for models that are installed and
not held back. It never accepts a path, so nothing outside the model folders
can be reached. Everything here is read-only.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from blender import pack_profiles
from blender.animation_library import (
    library_contents,
    library_folder,
    library_time_scale,
)
from brain.appearance import models as core
from brain.appearance.model_matrix import animation_gaps, animation_support
from brain.appearance.spec import (
    available_custom_templates,
    load_templates_catalog,
)


def _usable_body(deploy_dir: Path, body: str) -> bool:
    """A body a look may use: a catalog template, or a `custom:` model that is not held."""
    templates = load_templates_catalog(deploy_dir / "assets" / "catalogs")[
        "templates"
    ]
    return body in templates or body in available_custom_templates(deploy_dir)


def body_file(deploy_dir: Path, body: str) -> Path | None:
    """The `.glb` of an installed, usable body, or None (a procedural body has no
    file, and a `.blend` cannot be shown in a browser)."""
    if not _usable_body(deploy_dir, body):
        return None
    model = core._model_file(deploy_dir, body)
    return model if model is not None and model.suffix == ".glb" else None


def outfit_file(deploy_dir: Path, body: str, outfit: str) -> Path | None:
    """An outfit model made for `body`, by the name `discover_outfit_models` lists."""
    if body_file(deploy_dir, body) is None:
        return None
    if outfit not in core.discover_outfit_models(deploy_dir, body):
        return None
    path = core.outfit_model_path(deploy_dir, body, outfit)
    return path if path.is_file() else None


def accessory_file(deploy_dir: Path, socket: str, item: str) -> Path | None:
    """An accessory model file, by the socket and item `discover_accessory_models` lists."""
    for accessory in core.discover_accessory_models(deploy_dir):
        if accessory.socket == socket and accessory.item == item:
            return accessory.path if accessory.path.is_file() else None
    return None


def _fitting_libraries(
    deploy_dir: Path, body: str
) -> dict[str, dict[str, str]]:
    support = animation_support(deploy_dir, body) or {}
    return {name: roles for name, roles in support.items() if name and roles}


def library_file(deploy_dir: Path, body: str, library: str) -> Path | None:
    """The library file `body` plays: a library that fits its rig, from the folder
    the build uses (the shared one, or the one retargeted onto a converted body).
    """
    if body_file(deploy_dir, body) is None:
        return None
    if library not in _fitting_libraries(deploy_dir, body):
        return None
    folder = library_folder(deploy_dir, core._model_file(deploy_dir, body))
    path = folder / f"{library}.glb"
    return path if path.is_file() else None


def _ingredients(entry: dict[str, Any]) -> set[str]:
    """The clips a derived clip is made of: what it `requires` and what it composes."""
    names = set(entry.get("requires", []))
    for part in (entry.get("options") or {}).get("compose", []):
        names.add(part[0] if isinstance(part, list) else part)
    return {n for n in names if isinstance(n, str)}


@core._with_profiles
def body_animations(deploy_dir: Path, body: str) -> dict[str, Any]:
    """What the viewer can play for `body`: {"model": bool, "libraries": [{name,
    gaps, clips: [{name, roles, part_of, speed}]}]}. Only libraries that fit the rig are
    listed; a clip carries the roles it fills for this body (empty for the many
    that fill none) and the speed it is played at (a role's `time_scale` slows a
    pack's idle, as the build does), and `gaps` are the bones the body lacks,
    whose tracks the build drops. A clip that no role names can still be an ingredient
    of a derived clip that fills one (the burpee a workout is made of): `part_of` lists
    those ({clip, roles}), so the page does not call it unused."""
    model = body_file(deploy_dir, body) is not None
    libraries: list[dict[str, Any]] = []
    if model:
        gaps = animation_gaps(deploy_dir, body)
        folder = library_folder(deploy_dir, core._model_file(deploy_dir, body))
        for name, roles in sorted(_fitting_libraries(deploy_dir, body).items()):
            path = folder / f"{name}.glb"
            try:
                _joints, clip_names = library_contents(path)
            except (OSError, ValueError):
                continue
            slower = library_time_scale()
            derived = {
                entry["name"]: _ingredients(entry)
                for entry in pack_profiles.merged().derived
            }
            # a role's variants (`talk.alt1`, `greet.low`) are the same role to the reader
            by_clip: dict[str, set[str]] = {}
            for role, clip in roles.items():
                by_clip.setdefault(clip, set()).add(role.split(".")[0])
            libraries.append(
                {
                    "name": name,
                    "gaps": gaps.get(name, []),
                    "clips": [
                        {
                            "name": clip,
                            "roles": sorted(by_clip.get(clip, set())),
                            "part_of": [
                                {"clip": made, "roles": sorted(by_clip[made])}
                                for made, parts in sorted(derived.items())
                                if clip in parts and made in by_clip
                            ],
                            "speed": min(
                                (
                                    1.0 / slower.get(role, 1.0)
                                    for role in by_clip.get(clip, set())
                                    if slower.get(role, 1.0) > 0
                                ),
                                default=1.0,
                            ),
                        }
                        for clip in sorted(c for c in clip_names if c)
                    ],
                }
            )
    return {"model": model, "libraries": libraries}
