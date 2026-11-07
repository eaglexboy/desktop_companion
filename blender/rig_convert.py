"""Makes a body of a known rig family (Character Creator, Auto-Rig Pro) play the Quaternius animation
libraries. Run headless, never imported:

    blender --background --factory-startup --python blender/rig_convert.py -- body <in.glb> <out.glb>
    blender --background --factory-startup --python blender/rig_convert.py -- clips <spec.json>

`body` names the bones that have a Quaternius equivalent as that bone (`blender/rigs.py` has the maps;
the others stay as they are and follow their parents), turns the body to face as the Quaternius
characters do, and, if its arms do not stand out to the sides, poses them into a T-pose and makes that
its rest pose (the meshes are deformed to it): a library clip is the change from the rest pose of the
Quaternius skeleton, so the body has to rest as that does.

`clips` retargets the clips of a library onto the body. The body's bones turn as the library's
turn, measured in world space as the change from each skeleton's rest pose (the bones' own axes
differ from rig to rig and do not matter); the hips' travel is scaled by how tall the two hips stand.
The result is written as a library for that body alone (the body's skeleton and the clips), keys
thinned as the Mixamo converter does. Spec JSON: {"body": <body .glb>, "library": <a library .glb>,
"out": <.glb>, "clips": [clip names]}. The reasons are in docs/ADR_ACCESSORY_MODELS.md, decision 18.
"""

import json
import math
import sys
from pathlib import Path

import bpy  # pyright: ignore[reportMissingImports]
import mathutils  # pyright: ignore[reportMissingImports]

sys.path.insert(
    0, str(Path(__file__).resolve().parent.parent)
)  # repo root, for blender.rigs
from blender.animation_library import q_angle_deg, reduce_keys  # noqa: E402
from blender.rigs import quaternius_names, rig_family  # noqa: E402

T_POSE_TOLERANCE_DEG = (
    12.0  # arms within this of straight out to the sides are left as they are
)
ROTATION_TOLERANCE_DEG = 0.25
HIPS_TOLERANCE_M = 0.002
ARM_CHAIN = ("upperarm", "lowerarm", "hand")


def _armature():
    return max(
        (o for o in bpy.data.objects if o.type == "ARMATURE"),
        key=lambda a: len(a.data.bones),
    )


def _assign(armature, action) -> None:
    armature.animation_data_create()
    armature.animation_data.action = action
    if getattr(armature.animation_data, "action_slot", None) is None and action.slots:
        armature.animation_data.action_slot = action.slots[0]


def _lateral_yaw(arm) -> float:
    """Which way the body's left is, as a yaw about Z (0: its left is along +X, as on the Quaternius
    characters, which face -Y)."""
    bones = arm.data.bones
    v = (arm.matrix_world @ bones["thigh_l"].head_local) - (
        arm.matrix_world @ bones["thigh_r"].head_local
    )
    return math.atan2(v.y, v.x)


def _pose_arms_out(arm) -> bool:
    """Poses the arms of a body whose arms hang or slope into a T-pose (each bone of the chain turned to
    point straight out to its own side). True if anything was posed."""
    posed = False
    bpy.context.view_layer.objects.active = arm
    bpy.ops.object.mode_set(mode="POSE")
    world = arm.matrix_world
    inverse = world.to_3x3().inverted()
    for side in ("l", "r"):
        for part in ARM_CHAIN:
            pb = arm.pose.bones.get(f"{part}_{side}")
            if pb is None:
                continue
            head, tail = world @ pb.head, world @ pb.tail
            current = (tail - head).normalized()
            out = (
                1.0
                if (world @ arm.pose.bones[f"upperarm_{side}"].head).x >= 0
                else -1.0
            )
            wanted = mathutils.Vector((out, 0.0, 0.0))
            if math.degrees(current.angle(wanted)) <= T_POSE_TOLERANCE_DEG:
                continue
            turn = (
                inverse
                @ current.rotation_difference(wanted).to_matrix()
                @ world.to_3x3()
            )
            m = pb.matrix
            pb.matrix = (
                mathutils.Matrix.Translation(m.translation)
                @ (turn @ m.to_3x3()).to_4x4()
            )
            bpy.context.view_layer.update()
            posed = True
    bpy.ops.object.mode_set(mode="OBJECT")
    return posed


