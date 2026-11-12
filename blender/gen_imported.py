"""
Imported (Tier 1/2) models: their import, per-model pose fixes, and the arms
relaxed from a T-pose.
"""

from __future__ import annotations

import json
import math
import re
from pathlib import Path

import bpy  # pyright: ignore[reportMissingImports]
import mathutils  # pyright: ignore[reportMissingImports]

from blender.gen_clips import (
    find_limb_bones,
)
from blender.gen_garments import (
    _BODY,
)
from blender.gen_helpers import (
    _names_of,
)
from blender.sockets import wrist_is_tail
from blender.viseme_shapekeys import (
    SHAPE_KEY_PREFIX,
    VISEME_CODES,
    shape_key_name,
)

IMPORT_TARGET_HEIGHT = (
    2.5  # metres; the procedural bodies stand roughly this tall
)


def _import_objects(path: Path) -> list:
    before = {obj.name for obj in bpy.data.objects}
    if path.suffix == ".glb":
        bpy.ops.import_scene.gltf(filepath=str(path))
    elif path.suffix == ".blend":
        with bpy.data.libraries.load(str(path), link=False) as (
            data_from,
            data_to,
        ):
            data_to.objects = data_from.objects
        for obj in data_to.objects:
            if obj is not None:
                bpy.context.scene.collection.objects.link(obj)
    else:
        raise ValueError(f"unrecognized model file extension {path.suffix!r}")
    return [obj for obj in bpy.data.objects if obj.name not in before]


def _world_bounds(objs) -> tuple:
    """World-space extent of the *deformed* meshes: a skinned mesh's own
    vertices and bound_box are its bind shape, which for a converted model can
    be lying down or splayed while the posed figure stands."""
    depsgraph = bpy.context.evaluated_depsgraph_get()
    lo = mathutils.Vector((math.inf, math.inf, math.inf))
    hi = mathutils.Vector((-math.inf, -math.inf, -math.inf))
    for obj in objs:
        evaluated = obj.evaluated_get(depsgraph)
        mesh = evaluated.to_mesh()
        for vertex in mesh.vertices:
            co = evaluated.matrix_world @ vertex.co
            lo = mathutils.Vector(
                (min(lo.x, co.x), min(lo.y, co.y), min(lo.z, co.z))
            )
            hi = mathutils.Vector(
                (max(hi.x, co.x), max(hi.y, co.y), max(hi.z, co.z))
            )
        evaluated.to_mesh_clear()
    return lo, hi


def _flatten_static_meshes(meshes, objs) -> list:
    """A model with no rig of its own is bound to ours below, and a skinned
    mesh is exported without its node's transform. Models exported from other
    tools (Sketchfab's FBX conversions, for one) carry their size, rotation and
    axis flip on parent empties, so binding them as-is loses that and the
    figure comes out stretched or flat. Bake each mesh's world transform into
    its vertices and unparent it; the now-empty parents are dropped."""
    bpy.context.view_layer.update()
    for mesh in meshes:
        if (
            mesh.data.users > 1
        ):  # instanced nodes share one mesh; each is moved on its own
            mesh.data = mesh.data.copy()
        world = mesh.matrix_world.copy()
        mesh.parent = None
        mesh.matrix_world = world
    bpy.ops.object.select_all(action="DESELECT")
    for mesh in meshes:
        mesh.select_set(True)
    bpy.context.view_layer.objects.active = meshes[0]
    bpy.ops.object.transform_apply(location=True, rotation=True, scale=True)
    kept = [obj for obj in objs if obj.type != "EMPTY"]
    for obj in objs:
        if obj.type == "EMPTY":
            bpy.data.objects.remove(obj, do_unlink=True)
    return kept


def _draw_both_sides(meshes) -> None:
    """An imported material is drawn from the front only unless the file says otherwise, and the
    overlay's renderer then leaves out every face that is turned the wrong way (a low-poly model
    whose faces were not all made to point outward shows holes). Blender's own render draws both
    sides, so only the overlay shows it; both sides are drawn for every imported material.
    """
    for mesh in meshes:
        for material in mesh.data.materials:
            if material is not None:
                material.use_backface_culling = False


