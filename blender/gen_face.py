"""
The face overlay for a model whose face is painted on: eyelids, a patch, a
nine-viseme mouth.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import bpy  # pyright: ignore[reportMissingImports]

from blender.gen_body import (
    BLINK_SHAPE_KEY,
    _build_shape_keyed_plane,
)
from blender.gen_constants import (
    VISEME_MOUTH_MATRIX,
)
from blender.gen_helpers import (
    hex_to_rgba,
    make_material,
)
from blender.viseme_shapekeys import (
    VISEME_CODES,
)


def _face_sidecar(model_path: Path) -> dict | None:
    path = model_path.with_name(model_path.stem + ".face.json")
    if not path.is_file():
        return None
    try:
        face = json.loads(path.read_text())
        for key in ("eye_l", "eye_r", "mouth"):
            if not {"x", "z", "w", "h"} <= set(face[key]):
                raise ValueError(f"{key} needs x, z, w and h")
        return face
    except (ValueError, KeyError, OSError) as exc:
        print(f"DESKMATE_WARN could not read {path.name}: {exc}")
        return None


def _hex_linear(hex_color: str) -> tuple[float, float, float, float]:
    """sRGB hex -> the linear colour a Blender material wants, so a patch
    matches a painted texture of the same hex."""
    r, g, b, _ = hex_to_rgba(hex_color)

    def lin(c: float) -> float:
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

    return (lin(r), lin(g), lin(b), 1.0)


def _flat_material(name: str, hex_color: str) -> bpy.types.Material:
    mat = make_material(name, _hex_linear(hex_color))
    bsdf = mat.node_tree.nodes.get("Principled BSDF")
    if bsdf:
        bsdf.inputs["Roughness"].default_value = 1.0
        if "Specular IOR Level" in bsdf.inputs:
            bsdf.inputs["Specular IOR Level"].default_value = 0.0
    return mat


def _skin_texel(meshes, x: float, z: float):
    """(material, uv) of the model's own skin at world (x, z): the material and
    texture coordinate of the front-most triangle there. Overlay parts that use
    them are lit and coloured exactly like the painted skin around them, which a
    flat colour matched by eye never quite is. None if the model has no UVs."""
    best = None
    for mesh in meshes:
        data = mesh.data
        if not data.uv_layers or not mesh.material_slots:
            continue
        data.calc_loop_triangles()
        uv_data = data.uv_layers.active.data
        world = mesh.matrix_world
        for tri in data.loop_triangles:
            (ax, ay, az), (bx, by, bz), (cx, cy, cz) = (
                tuple(world @ data.vertices[i].co) for i in tri.vertices
            )
            det = (bz - cz) * (ax - cx) + (cx - bx) * (az - cz)
            if abs(det) < 1e-12:
                continue
            l1 = ((bz - cz) * (x - cx) + (cx - bx) * (z - cz)) / det
            l2 = ((cz - az) * (x - cx) + (ax - cx) * (z - cz)) / det
            l3 = 1.0 - l1 - l2
            if min(l1, l2, l3) < -1e-6:
                continue
            y = l1 * ay + l2 * by + l3 * cy
            if best is None or y < best[0]:
                uvs = [uv_data[loop].uv for loop in tri.loops]
                uv = (
                    sum(w * c.x for w, c in zip((l1, l2, l3), uvs)),
                    sum(w * c.y for w, c in zip((l1, l2, l3), uvs)),
                )
                slot = (
                    mesh.material_slots[tri.material_index]
                    if tri.material_index < len(mesh.material_slots)
                    else None
                )
                if slot is not None and slot.material is not None:
                    best = (y, slot.material, uv)
    return (best[1], best[2]) if best else None


def _point_uvs_at(obj, uv, spread: float = 0.004) -> None:
    """Every vertex of `obj` samples a texel of plain skin around `uv`. The
    coordinates must not all be identical: a renderer that builds tangents from
    them gets nothing from a degenerate map and shades the part black. The spread
    (a few pixels of the texture) stays inside the skin."""
    layer = obj.data.uv_layers.active or obj.data.uv_layers.new(
        name="UVMap"
    )  # primitives come with their own
    for polygon in obj.data.polygons:
        for loop_index in polygon.loop_indices:
            co = obj.data.vertices[obj.data.loops[loop_index].vertex_index].co
            layer.data[loop_index].uv = (
                uv[0] + co.x * spread,
                uv[1] + co.y * spread,
            )


def _ellipse(
    name: str, centre, w: float, h: float, material
) -> bpy.types.Object:
    """A flat ellipse facing the camera (-Y). Local circle radius 0.5 scaled to
    w x h, so local coordinates stay in -0.5..0.5 for the shape keys."""
    bpy.ops.mesh.primitive_circle_add(
        vertices=32, radius=0.5, fill_type="TRIFAN", location=centre
    )
    obj = bpy.context.active_object
    obj.name = name
    obj.rotation_euler = (math.radians(90), 0, 0)
    obj.scale = (w, h, 1.0)
    obj.data.materials.append(material)
    return obj


def _face_bone(meshes, point) -> str | None:
    """The bone that moves the model's skin nearest `point`."""
    best, best_d = None, math.inf
    for mesh in meshes:
        for vertex in mesh.data.vertices:
            if not vertex.groups:
                continue
            d = (mesh.matrix_world @ vertex.co - point).length
            if d < best_d:
                group = max(vertex.groups, key=lambda g: g.weight)
                best, best_d = mesh.vertex_groups[group.group].name, d
    return best


