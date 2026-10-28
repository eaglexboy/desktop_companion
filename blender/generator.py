"""Parametric character builder, run headless:

    blender --background --factory-startup --python generator.py -- \
        --spec <LookSpec.json> --out <output.glb> --deploy-dir <deploy_dir>
    blender --background --factory-startup --python generator.py -- \
        --inspect <custom.glb> --out <capabilities.json>

Reads a validated LookSpec (see brain/appearance/spec.py) and procedurally
builds a rigged mesh, a mouth mesh with the full 9-viseme shape-key set (if
has_face) or a talk_pulse armature action (if not), requested wardrobe/
accessories, and the full animation clip catalog -- then exports glTF 2.0
(.glb) with skinning, morph targets, and animations all included. Writes
`<out>.build.json` next to the .glb with the actual shape-key/animation-clip
names and geometry stats produced, which brain/appearance/blender_gen.py
uses as ground truth for the asset manifest (rather than predicting what
this script "should" produce and risking drift between the two).

`--deploy-dir` exists because the 3-tier template/wardrobe/accessory
resolution hierarchy reads real files from <deploy_dir>/assets/, and this
subprocess has no other way to learn that path (no shared Settings object,
no env var for it).

Only ever invoked as a subprocess by blender_gen.py — this file is not
imported by normal Python (bpy only exists inside Blender's interpreter).
"""

from __future__ import annotations

import hashlib
import json
import math
import random
import sys
import time
from pathlib import Path

import bmesh
import bpy
import mathutils

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # repo root, for blender.viseme_shapekeys
from blender.viseme_shapekeys import (
    SHAPE_KEY_PREFIX,
    VISEME_CODES,
    shape_key_name,
)

# --- CLI args (after Blender's own args, separated by "--") -----------------

argv = sys.argv[sys.argv.index("--") + 1 :] if "--" in sys.argv else []


def _arg(flag: str, default: str | None = None) -> str | None:
    if flag in argv:
        return argv[argv.index(flag) + 1]
    return default


SPEC_PATH = _arg("--spec")
OUT_PATH = Path(_arg("--out"))
DEPLOY_DIR = Path(_arg("--deploy-dir", "."))
INSPECT_PATH = _arg("--inspect")

TRIANGLE_BUDGET = 30_000
SIZE_BUDGET_BYTES = 10_485_760

CORE_IDLE_CLIPS: tuple[tuple[str, bool, float], ...] = (
    ("idle_lookaround", True, 4.0),
    ("idle_stretch", True, 3.0),
    ("idle_peek", False, 2.5),
    ("idle_blink", True, 3.5),
)
STATE_LOOP_CLIPS: tuple[tuple[str, bool, float], ...] = (
    ("move", True, 1.0),
    ("think", True, 3.0),
)
MOOD_IDLE_CLIPS: tuple[tuple[str, bool, float], ...] = (
    ("idle_bored", True, 5.0),
    ("idle_curious", True, 4.0),
    ("idle_sleepy", True, 6.0),
    ("idle_fidget", True, 3.0),
    ("idle_focused", True, 4.0),
)
DELIVERY_CLIPS: tuple[tuple[str, bool, float], ...] = (
    ("talk_subdued", True, 2.0),
    ("talk_animated", True, 2.0),
    ("talk_explanatory", True, 2.5),
    ("talk_deadpan", True, 2.0),
)
AUXILIARY_CLIPS: tuple[tuple[str, bool, float], ...] = (
    ("greet", False, 1.8),
    ("celebrate", False, 2.2),
    ("shrug", False, 1.5),
    ("agree", False, 1.2),
    ("disagree", False, 1.2),
)

# (Open, Width, Round, Teeth) per viseme. Teeth is 0/1 and drives a small
# secondary teeth-strip mesh's visibility rather than the mouth plane itself.
VISEME_MOUTH_MATRIX: dict[str, tuple[float, float, float, int]] = {
    "X": (0.0, 1.0, 0.0, 0),
    "A": (0.0, 0.9, 0.0, 0),
    "B": (0.2, 1.1, 0.0, 1),
    "C": (0.5, 1.0, 0.1, 0),
    "D": (1.0, 1.0, 0.2, 0),
    "E": (0.7, 0.7, 0.8, 0),
    "F": (0.3, 0.5, 1.0, 0),
    "G": (0.2, 1.0, 0.0, 1),
    "H": (0.5, 0.9, 0.0, 0),
}

TALK_PULSE_HZ = 4.5
TALK_PULSE_MIN_SCALE = 0.95
TALK_PULSE_MAX_SCALE = 1.08


# --- small helpers ------------------------------------------------------------


def reset_scene() -> None:
    bpy.ops.wm.read_factory_settings(use_empty=True)


def hex_to_rgba(hex_color: str) -> tuple[float, float, float, float]:
    h = hex_color.lstrip("#")
    r, g, b = (int(h[i : i + 2], 16) / 255.0 for i in (0, 2, 4))
    return (r, g, b, 1.0)


def make_material(
    name: str, color: tuple[float, float, float, float], *, material: str = "matte", emissive: bool = False
) -> bpy.types.Material:
    mat = bpy.data.materials.new(name)
    mat.use_nodes = True
    bsdf = mat.node_tree.nodes.get("Principled BSDF")
    if bsdf:
        bsdf.inputs["Base Color"].default_value = color
        if material == "metallic":
            bsdf.inputs["Metallic"].default_value = 1.0
            bsdf.inputs["Roughness"].default_value = 0.3
        elif material == "glossy":
            bsdf.inputs["Roughness"].default_value = 0.05
        elif material == "glass":
            bsdf.inputs["Transmission Weight"].default_value = 1.0
            bsdf.inputs["Roughness"].default_value = 0.0
        if emissive or material == "emissive_circuit":
            if "Emission Color" in bsdf.inputs:
                bsdf.inputs["Emission Color"].default_value = color
            if "Emission Strength" in bsdf.inputs:
                bsdf.inputs["Emission Strength"].default_value = 2.0
    return mat


def write_build_failure(out_build_json: Path, reason: str) -> None:
    out_build_json.write_text(json.dumps({"status": "fail", "reason": reason, "warnings": []}, indent=2))


def fail(out_build_json: Path, reason: str) -> None:
    write_build_failure(out_build_json, reason)
    print(f"DESKMATE_BUILD_FAIL {reason}")
    sys.exit(1)


# --- 3-tier template resolution ----------------------------------------------


def resolve_template(base_template: str, deploy_dir: Path) -> dict:
    """Returns {"tier": ..., "imported": bool, "path": Path|None}. Tier 1/2
    import an existing asset if present; Tier 3 (or a missing Tier 1/2 file)
    means "build procedurally, zero files" -- the fallback that always
    works even before any Tier 1/2 asset has been authored."""
    if base_template.startswith("custom:"):
        name = base_template[len("custom:") :]
        custom_dir = deploy_dir / "assets" / "custom_templates"
        for ext in (".glb", ".blend"):
            path = custom_dir / f"{name}{ext}"
            if path.is_file():
                return {"tier": "tier_1_custom", "imported": True, "path": path}
        return {"tier": "tier_3_primitive", "imported": False, "path": None}

    if base_template in ("standard_humanoid_female", "standard_humanoid_male"):
        path = deploy_dir / "assets" / "base_templates" / f"{base_template}.glb"
        if path.is_file():
            return {"tier": "tier_2_standard", "imported": True, "path": path}
        return {"tier": "tier_3_primitive", "imported": False, "path": None}

    return {"tier": "tier_3_primitive", "imported": False, "path": None}


# --- body / head / eyes / mouth / teeth ---------------------------------------


def build_body_tier3(base_template: str, proportions: dict, palette: dict, surface_material: str):
    """Returns (body_obj, head_location, has_arms, has_legs). Tier 3 only:
    primitive_blob (sphere, no limbs) or primitive_mechanical (boxy torso +
    arms, no legs -- a simple hovering/treaded bot)."""
    primary = hex_to_rgba(palette["primary"])
    height_scale = proportions.get("height", 1.0)
    build_scale = proportions.get("build", 1.0)

    if base_template == "primitive_mechanical":
        bpy.ops.mesh.primitive_cube_add(size=1.4, location=(0, 0, 1.0))
        body = bpy.context.active_object
        body.scale = (0.8 * build_scale, 0.6 * build_scale, 1.0 * height_scale)
        head_loc = (0, 0, 1.95 * height_scale)
        has_arms, has_legs = True, False
    else:  # primitive_blob (default Tier 3 fallback)
        bpy.ops.mesh.primitive_uv_sphere_add(radius=1.0, location=(0, 0, 1.0))
        body = bpy.context.active_object
        body.scale = (1.0 * build_scale, 0.9 * build_scale, 1.15 * height_scale)
        head_loc = (0, -0.85, 1.75 * height_scale)
        has_arms, has_legs = False, False

    body.name = "Body"
    bpy.ops.object.shade_smooth()
    body.data.materials.append(make_material("BodyMat", primary, material=surface_material))
    return body, head_loc, has_arms, has_legs


