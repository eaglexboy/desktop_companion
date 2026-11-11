"""Which clips of an animation library a body can play, and as what.

Pure Python (no bpy), shared by the generator, which moves the clips onto a body's rig,
and by `brain.appearance.models`, which reports what each installed body gets. A
library is a .glb of clips made for a rig (Quaternius' Universal Animation Library is
one); its clips are keyed by bone name, so a body whose rig has the bones they need (all of
them, or the core of the body with some bones missing: `library_gaps`) can play them and any
other cannot.
"""

from __future__ import annotations

import json
from pathlib import Path

from blender import pack_profiles
from blender.clip_ops import MIRROR_SUFFIX


# Which clips fill which role, how each clip is converted and which clips are made of others is
# data, in profiles (`blender/pack_profiles.py`): the ones shipped in `blender/packs/` and the user's
# in the deploy directory. The roles a clip may fill are the overlay's behaviours (`KNOWN_ROLES`).
def library_roles() -> dict[str, tuple[str, ...]]:
    """role -> the clips that can fill it, best first. The overlay plays one clip at a time by role."""
    return pack_profiles.merged().roles


def library_extras() -> dict[str, tuple[str, ...]]:
    """More clips for a role, played as options alongside its first one (the overlay picks one at
    random each time: another way to talk, to wave hello, to sleep): listed as `<role>.alt1`,
    `<role>.alt2`, with their own `.low` / `.high` / `.mirror` copies. A role whose own clip is not
    in the library is played with its first extra."""
    return pack_profiles.merged().extras


# Roles whose clips belong together as a set: a way of sleeping has the clip that lies down into it,
# the loop, the shift in it and the get-up out of it (each starts and ends in the pose the next
# needs), and a way of sitting on the floor has its enter, idle and exit. The overlay plays a whole
# set, so option N of each role in a group has to be the same set: `merge_picks` keeps them aligned
# whichever library has which.
PAIRED_ROLES: tuple[tuple[str, ...], ...] = (
    ("lie_down", "sleep", "sleep_alt", "wake", "wake_slow"),
    ("floor_sit_enter", "floor_sit_idle", "floor_sit_exit"),
)

# A clip converted with intensity variants also has `<clip>_Low` and `<clip>_High`; they are
# listed as `<role>.low` / `<role>.high` and chosen by how the companion feels (see the
# overlay's gestures). Which variant suffixes there are:
VARIANT_SUFFIXES: dict[str, str] = {"low": "_Low", "high": "_High"}
# ... and every one of those may also be mirrored (`<clip>_Mirror`, `<clip>_Low_Mirror`: left and
# right swapped), listed as `<role>.mirror`, `<role>.low.mirror`; the overlay plays one of the
# two at random so a gesture is not always made with the same hand.


# Sitting down after a long idle: the enter clip, a loop while it sits, the exit clip. The three
# have to come from one pack and be one kind: sitting on the ground needs nothing, but the
# packs' free Sitting_* clips are for a chair, so a body that uses them gets a chair built
# for it (the generator's `build_sit_prop`) that the overlay shows only while it sits. Best first.
def library_sit_sets() -> tuple[dict[str, tuple[str, ...]], ...]:
    return pack_profiles.merged().sit_sets


def chair_sit_clips() -> frozenset[str]:
    """The clips of the sit sets that are for a chair."""
    return pack_profiles.merged().chair_clips


SIT_PROP_NODE = "SitProp"  # the chair's name in the file; the overlay shows and hides it by this


def library_time_scale() -> dict[str, float]:
    """Clips played slower than they were made, by role: a pack's idle loops may breathe every 1.25 s
    (48 a minute), which on a standing character reads as winded."""
    return pack_profiles.merged().time_scale


def read_glb_json(path: Path) -> dict:
    with path.open(
        "rb"
    ) as f:  # the header and the JSON chunk only: a model can be hundreds of MB
        head = f.read(20)
        return json.loads(f.read(int.from_bytes(head[12:16], "little")))


RETARGETED_DIR = "animation_library_body"


