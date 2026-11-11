"""Animation libraries and outfit models worn by an imported body."""

from __future__ import annotations

import math
import re
from pathlib import Path

import bmesh  # pyright: ignore[reportMissingImports]
import bpy  # pyright: ignore[reportMissingImports]
import mathutils  # pyright: ignore[reportMissingImports]

from blender.animation_library import (
    SIT_PROP_NODE,
    library_contents,
    library_extras,
    library_folder,
    library_gaps,
    library_time_scale,
    merge_picks,
    pick_clips,
)
from blender.frame_fit import FRAME_MARGIN, solve_fit
from blender.gen_accessory_models import _join_objects
from blender.gen_garments import _BODY
from blender.gen_helpers import (
    hex_to_rgba,
)
from blender.gen_imported import (
    _bake_import_pose,
    _import_objects,
    _refresh_bounds,
)


def plan_animation_library(
    deploy_dir: Path, armature, model_path: Path | None = None
) -> dict[str, tuple[Path, str]]:
    """{role: (library file, clip)} for the clips an installed library can give `armature`: the
    roles whose clip exists in a library the body has the core bones of (a body may lack some,
    such as fingers; `wear_animation_library` drops those tracks). Empty when there is no
    library or none fits (a mech's or a mascot's rig has other bone names). A body converted from
    another rig (`model_path`'s `.rig.json`) plays the libraries retargeted onto it.
    """
    folder = library_folder(deploy_dir, model_path)
    if armature is None or not folder.is_dir():
        return {}
    ours = {bone.name for bone in armature.data.bones}
    plan: dict[str, tuple[Path, str]] = {}
    for path in sorted(folder.glob("*.glb")):
        try:
            joints, clips = library_contents(path)
        except (OSError, ValueError):
            continue
        if library_gaps(joints, ours) is None:
            continue
        # roles with options take this library's clips as more of them; the others stay with the
        # first library that fills them (a sit set is all from one library)
        held = {
            key for key in plan if key.split(".")[0] not in library_extras()
        }
        merge_picks(plan, pick_clips(clips, held), path)
    return plan


def _pose_state(arm_obj):
    """What `_restore_pose` needs to put `arm_obj`'s animation back as it is now."""
    animation = arm_obj.animation_data_create()
    return (animation.action, getattr(animation, "action_slot", None))


def _sit_pose(arm_obj, action_name: str):
    """Poses `arm_obj` at the first frame of a clip; returns what to hand to `_restore_pose`."""
    action = bpy.data.actions[action_name]
    animation = arm_obj.animation_data_create()
    before = (animation.action, getattr(animation, "action_slot", None))
    animation.action = action
    if getattr(animation, "action_slot", None) is None and action.slots:
        animation.action_slot = action.slots[0]
    bpy.context.scene.frame_set(int(action.frame_range[0]))
    bpy.context.view_layer.update()
    return before


def _restore_pose(arm_obj, before) -> None:
    animation = arm_obj.animation_data
    animation.action = before[0]
    if before[1] is not None:
        animation.action_slot = before[1]
    bpy.context.scene.frame_set(1)
    bpy.context.view_layer.update()