def build_body_humanoid(proportions: dict, palette: dict, surface_material: str):
    """Procedural stand-in for a standard_humanoid_* model: pelvis, torso, two
    legs, two arms with hands, feet on the ground (z=0) and the head sitting
    on top, facing -Y. Returns (body_obj, head_location, has_arms, has_legs,
    layout) -- `layout` is what build_armature needs to put bones where the
    limbs are. muscularity/femininity reshape the shoulders and hips; height
    and build scale the whole figure like the other templates."""
    primary = hex_to_rgba(palette["primary"])
    h = proportions.get("height", 1.0)
    b = proportions.get("build", 1.0)
    mus = proportions.get("muscularity", 0.5)
    fem = proportions.get("femininity", 0.5)

    shoulder = 0.46 * b * (1.0 + 0.30 * (mus - 0.5) - 0.20 * (fem - 0.5))
    hip = 0.40 * b * (1.0 + 0.25 * (fem - 0.5))
    leg_x = 0.30 * b
    arm_x = shoulder + 0.14
    shoulder_z, hand_z = 1.5 * h, 0.85 * h
    arm_r = 0.11 * b * (1.0 + 0.4 * (mus - 0.5))
    mat = make_material("BodyMat", primary, material=surface_material)

    parts = [
        (_prim("sphere", (0, 0, 0.95 * h), scale=(hip, 0.30 * b, 0.28 * h)), mat),  # pelvis
        (_prim("sphere", (0, 0, 1.22 * h), scale=(shoulder, 0.32 * b, 0.46 * h)), mat),  # torso
        (_prim("cylinder", (0, 0, 1.66 * h), r=0.13 * b, depth=0.2 * h), mat),  # neck
    ]
    for x in (-leg_x, leg_x):
        parts.append((_prim("cylinder", (x, 0, 0.45 * h), r=0.15 * b, depth=0.9 * h), mat))
    for x in (-arm_x, arm_x):
        parts.append((_prim("cylinder", (x, 0, (shoulder_z + hand_z) / 2), r=arm_r, depth=shoulder_z - hand_z), mat))
        parts.append((_prim("sphere", (x, 0, hand_z), r=arm_r * 1.3), mat))  # hand

    body = _assemble("Body", parts)
    # Origin at the bounding-box centre, like the Tier 3 primitives, so the
    # _BODY bounds (footwear height, hoodie pocket) read it the same way.
    bpy.context.view_layer.objects.active = body
    body.select_set(True)
    bpy.ops.object.origin_set(type="ORIGIN_GEOMETRY", center="BOUNDS")
    body.select_set(False)
    head_loc = (0.0, 0.0, 2.05 * h)
    layout = {"arm_x": arm_x, "shoulder_z": shoulder_z, "hand_z": hand_z, "leg_x": leg_x, "hip_z": 0.9 * h}
    return body, head_loc, True, True, layout


def build_head(head_loc) -> bpy.types.Object:
    bpy.ops.mesh.primitive_uv_sphere_add(radius=0.55, location=head_loc)
    head = bpy.context.active_object
    head.name = "Head"
    bpy.ops.object.shade_smooth()
    head.data.materials.append(bpy.data.materials.get("BodyMat"))
    return head


def build_eyes(head_loc, color: tuple[float, float, float, float]):
    eyes = []
    for side, x in (("L", -0.22), ("R", 0.22)):
        bpy.ops.mesh.primitive_uv_sphere_add(
            radius=0.08, location=(head_loc[0] + x, head_loc[1] - 0.48, head_loc[2] + 0.05)
        )
        eye = bpy.context.active_object
        eye.name = f"Eye_{side}"
        eye.data.materials.append(make_material(f"EyeMat_{side}", (0.05, 0.05, 0.05, 1.0)))
        eyes.append(eye)
    return eyes


def _build_shape_keyed_plane(
    name: str, size: float, location, rotation, material, params_by_code: dict, rest_code: str = "X"
) -> tuple:
    """The plane is built flat in local XY and rotated by the *object*
    transform to face forward, so local X is width, local Y is height, and
    local Z is the forward/back (pucker) axis.

    The mesh itself (and so the Basis shape key, which is what every
    morph-target weight of 0 renders) is set to the REST viseme's shape, so a
    client that leaves all weights at 0 shows a closed, resting mouth rather
    than the fully-open source plane. Every viseme key is an absolute shape
    deformed from the original plane; the rest viseme's key therefore has a
    zero delta."""
    bpy.ops.mesh.primitive_plane_add(size=size, location=location)
    obj = bpy.context.active_object
    obj.name = name
    obj.rotation_euler = rotation
    bpy.ops.object.mode_set(mode="EDIT")
    bpy.ops.mesh.subdivide(number_cuts=3)
    bpy.ops.object.mode_set(mode="OBJECT")
    obj.data.materials.append(material)

    base_coords = [v.co.copy() for v in obj.data.vertices]
    rest_fn = params_by_code[rest_code]
    for v, co in zip(obj.data.vertices, base_coords):
        v.co.x, v.co.y, v.co.z = rest_fn(co)
    obj.shape_key_add(name="Basis")  # captures the rest shape set just above
    created = []
    for code in VISEME_CODES:
        key_name = shape_key_name(code)
        sk = obj.shape_key_add(name=key_name)
        deform_fn = params_by_code[code]
        for i, co in enumerate(base_coords):
            new_co = deform_fn(co)
            sk.data[i].co.x = new_co[0]
            sk.data[i].co.y = new_co[1]
            sk.data[i].co.z = new_co[2]
        created.append(key_name)
    return obj, created


def build_mouth(head_loc, has_face: bool) -> tuple:
    """Mode A (has_face=True): a subdivided plane with a Basis shape key plus
    one shape key per viseme code, deformed per the (Open, Width, Round)
    mouth-matrix values. A secondary small teeth-strip mesh, same technique,
    uses the matrix's Teeth flag to toggle its own visibility per viseme.
    Returns (mouth_obj, teeth_obj, viseme_shape_key_names)."""
    if not has_face:
        return None, None, []

    mouth_mat = make_material("MouthMat", (0.35, 0.05, 0.08, 1.0))

    def mouth_deform(code):
        open_, width, round_, _teeth = VISEME_MOUTH_MATRIX[code]

        def fn(co):
            x = co.x * width * (1.0 - round_ * 0.25)
            y = co.y * (0.15 + open_ * 0.85)  # local Y is height: this is the mouth opening
            z = co.z + round_ * 0.04  # local Z is forward: slight pucker bulge
            return (x, y, z)

        return fn

    mouth, viseme_keys = _build_shape_keyed_plane(
        "Mouth",
        0.28,
        (head_loc[0], head_loc[1] - 0.5, head_loc[2] - 0.18),
        (math.radians(90), 0, 0),
        mouth_mat,
        {code: mouth_deform(code) for code in VISEME_CODES},
    )

    teeth_mat = make_material("TeethMat", (0.95, 0.95, 0.9, 1.0))

    def teeth_deform(code):
        _open, _width, _round, teeth = VISEME_MOUTH_MATRIX[code]
        visible_scale = 1.0 if teeth else 0.02

        def fn(co):
            return (co.x * visible_scale, co.y * visible_scale, co.z)

        return fn

    teeth, _teeth_keys = _build_shape_keyed_plane(
        "Teeth",
        0.14,
        (head_loc[0], head_loc[1] - 0.52, head_loc[2] - 0.12),
        (math.radians(90), 0, 0),
        teeth_mat,
        {code: teeth_deform(code) for code in VISEME_CODES},
    )

    return mouth, teeth, viseme_keys


