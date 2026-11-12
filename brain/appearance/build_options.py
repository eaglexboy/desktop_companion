"""What the Build page may offer for each body (docs/ADR_BUILD_YOUR_LOOK.md).

A read-only view of the deploy directory: for every body that can be used in a
look, which controls apply and what each one offers, with the reason when a
control does not apply. It reads the same sources the designer and
`validate_against_catalogs` use (`effective_wardrobe_catalog`, the body's
capabilities, the pack profiles), so the page can never offer what validation
would reject. It writes nothing and never runs Blender.
"""

from __future__ import annotations

import json
import re
import struct
from pathlib import Path
from typing import Any, get_args

from brain.appearance import models as core
from brain.appearance.model_matrix import animation_matrix
from brain.appearance.spec import (
    SurfaceMaterial,
    available_custom_templates,
    load_templates_catalog,
)

_SOCKET_ROLE = {
    "socket_head_top": "head",
    "socket_eyes": "head",
    "socket_neck": "neck",
    "socket_chest": "chest",
    "socket_waist": "waist",
    "socket_hand_R": "right hand",
    "socket_hand_L": "left hand",
    "socket_orbit": "body",
}

# The materials of a body's own hair, eyebrows and eyelashes: the ones the build can tint.
# Mirrors blender/gen_imported.py `HAIR_MATERIAL` (a test pins them together).
HAIR_MATERIAL = re.compile(r"hair|eyebrow|eyelash", re.IGNORECASE)

_PROPORTIONS_REASON = "Proportions apply to procedural bodies only; an installed body keeps its own shape."
_FOOTWEAR_REASON = "An installed body keeps its own feet; an outfit model made for it brings its own footwear."
_TEXTURED_NOTE = "This model is textured and keeps its own colors."

# Which palette entries each part of a look uses. Mirrors blender/gen_wardrobe.py (accessories),
# gen_garments.py (outfit, footwear) and gen_body.py (the built-in bodies); a test pins the shape.
_ACCESSORY_COLORS: dict[str, list[str]] = {
    "socket_head_top": ["accent", "emissive"],
    "socket_eyes": ["accent", "emissive"],
    "socket_neck": ["accent"],
    "socket_chest": ["accent"],
    "socket_waist": ["accent"],
    "socket_hand_R": ["secondary", "accent", "emissive"],
    "socket_hand_L": ["secondary", "accent", "emissive"],
    "socket_orbit": ["accent", "emissive"],
}
_OUTFIT_COLORS = ["secondary"]
_FOOTWEAR_COLORS = ["secondary", "accent"]
_COLOR_REASONS = {
    "installed": "This model keeps its own colors.",
    "unused": "Nothing you have chosen uses this color. It colors the outfit, footwear and accessories.",
    "hair": "This body has no hair, eyebrows or eyelashes of its own to color.",
    "hairstyle": "Choose a hair or beard model to color it.",
    "surface": "Only built-in bodies and their outfits have a surface; an installed model keeps its own.",
}


def _has_hair(model: Path | None) -> bool:
    """Whether a body's model file has a hair, eyebrow or eyelash material. Reads only the
    file's JSON header; a file that is not a readable .glb has none."""
    if model is None or model.suffix.lower() != ".glb":
        return False
    try:
        with model.open("rb") as f:
            head = f.read(20)
            length = struct.unpack("<I", head[12:16])[0]
            names = json.loads(f.read(length)).get("materials", [])
    except (OSError, ValueError, struct.error):
        return False
    return any(HAIR_MATERIAL.search(str(m.get("name", ""))) for m in names)


def _control(choices: list[str], reason: str | None = None) -> dict[str, Any]:
    """One control: usable when it has something to choose and no reason against."""
    usable = bool(choices) and reason is None
    return {
        "available": usable,
        "choices": choices if usable else [],
        "reason": None if usable else reason,
    }


def _kind(body: str, installed: bool, converted: bool) -> str:
    """How well the body is supported (ADR_ACCESSORY_MODELS 15). Only the
    standard humanoids are known to be Quaternius, so only they say so."""
    if not installed:
        return "procedural"
    if body in core.STANDARD_HUMANOIDS:
        return "quaternius"
    return "converted" if converted else "installed"


def _order(body: str) -> tuple[int, str]:
    """The built-in primitives, then the standard humanoids, then installed models, each
    alphabetical."""
    if body.startswith("primitive_"):
        return (0, body)
    if body in core.STANDARD_HUMANOIDS:
        return (1, body)
    return (2, body)


