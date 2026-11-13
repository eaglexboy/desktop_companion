"""What the Model settings page reads and writes (docs/ADR_MODEL_SETTINGS_PAGE.md).

The page names a body by id (`custom:<name>` or a standard humanoid); this module turns that id into
the files beside the installed model and nothing else, so no path ever comes from the browser. Reading
shows the body with what can be chosen for it and the values it has now; writing validates a whole
settings document against what is installed (`blender/model_config.py`) and replaces the file
atomically. Nothing here runs Blender.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from blender import model_config, pack_profiles
from blender.animation_library import library_contents, library_folder
from brain.appearance import models as core
from brain.appearance.manifest import load_manifest
from brain.appearance.model_matrix import animation_gaps, animation_support
from brain.appearance.spec import (
    AccessoriesSpec,
    LookSpec,
    PaletteSpec,
    WardrobeSpec,
    hold_reason,
)

MAX_CREDIT_FIELD = 400
CREDIT_FIELDS = ("title", "author", "source", "license")
FACE_PARTS = ("eye_l", "eye_r", "mouth")
FACE_KEYS = ("x", "z", "w", "h")
_BODY_ID = re.compile(
    r"^(custom:[A-Za-z0-9_.\- ]{1,80}|standard_humanoid_(female|male))$"
)


class SettingsError(ValueError):
    """The request cannot be done; the message says why and is shown to the user."""


# --- finding a body -----------------------------------------------------------------------------


def installed_bodies(deploy_dir: Path) -> list[str]:
    """Every body with a model file, held ones included: the standard humanoids that were installed
    and the custom ones. Procedural bodies have no file and no settings."""
    assets = deploy_dir / "assets"
    found = [
        body
        for body in core.STANDARD_HUMANOIDS
        if any(
            (assets / "base_templates" / f"{body}{e}").is_file()
            for e in (".glb", ".blend")
        )
    ]
    custom = assets / "custom_templates"
    if custom.is_dir():
        names = {
            p.stem for p in custom.iterdir() if p.suffix in (".glb", ".blend")
        }
        found += [f"custom:{name}" for name in sorted(names)]
    return found


def _model(deploy_dir: Path, body: str) -> Path:
    """The model file of `body`, or SettingsError. The id is checked before it is used."""
    if not isinstance(body, str) or not _BODY_ID.match(body):
        raise SettingsError("not a body id")
    model = core._model_file(deploy_dir, body)
    if model is None:
        raise SettingsError(f"{body} is not an installed model")
    return model


# --- what can be chosen -------------------------------------------------------------------------


def _libraries(
    deploy_dir: Path, body: str, model: Path
) -> tuple[list[dict], bool]:
    """([{name, gaps, clips: [{name, roles}]}] for the libraries that fit the body's rig, whether the
    body has a rig at all). A clip carries the roles it fills by default."""
    support = animation_support(deploy_dir, body) or {}
    fitting = {name: roles for name, roles in support.items() if name and roles}
    gaps = animation_gaps(deploy_dir, body)
    folder = library_folder(deploy_dir, model)
    out: list[dict] = []
    for name, roles in sorted(fitting.items()):
        try:
            _joints, clips = library_contents(folder / f"{name}.glb")
        except (OSError, ValueError):
            continue
        by_clip: dict[str, set[str]] = {}
        for role, clip in roles.items():
            by_clip.setdefault(clip, set()).add(role.split(".")[0])
        out.append(
            {
                "name": name,
                "gaps": gaps.get(name, []),
                "clips": [
                    {"name": c, "roles": sorted(by_clip.get(c, set()))}
                    for c in sorted(c for c in clips if c)
                ],
            }
        )
    return out, bool(out)


def _available(libraries: list[dict], catalog: dict) -> model_config.Available:
    return model_config.Available(
        libraries={
            lib["name"]: {c["name"] for c in lib["clips"]} for lib in libraries
        },
        sockets=set(catalog["accessories"]),
        items={
            socket: set(items)
            for socket, items in catalog["accessories"].items()
        },
    )


@core._with_profiles
def available_for(deploy_dir: Path, body: str) -> model_config.Available:
    model = _model(deploy_dir, body)
    libraries, _ = _libraries(deploy_dir, body, model)
    return _available(
        libraries,
        core.effective_wardrobe_catalog(deploy_dir, body, with_settings=False),
    )


# --- credits ------------------------------------------------------------------------------------


def licence_asks(license_text: str) -> list[str]:
    """What a Creative Commons licence line asks of the user, in words (an empty list when it is
    not one this recognises: the page then shows the line as it is)."""
    text = (license_text or "").upper()
    if not text.strip():
        return []
    if "CC0" in text or "PUBLIC DOMAIN" in text:
        return ["nothing: public domain"]
    asks = []
    if re.search(r"\bBY\b", text):
        asks.append("credit the author")
    if re.search(r"\bNC\b", text):
        asks.append("non-commercial use only")
    if re.search(r"\bND\b", text):
        asks.append("no derivatives (do not change the model)")
    if re.search(r"\bSA\b", text):
        asks.append("share alike")
    return asks


# --- reading ------------------------------------------------------------------------------------


def _read_json(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def list_models(deploy_dir: Path) -> list[dict[str, Any]]:
    """The installed bodies for the page's list, held ones marked."""
    out = []
    for body in installed_bodies(deploy_dir):
        model = core._model_file(deploy_dir, body)
        settings, problems = model_config.load(model) if model else ({}, [])
        name = body.removeprefix("custom:")
        held = (
            hold_reason(deploy_dir, name)
            if body.startswith("custom:")
            else None
        )
        out.append(
            {
                "id": body,
                "held": held,
                "customised": any(
                    settings.get(s)
                    for s in ("animations", "accessories", "pose")
                ),
                "problems": problems,
                "credit": core.credit_line(core.read_credits(model)),
            }
        )
    return out