# --- wardrobe: procedural garments + socket attachments -----------------------
#
# Blender space: +Z up, the avatar faces -Y (the overlay camera looks at it
# from -Y), +X is the avatar's left as seen by the viewer. Every catalog item
# below is its own recognisable shape -- the companion is told what it is
# wearing (see brain/appearance/spec.py's look_summary), so an item that renders
# as something else, or not at all, makes it describe a look nobody can see.

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


def _prim(kind: str, location, *, scale=(1.0, 1.0, 1.0), rotation=(0.0, 0.0, 0.0), **kw):
    """One primitive, already scaled/rotated. Dimensions are in metres and
    describe the unscaled primitive (spheres by radius, cubes by edge)."""
    ops = bpy.ops.mesh
    if kind == "sphere":
        ops.primitive_uv_sphere_add(radius=kw.get("r", 1.0), segments=24, ring_count=12, location=location)
    elif kind == "torus":
        ops.primitive_torus_add(
            major_radius=kw["major"], minor_radius=kw["minor"], major_segments=32, minor_segments=8, location=location
        )
    elif kind == "cylinder":
        ops.primitive_cylinder_add(radius=kw["r"], depth=kw["depth"], vertices=24, location=location)
    elif kind == "cone":
        ops.primitive_cone_add(
            radius1=kw["r"], radius2=kw.get("r2", 0.0), depth=kw["depth"], vertices=24, location=location
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
        obj.data.materials.append(mat)
    bpy.ops.object.select_all(action="DESELECT")
    for obj, _mat in parts:
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
    ray_dir = (inv.to_3x3() @ mathutils.Vector((0.0, direction, 0.0))).normalized()
    hit, location, _normal, _index = body.ray_cast(origin, ray_dir)
    if hit:
        return (body.matrix_world @ location).y
    return _BODY["front"] if front else _BODY["back"]


def _body_half_width_at(z: float) -> float:
    """Half-width of the body at height z (ray cast in from the +X side)."""
    body = _BODY["obj"]
    inv = body.matrix_world.inverted()
    origin = inv @ mathutils.Vector((6.0, 0.0, z))
    ray_dir = (inv.to_3x3() @ mathutils.Vector((-1.0, 0.0, 0.0))).normalized()
    hit, location, _normal, _index = body.ray_cast(origin, ray_dir)
    return (body.matrix_world @ location).x if hit else _BODY["half_width"]


def build_outfit(body_obj, outfit: str, head_loc, palette: dict, surface_material: str) -> list:
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
    garment.scale = (garment.scale[0] * (1.0 + flare), garment.scale[1] * (1.0 + flare), garment.scale[2])

    solidify = garment.modifiers.new("OutfitSolidify", type="SOLIDIFY")
    solidify.thickness = thickness
    bpy.context.view_layer.objects.active = garment
    bpy.ops.object.modifier_apply(modifier=solidify.name)

    secondary = hex_to_rgba(palette["secondary"])
    cloth = make_material(f"OutfitMat_{outfit}", secondary, material=surface_material)
    garment.data.materials.clear()
    garment.data.materials.append(cloth)
    objs = [garment]

    hx, hy, hz = head_loc
    if outfit == "hoodie":
        # Hood: a shell larger than the head, shifted back, so the face stays
        # clear in front and a rim of fabric shows around and above it.
        hood = _prim("sphere", (hx, hy + 0.2, hz + 0.02), r=0.68, scale=(1.0, 0.95, 1.0))
        # Kangaroo pocket on the belly.
        pocket_y = _body_surface_y(0.0, 0.6) * (1.0 + flare)
        pocket = _prim("cube", (0.0, pocket_y, 0.6), scale=(0.55, 0.1, 0.24))
        # Hood opening trim.
        trim = _prim("torus", (hx, hy - 0.42, hz), rotation=(math.pi / 2, 0, 0), major=0.5, minor=0.035)
        accent = make_material("OutfitTrim_hoodie", _darker(secondary, 0.7), material=surface_material)
        objs.append(_assemble("Outfit_hoodie_details", [(hood, cloth), (pocket, accent), (trim, accent)]))
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
                (_prim("sphere", (x, cy, ground + 0.12), scale=(0.17, 0.34, 0.12)), upper),
                (_prim("cylinder", (x, cy + 0.12, ground + 0.3), r=0.14, depth=0.42), upper),
                (_prim("cylinder", (x, cy + 0.12, ground + 0.5), r=0.16, depth=0.06), sole),
                (_prim("cube", (x, cy, ground + 0.025), scale=(0.36, 0.7, 0.05)), strap),
            ]
        elif footwear == "sandals":
            parts = [
                (_prim("cube", (x, cy, ground + 0.025), scale=(0.3, 0.66, 0.05)), sole),
                (_prim("cube", (x, cy - 0.1, ground + 0.07), scale=(0.3, 0.07, 0.04)), strap),
                (_prim("cube", (x, cy + 0.12, ground + 0.07), scale=(0.3, 0.07, 0.04)), strap),
            ]
        else:  # sneakers
            parts = [
                (_prim("sphere", (x, cy, ground + 0.12), scale=(0.17, 0.32, 0.11)), upper),
                (_prim("cube", (x, cy, ground + 0.025), scale=(0.36, 0.68, 0.05)), sole),
                (_prim("cube", (x, cy - 0.05, ground + 0.225), scale=(0.1, 0.16, 0.02)), strap),
            ]
        objs.append(_assemble(f"Footwear_{footwear}_{side}", parts))
    return objs


_SOCKET_LOCATIONS = {
    "socket_head_top": lambda head_loc: (head_loc[0], head_loc[1], head_loc[2] + 0.65),
    "socket_eyes": lambda head_loc: (head_loc[0], head_loc[1] - 0.5, head_loc[2] + 0.05),
    "socket_neck": lambda head_loc: (head_loc[0], head_loc[1], head_loc[2] - 0.35),
    "socket_chest": lambda head_loc: (0, 0, 1.1),
    "socket_waist": lambda head_loc: (0, 0, 0.75),
    "socket_hand_R": lambda head_loc: (_body_half_width_at(0.9) + 0.2, -0.2, 0.9),
    "socket_hand_L": lambda head_loc: (-(_body_half_width_at(0.9) + 0.2), -0.2, 0.9),
    "socket_orbit": lambda head_loc: (head_loc[0], head_loc[1], head_loc[2] + 0.65),
}

_HEAD_R = 0.55  # build_head's radius


def _eyewear(item: str, loc, palette: dict):
    """Everything faces -Y: rings/discs are rotated so their axis points at the
    viewer (a torus is built with its axis on Z, which seen from the front is
    a flat line across the eyes)."""
    x0, y0, z0 = loc
    y0 -= 0.04  # just in front of the eyeballs
    face = (math.pi / 2, 0.0, 0.0)
    accent = make_material(f"EyewearFrame_{item}", hex_to_rgba(palette["accent"]))
    glow = make_material(f"EyewearLens_{item}", hex_to_rgba(palette["emissive"]), material="glossy", emissive=True)
    dark = make_material(f"EyewearDark_{item}", _DARK, material="glossy")
    parts = []
    if item == "glasses":
        for x in (-0.22, 0.22):
            parts.append((_prim("torus", (x0 + x, y0, z0), rotation=face, major=0.13, minor=0.014), accent))
        parts.append((_prim("cylinder", (x0, y0, z0 + 0.02), r=0.012, depth=0.1, rotation=(0, math.pi / 2, 0)), accent))
    elif item == "sunglasses":
        for x in (-0.22, 0.22):
            parts.append((_prim("cylinder", (x0 + x, y0, z0), r=0.15, depth=0.02, rotation=face), dark))
            parts.append((_prim("torus", (x0 + x, y0, z0), rotation=face, major=0.15, minor=0.015), accent))
        parts.append((_prim("cylinder", (x0, y0, z0 + 0.03), r=0.012, depth=0.1, rotation=(0, math.pi / 2, 0)), accent))
    elif item == "goggles":
        for x in (-0.22, 0.22):
            parts.append((_prim("cylinder", (x0 + x, y0 - 0.005, z0), r=0.15, depth=0.03, rotation=face), glow))
            parts.append((_prim("torus", (x0 + x, y0, z0), rotation=face, major=0.16, minor=0.035), accent))
        parts.append((_prim("cube", (x0, y0, z0), scale=(0.1, 0.05, 0.06)), accent))
        # Strap round the head at eye height (a torus, axis up, is a ring seen from above).
        parts.append((_prim("torus", (x0, y0 + 0.5, z0), major=_HEAD_R + 0.01, minor=0.03), accent))
    else:  # visor: one wide glowing band across both eyes
        parts.append((_prim("sphere", (x0, y0 - 0.02, z0), r=1.0, scale=(0.5, 0.08, 0.12)), glow))
        parts.append((_prim("cube", (x0, y0 - 0.02, z0 + 0.12), scale=(0.9, 0.07, 0.03)), accent))
        for x in (-0.5, 0.5):
            parts.append((_prim("sphere", (x0 + x, y0 + 0.1, z0), r=0.07, scale=(0.8, 1.6, 1.4)), accent))
    return parts