# The materials of a body's own hair, eyebrows and eyelashes. Mirrored by
# brain/appearance/build_options.py `HAIR_MATERIAL` (a test pins them together).
HAIR_MATERIAL = re.compile(r"hair|eyebrow|eyelash", re.IGNORECASE)


def _srgb_to_linear(c: float) -> float:
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def tint_hair(meshes, hex_color: str, flat: bool = False) -> int:
    """Multiplies the colour texture of the body's hair materials by `hex_color`. The hair of the
    standard humanoids is a grey texture made to be tinted, so a multiply gives any hair colour and
    white leaves it as modelled. A hair or beard model is made with a near-black texture, which a
    multiply cannot lighten, so with `flat` the texture is dropped and the colour is the material's
    own. Returns how many materials were tinted."""
    h = hex_color.lstrip("#")
    r, g, b = (
        _srgb_to_linear(int(h[i : i + 2], 16) / 255.0) for i in (0, 2, 4)
    )
    if min(r, g, b) >= 0.9999:
        return 0
    done: set = set()
    for mesh in meshes:
        for material in mesh.data.materials:
            if (
                material is None
                or material.name in done
                or not HAIR_MATERIAL.search(material.name)
                or material.node_tree is None
            ):
                continue
            done.add(material.name)
            tree = material.node_tree
            bsdf = next(
                (n for n in tree.nodes if n.type == "BSDF_PRINCIPLED"), None
            )
            if bsdf is None:
                continue
            socket = bsdf.inputs["Base Color"]
            if flat:
                for link in list(socket.links):
                    tree.links.remove(link)
                socket.default_value = (r, g, b, 1.0)
                continue
            mix = tree.nodes.new("ShaderNodeMix")
            mix.data_type = "RGBA"
            mix.blend_type = "MULTIPLY"
            mix.inputs["Factor"].default_value = 1.0
            mix.inputs["B"].default_value = (r, g, b, 1.0)
            if socket.links:
                tree.links.new(socket.links[0].from_socket, mix.inputs["A"])
            else:
                mix.inputs["A"].default_value = socket.default_value
            tree.links.new(mix.outputs["Result"], socket)
    return len(done)


def _bind_unweighted_vertices(armature, meshes) -> int:
    """A vertex that no bone has a weight for stays where it was when the body moves, and the glTF
    exporter ties it to a bone of its own that never moves (`neutral_bone`): a pair of eyes the model
    was made with no weights for floated in front of the face when the head turned. Each such vertex
    is given to the bone nearest to it. A file that was exported once already has that bone and the
    weights on it: those count as no weights, and are removed. Returns how many vertices were bound.
    """
    import numpy as np

    neutral = "neutral_bone"

    bones = {
        bone.name: (
            np.array(armature.matrix_world @ bone.head_local),
            np.array(armature.matrix_world @ bone.tail_local),
        )
        for bone in armature.data.bones
        if bone.name not in ("root", "neutral_bone")
    }
    if not bones:
        return 0
    names = list(bones)
    heads = np.array([bones[n][0] for n in names])
    tails = np.array([bones[n][1] for n in names])
    along = tails - heads
    length_squared = np.maximum((along * along).sum(axis=1), 1e-12)
    bound = 0
    for mesh in meshes:
        if not any(m.type == "ARMATURE" for m in mesh.modifiers):
            continue
        index = {g.index: g.name for g in mesh.vertex_groups}
        loose = [
            v
            for v in mesh.data.vertices
            if sum(g.weight for g in v.groups if index.get(g.group) != neutral)
            <= 1e-6
        ]
        # Every weight on that bone goes, not only a loose piece's: a vertex that was a few percent on it
        # (the face of this model) is pulled that far toward a bone that is not where the skin was bound.
        if neutral in mesh.vertex_groups:
            mesh.vertex_groups.remove(mesh.vertex_groups[neutral])
        for vertex in loose:
            point = np.array(mesh.matrix_world @ vertex.co)
            t = np.clip(
                ((point - heads) * along).sum(axis=1) / length_squared, 0, 1
            )
            nearest = np.linalg.norm(
                point - (heads + t[:, None] * along), axis=1
            )
            name = names[int(nearest.argmin())]
            group = mesh.vertex_groups.get(name) or mesh.vertex_groups.new(
                name=name
            )
            group.add([vertex.index], 1.0, "REPLACE")
            bound += 1
    return bound


