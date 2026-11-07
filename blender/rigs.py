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

from collections.abc import Callable, Iterable

CHARACTER_CREATOR = "character_creator"
AUTO_RIG_PRO = "auto_rig_pro"

_FINGERS = {
    "thumb": "thumb",
    "index": "index",
    "middle": "middle",
    "ring": "ring",
    "pinky": "pinky",
}


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
                if part.startswith(finger) and part[len(finger) :] in ("1", "2", "3"):
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
                if part.startswith(finger) and part[len(finger) :] in ("1", "2", "3"):
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


def rig_family(bone_names: Iterable[str]) -> str | None:
    """The family of a skeleton (its bone names, as cleaned by `blender/sketchfab.py`), None if it is
    neither known family. A skeleton already named for Quaternius is no family: it needs no conversion.
    """
    names = set(bone_names)
    for family, (_map, signature) in _FAMILIES.items():
        if signature <= names:
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
    return {bone: target for bone, target in found.items() if count[target] == 1}