def _headwear(item: str, loc, palette: dict):
    x0, y0, z0 = loc
    top = z0 - 0.1  # top of the head
    # Kept low on purpose: the overlay's fixed camera frames the head close to
    # the top edge, so anything much taller than ~0.3m above it is cropped.
    accent = make_material(f"HeadMat_{item}", hex_to_rgba(palette["accent"]))
    glow = make_material(f"HeadGlow_{item}", hex_to_rgba(palette["emissive"]), emissive=True)
    parts = []
    if item == "crown":
        parts.append((_prim("cylinder", (x0, y0, top + 0.05), r=0.3, depth=0.1), accent))
        for i in range(5):
            a = i * 2 * math.pi / 5
            parts.append((_prim("cone", (x0 + 0.26 * math.cos(a), y0 + 0.26 * math.sin(a), top + 0.14), r=0.05, depth=0.12), accent))
    elif item == "horns":
        for sign in (-1, 1):
            parts.append(
                (_prim("cone", (x0 + sign * 0.32, y0, top + 0.08), r=0.08, depth=0.26, rotation=(0, sign * 0.55, 0)), accent)
            )
    elif item == "antenna":
        parts.append((_prim("cylinder", (x0, y0, top + 0.09), r=0.015, depth=0.18), accent))
        parts.append((_prim("sphere", (x0, y0, top + 0.2), r=0.055), glow))
    else:  # pointy_hat
        parts.append((_prim("cylinder", (x0, y0, top + 0.02), r=0.42, depth=0.03), accent))
        parts.append((_prim("cone", (x0, y0, top + 0.12), r=0.27, depth=0.22), accent))
    return parts


def _neckwear(item: str, loc, palette: dict):
    x0, y0, z0 = loc
    ring_r = math.sqrt(_HEAD_R**2 - 0.35**2) + 0.02  # head radius at this height
    accent = make_material(f"NeckMat_{item}", hex_to_rgba(palette["accent"]))
    parts = []
    if item == "tie":
        chin = z0 - 0.2  # head centre is 0.35 above this socket; its lowest point is another 0.2 down
        knot_y = _body_surface_y(0.0, chin - 0.1) - 0.01
        parts.append((_prim("sphere", (x0, knot_y, chin - 0.06), r=0.06), accent))
        parts.append((_prim("cone", (x0, knot_y, chin - 0.25), r=0.1, r2=0.03, depth=0.28, rotation=(math.pi, 0, 0), scale=(1.0, 0.35, 1.0)), accent))
    elif item == "collar":
        parts.append((_prim("torus", (x0, y0, z0), major=ring_r + 0.03, minor=0.05), accent))
        for x in (-0.08, 0.08):
            parts.append((_prim("cone", (x0 + x, y0 - ring_r - 0.02, z0 - 0.06), r=0.08, depth=0.18, rotation=(math.pi, 0, 0)), accent))
    else:  # scarf
        parts.append((_prim("torus", (x0, y0, z0), major=ring_r, minor=0.085), accent))
        parts.append((_prim("cube", (x0 + 0.2, y0 - ring_r + 0.02, z0 - 0.2), scale=(0.14, 0.05, 0.4)), accent))
    return parts


def _chestwear(item: str, loc, palette: dict):
    accent = make_material(f"ChestMat_{item}", hex_to_rgba(palette["accent"]))
    dark = make_material(f"ChestDark_{item}", _darker(hex_to_rgba(palette["accent"]), 0.5))
    if item == "backpack":
        back = _body_surface_y(0.0, 1.0, front=False)
        return [
            (_prim("cube", (0.0, back + 0.14, 1.0), scale=(0.55, 0.3, 0.65)), accent),
            (_prim("cube", (0.0, back + 0.32, 0.85), scale=(0.35, 0.06, 0.25)), dark),
        ]
    x = 0.28
    y = _body_surface_y(x, loc[2])
    return [(_prim("cylinder", (x, y, loc[2]), r=0.09, depth=0.04, rotation=(math.pi / 2, 0, 0)), accent)]  # badge


def _waistwear(item: str, loc, palette: dict):
    z = loc[2]
    accent = make_material(f"WaistMat_{item}", hex_to_rgba(palette["accent"]))
    dark = make_material(f"WaistDark_{item}", _darker(hex_to_rgba(palette["accent"]), 0.5))
    half_w = _body_half_width_at(z)
    front = _body_surface_y(0.0, z)
    back = _body_surface_y(0.0, z, front=False)
    if item == "pouch":
        x = half_w * 0.55
        return [(_prim("sphere", (x, _body_surface_y(x, z) - 0.02, z - 0.05), r=1.0, scale=(0.18, 0.12, 0.2)), accent)]
    # belt: a ring scaled to the body's cross-section at this height (+ margin for an outfit)
    rx, ry = (half_w * 1.1), ((front - back) / -2.0) * 1.1
    cy = (front + back) / 2.0
    return [
        (_prim("torus", (0.0, cy, z), major=1.0, minor=0.04 / max(rx, 0.1), scale=(rx, ry, 1.0)), dark),
        (_prim("cube", (0.0, front * 1.1 - 0.01, z), scale=(0.16, 0.04, 0.12)), accent),
    ]


def _handheld(item: str, loc, palette: dict):
    x0, y0, z0 = loc
    accent = make_material(f"HandMat_{item}", hex_to_rgba(palette["accent"]))
    glow = make_material(f"HandGlow_{item}", hex_to_rgba(palette["emissive"]), emissive=True)
    dark = make_material(f"HandDark_{item}", _darker(hex_to_rgba(palette["secondary"]), 0.5))
    if item == "wand":
        return [
            (_prim("cylinder", (x0, y0, z0 + 0.1), r=0.02, depth=0.7, rotation=(0, 0.25, 0)), dark),
            (_prim("sphere", (x0 + 0.09, y0, z0 + 0.45), r=0.06), glow),
        ]
    if item == "tool":
        return [
            (_prim("cylinder", (x0, y0, z0), r=0.03, depth=0.6), dark),
            (_prim("cube", (x0, y0, z0 + 0.34), scale=(0.2, 0.06, 0.1)), accent),
        ]
    if item == "shield":
        return [
            (_prim("cylinder", (x0, y0 - 0.05, z0), r=0.3, depth=0.05, rotation=(math.pi / 2, 0, 0)), accent),
            (_prim("sphere", (x0, y0 - 0.1, z0), r=0.07), dark),
        ]
    return [(_prim("sphere", (x0, y0, z0), r=0.16), glow)]  # orb


def _orbiting(item: str, loc, palette: dict):
    x0, y0, z0 = loc
    accent = make_material(f"OrbitMat_{item}", hex_to_rgba(palette["accent"]))
    glow = make_material(f"OrbitGlow_{item}", hex_to_rgba(palette["emissive"]), emissive=True)
    if item == "floating_cube":
        return [(_prim("cube", (x0 + 0.85, y0 - 0.1, z0 - 0.5), scale=(0.2, 0.2, 0.2), rotation=(0.6, 0.4, 0.5)), glow)]
    return [(_prim("torus", (x0, y0, z0), rotation=(0.15, 0, 0), major=0.4, minor=0.03), accent)]  # crystal_ring (a halo)


_ACCESSORY_BUILDERS = {
    "socket_eyes": _eyewear,
    "socket_head_top": _headwear,
    "socket_neck": _neckwear,
    "socket_chest": _chestwear,
    "socket_waist": _waistwear,
    "socket_hand_R": _handheld,
    "socket_hand_L": _handheld,
    "socket_orbit": _orbiting,
}