def _rename_meshes_named_for_bones(armature, meshes) -> None:
    """A mesh object named like a bone ("Head" on a model whose pieces were named for the bones they follow)
    exports as a node of the same name as the bone's, and a loader that finds a bone by its name (Qt's,
    through assimp) takes the mesh node for it: the part is left where the mesh sits, the head of a
    robot sinks into its body. The mesh gets another name."""
    bones = {bone.name for bone in armature.data.bones}
    for obj in meshes:
        if obj.name in bones:
            obj.name = obj.name + "_part"


def _bake_import_pose(armature, meshes, warnings: list[str]) -> None:
    """Makes the model's pose as imported its rest pose. Converted files often
    store a bind shape that is not the standing figure (lying down, say) and
    put the difference in the joints' node transforms; Blender shows that as a
    pose. The exporter writes the rest pose, so without this the avatar would
    come out in its bind shape. Applies the armature to each skinned mesh,
    commits the pose as rest, and re-attaches the armature. A mesh with shape
    keys cannot have its armature applied, so such a model is left as it is."""
    posed = any(not pb.matrix_basis.is_identity for pb in armature.pose.bones)
    if not posed:
        return
    skinned = [
        m
        for m in meshes
        if any(
            md.type == "ARMATURE" and md.object == armature
            for md in m.modifiers
        )
    ]
    if any(m.data.shape_keys is not None for m in skinned):
        warnings.append(
            "model is posed differently from its rest shape and has shape keys; kept as imported"
        )
        return
    _commit_pose_as_rest(armature, skinned)


def _commit_pose_as_rest(armature, skinned) -> None:
    """Applies the armature's current pose to each skinned mesh, makes that pose the rest
    pose, and puts the armature back on the meshes. (The meshes must have no shape keys.)
    """
    for mesh in skinned:
        bpy.ops.object.select_all(action="DESELECT")
        mesh.select_set(True)
        bpy.context.view_layer.objects.active = mesh
        for mod in [
            md
            for md in mesh.modifiers
            if md.type == "ARMATURE" and md.object == armature
        ]:
            bpy.ops.object.modifier_apply(modifier=mod.name)
    bpy.ops.object.select_all(action="DESELECT")
    armature.select_set(True)
    bpy.context.view_layer.objects.active = armature
    bpy.ops.object.mode_set(mode="POSE")
    bpy.ops.pose.armature_apply(selected=False)
    bpy.ops.object.mode_set(mode="OBJECT")
    for mesh in skinned:
        mod = mesh.modifiers.new("Armature", "ARMATURE")
        mod.object = armature
    bpy.context.view_layer.update()


