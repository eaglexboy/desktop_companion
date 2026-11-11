"""
Animation clips: the generated idle and talk loops, the skeletal clips of an
imported rig, and the limb poses baked as morph targets.
"""

from __future__ import annotations

import math

import bpy  # pyright: ignore[reportMissingImports]
import mathutils  # pyright: ignore[reportMissingImports]

from blender.gen_constants import (
    AUXILIARY_CLIPS,
    CORE_IDLE_CLIPS,
    DELIVERY_CLIPS,
    MOOD_IDLE_CLIPS,
    STATE_LOOP_CLIPS,
)


def _insert_bone_keyframes(
    arm_obj, bone_name, frame, *, rot_euler=None, scale=None
):
    pbone = arm_obj.pose.bones.get(bone_name)
    if pbone is None:
        return
    if rot_euler is not None:
        pbone.rotation_mode = "XYZ"
        pbone.rotation_euler = rot_euler
        pbone.keyframe_insert(data_path="rotation_euler", frame=frame)
    if scale is not None:
        pbone.scale = scale
        pbone.keyframe_insert(data_path="scale", frame=frame)


_CLIP_SKIP: frozenset = (
    frozenset()
)  # clip names build_all_clips leaves to a better version (see build_imported_clips)


def bake_armature_clip(arm_obj, clip_name: str, keyframes: dict) -> None:
    """`keyframes`: {frame: {bone_name: {"rot": (x,y,z) rad, "scale": (x,y,z)}}}"""
    if clip_name in _CLIP_SKIP:
        return
    action = bpy.data.actions.new(clip_name)
    arm_obj.animation_data_create()
    arm_obj.animation_data.action = action
    for frame, bones in keyframes.items():
        bpy.context.scene.frame_set(frame)
        for bone_name, ops in bones.items():
            _insert_bone_keyframes(
                arm_obj,
                bone_name,
                frame,
                rot_euler=ops.get("rot"),
                scale=ops.get("scale"),
            )
    action.use_fake_user = True


def bake_object_clip(obj, clip_name: str, keyframes: dict) -> None:
    """`keyframes`: {frame: (sx, sy, sz)} applied to obj.scale."""
    if clip_name in _CLIP_SKIP:
        return
    action = bpy.data.actions.new(f"{clip_name}.{obj.name}")
    obj.animation_data_create()
    obj.animation_data.action = action
    for frame, scale in keyframes.items():
        bpy.context.scene.frame_set(frame)
        obj.scale = scale
        obj.keyframe_insert(data_path="scale", frame=frame)
    action.use_fake_user = True


R = math.radians


def build_all_clips(
    arm_obj,
    eyes,
    *,
    has_arms: bool,
    has_legs: bool,
    skip: frozenset = frozenset(),
) -> list[str]:
    """Bakes the full animation catalog, applying capability-degradation
    fallbacks: arm-dependent clips (greet/celebrate/shrug) are omitted
    cleanly when has_arms is False; move falls back to squash-and-stretch
    hopping when has_legs is False (every Tier 3 primitive lacks legs, so
    this is always the active path for procedural bodies); think engages a
    pulsing spine oscillation regardless of arms. Returns the list of clip
    names actually baked (a `skip`ped clip is still listed: whoever baked it better
    owns its action)."""
    global _CLIP_SKIP
    _CLIP_SKIP = skip
    try:
        return _bake_catalog(
            arm_obj, eyes, has_arms=has_arms, has_legs=has_legs
        )
    finally:
        _CLIP_SKIP = frozenset()


