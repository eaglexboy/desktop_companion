"""
Procedural outfits and footwear, and the primitives they and the accessories are
built from.
"""

from __future__ import annotations

import math

import bpy  # pyright: ignore[reportMissingImports]
import mathutils  # pyright: ignore[reportMissingImports]

from blender.gen_helpers import (
    hex_to_rgba,
    make_material,
)

_GARMENT_PARAMS = {
    # outfit: (flare_m, thickness_m)
    "hoodie": (0.08, 0.003),
    "wizard_robe": (0.15, 0.003),
    "tshirt_jeans": (0.04, 0.002),
    "lab_coat": (0.1, 0.0025),
    "vest": (0.03, 0.002),
    "armor_light": (0.06, 0.004),
}


_DARK = (0.04, 0.04, 0.06, 1.0)


# Set by main() once the body exists; the wardrobe builders size and place
# things against the real body rather than against assumed template numbers.
_BODY: dict = {}


def _darker(color, factor: float = 0.5):
    return (color[0] * factor, color[1] * factor, color[2] * factor, 1.0)


def _prim(
    kind: str,
    location,
    *,
    scale=(1.0, 1.0, 1.0),
    rotation=(0.0, 0.0, 0.0),
    **kw,
):
    """One primitive, already scaled/rotated. Dimensions are in metres and
    describe the unscaled primitive (spheres by radius, cubes by edge)."""
    ops = bpy.ops.mesh
    if kind == "sphere":
        ops.primitive_uv_sphere_add(
            radius=kw.get("r", 1.0),
            segments=24,
            ring_count=12,
            location=location,
        )
    elif kind == "torus":
        ops.primitive_torus_add(
            major_radius=kw["major"],
            minor_radius=kw["minor"],
            major_segments=32,
            minor_segments=8,
            location=location,
        )
    elif kind == "cylinder":
        ops.primitive_cylinder_add(
            radius=kw["r"], depth=kw["depth"], vertices=24, location=location
        )
    elif kind == "cone":
        ops.primitive_cone_add(
            radius1=kw["r"],
            radius2=kw.get("r2", 0.0),
            depth=kw["depth"],
            vertices=24,
            location=location,
        )
    else:  # cube
        ops.primitive_cube_add(size=1.0, location=location)
    obj = bpy.context.active_object
    obj.scale = scale
    obj.rotation_euler = rotation
    return obj


def _assemble(name: str, parts) -> bpy.types.Object:
    """Joins (object, material) pairs into one mesh object. One object per
    garment/accessory keeps armature binding cheap and gives the exported
    glTF a node per item instead of a node per primitive."""
    for obj, mat in parts:
        bpy.context.view_layer.objects.active = obj
        bpy.ops.object.shade_smooth()
        # Scale and rotation go into the vertices: the joined object keeps the first part's
        # transform, which a skinned export drops, collapsing the others with it.
        bpy.ops.object.transform_apply(
            location=False, rotation=True, scale=True
        )
        obj.data.materials.append(mat)
    bpy.ops.object.select_all(action="DESELECT")
    for obj, _ in parts:
        obj.select_set(True)
    bpy.context.view_layer.objects.active = parts[0][0]
    if len(parts) > 1:
        bpy.ops.object.join()
    joined = bpy.context.active_object
    joined.name = name
    joined.select_set(False)
    return joined


def _body_surface_y(x: float, z: float, *, front: bool = True) -> float:
    """Y of the body's surface at (x, z), by ray cast, so belts, badges and
    pockets sit on the body whatever its shape; falls back to the bounding
    box if the ray misses."""
    body = _BODY["obj"]
    inv = body.matrix_world.inverted()
    origin_y, direction = (-6.0, 1.0) if front else (6.0, -1.0)
    origin = inv @ mathutils.Vector((x, origin_y, z))
    ray_dir = (
        inv.to_3x3() @ mathutils.Vector((0.0, direction, 0.0))
    ).normalized()
    hit, location, *_ = body.ray_cast(origin, ray_dir)
    if hit:
        return (body.matrix_world @ location).y
    return _BODY["front"] if front else _BODY["back"]


def _body_half_width_at(z: float) -> float:
    """Half-width of the body at height z (ray cast in from the +X side)."""
    body = _BODY["obj"]
    inv = body.matrix_world.inverted()
    origin = inv @ mathutils.Vector((6.0, 0.0, z))
    ray_dir = (inv.to_3x3() @ mathutils.Vector((-1.0, 0.0, 0.0))).normalized()
    hit, location, *_ = body.ray_cast(origin, ray_dir)
    return (body.matrix_world @ location).x if hit else _BODY["half_width"]