def build_imported_model(path: Path, warnings: list[str]) -> dict:
    """Uses an authored model as the body instead of building one: imports it,
    scales it to the house height, stands it on the floor facing -Y, and
    reads what it can do (head/arms/legs from its bone names, lip-sync from
    its viseme shape keys). Wardrobe is the model's own, so outfits/footwear
    are not layered on top; accessories still attach. A model with no
    armature gets the canonical rig (the caller parents it); one with its own
    armature keeps it. Raises if the file holds no mesh at all."""
    objs = _import_objects(path)
    # The glTF importer adds a sphere mesh as the display shape of the rig's
    # bones. It is not part of the model, and left in it inflates the size
    # and triangle count.
    bone_shapes = {
        pb.custom_shape
        for obj in objs
        if obj.type == "ARMATURE"
        for pb in obj.pose.bones
        if pb.custom_shape is not None
    }
    discard = [
        obj
        for obj in objs
        if obj.type in ("CAMERA", "LIGHT") or obj in bone_shapes
    ]
    objs = [
        obj for obj in objs if obj not in discard
    ]  # before removing: a removed object cannot be compared
    for obj in discard:
        bpy.data.objects.remove(obj, do_unlink=True)
    meshes = [obj for obj in objs if obj.type == "MESH"]
    if not meshes:
        raise ValueError(f"{path.name} contains no mesh")
    _draw_both_sides(meshes)
    armatures = [obj for obj in objs if obj.type == "ARMATURE"]
    if not armatures:
        objs = _flatten_static_meshes(meshes, objs)
    else:
        _bake_import_pose(armatures[0], meshes, warnings)
        _rename_meshes_named_for_bones(armatures[0], meshes)
        if _bind_unweighted_vertices(armatures[0], meshes):
            warnings.append(
                "some vertices had no bone weights: each follows the bone nearest to it"
            )

    bpy.context.view_layer.update()
    roots = [
        obj for obj in objs if obj.parent is None or obj.parent not in objs
    ]
    face_yaw = _pose_sidecar(path).get("face_yaw", 0.0)
    if (
        face_yaw
    ):  # a model that does not face -Y (the overlay's front) is turned to
        turn = mathutils.Matrix.Rotation(math.radians(face_yaw), 4, "Z")
        for root in roots:
            root.matrix_world = turn @ root.matrix_world
        bpy.context.view_layer.update()
    lo, hi = _world_bounds(meshes)
    k = IMPORT_TARGET_HEIGHT / max(hi.z - lo.z, 1e-6)
    for (
        root
    ) in (
        roots
    ):  # scale about the world origin, so separate parts keep their spacing
        root.scale = (root.scale.x * k, root.scale.y * k, root.scale.z * k)
        root.location = root.location * k
    bpy.context.view_layer.update()
    lo, hi = _world_bounds(meshes)
    shift = mathutils.Vector(((lo.x + hi.x) / -2, (lo.y + hi.y) / -2, -lo.z))
    for root in roots:
        root.location = root.location + shift
    bpy.context.view_layer.update()
    lo, hi = _world_bounds(meshes)

    bone_names = (
        [b.name.lower() for b in armatures[0].data.bones] if armatures else []
    )
    viseme_keys: set[str] = set()
    for mesh in meshes:
        if mesh.data.shape_keys is not None:
            viseme_keys |= {
                kb.name
                for kb in mesh.data.shape_keys.key_blocks
                if kb.name.startswith(SHAPE_KEY_PREFIX)
            }
    expected = {shape_key_name(code) for code in VISEME_CODES}
    if viseme_keys and not expected <= viseme_keys:
        warnings.append(
            f"model has only part of the viseme set ({len(viseme_keys & expected)}/9); lip-sync disabled"
        )
        viseme_keys = set()

    head_loc = (0.0, (lo.y + hi.y) / 2, hi.z - 0.55)
    if armatures:
        head_bones = [
            b for b in armatures[0].data.bones if "head" in b.name.lower()
        ]
        if head_bones:
            head_loc = tuple(
                armatures[0].matrix_world @ head_bones[0].head_local
            )

    largest = max(
        meshes, key=lambda m: m.dimensions.x * m.dimensions.y * m.dimensions.z
    )
    _BODY.update(
        obj=largest,
        imported=True,
        build=1.0,
        front=lo.y,
        back=hi.y,
        bottom=lo.z,
        half_width=(hi.x - lo.x) / 2,
    )
    return {
        "meshes": meshes,
        "armature": armatures[0] if armatures else None,
        "head_loc": head_loc,
        "has_arms": any("arm" in n for n in bone_names),
        "has_legs": any("leg" in n or "thigh" in n for n in bone_names),
        "viseme_keys": sorted(
            viseme_keys,
            key=lambda n: VISEME_CODES.index(n[len(SHAPE_KEY_PREFIX) :]),
        ),
        "body": largest,
        "import_scale": k,  # how much the model was scaled to the build's standard height
        "bounds": (lo, hi),
        "body_bounds": (
            lo,
            hi,
        ),  # the model's own box: face sidecar fractions are measured against it
    }