def _make_pose_the_rest_pose(arm) -> None:
    """The meshes deformed as they are posed, and the pose made the rest pose of the skeleton."""
    meshes = [
        o
        for o in bpy.data.objects
        if o.type == "MESH" and any(m.type == "ARMATURE" for m in o.modifiers)
    ]
    for mesh in meshes:
        if mesh.data.users > 1:
            mesh.data = mesh.data.copy()
        bpy.context.view_layer.objects.active = mesh
        for modifier in [m for m in mesh.modifiers if m.type == "ARMATURE"]:
            bpy.ops.object.modifier_apply(modifier=modifier.name)
    bpy.context.view_layer.objects.active = arm
    bpy.ops.object.mode_set(mode="POSE")
    bpy.ops.pose.armature_apply(selected=False)
    bpy.ops.object.mode_set(mode="OBJECT")
    for mesh in meshes:
        mesh.modifiers.new("Armature", "ARMATURE").object = arm


def convert_body(source: str, out: str) -> None:
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.import_scene.gltf(filepath=source)
    arm = _armature()
    names = [b.name for b in arm.data.bones]
    family = rig_family(names)
    if family is None:
        raise SystemExit(
            "this skeleton is not one of the known rigs (Character Creator, Auto-Rig Pro): nothing to map it by"
        )
    mapping = quaternius_names(family, names)
    spare = {n for n in names if n not in mapping}
    mapping = {
        old: new for old, new in mapping.items() if new == old or new not in spare
    }  # not onto a name another bone has
    renamed = {old: new for old, new in mapping.items() if old != new}

    bpy.context.view_layer.objects.active = arm
    bpy.ops.object.mode_set(mode="EDIT")
    for old, new in renamed.items():
        arm.data.edit_bones[old].name = new
    if "root" not in arm.data.edit_bones:
        root = arm.data.edit_bones.new("root")
        root.head, root.tail = (0.0, 0.0, 0.0), (0.0, 0.0, 0.1)
        for bone in [
            b for b in arm.data.edit_bones if b.parent is None and b is not root
        ]:
            bone.parent = root
    bpy.ops.object.mode_set(mode="OBJECT")
    meshes = [o for o in bpy.data.objects if o.type == "MESH"]
    for mesh in meshes:
        for group in mesh.vertex_groups:
            group.name = renamed.get(group.name, group.name)

    missing = [
        n
        for n in ("pelvis", "thigh_l", "thigh_r", "upperarm_l", "upperarm_r")
        if n not in arm.data.bones
    ]
    if missing:
        raise SystemExit(
            f"the skeleton has no {', '.join(missing)} to map: it cannot play the libraries"
        )

    # Facing as the Quaternius characters do: its left along +X.
    turn = -_lateral_yaw(arm)
    if abs(turn) > math.radians(1.0):
        arm.rotation_euler.z += turn
        bpy.ops.object.select_all(action="DESELECT")
        for obj in (arm, *meshes):
            obj.select_set(True)
        bpy.context.view_layer.objects.active = arm
        bpy.ops.object.transform_apply(location=True, rotation=True, scale=False)

    tposed = _pose_arms_out(arm)
    if tposed:
        _make_pose_the_rest_pose(arm)
    bpy.ops.export_scene.gltf(
        filepath=out,
        export_format="GLB",
        export_yup=True,
        export_animations=False,
        export_skins=True,
        export_image_format="JPEG",
        export_jpeg_quality=85,
        export_draco_mesh_compression_enable=False,
    )
    print(
        f"RIG_BODY_OK {out} family={family} mapped={len(mapping)} turned={math.degrees(turn):.0f} tposed={int(tposed)}"
    )


# --- the libraries ---------------------------------------------------------------------------------


def _rest_world(arm) -> dict:
    w = arm.matrix_world.to_3x3()
    return {b.name: (w @ b.matrix_local.to_3x3()).normalized() for b in arm.data.bones}


def _in_order(arm) -> list:
    order = []

    def walk(bone) -> None:
        order.append(bone)
        for child in bone.children:
            walk(child)

    for bone in arm.data.bones:
        if bone.parent is None:
            walk(bone)
    return order