def _bake_catalog(
    arm_obj, eyes, *, has_arms: bool, has_legs: bool
) -> list[str]:
    baked: list[str] = []

    # --- core idles ---
    bake_armature_clip(
        arm_obj,
        "idle_lookaround",
        {
            1: {"head": {"rot": (0, 0, 0)}},
            30: {"head": {"rot": (0, 0, R(25))}},
            60: {"head": {"rot": (0, 0, R(-25))}},
            90: {"head": {"rot": (0, 0, 0)}},
        },
    )
    baked.append("idle_lookaround")
    bake_armature_clip(
        arm_obj,
        "idle_stretch",
        {
            1: {"spine": {"scale": (1, 1, 1)}},
            20: {"spine": {"scale": (1.05, 1.05, 1.15)}},
            40: {"spine": {"scale": (1, 1, 1)}},
        },
    )
    baked.append("idle_stretch")
    bake_armature_clip(
        arm_obj,
        "idle_peek",
        {
            1: {"head": {"rot": (0, 0, 0)}},
            15: {"head": {"rot": (R(10), 0, R(15))}},
            45: {"head": {"rot": (R(10), 0, R(15))}},
            60: {"head": {"rot": (0, 0, 0)}},
        },
    )
    baked.append("idle_peek")
    for eye in eyes:
        bake_object_clip(
            eye,
            "idle_blink",
            {1: (1, 1, 1), 10: (1, 1, 1), 12: (1, 1, 0.1), 14: (1, 1, 1)},
        )
    if eyes:
        baked.append("idle_blink")

    # --- state loops ---
    if has_legs:
        bake_armature_clip(
            arm_obj,
            "move",
            {
                1: {"root": {"scale": (1, 1, 1)}},
                15: {"root": {"scale": (1, 1, 1)}},
            },
        )
    else:
        # hop/hover fallback for legless bodies
        bake_armature_clip(
            arm_obj,
            "move",
            {
                1: {"root": {"scale": (1, 1, 1)}},
                8: {"root": {"scale": (1.05, 1.05, 0.9)}},
                15: {"root": {"scale": (1, 1, 1)}},
            },
        )
    baked.append("move")

    if has_arms:
        bake_armature_clip(
            arm_obj,
            "think",
            {
                1: {"head": {"rot": (0, 0, 0)}},
                40: {"head": {"rot": (0, R(15), R(-10))}},
                80: {"head": {"rot": (0, 0, 0)}},
            },
        )
    else:
        bake_armature_clip(
            arm_obj,
            "think",
            {
                1: {"spine": {"scale": (1, 1, 1)}},
                40: {"spine": {"scale": (1.03, 1.03, 0.98)}},
                80: {"spine": {"scale": (1, 1, 1)}},
            },
        )
    baked.append("think")

    # --- mood idles ---
    bake_armature_clip(
        arm_obj,
        "idle_bored",
        {
            1: {"head": {"rot": (0, 0, 0)}},
            75: {"head": {"rot": (R(-5), 0, 0)}},
            150: {"head": {"rot": (0, 0, 0)}},
        },
    )
    baked.append("idle_bored")
    bake_armature_clip(
        arm_obj,
        "idle_curious",
        {
            1: {"head": {"rot": (0, 0, 0)}},
            60: {"head": {"rot": (R(8), 0, R(12))}},
            120: {"head": {"rot": (0, 0, 0)}},
        },
    )
    baked.append("idle_curious")
    bake_armature_clip(
        arm_obj,
        "idle_sleepy",
        {
            1: {"head": {"rot": (0, 0, 0)}},
            90: {"head": {"rot": (R(15), 0, 0)}},
            180: {"head": {"rot": (0, 0, 0)}},
        },
    )
    baked.append("idle_sleepy")
    bake_armature_clip(
        arm_obj,
        "idle_fidget",
        {
            1: {"spine": {"rot": (0, 0, 0)}},
            45: {"spine": {"rot": (0, 0, R(5))}},
            90: {"spine": {"rot": (0, 0, 0)}},
        },
    )
    baked.append("idle_fidget")
    bake_armature_clip(
        arm_obj,
        "idle_focused",
        {
            1: {"head": {"rot": (0, 0, 0)}},
            60: {"head": {"rot": (0, 0, R(3))}},
            120: {"head": {"rot": (0, 0, 0)}},
        },
    )
    baked.append("idle_focused")

    # --- conversational delivery gestures ---
    bake_armature_clip(
        arm_obj,
        "talk_subdued",
        {
            1: {"head": {"rot": (0, 0, 0)}},
            30: {"head": {"rot": (R(-3), 0, 0)}},
            60: {"head": {"rot": (0, 0, 0)}},
        },
    )
    baked.append("talk_subdued")
    bake_armature_clip(
        arm_obj,
        "talk_animated",
        {
            1: {"head": {"rot": (0, 0, 0)}},
            30: {"head": {"rot": (R(8), 0, R(10))}},
            60: {"head": {"rot": (0, 0, 0)}},
        },
    )
    baked.append("talk_animated")
    bake_armature_clip(
        arm_obj,
        "talk_explanatory",
        {
            1: {"head": {"rot": (0, 0, 0)}},
            37: {"head": {"rot": (0, R(10), R(-8))}},
            75: {"head": {"rot": (0, 0, 0)}},
        },
    )
    baked.append("talk_explanatory")
    bake_armature_clip(
        arm_obj,
        "talk_deadpan",
        {
            1: {"head": {"rot": (0, 0, 0)}},
            30: {"head": {"rot": (0, 0, 0)}},
            60: {"head": {"rot": (0, 0, 0)}},
        },
    )
    baked.append("talk_deadpan")

    # --- auxiliary gestures (arm-dependent ones omitted cleanly if no arms) ---
    if has_arms:
        bake_armature_clip(
            arm_obj,
            "greet",
            {
                1: {"arm_R": {"rot": (0, 0, 0)}},
                15: {"arm_R": {"rot": (0, 0, R(120))}},
                30: {"arm_R": {"rot": (0, R(30), R(90))}},
                45: {"arm_R": {"rot": (0, 0, R(120))}},
                54: {"arm_R": {"rot": (0, 0, 0)}},
            },
        )
        baked.append("greet")
        bake_armature_clip(
            arm_obj,
            "shrug",
            {
                1: {"arm_L": {"rot": (0, 0, 0)}, "arm_R": {"rot": (0, 0, 0)}},
                22: {
                    "arm_L": {"rot": (0, 0, R(-40))},
                    "arm_R": {"rot": (0, 0, R(40))},
                },
                45: {"arm_L": {"rot": (0, 0, 0)}, "arm_R": {"rot": (0, 0, 0)}},
            },
        )
        baked.append("shrug")
    bake_armature_clip(
        arm_obj,
        "celebrate",
        {
            1: {"root": {"scale": (1, 1, 1)}},
            15: {"root": {"scale": (1.1, 1.1, 0.85)}},
            30: {"root": {"scale": (0.95, 0.95, 1.1)}},
            44: {"root": {"scale": (1, 1, 1)}},
        },
    )
    baked.append("celebrate")
    bake_armature_clip(
        arm_obj,
        "agree",
        {
            1: {"head": {"rot": (0, 0, 0)}},
            12: {"head": {"rot": (R(15), 0, 0)}},
            24: {"head": {"rot": (0, 0, 0)}},
            36: {"head": {"rot": (R(15), 0, 0)}},
        },
    )
    baked.append("agree")
    bake_armature_clip(
        arm_obj,
        "disagree",
        {
            1: {"head": {"rot": (0, 0, 0)}},
            12: {"head": {"rot": (0, R(20), 0)}},
            24: {"head": {"rot": (0, 0, 0)}},
            36: {"head": {"rot": (0, R(20), 0)}},
        },
    )
    baked.append("disagree")

    return baked


