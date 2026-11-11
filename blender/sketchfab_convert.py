"""Cleans up a model downloaded from Sketchfab. Run headless, never imported:

    blender --background --factory-startup --python blender/sketchfab_convert.py -- <in.glb> <out.glb> [<max texture px> [<max triangles>]]

What it does, and why (docs/ADR_ACCESSORY_MODELS.md, decision 17):

* The author's centimetre scale and the wrapper nodes Sketchfab puts above the model are baked into
  the skeleton and the meshes, so that it is one metre-scale, Z-up object with no parents.
* The helper bones Blender's importer adds to carry a node's scale (`<bone>_scaleCompensation`),
  and Sketchfab's `_rootJoint`, are removed, the bones under them re-parented one level up.
* Sketchfab's uniqueness suffix is taken off the bones and their vertex groups (`CC_Base_Head_038`
  -> `CC_Base_Head`), so that the sockets and the library mapping can recognise them.
* Shape keys are dropped: a mesh with shape keys cannot be decimated, and a model that cannot be
  brought under the triangle budget cannot be built (a Sketchfab model of half a million triangles
  is normal). Textures are shrunk and the meshes decimated to the budgets in the same pass.
* A model whose rest pose lies on its back (the author's skeleton is bound lying down and the
  upright figure is the first frame of its animation, which is thrown away) is turned upright: if the
  posed figure is clearly upright and the bound one is not, the whole figure is turned by the shortest
  rotation that takes the bound figure's up direction onto Z (its bind pose is kept as it is).
* The objects Blender's importer makes for display (the bone-shape sphere), the empties, and the
  baked motion Sketchfab models are exported with, are left out.

Rigid parts attached to a single bone are bound to it by a vertex group, the way the generator binds
loose parts of other models.
"""

import sys
from pathlib import Path

import bpy  # pyright: ignore[reportMissingImports]
import mathutils  # pyright: ignore[reportMissingImports]
import numpy as np

sys.path.insert(
    0, str(Path(__file__).resolve().parent.parent)
)  # repo root, for blender.sketchfab
from blender.sketchfab import clean_names, is_helper_bone

SMALL_PART_TRIANGLES = (
    600  # parts this small keep their detail when the rest is decimated
)


def _triangles(mesh) -> int:
    return sum(len(p.vertices) - 2 for p in mesh.data.polygons)


def _decimate(meshes: list, max_triangles: int) -> None:
    for mesh in meshes:
        if (
            mesh.data.users > 1
        ):  # instances of one mesh: a modifier cannot be applied to shared data
            mesh.data = mesh.data.copy()
    counts = {m.name: _triangles(m) for m in meshes}
    total = sum(counts.values())
    if total <= max_triangles:
        return
    big = [
        m
        for m in meshes
        if counts[m.name] >= SMALL_PART_TRIANGLES and m.data.shape_keys is None
    ]
    kept = total - sum(
        counts[m.name] for m in big
    )  # the small parts, which are left as they are
    ratio = max(
        0.01, (max_triangles - kept) / max(1, sum(counts[m.name] for m in big))
    )
    for mesh in big:
        modifier = mesh.modifiers.new("Decimate", "DECIMATE")
        modifier.ratio = ratio
        bpy.context.view_layer.objects.active = mesh
        bpy.ops.object.modifier_move_to_index(
            modifier=modifier.name, index=0
        )  # on the bind shape, before the armature
        bpy.ops.object.modifier_apply(modifier=modifier.name)


def _points(arm, meshes, posed: bool, skinned_only: bool = True) -> np.ndarray:
    """The vertices (N x 3, world space) of the skinned meshes of `arm` (or of all the meshes), posed or
    as bound, in a fixed order."""
    out = []
    depsgraph = bpy.context.evaluated_depsgraph_get()
    for mesh in meshes:
        if skinned_only and not any(
            m.type == "ARMATURE" and m.object is arm for m in mesh.modifiers
        ):
            continue
        source = mesh.evaluated_get(depsgraph) if posed else mesh
        data = source.to_mesh() if posed else mesh.data
        n = len(data.vertices)
        flat = np.empty(n * 3)
        data.vertices.foreach_get("co", flat)
        world = np.array(source.matrix_world)
        out.append(flat.reshape(n, 3) @ world[:3, :3].T + world[:3, 3])
        if posed:
            source.to_mesh_clear()
    return np.vstack(out) if out else np.zeros((0, 3))


def _size(points: np.ndarray) -> np.ndarray:
    return (
        points.max(axis=0) - points.min(axis=0) if len(points) else np.zeros(3)
    )