def _bind_rigid(obj, armature, bone: str) -> None:
    """Skin an overlay part wholly to one bone, so it travels with the face
    and never deforms the way automatic weights on a flat shape might."""
    # The exporter drops the scale of a skinned object, so bake it into the
    # vertices (and shape keys) first.
    bpy.ops.object.select_all(action="DESELECT")
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj
    bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)
    group = obj.vertex_groups.new(name=bone)
    group.add(list(range(len(obj.data.vertices))), 1.0, "REPLACE")
    modifier = obj.modifiers.new("Armature", "ARMATURE")
    modifier.object = armature
    obj.parent = armature
    obj.matrix_parent_inverse = armature.matrix_world.inverted()


def build_face_overlay(
    face: dict, imported_meshes, lo, hi, armature, bone: str | None
):
    """Returns (lids, parts, viseme_keys): the two eyelid objects (blink),
    every other overlay object, and the viseme shape key names."""
    width, height = hi.x - lo.x, hi.z - lo.z
    depsgraph = bpy.context.evaluated_depsgraph_get()
    verts = []
    for mesh in imported_meshes:
        evaluated = mesh.evaluated_get(depsgraph)
        data = evaluated.to_mesh()
        verts += [evaluated.matrix_world @ v.co for v in data.vertices]
        evaluated.to_mesh_clear()

    def landmark(key):
        m = face[key]
        return (
            lo.x + m["x"] * width,
            lo.z + m["z"] * height,
            m["w"] * width,
            m["h"] * height,
        )

    def surface_y(x, z, w, h) -> float:
        ys = [
            v.y
            for v in verts
            if abs(v.x - x) <= w / 2 and abs(v.z - z) <= h / 2
        ]
        return min(ys) if ys else lo.y

    # Skin: the model's own material at a plain spot between the eyes if it has
    # one, else a flat colour from the sidecar.
    ex = (face["eye_l"]["x"] + face["eye_r"]["x"]) / 2
    ez = (face["eye_l"]["z"] + face["eye_r"]["z"]) / 2
    skin_at = face.get("skin_at", {"x": ex, "z": ez})
    texel = _skin_texel(
        imported_meshes,
        lo.x + skin_at["x"] * width,
        lo.z + skin_at["z"] * height,
    )
    skin_mat = (
        texel[0]
        if texel
        else _flat_material("FaceSkin", face.get("skin", "#D37454"))
    )
    layer = 0.004  # each overlay layer sits this much nearer the camera than the last

    lids = []
    for side, key in (("L", "eye_l"), ("R", "eye_r")):
        x, z, w, h = landmark(key)
        y = surface_y(x, z, w, h) - 2 * layer
        lid = _ellipse(f"Eye_{side}", (x, y, z), w, h, skin_mat)
        if texel:
            _point_uvs_at(lid, texel[1])
        full = [v.co.copy() for v in lid.data.vertices]
        for v in lid.data.vertices:  # open: a hairline at the top edge
            v.co.y = 0.5 + (v.co.y - 0.5) * 0.002
        lid.shape_key_add(name="Basis")
        closed = lid.shape_key_add(name=BLINK_SHAPE_KEY)
        for i, co in enumerate(full):
            closed.data[i].co = co
        lids.append(lid)

    mx, mz, mw, mh = landmark("mouth")
    my = surface_y(mx, mz, mw, mh)
    parts = []
    if face.get(
        "patch", True
    ):  # a model with real lips keeps them: "patch": false draws the mouth over them
        patch = _ellipse(
            "MouthPatch", (mx, my - layer, mz), mw * 1.14, mh * 1.35, skin_mat
        )
        if texel:
            _point_uvs_at(patch, texel[1])
        parts.append(patch)

    smile = float(face.get("smile", 0.0))

    def mouth_deform(code):
        open_, width_k, round_, _ = VISEME_MOUTH_MATRIX[code]

        def fn(co):
            xn = co.x / 0.5  # -1..1 across the mouth
            x = co.x * width_k * (1.0 - round_ * 0.25)
            y = co.y * (0.15 + open_ * 0.85) + smile * 0.25 * (xn * xn - 0.5)
            return (x, y, co.z + round_ * 0.04)

        return fn

    def fill_deform(code):
        # The light inside of the mouth: the same shape inset from the black
        # outline, thin at rest, so a closed mouth is just the line.
        open_, width_k, round_, _ = VISEME_MOUTH_MATRIX[code]

        def fn(co):
            xn = co.x / 0.5
            x = co.x * width_k * (1.0 - round_ * 0.25) * 0.88
            # A hairline at rest, never zero: a collapsed rest shape has no defined facing, and the
            # normals pass flips it so it is culled once it opens.
            y = co.y * (0.02 + open_ * 0.85 * 0.72) + smile * 0.25 * (
                xn * xn - 0.5
            )
            return (x, y, co.z + round_ * 0.04)

        return fn

    def teeth_deform(code):
        # For a realistic mouth the second plane is not a lighter inset of the first but a
        # sliver of upper teeth hugging the top edge of the dark opening, shown when it is
        # open enough (or the viseme is a teeth-together one) and a hairline otherwise.
        open_, width_k, round_, together = VISEME_MOUTH_MATRIX[code]
        full_h = (
            0.15 + open_ * 0.85
        )  # the opening's height (it spans +-half of this)
        share = (
            0.8 if together else 0.3 if (open_ >= 0.3 and round_ < 0.5) else 0.0
        )
        strip = max(0.004, full_h * share * 0.5)
        centre = (full_h * 0.5 - strip * 0.5) * 0.92
        # The opening narrows towards its top: its half-width at height f (of its half-height) is
        # sqrt(1 - f^2 / 2) of the full one, for the square-to-disc map the plane is built with.
        edge = 0.92 * math.sqrt(
            max(0.02, 1.0 - 0.5 * (centre / (full_h * 0.5)) ** 2)
        )

        def fn(co):
            return (
                co.x * width_k * (1.0 - round_ * 0.25) * edge,
                centre + co.y * strip,
                co.z + round_ * 0.04,
            )

        return fn

    # The planes are built unit-sized and stretched by object scale, like the lids.
    mouth, viseme_keys = _build_shape_keyed_plane(
        "Mouth",
        1.0,
        (mx, my - 2 * layer, mz),
        (math.radians(90), 0, 0),
        _flat_material("MouthMat", face.get("mouth_color", "#101010")),
        {c: mouth_deform(c) for c in VISEME_CODES},
        oval=bool(face.get("oval", False)),
    )
    mouth.scale = (mw * 0.92, mh * 1.15, 1.0)
    teeth, _ = (
        _build_shape_keyed_plane(  # named Teeth: the second viseme-driven mesh the overlay expects
            "Teeth",
            1.0,
            (mx, my - 3 * layer, mz),
            (math.radians(90), 0, 0),
            _flat_material("TeethMat", face.get("teeth_color", "#C4C4C4")),
            {
                c: (
                    fill_deform(c)
                    if face.get("fill") or not face.get("oval")
                    else teeth_deform(c)
                )
                for c in VISEME_CODES
            },
            oval=bool(face.get("oval", False)),
        )
    )
    teeth.scale = (mw * 0.92, mh * 1.15, 1.0)
    parts += [mouth, teeth]

    def tongue_deform(code):
        # A tongue lying on the floor of the mouth's inside: narrower than it, as tall as 40% of
        # it, and a hairline (never zero) while the mouth is closed. Follows the same smile curve.
        open_, width_k, round_, _ = VISEME_MOUTH_MATRIX[code]
        inner = 0.02 + open_ * 0.85 * 0.72
        tall = max(0.006, inner * 0.4)

        def fn(co):
            xn = (
                co.x
            )  # -0.5..0.5 here: the tongue is half as wide as the inside
            return (
                co.x * width_k * (1.0 - round_ * 0.25) * 0.88 * 0.5,
                -inner * 0.5
                + tall * 0.5
                + co.y * tall
                + smile * 0.25 * (xn * xn - 0.5),
                co.z + round_ * 0.04,
            )

        return fn

    if face.get("tongue_color"):
        tongue, _ = _build_shape_keyed_plane(
            "Tongue",  # a third viseme-driven mesh: the overlay drives every mesh with nine targets
            1.0,
            (mx, my - 4 * layer, mz),
            (math.radians(90), 0, 0),
            _flat_material("TongueMat", face["tongue_color"]),
            {c: tongue_deform(c) for c in VISEME_CODES},
            oval=True,
        )
        tongue.scale = (mw * 0.92, mh * 1.15, 1.0)
        parts.append(tongue)

    for obj in [*lids, *parts]:
        _bind_rigid(obj, armature, bone or "head")
    return lids, parts, viseme_keys