# name: (loops, seconds, {frame: {role: (yaw, pitch, roll) in degrees}}); the first and last
# frames are the rest pose, so a loop is seamless and a stopped clip rewinds to rest.
_SKELETAL_CLIPS: dict[str, tuple[bool, float, dict]] = {
    "idle_lookaround": (
        True,
        4.0,
        {
            1: {"head": (0, 0, 0)},
            31: {"head": (28, 0, 0)},
            91: {"head": (-28, 0, 0)},
            121: {"head": (0, 0, 0)},
        },
    ),
    "idle_stretch": (
        True,
        3.0,
        {
            1: {"head": (0, 0, 0), "chest": (0, 0, 0)},
            36: {"head": (0, -12, 0), "chest": (0, -9, 0)},
            61: {"head": (0, -12, 0), "chest": (0, -9, 0)},
            91: {"head": (0, 0, 0), "chest": (0, 0, 0)},
        },
    ),
    "idle_peek": (
        False,
        2.5,
        {
            1: {"head": (0, 0, 0)},
            16: {"head": (20, 9, 6)},
            56: {"head": (20, 9, 6)},
            76: {"head": (0, 0, 0)},
        },
    ),
    "idle_sleepy": (
        True,
        6.0,
        {
            1: {"head": (0, 0, 0), "chest": (0, 0, 0)},
            91: {"head": (0, 18, 0), "chest": (0, 5, 0)},
            181: {"head": (0, 0, 0), "chest": (0, 0, 0)},
        },
    ),
    "idle_fidget": (
        True,
        3.0,
        {
            1: {"chest": (0, 0, 0), "head": (0, 0, 0)},
            23: {"chest": (7, 0, 0), "head": (-4, 0, 0)},
            68: {"chest": (-7, 0, 0), "head": (4, 0, 0)},
            91: {"chest": (0, 0, 0), "head": (0, 0, 0)},
        },
    ),
    "agree": (
        False,
        1.2,
        {
            1: {"head": (0, 0, 0)},
            10: {"head": (0, 16, 0)},
            19: {"head": (0, 0, 0)},
            28: {"head": (0, 16, 0)},
            37: {"head": (0, 0, 0)},
        },
    ),
    "disagree": (
        False,
        1.2,
        {
            1: {"head": (0, 0, 0)},
            10: {"head": (22, 0, 0)},
            19: {"head": (-22, 0, 0)},
            28: {"head": (22, 0, 0)},
            37: {"head": (0, 0, 0)},
        },
    ),
}