def build_sit_prop(
    arm_obj, sit_clip: str, enter_clip: str, wood: str = "#7a5636"
) -> bool:
    """A plain chair (seat, four legs, a low back) placed under the body the way `sit_clip`
    sits it, for the sit clips that are made for a chair. Its size comes from the posed body
    (a thigh's length against the mannequin's 0.4 m; the lowest deformed vertex under the
    hips is where the seat's top goes), so it fits whichever body wears the clip. Named
    SIT_PROP_NODE; the overlay keeps it hidden except while the body sits. The seat starts
    just behind where the body stands when `enter_clip` begins, so it stands in front of the
    chair and steps back onto it rather than starting in the middle of it. False when the
    rig lacks the bones to place it."""
    bones = arm_obj.pose.bones
    if not all(name in bones for name in ("pelvis", "thigh_l", "calf_l")):
        return False
    before = _sit_pose(arm_obj, sit_clip)
    try:
        world = arm_obj.matrix_world
        pelvis = world @ bones["pelvis"].head
        hip = world @ bones["thigh_l"].head
        knee = world @ bones["calf_l"].head
        k = max(
            (hip - knee).length / 0.4, 0.2
        )  # how much bigger than the 1.8 m mannequin
        depsgraph = bpy.context.evaluated_depsgraph_get()
        seat_z = None
        for obj in bpy.data.objects:
            if obj.type != "MESH" or not any(
                m.type == "ARMATURE" for m in obj.modifiers
            ):
                continue
            evaluated = obj.evaluated_get(depsgraph)
            mesh = evaluated.to_mesh()
            for vertex in mesh.vertices:
                co = evaluated.matrix_world @ vertex.co
                if (
                    abs(co.x) < 0.35 * k
                    and pelvis.y - 0.15 * k < co.y < pelvis.y + 0.2 * k
                    and pelvis.z - 0.3 * k < co.z < pelvis.z + 0.05 * k
                ):
                    seat_z = co.z if seat_z is None else min(seat_z, co.z)
            evaluated.to_mesh_clear()
    finally:
        _restore_pose(arm_obj, before)
    if seat_z is None:
        return False
    before = _sit_pose(
        arm_obj, enter_clip
    )  # where it stands as the sit-down begins
    try:
        standing_y = (arm_obj.matrix_world @ bones["pelvis"].head).y
    finally:
        _restore_pose(arm_obj, before)

    half_width = max(abs(hip.x) * 2.4, 0.2 * k)
    back_y = pelvis.y + 0.2 * k
    front_y = min(
        standing_y + 0.14 * k, back_y - 0.25 * k
    )  # behind its standing hips, and not too shallow
    centre_y, depth = (back_y + front_y) / 2, back_y - front_y
    thick, leg = 0.05 * k, 0.04 * k
    parts = []

    def box(name: str, centre, size) -> None:
        bpy.ops.mesh.primitive_cube_add(size=1, location=centre)
        piece = bpy.context.active_object
        piece.name = name
        piece.scale = size
        bpy.ops.object.transform_apply(scale=True)
        parts.append(piece)

    box(
        "seat",
        (0, centre_y, seat_z - thick / 2),
        (2 * half_width, depth, thick),
    )
    for sx in (-1, 1):
        for y in (back_y - leg, front_y + leg):
            box(
                "leg",
                (sx * (half_width - leg), y, (seat_z - thick) / 2),
                (leg, leg, seat_z - thick),
            )
    box(
        "back",
        (0, back_y - leg / 2, seat_z + 0.2 * k),
        (2 * half_width, leg, 0.4 * k),
    )
    chair = _join_objects(parts, SIT_PROP_NODE)
    material = bpy.data.materials.new("sit_prop_wood")
    material.diffuse_color = hex_to_rgba(wood)
    material.use_nodes = True
    shader = material.node_tree.nodes.get("Principled BSDF")
    if shader is not None:
        shader.inputs["Base Color"].default_value = hex_to_rgba(wood)
    chair.data.materials.append(material)
    _add_collapse_keys(chair, mathutils.Vector((0.0, centre_y, 0.0)))
    return True


# The overlay cannot find a scene node by name, so it hides the chair with a morph target:
# "collapse" shrinks it to a point on the floor, and the weight is driven like blinking is. It
# finds the mesh by having exactly three targets (`kSitPropTargets` there), a count nothing else in
# a build has (1 blink, 2 run, 4 gesture, 6 run and gesture, 9 viseme); the other two are
# a hair's breadth of movement, there to make the count.


def _add_collapse_keys(obj, base) -> None:
    obj.shape_key_add(name="Basis", from_mix=False)
    collapse = obj.shape_key_add(name="sit_prop_collapse", from_mix=False)
    for point, vertex in zip(collapse.data, obj.data.vertices):
        point.co = base + (vertex.co - base) * 0.001
    for index, name in enumerate(("sit_prop_pad_a", "sit_prop_pad_b")):
        pad = obj.shape_key_add(name=name, from_mix=False)
        for point, vertex in zip(pad.data, obj.data.vertices):
            offset = [0.0, 0.0, 0.0]
            offset[index] = 0.0005
            point.co = vertex.co + mathutils.Vector(offset)
    for key in obj.data.shape_keys.key_blocks:
        key.value = 0.0


def _action_fcurves(action):
    """Every F-curve of an action, on Blender's layered actions (4.4+) and the old flat kind."""
    if hasattr(action, "fcurves"):
        yield from action.fcurves
        return
    for layer in action.layers:
        for strip in layer.strips:
            for channelbag in strip.channelbags:
                yield from channelbag.fcurves