def build_socket_accessory(socket_name: str, item: str, head_loc, palette: dict, deploy_dir: Path):
    """Discovers a pre-modeled asset under <deploy_dir>/assets/wardrobe/<socket>/<item>.glb;
    falls back to a parametric model of the named item if absent (keeps
    accessories testable with zero authored assets). Each catalog item has its
    own shape -- they are not interchangeable primitives."""
    location = _SOCKET_LOCATIONS[socket_name](head_loc)
    asset_path = deploy_dir / "assets" / "wardrobe" / socket_name / f"{item}.glb"
    if asset_path.is_file():
        before = {obj.name for obj in bpy.data.objects}
        bpy.ops.import_scene.gltf(filepath=str(asset_path))
        imported = [obj for obj in bpy.data.objects if obj.name not in before]
        for obj in imported:
            obj.location = location
            obj.name = f"Accessory_{socket_name}_{obj.name}"
        return imported

    parts = _ACCESSORY_BUILDERS[socket_name](item, location, palette)
    return [_assemble(f"Accessory_{socket_name}_{item}", parts)]


# --- armature / rigging --------------------------------------------------------


def build_armature(has_arms: bool, head_loc, *, has_legs: bool = False, layout: dict | None = None) -> bpy.types.Object:
    bpy.ops.object.armature_add(enter_editmode=True, location=(0, 0, 0))
    arm_obj = bpy.context.active_object
    arm_obj.name = "Rig"
    eb = arm_obj.data.edit_bones
    eb.remove(eb[0])  # drop the default bone, build our own set

    root = eb.new("root")
    root.head, root.tail = (0, 0, 0), (0, 0, 0.3)

    spine = eb.new("spine")
    spine.head, spine.tail = (0, 0, 0.3), (0, 0, head_loc[2] - 0.4)
    spine.parent = root

    head_bone = eb.new("head")
    head_bone.head, head_bone.tail = (0, 0, head_loc[2] - 0.4), (0, 0, head_loc[2] + 0.3)
    head_bone.parent = spine

    if has_arms:
        for side, x in (("L", -0.7), ("R", 0.7)):
            arm = eb.new(f"arm_{side}")
            if layout is not None:  # limbs that hang straight down from the shoulder
                ax = x / 0.7 * layout["arm_x"]
                arm.head, arm.tail = (ax, 0, layout["shoulder_z"]), (ax, 0, layout["hand_z"])
            else:
                arm.head, arm.tail = (0, 0, head_loc[2] - 0.6), (x, 0, head_loc[2] - 1.0)
            arm.parent = spine

    if has_legs and layout is not None:
        for side, sign in (("L", -1), ("R", 1)):
            leg = eb.new(f"leg_{side}")
            leg.head, leg.tail = (sign * layout["leg_x"], 0, layout["hip_z"]), (sign * layout["leg_x"], 0, 0.05)
            leg.parent = root

    bpy.ops.object.mode_set(mode="OBJECT")
    return arm_obj


def parent_to_armature(arm_obj, meshes):
    meshes = [m for m in meshes if m is not None]
    bpy.ops.object.select_all(action="DESELECT")
    for m in meshes:
        m.select_set(True)
    arm_obj.select_set(True)
    bpy.context.view_layer.objects.active = arm_obj
    bpy.ops.object.parent_set(type="ARMATURE_AUTO")
    for m in meshes:
        m.select_set(False)


# --- proportion baking ---------------------------------------------------------


def bake_proportions(arm_obj, proportions: dict) -> None:
    """height -> Z-scale of spine/head/arm bones; build -> X/Y-scale of the
    same -- applied in Pose mode, then committed via "Pose as Rest Pose".
    Tier 3's body mesh already bakes height/build into its own initial
    scale (see
    build_body_tier3) since there's no separate body-mesh-vs-skeleton
    decoupling for a procedural primitive; this additionally scales the
    skeleton itself so any socket-attached/wardrobe geometry parented to a
    bone inherits the same proportions.
    """
    height = proportions.get("height", 1.0)
    build = proportions.get("build", 1.0)
    if height == 1.0 and build == 1.0:
        return  # nothing to bake, skip the mode-switch overhead

    bpy.context.view_layer.objects.active = arm_obj
    bpy.ops.object.mode_set(mode="POSE")
    for bone_name in ("spine", "head"):
        pbone = arm_obj.pose.bones.get(bone_name)
        if pbone:
            pbone.scale = (build, build, height)
    # "Pose as Rest Pose" -- bpy.ops.pose.armature_apply must run while still
    # in Pose Mode (its poll requires context.mode == 'POSE'); switch to
    # Object Mode only after it's applied.
    bpy.ops.pose.armature_apply(selected=False)
    bpy.ops.object.mode_set(mode="OBJECT")


def bake_morphological_proportions(body_obj, proportions: dict) -> None:
    """Evaluates muscularity/femininity shape keys at target weights, bakes
    into Basis, removes from the runtime list. A no-op for Tier 3
    primitives, which have no such shape keys to bake in the first place --
    this only does real work once a Tier 2 base template with pre-authored
    muscularity/femininity shape keys exists. Written defensively (checks
    for key presence) so it's forward-compatible with that asset arriving
    later without needing this function to change."""
    if body_obj is None or body_obj.data.shape_keys is None:
        return
    key_blocks = body_obj.data.shape_keys.key_blocks
    for morph_name in ("muscularity", "femininity"):
        if morph_name not in key_blocks:
            continue
        key_blocks[morph_name].value = proportions.get(morph_name, 0.5)
    # Baking an evaluated shape-key mix into Basis and removing the
    # intermediate keys is a mesh-data operation (bpy.ops.object.shape_key_*
    # doesn't have a direct "bake all to basis" op) -- deferred to when a
    # real Tier 2 asset exists to implement and test this against for real,
    # per the same reasoning as above.


# --- dual speech indicators: Mode B (faceless) ---------------------------------