def skeletal_bones(arm_obj, socket_bones: dict) -> dict:
    """{role: [(bone, share)]} for the roles the skeletal clips use, from the rig's own
    bones: the head (with the neck taking part of a turn) and the chest."""
    bones = {b.name for b in arm_obj.data.bones}
    head = socket_bones.get("socket_head_top")
    neck = socket_bones.get("socket_neck")
    chest = socket_bones.get("socket_chest")
    roles: dict[str, list] = {}
    # A rig with no head of its own lets its trunk stand in for the head sockets (sockets.py);
    # turning that "head" would swivel the whole body, so it takes no skeletal idles.
    if head is not None and head in bones and "head" in head.lower():
        roles["head"] = (
            [(head, 0.65), (neck, 0.35)]
            if neck in bones and neck != head
            else [(head, 1.0)]
        )
    if chest in bones and chest != head:
        roles["chest"] = [(chest, 1.0)]
    return roles


def _world_turn_in_bone(
    arm_obj, bone, yaw: float, pitch: float, roll: float
) -> mathutils.Quaternion:
    """The pose rotation of `bone` that turns it by yaw (about world Z), pitch (about X) and
    roll (about Y), in radians, as a quaternion in the bone's own rest frame."""
    world = (
        mathutils.Quaternion((0, 0, 1), yaw)
        @ mathutils.Quaternion((1, 0, 0), pitch)
        @ mathutils.Quaternion((0, 1, 0), roll)
    )
    return _world_quat_in_bone(arm_obj, bone, world)


def _world_quat_in_bone(
    arm_obj, bone, world: mathutils.Quaternion
) -> mathutils.Quaternion:
    """A rotation given in world axes, as the pose rotation of `bone` in its own rest frame."""
    to_armature = arm_obj.matrix_world.to_3x3().normalized().inverted()
    frame = (
        bone.matrix_local.to_3x3().normalized()
    ).to_quaternion()  # the bone's rest orientation in the armature
    world_in_armature = (
        to_armature.to_quaternion()
        @ world
        @ to_armature.to_quaternion().inverted()
    )
    return frame.inverted() @ world_in_armature @ frame


def build_imported_clips(arm_obj, eyes, socket_bones: dict) -> list[str]:
    """The skeletal idles for an imported rig plus the lids' blink, on the rig's own bones.
    Returns the clip names baked ([] when the rig has no head bone to turn)."""
    roles = skeletal_bones(arm_obj, socket_bones)
    if "head" not in roles:
        return []
    baked: list[str] = []
    arm_obj.animation_data_create()
    for name, (*_, frames) in _SKELETAL_CLIPS.items():
        used = {role for keys in frames.values() for role in keys}
        if not used <= set(roles):
            continue  # needs a chest bone this rig does not have
        action = bpy.data.actions.new(name)
        arm_obj.animation_data.action = action
        for frame, turns in frames.items():
            for role, (yaw, pitch, roll) in turns.items():
                for bone_name, share in roles[role]:
                    pbone = arm_obj.pose.bones[bone_name]
                    pbone.rotation_mode = "QUATERNION"
                    pbone.rotation_quaternion = _world_turn_in_bone(
                        arm_obj,
                        arm_obj.data.bones[bone_name],
                        math.radians(yaw * share),
                        math.radians(pitch * share),
                        math.radians(roll * share),
                    )
                    pbone.keyframe_insert(
                        data_path="rotation_quaternion", frame=frame
                    )
        action.use_fake_user = True
        baked.append(name)
    for pbone in arm_obj.pose.bones:  # back to rest for whatever bakes next
        pbone.rotation_quaternion = (1, 0, 0, 0)
    for eye in eyes:
        bake_object_clip(
            eye,
            "idle_blink",
            {1: (1, 1, 1), 10: (1, 1, 1), 12: (1, 1, 0.1), 14: (1, 1, 1)},
        )
    if eyes:
        baked.append("idle_blink")
    return baked