def _drop_curves(action, keep) -> int:
    """Removes the F-curves of `action` whose bone is not in `keep`; returns how many."""
    dropped = 0
    containers = (
        [action]
        if hasattr(action, "fcurves")
        else [
            bag
            for layer in action.layers
            for strip in layer.strips
            for bag in strip.channelbags
        ]
    )
    for container in containers:
        for curve in list(container.fcurves):
            match = re.match(r'pose\.bones\["(.+?)"\]', curve.data_path)
            if match and match.group(1) not in keep:
                container.fcurves.remove(curve)
                dropped += 1
    return dropped


def _spread_arms(action, arm_obj, degrees: float) -> None:
    """Turns the upper arms of every pose of `action` outward by `degrees`, about the axis the body
    faces, so an arm that hangs does not hang through a leg it is too near."""
    for side in "lr":
        bone = arm_obj.data.bones.get(f"upperarm_{side}")
        if bone is None:
            continue
        path = f'pose.bones["{bone.name}"].rotation_quaternion'
        curves = {
            c.array_index: c
            for c in _action_fcurves(action)
            if c.data_path == path
        }
        if (
            len(curves) != 4
            or len({len(c.keyframe_points) for c in curves.values()}) != 1
        ):
            continue
        outward = -1.0 if bone.head_local.x > 0 else 1.0
        axis = bone.matrix_local.to_3x3().inverted() @ mathutils.Vector(
            (0, 1, 0)
        )
        turn = mathutils.Quaternion(
            axis.normalized(), math.radians(degrees) * outward
        )
        for i in range(len(curves[0].keyframe_points)):
            q = turn @ mathutils.Quaternion(
                [curves[k].keyframe_points[i].co.y for k in range(4)]
            )
            for k in range(4):
                point = curves[k].keyframe_points[i]
                shift = q[k] - point.co.y
                point.co.y += shift
                point.handle_left.y += shift
                point.handle_right.y += shift
        for curve in curves.values():
            curve.update()


def wear_animation_library(
    arm_obj,
    plan: dict[str, tuple[Path, str]],
    warnings: list[str],
    arm_spread: float = 0.0,
) -> dict[str, str]:
    """Moves the planned clips onto `arm_obj` (as actions named for the clip) and returns
    {role: clip} for those that made it. Each library file is imported to take its actions
    and then dropped again, so none of its mesh or rig is left in the scene."""
    worn: dict[str, str] = {}
    hips = arm_obj.data.bones.get("pelvis")
    ours = {bone.name for bone in arm_obj.data.bones}
    for path in dict.fromkeys(file for file, _clip in plan.values()):
        wanted = {clip for file, clip in plan.values() if file == path}
        had_actions = set(bpy.data.actions)
        objs = _import_objects(path)
        source_rig = next((o for o in objs if o.type == "ARMATURE"), None)
        source_hips = (
            source_rig.data.bones.get("pelvis")
            if source_rig is not None
            else None
        )
        ratio = 1.0
        if (
            hips is not None
            and source_hips is not None
            and source_hips.head_local.z > 1e-6
        ):
            ratio = hips.head_local.z / source_hips.head_local.z
        scales = library_time_scale()
        slower = {
            clip: scales[role]
            for role, (file, clip) in plan.items()
            if file == path and role in scales
        }
        for action in [a for a in bpy.data.actions if a not in had_actions]:
            if action.name not in wanted:
                bpy.data.actions.remove(action)
                continue
            stretch = slower.get(action.name, 1.0)
            _drop_curves(action, ours)
            if arm_spread:
                _spread_arms(action, arm_obj, arm_spread)
            for curve in _action_fcurves(action):
                is_hips_travel = curve.data_path.endswith(
                    ".location"
                )  # in this body's size
                for point in curve.keyframe_points:
                    if is_hips_travel:
                        point.co.y *= ratio
                        point.handle_left.y *= ratio
                        point.handle_right.y *= ratio
                    if stretch != 1.0:
                        point.co.x *= stretch
                        point.handle_left.x *= stretch
                        point.handle_right.x *= stretch
                curve.update()
            action.use_fake_user = True
        for obj in objs:
            bpy.data.objects.remove(obj, do_unlink=True)
        for role, (file, clip) in plan.items():
            if file != path:
                continue
            if bpy.data.actions.get(clip) is not None:
                worn[role] = clip
            else:
                warnings.append(
                    f"animation library clip {clip} was not found in {path.name}"
                )
    return worn