def rig_sidecar(model_path: Path) -> Path:
    return model_path.with_name(f"{model_path.stem}.rig.json")


def rig_family_of(model_path: Path | None) -> str | None:
    """The rig family a body was converted from (`blender/rigs.py`), from its sidecar."""
    if model_path is None:
        return None
    try:
        data = json.loads(rig_sidecar(model_path).read_text())
    except (OSError, ValueError):
        return None
    family = data.get("family") if isinstance(data, dict) else None
    return family if isinstance(family, str) else None


def is_retargeted(model_path: Path | None) -> bool:
    """True for a body converted to the Quaternius skeleton from another rig (`blender/rig_convert.py`):
    it plays libraries retargeted onto it, not the Quaternius ones as they are.
    """
    if model_path is None:
        return False
    try:
        data = json.loads(rig_sidecar(model_path).read_text())
    except (OSError, ValueError):
        return False
    return isinstance(data, dict) and bool(data.get("retargeted"))


def library_folder(deploy_dir: Path, model_path: Path | None = None) -> Path:
    """Where the animation libraries a body plays are: the folder of libraries everyone shares, or,
    for a body that was converted from another rig, the folder of the libraries retargeted onto it.
    """
    if model_path is not None and is_retargeted(model_path):
        return deploy_dir / "assets" / RETARGETED_DIR / model_path.stem
    return deploy_dir / "assets" / "animation_library"


def library_contents(path: Path) -> tuple[set[str], set[str]]:
    """(the names of the bones the library's rig has, the names of its clips)."""
    gltf = read_glb_json(path)
    nodes = gltf.get("nodes", [])
    joints = {
        nodes[j].get("name")
        for skin in gltf.get("skins", [])
        for j in skin.get("joints", [])
    }
    return joints, {anim.get("name") for anim in gltf.get("animations", [])}


def late_starts(gltf: dict) -> list[str]:
    """The clips of a library (its parsed glTF JSON) whose first keyframe is after time 0. Until that
    key the bones have no pose from the clip, so the body shows its rest pose (a T-pose) for that
    long whenever the clip starts."""
    accessors = gltf.get("accessors", [])
    late = []
    for anim in gltf.get("animations", []):
        starts = [
            accessors[sampler["input"]]["min"][0]
            for sampler in anim.get("samplers", [])
            if accessors[sampler["input"]].get("min")
        ]
        if starts and max(starts) > 1e-4:
            late.append(anim.get("name", ""))
    return late


def pick_clips(clips: set[str], have: set[str] = frozenset()) -> dict[str, str]:  # type: ignore[assignment]
    """{role: clip} for the roles not in `have` that `clips` can fill: the first of each role's
    candidates that is there, and a whole sit set (all three clips, the best set that is complete).
    """
    picked: dict[str, str] = {}
    extras = library_extras()
    for role, candidates in library_roles().items():
        first = next((c for c in candidates if c in clips), None)
        chosen = list(
            dict.fromkeys(  # (a clip a profile makes the role's own is not also one of its extras)
                c for c in (first, *extras.get(role, ())) if c and c in clips
            )
        )
        if role in have or not chosen:
            continue
        for n, clip in enumerate(chosen):
            key = role if n == 0 else f"{role}.alt{n}"
            picked[key] = clip
            for variant, suffix in VARIANT_SUFFIXES.items():
                if clip + suffix in clips:
                    picked[f"{key}.{variant}"] = clip + suffix
            for sub_key, name in [
                (k, v)
                for k, v in picked.items()
                if k == key or k.startswith(key + ".")
            ]:
                if name + MIRROR_SUFFIX in clips:
                    picked[f"{sub_key}.mirror"] = name + MIRROR_SUFFIX
    if "sit_idle" not in have:
        for sit_set in library_sit_sets():
            set_pick = {
                role: next((c for c in candidates if c in clips), None)
                for role, candidates in sit_set.items()
            }
            found = {
                role: clip
                for role, clip in set_pick.items()
                if clip is not None
            }
            if len(found) == len(set_pick):
                picked.update(found)
                break
    return picked