_ALL_CLIP_META: dict[str, tuple[bool, float]] = {
    name: (loop, duration_s)
    for name, loop, duration_s in (
        *CORE_IDLE_CLIPS,
        *STATE_LOOP_CLIPS,
        *MOOD_IDLE_CLIPS,
        *DELIVERY_CLIPS,
        *AUXILIARY_CLIPS,
    )
}


_LIMB_NAME_PARTS = {
    "upper_arm": (
        "upper_arm",
        "upperarm",
        "arm_stretch",
    ),  # arm_stretch: Auto-Rig Pro's names
    "forearm": ("forearm", "lowerarm", "lower_arm"),
    "thigh": ("thigh", "upleg", "upperleg", "upper_leg"),
    "shin": ("shin", "calf", "lowerleg", "lower_leg", "leg_stretch"),
    "foot": ("foot", "ankle"),
}


_LIMB_NAME_SKIP = ("end", "twist", "ik", "pole", "helper", "toe")


LIMB_MIN_DISPLACEMENT = (
    0.01  # metres a mesh must move in some pose to get a target
)


RUN_KEYS = ("run_a", "run_b")


GESTURE_KEYS = (
    "gesture_l_out",
    "gesture_l_in",
    "gesture_r_out",
    "gesture_r_in",
)


def find_limb_bones(arm_obj) -> dict:
    """{role: {"min_x": bone_name, "max_x": bone_name}} for the arm and leg bones,
    sides told apart by where they sit (the min-x one is the avatar's right)."""
    found: dict[str, list] = {role: [] for role in _LIMB_NAME_PARTS}
    for bone in arm_obj.data.bones:
        lowered = bone.name.lower()
        if any(part in lowered for part in _LIMB_NAME_SKIP):
            continue
        for role, parts in _LIMB_NAME_PARTS.items():
            if role == "upper_arm" and "forearm" in lowered:
                continue  # forearm_stretch is the forearm, though it contains arm_stretch
            if any(part in lowered for part in parts):
                found[role].append((bone.head_local.x, bone.name))
    roles = {}
    for role, entries in found.items():
        if len(entries) == 2:
            entries.sort()
            roles[role] = {"min_x": entries[0][1], "max_x": entries[1][1]}
    return roles


def _feet_follow(
    arm_obj, roles: dict, side: str, thigh_angle: float, shin_angle: float, axis
) -> tuple:
    """A foot bone parented to the root rather than the shin (common in rigs
    exported with control bones) stays put while the leg swings, stretching the
    shoe. Returns (foot rotation about `axis`, offset in armature space) that carry
    it to where the leg's own motion puts the ankle; (0, zero) when the foot already
    rides on the shin."""
    if "foot" not in roles:
        return 0.0, (0.0, 0.0, 0.0)
    bones = arm_obj.data.bones
    foot, shin, thigh = (
        bones[roles["foot"][side]],
        bones[roles["shin"][side]],
        bones[roles["thigh"][side]],
    )
    if any(parent == shin for parent in foot.parent_recursive):
        return 0.0, (0.0, 0.0, 0.0)
    arm_axis = (
        arm_obj.matrix_world.to_3x3().normalized().inverted()
        @ mathutils.Vector(axis)
    )
    rot_thigh = mathutils.Quaternion(arm_axis, thigh_angle)
    rot_both = mathutils.Quaternion(arm_axis, thigh_angle + shin_angle)
    knee = thigh.head_local + rot_thigh @ (shin.head_local - thigh.head_local)
    ankle = knee + rot_both @ (foot.head_local - shin.head_local)
    return thigh_angle + shin_angle, tuple(ankle - foot.head_local)


def _deformed_points(mesh) -> list:
    evaluated = mesh.evaluated_get(bpy.context.evaluated_depsgraph_get())
    data = evaluated.to_mesh()
    points = [v.co.copy() for v in data.vertices]
    evaluated.to_mesh_clear()
    return points