# The overlay frames every body alike (overlay/qml/Avatar3D.qml): a 50-degree camera 4 m away
# looking at a window `1 + HEADROOM` times as tall as it is wide, with the bottom edge fixed. A
# clip that swings a part out of that window is cut off on screen.
FRAME_DISTANCE = 4.0
FRAME_FOV_HALF_TAN = 0.4663  # tan(25 degrees): the base framing
FRAME_HEADROOM = 0.15  # Main.qml `headroom`, as a fraction of the window height
# The roles that reach: a clip of any other (walk, sit, sleep) keeps the body near where it stands.
REACHING_ROLES = ("attention", "greet", "celebrate", "angry", "hip_thrust")


def _frame_use(arm_obj, clip: str, meshes: list, fit: float = 1.0) -> float:
    """How much of the overlay window `clip` uses at its furthest when the body is drawn at `fit`
    times its size, standing on the same spot: 1.0 is the edge, more is outside.
    """
    import numpy as np

    scale = 1.0 + FRAME_HEADROOM
    half = FRAME_DISTANCE * FRAME_FOV_HALF_TAN
    centre_y = 1.0 - half + half * scale
    action = bpy.data.actions[clip]
    first, last = action.frame_range
    animation = arm_obj.animation_data
    animation.action = action
    if getattr(animation, "action_slot", None) is None and action.slots:
        animation.action_slot = action.slots[0]
    use = 0.0
    for step in range(9):
        bpy.context.scene.frame_set(int(first + (last - first) * step / 8))
        bpy.context.view_layer.update()
        graph = bpy.context.evaluated_depsgraph_get()
        for mesh_obj in meshes:
            evaluated = mesh_obj.evaluated_get(graph)
            mesh = evaluated.to_mesh()
            flat = np.empty(len(mesh.vertices) * 3, dtype=np.float32)
            mesh.vertices.foreach_get("co", flat)
            evaluated.to_mesh_clear()
            if not len(flat):
                continue
            matrix = np.array(mesh_obj.matrix_world)
            pts = (flat.reshape(-1, 3) @ matrix[:3, :3].T + matrix[:3, 3]) * fit
            # glTF faces +Z, which is Blender's -Y: a part toward the camera is magnified.
            distance = np.maximum(FRAME_DISTANCE + pts[:, 1], 0.1)
            across = np.abs(pts[:, 0]) / (distance * FRAME_FOV_HALF_TAN)
            up = (pts[:, 2] - centre_y) / (
                distance * FRAME_FOV_HALF_TAN * scale
            )
            use = max(use, float(across.max()), float(up.max()))
    return use


def fit_clips_to_the_frame(
    arm_obj, clips: dict[str, str], warnings: list[str]
) -> float:
    """How big the overlay should draw this body (a fraction of its size, 1.0 for most) so that
    its gestures (long arms thrown overhead) stay inside the window: just small enough for the one
    that reaches furthest, but never below MIN_FIT_SCALE. A gesture that still leaves the window at
    that size is removed from `clips`, with the actions nothing else uses; a body left without an
    attention clip asks for attention with the greeting that reaches furthest and fits.
    """
    meshes = [o for o in arm_obj.children if o.type == "MESH"]
    previous = _pose_state(arm_obj)
    reaching = {
        clip
        for key, clip in clips.items()
        if key.split(".")[0] in REACHING_ROLES
    }
    fit, use = solve_fit(
        lambda at: {
            clip: _frame_use(arm_obj, clip, meshes, at) for clip in reaching
        }
    )
    lost_attention = False
    for key, clip in list(clips.items()):
        if key.split(".")[0] in REACHING_ROLES and use[clip] > FRAME_MARGIN:
            del clips[key]
            lost_attention = lost_attention or key == "attention"
            warnings.append(
                f"clip {clip} ({key}) swings the body out of the overlay window even at {fit:.0%} of its size: not used"
            )
    if lost_attention and "attention" not in clips:
        greetings = [
            clips[k]
            for k in clips
            if k.split(".")[0] == "greet"
            and not k.endswith((".low", ".high", ".mirror"))
        ]
        if greetings:
            clips["attention"] = max(greetings, key=lambda c: use[c])
    _restore_pose(arm_obj, previous)
    for clip, amount in use.items():
        if amount > FRAME_MARGIN and clip not in clips.values():
            bpy.data.actions.remove(bpy.data.actions[clip])
    if fit < 1.0:
        warnings.append(
            f"drawn at {fit:.0%} of its size so that its gestures stay in the overlay window"
        )
    return fit


