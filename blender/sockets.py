"""Which bone of a rig carries each accessory socket.

Pure Python (no bpy), shared by the generator, which reads a model's bones to
decide what it can wear and where to attach it, and by the tests. Rigs name
their bones differently (Mixamo `mixamorig:RightHand`, Blender `hand.R`,
Quaternius `Hand_R`...), so this matches on meaning, not on one convention.
A socket is only reported when the rig really has the bone for it; a model
with no armature gets the generator's own canonical rig instead
(`CANONICAL_BONES`).
"""

from __future__ import annotations

import re

SOCKET_ROLES = {
    "socket_head_top": "head",
    "socket_eyes": "head",
    "socket_neck": "neck",
    "socket_chest": "chest",
    "socket_waist": "waist",
    "socket_hand_R": "hand_R",
    "socket_hand_L": "hand_L",
    "socket_orbit": "head",
}

# The rig the generator gives a model that has none (build_armature): no hand
# bones, so no hand sockets.
CANONICAL_BONES = ("root", "spine", "head")

_FINGER = re.compile(
    r"thumb|index|middle|ring|pinky|pinkie|finger|metacarpal|twist|roll|end|tip|nub"
)
_TOKEN = re.compile(r"[a-z]+|\d+")


def _normal(name: str) -> str:
    """Lower-case, with any `mixamorig:`-style namespace dropped."""
    return name.lower().rsplit(":", 1)[-1]


def _tokens(name: str) -> list[str]:
    return _TOKEN.findall(_normal(name))


def _side(name: str) -> str | None:
    n = _normal(name)
    tokens = _tokens(name)
    if "right" in n or "r" in tokens:
        return "R"
    if "left" in n or "l" in tokens:
        return "L"
    return None


def _has(name: str, word: str) -> bool:
    return word in _normal(name)


def _pick(
    names: list[str],
    *words: str,
    exclude: re.Pattern | None = None,
    side: str | None = None,
) -> str | None:
    """The first bone (in the rig's order) whose name has one of `words`."""
    for word in words:
        for name in names:
            if not _has(name, word):
                continue
            if exclude is not None and exclude.search(_normal(name)):
                continue
            if side is not None and _side(name) != side:
                continue
            return name
    return None


_TRUNK = re.compile(r"body|torso|trunk|core|centre|center")


def _trunk(names: list[str]) -> str | None:
    """The bone that is the whole body, for a rig with no head/spine of its own
    (a floating mascot with arms and legs, say). Only a bone that says so by
    name counts; a root or limb bone is never guessed."""
    for name in names:
        if _TRUNK.search(_normal(name)):
            return name
    return None


_FOREARM = ("lowerarm", "lower_arm", "forearm")


def _hand(names: list[str], side: str) -> str | None:
    """The hand bone of one side, or, for a rig whose arm ends in finger and palm
    segments with no hand bone of its own, the forearm: its tail is the wrist
    (`wrist_is_tail` tells the caller to attach there)."""
    hand = _pick(names, "hand", exclude=_FINGER, side=side)
    return hand or _pick(names, *_FOREARM, exclude=_FINGER, side=side)


def wrist_is_tail(bone_name: str) -> bool:
    """True for a forearm bone standing in for a hand (see `_hand`)."""
    return any(
        word in _normal(bone_name) for word in _FOREARM
    ) and "hand" not in _normal(bone_name)


def find_socket_bones(bone_names: list[str]) -> dict[str, str]:
    """socket name -> the bone to attach it to, for every socket this rig can
    carry. Sockets the rig lacks a bone for are left out."""
    head = _pick(bone_names, "head", exclude=_FINGER)
    neck = _pick(bone_names, "neck")
    spine = [n for n in bone_names if _has(n, "spine") or _has(n, "chest")]
    chest = _pick(bone_names, "upperchest", "chest") or (
        spine[-1] if spine else None
    )
    waist = _pick(bone_names, "hips", "pelvis", "waist") or (
        spine[0] if spine else None
    )
    if not (
        head or neck or chest or waist
    ):  # no torso bones at all: the trunk carries them
        head = neck = chest = waist = _trunk(bone_names)
    roles = {
        "head": head,
        "neck": neck or head,
        "chest": chest,
        "waist": waist,
        "hand_R": _hand(bone_names, "R"),
        "hand_L": _hand(bone_names, "L"),
    }
    return {
        socket: name
        for socket, role in SOCKET_ROLES.items()
        if (name := roles[role])
    }


def canonical_socket_bones() -> dict[str, str]:
    return find_socket_bones(list(CANONICAL_BONES))