def _capture_pose(
    arm_obj, meshes, rotations: list, offsets: dict | None = None
) -> dict:
    """Poses the rig (`rotations`: [(bone, world_axis, angle)], `offsets`:
    {bone: armature-space offset}), reads every mesh's deformed vertices, and puts
    the rig back to rest."""
    touched = {name for name, *_ in rotations} | set(offsets or {})
    turns: dict[str, mathutils.Quaternion] = {}
    for (
        name,
        axis,
        angle,
    ) in (
        rotations
    ):  # several turns of one bone compose, each about world axes, in order
        turn = mathutils.Quaternion(axis, angle)
        turns[name] = turn @ turns[name] if name in turns else turn
    for name, turn in turns.items():
        pbone = arm_obj.pose.bones[name]
        pbone.rotation_mode = "QUATERNION"
        pbone.rotation_quaternion = _world_quat_in_bone(
            arm_obj, arm_obj.data.bones[name], turn
        )
    for name, delta in (offsets or {}).items():
        bone = arm_obj.data.bones[name]
        arm_obj.pose.bones[
            name
        ].location = bone.matrix_local.to_3x3().normalized().inverted() @ mathutils.Vector(
            delta
        )
    bpy.context.view_layer.update()
    points = {mesh.name: _deformed_points(mesh) for mesh in meshes}
    for name in touched:
        arm_obj.pose.bones[name].rotation_quaternion = (1, 0, 0, 0)
        arm_obj.pose.bones[name].location = (0, 0, 0)
    bpy.context.view_layer.update()
    return points


def _add_pose_targets(
    meshes, rest: dict, poses: dict, existing_keys: frozenset = frozenset()
) -> list:
    """Adds one morph target per pose (`poses`: {key name: captured points}) to every
    mesh some pose moves by more than LIMB_MIN_DISPLACEMENT. Returns the meshes that
    got them. A mesh that already carries shape keys is skipped unless they are all
    in `existing_keys` (the run poses, when the gestures are added after them: the
    overlay reads a mesh's targets by count and order, so run first, then gestures,
    and nothing else)."""
    gained = []
    for mesh in meshes:
        moved = max(
            (pose[mesh.name][i] - rest[mesh.name][i]).length
            for pose in poses.values()
            for i in range(len(rest[mesh.name]))
        )
        if moved < LIMB_MIN_DISPLACEMENT:
            continue
        keys = mesh.data.shape_keys
        if keys is not None and not {kb.name for kb in keys.key_blocks} <= {
            "Basis",
            *existing_keys,
        }:
            continue
        if keys is None:
            mesh.shape_key_add(name="Basis")
        for name, pose in poses.items():
            key = mesh.shape_key_add(name=name)
            key.value = 0.0
            for i, point in enumerate(pose[mesh.name]):
                key.data[i].co = point
        gained.append(mesh)
    return gained


