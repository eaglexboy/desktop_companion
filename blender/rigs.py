"""Which rig a body has, and how its bones map to the Quaternius skeleton the animation libraries are
made for.

Pure Python (no bpy), shared by `blender/rig_convert.py`, which converts the body and retargets the
libraries onto it, and by `brain.appearance.models`, which decides whether a body needs that.

A rig family is a rig made by one tool, with the bone names that tool gives: Character Creator's
`CC_Base_*` and Auto-Rig Pro's `*_stretch_l` are the two known. A family is mapped one-to-one onto the
Quaternius bones it has an equivalent of; its other bones (twists, facial bones, a second spine joint)
stay where they are, as they are, and follow their parents. The Quaternius bones a rig lacks (the
index finger of a four-fingered mech) are left out of the clips: a body does not need every bone
(`blender/animation_library.py`, `library_gaps`).
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from pathlib import Path

RIG_MAPS_DIR = Path(__file__).resolve().parent / "rig_maps"
RIG_MAP_FORMAT = 1
MAX_ARM_SPREAD = 60

CHARACTER_CREATOR = "character_creator"
AUTO_RIG_PRO = "auto_rig_pro"


def _finger(prefix: str, joint: int, side: str) -> str:
    return f"{prefix}_{joint:02d}_{side}"


# --- Character Creator -----------------------------------------------------------------------------

_CC_CENTRE = {
    "RL_BoneRoot": "root",
    "CC_Base_Hip": "pelvis",  # the hips; `CC_Base_Pelvis` below it (the thighs hang from it) stays as it is
    "CC_Base_Waist": "spine_01",
    "CC_Base_Spine01": "spine_02",
    "CC_Base_Spine02": "spine_03",
    "CC_Base_NeckTwist01": "neck_01",
    "CC_Base_Head": "Head",
}
_CC_LIMB = {
    "Clavicle": "clavicle",
    "Upperarm": "upperarm",
    "Forearm": "lowerarm",
    "Hand": "hand",
    "Thigh": "thigh",
    "Calf": "calf",
    "Foot": "foot",
    "ToeBase": "ball",
}
_CC_FINGER = {
    "Thumb": "thumb",
    "Index": "index",
    "Mid": "middle",
    "Ring": "ring",
    "Pinky": "pinky",
}


def character_creator_to_quaternius(name: str) -> str | None:
    if name in _CC_CENTRE:
        return _CC_CENTRE[name]
    for marker, side in (("CC_Base_L_", "l"), ("CC_Base_R_", "r")):
        if name.startswith(marker):
            part = name[len(marker) :]
            if part in _CC_LIMB:
                return f"{_CC_LIMB[part]}_{side}"
            for finger, target in _CC_FINGER.items():
                if part.startswith(finger) and part[len(finger) :] in (
                    "1",
                    "2",
                    "3",
                ):
                    return _finger(target, int(part[len(finger) :]), side)
    return None


# --- Auto-Rig Pro ----------------------------------------------------------------------------------

_ARP_CENTRE = {
    "root": "root",
    "root_x": "pelvis",
    "spine_01_x": "spine_01",
    "spine_03_x": "spine_02",  # five spine joints on the rig: the first, middle and last
    "spine_05_x": "spine_03",
    "neck_x": "neck_01",
    "head_x": "Head",
}
_ARP_LIMB = {
    "shoulder": "clavicle",
    "arm_stretch": "upperarm",
    "forearm_stretch": "lowerarm",
    "hand": "hand",
    "thigh_stretch": "thigh",
    "leg_stretch": "calf",
    "foot": "foot",
    "toes_01": "ball",
}
_ARP_FINGER = {
    "thumb": "thumb",
    "index": "index",
    "middle": "middle",
    "ring": "ring",
    "pinky": "pinky",
}


def auto_rig_pro_to_quaternius(name: str) -> str | None:
    if name in _ARP_CENTRE:
        return _ARP_CENTRE[name]
    for suffix, side in (("_l", "l"), ("_r", "r")):
        if name.endswith(suffix):
            part = name[: -len(suffix)]
            if part in _ARP_LIMB:
                return f"{_ARP_LIMB[part]}_{side}"
            for finger, target in _ARP_FINGER.items():
                if part.startswith(finger) and part[len(finger) :] in (
                    "1",
                    "2",
                    "3",
                ):
                    return _finger(target, int(part[len(finger) :]), side)
    return None


_FAMILIES: dict[str, tuple[Callable[[str], str | None], frozenset[str]]] = {
    CHARACTER_CREATOR: (
        character_creator_to_quaternius,
        frozenset({"CC_Base_Hip", "CC_Base_L_Upperarm", "CC_Base_L_Thigh"}),
    ),
    AUTO_RIG_PRO: (
        auto_rig_pro_to_quaternius,
        frozenset({"root_x", "arm_stretch_l", "thigh_stretch_l"}),
    ),
}


# --- rig maps: families that are data -----------------------------------------------------------------------
#
# A rig map (`blender/rig_maps/<name>.json`) says which bones of a skeleton are which Quaternius bones, for
# a skeleton that has no tool of its own to recognise it by (a mech pack, a toon character, a Rigify-like
# rig). {"format": 1, "name": ..., "signature": [bones all present], "absent": [bones that must not be],
# "bones": {source bone: Quaternius bone}, "prune": true to fold the bones that have no Quaternius name
# into their parents (a skeleton of 180 bones, most of them for parts that break off, makes a look too
# big and too slow to build), "fold": {source bone: Quaternius bone} to fold named bones into a bone
# that is kept (a control bone the libraries do not drive, such as an IK foot, that has a mesh weighted
# to it: it would stay where it was while the leg moved), "arm_spread": degrees (0-60) to hold the upper
# arms out from the body in every clip (a body whose hips or legs are wider than its shoulders: the
# hanging arms of the libraries go through them)}. The names are as `blender/sketchfab.py` cleans them.


class RigMapError(ValueError):
    pass


def load_rig_map(
    path: Path,
) -> tuple[
    str,
    Callable[[str], str | None],
    frozenset[str],
    frozenset[str],
    bool,
    dict[str, str],
    float,
]:
    """(name, map, signature, absent, prune, fold, arm_spread) of a rig map file; RigMapError if it is not valid."""
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise RigMapError(f"{path.name}: not readable JSON ({exc})") from None
    if not isinstance(data, dict) or data.get("format") != RIG_MAP_FORMAT:
        raise RigMapError(f"{path.name}: format must be {RIG_MAP_FORMAT}")
    name = data.get("name")
    if not isinstance(name, str) or not name:
        raise RigMapError(f"{path.name}: needs a name")
    bones, signature, absent = (
        data.get("bones"),
        data.get("signature"),
        data.get("absent", []),
    )
    if (
        not isinstance(bones, dict)
        or not bones
        or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in bones.items()
        )
    ):
        raise RigMapError(
            f"{path.name}: bones must map source bone names to Quaternius names"
        )
    if (
        not isinstance(signature, list)
        or not signature
        or not set(signature) <= set(bones)
    ):
        raise RigMapError(f"{path.name}: signature must list bones of the map")
    if not isinstance(absent, list) or set(absent) & set(signature):
        raise RigMapError(
            f"{path.name}: absent must be a list that does not overlap the signature"
        )
    if not isinstance(data.get("prune", False), bool):
        raise RigMapError(f"{path.name}: prune must be true or false")
    fold = data.get("fold", {})
    if (
        not isinstance(fold, dict)
        or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in fold.items()
        )
        or not set(fold.values()) <= set(bones.values())
        or set(fold) & set(bones)
    ):
        raise RigMapError(
            f"{path.name}: fold must map bones that are not in the map onto Quaternius names the map has"
        )
    spread = data.get("arm_spread", 0)
    if (
        isinstance(spread, bool)
        or not isinstance(spread, (int, float))
        or not 0 <= spread <= MAX_ARM_SPREAD
    ):
        raise RigMapError(
            f"{path.name}: arm_spread must be a number of degrees from 0 to {MAX_ARM_SPREAD}"
        )
    return (
        name,
        bones.get,
        frozenset(signature),
        frozenset(absent),
        bool(data.get("prune", False)),
        fold,
        float(spread),
    )


_ABSENT: dict[str, frozenset[str]] = {}
_PRUNE: set[str] = set()
_FOLD: dict[str, dict[str, str]] = {}
_SPREAD: dict[str, float] = {}
for _path in sorted(RIG_MAPS_DIR.glob("*.json")):
    _name, _to, _signature, _absent, _prune, _fold, _spread = load_rig_map(
        _path
    )
    _FAMILIES[_name] = (_to, _signature)
    _ABSENT[_name] = _absent
    _FOLD[_name] = _fold
    _SPREAD[_name] = _spread
    if _prune:
        _PRUNE.add(_name)


def folded_bones(family: str) -> dict[str, str]:
    """The bones of `family` that are folded into a kept bone: {source bone: Quaternius bone}."""
    return _FOLD.get(family, {})


def arm_spread(family: str | None) -> float:
    """Degrees the upper arms of a skeleton of `family` are held out from the body in every clip."""
    return _SPREAD.get(family or "", 0.0)


def prunes_extra_bones(family: str) -> bool:
    """True if a skeleton of `family` has its bones without a Quaternius name folded into their parents."""
    return family in _PRUNE


def _distance_to_segment(point, a, b) -> float:
    ab = [y - x for x, y in zip(a, b)]
    ap = [y - x for x, y in zip(a, point)]
    length_squared = sum(x * x for x in ab)
    t = (
        0.0
        if length_squared == 0
        else max(
            0.0, min(1.0, sum(x * y for x, y in zip(ap, ab)) / length_squared)
        )
    )
    return sum((p - (x + t * d)) ** 2 for p, x, d in zip(point, a, ab)) ** 0.5


def fold_target(
    point,
    ancestor: str | None,
    segments: dict[str, tuple],
) -> str | None:
    """The kept bone a pruned bone's weights go to: its nearest kept ancestor, `ancestor`, as a rule
    (a twist bone, a finger joint), but the kept bone that is nearest in space when there is none or
    it is `root`, which never moves with the body: a prop on a shoulder is parented to the model's
    root by a tool that does not care, and would otherwise be left standing where it was when the
    body lies down. `segments` is {kept bone: (head, tail)} without the root; `point` is where the
    pruned bone is.
    """
    if ancestor not in (None, "root") or not segments:
        return ancestor
    return min(
        segments, key=lambda name: _distance_to_segment(point, *segments[name])
    )


def rig_family(bone_names: Iterable[str]) -> str | None:
    """The family of a skeleton (its bone names, as cleaned by `blender/sketchfab.py`), None if it is
    neither known family. A skeleton already named for Quaternius is no family: it needs no conversion.
    """
    names = set(bone_names)
    for family, (_map, signature) in _FAMILIES.items():
        if signature <= names and not (
            _ABSENT.get(family, frozenset()) & names
        ):
            return family
    return None


def quaternius_names(family: str, bone_names: Iterable[str]) -> dict[str, str]:
    """{bone: its Quaternius name} for the bones of a skeleton of `family` that have one. A target
    two bones would get is given to neither: a skeleton cannot have the same name twice.
    """
    to = _FAMILIES[family][0]
    found: dict[str, str] = {}
    for name in bone_names:
        target = to(name)
        if target:
            found[name] = target
    count: dict[str, int] = {}
    for target in found.values():
        count[target] = count.get(target, 0) + 1
    return {
        bone: target for bone, target in found.items() if count[target] == 1
    }