def _accessory_controls(
    effective: dict[str, Any],
    sockets: frozenset[str] | None,
    placements: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for socket in core.SOCKETS:
        choices = list(effective["accessories"].get(socket, []))
        reason = None
        if not choices:
            if sockets is not None and socket not in sockets:
                role = _SOCKET_ROLE.get(socket, "body")
                reason = f"This body's rig has no {role} to carry it."
            else:
                reason = "No accessory is installed for this socket."
        result[socket] = _control(choices, reason)
        # the items that are model files, with how they are placed: the page can draw these
        # itself; a parametric item exists only inside Blender
        result[socket]["models"] = {
            item: placements[socket][item]
            for item in choices
            if item in placements.get(socket, {})
        }
    return result


def _colors(
    installed: bool,
    models: dict[str, list[str]],
    hair: bool,
    hair_models: dict[str, list[str]],
) -> dict[str, Any]:
    """Which palette entries and surface apply to a body and what it can wear. An
    installed model keeps its own colors, so only the parametric accessories take the
    palette; a built-in body is colored by `primary` and has a surface. The page adds up
    what the current choices use (accessories that are model files use their own colors).
    """
    return {
        "body": ["hair"] if hair else ([] if installed else ["primary"]),
        "surface": not installed,
        "outfit": [] if installed else _OUTFIT_COLORS,
        "footwear": [] if installed else _FOOTWEAR_COLORS,
        "accessories": _ACCESSORY_COLORS,
        "models": models,
        # the model files that are hair or a beard, which `hairstyle` colors
        "hair_models": hair_models,
        "reasons": _COLOR_REASONS,
    }


def build_options(deploy_dir: Path) -> dict[str, Any]:
    """{"materials": [...], "bodies": [{id, kind, held: False, credit, has_face,
    note, controls: {outfit, footwear, proportions, accessories}, animations}]}.

    Held bodies are left out: validation refuses them. `animations` is the
    body's per-library fit (as /api/animations), `None` when its rig was never
    inspected."""
    catalogs = deploy_dir / "assets" / "catalogs"
    templates = load_templates_catalog(catalogs)["templates"]
    matrix = {
        entry["body"]: entry["libraries"]
        for entry in animation_matrix(deploy_dir)["bodies"]
    }
    ids = [*sorted(templates), *available_custom_templates(deploy_dir)]
    model_items: dict[str, list[str]] = {}
    placements: dict[str, dict[str, Any]] = {}
    hair_models: dict[str, list[str]] = {}
    for accessory in core.discover_accessory_models(deploy_dir):
        model_items.setdefault(accessory.socket, []).append(accessory.item)
        if _has_hair(accessory.path):
            hair_models.setdefault(accessory.socket, []).append(accessory.item)
        placements.setdefault(accessory.socket, {})[accessory.item] = {
            "offset": list(accessory.offset),
            "scale": accessory.scale,
            "rotation": list(accessory.rotation),
            "skin": accessory.skin,
        }
    bodies: list[dict[str, Any]] = []
    for body in ids:
        profile = core.body_profile(deploy_dir, body, templates)
        model = core._model_file(deploy_dir, body)
        installed = profile.imported
        converted = (
            model is not None
            and model.with_name(f"{model.stem}.rig.json").is_file()
        )
        effective = core.effective_wardrobe_catalog(deploy_dir, body)

        outfit_reason = None
        if installed and effective["outfits"] == ["none"]:
            outfit_reason = "This body keeps its own clothes; no outfit model made for it is installed."
        proportions = (
            []
            if installed
            else list(templates.get(body, {}).get("supported_proportions", []))
        )
        if installed:
            has_face = bool((profile.capabilities or {}).get("has_face", False))
        else:
            has_face = bool(templates.get(body, {}).get("has_face", False))

        bodies.append(
            {
                "id": body,
                "kind": _kind(body, installed, converted),
                "held": False,
                "credit": core.credit_line(core.read_credits(model)),
                "has_face": has_face,
                "note": _TEXTURED_NOTE if installed else None,
                "colors": _colors(installed, model_items, _has_hair(model), hair_models),
                "controls": {
                    "outfit": _control(effective["outfits"], outfit_reason),
                    "footwear": _control(
                        effective["footwear"],
                        _FOOTWEAR_REASON if installed else None,
                    ),
                    "proportions": _control(
                        proportions, _PROPORTIONS_REASON if installed else None
                    ),
                    "accessories": _accessory_controls(
                        effective, profile.sockets, placements
                    ),
                },
                "animations": matrix.get(body),
            }
        )
    bodies.sort(key=lambda b: _order(b["id"]))
    return {"materials": list(get_args(SurfaceMaterial)), "bodies": bodies}