def retarget_library(spec_path: str) -> None:
    spec = json.loads(Path(spec_path).read_text())
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.context.scene.render.fps = 30
    bpy.ops.import_scene.gltf(filepath=spec["body"])
    target = _armature()
    for obj in [o for o in bpy.data.objects if o.type == "MESH"]:
        bpy.data.objects.remove(obj, do_unlink=True)

    before_objects, before_actions = set(bpy.data.objects), set(bpy.data.actions)
    bpy.ops.import_scene.gltf(filepath=spec["library"])
    new_objects = [o for o in bpy.data.objects if o not in before_objects]
    source = next(o for o in new_objects if o.type == "ARMATURE")
    actions = {a.name: a for a in bpy.data.actions if a not in before_actions}

    rest_s, rest_t = _rest_world(source), _rest_world(target)
    common = [
        n for n in source.data.bones.keys() if n in target.data.bones and n != "root"
    ]
    order = _in_order(target)
    wanted = [c for c in spec["clips"] if c in actions]

    yaw = _lateral_yaw(target) - _lateral_yaw(source)
    spin = mathutils.Matrix.Rotation(yaw, 3, "Z")
    pelvis_s = source.matrix_world @ source.data.bones["pelvis"].head_local
    pelvis_t = target.matrix_world @ target.data.bones["pelvis"].head_local
    ratio = pelvis_t.z / pelvis_s.z if pelvis_s.z > 1e-6 else 1.0
    s3, t3 = source.matrix_world.to_3x3(), target.matrix_world.to_3x3()
    pelvis_rest_t = rest_t["pelvis"]

    made = []
    for name in wanted:
        action = actions[name]
        _assign(source, action)
        first, last = int(action.frame_range[0]), int(action.frame_range[1])
        frames: list[float] = []
        quats = {n: [] for n in common}
        travel = []
        for frame in range(first, last + 1):
            bpy.context.scene.frame_set(frame)
            bpy.context.view_layer.update()
            change = {
                n: (s3 @ source.pose.bones[n].matrix.to_3x3().normalized())
                @ rest_s[n].inverted()
                for n in common
            }
            desired = {}
            for bone in order:
                rest = rest_t[bone.name]
                if bone.name in change:
                    desired[bone.name] = (
                        spin @ change[bone.name] @ spin.inverted() @ rest
                    )
                elif bone.parent:
                    parent = bone.parent.name
                    desired[bone.name] = (
                        desired[parent] @ rest_t[parent].inverted() @ rest
                    )  # keeps its place under its parent
                else:
                    desired[bone.name] = rest
            for bone in order:
                if bone.name not in change:
                    continue
                rest = rest_t[bone.name]
                if bone.parent:
                    parent = bone.parent.name
                    basis = (
                        rest.inverted()
                        @ rest_t[parent]
                        @ desired[parent].inverted()
                        @ desired[bone.name]
                    )
                else:
                    basis = rest.inverted() @ desired[bone.name]
                q = basis.to_quaternion()
                quats[bone.name].append((q.w, q.x, q.y, q.z))
            moved = (source.matrix_world @ source.pose.bones["pelvis"].head) - pelvis_s
            travel.append(spin @ moved * ratio)
            frames.append(float(frame - first))
        made.append((name, frames, quats, travel))

    for old in actions.values():
        bpy.data.actions.remove(old)
    for obj in new_objects:
        bpy.data.objects.remove(obj, do_unlink=True)
    for name, frames, quats, travel in made:
        action = bpy.data.actions.new(name)
        action.use_fake_user = True
        _assign(target, action)
        for bone_name, samples in quats.items():
            pose_bone = target.pose.bones[bone_name]
            pose_bone.rotation_mode = "QUATERNION"
            for i in reduce_keys(frames, samples, q_angle_deg, ROTATION_TOLERANCE_DEG):
                pose_bone.rotation_quaternion = mathutils.Quaternion(samples[i])
                pose_bone.keyframe_insert("rotation_quaternion", frame=frames[i])
        pelvis = target.pose.bones["pelvis"]
        basis_travel = [
            pelvis_rest_t.inverted() @ t3.inverted() @ mathutils.Vector(t)
            for t in travel
        ]
        for i in reduce_keys(
            frames,
            [tuple(t) for t in basis_travel],
            lambda a, b: math.dist(a, b),
            HIPS_TOLERANCE_M,
        ):
            pelvis.location = basis_travel[i]
            pelvis.keyframe_insert("location", frame=frames[i])

    # A glTF has no skin without a mesh: the library's skeleton gets one small triangle.
    mesh = bpy.data.meshes.new("library_triangle")
    mesh.from_pydata([(0, 0, 0), (0.01, 0, 0), (0, 0, 0.01)], [], [(0, 1, 2)])
    triangle = bpy.data.objects.new("library_triangle", mesh)
    bpy.context.scene.collection.objects.link(triangle)
    triangle.vertex_groups.new(name="pelvis").add([0, 1, 2], 1.0, "REPLACE")
    triangle.parent = target
    triangle.modifiers.new("Armature", "ARMATURE").object = target
    bpy.ops.export_scene.gltf(
        filepath=spec["out"],
        export_format="GLB",
        export_yup=True,
        export_animations=True,
        export_animation_mode="ACTIONS",
        export_skins=True,
        export_draco_mesh_compression_enable=False,
    )
    print("RIG_CLIPS_OK", spec["out"], ",".join(name for name, *_ in made))


def main() -> None:
    args = sys.argv[sys.argv.index("--") + 1 :]
    if args[0] == "body":
        convert_body(args[1], args[2])
    elif args[0] == "clips":
        retarget_library(args[1])
    else:
        raise SystemExit(f"unknown mode {args[0]!r}")


if __name__ == "__main__":
    main()
