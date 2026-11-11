"""
Accessory models (.glb) worn on a body's sockets: their sidecar settings,
import, placement and binding.
"""

from __future__ import annotations

import json
from pathlib import Path

import bpy  # pyright: ignore[reportMissingImports]
import mathutils  # pyright: ignore[reportMissingImports]

from blender.gen_garments import _assemble
from blender.gen_wardrobe import _ACCESSORY_BUILDERS, _SOCKET_LOCATIONS


def _accessory_sidecar(asset_path: Path) -> dict:
    """
    Settings next to an accessory model (`<item>.json`): scale, offset
    (metres, in the socket's frame) and rotation (radians). All optional.
    """
    path = asset_path.with_suffix(".json")
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _sidecar_number(value, default: float) -> float:
    try:
        return float(value) if value is not None else default
    except (TypeError, ValueError):
        return default


def _sidecar_triple(value) -> tuple[float, float, float]:
    try:
        x, y, z = (float(v) for v in value)
    except (TypeError, ValueError):
        return (0.0, 0.0, 0.0)
    return (x, y, z)


def _bake_into_world(obj, matrix) -> None:
    """Moves a mesh object's vertices by `matrix` (applied after its own world
    transform) and resets the object transform, so the geometry sits where it
    should whatever its origin was."""
    obj.data.transform(matrix @ obj.matrix_world)
    obj.matrix_world = mathutils.Matrix.Identity(4)
    obj.data.update()


def _scaled_about(location, k: float):
    loc = mathutils.Vector(location)
    return (
        mathutils.Matrix.Translation(loc)
        @ mathutils.Matrix.Scale(k, 4)
        @ mathutils.Matrix.Translation(-loc)
    )


def build_socket_accessory(
    socket_name: str,
    item: str,
    head_loc,
    palette: dict,
    deploy_dir: Path,
    frame: dict | None = None,
):
    """Discovers a pre-modeled asset under <deploy_dir>/assets/wardrobe/<socket>/<item>.glb;
    falls back to a parametric model of the named item if absent (keeps
    accessories testable with zero authored assets). Each catalog item has its
    own shape -- they are not interchangeable primitives.

    `frame` is set for an installed body, whose proportions differ from the
    procedural bodies the parametric items were drawn for: {"location": where
    the socket is on this body, "k": how much smaller than the procedural
    figure its head/hands are}. A model asset is placed at the location (its
    origin is the attach point) scaled by its sidecar's `scale`; a parametric
    item is built where the procedural bodies carry it and scaled to fit."""
    location = (
        tuple(frame["location"])
        if frame
        else _SOCKET_LOCATIONS[socket_name](head_loc)
    )
    asset_path = (
        deploy_dir / "assets" / "wardrobe" / socket_name / f"{item}.glb"
    )
    if asset_path.is_file():
        meta = _accessory_sidecar(asset_path)
        meshes = _import_accessory_meshes(asset_path)
        scale = _sidecar_number(meta.get("scale"), 1.0)
        offset = mathutils.Vector(_sidecar_triple(meta.get("offset")))
        rotation = mathutils.Euler(_sidecar_triple(meta.get("rotation")))
        matrix = (
            mathutils.Matrix.Translation(mathutils.Vector(location) + offset)
            @ rotation.to_matrix().to_4x4()
            @ mathutils.Matrix.Scale(scale, 4)
        )
        for obj in meshes:
            _bake_into_world(obj, matrix)
        joined = _join_objects(meshes, f"Accessory_{socket_name}_{item}")
        return [joined]

    parts = _ACCESSORY_BUILDERS[socket_name](item, location, palette)
    built = _assemble(f"Accessory_{socket_name}_{item}", parts)
    if frame and frame.get("k", 1.0) != 1.0:
        _bake_into_world(built, _scaled_about(location, frame["k"]))
    return [built]


def _import_accessory_meshes(asset_path: Path) -> list:
    """The mesh objects of an accessory .glb, with any parent empties/rigs
    flattened away so each mesh carries its own world transform. A rigged model
    is taken in its rest pose as one rigid piece: the accessory is bound to a
    single bone of the body, so its own skin and bone-display shapes go."""
    before = {obj.name for obj in bpy.data.objects}
    bpy.ops.import_scene.gltf(filepath=str(asset_path))
    imported = [obj for obj in bpy.data.objects if obj.name not in before]
    bone_shapes = {
        pb.custom_shape
        for obj in imported
        if obj.type == "ARMATURE"
        for pb in obj.pose.bones
        if pb.custom_shape is not None
    }
    meshes = [
        obj for obj in imported if obj.type == "MESH" and obj not in bone_shapes
    ]
    bpy.context.view_layer.update()
    for mesh in meshes:
        if (
            mesh.data.users > 1
        ):  # instanced nodes share one mesh; each is moved on its own
            mesh.data = mesh.data.copy()
        world = mesh.matrix_world.copy()
        mesh.parent = None
        mesh.matrix_world = world
        for modifier in [m for m in mesh.modifiers if m.type == "ARMATURE"]:
            mesh.modifiers.remove(modifier)
        mesh.vertex_groups.clear()
    for obj in imported:
        if obj not in meshes:
            bpy.data.objects.remove(obj, do_unlink=True)
    if not meshes:
        raise ValueError(f"{asset_path.name} contains no mesh")
    return meshes


def _join_objects(objs, name: str):
    bpy.ops.object.select_all(action="DESELECT")
    for obj in objs:
        obj.select_set(True)
    bpy.context.view_layer.objects.active = objs[0]
    if len(objs) > 1:
        bpy.ops.object.join()
    joined = bpy.context.active_object
    joined.name = name
    joined.select_set(False)
    return joined


def bind_rigid(arm_obj, objs, bone_name: str) -> None:
    """Attaches whole objects to one bone (weight 1.0), so a hat follows the
    head exactly instead of being smeared across neighbouring bones the way
    automatic weights would."""
    for obj in objs:
        group = obj.vertex_groups.new(name=bone_name)
        group.add(range(len(obj.data.vertices)), 1.0, "REPLACE")
        modifier = obj.modifiers.new("Armature", "ARMATURE")
        modifier.object = arm_obj
        obj.parent = arm_obj
        obj.matrix_parent_inverse = arm_obj.matrix_world.inverted()