def _width_above(meshes, z: float) -> float:
    """Sideways extent of the deformed meshes above height `z`."""
    depsgraph = bpy.context.evaluated_depsgraph_get()
    lo_x, hi_x = math.inf, -math.inf
    for obj in meshes:
        evaluated = obj.evaluated_get(depsgraph)
        mesh = evaluated.to_mesh()
        for vertex in mesh.vertices:
            co = evaluated.matrix_world @ vertex.co
            if co.z > z:
                lo_x, hi_x = min(lo_x, co.x), max(hi_x, co.x)
        evaluated.to_mesh_clear()
    return hi_x - lo_x if hi_x > lo_x else 0.0


def _top_centre(meshes, z: float) -> tuple[float, float] | None:
    """Middle (x, y) of the deformed meshes above height `z`: where the top of the head, or of
    whatever is worn on it, really is. The head bone starts at the base of the head, so its x and
    y are not the middle of the skull."""
    depsgraph = bpy.context.evaluated_depsgraph_get()
    lo_x, hi_x = math.inf, -math.inf
    lo_y, hi_y = math.inf, -math.inf
    for obj in meshes:
        evaluated = obj.evaluated_get(depsgraph)
        mesh = evaluated.to_mesh()
        for vertex in mesh.vertices:
            co = evaluated.matrix_world @ vertex.co
            if co.z > z:
                lo_x, hi_x = min(lo_x, co.x), max(hi_x, co.x)
                lo_y, hi_y = min(lo_y, co.y), max(hi_y, co.y)
        evaluated.to_mesh_clear()
    if hi_x < lo_x:
        return None
    return (lo_x + hi_x) / 2, (lo_y + hi_y) / 2


def _front_surface_y(meshes, x: float, z: float) -> float | None:
    """Y of the front-most surface of `meshes` at (x, z) (figures face -Y), by
    ray cast from in front; None if the ray misses everything."""
    best = None
    for obj in meshes:
        inv = obj.matrix_world.inverted()
        origin = inv @ mathutils.Vector((x, -10.0, z))
        direction = (
            inv.to_3x3() @ mathutils.Vector((0.0, 1.0, 0.0))
        ).normalized()
        hit, location, *_ = obj.ray_cast(origin, direction)
        if hit:
            y = (obj.matrix_world @ location).y
            best = y if best is None else min(best, y)
    return best


def _pose_sidecar(model_path: Path) -> dict:
    path = model_path.with_name(model_path.stem + ".pose.json")
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text())
        rolls = data.get("roll_bones", {})
        if not isinstance(rolls, dict) or not all(
            isinstance(v, (int, float)) for v in rolls.values()
        ):
            raise ValueError("roll_bones must map a bone name to degrees")
        yaw = data.get("face_yaw", 0)
        if not isinstance(yaw, (int, float)):
            raise ValueError("face_yaw must be degrees")
        gain = data.get("emissive_gain", 1.0)
        if not isinstance(gain, (int, float)) or gain <= 0:
            raise ValueError("emissive_gain must be a number above 0")
        return {
            "roll_bones": {str(k): float(v) for k, v in rolls.items()},
            "face_yaw": float(yaw),
            "emissive_gain": float(gain),
        }
    except (ValueError, OSError, AttributeError) as exc:
        print(f"DESKMATE_WARN could not read {path.name}: {exc}")
        return {}