def bake_limb_morphs(
    arm_obj, meshes, warnings: list[str], humanoid_arms: bool = False
) -> tuple[list[str], list[str]]:
    """Run poses for the legs and arm-gesture poses for the arms as morph targets on
    the skinned meshes they deform. Returns (run key names, gesture key names), each
    empty when the rig lacks the bones. `humanoid_arms` is for a body whose arms were brought
    down to hang (relax_arms): the arms swing against the legs in the run poses, and the
    gestures bend the elbow forward like a person's. A model with an arm pose of its own
    keeps the older poses, tuned for such arms."""
    roles = find_limb_bones(arm_obj)
    lateral, forward = (1.0, 0.0, 0.0), (0.0, 1.0, 0.0)
    skinned = [
        m
        for m in meshes
        if any(
            md.type == "ARMATURE" and md.object == arm_obj for md in m.modifiers
        )
    ]
    bpy.context.view_layer.update()
    rest = {m.name: _deformed_points(m) for m in skinned}
    run_keys: list[str] = []
    gesture_keys: list[str] = []
    run_meshes: list = []

    if "thigh" in roles and "shin" in roles:

        def stride(ahead: str):
            """One leg forward (thigh swung forward, knee a little bent), the other
            trailing (thigh back, heel kicked up). Forward is -Y, a negative turn about +X.
            """
            rotations, offsets = [], {}
            for side in ("min_x", "max_x"):
                leading = side == ahead
                thigh_angle = -R(38) if leading else R(30)
                shin_angle = R(25) if leading else R(70)
                rotations += [
                    (roles["thigh"][side], lateral, thigh_angle),
                    (roles["shin"][side], lateral, shin_angle),
                ]
                foot_angle, foot_offset = _feet_follow(
                    arm_obj, roles, side, thigh_angle, shin_angle, lateral
                )
                if "foot" in roles and (
                    foot_angle or foot_offset != (0.0, 0.0, 0.0)
                ):
                    rotations.append((roles["foot"][side], lateral, foot_angle))
                    offsets[roles["foot"][side]] = foot_offset
                if (
                    humanoid_arms
                    and "upper_arm" in roles
                    and "forearm" in roles
                ):
                    # The arms swing against the legs: the one on the side of the leading leg goes
                    # back, the other comes forward with the elbow bent.
                    rotations.append(
                        (
                            roles["upper_arm"][side],
                            lateral,
                            R(22) if leading else -R(28),
                        )
                    )
                    rotations.append(
                        (
                            roles["forearm"][side],
                            lateral,
                            -R(12) if leading else -R(38),
                        )
                    )
            return rotations, offsets

        poses = {
            name: _capture_pose(arm_obj, skinned, *stride(ahead))
            for name, ahead in zip(RUN_KEYS, ("min_x", "max_x"))
        }
        run_meshes = _add_pose_targets(skinned, rest, poses)
        if run_meshes:
            run_keys = list(RUN_KEYS)
    else:
        warnings.append(
            "model rig has no thigh/shin bones to run with; it keeps the hop"
        )

    if "upper_arm" in roles and "forearm" in roles:
        ua, fa = roles["upper_arm"], roles["forearm"]
        # Turning about world Y swings a limb sideways, which is what reads from the
        # front. Which sign is "out" depends on the pose the model rests in; these were
        # chosen by looking at it, and the rest pose is the middle of each pair.
        arm_poses = {
            "gesture_l_out": [
                (ua["min_x"], forward, R(34)),
                (fa["min_x"], forward, R(-60)),
            ],
            "gesture_l_in": [
                (ua["min_x"], forward, R(-24)),
                (fa["min_x"], forward, R(50)),
            ],
            "gesture_r_out": [
                (ua["max_x"], forward, R(-24)),
                (fa["max_x"], forward, R(-34)),
            ],
            "gesture_r_in": [
                (ua["max_x"], forward, R(16)),
                (fa["max_x"], forward, R(20)),
            ],
        }
        if humanoid_arms:
            # The overlay blends each arm between its "out" and "in" pose about once a second, so
            # both must be gestures a person makes: the elbow bends forward (about the lateral axis,
            # as an elbow hinges) and the upper arm moves only a little, which also keeps the shoulder
            # and torso skin from being dragged. "out" brings the hand up and a touch away from the
            # body; "in" is a loose fold. Turning about +Y takes the -X arm outward.
            def talking_arm(
                side: str, out: float, forward_swing: float, bend: float
            ) -> list:
                outward = 1.0 if side == "min_x" else -1.0
                return [
                    (ua[side], forward, outward * R(out)),
                    (ua[side], lateral, -R(forward_swing)),
                    (fa[side], lateral, -R(bend)),
                ]

            arm_poses = {
                "gesture_l_out": talking_arm("min_x", 14, 20, 72),
                "gesture_l_in": talking_arm("min_x", 3, 8, 28),
                "gesture_r_out": talking_arm("max_x", 14, 20, 72),
                "gesture_r_in": talking_arm("max_x", 3, 8, 28),
            }
        captured = {
            name: _capture_pose(arm_obj, skinned, rotations)
            for name, rotations in arm_poses.items()
        }
        # A mesh that also has the run poses gets the gestures after them (one mesh is often the
        # whole body, moved by legs and arms alike).
        arm_meshes = _add_pose_targets(
            skinned, rest, captured, frozenset(RUN_KEYS)
        )
        if arm_meshes:
            gesture_keys = list(GESTURE_KEYS)
    else:
        warnings.append(
            "model rig has no upper_arm/forearm bones to gesture with"
        )

    return run_keys, gesture_keys


def clip_metadata(baked_names: list[str]) -> list[dict]:
    return [
        {
            "name": name,
            "loop": _ALL_CLIP_META[name][0],
            "duration_s": _ALL_CLIP_META[name][1],
        }
        for name in baked_names
        if name in _ALL_CLIP_META
    ]