_OUTFIT_KEEPS_BODY_BONES = ("head", "neck")


def outfit_model_path(
    deploy_dir: Path, base_template: str, outfit: str
) -> Path:
    """Mirrors brain/appearance/models.py `outfit_model_path` (a test keeps them equal)."""
    return (
        deploy_dir
        / "assets"
        / "outfit_models"
        / base_template.replace(":", "_")
        / f"{outfit}.glb"
    )


def _strip_body_to_head(body) -> bool:
    """Deletes every vertex of `body` whose strongest bone weight is not the head or
    neck. False (nothing changed) if the mesh has no weights or has shape keys.
    """
    if body.data.shape_keys is not None or not body.vertex_groups:
        return False
    bm = bmesh.new()
    bm.from_mesh(body.data)
    layer = bm.verts.layers.deform.active
    if layer is None:
        bm.free()
        return False
    names = {group.index: group.name.lower() for group in body.vertex_groups}
    drop = []
    for vert in bm.verts:
        weights = vert[layer]
        strongest = (
            max(weights.items(), key=lambda item: item[1])[0]
            if weights
            else None
        )
        if strongest is None or not any(
            part in names.get(strongest, "")
            for part in _OUTFIT_KEEPS_BODY_BONES
        ):
            drop.append(vert)
    bmesh.ops.delete(bm, geom=drop, context="VERTS")
    bm.to_mesh(body.data)
    bm.free()
    body.data.update()
    return True


def wear_outfit_model(imported: dict, path: Path, warnings: list[str]) -> bool:
    """Puts the outfit model at `path` on the imported body. False, with a
    warning, if it cannot be worn (no meshes or rig, or bones the body lacks), in
    which case the scene is left as it was."""
    armature = imported["armature"]
    objs = _import_objects(path)
    shapes = {
        pb.custom_shape
        for o in objs
        if o.type == "ARMATURE"
        for pb in o.pose.bones
        if pb.custom_shape is not None
    }
    for obj in [
        o for o in objs if o.type in ("CAMERA", "LIGHT") or o in shapes
    ]:
        objs.remove(obj)
        bpy.data.objects.remove(obj, do_unlink=True)
    meshes = [o for o in objs if o.type == "MESH"]
    rigs = [o for o in objs if o.type == "ARMATURE"]

    def discard() -> bool:
        for obj in objs:
            bpy.data.objects.remove(obj, do_unlink=True)
        return False

    if not meshes or not rigs:
        warnings.append(f"outfit {path.stem} has no skinned meshes; not worn")
        return discard()
    foreign = {b.name for b in rigs[0].data.bones} - {
        b.name for b in armature.data.bones
    }
    if foreign:
        warnings.append(
            f"outfit {path.stem} is for a different skeleton (bones {sorted(foreign)[:3]}...); not worn"
        )
        return discard()

    _bake_import_pose(rigs[0], meshes, warnings)
    bpy.context.view_layer.update()
    for mesh in meshes:
        local = rigs[0].matrix_world.inverted() @ mesh.matrix_world
        for mod in [m for m in mesh.modifiers if m.type == "ARMATURE"]:
            mesh.modifiers.remove(mod)
        mesh.parent = armature
        mesh.matrix_parent_inverse = mathutils.Matrix.Identity(4)
        mesh.matrix_basis = (
            local  # the body's own scale and position now carry it
        )
        mod = mesh.modifiers.new("Armature", "ARMATURE")
        mod.object = armature
    for obj in [o for o in objs if o not in meshes]:
        bpy.data.objects.remove(obj, do_unlink=True)

    if not _strip_body_to_head(imported["body"]):
        warnings.append(
            "could not remove the body under the outfit; it may show through"
        )
    imported["meshes"] = [*imported["meshes"], *meshes]
    # Belts, badges and pockets fit the worn torso, not the (now headless) body.
    torsos = [
        m for m in meshes if m.name.lower().rsplit(".", 1)[0].endswith("body")
    ]
    _BODY["obj"] = max(
        torsos or meshes,
        key=lambda m: m.dimensions.x * m.dimensions.y * m.dimensions.z,
    )
    _refresh_bounds(imported)
    return True