def roll_model_bones(imported: dict, rolls: dict, warnings: list[str]) -> bool:
    """Turns each bone in `rolls` (name -> degrees) about its own length and makes that the
    rest pose. False if nothing was turned."""
    armature = imported["armature"]
    skinned = [
        m
        for m in imported["meshes"]
        if any(
            md.type == "ARMATURE" and md.object == armature
            for md in m.modifiers
        )
    ]
    if any(m.data.shape_keys is not None for m in skinned):
        warnings.append(
            "a mesh has shape keys; the bone rolls in its pose.json could not be applied"
        )
        return False
    turned = False
    for name, degrees in rolls.items():
        pbone = armature.pose.bones.get(name)
        if pbone is None:
            warnings.append(f"pose.json names a bone this model lacks: {name}")
            continue
        pbone.rotation_mode = "QUATERNION"
        pbone.rotation_quaternion = mathutils.Quaternion(
            (0, 1, 0), math.radians(degrees)
        )  # a bone's own Y runs along it
        turned = True
    if turned:
        bpy.context.view_layer.update()
        _commit_pose_as_rest(armature, skinned)
        _refresh_bounds(imported)
    return turned


RELAXED_ARM_DEG = 10  # how far off vertical a relaxed upper arm hangs


RELAXED_ELBOW_DEG = (
    5  # and how far forward its forearm is bent from it, as a person's hang
)


RELAX_IF_HANGING_LESS_THAN = 0.75  # an upper arm whose downward component is below this (over ~41 degrees off


def _refresh_bounds(imported: dict) -> None:
    """Re-measures the figure after its shape changed (an outfit, relaxed arms)."""
    bpy.context.view_layer.update()
    lo, hi = _world_bounds(imported["meshes"])
    imported["bounds"] = (lo, hi)
    _BODY.update(
        front=lo.y, back=hi.y, bottom=lo.z, half_width=(hi.x - lo.x) / 2
    )


def _aim_bone(armature, name: str, direction_world: mathutils.Vector) -> None:
    """Turns the posed bone `name` (about its head) so it points along `direction_world`."""
    pbone = armature.pose.bones[name]
    to_armature = armature.matrix_world.to_3x3().normalized().inverted()
    target = (to_armature @ direction_world).normalized()
    current = (pbone.tail - pbone.head).normalized()
    pivot = mathutils.Matrix.Translation(pbone.head)
    pbone.matrix = (
        pivot
        @ current.rotation_difference(target).to_matrix().to_4x4()
        @ pivot.inverted()
        @ pbone.matrix
    )
    bpy.context.view_layer.update()


def relax_arms(imported: dict) -> bool:
    """Brings arms that are held out or up (a T-pose, a raised wave) down to a relaxed
    stance, the forearm hanging with a slight bend, and makes that the rest pose. False if
    the rig has no arm bones, the arms already hang (or are held forward, like a mech's), or
    a mesh has shape keys (its armature cannot be applied)."""
    armature = imported["armature"]
    roles = find_limb_bones(armature)
    if "upper_arm" not in roles or "forearm" not in roles:
        return False
    skinned = [
        m
        for m in imported["meshes"]
        if any(
            md.type == "ARMATURE" and md.object == armature
            for md in m.modifiers
        )
    ]
    if any(m.data.shape_keys is not None for m in skinned):
        return False
    world = armature.matrix_world
    changed = False
    for side in ("min_x", "max_x"):
        upper = armature.data.bones[roles["upper_arm"][side]]
        along = (
            (world @ upper.tail_local) - (world @ upper.head_local)
        ).normalized()
        if -along.z >= RELAX_IF_HANGING_LESS_THAN:
            continue  # already hanging
        out = 1.0 if along.x >= 0 else -1.0
        hang, bend = (
            math.radians(RELAXED_ARM_DEG),
            math.radians(RELAXED_ELBOW_DEG),
        )
        upper_to = mathutils.Vector(
            (out * math.sin(hang), 0.0, -math.cos(hang))
        )
        forearm_to = mathutils.Vector(
            (
                out * math.sin(hang - bend * 0.5),
                -math.sin(bend),
                -math.cos(hang - bend * 0.5),
            )
        ).normalized()
        _aim_bone(armature, upper.name, upper_to)
        _aim_bone(
            armature, roles["forearm"][side], forearm_to
        )  # after the upper arm, which carried it along
        changed = True
    if not changed:
        return False
    _commit_pose_as_rest(armature, skinned)
    _refresh_bounds(imported)
    return True


