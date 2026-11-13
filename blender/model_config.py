"""Per-model settings: `<model>.config.json` beside an installed model (ADR_MODEL_SETTINGS_PAGE).

The file holds only what the user changed, never a copy of the defaults, so a better rule or a new pack
still reaches a model that has not overridden it. This module is the format: the reader (tolerant, a
settings file never fails a build), the validator (strict, run before a save), the atomic writer, and
the `pose` fallback to the older `<model>.pose.json`. It reads no model and runs no Blender.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from blender.pack_profiles import KNOWN_ROLES

FORMAT = 1
MAX_BYTES = 64 * 1024  # a settings file is small; a bigger one is not one

_SECTIONS = ("animations", "accessories", "pose")
_ANIMATION_KEYS = ("libraries", "clips", "roles")
_ACCESSORY_KEYS = ("sockets", "items", "placement")
_POSE_KEYS = (
    "face_yaw",
    "roll_bones",
    "emissive_gain",
    "relax_arms",
    "arm_spread",
)
_PLACEMENT_KEYS = ("offset", "scale", "rotation")

MAX_ARM_SPREAD = 60.0  # degrees, as for a rig map (blender/rigs.py)
MAX_OFFSET = 2.0  # metres, in the socket's frame
SCALE_RANGE = (0.05, 5.0)
MAX_ANGLE = 360.0
MAX_EMISSIVE_GAIN = 20.0

# A name in the file is only ever compared with names the brain already knows; it is never a path.
_NAME = re.compile(r"^[^\x00-\x1f/\\]{1,120}$")


def config_path(model_file: Path) -> Path:
    return model_file.with_name(f"{model_file.stem}.config.json")


def pose_sidecar_path(model_file: Path) -> Path:
    return model_file.with_name(f"{model_file.stem}.pose.json")


@dataclass
class Available:
    """What a body can be given, for the validator. A field left None is not checked (the caller
    could not say), so a partial answer never rejects a good file."""

    libraries: dict[str, set[str]] | None = (
        None  # library name -> its clip names
    )
    sockets: set[str] | None = None
    items: dict[str, set[str]] | None = (
        None  # socket -> item names offered there
    )


@dataclass
class Report:
    errors: list[str] = field(
        default_factory=list
    )  # the document is not acceptable
    stale: list[str] = field(
        default_factory=list
    )  # entries that name something no longer there
    stale_at: list[tuple] = field(
        default_factory=list
    )  # ... as key paths, for removing them

    def mark_stale(self, path: tuple, message: str) -> None:
        self.stale.append(message)
        self.stale_at.append(path)

    @property
    def ok(self) -> bool:
        return not self.errors


# --- validation -----------------------------------------------------------------------------------


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _check_name(value: Any, where: str, errors: list[str]) -> bool:
    if (
        not isinstance(value, str)
        or not _NAME.match(value)
        or value.strip(".") == ""
    ):
        errors.append(f"{where}: not a usable name")
        return False
    return True


def _check_number(
    value: Any, where: str, lo: float, hi: float, errors: list[str]
) -> None:
    if not _is_number(value) or not (lo <= value <= hi):
        errors.append(f"{where}: a number from {lo:g} to {hi:g} is expected")


def _check_vector(
    value: Any, where: str, lim: float, errors: list[str]
) -> None:
    if not isinstance(value, list) or len(value) != 3:
        errors.append(f"{where}: three numbers are expected")
        return
    for n, v in enumerate(value):
        _check_number(v, f"{where}[{n}]", -lim, lim, errors)


def _object(
    doc: Any, where: str, allowed: tuple[str, ...], errors: list[str]
) -> dict:
    if not isinstance(doc, dict):
        errors.append(f"{where}: an object is expected")
        return {}
    for key in doc:
        if key not in allowed:
            errors.append(
                f"{where}: '{key}' is not a setting (known: {', '.join(allowed)})"
            )
    return doc


def _bool_table(table: Any, where: str, errors: list[str]) -> dict:
    if not isinstance(table, dict):
        errors.append(f"{where}: an object is expected")
        return {}
    for key, value in table.items():
        _check_name(key, f"{where}.{key}", errors)
        if not isinstance(value, bool):
            errors.append(f"{where}.{key}: true or false is expected")
    return table


def _check_animations(doc: Any, avail: Available, report: Report) -> None:
    errors = report.errors
    sec = _object(doc, "animations", _ANIMATION_KEYS, errors)
    libraries = _bool_table(
        sec.get("libraries", {}), "animations.libraries", errors
    )
    clips = _bool_table(sec.get("clips", {}), "animations.clips", errors)
    roles = sec.get("roles", {})
    if not isinstance(roles, dict):
        errors.append("animations.roles: an object is expected")
        roles = {}

    known_clips: set[str] | None = None
    if avail.libraries is not None:
        known_clips = (
            set().union(*avail.libraries.values()) if avail.libraries else set()
        )
        for lib in libraries:
            if lib not in avail.libraries:
                report.mark_stale(
                    ("animations", "libraries", lib),
                    f"animations.libraries.{lib}: no longer available",
                )
        # a clip that is only in a library switched off cannot fill a role
        usable = (
            set().union(
                *(
                    c
                    for lib, c in avail.libraries.items()
                    if libraries.get(lib, True)
                )
            )
            if avail.libraries
            else set()
        )
    else:
        usable = None

    if known_clips is not None:
        for clip in clips:
            if clip not in known_clips:
                report.mark_stale(
                    ("animations", "clips", clip),
                    f"animations.clips.{clip}: no longer available",
                )

    for role, chosen in roles.items():
        if role not in KNOWN_ROLES:
            errors.append(
                f"animations.roles.{role}: not a role (known: {', '.join(sorted(KNOWN_ROLES))})"
            )
            continue
        if chosen is None:
            continue  # the role is off for this body
        if not isinstance(chosen, list) or not chosen:
            errors.append(
                f"animations.roles.{role}: a list of clips, or null to switch it off"
            )
            continue
        seen: set[str] = set()
        for n, clip in enumerate(chosen):
            where = f"animations.roles.{role}[{n}]"
            if not _check_name(clip, where, errors):
                continue
            if clip in seen:
                errors.append(f"{where}: '{clip}' is listed twice")
            seen.add(clip)
            if known_clips is not None and clip not in known_clips:
                report.mark_stale(
                    ("animations", "roles", role, n),
                    f"{where}: '{clip}' is no longer available",
                )
            elif usable is not None and clip not in usable:
                errors.append(
                    f"{where}: '{clip}' is only in a library switched off for this body"
                )
            elif clips.get(clip) is False:
                errors.append(
                    f"{where}: '{clip}' is switched off for this body"
                )


def _check_accessories(doc: Any, avail: Available, report: Report) -> None:
    errors = report.errors
    sec = _object(doc, "accessories", _ACCESSORY_KEYS, errors)
    sockets = _bool_table(sec.get("sockets", {}), "accessories.sockets", errors)
    if avail.sockets is not None:
        for socket in sockets:
            if socket not in avail.sockets:
                report.mark_stale(
                    ("accessories", "sockets", socket),
                    f"accessories.sockets.{socket}: no longer available",
                )

    def known_item(socket: str, item: str, where: str, path: tuple) -> None:
        if avail.items is None:
            return
        if socket not in avail.items or item not in avail.items[socket]:
            report.mark_stale(
                path, f"{where}: '{item}' is no longer offered on {socket}"
            )

    items = sec.get("items", {})
    if not isinstance(items, dict):
        errors.append("accessories.items: an object is expected")
        items = {}
    for socket, names in items.items():
        where = f"accessories.items.{socket}"
        _check_name(socket, where, errors)
        if not isinstance(names, list):
            errors.append(f"{where}: a list of items is expected")
            continue
        for n, item in enumerate(names):
            if _check_name(item, f"{where}[{n}]", errors):
                known_item(
                    socket,
                    item,
                    f"{where}[{n}]",
                    ("accessories", "items", socket, n),
                )

    placement = sec.get("placement", {})
    if not isinstance(placement, dict):
        errors.append("accessories.placement: an object is expected")
        placement = {}
    for socket, by_item in placement.items():
        where = f"accessories.placement.{socket}"
        _check_name(socket, where, errors)
        if not isinstance(by_item, dict):
            errors.append(f"{where}: an object of items is expected")
            continue
        for item, spec in by_item.items():
            at = f"{where}.{item}"
            if _check_name(item, at, errors):
                known_item(
                    socket, item, at, ("accessories", "placement", socket, item)
                )
            spec = _object(spec, at, _PLACEMENT_KEYS, errors)
            if "offset" in spec:
                _check_vector(
                    spec["offset"], f"{at}.offset", MAX_OFFSET, errors
                )
            if "scale" in spec:
                _check_number(
                    spec["scale"], f"{at}.scale", *SCALE_RANGE, errors
                )
            if "rotation" in spec:
                _check_vector(
                    spec["rotation"], f"{at}.rotation", MAX_ANGLE, errors
                )


def _check_pose(doc: Any, errors: list[str]) -> None:
    sec = _object(doc, "pose", _POSE_KEYS, errors)
    if "face_yaw" in sec:
        _check_number(
            sec["face_yaw"], "pose.face_yaw", -MAX_ANGLE, MAX_ANGLE, errors
        )
    if "emissive_gain" in sec:
        _check_number(
            sec["emissive_gain"],
            "pose.emissive_gain",
            0.01,
            MAX_EMISSIVE_GAIN,
            errors,
        )
    if "arm_spread" in sec:
        _check_number(
            sec["arm_spread"], "pose.arm_spread", 0, MAX_ARM_SPREAD, errors
        )
    if "relax_arms" in sec and not isinstance(sec["relax_arms"], bool):
        errors.append("pose.relax_arms: true or false is expected")
    if "roll_bones" in sec:
        rolls = sec["roll_bones"]
        if not isinstance(rolls, dict):
            errors.append("pose.roll_bones: an object is expected")
        else:
            for bone, angle in rolls.items():
                _check_name(bone, f"pose.roll_bones.{bone}", errors)
                _check_number(
                    angle,
                    f"pose.roll_bones.{bone}",
                    -MAX_ANGLE,
                    MAX_ANGLE,
                    errors,
                )


def validate(doc: Any, available: Available | None = None) -> Report:
    """Check a settings document. `errors` make it unsavable; `stale` are entries that name a library,
    clip, socket or item that is not there (any more), which the page shows and a build ignores.
    """
    report = Report()
    avail = available or Available()
    if not isinstance(doc, dict):
        report.errors.append("the settings are not an object")
        return report
    if doc.get("format") != FORMAT:
        report.errors.append(f"format: {FORMAT} is expected")
    for key in doc:
        if key != "format" and key not in _SECTIONS:
            report.errors.append(
                f"'{key}' is not a section (known: {', '.join(_SECTIONS)})"
            )
    if "animations" in doc:
        _check_animations(doc["animations"], avail, report)
    if "accessories" in doc:
        _check_accessories(doc["accessories"], avail, report)
    if "pose" in doc:
        _check_pose(doc["pose"], report.errors)
    return report


# --- reading and writing --------------------------------------------------------------------------


def empty() -> dict:
    return {"format": FORMAT}


_override: dict | None = None


def set_override(doc: dict | None) -> None:
    """Make `load` answer `doc` for every model, in this process (a throwaway build that shows
    settings that are not saved yet; the generator sets it from `--settings-override`).
    """
    global _override
    _override = doc


def load(model_file: Path) -> tuple[dict, list[str]]:
    """(the settings of a model, warnings). A missing file is no settings; a file that is not valid is
    ignored with a warning, because a settings file never fails a build. Stale entries are kept (the
    page shows them) and are skipped by whoever uses the settings."""
    if _override is not None:
        return _override, []
    path = config_path(model_file)
    try:
        if path.stat().st_size > MAX_BYTES:
            return empty(), [f"{path.name}: too large, ignored"]
        doc = json.loads(path.read_text())
    except FileNotFoundError:
        return empty(), []
    except (OSError, ValueError) as err:
        return empty(), [f"{path.name}: not readable ({err}), ignored"]
    report = validate(doc)
    if not report.ok:
        return empty(), [f"{path.name}: {report.errors[0]}, ignored"]
    return doc, []


def save(
    model_file: Path, doc: dict, available: Available | None = None
) -> Report:
    """Validate and write `doc` beside the model; the old file is replaced only when the new one is
    complete. A document with nothing in it removes the file (the model is back to the defaults).
    Nothing is written when there are errors."""
    report = validate(doc, available)
    if not report.ok:
        return report
    path = config_path(model_file)
    if not any(doc.get(s) for s in _SECTIONS):
        path.unlink(missing_ok=True)
        return report
    text = json.dumps(doc, indent=2, sort_keys=True) + "\n"
    if len(text.encode()) > MAX_BYTES:
        report.errors.append("the settings are too large")
        return report
    fd, tmp = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return report


def pose(model_file: Path) -> dict:
    """The pose settings of a model: `<model>.pose.json` with the config's `pose` section over it, key
    by key. Neither file is changed."""
    merged: dict = {}
    try:
        old = json.loads(pose_sidecar_path(model_file).read_text())
        if isinstance(old, dict):
            merged.update(old)
    except (OSError, ValueError):
        pass
    doc, _ = load(model_file)
    merged.update(doc.get("pose", {}))
    return merged


def placement(settings: dict, socket: str, item: str) -> dict:
    """The per-model placement of `item` on `socket` (`offset` metres in the socket's frame, `scale`
    a factor, `rotation` degrees), layered over the item's own sidecar: the offset is added, the
    scale multiplies, the rotation is added. Missing parts are the identity."""
    spec = (
        settings.get("accessories", {})
        .get("placement", {})
        .get(socket, {})
        .get(item, {})
    )
    return {
        "offset": tuple(float(v) for v in spec.get("offset", (0, 0, 0))),
        "scale": float(spec.get("scale", 1.0)),
        "rotation": tuple(float(v) for v in spec.get("rotation", (0, 0, 0))),
    }