def build_outfit(
    body_obj, outfit: str, head_loc, palette: dict, surface_material: str
) -> list:
    """Procedural body garment: duplicates the body mesh, flares it outward
    per-outfit, solidifies, and copies the body's armature vertex groups 1:1
    so it inherits exact bone weights -- guarantees zero clipping at any body
    scale. Returns every mesh the outfit adds (the shell, plus any detail).

    The shell alone is just a recoloured body, so outfits that have a
    defining feature get it as extra geometry: a hoodie has a hood around the
    head and a front pocket. Outfits are otherwise differentiated only by
    flare/thickness, not vertical coverage -- a cropped "vest" and a
    floor-length "wizard_robe" come out the same length. Per-outfit vertical
    cropping is a real visual-fidelity gap, deferred."""
    if outfit == "none":
        return []

    bpy.ops.object.select_all(action="DESELECT")
    bpy.context.view_layer.objects.active = body_obj
    body_obj.select_set(True)
    bpy.ops.object.duplicate(linked=False)
    garment = bpy.context.active_object
    garment.select_set(False)
    body_obj.select_set(False)
    garment.name = f"Outfit_{outfit}"

    flare, thickness = _GARMENT_PARAMS.get(outfit, (0.05, 0.003))
    garment.scale = (
        garment.scale[0] * (1.0 + flare),
        garment.scale[1] * (1.0 + flare),
        garment.scale[2],
    )

    solidify = garment.modifiers.new("OutfitSolidify", type="SOLIDIFY")
    solidify.thickness = thickness
    bpy.context.view_layer.objects.active = garment
    bpy.ops.object.modifier_apply(modifier=solidify.name)

    secondary = hex_to_rgba(palette["secondary"])
    cloth = make_material(
        f"OutfitMat_{outfit}", secondary, material=surface_material
    )
    garment.data.materials.clear()
    garment.data.materials.append(cloth)
    objs = [garment]

    hx, hy, hz = head_loc
    if outfit == "hoodie":
        # Hood: a shell larger than the head, shifted back, so the face stays
        # clear in front and a rim of fabric shows around and above it.
        hood = _prim(
            "sphere", (hx, hy + 0.2, hz + 0.02), r=0.68, scale=(1.0, 0.95, 1.0)
        )
        # Kangaroo pocket on the belly.
        pocket_y = _body_surface_y(0.0, 0.6) * (1.0 + flare)
        pocket = _prim("cube", (0.0, pocket_y, 0.6), scale=(0.55, 0.1, 0.24))
        # Hood opening trim.
        trim = _prim(
            "torus",
            (hx, hy - 0.42, hz),
            rotation=(math.pi / 2, 0, 0),
            major=0.5,
            minor=0.035,
        )
        accent = make_material(
            "OutfitTrim_hoodie",
            _darker(secondary, 0.7),
            material=surface_material,
        )
        objs.append(
            _assemble(
                "Outfit_hoodie_details",
                [(hood, cloth), (pocket, accent), (trim, accent)],
            )
        )
    return objs


def _shoe_color(palette: dict):
    return hex_to_rgba(palette["accent"])


def build_footwear(footwear: str, palette: dict):
    """A pair of shoes planted at the bottom of the body, toes pointing at the
    viewer. Neither Tier 3 template has legs, so the shoes sit directly under
    the body; they are placed from the body's real bounds so they show below
    and in front of it rather than hiding inside a round body."""
    if footwear == "none":
        return []
    ground = _BODY["bottom"]
    cx = 0.32 * _BODY["build"]
    cy = _BODY["front"] * 0.55 - 0.05
    upper_c = _shoe_color(palette)
    sole_c = hex_to_rgba(palette["secondary"])
    upper = make_material(f"FootwearMat_{footwear}", upper_c)
    sole = make_material(f"FootwearSole_{footwear}", sole_c)
    strap = make_material(f"FootwearStrap_{footwear}", _darker(upper_c, 0.6))

    objs = []
    for side, x in (("L", -cx), ("R", cx)):
        if footwear == "boots":
            parts = [
                (
                    _prim(
                        "sphere",
                        (x, cy, ground + 0.12),
                        scale=(0.17, 0.34, 0.12),
                    ),
                    upper,
                ),
                (
                    _prim(
                        "cylinder",
                        (x, cy + 0.12, ground + 0.3),
                        r=0.14,
                        depth=0.42,
                    ),
                    upper,
                ),
                (
                    _prim(
                        "cylinder",
                        (x, cy + 0.12, ground + 0.5),
                        r=0.16,
                        depth=0.06,
                    ),
                    sole,
                ),
                (
                    _prim(
                        "cube", (x, cy, ground + 0.025), scale=(0.36, 0.7, 0.05)
                    ),
                    strap,
                ),
            ]
        elif footwear == "sandals":
            parts = [
                (
                    _prim(
                        "cube", (x, cy, ground + 0.025), scale=(0.3, 0.66, 0.05)
                    ),
                    sole,
                ),
                (
                    _prim(
                        "cube",
                        (x, cy - 0.1, ground + 0.07),
                        scale=(0.3, 0.07, 0.04),
                    ),
                    strap,
                ),
                (
                    _prim(
                        "cube",
                        (x, cy + 0.12, ground + 0.07),
                        scale=(0.3, 0.07, 0.04),
                    ),
                    strap,
                ),
            ]
        else:  # sneakers
            parts = [
                (
                    _prim(
                        "sphere",
                        (x, cy, ground + 0.12),
                        scale=(0.17, 0.32, 0.11),
                    ),
                    upper,
                ),
                (
                    _prim(
                        "cube",
                        (x, cy, ground + 0.025),
                        scale=(0.36, 0.68, 0.05),
                    ),
                    sole,
                ),
                (
                    _prim(
                        "cube",
                        (x, cy - 0.05, ground + 0.225),
                        scale=(0.1, 0.16, 0.02),
                    ),
                    strap,
                ),
            ]
        objs.append(_assemble(f"Footwear_{footwear}_{side}", parts))
    return objs