def imported_socket_frames(imported: dict, socket_bones: dict) -> dict:
    """Where each accessory socket is on an installed body, and how much to
    shrink the procedural items for it: {socket: {"location", "k"}}. Head
    sockets come from the top of the figure and the size of its head; chest,
    waist and hands from the rig's bones, falling back to proportions of the
    figure's height. `k` is the head (or hand) size relative to the procedural
    bodies' 1.1 m head, which the parametric items were drawn for; items that
    fit themselves to the body (belt, badge, backpack) are not scaled."""
    lo, hi = imported["bounds"]
    armature = imported["armature"]
    height = hi.z - lo.z

    def bone_pos(name, along: float = 0.0):
        """World position `along` (0 = head, 1 = tail) of a bone."""
        if armature is None or name not in armature.data.bones:
            return None
        bone = armature.data.bones[name]
        return armature.matrix_world @ bone.head_local.lerp(
            bone.tail_local, along
        )

    head_pos = bone_pos(socket_bones.get("socket_head_top", ""))
    if head_pos is not None:
        # The head bone usually starts at the base of the head, but some rigs start it
        # mid-head; the width of the mesh above it is the check on that.
        head_h = max(
            hi.z - head_pos.z,
            _width_above(imported["meshes"], head_pos.z),
            0.05 * height,
        )
    else:
        head_h = 0.14 * height
    k_head = max(0.2, min(1.0, head_h / 1.1))
    k_hand = max(k_head, 0.5)
    cx = head_pos.x if head_pos is not None else (lo.x + hi.x) / 2
    cy = head_pos.y if head_pos is not None else (lo.y + hi.y) / 2
    centre_z = hi.z - head_h / 2

    eyes = _find_eyes(imported)[0] if "socket_eyes" in socket_bones else None
    # What sits on the head is centred on the head itself (the middle of the mesh near the top,
    # which is the hood if one is worn), not on the head bone, and rests on its top. The parametric
    # head items are drawn with their base 0.1 below their socket and then scaled about it by
    # `k_head`, so the socket is put 0.1 * k_head above the point the item should stand on.
    middle = _top_centre(imported["meshes"], hi.z - 0.5 * head_h)
    hx, hy = middle if middle is not None else (cx, cy)
    top = (hx, hy, hi.z + 0.1 * k_head)
    worn = (
        hx,
        hy,
        hi.z - 0.03 * head_h + 0.1 * k_head,
    )  # a hat sits just down on the head; the orbit floats above it
    frames: dict = {}
    for socket, bone in socket_bones.items():
        # A spine bone runs from the pelvis to the chest: the waist is its lower part, the chest its
        # middle. A hips/pelvis bone sits at the hip joints and points up the body: its tail is the waist.
        lowered = bone.lower()
        waist_along = (
            0.25
            if "spine" in lowered
            else 1.0 if ("hips" in lowered or "pelvis" in lowered) else 0.0
        )
        along = {"socket_chest": 0.5, "socket_waist": waist_along}.get(
            socket, 1.0 if wrist_is_tail(bone) else 0.0
        )
        pos = bone_pos(bone, along)
        if socket == "socket_head_top":
            # a model file's origin is its attach point: for the head that is the head bone's own
            # point (as the neck's and the hands' are their bones'), not the point the parametric
            # items are drawn from (`worn`). Measured from the mesh the fit is only within a few
            # millimetres, which shows as the skull poking through a hairstyle.
            frames[socket] = {
                "location": worn,
                "k": k_head,
                "asset": (
                    tuple(head_pos) if head_pos is not None else (hx, hy, hi.z)
                ),
            }
        elif socket == "socket_orbit":
            frames[socket] = {"location": top, "k": k_head}
        elif socket == "socket_eyes":
            if (
                eyes is not None
            ):  # the model's own eyes: centred on them, sized by how far apart they are
                eye_x, eye_z = (
                    (eyes[0][0] + eyes[1][0]) / 2,
                    (eyes[0][1] + eyes[1][1]) / 2,
                )
                face = _front_surface_y(imported["meshes"], eyes[0][0], eye_z)
                y = face if face is not None else cy - 0.5 * k_head
                # The procedural glasses have their lenses 0.22 either side of the middle.
                frames[socket] = {
                    "location": (eye_x, y, eye_z),
                    "k": abs(eyes[1][0] - eyes[0][0]) / 2 / 0.22,
                }
                continue
            eye_z = centre_z + 0.05 * k_head
            face = _front_surface_y(
                imported["meshes"], cx, eye_z
            )  # the glasses sit on the face, wherever it is
            frames[socket] = {
                "location": (
                    cx,
                    (
                        (face + 0.05 * k_head)
                        if face is not None
                        else cy - 0.5 * k_head
                    ),
                    eye_z,
                ),
                "k": k_head,
            }
        elif socket == "socket_neck":
            z = pos.z if pos is not None else hi.z - head_h - 0.1 * k_head
            frames[socket] = {"location": (cx, cy, z), "k": k_head}
        elif socket == "socket_chest":
            frames[socket] = {
                "location": (
                    0.0,
                    cy,
                    pos.z if pos is not None else lo.z + 0.62 * height,
                ),
                "k": 1.0,
            }
        elif socket == "socket_waist":
            frames[socket] = {
                "location": (
                    0.0,
                    cy,
                    pos.z if pos is not None else lo.z + 0.45 * height,
                ),
                "k": 1.0,
            }
        elif pos is not None:  # a hand
            frames[socket] = {"location": tuple(pos), "k": k_hand}
    return frames