# A body does not need every bone of a library's rig to play its clips: a clip is bone rotations, so
# the tracks of a bone the body lacks (fingers, toes, a clavicle, a spine joint) are dropped and the
# rest plays. These are the bones it cannot do without: no hips, legs or arms, nothing to animate.
REQUIRED_BONES = frozenset(
    {
        "pelvis",
        "thigh_l",
        "thigh_r",
        "calf_l",
        "calf_r",
        "upperarm_l",
        "upperarm_r",
        "lowerarm_l",
        "lowerarm_r",
    }
)


def library_gaps(joints: set[str], bone_names: set[str]) -> set[str] | None:
    """The bones of a library's rig (`joints`) that a body with `bone_names` lacks, whose tracks are
    dropped when it wears the library; None when the body lacks one it cannot do without.
    """
    if not joints or not REQUIRED_BONES <= (joints & bone_names):
        return None
    return joints - bone_names


def wanted_clips(clip_names: set[str]) -> list[str]:
    """The clips of a library that are worth retargeting onto a converted body: those a role uses
    (with their variants), not the other forty-odd (swimming, sword fights...).
    """
    return sorted(set(pick_clips(clip_names).values()))


def merge_picks(plan: dict, picks: dict[str, str], source) -> dict:
    """Adds one library's `picks` ({key: clip}, from `pick_clips`) to `plan` ({key: (source, clip)}).
    A role that has options (`library_extras`: talk, greet, sleep...) that an earlier library
    already fills gets this library's clips as further options (`<role>.alt<N>`, with their
    variants); any other role is left to the first library that fills it. The roles of a set
    (`PAIRED_ROLES`) are numbered together, so that option N of each is the same set whichever
    roles a library has. Returns the plan.
    """

    def options_of(role: str) -> int:
        if not any(k == role or k.startswith(role + ".") for k in plan):
            return 0
        return 1 + len(
            {
                k.split(".")[1]
                for k in plan
                if k.split(".")[0] == role and k.startswith(role + ".alt")
            }
        )

    group_of = {role: group for group in PAIRED_ROLES for role in group}
    # how many sets there already are: the options of the group's loop role (sleep, the floor sit's idle)
    group_start = {group: options_of(group[1]) for group in PAIRED_ROLES}
    roles = {key.split(".")[0] for key in plan}
    options = {
        role: options_of(role) for role in roles if role in library_extras()
    }
    for key, clip in picks.items():
        role, _, rest = key.partition(".")
        if role in group_of:
            index, _, tail = (
                rest[3:] if rest.startswith("alt") else "0."
            ).partition(".")
            if not rest.startswith("alt"):
                tail = rest
            number = group_start[group_of[role]] + int(index or 0)
            new = role if number == 0 else f"{role}.alt{number}"
            plan[new + (f".{tail}" if tail else "")] = (source, clip)
        elif role not in roles:
            plan[key] = (source, clip)
        elif role in options:
            if rest.startswith("alt"):
                index, _, tail = rest[3:].partition(".")
                new = f"{role}.alt{options[role] + int(index)}" + (
                    f".{tail}" if tail else ""
                )
            else:
                new = f"{role}.alt{options[role]}" + (
                    f".{rest}" if rest else ""
                )
            plan[new] = (source, clip)
    return plan


def library_support(bone_names: set[str], library: Path) -> dict[str, str]:
    """What `library` gives a body with these bones: {role: clip}, empty if it does not fit."""
    try:
        joints, clips = library_contents(library)
    except (OSError, ValueError):
        return {}
    if library_gaps(joints, bone_names) is None:
        return {}
    return pick_clips(clips)


# --- Mixamo ----------------------------------------------------------------------------------
#
# Mixamo's skeleton (`mixamorig:Hips`...) has the same 65 bones as the Quaternius one in the same
# arrangement, bar a tip bone, and the same T-pose; `mixamo_to_quaternius` is the name map the
# converter (blender/mixamo_convert.py) uses to turn a Mixamo body, or a Mixamo clip, into a
# Quaternius one. Pure Python so that the brain can recognise a Mixamo file and the tests can
# check the map without Blender.