def stand_up_posed_figure(arm, meshes) -> str | None:
    """Turns the figure upright when it is bound lying down and upright only in the first frame of its
    animation (see the module docstring). The pose is not baked in: the bind pose is what the
    libraries are measured from, and baking a raised arm in would pull whatever shares its weights.
    The turn is the shortest one that takes the bound figure's up direction (the axis along which its
    vertices rise from the bound to the posed state) onto Z."""
    posed, bound = _points(arm, meshes, True), _points(arm, meshes, False)
    whole = _points(arm, meshes, True, skinned_only=False)
    posed_size, bound_size, whole_size = (
        _size(posed),
        _size(bound),
        _size(whole),
    )
    if not (
        bound_size[2] < bound_size[1]
        and posed_size[2] > 1.5 * posed_size[1]
        and posed_size[2] >= 0.7 * whole_size[2]
    ):
        return (
            None  # (a few skinned parts of a bigger model are not the figure)
        )
    up, *_ = np.linalg.lstsq(
        bound - bound.mean(axis=0), posed[:, 2] - posed[:, 2].mean(), rcond=None
    )
    norm = float(np.linalg.norm(up))
    if norm < 1e-9:
        return None
    direction = up / norm
    axis = int(np.argmax(np.abs(direction)))
    if (
        abs(direction[axis]) > 0.8
    ):  # a figure lying down lies at a right angle: no stray tilt
        direction = np.zeros(3)
        direction[axis] = np.sign(up[axis])
    turn = (
        mathutils.Vector(direction)
        .rotation_difference(mathutils.Vector((0.0, 0.0, 1.0)))
        .to_matrix()
        .to_4x4()
    )
    for obj in (arm, *meshes):
        obj.matrix_world = turn @ obj.matrix_world
    bpy.context.view_layer.update()
    return f"the figure is bound lying down: turned upright (height {bound_size[2]:.2f} -> {posed_size[2]:.2f})"


def convert(
    source: str, out: str, max_texture: int = 0, max_triangles: int = 0
) -> None:
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.import_scene.gltf(filepath=source)
    armatures = [o for o in bpy.data.objects if o.type == "ARMATURE"]
    if not armatures:
        raise SystemExit(
            "this model has no skeleton: nothing to rig it with (is it a Sketchfab download with its rig?)"
        )
    arm = max(armatures, key=lambda a: len(a.data.bones))
    shapes = {
        pb.custom_shape
        for a in armatures
        for pb in a.pose.bones
        if pb.custom_shape is not None
    }
    for obj in list(shapes):
        bpy.data.objects.remove(
            obj, do_unlink=True
        )  # the sphere the importer shows bones with
    meshes = [o for o in bpy.data.objects if o.type == "MESH"]

    # Loose parts that hang from one bone become skinned to it.
    for mesh in meshes:
        if (
            mesh.parent is arm
            and mesh.parent_type == "BONE"
            and mesh.parent_bone
        ):
            world, bone = mesh.matrix_world.copy(), mesh.parent_bone
            mesh.parent = None
            mesh.matrix_world = world
            group = mesh.vertex_groups.new(name=bone)
            group.add([v.index for v in mesh.data.vertices], 1.0, "REPLACE")
            mesh.modifiers.new("Armature", "ARMATURE").object = arm

    # Flat, metre-scale objects with nothing above them.
    for obj in (arm, *meshes):
        world = obj.matrix_world.copy()
        obj.parent = None
        obj.matrix_world = world

    note = stand_up_posed_figure(arm, meshes)
    if note:
        print(f"SKETCHFAB_FIX {note}")

    # The helper bones out, the names cleaned.
    bpy.context.view_layer.objects.active = arm
    bpy.ops.object.mode_set(mode="EDIT")
    edit = arm.data.edit_bones
    for bone in [b for b in edit if is_helper_bone(b.name)]:
        for child in list(bone.children):
            child.parent = bone.parent
        edit.remove(bone)
    renames = clean_names([b.name for b in edit])
    for bone in list(edit):
        bone.name = renames.get(bone.name, bone.name)
    bpy.ops.object.mode_set(mode="OBJECT")
    for mesh in meshes:
        for group in mesh.vertex_groups:
            group.name = renames.get(group.name, group.name)
        if mesh.data.shape_keys is not None:
            for key in list(mesh.data.shape_keys.key_blocks)[::-1]:
                mesh.shape_key_remove(key)

    arm.animation_data_clear()
    for action in list(bpy.data.actions):
        bpy.data.actions.remove(action)
    for obj in [
        o for o in bpy.data.objects if o is not arm and o not in meshes
    ]:
        bpy.data.objects.remove(
            obj, do_unlink=True
        )  # the empties of the wrappers, and anything else loose

    bpy.ops.object.select_all(action="DESELECT")
    for obj in (arm, *meshes):
        obj.select_set(True)
    bpy.context.view_layer.objects.active = arm
    bpy.ops.object.transform_apply(location=True, rotation=True, scale=True)
    for mesh in meshes:
        mesh.parent = arm
        mesh.matrix_parent_inverse = mathutils.Matrix.Identity(4)

    if max_texture:
        for image in bpy.data.images:
            width, height = image.size
            if max(width, height) > max_texture:
                ratio = max_texture / max(width, height)
                image.scale(
                    max(1, round(width * ratio)), max(1, round(height * ratio))
                )
    if max_triangles:
        _decimate(meshes, max_triangles)
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
    total = sum(_triangles(m) for m in meshes)
    print(f"SKETCHFAB_OK {out} bones={len(arm.data.bones)} triangles={total}")


def main() -> None:
    args = sys.argv[sys.argv.index("--") + 1 :]
    convert(
        args[0],
        args[1],
        int(args[2]) if len(args) > 2 else 0,
        int(args[3]) if len(args) > 3 else 0,
    )


if __name__ == "__main__":
    main()
