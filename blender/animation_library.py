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
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

# role -> the clips that can fill it, best first. The overlay plays one clip at a time by role.
LIBRARY_ROLES: dict[str, tuple[str, ...]] = {
    "idle": ("Idle_Loop",),
    "talk": ("Idle_Talking_Loop",),
    "run": ("Sprint_Loop", "Jog_Fwd_Loop"),
    "walk": ("Walk_Loop",),
    "idle_alt": ("Idle_FoldArms_Loop",),
    "celebrate": ("Dance_Loop",),
    # From a converted Mixamo pack (see "Mixamo" below):
    "greet": ("Standing_Greeting",),
    "angry": ("Angry",),
    "hip_thrust": ("Angry_Hip_Thrust",),
    "sit_alt": ("Sitting_Rubbing_Arm",),
    "sit_talk": (
        "Sitting_Talking",
    ),  # talking to the user from the chair, without standing up
    # Floor work, sleeping and more ways to carry on (converted Mixamo clips):
    "workout": ("Workout_Burpee",),
    "workout_floor": ("Workout_Crunch",),
    "lie_down": ("Lie_Down",),
    "sleep": ("Sleeping_Idle_1",),  # lying on its back, breathing
    "sleep_alt": ("Sleeping_Roll",),  # now and then it shifts, slowly, and settles back
    "wake": ("Stand_Up",),
    "wake_slow": ("Standing_Up",),
    "idle_look": ("Looking",),
    "idle_sad": ("Sad_Idle",),
    "floor_sit_enter": ("FloorSit_Enter",),
    "floor_sit_idle": ("FloorSit_Idle",),
    "floor_sit_exit": ("FloorSit_Exit",),
}

# More clips for a role, played as options alongside its first one (the overlay picks one at
# random each time: another way to talk, to wave hello, to sleep): listed as `<role>.alt1`,
# `<role>.alt2`, with their own `.low` / `.high` / `.mirror` copies. A role whose own clip is not
# in the library is played with its first extra.
LIBRARY_EXTRAS: dict[str, tuple[str, ...]] = {
    "talk": ("Talking", "Talking_1"),
    "greet": ("Waving", "Waving_1"),
    "idle_look": ("Looking_Around",),
}

# How much slower than the Mixamo clip the sleeping roll plays.
SLEEP_ROLL_SLOWDOWN = 1.5

# A clip converted with intensity variants also has `<clip>_Low` and `<clip>_High`; they are
# listed as `<role>.low` / `<role>.high` and chosen by how the companion feels (see the
# overlay's gestures). Which variant suffixes there are:
VARIANT_SUFFIXES: dict[str, str] = {"low": "_Low", "high": "_High"}
# ... and every one of those may also be mirrored (`<clip>_Mirror`, `<clip>_Low_Mirror`: left and
# right swapped), listed as `<role>.mirror`, `<role>.low.mirror`; the overlay plays one of the
# two at random so a gesture is not always made with the same hand.
MIRROR_SUFFIX = "_Mirror"

# Sitting down after a long idle: the enter clip, a loop while it sits, the exit clip. The three
# have to come from one pack and be one kind: sitting on the ground needs nothing, but the
# packs' free Sitting_* clips are for a chair, so a body that uses them gets a chair built
# for it (the generator's `build_sit_prop`) that the overlay shows only while it sits. Best first.
LIBRARY_SIT_SETS: tuple[dict[str, tuple[str, ...]], ...] = (
    {
        "sit_enter": ("GroundSit_Enter",),
        "sit_idle": ("GroundSit_Idle", "GroundSit_Idle_Loop"),
        "sit_exit": ("GroundSit_Exit",),
    },
    {
        "sit_enter": ("Sitting_Enter",),
        "sit_idle": ("Sitting_Idle_Loop",),
        "sit_exit": ("Sitting_Exit",),
    },
)
CHAIR_SIT_CLIPS = frozenset({"Sitting_Enter", "Sitting_Idle_Loop", "Sitting_Exit"})
SIT_PROP_NODE = (
    "SitProp"  # the chair's name in the file; the overlay shows and hides it by this
)