def _find_eyes(imported: dict) -> tuple:
    """(eyes, eye_meshes, None) for a model with a plausible pair of eyes, else
    (None, [], the reason). `eyes` is [(x, z, width, height)] for the low-x eye and
    then the high-x one, in world metres. The eyes are the mesh whose material or
    texture is named for them. A mesh that merely has "eye" in a name (a whole-body
    material, a single central eye) must not be taken for a face: eyes are small, a
    pair, and near the top of the figure."""
    lo, hi = imported["body_bounds"]
    width, height = hi.x - lo.x, hi.z - lo.z
    eye_meshes = [
        m
        for m in imported["meshes"]
        if "eye" in _names_of(m) and "brow" not in m.name.lower()
    ]  # eyebrows are named for eyes too
    if not eye_meshes:
        return (
            None,
            [],
            "no eye mesh found (one whose material or texture is named for the eyes)",
        )
    depsgraph = bpy.context.evaluated_depsgraph_get()
    points = []
    for mesh in eye_meshes:
        evaluated = mesh.evaluated_get(depsgraph)
        data = evaluated.to_mesh()
        points += [evaluated.matrix_world @ v.co for v in data.vertices]
        evaluated.to_mesh_clear()
    mid = sum(p.x for p in points) / len(points)
    eyes = []
    for side in (
        [p for p in points if p.x < mid],
        [p for p in points if p.x >= mid],
    ):
        if not side:
            return None, [], "the eye mesh is not a left/right pair of eyes"
        xs, zs = [p.x for p in side], [p.z for p in side]
        eyes.append(
            (
                (min(xs) + max(xs)) / 2,
                (min(zs) + max(zs)) / 2,
                max(xs) - min(xs),
                max(zs) - min(zs),
            )
        )
    for _, _, ew, eh in eyes:
        if ew > 0.2 * width or eh > 0.15 * height:
            return (
                None,
                [],
                f"the eye mesh is too large to be a pair of eyes ({100 * ew / width:.0f}% of the figure's width, {100 * eh / height:.0f}% of its height)",
            )
    eye_w = sum(e[2] for e in eyes) / 2
    eye_z = sum(e[1] for e in eyes) / 2
    if (
        abs(eyes[0][0] - eyes[1][0]) < 0.9 * eye_w
        or not 0.5 < (eye_z - lo.z) / height < 0.98
    ):
        return (
            None,
            [],
            "the eye mesh is not a left/right pair of eyes near the top of the figure",
        )
    return eyes, eye_meshes, None