def build_talk_pulse(arm_obj) -> str | None:
    """Mode B (has_face=False): spine Z-scale pulses between 0.95 and 1.08 at
    4.5Hz, baked as a looping action named talk_pulse. The presentation
    client separately modulates the static emissive material's intensity in
    real time against audio RMS -- that's client-side, not baked here."""
    if "spine" not in arm_obj.pose.bones:
        return None
    period_s = 1.0 / TALK_PULSE_HZ
    fps = bpy.context.scene.render.fps
    period_frames = max(2, round(period_s * fps))
    half = max(1, period_frames // 2)
    action = bpy.data.actions.new("talk_pulse")
    arm_obj.animation_data_create()
    arm_obj.animation_data.action = action
    pbone = arm_obj.pose.bones["spine"]
    pbone.rotation_mode = "XYZ"
    for frame, scale_z in ((1, TALK_PULSE_MIN_SCALE), (1 + half, TALK_PULSE_MAX_SCALE), (1 + period_frames, TALK_PULSE_MIN_SCALE)):
        pbone.scale = (1.0, 1.0, scale_z)
        pbone.keyframe_insert(data_path="scale", frame=frame)
    action.use_fake_user = True
    return "talk_pulse"


# --- animation clips ------------------------------------------------------------


def _insert_bone_keyframes(arm_obj, bone_name, frame, *, rot_euler=None, scale=None):
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


def bake_armature_clip(arm_obj, clip_name: str, keyframes: dict) -> None:
    """`keyframes`: {frame: {bone_name: {"rot": (x,y,z) rad, "scale": (x,y,z)}}}"""
    action = bpy.data.actions.new(clip_name)
    arm_obj.animation_data_create()
    arm_obj.animation_data.action = action
    for frame, bones in keyframes.items():
        bpy.context.scene.frame_set(frame)
        for bone_name, ops in bones.items():
            _insert_bone_keyframes(arm_obj, bone_name, frame, rot_euler=ops.get("rot"), scale=ops.get("scale"))
    action.use_fake_user = True


def bake_object_clip(obj, clip_name: str, keyframes: dict) -> None:
    """`keyframes`: {frame: (sx, sy, sz)} applied to obj.scale."""
    action = bpy.data.actions.new(f"{clip_name}.{obj.name}")
    obj.animation_data_create()
    obj.animation_data.action = action
    for frame, scale in keyframes.items():
        bpy.context.scene.frame_set(frame)
        obj.scale = scale
        obj.keyframe_insert(data_path="scale", frame=frame)
    action.use_fake_user = True


R = math.radians


def build_all_clips(arm_obj, eyes, *, has_arms: bool, has_legs: bool, has_head: bool = True) -> list[str]:
    """Bakes the full animation catalog, applying capability-degradation
    fallbacks: arm-dependent clips (greet/celebrate/shrug) are omitted
    cleanly when has_arms is False; move falls back to squash-and-stretch
    hopping when has_legs is False (every Tier 3 primitive lacks legs, so
    this is always the active path for procedural bodies); think engages a
    pulsing spine oscillation regardless of arms. Returns the list of clip
    names actually baked."""
    baked: list[str] = []

    # --- core idles ---
    bake_armature_clip(arm_obj, "idle_lookaround", {1: {"head": {"rot": (0, 0, 0)}}, 30: {"head": {"rot": (0, 0, R(25))}}, 60: {"head": {"rot": (0, 0, R(-25))}}, 90: {"head": {"rot": (0, 0, 0)}}})
    baked.append("idle_lookaround")
    bake_armature_clip(arm_obj, "idle_stretch", {1: {"spine": {"scale": (1, 1, 1)}}, 20: {"spine": {"scale": (1.05, 1.05, 1.15)}}, 40: {"spine": {"scale": (1, 1, 1)}}})
    baked.append("idle_stretch")
    bake_armature_clip(arm_obj, "idle_peek", {1: {"head": {"rot": (0, 0, 0)}}, 15: {"head": {"rot": (R(10), 0, R(15))}}, 45: {"head": {"rot": (R(10), 0, R(15))}}, 60: {"head": {"rot": (0, 0, 0)}}})
    baked.append("idle_peek")
    for eye in eyes:
        bake_object_clip(eye, "idle_blink", {1: (1, 1, 1), 10: (1, 1, 1), 12: (1, 1, 0.1), 14: (1, 1, 1)})
    if eyes:
        baked.append("idle_blink")

    # --- state loops ---
    if has_legs:
        bake_armature_clip(arm_obj, "move", {1: {"root": {"scale": (1, 1, 1)}}, 15: {"root": {"scale": (1, 1, 1)}}})
    else:
        # hop/hover fallback for legless bodies
        bake_armature_clip(arm_obj, "move", {1: {"root": {"scale": (1, 1, 1)}}, 8: {"root": {"scale": (1.05, 1.05, 0.9)}}, 15: {"root": {"scale": (1, 1, 1)}}})
    baked.append("move")

    if has_arms:
        bake_armature_clip(arm_obj, "think", {1: {"head": {"rot": (0, 0, 0)}}, 40: {"head": {"rot": (0, R(15), R(-10))}}, 80: {"head": {"rot": (0, 0, 0)}}})
    else:
        bake_armature_clip(arm_obj, "think", {1: {"spine": {"scale": (1, 1, 1)}}, 40: {"spine": {"scale": (1.03, 1.03, 0.98)}}, 80: {"spine": {"scale": (1, 1, 1)}}})
    baked.append("think")

    # --- mood idles ---
    bake_armature_clip(arm_obj, "idle_bored", {1: {"head": {"rot": (0, 0, 0)}}, 75: {"head": {"rot": (R(-5), 0, 0)}}, 150: {"head": {"rot": (0, 0, 0)}}})
    baked.append("idle_bored")
    bake_armature_clip(arm_obj, "idle_curious", {1: {"head": {"rot": (0, 0, 0)}}, 60: {"head": {"rot": (R(8), 0, R(12))}}, 120: {"head": {"rot": (0, 0, 0)}}})
    baked.append("idle_curious")
    bake_armature_clip(arm_obj, "idle_sleepy", {1: {"head": {"rot": (0, 0, 0)}}, 90: {"head": {"rot": (R(15), 0, 0)}}, 180: {"head": {"rot": (0, 0, 0)}}})
    baked.append("idle_sleepy")
    bake_armature_clip(arm_obj, "idle_fidget", {1: {"spine": {"rot": (0, 0, 0)}}, 45: {"spine": {"rot": (0, 0, R(5))}}, 90: {"spine": {"rot": (0, 0, 0)}}})
    baked.append("idle_fidget")
    bake_armature_clip(arm_obj, "idle_focused", {1: {"head": {"rot": (0, 0, 0)}}, 60: {"head": {"rot": (0, 0, R(3))}}, 120: {"head": {"rot": (0, 0, 0)}}})
    baked.append("idle_focused")

    # --- conversational delivery gestures ---
    bake_armature_clip(arm_obj, "talk_subdued", {1: {"head": {"rot": (0, 0, 0)}}, 30: {"head": {"rot": (R(-3), 0, 0)}}, 60: {"head": {"rot": (0, 0, 0)}}})
    baked.append("talk_subdued")
    bake_armature_clip(arm_obj, "talk_animated", {1: {"head": {"rot": (0, 0, 0)}}, 30: {"head": {"rot": (R(8), 0, R(10))}}, 60: {"head": {"rot": (0, 0, 0)}}})
    baked.append("talk_animated")
    bake_armature_clip(arm_obj, "talk_explanatory", {1: {"head": {"rot": (0, 0, 0)}}, 37: {"head": {"rot": (0, R(10), R(-8))}}, 75: {"head": {"rot": (0, 0, 0)}}})
    baked.append("talk_explanatory")
    bake_armature_clip(arm_obj, "talk_deadpan", {1: {"head": {"rot": (0, 0, 0)}}, 30: {"head": {"rot": (0, 0, 0)}}, 60: {"head": {"rot": (0, 0, 0)}}})
    baked.append("talk_deadpan")

    # --- auxiliary gestures (arm-dependent ones omitted cleanly if no arms) ---
    if has_arms:
        bake_armature_clip(arm_obj, "greet", {1: {"arm_R": {"rot": (0, 0, 0)}}, 15: {"arm_R": {"rot": (0, 0, R(120))}}, 30: {"arm_R": {"rot": (0, R(30), R(90))}}, 45: {"arm_R": {"rot": (0, 0, R(120))}}, 54: {"arm_R": {"rot": (0, 0, 0)}}})
        baked.append("greet")
        bake_armature_clip(arm_obj, "shrug", {1: {"arm_L": {"rot": (0, 0, 0)}, "arm_R": {"rot": (0, 0, 0)}}, 22: {"arm_L": {"rot": (0, 0, R(-40))}, "arm_R": {"rot": (0, 0, R(40))}}, 45: {"arm_L": {"rot": (0, 0, 0)}, "arm_R": {"rot": (0, 0, 0)}}})
        baked.append("shrug")
    bake_armature_clip(arm_obj, "celebrate", {1: {"root": {"scale": (1, 1, 1)}}, 15: {"root": {"scale": (1.1, 1.1, 0.85)}}, 30: {"root": {"scale": (0.95, 0.95, 1.1)}}, 44: {"root": {"scale": (1, 1, 1)}}})
    baked.append("celebrate")
    bake_armature_clip(arm_obj, "agree", {1: {"head": {"rot": (0, 0, 0)}}, 12: {"head": {"rot": (R(15), 0, 0)}}, 24: {"head": {"rot": (0, 0, 0)}}, 36: {"head": {"rot": (R(15), 0, 0)}}})
    baked.append("agree")
    bake_armature_clip(arm_obj, "disagree", {1: {"head": {"rot": (0, 0, 0)}}, 12: {"head": {"rot": (0, R(20), 0)}}, 24: {"head": {"rot": (0, 0, 0)}}, 36: {"head": {"rot": (0, R(20), 0)}}})
    baked.append("disagree")

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


def clip_metadata(baked_names: list[str]) -> list[dict]:
    return [
        {"name": name, "loop": _ALL_CLIP_META[name][0], "duration_s": _ALL_CLIP_META[name][1]}
        for name in baked_names
        if name in _ALL_CLIP_META
    ]


# --- budget enforcement + export -----------------------------------------------


def count_triangles() -> int:
    depsgraph = bpy.context.evaluated_depsgraph_get()
    total = 0
    for obj in bpy.context.scene.objects:
        if obj.type != "MESH":
            continue
        eval_obj = obj.evaluated_get(depsgraph)
        me = eval_obj.to_mesh()
        bm = bmesh.new()
        bm.from_mesh(me)
        bmesh.ops.triangulate(bm, faces=bm.faces[:])
        total += len(bm.faces)
        bm.free()
        eval_obj.to_mesh_clear()
    return total


def recalc_normals() -> None:
    for obj in bpy.context.scene.objects:
        if obj.type != "MESH":
            continue
        bpy.context.view_layer.objects.active = obj
        bpy.ops.object.mode_set(mode="EDIT")
        bpy.ops.mesh.select_all(action="SELECT")
        bpy.ops.mesh.normals_make_consistent(inside=False)
        bpy.ops.object.mode_set(mode="OBJECT")


def validate_meshes() -> None:
    """ARMATURE_AUTO heat-map weighting routinely leaves vertex-group weights
    a hair over 1.0 (e.g. 1.000008) on small/thin meshes like the mouth/teeth
    planes -- harmless, but it makes the glTF exporter log a "Mesh X is not
    valid" warning while it silently auto-clamps during its own internal
    validate() call. Running that same clamp ourselves beforehand keeps the
    build log clean rather than depending on the exporter's fixup."""
    for obj in bpy.context.scene.objects:
        if obj.type == "MESH":
            obj.data.validate()


def compute_bounds() -> dict:
    min_co = mathutils.Vector((math.inf, math.inf, math.inf))
    max_co = mathutils.Vector((-math.inf, -math.inf, -math.inf))
    found = False
    for obj in bpy.context.scene.objects:
        if obj.type != "MESH":
            continue
        for corner in obj.bound_box:
            world_co = obj.matrix_world @ mathutils.Vector(corner)
            min_co.x, min_co.y, min_co.z = min(min_co.x, world_co.x), min(min_co.y, world_co.y), min(min_co.z, world_co.z)
            max_co.x, max_co.y, max_co.z = max(max_co.x, world_co.x), max(max_co.y, world_co.y), max(max_co.z, world_co.z)
            found = True
    if not found:
        return {"height_m": 0.0, "width_m": 0.0, "depth_m": 0.0}
    size = max_co - min_co
    return {"height_m": round(size.z, 3), "width_m": round(size.x, 3), "depth_m": round(size.y, 3)}


def export_glb(out_path: Path) -> None:
    for action in bpy.data.actions:
        action.use_fake_user = True
    out_path.parent.mkdir(parents=True, exist_ok=True)
    bpy.ops.export_scene.gltf(
        filepath=str(out_path),
        export_format="GLB",
        export_yup=True,
        export_animations=True,
        export_animation_mode="ACTIONS",
        export_morph=True,
        export_morph_normal=True,
        export_skins=True,
        export_all_influences=False,
        export_draco_mesh_compression_enable=False,
    )


# --- imported (Tier 1/2) models --------------------------------------------------

IMPORT_TARGET_HEIGHT = 2.5  # metres; the procedural bodies stand roughly this tall


def _import_objects(path: Path) -> list:
    before = {obj.name for obj in bpy.data.objects}
    if path.suffix == ".glb":
        bpy.ops.import_scene.gltf(filepath=str(path))
    elif path.suffix == ".blend":
        with bpy.data.libraries.load(str(path), link=False) as (data_from, data_to):
            data_to.objects = data_from.objects
        for obj in data_to.objects:
            if obj is not None:
                bpy.context.scene.collection.objects.link(obj)
    else:
        raise ValueError(f"unrecognized model file extension {path.suffix!r}")
    return [obj for obj in bpy.data.objects if obj.name not in before]


def _world_bounds(objs) -> tuple:
    lo = mathutils.Vector((math.inf, math.inf, math.inf))
    hi = mathutils.Vector((-math.inf, -math.inf, -math.inf))
    for obj in objs:
        for corner in obj.bound_box:
            co = obj.matrix_world @ mathutils.Vector(corner)
            lo = mathutils.Vector((min(lo.x, co.x), min(lo.y, co.y), min(lo.z, co.z)))
            hi = mathutils.Vector((max(hi.x, co.x), max(hi.y, co.y), max(hi.z, co.z)))
    return lo, hi


def build_imported_model(path: Path, warnings: list[str]) -> dict:
    """Uses an authored model as the body instead of building one: imports it,
    scales it to the house height, stands it on the floor facing -Y, and
    reads what it can do (head/arms/legs from its bone names, lip-sync from
    its viseme shape keys). Wardrobe is the model's own, so outfits/footwear
    are not layered on top; accessories still attach. A model with no
    armature gets the canonical rig (the caller parents it); one with its own
    armature keeps it. Raises if the file holds no mesh at all."""
    objs = _import_objects(path)
    for obj in objs:
        if obj.type in ("CAMERA", "LIGHT"):
            bpy.data.objects.remove(obj, do_unlink=True)
    objs = [obj for obj in objs if obj.name in bpy.data.objects]
    meshes = [obj for obj in objs if obj.type == "MESH"]
    if not meshes:
        raise ValueError(f"{path.name} contains no mesh")
    armatures = [obj for obj in objs if obj.type == "ARMATURE"]

    bpy.context.view_layer.update()
    lo, hi = _world_bounds(meshes)
    k = IMPORT_TARGET_HEIGHT / max(hi.z - lo.z, 1e-6)
    roots = [obj for obj in objs if obj.parent is None or obj.parent not in objs]
    for root in roots:  # scale about the world origin, so separate parts keep their spacing
        root.scale = (root.scale.x * k, root.scale.y * k, root.scale.z * k)
        root.location = root.location * k
    bpy.context.view_layer.update()
    lo, hi = _world_bounds(meshes)
    shift = mathutils.Vector(((lo.x + hi.x) / -2, (lo.y + hi.y) / -2, -lo.z))
    for root in roots:
        root.location = root.location + shift
    bpy.context.view_layer.update()
    lo, hi = _world_bounds(meshes)

    bone_names = [b.name.lower() for b in armatures[0].data.bones] if armatures else []
    viseme_keys: set[str] = set()
    for mesh in meshes:
        if mesh.data.shape_keys is not None:
            viseme_keys |= {kb.name for kb in mesh.data.shape_keys.key_blocks if kb.name.startswith(SHAPE_KEY_PREFIX)}
    expected = {shape_key_name(code) for code in VISEME_CODES}
    if viseme_keys and not expected <= viseme_keys:
        warnings.append(f"model has only part of the viseme set ({len(viseme_keys & expected)}/9); lip-sync disabled")
        viseme_keys = set()

    head_loc = (0.0, (lo.y + hi.y) / 2, hi.z - 0.55)
    if armatures:
        head_bones = [b for b in armatures[0].data.bones if "head" in b.name.lower()]
        if head_bones:
            head_loc = tuple(armatures[0].matrix_world @ head_bones[0].head_local)

    largest = max(meshes, key=lambda m: m.dimensions.x * m.dimensions.y * m.dimensions.z)
    _BODY.update(
        obj=largest,
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
        "viseme_keys": sorted(viseme_keys, key=lambda n: VISEME_CODES.index(n[len(SHAPE_KEY_PREFIX) :])),
        "body": largest,
    }


# --- --inspect mode -------------------------------------------------------------

_SOCKET_NAMES = (
    "socket_head_top",
    "socket_eyes",
    "socket_neck",
    "socket_chest",
    "socket_waist",
    "socket_hand_R",
    "socket_hand_L",
    "socket_orbit",
)


def run_inspect(in_path: Path, out_path: Path) -> None:
    """Analyzes a Tier-1 custom asset without compiling a scene. Convention
    for a custom asset to declare socket attach points: an Empty object
    named exactly one of the known socket_* names."""
    reset_scene()
    warnings: list[str] = []

    if in_path.suffix == ".glb":
        bpy.ops.import_scene.gltf(filepath=str(in_path))
    elif in_path.suffix == ".blend":
        with bpy.data.libraries.load(str(in_path), link=False) as (data_from, data_to):
            data_to.objects = data_from.objects
        for obj in data_to.objects:
            if obj is not None:
                bpy.context.scene.collection.objects.link(obj)
    else:
        warnings.append(f"unrecognized file extension {in_path.suffix!r}, attempted no import")

    armatures = [o for o in bpy.context.scene.objects if o.type == "ARMATURE"]
    bone_names: list[str] = []
    if armatures:
        bone_names = [b.name for b in armatures[0].data.bones]
    else:
        warnings.append("no armature found")

    viseme_keys: set[str] = set()
    supported_proportions: list[str] = []
    for obj in bpy.context.scene.objects:
        if obj.type != "MESH" or obj.data.shape_keys is None:
            continue
        for kb in obj.data.shape_keys.key_blocks:
            if kb.name.startswith(SHAPE_KEY_PREFIX):
                viseme_keys.add(kb.name)
            if kb.name in ("muscularity", "femininity") and kb.name not in supported_proportions:
                supported_proportions.append(kb.name)
    if bone_names and any("spine" in b.lower() for b in bone_names):
        supported_proportions += ["height", "build"]

    lowered_bones = [b.lower() for b in bone_names]
    has_head = any("head" in b for b in lowered_bones)
    has_arms = any("arm" in b for b in lowered_bones)
    has_legs = any("leg" in b or "thigh" in b for b in lowered_bones)
    has_face = len(viseme_keys) > 0

    supported_sockets = [
        obj.name for obj in bpy.context.scene.objects if obj.type == "EMPTY" and obj.name in _SOCKET_NAMES
    ]

    triangle_count = count_triangles()

    result = {
        "template_id": in_path.stem,
        "tier": "tier_1_custom",
        "has_face": has_face,
        "capabilities": {"has_head": has_head, "has_arms": has_arms, "has_legs": has_legs, "has_face": has_face},
        "bone_names": bone_names,
        "supported_proportions": supported_proportions,
        "viseme_shape_keys": sorted(
            viseme_keys,
            key=lambda n: VISEME_CODES.index(n[len(SHAPE_KEY_PREFIX) :])
            if n[len(SHAPE_KEY_PREFIX) :] in VISEME_CODES
            else 99,
        ),
        "supported_sockets": supported_sockets,
        "triangle_count": triangle_count,
        "warnings": warnings,
    }
    out_path.write_text(json.dumps(result, indent=2))
    print(f"DESKMATE_BUILD_OK {out_path}")


# --- main (--spec mode) ----------------------------------------------------------


def main() -> None:
    out_build_json = OUT_PATH.with_suffix(".build.json")

    if INSPECT_PATH is not None:
        run_inspect(Path(INSPECT_PATH), OUT_PATH)
        return

    if SPEC_PATH is None:
        fail(out_build_json, "neither --spec nor --inspect was given")
        return

    spec = json.loads(Path(SPEC_PATH).read_text())

    seed_val = spec.get("seed", 42)
    random.seed(seed_val)
    mathutils.noise.seed_set(seed_val)

    reset_scene()
    bpy.context.scene.render.fps = 30

    base_template = spec["base_template"]
    has_face = spec["has_face"]
    proportions = spec.get("proportions", {})
    palette = spec["palette"]
    surface_material = spec.get("surface_material", "matte")
    wardrobe = spec.get("wardrobe", {})
    accessories = spec.get("accessories", {})

    resolution = resolve_template(base_template, DEPLOY_DIR)
    warnings: list[str] = []

    imported = None
    if resolution["imported"]:
        try:
            imported = build_imported_model(resolution["path"], warnings)
        except Exception as exc:  # noqa: BLE001 -- an unusable asset must not fail the whole build
            warnings.append(f"could not use model {resolution['path'].name} ({exc}); building procedurally instead")
            reset_scene()
            bpy.context.scene.render.fps = 30

    if imported is not None:
        body = imported["body"]
        head_loc, has_arms, has_legs = imported["head_loc"], imported["has_arms"], imported["has_legs"]
        viseme_keys = imported["viseme_keys"]
        if has_face and not viseme_keys:
            warnings.append("model has no viseme shape keys; building it without a face (talk_pulse speech indicator)")
        has_face = bool(viseme_keys)
        eyes: list = []
        if wardrobe.get("outfit", "none") != "none" or wardrobe.get("footwear", "none") != "none":
            warnings.append("wardrobe is not layered onto an imported model; it keeps its own clothes")

        accessory_objs = []
        for socket_name in _SOCKET_NAMES:
            item = accessories.get(socket_name)
            if item:
                accessory_objs += build_socket_accessory(socket_name, item, head_loc, palette, DEPLOY_DIR)

        arm_obj = imported["armature"]
        meshes = accessory_objs
        if arm_obj is None:  # a static model: give it the canonical rig and bind it
            arm_obj = build_armature(has_arms, head_loc)
            meshes = [*imported["meshes"], *accessory_objs]
        else:
            bone_names = {b.name for b in arm_obj.data.bones}
            missing = {"root", "spine", "head"} - bone_names
            if missing:
                warnings.append(f"model rig has no {sorted(missing)} bone(s); those animations are empty")
        parent_to_armature(arm_obj, meshes)
        if imported["armature"] is None:
            bake_proportions(arm_obj, proportions)
        bake_morphological_proportions(body, proportions)
    else:
        layout = None
        if base_template in ("standard_humanoid_female", "standard_humanoid_male"):
            body, head_loc, has_arms, has_legs, layout = build_body_humanoid(proportions, palette, surface_material)
        else:
            procedural_template = base_template if base_template == "primitive_mechanical" else "primitive_blob"
            body, head_loc, has_arms, has_legs = build_body_tier3(procedural_template, proportions, palette, surface_material)
        head = build_head(head_loc)
        eyes = build_eyes(head_loc, hex_to_rgba(palette["secondary"]))
        mouth, teeth, viseme_keys = build_mouth(head_loc, has_face)

        bpy.context.view_layer.update()
        _BODY.update(
            obj=body,
            build=proportions.get("build", 1.0),
            front=body.location.y - body.dimensions.y / 2,
            back=body.location.y + body.dimensions.y / 2,
            bottom=body.location.z - body.dimensions.z / 2,
            half_width=body.dimensions.x / 2,
        )

        outfit_objs = build_outfit(body, wardrobe.get("outfit", "none"), head_loc, palette, surface_material)
        footwear_objs = build_footwear(wardrobe.get("footwear", "none"), palette)

        accessory_objs = []
        for socket_name in _SOCKET_NAMES:
            item = accessories.get(socket_name)
            if item:
                accessory_objs += build_socket_accessory(socket_name, item, head_loc, palette, DEPLOY_DIR)

        arm_obj = build_armature(has_arms, head_loc, has_legs=has_legs, layout=layout)
        meshes = [body, head, *eyes, mouth, teeth, *outfit_objs, *footwear_objs, *accessory_objs]
        parent_to_armature(arm_obj, meshes)

        bake_proportions(arm_obj, proportions)
        bake_morphological_proportions(body, proportions)

    speech_indicator_mode = "viseme_shapekeys" if has_face else "talk_pulse"
    talk_pulse_clip = None
    if not has_face:
        talk_pulse_clip = build_talk_pulse(arm_obj)

    baked_clips = build_all_clips(arm_obj, eyes, has_arms=has_arms, has_legs=has_legs)
    if talk_pulse_clip:
        baked_clips.append(talk_pulse_clip)

    bpy.context.scene.frame_set(1)
    recalc_normals()
    validate_meshes()

    total_triangles = count_triangles()
    if total_triangles > TRIANGLE_BUDGET:
        fail(out_build_json, f"budget_exceeded: triangles={total_triangles} (max {TRIANGLE_BUDGET})")
        return

    bounds = compute_bounds()
    export_glb(OUT_PATH)

    size_bytes = OUT_PATH.stat().st_size
    if size_bytes > SIZE_BUDGET_BYTES:
        fail(out_build_json, f"budget_exceeded: size_bytes={size_bytes} (max {SIZE_BUDGET_BYTES})")
        return

    sha256 = hashlib.sha256(OUT_PATH.read_bytes()).hexdigest()

    build_result = {
        "glb_filename": OUT_PATH.name,
        "sha256": sha256,
        "created_at": time.time(),
        "status": "ok",
        "seed": seed_val,
        "evaluated_triangles": total_triangles,
        "size_bytes": size_bytes,
        "has_face": has_face,
        "speech_indicator_mode": speech_indicator_mode,
        "capabilities": {"has_head": True, "has_arms": has_arms, "has_legs": has_legs, "has_face": has_face},
        "viseme_shape_keys": viseme_keys,
        "animation_clips": clip_metadata(baked_clips),
        "bounds": bounds,
        "warnings": warnings,
    }
    out_build_json.write_text(json.dumps(build_result, indent=2))
    print(f"DESKMATE_BUILD_OK {OUT_PATH}")


if __name__ == "__main__":
    main()