@core._with_profiles
def describe(deploy_dir: Path, body: str) -> dict[str, Any]:
    """Everything the page needs for one body: its settings, the stale entries among them, what can be
    chosen, and the records beside the model."""
    model = _model(deploy_dir, body)
    settings, problems = model_config.load(model)
    libraries, has_rig = _libraries(deploy_dir, body, model)
    catalog = core.effective_wardrobe_catalog(
        deploy_dir, body, with_settings=False
    )
    available = _available(libraries, catalog)
    report = model_config.validate(settings, available)
    model_items = {
        (a.socket, a.item) for a in core.discover_accessory_models(deploy_dir)
    }
    name = body.removeprefix("custom:")
    credits = core.read_credits(model)
    manifest = load_manifest(deploy_dir)
    current = None
    if manifest is not None:
        entry = next(
            (
                v
                for v in manifest.versions
                if v.version == manifest.current_version
            ),
            None,
        )
        current = (
            (entry.look_spec or {}).get("base_template")
            if entry is not None
            else None
        )
    return {
        "id": body,
        "held": (
            hold_reason(deploy_dir, name)
            if body.startswith("custom:")
            else None
        ),
        "settings": settings,
        "problems": problems,
        "stale": report.stale,
        "choices": {
            "roles": sorted(pack_profiles.KNOWN_ROLES),
            "libraries": libraries,
            "has_rig": has_rig,
            "sockets": sorted(catalog["accessories"]),
            "items": catalog["accessories"],
            "model_items": sorted(f"{s}/{i}" for s, i in model_items),
            "limits": {
                "offset": model_config.MAX_OFFSET,
                "scale": list(model_config.SCALE_RANGE),
                "rotation": model_config.MAX_ANGLE,
                "arm_spread": model_config.MAX_ARM_SPREAD,
                "emissive_gain": model_config.MAX_EMISSIVE_GAIN,
                "face_yaw": model_config.MAX_ANGLE,
            },
        },
        "pose_file": _read_json(model_config.pose_sidecar_path(model)) or {},
        "face": _read_json(core.face_sidecar_path(model)),
        "credits": credits,
        "licence_asks": licence_asks((credits or {}).get("license", "")),
        "in_current_look": current == body,
    }