_MIXAMO_BONES = {
    "Hips": "pelvis",
    "Spine": "spine_01",
    "Spine1": "spine_02",
    "Spine2": "spine_03",
    "Neck": "neck_01",
    "Head": "Head",
    "Shoulder": "clavicle",
    "Arm": "upperarm",
    "ForeArm": "lowerarm",
    "Hand": "hand",
    "UpLeg": "thigh",
    "Leg": "calf",
    "Foot": "foot",
    "ToeBase": "ball",
    "Toe_End": "ball_leaf",
}
_MIXAMO_FINGERS = {
    "Thumb": "thumb",
    "Index": "index",
    "Middle": "middle",
    "Ring": "ring",
    "Pinky": "pinky",
}
MIXAMO_PREFIX = "mixamorig:"


def mixamo_to_quaternius(bone: str) -> str | None:
    """The Quaternius name of a Mixamo bone, None for the one with no counterpart (`HeadTop_End`)."""
    name = bone.removeprefix(MIXAMO_PREFIX)
    side = (
        "_l"
        if name.startswith("Left")
        else "_r" if name.startswith("Right") else ""
    )
    name = name.removeprefix("Left").removeprefix("Right")
    if name in _MIXAMO_BONES:
        return _MIXAMO_BONES[name] + side
    for finger, target in _MIXAMO_FINGERS.items():
        if name.startswith("Hand" + finger) and name[-1:].isdigit():
            joint = int(name[-1])
            return (
                f"{target}_04_leaf{side}"
                if joint == 4
                else f"{target}_0{joint}{side}"
            )
    return None


def is_mixamo_rig(bone_names: set[str] | list[str]) -> bool:
    names = set(bone_names)
    return (
        f"{MIXAMO_PREFIX}Hips" in names and f"{MIXAMO_PREFIX}LeftHand" in names
    )


def clip_name_from_file(stem: str) -> str:
    """`Mixamo_Standing_Greeting` -> `Standing_Greeting` (also the `Maximo_` typo)."""
    for prefix in ("Mixamo_", "Maximo_", "mixamo_"):
        if stem.startswith(prefix):
            return stem[len(prefix) :]
    return stem


# What a converted clip needs, by what it is (the profiles' `clips` rules: a seated clip is lined up
# with the seat the sit clips use and is looped, a greeting gets intensity variants...). Anything can
# be overridden in an options file given to `add-animations --options`.
def mixamo_clip_defaults(name: str) -> dict:
    return pack_profiles.clip_options(name)


def mixamo_derived_clips(names: set[str] | list[str]) -> list[dict]:
    """Clips made of others, for the clips present (`names`): the profiles' `derived` recipes whose
    clips are all there (a workout is lying down, the exercise a few times and standing up, eased
    together and from and to the standing idle; sitting on the floor is eased down from standing, the
    idle, and back up). `{"name", "options": {...}}`, to go after the others."""
    return pack_profiles.derived_clips(names)


def mirror_bone(name: str) -> str:
    """The bone on the other side of the body (`hand_l` -> `hand_r`); a centre bone is its own."""
    if name.endswith("_l"):
        return name[:-2] + "_r"
    if name.endswith("_r"):
        return name[:-2] + "_l"
    return name


# --- quaternions (w, x, y, z) as plain tuples -------------------------------------------------


from blender.clip_ops import (  # noqa: F401 -- re-exported
    IDENTITY_Q,
    INTENSITY_VARIANTS,
    Clip,
    Pose,
    Quat,
    Vec,
    _mix_hips,
    _smooth,
    blend_bone,
    blend_through,
    close_loop,
    ease_ends,
    finish_clips,
    in_place_clip,
    join_clips,
    loop_clip,
    lying_turn,
    offset_hips,
    pelvis_rest,
    ping_pong,
    pose_at,
    q_about_z,
    q_angle_deg,
    q_conj,
    q_dot,
    q_mul,
    q_rotate,
    q_slerp,
    reduce_keys,
    reverse_clip,
    scale_intensity,
    seat_offset,
    trim_clip,
)