# Clips played slower than they were made: the pack's idle loops breathe about every 1.25 s
# (48 a minute), which on a standing character reads as winded. 2.5x is about 19 a minute.
LIBRARY_TIME_SCALE: dict[str, float] = {"idle": 2.5, "idle_alt": 2.5, "sit_idle": 2.0}


def read_glb_json(path: Path) -> dict:
    with path.open(
        "rb"
    ) as f:  # the header and the JSON chunk only: a model can be hundreds of MB
        head = f.read(20)
        return json.loads(f.read(int.from_bytes(head[12:16], "little")))


RETARGETED_DIR = "animation_library_body"


def rig_sidecar(model_path: Path) -> Path:
    return model_path.with_name(f"{model_path.stem}.rig.json")


def is_retargeted(model_path: Path | None) -> bool:
    """True for a body converted to the Quaternius skeleton from another rig (`blender/rig_convert.py`):
    it plays libraries retargeted onto it, not the Quaternius ones as they are."""
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
    for role, candidates in LIBRARY_ROLES.items():
        first = next((c for c in candidates if c in clips), None)
        chosen = [c for c in (first, *LIBRARY_EXTRAS.get(role, ())) if c and c in clips]
        if role in have or not chosen:
            continue
        for n, clip in enumerate(chosen):
            key = role if n == 0 else f"{role}.alt{n}"
            picked[key] = clip
            for variant, suffix in VARIANT_SUFFIXES.items():
                if clip + suffix in clips:
                    picked[f"{key}.{variant}"] = clip + suffix
            for sub_key, name in [
                (k, v) for k, v in picked.items() if k == key or k.startswith(key + ".")
            ]:
                if name + MIRROR_SUFFIX in clips:
                    picked[f"{sub_key}.mirror"] = name + MIRROR_SUFFIX
    if "sit_idle" not in have:
        for sit_set in LIBRARY_SIT_SETS:
            chosen = {
                role: next((c for c in candidates if c in clips), None)
                for role, candidates in sit_set.items()
            }
            if all(chosen.values()):
                picked.update(chosen)  # type: ignore[arg-type]
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
    (with their variants), not the other forty-odd (swimming, sword fights...)."""
    return sorted(set(pick_clips(clip_names).values()))


def merge_picks(plan: dict, picks: dict[str, str], source) -> dict:
    """Adds one library's `picks` ({key: clip}, from `pick_clips`) to `plan` ({key: (source, clip)}).
    A role that has options (`LIBRARY_EXTRAS`: talk, greet, sleep...) that an earlier library
    already fills gets this library's clips as further options (`<role>.alt<N>`, with their
    variants); any other role is left to the first library that fills it. Returns the plan.
    """
    roles = {key.split(".")[0] for key in plan}
    options = {
        role: 1
        + len(
            {
                key.split(".")[1]
                for key in plan
                if key.split(".")[0] == role and key.startswith(role + ".alt")
            }
        )
        for role in roles
        if role in LIBRARY_EXTRAS
    }
    for key, clip in picks.items():
        role, _, rest = key.partition(".")
        if role not in roles:
            plan[key] = (source, clip)
        elif role in options:
            if rest.startswith("alt"):
                index, _, tail = rest[3:].partition(".")
                new = f"{role}.alt{options[role] + int(index)}" + (
                    f".{tail}" if tail else ""
                )
            else:
                new = f"{role}.alt{options[role]}" + (f".{rest}" if rest else "")
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
    side = "_l" if name.startswith("Left") else "_r" if name.startswith("Right") else ""
    name = name.removeprefix("Left").removeprefix("Right")
    if name in _MIXAMO_BONES:
        return _MIXAMO_BONES[name] + side
    for finger, target in _MIXAMO_FINGERS.items():
        if name.startswith("Hand" + finger) and name[-1:].isdigit():
            joint = int(name[-1])
            return (
                f"{target}_04_leaf{side}" if joint == 4 else f"{target}_0{joint}{side}"
            )
    return None


def is_mixamo_rig(bone_names: "set[str] | list[str]") -> bool:
    names = set(bone_names)
    return f"{MIXAMO_PREFIX}Hips" in names and f"{MIXAMO_PREFIX}LeftHand" in names


def clip_name_from_file(stem: str) -> str:
    """`Mixamo_Standing_Greeting` -> `Standing_Greeting` (also the `Maximo_` typo)."""
    for prefix in ("Mixamo_", "Maximo_", "mixamo_"):
        if stem.startswith(prefix):
            return stem[len(prefix) :]
    return stem


# What a converted clip needs, by what it is: a seated clip is lined up with the seat the sit
# clips use and is looped; a greeting gets intensity variants. Anything can be overridden in
# an options file given to `add-animations --options`.
def mixamo_clip_defaults(name: str) -> dict:
    options: dict = {
        "loop": False,
        "variants": False,
        "mirror": True,
        "seat": False,
        "in_place": False,
        "trim": None,
        "extract": [],
    }
    if name.startswith("Sitting"):
        options.update(
            seat=True, blend=12, max_seconds=20
        )  # a variation: eased in from, and back to, the sit pose
    if name == "Sitting_Talking":
        options.update(
            mirror=False
        )  # it loops while she speaks; a mirrored copy would only cost size
    if "Greeting" in name:
        options.update(variants=True)
    # Floor work. Stand_Up and Burpee start from a push-up position, Sleeping_Idle and Laying_Idle
    # are on the floor: they are joined by easing (see `finish_clips`), the standing ones are made
    # to end where `stand_reference` (the idle) begins, and nothing travels.
    if name == "Stand_Up":
        options.update(
            in_place=True,
            mirror=False,
            ease_from="Sleeping_Idle",
            ease_to="@stand",
            ease_frames=36,  # the spin and roll between the floor and the push-up position
            extract=[
                {
                    "name": "Lie_Down",
                    "reverse": True,
                    "ease_from": "@stand",
                    "ease_to": "Sleeping_Idle",
                }
            ],
        )
    if name == "Standing_Up":  # the slow get-up, from lying flat (11 s)
        options.update(
            in_place=True, mirror=False, ease_from="Sleeping_Idle", ease_to="@stand"
        )
    # Sleeping_Idle is the clip in which she turns right over (150 degrees by the ninth second,
    # which a loop undid in 0.4 s: a barrel roll); only its first, restless 6.5 s are used, as
    # `Sleeping_Roll`: out and back along the same path, slowed, so she shifts in her sleep and
    # ends in the pose she began in. Sleeping_Idle_1 (breathing, on her back, the same first pose)
    # is the loop she sleeps in.
    if name == "Sleeping_Idle":
        options.update(
            mirror=False,
            max_seconds=7,
            extract=[
                {
                    "name": "Sleeping_Roll",
                    "max_seconds": 6.5,
                    "ping_pong": SLEEP_ROLL_SLOWDOWN,
                }
            ],
        )
    if name == "Sleeping_Idle_1":
        options.update(loop=True, mirror=False)
    # Laying and Laying_Idle are the position she rolls over into (133 degrees from the sleeping
    # pose): joined to it by easing they were a second barrel roll, so no role uses them.
    if name in ("Laying_Idle", "Laying"):
        options.update(mirror=False, blend_ref="Sleeping_Idle", max_seconds=8)
    if name in ("Burpee", "Bicycle_Crunch"):
        options.update(in_place=True, mirror=False, loop=name == "Bicycle_Crunch")
    if name in ("Talking", "Talking_1", "Sad_Idle"):
        options.update(
            loop=True, mirror=False
        )  # standing loops: they begin where they end
    if name in (
        "Looking",
        "Looking_Around",
    ):  # played once, now and then: eased from and to the idle
        options.update(mirror=False, blend_ref="@stand", ease_frames=12)
    if name in (
        "Waving",
        "Waving_1",
    ):  # eased from and to the idle; Waving also has a calm and an eager copy
        options.update(variants=name == "Waving", blend_ref="@stand", ease_frames=12)
    if name == "Ground_Sitting_Idle":
        options.update(loop=True, mirror=False)
    if name == "Standard_Run":
        options.update(in_place=True, loop=True, mirror=False)
    if name == "Angry":
        # the first half: arms crossed and a hip pushed out to the side every 2.7 s, which loops
        # cleanly from frame 57 to 217 (two pushes). Cut out as its own clip.
        options.update(
            extract=[{"name": "Angry_Hip_Thrust", "trim": [57, 217], "loop": True}],
            mirror_main=False,  # the whole clip is long: only the hip thrust is mirrored
        )
    return options


def mixamo_derived_clips(names: "set[str] | list[str]") -> list[dict]:
    """Clips made of others, for the clips present (`names`): a workout is lying down (Stand_Up
    backwards), the exercise a few times, and standing up, eased together and from and to the
    standing idle; sitting on the floor is eased down from standing, the idle, and back up.
    `{"name", "options": {...}}`, to go after the others."""
    have = set(names)
    derived = []
    for name, exercise, repeat in (
        ("Workout_Burpee", "Burpee", 2),
        ("Workout_Crunch", "Bicycle_Crunch", 5),
    ):
        if {exercise, "Stand_Up"} <= have:
            parts: list = ["Lie_Down", [exercise, repeat], "Stand_Up"]
            derived.append(
                {
                    "name": name,
                    "options": {
                        "compose": parts,
                        "ease": 30,  # ~1 s: the spin and the roll between the plank and the floor
                        "ease_from": "@stand",
                        "ease_to": "@stand",
                    },
                }
            )
    if "Ground_Sitting_Idle" in have:
        derived += [
            {
                "name": "FloorSit_Idle",
                "options": {"compose": ["Ground_Sitting_Idle"], "ease": 0},
            },
            {
                "name": "FloorSit_Enter",
                "options": {
                    "hold": "Ground_Sitting_Idle",
                    "ease_from": "@stand",
                    "ease_frames": 36,
                },
            },
            {
                "name": "FloorSit_Exit",
                "options": {
                    "hold": "Ground_Sitting_Idle",
                    "ease_to": "@stand",
                    "ease_frames": 36,
                },
            },
        ]
    return derived


def mirror_bone(name: str) -> str:
    """The bone on the other side of the body (`hand_l` -> `hand_r`); a centre bone is its own."""
    if name.endswith("_l"):
        return name[:-2] + "_r"
    if name.endswith("_r"):
        return name[:-2] + "_l"
    return name


# --- quaternions (w, x, y, z) as plain tuples -------------------------------------------------

Quat = tuple[float, float, float, float]


def q_dot(a: Quat, b: Quat) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2] + a[3] * b[3]


def q_slerp(a: Quat, b: Quat, t: float) -> Quat:
    """Spherical interpolation from `a` to `b`; `t` outside 0..1 extrapolates (an exaggerated
    or damped motion about `a`)."""
    import math

    if q_dot(a, b) < 0.0:
        b = (-b[0], -b[1], -b[2], -b[3])
    dot = min(1.0, q_dot(a, b))
    angle = math.acos(dot)
    if angle < 1e-6:
        return a
    sa, sb = math.sin((1 - t) * angle), math.sin(t * angle)
    sn = math.sin(angle)
    out = tuple((sa * x + sb * y) / sn for x, y in zip(a, b))
    norm = math.sqrt(sum(c * c for c in out)) or 1.0
    return tuple(c / norm for c in out)  # type: ignore[return-value]


def q_angle_deg(a: Quat, b: Quat) -> float:
    import math

    return math.degrees(2 * math.acos(min(1.0, abs(q_dot(a, b)))))


IDENTITY_Q: Quat = (1.0, 0.0, 0.0, 0.0)


def q_mul(a: Quat, b: Quat) -> Quat:
    return (
        a[0] * b[0] - a[1] * b[1] - a[2] * b[2] - a[3] * b[3],
        a[0] * b[1] + a[1] * b[0] + a[2] * b[3] - a[3] * b[2],
        a[0] * b[2] - a[1] * b[3] + a[2] * b[0] + a[3] * b[1],
        a[0] * b[3] + a[1] * b[2] - a[2] * b[1] + a[3] * b[0],
    )


def q_conj(a: Quat) -> Quat:
    return (a[0], -a[1], -a[2], -a[3])


def q_rotate(q: Quat, v: tuple[float, float, float]) -> tuple[float, float, float]:
    r = q_mul(q_mul(q, (0.0, v[0], v[1], v[2])), q_conj(q))
    return (r[1], r[2], r[3])


def q_about_z(angle: float) -> Quat:
    import math

    return (math.cos(angle / 2), 0.0, 0.0, math.sin(angle / 2))


def _smooth(x: float) -> float:
    x = min(max(x, 0.0), 1.0)
    return x * x * (3 - 2 * x)


def lying_turn(a: Quat, b: Quat, rest: Quat | None) -> Callable[[float], Quat] | None:
    """How the pelvis goes from rotation `a` to `b` when the body lies down in both (its long axis,
    the bone's y, is within 37 degrees of the floor): `None` (the plain slerp will do) or a function
    of t (0..1) giving the rotation. Turning from lying face down, head to one side, to lying on the
    back, head to the other, the slerp takes the shortest way: head over heels, through a handstand.
    This goes the way a body on the floor does: it spins about the vertical for the first 70 % of
    the time and rolls about its own length (before that spin: the roll is about the body, then the
    whole is turned) from 30 % on (`rest` is the pelvis bone's rest
    orientation in world space, which says which way is up; `a` and `b` are in the bone's frame).
    """
    import math

    if rest is None:
        return None
    base, goal = q_mul(rest, a), q_mul(rest, b)
    l1, l2 = q_rotate(base, (0.0, 1.0, 0.0)), q_rotate(goal, (0.0, 1.0, 0.0))
    if abs(l1[2]) > 0.6 or abs(l2[2]) > 0.6:
        return None
    yaw = math.atan2(l1[0] * l2[1] - l1[1] * l2[0], l1[0] * l2[0] + l1[1] * l2[1])
    residual = q_mul(q_about_z(-yaw), q_mul(goal, q_conj(base)))
    back = q_conj(rest)

    def at(t: float) -> Quat:
        spin = q_about_z(yaw * _smooth(t / 0.7))
        roll = q_slerp(IDENTITY_Q, residual, _smooth((t - 0.3) / 0.7))
        return q_mul(back, q_mul(q_mul(spin, roll), base))

    return at


def pelvis_rest(meta: dict) -> Quat | None:
    value = meta.get("pelvis_rest")
    return tuple(value) if value else None  # type: ignore[return-value]


def blend_bone(
    bone: str, a: Quat, b: Quat, t: float, turn: Callable[[float], Quat] | None
) -> Quat:
    """A bone's rotation `t` of the way from `a` to `b`: the pelvis goes by `turn` (`lying_turn`)
    when there is one, every other bone by the shortest arc."""
    if bone == "pelvis" and turn is not None:
        return turn(t)
    return q_slerp(a, b, t)


def reduce_keys(
    frames: list[float], values: list, distance, tolerance: float
) -> list[int]:
    """The indices of the samples worth keeping: a sample is dropped when interpolating
    between its kept neighbours stays within `tolerance` of it (`distance(a, b)` says how far
    apart two values are, `values` being quaternions or positions). Always keeps the first
    and last. Douglas-Peucker on the interpolation error."""
    if len(frames) <= 2:
        return list(range(len(frames)))

    def lerp(a, b, t):
        if isinstance(a, tuple) and len(a) == 4:
            return q_slerp(a, b, t)
        return tuple(x + (y - x) * t for x, y in zip(a, b))

    keep = {0, len(frames) - 1}
    stack = [(0, len(frames) - 1)]
    while stack:
        lo, hi = stack.pop()
        worst, worst_i = 0.0, -1
        for i in range(lo + 1, hi):
            t = (frames[i] - frames[lo]) / (frames[hi] - frames[lo])
            err = distance(lerp(values[lo], values[hi], t), values[i])
            if err > worst:
                worst, worst_i = err, i
        if worst > tolerance and worst_i >= 0:
            keep.add(worst_i)
            stack += [(lo, worst_i), (worst_i, hi)]
    return sorted(keep)


def close_loop(samples: list, blend: int, mix) -> list:
    """Makes a clip loop without a jump: the last `blend` samples are eased towards the first one,
    so the clip ends where it starts. `mix(a, b, t)` blends two samples."""
    if len(samples) <= blend + 1:
        return list(samples)
    out = list(samples)
    start = len(samples) - blend
    for i in range(start, len(samples)):
        out[i] = mix(samples[i], samples[0], (i - start + 1) / blend)
    return out


# --- converted clips ---------------------------------------------------------------------------

Vec = tuple[float, float, float]


@dataclass
class Clip:
    """A clip as samples, one per source frame, in the Quaternius rig's terms: each bone's
    rotation (what goes in its pose-bone `rotation_quaternion`) and how far the hips have
    moved from their rest position, in world axes."""

    frames: list[float]
    quats: dict[str, list[Quat]]
    hips: list[Vec]
    foot_clearance: float = (
        0.0  # how far the lowest foot is above the floor in the first frame
    )
    meta: dict = field(default_factory=dict)

    def copy(self) -> "Clip":
        return Clip(
            list(self.frames),
            {b: list(q) for b, q in self.quats.items()},
            list(self.hips),
            self.foot_clearance,
            dict(self.meta),
        )

    def __len__(self) -> int:
        return len(self.frames)


def trim_clip(clip: Clip, first: float, last: float) -> Clip:
    """The samples whose source frame is in first..last, restarted from frame 0."""
    keep = [i for i, f in enumerate(clip.frames) if first <= f <= last]
    if not keep:
        raise ValueError(f"no frames between {first} and {last}")
    lo = clip.frames[keep[0]]
    return Clip(
        [clip.frames[i] - lo for i in keep],
        {b: [q[i] for i in keep] for b, q in clip.quats.items()},
        [clip.hips[i] for i in keep],
        clip.foot_clearance,
        dict(clip.meta),
    )


def loop_clip(clip: Clip, blend: int = 12) -> Clip:
    """Eases the last `blend` samples back to the first, so that the clip ends where it started."""
    out = clip.copy()
    out.quats = {
        b: close_loop(q, blend, lambda a, c, t: q_slerp(a, c, t))
        for b, q in clip.quats.items()
    }
    out.hips = close_loop(clip.hips, blend, lambda a, c, t: tuple(x + (y - x) * t for x, y in zip(a, c)))  # type: ignore[arg-type]
    return out


def in_place_clip(clip: Clip) -> Clip:
    """Takes the hips' sideways and forward travel out (it keeps their height)."""
    out = clip.copy()
    start = clip.hips[0]
    out.hips = [(start[0], start[1], h[2]) for h in clip.hips]
    return out


def offset_hips(clip: Clip, offset: Vec) -> Clip:
    out = clip.copy()
    out.hips = [
        (h[0] + offset[0], h[1] + offset[1], h[2] + offset[2]) for h in clip.hips
    ]
    return out


def scale_intensity(clip: Clip, amount: float, speed: float = 1.0) -> Clip:
    """The same motion bigger (`amount` above 1) or smaller (below 1) about the clip's first
    pose, and faster (`speed` above 1) or slower: a calm or an eager version of one clip.
    """
    out = clip.copy()
    out.quats = {
        b: [q_slerp(q[0], x, amount) for x in q] for b, q in clip.quats.items()
    }
    first = clip.hips[0]
    out.hips = [tuple(f + (h - f) * amount for h, f in zip(hip, first)) for hip in clip.hips]  # type: ignore[misc]
    out.frames = [f / speed for f in clip.frames]
    return out


INTENSITY_VARIANTS: dict[str, tuple[float, float]] = {
    "Low": (0.55, 0.85),
    "High": (1.4, 1.15),
}  # (amount, speed)


def seat_offset(first_hips_world: Vec, target_world: Vec, foot_clearance: float) -> Vec:
    """How far to move a seated clip's hips so it sits where the sit clips seat the body, so there
    is no jump between them. The feet may not go through the floor: lowering the hips by more
    than the feet's clearance is cut short (the clip keeps the rest of its height difference).
    """
    dx, dy, dz = (t - f for t, f in zip(target_world, first_hips_world))
    if dz < 0 and foot_clearance + dz < 0:
        dz = -max(foot_clearance, 0.0)
    return (dx, dy, dz)


Pose = tuple[dict[str, Quat], Vec]  # every bone's rotation and where the hips are


def pose_at(clip: Clip, index: int) -> Pose:
    """The pose of sample `index` (negative counts from the end)."""
    return {b: q[index] for b, q in clip.quats.items()}, clip.hips[index]


def _mix_hips(a: Vec, b: Vec, t: float) -> Vec:
    return tuple(x + (y - x) * t for x, y in zip(a, b))  # type: ignore[return-value]


def ease_ends(clip: Clip, start: Pose | None, end: Pose | None, frames: int) -> Clip:
    """The clip with `frames` samples before it that ease in from the pose `start` and as many after
    it that ease out to the pose `end` (either may be None: that end is left alone). A clip that
    starts and ends in the pose another clip leaves the body in, so playing one after the other
    does not jump."""
    n = len(clip)
    step = (clip.frames[-1] - clip.frames[0]) / max(n - 1, 1)
    lead = frames if start is not None else 0
    tail = frames if end is not None else 0
    quats = {b: list(q) for b, q in clip.quats.items()}
    hips = list(clip.hips)
    if start is not None:
        ref_q, ref_h = start
        turn = (
            lying_turn(
                ref_q.get("pelvis", clip.quats["pelvis"][0]),
                clip.quats["pelvis"][0],
                pelvis_rest(clip.meta),
            )
            if "pelvis" in clip.quats
            else None
        )
        for b, q in clip.quats.items():
            quats[b] = [
                blend_bone(b, ref_q.get(b, q[0]), q[0], (k + 1) / (frames + 1), turn)
                for k in range(frames)
            ] + quats[b]
        hips = [
            _mix_hips(ref_h, clip.hips[0], (k + 1) / (frames + 1))
            for k in range(frames)
        ] + hips
    if end is not None:
        ref_q, ref_h = end
        turn = (
            lying_turn(
                clip.quats["pelvis"][-1],
                ref_q.get("pelvis", clip.quats["pelvis"][-1]),
                pelvis_rest(clip.meta),
            )
            if "pelvis" in clip.quats
            else None
        )
        for b, q in clip.quats.items():
            quats[b] += [
                blend_bone(b, q[-1], ref_q.get(b, q[-1]), (k + 1) / (frames + 1), turn)
                for k in range(frames)
            ]
        hips += [
            _mix_hips(clip.hips[-1], ref_h, (k + 1) / (frames + 1))
            for k in range(frames)
        ]
    out_frames = [step * k for k in range(lead + n + tail)]
    return Clip(out_frames, quats, hips, clip.foot_clearance, dict(clip.meta))


def blend_through(
    clip: Clip, ref_quats: dict[str, Quat], ref_hips: Vec, frames: int
) -> Clip:
    """The clip eased in from, and back out to, one reference pose: a seated variation that starts
    and ends in the pose the sit clips leave the body in, so playing it after one of them (and a
    sit loop after it) does not jump."""
    return ease_ends(clip, (ref_quats, ref_hips), (ref_quats, ref_hips), frames)


def reverse_clip(clip: Clip) -> Clip:
    """The clip played backwards (standing up becomes lying down)."""
    last = clip.frames[-1]
    out = clip.copy()
    out.frames = [last - f for f in reversed(clip.frames)]
    out.quats = {b: list(reversed(q)) for b, q in clip.quats.items()}
    out.hips = list(reversed(clip.hips))
    return out


def ping_pong(clip: Clip, slow: float = 1.0) -> Clip:
    """The clip out and back along the same path, `slow` times slower, so that it ends in the pose
    it began in (a roll in her sleep, not a snap back to the start). The time is eased (smoothstep),
    so the body is still at the start, at the turn and at the end instead of bouncing off the
    turn."""
    n = len(clip)
    if n < 2:
        return clip.copy()
    step = (clip.frames[-1] - clip.frames[0]) / (n - 1)
    total = max(2, round(2 * (n - 1) * max(slow, 0.1)) + 1)
    out = Clip(
        [], {b: [] for b in clip.quats}, [], clip.foot_clearance, dict(clip.meta)
    )
    for m in range(total):
        u = m / (total - 1)
        v = 1 - abs(2 * u - 1)  # 0 -> 1 -> 0
        w = v * v * (3 - 2 * v)
        position = w * (n - 1)
        lo = min(int(position), n - 2)
        t = position - lo
        out.frames.append(step * m)
        for b, q in clip.quats.items():
            out.quats[b].append(q_slerp(q[lo], q[lo + 1], t))
        out.hips.append(_mix_hips(clip.hips[lo], clip.hips[lo + 1], t))
    return out


def join_clips(parts: list[Clip], ease: int) -> Clip:
    """The clips one after another, with `ease` samples between each pair that ease from where the
    first ends to where the next begins. Sampled at the first clip's rate."""
    bones = set().union(*(p.quats for p in parts))
    identity: Quat = (1.0, 0.0, 0.0, 0.0)
    quats: dict[str, list[Quat]] = {b: [] for b in bones}
    hips: list[Vec] = []
    for index, part in enumerate(parts):
        if index and ease:
            first = pose_at(part, 0)
            before = {b: quats[b][-1] for b in bones}
            before_hips = hips[-1]
            turn = (
                lying_turn(
                    before["pelvis"],
                    first[0].get("pelvis", before["pelvis"]),
                    pelvis_rest(parts[0].meta),
                )
                if "pelvis" in bones
                else None
            )
            for k in range(ease):
                t = (k + 1) / (ease + 1)
                for b in bones:
                    quats[b].append(
                        blend_bone(b, before[b], first[0].get(b, before[b]), t, turn)
                    )
                hips.append(_mix_hips(before_hips, first[1], t))
        for j in range(len(part)):
            for b in bones:
                quats[b].append(part.quats[b][j] if b in part.quats else identity)
            hips.append(part.hips[j])
    step = (parts[0].frames[-1] - parts[0].frames[0]) / max(len(parts[0]) - 1, 1)
    return Clip(
        [step * k for k in range(len(hips))],
        quats,
        hips,
        parts[0].foot_clearance,
        dict(parts[0].meta),
    )


def finish_clips(
    made: dict[str, tuple[Clip, dict]], derived: list[dict], references: dict[str, Pose]
) -> dict[str, tuple[Clip, dict]]:
    """The converted clips joined up: {name: (clip, options)} in, the same plus the derived clips out.
    `ease_from` / `ease_to` name the clip (or `@name`, one of `references`) whose first pose this clip
    eases in from / out to, `blend_ref` does both to one pose, over `ease_frames`; `compose` (derived
    clips) joins the clips named, a `[name, repeat]` pair repeating one; `hold` is a pose held, to be
    eased into or out of. Mirrored clips (`_Mirror`) refer to the mirrored ones. A reference that is
    not there is ignored (the pack may lack it)."""

    def reference(key: str | None, suffix: str) -> Pose | None:
        if not key:
            return None
        if key.startswith("@"):
            return references.get(key)
        other = made.get(key + suffix)
        return pose_at(other[0], 0) if other else None

    def settle(clip: Clip, options: dict, suffix: str) -> Clip:
        frames = int(options.get("ease_frames", 24))
        if options.get("blend_ref"):
            pose = reference(options["blend_ref"], suffix)
            return ease_ends(clip, pose, pose, frames) if pose else clip
        start, end = reference(options.get("ease_from"), suffix), reference(
            options.get("ease_to"), suffix
        )
        return ease_ends(clip, start, end, frames) if start or end else clip

    out: dict[str, tuple[Clip, dict]] = {}
    for name, (clip, options) in made.items():
        suffix = MIRROR_SUFFIX if name.endswith(MIRROR_SUFFIX) else ""
        out[name] = (settle(clip, options, suffix), options)
    for entry in derived:
        options = entry["options"]
        if options.get("hold"):
            if options["hold"] in made:
                held = pose_at(made[options["hold"]][0], 0)
                still = Clip(
                    [0.0, 1.0],
                    {b: [q, q] for b, q in held[0].items()},
                    [held[1], held[1]],
                )
                out[entry["name"]] = (settle(still, options, ""), options)
            continue
        parts: list[Clip] = []
        for item in options["compose"]:
            part, repeat = (item, 1) if isinstance(item, str) else item
            if part not in made:
                break
            parts += [made[part][0]] * int(repeat)
        else:
            out[entry["name"]] = (
                settle(join_clips(parts, int(options.get("ease", 14))), options, ""),
                options,
            )
    return out