# --- writing ------------------------------------------------------------------------------------


def _write_json(path: Path, doc: dict) -> None:
    """Replace `path` with `doc` only when it is completely written."""
    fd, tmp = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(doc, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def validate(deploy_dir: Path, body: str, doc: Any) -> model_config.Report:
    """Check a document without writing it (the page's live feedback)."""
    return model_config.validate(doc, available_for(deploy_dir, body))


def save(deploy_dir: Path, body: str, doc: Any) -> model_config.Report:
    """Validate `doc` against what is installed for `body` and, if it is acceptable, write it."""
    model = _model(deploy_dir, body)
    return model_config.save(model, doc, available_for(deploy_dir, body))


def _without_stale(doc: dict, paths: list[tuple]) -> dict:
    """`doc` without the entries at `paths` (key tuples; an int is a list index). Removed from the
    deepest, highest index first, so that earlier removals do not move later ones.
    """
    out = json.loads(json.dumps(doc))
    for path in sorted(
        paths, key=lambda p: tuple(str(k).zfill(6) for k in p), reverse=True
    ):
        node: Any = out
        for key in path[:-1]:
            try:
                node = node[key]
            except (KeyError, IndexError, TypeError):
                node = None
                break
        last = path[-1]
        if isinstance(node, dict):
            node.pop(last, None)
        elif (
            isinstance(node, list)
            and isinstance(last, int)
            and last < len(node)
        ):
            del node[last]
    return out


def remove_stale(deploy_dir: Path, body: str) -> model_config.Report:
    """Drop the entries that name something no longer there (the page's "remove" for them)."""
    model = _model(deploy_dir, body)
    settings, _ = model_config.load(model)
    available = available_for(deploy_dir, body)
    stale = model_config.validate(settings, available).stale_at
    return model_config.save(model, _without_stale(settings, stale), available)


def set_availability(deploy_dir: Path, body: str, reason: str | None) -> None:
    """Hold a custom model back with a reason, or release it (reason None)."""
    _model(deploy_dir, body)
    if reason is not None:
        reason = reason.strip()
        if not reason or len(reason) > MAX_CREDIT_FIELD or "\n" in reason:
            raise SettingsError("a hold needs a short one-line reason")
    try:
        core.set_hold(deploy_dir, body, reason)
    except SystemExit as exit_:
        raise SettingsError(str(exit_)) from None


def save_credits(deploy_dir: Path, body: str, credits: Any) -> dict:
    """Write the credit and licence record. Only the known text fields (and a list of notes) are kept."""
    model = _model(deploy_dir, body)
    if not isinstance(credits, dict):
        raise SettingsError("the credits are not an object")
    existing = core.read_credits(model) or {}
    out = dict(existing)
    for key in CREDIT_FIELDS:
        if key in credits:
            value = credits[key]
            if (
                not isinstance(value, str)
                or len(value) > MAX_CREDIT_FIELD
                or "\x00" in value
            ):
                raise SettingsError(
                    f"{key}: text of up to {MAX_CREDIT_FIELD} characters is expected"
                )
            out[key] = value
    if "notes" in credits:
        notes = credits["notes"]
        if (
            not isinstance(notes, list)
            or len(notes) > 20
            or not all(
                isinstance(n, str) and len(n) <= MAX_CREDIT_FIELD for n in notes
            )
        ):
            raise SettingsError(
                "notes: a short list of short lines is expected"
            )
        out["notes"] = notes
    if not any(out.get(k) for k in CREDIT_FIELDS):
        core.credits_path(model).unlink(missing_ok=True)
        return {}
    _write_json(core.credits_path(model), out)
    return out


def save_face(deploy_dir: Path, body: str, face: Any) -> dict:
    """Change the landmarks of an existing face sidecar (fractions of the body's box). Keys other than
    the numbers of eye_l, eye_r and mouth are kept as they are; a body with no sidecar has none to edit
    (it is measured, not chosen: `face-sidecar`)."""
    model = _model(deploy_dir, body)
    path = core.face_sidecar_path(model)
    current = _read_json(path)
    if current is None:
        raise SettingsError("this model has no face landmarks to edit")
    if not isinstance(face, dict):
        raise SettingsError("the face landmarks are not an object")
    merged = json.loads(json.dumps(current))
    for part, values in face.items():
        if part not in FACE_PARTS or not isinstance(values, dict):
            raise SettingsError(f"{part}: not a face landmark")
        for key, value in values.items():
            if key not in FACE_KEYS:
                raise SettingsError(f"{part}.{key}: not a landmark value")
            if (
                not isinstance(value, (int, float))
                or isinstance(value, bool)
                or not 0 <= value <= 1
            ):
                raise SettingsError(
                    f"{part}.{key}: a number from 0 to 1 is expected"
                )
            merged.setdefault(part, {})[key] = float(value)
    _write_json(path, merged)
    return merged


# --- the alignment sheet and the rebuild ----------------------------------------------------------


def alignment_spec(
    deploy_dir: Path, body: str, settings: dict, items: dict | None = None
) -> LookSpec:
    """A look of `body` with one item in every socket it carries (the item with a placement in
    `settings` where there is one), for a throwaway build that shows what is off on this model.

    `items` picks what to render instead, socket by socket: an item name wears that item, null
    leaves the socket empty, and a socket it does not mention keeps the default. So several
    placements can be checked one at a time."""
    items = items or {}
    for socket, item in items.items():
        offered = core.effective_wardrobe_catalog(
            deploy_dir, body, with_settings=False
        )["accessories"].get(socket)
        if offered is None:
            raise SettingsError(f"{socket!r} is not a socket of this model")
        if item is not None and item not in offered:
            raise SettingsError(f"{item!r} is not offered on {socket}")
    model = _model(deploy_dir, body)
    catalog = core.effective_wardrobe_catalog(
        deploy_dir, body, with_settings=False
    )
    placed = (
        settings.get("accessories", {}).get("placement", {})
        if isinstance(settings, dict)
        else {}
    )
    accessories = {}
    for socket, offered in catalog["accessories"].items():
        if not offered:
            continue
        if socket in items:
            if items[socket] is not None:
                accessories[socket] = items[socket]
            continue
        mine = [i for i in offered if i in placed.get(socket, {})]
        accessories[socket] = (mine or offered)[0]
    caps = core._read_json(core.caps_path(model)) or {}
    return LookSpec(
        base_template=body,
        has_face=bool(caps.get("capabilities", {}).get("has_face", False)),
        palette=PaletteSpec(
            primary="#4A90D9",
            secondary="#FFFFFF",
            accent="#F5A623",
            emissive="#7FD1FF",
        ),
        wardrobe=WardrobeSpec(),
        accessories=AccessoriesSpec(**accessories),
    )


def current_look_spec(deploy_dir: Path, body: str) -> LookSpec:
    """The spec of the current look, if it is on `body` (what Rebuild builds again)."""
    _model(deploy_dir, body)
    manifest = load_manifest(deploy_dir)
    entry = None
    if manifest is not None:
        entry = next(
            (
                v
                for v in manifest.versions
                if v.version == manifest.current_version
            ),
            None,
        )
    if entry is None or not entry.look_spec:
        raise SettingsError("there is no current look to rebuild")
    if entry.look_spec.get("base_template") != body:
        raise SettingsError("the current look is not on this model")
    return LookSpec.model_validate(entry.look_spec)
