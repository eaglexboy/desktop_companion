"""Parametric character builder, run headless:

    blender --background --factory-startup --python generator.py -- \
        --spec <LookSpec.json> --out <output.glb> --deploy-dir <deploy_dir>
    blender --background --factory-startup --python generator.py -- \
        --inspect <custom.glb> --out <capabilities.json>
    blender --background --factory-startup --python generator.py -- \
        --convert <model.gltf> --out <model.glb> [--max-texture 1024] [--max-triangles N]
    blender --background --factory-startup --python generator.py -- \
        --face-landmarks <model.glb> --out <model.face.json>

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
import os
import random
import re
import sys
import time
from pathlib import Path

import bmesh  # pyright: ignore[reportMissingImports]
import bpy  # pyright: ignore[reportMissingImports]
import mathutils  # pyright: ignore[reportMissingImports]

sys.path.insert(
    0, str(Path(__file__).resolve().parent.parent)
)  # repo root, for blender.viseme_shapekeys
from blender.animation_library import (
    CHAIR_SIT_CLIPS,
    LIBRARY_TIME_SCALE,
    SIT_PROP_NODE,
    library_contents,
    LIBRARY_EXTRAS,
    library_folder,
    library_gaps,
    merge_picks,
    pick_clips,
)
from blender.sockets import canonical_socket_bones, find_socket_bones, wrist_is_tail
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


def _arg_or(flag: str, default: str) -> str:
    """`_arg` for a flag that has a default: never None."""
    return _arg(flag, default) or default


def _required_arg(flag: str) -> str:
    value = _arg(flag)
    if value is None:
        raise SystemExit(f"generator.py: {flag} is required")
    return value


SPEC_PATH = _arg("--spec")
OUT_PATH = Path(_required_arg("--out"))
DEPLOY_DIR = Path(_arg_or("--deploy-dir", "."))
INSPECT_PATH = _arg("--inspect")
CONVERT_PATH = _arg("--convert")
FACE_PATH = _arg("--face-landmarks")
MAX_TEXTURE = int(_arg_or("--max-texture", "1024"))
MAX_TRIANGLES = int(_arg_or("--max-triangles", "0"))

TRIANGLE_BUDGET = 30_000
SIZE_BUDGET_BYTES = 15_728_640  # 15 MiB: the body, its textures and every clip it wears

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
    name: str,
    color: tuple[float, float, float, float],
    *,
    material: str = "matte",
    emissive: bool = False,
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
    out_build_json.write_text(
        json.dumps({"status": "fail", "reason": reason, "warnings": []}, indent=2)
    )


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


def build_body_tier3(
    base_template: str, proportions: dict, palette: dict, surface_material: str
):
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
    body.data.materials.append(
        make_material("BodyMat", primary, material=surface_material)
    )
    return body, head_loc, has_arms, has_legs


# Per-template body character, applied on top of the user's proportions so
# the two humanoid bases read as clearly different figures even at the
# overlay's small size: female = narrower shoulders, wider hips, slimmer
# limbs and neck; male = broad shoulders, narrower hips, heavier limbs/neck.
_HUMANOID_CHARACTER = {
    "standard_humanoid_female": {
        "shoulder": 0.84,
        "hip": 1.30,
        "limb": 0.86,
        "neck": 0.85,
    },
    "standard_humanoid_male": {
        "shoulder": 1.12,
        "hip": 0.90,
        "limb": 1.10,
        "neck": 1.15,
    },
}


def build_body_humanoid(
    proportions: dict, palette: dict, surface_material: str, base_template: str = ""
):
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

    char = _HUMANOID_CHARACTER.get(
        base_template, {"shoulder": 1.0, "hip": 1.0, "limb": 1.0, "neck": 1.0}
    )

    shoulder = (
        0.46 * b * (1.0 + 0.30 * (mus - 0.5) - 0.20 * (fem - 0.5)) * char["shoulder"]
    )
    hip = 0.40 * b * (1.0 + 0.25 * (fem - 0.5)) * char["hip"]
    leg_r = 0.15 * b * char["limb"]
    leg_x = 0.30 * b * math.sqrt(char["hip"])  # legs sit under the pelvis
    arm_x = shoulder + 0.14
    shoulder_z, hand_z = 1.5 * h, 0.85 * h
    arm_r = 0.11 * b * (1.0 + 0.4 * (mus - 0.5)) * char["limb"]
    mat = make_material("BodyMat", primary, material=surface_material)

    parts = [
        (
            _prim("sphere", (0, 0, 0.95 * h), scale=(hip, 0.30 * b, 0.28 * h)),
            mat,
        ),  # pelvis
        (
            _prim("sphere", (0, 0, 1.22 * h), scale=(shoulder, 0.32 * b, 0.46 * h)),
            mat,
        ),  # torso
        (
            _prim(
                "cylinder", (0, 0, 1.66 * h), r=0.13 * b * char["neck"], depth=0.2 * h
            ),
            mat,
        ),  # neck
    ]
    for x in (-leg_x, leg_x):
        parts.append((_prim("cylinder", (x, 0, 0.45 * h), r=leg_r, depth=0.9 * h), mat))
    for x in (-arm_x, arm_x):
        parts.append(
            (
                _prim(
                    "cylinder",
                    (x, 0, (shoulder_z + hand_z) / 2),
                    r=arm_r,
                    depth=shoulder_z - hand_z,
                ),
                mat,
            )
        )
        parts.append((_prim("sphere", (x, 0, hand_z), r=arm_r * 1.3), mat))  # hand

    body = _assemble("Body", parts)
    # Origin at the bounding-box centre, like the Tier 3 primitives, so the
    # _BODY bounds (footwear height, hoodie pocket) read it the same way.
    bpy.context.view_layer.objects.active = body
    body.select_set(True)
    bpy.ops.object.origin_set(type="ORIGIN_GEOMETRY", center="BOUNDS")
    body.select_set(False)
    head_loc = (0.0, 0.0, 2.05 * h)
    layout = {
        "arm_x": arm_x,
        "shoulder_z": shoulder_z,
        "hand_z": hand_z,
        "leg_x": leg_x,
        "hip_z": 0.9 * h,
        "arm_r": arm_r,
        "leg_r": leg_r,
        "height": h,
    }
    return body, head_loc, True, True, layout


def build_head(head_loc) -> bpy.types.Object:
    bpy.ops.mesh.primitive_uv_sphere_add(radius=0.55, location=head_loc)
    head = bpy.context.active_object
    head.name = "Head"
    bpy.ops.object.shade_smooth()
    head.data.materials.append(bpy.data.materials.get("BodyMat"))
    return head


BLINK_SHAPE_KEY = "blink"


def _eye_material(
    name: str, head_color: tuple[float, float, float, float]
) -> bpy.types.Material:
    """Dark eyes on a light head; light, self-lit eyes on a dark one. A dark
    eye on a dark head is only visible as a specular glint, which swings from
    one eye to the other as the avatar sways and makes a blink look uneven."""
    luminance = 0.2126 * head_color[0] + 0.7152 * head_color[1] + 0.0722 * head_color[2]
    if luminance >= 0.35:
        return make_material(name, (0.05, 0.05, 0.05, 1.0))
    glow = (0.92, 0.94, 1.0, 1.0)
    mat = make_material(name, glow)
    bsdf = mat.node_tree.nodes.get("Principled BSDF")
    if bsdf and "Emission Color" in bsdf.inputs:
        bsdf.inputs["Emission Color"].default_value = glow
        bsdf.inputs["Emission Strength"].default_value = (
            1.0  # <= 1 stays a plain glTF emissiveFactor
        )
    return mat


def build_eyes(head_loc, head_color: tuple[float, float, float, float]):
    """Two eyeball spheres, each with a Basis plus a `blink` shape key that
    squashes the eye flat along its vertical (local Z) axis about its own
    centre. The overlay drives that key's weight to blink; it is not a
    viseme and is reported separately as `blink_shape_keys`."""
    eyes = []
    for side, x in (("L", -0.22), ("R", 0.22)):
        bpy.ops.mesh.primitive_uv_sphere_add(
            radius=0.08,
            location=(head_loc[0] + x, head_loc[1] - 0.48, head_loc[2] + 0.05),
        )
        eye = bpy.context.active_object
        eye.name = f"Eye_{side}"
        eye.data.materials.append(_eye_material(f"EyeMat_{side}", head_color))
        eye.shape_key_add(name="Basis")
        blink = eye.shape_key_add(name=BLINK_SHAPE_KEY)
        for i, v in enumerate(eye.data.vertices):
            blink.data[i].co.x = v.co.x
            blink.data[i].co.y = v.co.y
            blink.data[i].co.z = v.co.z * 0.08
        eyes.append(eye)
    return eyes


def _build_shape_keyed_plane(
    name: str,
    size: float,
    location,
    rotation,
    material,
    params_by_code: dict,
    rest_code: str = "X",
    oval: bool = False,
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
    bpy.ops.mesh.subdivide(number_cuts=8 if oval else 3)
    bpy.ops.object.mode_set(mode="OBJECT")
    obj.data.materials.append(material)

    base_coords = [v.co.copy() for v in obj.data.vertices]
    if (
        oval
    ):  # the square's grid mapped onto its inscribed ellipse, so a mouth is round-edged
        half = size / 2
        for co in base_coords:
            u, v = co.x / half, co.y / half
            co.x, co.y = half * u * math.sqrt(1 - v * v / 2), half * v * math.sqrt(
                1 - u * u / 2
            )
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
        open_, width, round_, _ = VISEME_MOUTH_MATRIX[code]

        def fn(co):
            x = co.x * width * (1.0 - round_ * 0.25)
            y = co.y * (
                0.15 + open_ * 0.85
            )  # local Y is height: this is the mouth opening
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
        *_, teeth = VISEME_MOUTH_MATRIX[code]
        visible_scale = 1.0 if teeth else 0.02

        def fn(co):
            return (co.x * visible_scale, co.y * visible_scale, co.z)

        return fn

    teeth, _ = _build_shape_keyed_plane(
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


def _prim(
    kind: str, location, *, scale=(1.0, 1.0, 1.0), rotation=(0.0, 0.0, 0.0), **kw
):
    """One primitive, already scaled/rotated. Dimensions are in metres and
    describe the unscaled primitive (spheres by radius, cubes by edge)."""
    ops = bpy.ops.mesh
    if kind == "sphere":
        ops.primitive_uv_sphere_add(
            radius=kw.get("r", 1.0), segments=24, ring_count=12, location=location
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
    ray_dir = (inv.to_3x3() @ mathutils.Vector((0.0, direction, 0.0))).normalized()
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
    cloth = make_material(f"OutfitMat_{outfit}", secondary, material=surface_material)
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
            "OutfitTrim_hoodie", _darker(secondary, 0.7), material=surface_material
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
                    _prim("sphere", (x, cy, ground + 0.12), scale=(0.17, 0.34, 0.12)),
                    upper,
                ),
                (
                    _prim("cylinder", (x, cy + 0.12, ground + 0.3), r=0.14, depth=0.42),
                    upper,
                ),
                (
                    _prim("cylinder", (x, cy + 0.12, ground + 0.5), r=0.16, depth=0.06),
                    sole,
                ),
                (
                    _prim("cube", (x, cy, ground + 0.025), scale=(0.36, 0.7, 0.05)),
                    strap,
                ),
            ]
        elif footwear == "sandals":
            parts = [
                (_prim("cube", (x, cy, ground + 0.025), scale=(0.3, 0.66, 0.05)), sole),
                (
                    _prim(
                        "cube", (x, cy - 0.1, ground + 0.07), scale=(0.3, 0.07, 0.04)
                    ),
                    strap,
                ),
                (
                    _prim(
                        "cube", (x, cy + 0.12, ground + 0.07), scale=(0.3, 0.07, 0.04)
                    ),
                    strap,
                ),
            ]
        else:  # sneakers
            parts = [
                (
                    _prim("sphere", (x, cy, ground + 0.12), scale=(0.17, 0.32, 0.11)),
                    upper,
                ),
                (
                    _prim("cube", (x, cy, ground + 0.025), scale=(0.36, 0.68, 0.05)),
                    sole,
                ),
                (
                    _prim(
                        "cube", (x, cy - 0.05, ground + 0.225), scale=(0.1, 0.16, 0.02)
                    ),
                    strap,
                ),
            ]
        objs.append(_assemble(f"Footwear_{footwear}_{side}", parts))
    return objs


_SOCKET_LOCATIONS = {
    "socket_head_top": lambda head_loc: (head_loc[0], head_loc[1], head_loc[2] + 0.65),
    "socket_eyes": lambda head_loc: (
        head_loc[0],
        head_loc[1] - 0.5,
        head_loc[2] + 0.05,
    ),
    "socket_neck": lambda head_loc: (head_loc[0], head_loc[1], head_loc[2] - 0.35),
    "socket_chest": lambda _: (0, 0, 1.1),
    "socket_waist": lambda _: (0, 0, 0.75),
    "socket_hand_R": lambda _: (_body_half_width_at(0.9) + 0.2, -0.2, 0.9),
    "socket_hand_L": lambda _: (-(_body_half_width_at(0.9) + 0.2), -0.2, 0.9),
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
    glow = make_material(
        f"EyewearLens_{item}",
        hex_to_rgba(palette["emissive"]),
        material="glossy",
        emissive=True,
    )
    dark = make_material(f"EyewearDark_{item}", _DARK, material="glossy")
    parts = []
    if item == "glasses":
        for x in (-0.22, 0.22):
            parts.append(
                (
                    _prim(
                        "torus",
                        (x0 + x, y0, z0),
                        rotation=face,
                        major=0.13,
                        minor=0.014,
                    ),
                    accent,
                )
            )
        parts.append(
            (
                _prim(
                    "cylinder",
                    (x0, y0, z0 + 0.02),
                    r=0.012,
                    depth=0.1,
                    rotation=(0, math.pi / 2, 0),
                ),
                accent,
            )
        )
    elif item == "sunglasses":
        for x in (-0.22, 0.22):
            parts.append(
                (
                    _prim(
                        "cylinder", (x0 + x, y0, z0), r=0.15, depth=0.02, rotation=face
                    ),
                    dark,
                )
            )
            parts.append(
                (
                    _prim(
                        "torus",
                        (x0 + x, y0, z0),
                        rotation=face,
                        major=0.15,
                        minor=0.015,
                    ),
                    accent,
                )
            )
        parts.append(
            (
                _prim(
                    "cylinder",
                    (x0, y0, z0 + 0.03),
                    r=0.012,
                    depth=0.1,
                    rotation=(0, math.pi / 2, 0),
                ),
                accent,
            )
        )
    elif item == "goggles":
        for x in (-0.22, 0.22):
            parts.append(
                (
                    _prim(
                        "cylinder",
                        (x0 + x, y0 - 0.005, z0),
                        r=0.15,
                        depth=0.03,
                        rotation=face,
                    ),
                    glow,
                )
            )
            parts.append(
                (
                    _prim(
                        "torus",
                        (x0 + x, y0, z0),
                        rotation=face,
                        major=0.16,
                        minor=0.035,
                    ),
                    accent,
                )
            )
        parts.append((_prim("cube", (x0, y0, z0), scale=(0.1, 0.05, 0.06)), accent))
        # Strap round the head at eye height (a torus, axis up, is a ring seen from above).
        parts.append(
            (
                _prim("torus", (x0, y0 + 0.5, z0), major=_HEAD_R + 0.01, minor=0.03),
                accent,
            )
        )
    else:  # visor: one wide glowing band across both eyes
        parts.append(
            (_prim("sphere", (x0, y0 - 0.02, z0), r=1.0, scale=(0.5, 0.08, 0.12)), glow)
        )
        parts.append(
            (_prim("cube", (x0, y0 - 0.02, z0 + 0.12), scale=(0.9, 0.07, 0.03)), accent)
        )
        for x in (-0.5, 0.5):
            parts.append(
                (
                    _prim(
                        "sphere", (x0 + x, y0 + 0.1, z0), r=0.07, scale=(0.8, 1.6, 1.4)
                    ),
                    accent,
                )
            )
    return parts


def _headwear(item: str, loc, palette: dict):
    x0, y0, z0 = loc
    top = z0 - 0.1  # top of the head
    # Kept low on purpose: the overlay's fixed camera frames the head close to
    # the top edge, so anything much taller than ~0.3m above it is cropped.
    accent = make_material(f"HeadMat_{item}", hex_to_rgba(palette["accent"]))
    glow = make_material(
        f"HeadGlow_{item}", hex_to_rgba(palette["emissive"]), emissive=True
    )
    parts = []
    if item == "crown":
        parts.append(
            (_prim("cylinder", (x0, y0, top + 0.05), r=0.3, depth=0.1), accent)
        )
        for i in range(5):
            a = i * 2 * math.pi / 5
            parts.append(
                (
                    _prim(
                        "cone",
                        (x0 + 0.26 * math.cos(a), y0 + 0.26 * math.sin(a), top + 0.14),
                        r=0.05,
                        depth=0.12,
                    ),
                    accent,
                )
            )
    elif item == "horns":
        for sign in (-1, 1):
            parts.append(
                (
                    _prim(
                        "cone",
                        (x0 + sign * 0.32, y0, top + 0.08),
                        r=0.08,
                        depth=0.26,
                        rotation=(0, sign * 0.55, 0),
                    ),
                    accent,
                )
            )
    elif item == "antenna":
        parts.append(
            (_prim("cylinder", (x0, y0, top + 0.09), r=0.015, depth=0.18), accent)
        )
        parts.append((_prim("sphere", (x0, y0, top + 0.2), r=0.055), glow))
    else:  # pointy_hat
        parts.append(
            (_prim("cylinder", (x0, y0, top + 0.02), r=0.42, depth=0.03), accent)
        )
        parts.append((_prim("cone", (x0, y0, top + 0.12), r=0.27, depth=0.22), accent))
    return parts


def _neckwear(item: str, loc, palette: dict):
    x0, y0, z0 = loc
    ring_r = math.sqrt(_HEAD_R**2 - 0.35**2) + 0.02  # head radius at this height
    accent = make_material(f"NeckMat_{item}", hex_to_rgba(palette["accent"]))
    parts = []
    if item == "tie":
        chin = (
            z0 - 0.2
        )  # head centre is 0.35 above this socket; its lowest point is another 0.2 down
        knot_y = _body_surface_y(0.0, chin - 0.1) - 0.01
        parts.append((_prim("sphere", (x0, knot_y, chin - 0.06), r=0.06), accent))
        parts.append(
            (
                _prim(
                    "cone",
                    (x0, knot_y, chin - 0.25),
                    r=0.1,
                    r2=0.03,
                    depth=0.28,
                    rotation=(math.pi, 0, 0),
                    scale=(1.0, 0.35, 1.0),
                ),
                accent,
            )
        )
    elif item == "collar":
        parts.append(
            (_prim("torus", (x0, y0, z0), major=ring_r + 0.03, minor=0.05), accent)
        )
        for x in (-0.08, 0.08):
            parts.append(
                (
                    _prim(
                        "cone",
                        (x0 + x, y0 - ring_r - 0.02, z0 - 0.06),
                        r=0.08,
                        depth=0.18,
                        rotation=(math.pi, 0, 0),
                    ),
                    accent,
                )
            )
    else:  # scarf
        parts.append((_prim("torus", (x0, y0, z0), major=ring_r, minor=0.085), accent))
        parts.append(
            (
                _prim(
                    "cube",
                    (x0 + 0.2, y0 - ring_r + 0.02, z0 - 0.2),
                    scale=(0.14, 0.05, 0.4),
                ),
                accent,
            )
        )
    return parts


def _chestwear(item: str, loc, palette: dict):
    accent = make_material(f"ChestMat_{item}", hex_to_rgba(palette["accent"]))
    dark = make_material(
        f"ChestDark_{item}", _darker(hex_to_rgba(palette["accent"]), 0.5)
    )
    if item == "backpack":
        z = (
            loc[2] - 0.1
        )  # sits just below the chest socket (1.1 on the procedural bodies)
        back = _body_surface_y(0.0, z, front=False)
        return [
            (_prim("cube", (0.0, back + 0.14, z), scale=(0.55, 0.3, 0.65)), accent),
            (
                _prim("cube", (0.0, back + 0.32, z - 0.15), scale=(0.35, 0.06, 0.25)),
                dark,
            ),
        ]
    # The procedural bodies are drawn wide enough for 0.28; an installed body's torso
    # is measured (below the arms, which a T-pose holds out at chest height).
    x = (
        0.5 * min(_body_half_width_at(loc[2] - d) for d in (0.0, 0.15, 0.3))
        if _BODY.get("imported")
        else 0.28
    )
    y = _body_surface_y(x, loc[2])
    return [
        (
            _prim(
                "cylinder",
                (x, y, loc[2]),
                r=0.09,
                depth=0.04,
                rotation=(math.pi / 2, 0, 0),
            ),
            accent,
        )
    ]  # badge


def _waistwear(item: str, loc, palette: dict):
    z = loc[2]
    accent = make_material(f"WaistMat_{item}", hex_to_rgba(palette["accent"]))
    dark = make_material(
        f"WaistDark_{item}", _darker(hex_to_rgba(palette["accent"]), 0.5)
    )
    half_w = _body_half_width_at(z)
    front = _body_surface_y(0.0, z)
    back = _body_surface_y(0.0, z, front=False)
    if item == "pouch":
        x = half_w * 0.55
        return [
            (
                _prim(
                    "sphere",
                    (x, _body_surface_y(x, z) - 0.02, z - 0.05),
                    r=1.0,
                    scale=(0.18, 0.12, 0.2),
                ),
                accent,
            )
        ]
    # belt: a ring scaled to the body's cross-section at this height (+ margin for an outfit)
    rx, ry = (half_w * 1.1), ((front - back) / -2.0) * 1.1
    cy = (front + back) / 2.0
    return [
        (
            _prim(
                "torus",
                (0.0, cy, z),
                major=1.0,
                minor=0.04 / max(rx, 0.1),
                scale=(rx, ry, max(rx, 0.1)),
            ),
            dark,
        ),  # z scale keeps the strap's section round
        (_prim("cube", (0.0, front * 1.1 - 0.01, z), scale=(0.16, 0.04, 0.12)), accent),
    ]


def _handheld(item: str, loc, palette: dict):
    x0, y0, z0 = loc
    accent = make_material(f"HandMat_{item}", hex_to_rgba(palette["accent"]))
    glow = make_material(
        f"HandGlow_{item}", hex_to_rgba(palette["emissive"]), emissive=True
    )
    dark = make_material(
        f"HandDark_{item}", _darker(hex_to_rgba(palette["secondary"]), 0.5)
    )
    if item == "wand":
        return [
            (
                _prim(
                    "cylinder",
                    (x0, y0, z0 + 0.1),
                    r=0.02,
                    depth=0.7,
                    rotation=(0, 0.25, 0),
                ),
                dark,
            ),
            (_prim("sphere", (x0 + 0.09, y0, z0 + 0.45), r=0.06), glow),
        ]
    if item == "tool":
        return [
            (_prim("cylinder", (x0, y0, z0), r=0.03, depth=0.6), dark),
            (_prim("cube", (x0, y0, z0 + 0.34), scale=(0.2, 0.06, 0.1)), accent),
        ]
    if item == "shield":
        return [
            (
                _prim(
                    "cylinder",
                    (x0, y0 - 0.05, z0),
                    r=0.3,
                    depth=0.05,
                    rotation=(math.pi / 2, 0, 0),
                ),
                accent,
            ),
            (_prim("sphere", (x0, y0 - 0.1, z0), r=0.07), dark),
        ]
    return [(_prim("sphere", (x0, y0, z0), r=0.16), glow)]  # orb


def _orbiting(item: str, loc, palette: dict):
    x0, y0, z0 = loc
    accent = make_material(f"OrbitMat_{item}", hex_to_rgba(palette["accent"]))
    glow = make_material(
        f"OrbitGlow_{item}", hex_to_rgba(palette["emissive"]), emissive=True
    )
    if item == "floating_cube":
        return [
            (
                _prim(
                    "cube",
                    (x0 + 0.85, y0 - 0.1, z0 - 0.5),
                    scale=(0.2, 0.2, 0.2),
                    rotation=(0.6, 0.4, 0.5),
                ),
                glow,
            )
        ]
    return [
        (
            _prim("torus", (x0, y0, z0), rotation=(0.15, 0, 0), major=0.4, minor=0.03),
            accent,
        )
    ]  # crystal_ring (a halo)


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


def _accessory_sidecar(asset_path: Path) -> dict:
    """Settings next to an accessory model (`<item>.json`): scale, offset (metres,
    in the socket's frame) and rotation (radians). All optional."""
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
        tuple(frame["location"]) if frame else _SOCKET_LOCATIONS[socket_name](head_loc)
    )
    asset_path = deploy_dir / "assets" / "wardrobe" / socket_name / f"{item}.glb"
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
    meshes = [obj for obj in imported if obj.type == "MESH" and obj not in bone_shapes]
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


# --- armature / rigging --------------------------------------------------------


RUN_SHAPE_KEYS = ("run_a", "run_b")

# Run-cycle extremes, in degrees: swing about the hip/shoulder, plus how far
# the knee folds (heel kicked up on the trailing leg). Positive rotation about
# +X carries a point hanging below the pivot toward +Y, i.e. backwards, since
# avatars face -Y; so forward swing is negative.
_RUN_LEG_SWING = (-38.0, 32.0)  # (leading leg, trailing leg)
_RUN_KNEE_BEND = (20.0, 70.0)
_RUN_ARM_SWING = (-40.0, 40.0)  # arm on the side of the leading leg swings back


def _smooth01(t: float) -> float:
    t = max(0.0, min(1.0, t))
    return t * t * (3.0 - 2.0 * t)


def _rot_x(co, pivot_yz: tuple[float, float], angle: float):
    """Rotate `co` about the X axis through (any x, pivot_y, pivot_z)."""
    c, s = math.cos(angle), math.sin(angle)
    y, z = co.y - pivot_yz[0], co.z - pivot_yz[1]
    return mathutils.Vector(
        (co.x, pivot_yz[0] + y * c - z * s, pivot_yz[1] + y * s + z * c)
    )


def _run_pose(co, layout: dict, lead_side: int):
    """World-space `co` deformed into one extreme of the run cycle. `lead_side`
    is the sign of the x of the leg that is forward (-1 or +1). Pure geometry,
    keyed off the humanoid `layout`, so it also fits clothes and shoes built
    around the same body."""
    h = layout["height"]
    leg_x, leg_r, hip_z = layout["leg_x"], layout["leg_r"], layout["hip_z"]
    arm_x, arm_r, shoulder_z = layout["arm_x"], layout["arm_r"], layout["shoulder_z"]
    out = co.copy()

    # Legs. Weight fades out sideways toward the body centreline (so there is
    # no tear between the legs) and upward into the hips.
    leg_side = -1 if co.x < 0 else 1
    off = abs(abs(co.x) - leg_x)
    w_side = 1.0 - _smooth01((off - 1.5 * leg_r) / (0.5 * leg_r))
    w_hip = _smooth01((hip_z - co.z) / (0.25 * h))
    w_leg = w_side * w_hip
    if w_leg > 0.0:
        leading = leg_side == lead_side
        swing = math.radians(_RUN_LEG_SWING[0 if leading else 1])
        bend = math.radians(_RUN_KNEE_BEND[0 if leading else 1])
        knee_z = hip_z * 0.5
        w_knee = _smooth01((knee_z - co.z) / (0.18 * h))
        out = _rot_x(out, (0.0, knee_z), bend * w_knee * w_leg)
        out = _rot_x(out, (0.0, hip_z), swing * w_leg)

    # Arms swing against the leg on the same side.
    off = abs(abs(co.x) - arm_x)
    w_side = 1.0 - _smooth01((off - 1.45 * arm_r) / (0.55 * arm_r))
    w_sh = _smooth01((shoulder_z - co.z) / (0.2 * h))
    w_arm = (
        w_side * w_sh if abs(co.x) > arm_x - 2.0 * arm_r and co.z > hip_z * 0.7 else 0.0
    )
    if w_arm > 0.0:
        arm_side = -1 if co.x < 0 else 1
        swing = math.radians(_RUN_ARM_SWING[1 if arm_side == lead_side else 0])
        out = _rot_x(out, (0.0, shoulder_z), swing * w_arm)
    return out


def add_run_shape_keys(objs: list, layout: dict) -> list[str]:
    """Adds `run_a`/`run_b` (the two mirrored extremes of a run cycle: legs
    and arms swinging, knees folding) to each humanoid mesh. The overlay
    cross-fades between them while the avatar travels, which stands in for a
    run clip it cannot play. Returns the key names, or [] if nothing took."""
    added = False
    for obj in objs:
        if obj is None or obj.type != "MESH" or not obj.data.vertices:
            continue
        mw, inv = obj.matrix_world, obj.matrix_world.inverted()
        if obj.data.shape_keys is None:
            obj.shape_key_add(name="Basis")
        for key_name, lead_side in zip(RUN_SHAPE_KEYS, (-1, 1)):
            sk = obj.shape_key_add(name=key_name)
            for i, v in enumerate(obj.data.vertices):
                new_co = inv @ _run_pose(mw @ v.co, layout, lead_side)
                sk.data[i].co.x, sk.data[i].co.y, sk.data[i].co.z = (
                    new_co.x,
                    new_co.y,
                    new_co.z,
                )
        added = True
    return list(RUN_SHAPE_KEYS) if added else []


def build_armature(
    has_arms: bool, head_loc, *, has_legs: bool = False, layout: dict | None = None
) -> bpy.types.Object:
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
    head_bone.head, head_bone.tail = (0, 0, head_loc[2] - 0.4), (
        0,
        0,
        head_loc[2] + 0.3,
    )
    head_bone.parent = spine

    if has_arms:
        for side, x in (("L", -0.7), ("R", 0.7)):
            arm = eb.new(f"arm_{side}")
            if layout is not None:  # limbs that hang straight down from the shoulder
                ax = x / 0.7 * layout["arm_x"]
                arm.head, arm.tail = (ax, 0, layout["shoulder_z"]), (
                    ax,
                    0,
                    layout["hand_z"],
                )
            else:
                arm.head, arm.tail = (0, 0, head_loc[2] - 0.6), (
                    x,
                    0,
                    head_loc[2] - 1.0,
                )
            arm.parent = spine

    if has_legs and layout is not None:
        for side, sign in (("L", -1), ("R", 1)):
            leg = eb.new(f"leg_{side}")
            leg.head, leg.tail = (sign * layout["leg_x"], 0, layout["hip_z"]), (
                sign * layout["leg_x"],
                0,
                0.05,
            )
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
    _bind_unweighted_rigidly(arm_obj, meshes)


def _distance_to_segment(point, head, tail) -> float:
    axis = tail - head
    length_sq = axis.length_squared
    t = (
        0.0
        if length_sq < 1e-12
        else max(0.0, min(1.0, (point - head).dot(axis) / length_sq))
    )
    return (point - (head + axis * t)).length


def _bind_unweighted_rigidly(arm_obj, meshes) -> None:
    """Heat-map weighting fails on some meshes (open, self-intersecting or tiny ones, as in
    low-poly models made of many loose parts): they get no weights and no armature modifier,
    and come out of the export detached from the body. Such a part is bound whole to the
    bone nearest its centre, so it moves with the figure like a rigid piece."""
    bones = [
        (
            bone,
            arm_obj.matrix_world @ bone.head_local,
            arm_obj.matrix_world @ bone.tail_local,
        )
        for bone in arm_obj.data.bones
        if bone.use_deform
    ]
    if not bones:
        return
    for mesh in meshes:
        if mesh is None or mesh.type != "MESH" or not mesh.data.vertices:
            continue
        if any(len(v.groups) for v in mesh.data.vertices):
            continue
        centre = mesh.matrix_world @ (
            sum((mathutils.Vector(c) for c in mesh.bound_box), mathutils.Vector()) / 8
        )
        bone = min(
            bones, key=lambda item: _distance_to_segment(centre, item[1], item[2])
        )[0]
        group = mesh.vertex_groups.get(bone.name) or mesh.vertex_groups.new(
            name=bone.name
        )
        group.add([v.index for v in mesh.data.vertices], 1.0, "REPLACE")
        if not any(m.type == "ARMATURE" for m in mesh.modifiers):
            modifier = mesh.modifiers.new("Armature", "ARMATURE")
            modifier.object = arm_obj
        world = mesh.matrix_world.copy()
        mesh.parent = arm_obj
        mesh.matrix_parent_inverse = arm_obj.matrix_world.inverted()
        mesh.matrix_world = world


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
    for frame, scale_z in (
        (1, TALK_PULSE_MIN_SCALE),
        (1 + half, TALK_PULSE_MAX_SCALE),
        (1 + period_frames, TALK_PULSE_MIN_SCALE),
    ):
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
    has_head: bool = True,
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
        return _bake_catalog(arm_obj, eyes, has_arms=has_arms, has_legs=has_legs)
    finally:
        _CLIP_SKIP = frozenset()


def _bake_catalog(arm_obj, eyes, *, has_arms: bool, has_legs: bool) -> list[str]:
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
            {1: {"root": {"scale": (1, 1, 1)}}, 15: {"root": {"scale": (1, 1, 1)}}},
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
                22: {"arm_L": {"rot": (0, 0, R(-40))}, "arm_R": {"rot": (0, 0, R(40))}},
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


# --- skeletal clips for an imported rig -------------------------------------------
#
# The clips above drive bones named root/spine/head, which an imported rig does not
# have. For one that does have a head (and usually a neck and spine), the head and
# trunk idles are baked on its own bones instead. Each rotation is given about world
# axes (yaw about Z, pitch about X, roll about Y) and converted into the bone's own
# rest frame, so it does not depend on how the rig's bones are oriented. The overlay
# plays them by name (avatar_controller.h `playClip`), one at a time.

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
) -> "mathutils.Quaternion":
    """The pose rotation of `bone` that turns it by yaw (about world Z), pitch (about X) and
    roll (about Y), in radians, as a quaternion in the bone's own rest frame."""
    world = (
        mathutils.Quaternion((0, 0, 1), yaw)
        @ mathutils.Quaternion((1, 0, 0), pitch)
        @ mathutils.Quaternion((0, 1, 0), roll)
    )
    return _world_quat_in_bone(arm_obj, bone, world)


def _world_quat_in_bone(
    arm_obj, bone, world: "mathutils.Quaternion"
) -> "mathutils.Quaternion":
    """A rotation given in world axes, as the pose rotation of `bone` in its own rest frame."""
    to_armature = arm_obj.matrix_world.to_3x3().normalized().inverted()
    frame = (
        bone.matrix_local.to_3x3().normalized()
    ).to_quaternion()  # the bone's rest orientation in the armature
    world_in_armature = (
        to_armature.to_quaternion() @ world @ to_armature.to_quaternion().inverted()
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
                    pbone.keyframe_insert(data_path="rotation_quaternion", frame=frame)
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


# --- limb poses baked as morph targets, for a model's own rig ---------------------
#
# The core clips drive bones named root/spine/head, which an imported rig does not
# have, so they do nothing there; and a skeletal clip on the rig's own limbs is not
# applied by the overlay's glTF loader (checked: the legs and arms did not move).
# What the loader does honour is morph targets, so the limb motion is baked as
# those: the rig is posed here, each skinned mesh's deformation is recorded, and the
# overlay blends the targets by weight like blink, lip-sync and the generated
# avatars' run poses. Rotations are given about world axes and converted into each
# bone's own frame, so they do not depend on how the rig's bones are oriented.

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
LIMB_MIN_DISPLACEMENT = 0.01  # metres a mesh must move in some pose to get a target

RUN_KEYS = ("run_a", "run_b")
GESTURE_KEYS = ("gesture_l_out", "gesture_l_in", "gesture_r_out", "gesture_r_in")


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
    arm_axis = arm_obj.matrix_world.to_3x3().normalized().inverted() @ mathutils.Vector(
        axis
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
    turns: dict[str, "mathutils.Quaternion"] = {}
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
        if any(md.type == "ARMATURE" and md.object == arm_obj for md in m.modifiers)
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
                if "foot" in roles and (foot_angle or foot_offset != (0.0, 0.0, 0.0)):
                    rotations.append((roles["foot"][side], lateral, foot_angle))
                    offsets[roles["foot"][side]] = foot_offset
                if humanoid_arms and "upper_arm" in roles and "forearm" in roles:
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
                        (roles["forearm"][side], lateral, -R(12) if leading else -R(38))
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
        arm_meshes = _add_pose_targets(skinned, rest, captured, frozenset(RUN_KEYS))
        if arm_meshes:
            gesture_keys = list(GESTURE_KEYS)
    else:
        warnings.append("model rig has no upper_arm/forearm bones to gesture with")

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
        if (
            obj.type != "MESH" or obj.name == SIT_PROP_NODE
        ):  # the chair is shown only while sitting
            continue
        for corner in obj.bound_box:
            world_co = obj.matrix_world @ mathutils.Vector(corner)
            min_co.x, min_co.y, min_co.z = (
                min(min_co.x, world_co.x),
                min(min_co.y, world_co.y),
                min(min_co.z, world_co.z),
            )
            max_co.x, max_co.y, max_co.z = (
                max(max_co.x, world_co.x),
                max(max_co.y, world_co.y),
                max(max_co.z, world_co.z),
            )
            found = True
    if not found:
        return {"height_m": 0.0, "width_m": 0.0, "depth_m": 0.0}
    size = max_co - min_co
    return {
        "height_m": round(size.z, 3),
        "width_m": round(size.x, 3),
        "depth_m": round(size.y, 3),
    }


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
            lo = mathutils.Vector((min(lo.x, co.x), min(lo.y, co.y), min(lo.z, co.z)))
            hi = mathutils.Vector((max(hi.x, co.x), max(hi.y, co.y), max(hi.z, co.z)))
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
        if any(md.type == "ARMATURE" and md.object == armature for md in m.modifiers)
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
        obj for obj in objs if obj.type in ("CAMERA", "LIGHT") or obj in bone_shapes
    ]
    objs = [
        obj for obj in objs if obj not in discard
    ]  # before removing: a removed object cannot be compared
    for obj in discard:
        bpy.data.objects.remove(obj, do_unlink=True)
    meshes = [obj for obj in objs if obj.type == "MESH"]
    if not meshes:
        raise ValueError(f"{path.name} contains no mesh")
    armatures = [obj for obj in objs if obj.type == "ARMATURE"]
    if not armatures:
        objs = _flatten_static_meshes(meshes, objs)
    else:
        _bake_import_pose(armatures[0], meshes, warnings)

    bpy.context.view_layer.update()
    roots = [obj for obj in objs if obj.parent is None or obj.parent not in objs]
    face_yaw = _pose_sidecar(path).get("face_yaw", 0.0)
    if face_yaw:  # a model that does not face -Y (the overlay's front) is turned to
        turn = mathutils.Matrix.Rotation(math.radians(face_yaw), 4, "Z")
        for root in roots:
            root.matrix_world = turn @ root.matrix_world
        bpy.context.view_layer.update()
    lo, hi = _world_bounds(meshes)
    k = IMPORT_TARGET_HEIGHT / max(hi.z - lo.z, 1e-6)
    for (
        root
    ) in roots:  # scale about the world origin, so separate parts keep their spacing
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
        head_bones = [b for b in armatures[0].data.bones if "head" in b.name.lower()]
        if head_bones:
            head_loc = tuple(armatures[0].matrix_world @ head_bones[0].head_local)

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
            viseme_keys, key=lambda n: VISEME_CODES.index(n[len(SHAPE_KEY_PREFIX) :])
        ),
        "body": largest,
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


def _front_surface_y(meshes, x: float, z: float) -> float | None:
    """Y of the front-most surface of `meshes` at (x, z) (figures face -Y), by
    ray cast from in front; None if the ray misses everything."""
    best = None
    for obj in meshes:
        inv = obj.matrix_world.inverted()
        origin = inv @ mathutils.Vector((x, -10.0, z))
        direction = (inv.to_3x3() @ mathutils.Vector((0.0, 1.0, 0.0))).normalized()
        hit, location, *_ = obj.ray_cast(origin, direction)
        if hit:
            y = (obj.matrix_world @ location).y
            best = y if best is None else min(best, y)
    return best


# --- animation libraries ---------------------------------------------------------------
#
# A pack of clips made for a rig (assets/animation_library/<pack>.glb: Quaternius' Universal
# Animation Library is one) can be worn by any body with that rig's bones: the actions are
# keyed by bone name, so they are moved onto the body's armature as they are. A clip is
# rotation-and-hips motion, which does not depend on the body's proportions, except that the
# hips' travel is scaled by how tall the body's hips are. The overlay plays one clip at a
# time by role (idle, talk, run...): the `library_clips` map in the build says which clip
# fills which role. A body that wears a library keeps its own rest pose (the clips are
# relative to it) and gets no morph-target limb poses.


def plan_animation_library(
    deploy_dir: Path, armature, model_path: Path | None = None
) -> dict[str, tuple[Path, str]]:
    """{role: (library file, clip)} for the clips an installed library can give `armature`: the
    roles whose clip exists in a library the body has the core bones of (a body may lack some,
    such as fingers; `wear_animation_library` drops those tracks). Empty when there is no
    library or none fits (a mech's or a mascot's rig has other bone names). A body converted from
    another rig (`model_path`'s `.rig.json`) plays the libraries retargeted onto it."""
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
        held = {key for key in plan if key.split(".")[0] not in LIBRARY_EXTRAS}
        merge_picks(plan, pick_clips(clips, held), path)
    return plan


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
    before = _sit_pose(arm_obj, enter_clip)  # where it stands as the sit-down begins
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

    box("seat", (0, centre_y, seat_z - thick / 2), (2 * half_width, depth, thick))
    for sx in (-1, 1):
        for y in (back_y - leg, front_y + leg):
            box(
                "leg",
                (sx * (half_width - leg), y, (seat_z - thick) / 2),
                (leg, leg, seat_z - thick),
            )
    box("back", (0, back_y - leg / 2, seat_z + 0.2 * k), (2 * half_width, leg, 0.4 * k))
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
# finds the mesh by having exactly three targets (SIT_PROP_TARGETS), a count nothing else in
# a build has (1 blink, 2 run, 4 gesture, 6 run and gesture, 9 viseme); the other two are
# a hair's breadth of movement, there to make the count.
SIT_PROP_TARGETS = 3


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


def wear_animation_library(
    arm_obj, plan: dict[str, tuple[Path, str]], warnings: list[str]
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
            source_rig.data.bones.get("pelvis") if source_rig is not None else None
        )
        ratio = 1.0
        if (
            hips is not None
            and source_hips is not None
            and source_hips.head_local.z > 1e-6
        ):
            ratio = hips.head_local.z / source_hips.head_local.z
        slower = {
            clip: LIBRARY_TIME_SCALE[role]
            for role, (file, clip) in plan.items()
            if file == path and role in LIBRARY_TIME_SCALE
        }
        for action in [a for a in bpy.data.actions if a not in had_actions]:
            if action.name not in wanted:
                bpy.data.actions.remove(action)
                continue
            stretch = slower.get(action.name, 1.0)
            _drop_curves(action, ours)
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


# --- outfit models -----------------------------------------------------------------
#
# An outfit model is a set of skinned meshes made for the same skeleton as its body
# (assets/outfit_models/<body>/<outfit>.glb; the modular outfit packs are built that
# way). It is worn by re-binding its meshes to the body's armature, so they follow
# every pose the body does, and by deleting the part of the body the outfit replaces:
# left in, the body clips through the cloth. The head and neck stay; the outfit
# supplies the rest, hands and feet included.

_OUTFIT_KEEPS_BODY_BONES = ("head", "neck")


def outfit_model_path(deploy_dir: Path, base_template: str, outfit: str) -> Path:
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
    neck. False (nothing changed) if the mesh has no weights or has shape keys."""
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
            max(weights.items(), key=lambda item: item[1])[0] if weights else None
        )
        if strongest is None or not any(
            part in names.get(strongest, "") for part in _OUTFIT_KEEPS_BODY_BONES
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
    for obj in [o for o in objs if o.type in ("CAMERA", "LIGHT") or o in shapes]:
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
        mesh.matrix_basis = local  # the body's own scale and position now carry it
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
    torsos = [m for m in meshes if m.name.lower().rsplit(".", 1)[0].endswith("body")]
    _BODY["obj"] = max(
        torsos or meshes, key=lambda m: m.dimensions.x * m.dimensions.y * m.dimensions.z
    )
    _refresh_bounds(imported)
    return True


# --- per-model pose fixes ----------------------------------------------------------
#
# A model may come with a hand that faces the wrong way for the poses we give it (Miss
# Minutes' fist has its fingers turned outward, so an arm that bends reads as folding the
# wrong way). `<model>.pose.json` beside it says which bones to turn about their own length,
# in degrees: {"roll_bones": {"hand.L_04": 180}}. It is applied to the rest pose, before the
# arms are relaxed, so everything after starts from the corrected hand.


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
        return {
            "roll_bones": {str(k): float(v) for k, v in rolls.items()},
            "face_yaw": float(yaw),
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
        if any(md.type == "ARMATURE" and md.object == armature for md in m.modifiers)
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


# --- relaxed arms -------------------------------------------------------------------
#
# Character bases are modelled in a T-pose, arms straight out, and that stays their rest
# pose: the avatar would stand with its arms out through every idle. Arms held out are the
# wrong starting point for the gestures too, which swing a limb sideways from hanging (see
# bake_limb_morphs). So an arm held well off hanging (RELAX_IF_HANGING_LESS_THAN) is brought down to
# RELAXED_ARM_DEG off vertical, and that becomes the rest pose; gestures and the running
# swing then start from, and return to, a natural stance.

RELAXED_ARM_DEG = 10  # how far off vertical a relaxed upper arm hangs
RELAXED_ELBOW_DEG = (
    5  # and how far forward its forearm is bent from it, as a person's hang
)
RELAX_IF_HANGING_LESS_THAN = (
    0.75  # an upper arm whose downward component is below this (over ~41 degrees off
)
# vertical) is brought down: a T-pose arm is at 0, a raised or bent arm at about 0.6; the mechs' (0.8+) are not


def _refresh_bounds(imported: dict) -> None:
    """Re-measures the figure after its shape changed (an outfit, relaxed arms)."""
    bpy.context.view_layer.update()
    lo, hi = _world_bounds(imported["meshes"])
    imported["bounds"] = (lo, hi)
    _BODY.update(front=lo.y, back=hi.y, bottom=lo.z, half_width=(hi.x - lo.x) / 2)


def _aim_bone(armature, name: str, direction_world: "mathutils.Vector") -> None:
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
        if any(md.type == "ARMATURE" and md.object == armature for md in m.modifiers)
    ]
    if any(m.data.shape_keys is not None for m in skinned):
        return False
    world = armature.matrix_world
    changed = False
    for side in ("min_x", "max_x"):
        upper = armature.data.bones[roles["upper_arm"][side]]
        along = ((world @ upper.tail_local) - (world @ upper.head_local)).normalized()
        if -along.z >= RELAX_IF_HANGING_LESS_THAN:
            continue  # already hanging
        out = 1.0 if along.x >= 0 else -1.0
        hang, bend = math.radians(RELAXED_ARM_DEG), math.radians(RELAXED_ELBOW_DEG)
        upper_to = mathutils.Vector((out * math.sin(hang), 0.0, -math.cos(hang)))
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
        return armature.matrix_world @ bone.head_local.lerp(bone.tail_local, along)

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
    top = (cx, cy, hi.z + 0.1 * k_head)
    worn = (
        cx,
        cy,
        hi.z - 0.12 * head_h,
    )  # a hat sits down on the head; the orbit floats above it
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
            frames[socket] = {"location": worn, "k": k_head}
        elif socket == "socket_orbit":
            frames[socket] = {"location": top, "k": k_head}
        elif socket == "socket_eyes":
            if (
                eyes is not None
            ):  # the model's own eyes: centred on them, sized by how far apart they are
                eye_x, eye_z = (eyes[0][0] + eyes[1][0]) / 2, (
                    eyes[0][1] + eyes[1][1]
                ) / 2
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
                    (face + 0.05 * k_head) if face is not None else cy - 0.5 * k_head,
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


# --- face overlay for painted-on faces -----------------------------------------
#
# A model whose face is a texture has no eyelids or mouth to move. A sidecar
# `<model>.face.json` next to it says where its eyes and mouth are, as fractions
# of the model's front bounding box (x across, z up; w/h are fractions of the
# width/height too), plus the colours to match:
#
#   {"skin": "#D37454", "mouth_color": "#101010", "teeth_color": "#C4C4C4",
#    "eye_l": {"x": .444, "z": .709, "w": .128, "h": .225}, "eye_r": {...},
#    "mouth": {"x": .545, "z": .523, "w": .237, "h": .104}, "smile": 0.5}
#
# The generator lays its own parts over the painted ones: a skin-coloured lid
# per eye that closes from the top (the `blink` shape key), and a skin-coloured
# patch hiding the painted mouth with the nine-viseme mouth and teeth on top.
# The skin is the model's own material sampled at a plain spot (`skin_at`, default
# between the eyes), so the parts are lit like the painted skin; `skin` is only
# the fallback for a model with no texture coordinates.


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
    lin = lambda c: (
        c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
    )  # noqa: E731
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
            layer.data[loop_index].uv = (uv[0] + co.x * spread, uv[1] + co.y * spread)


def _ellipse(name: str, centre, w: float, h: float, material) -> bpy.types.Object:
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


def build_face_overlay(face: dict, imported_meshes, lo, hi, armature, bone: str | None):
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
        ys = [v.y for v in verts if abs(v.x - x) <= w / 2 and abs(v.z - z) <= h / 2]
        return min(ys) if ys else lo.y

    # Skin: the model's own material at a plain spot between the eyes if it has
    # one, else a flat colour from the sidecar.
    ex = (face["eye_l"]["x"] + face["eye_r"]["x"]) / 2
    ez = (face["eye_l"]["z"] + face["eye_r"]["z"]) / 2
    skin_at = face.get("skin_at", {"x": ex, "z": ez})
    texel = _skin_texel(
        imported_meshes, lo.x + skin_at["x"] * width, lo.z + skin_at["z"] * height
    )
    skin_mat = (
        texel[0] if texel else _flat_material("FaceSkin", face.get("skin", "#D37454"))
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
            y = co.y * (0.02 + open_ * 0.85 * 0.72) + smile * 0.25 * (xn * xn - 0.5)
            return (x, y, co.z + round_ * 0.04)

        return fn

    def teeth_deform(code):
        # For a realistic mouth the second plane is not a lighter inset of the first but a
        # sliver of upper teeth hugging the top edge of the dark opening, shown when it is
        # open enough (or the viseme is a teeth-together one) and a hairline otherwise.
        open_, width_k, round_, together = VISEME_MOUTH_MATRIX[code]
        full_h = 0.15 + open_ * 0.85  # the opening's height (it spans +-half of this)
        share = 0.8 if together else 0.3 if (open_ >= 0.3 and round_ < 0.5) else 0.0
        strip = max(0.004, full_h * share * 0.5)
        centre = (full_h * 0.5 - strip * 0.5) * 0.92
        # The opening narrows towards its top: its half-width at height f (of its half-height) is
        # sqrt(1 - f^2 / 2) of the full one, for the square-to-disc map the plane is built with.
        edge = 0.92 * math.sqrt(max(0.02, 1.0 - 0.5 * (centre / (full_h * 0.5)) ** 2))

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
                c: (teeth_deform(c) if face.get("oval") else fill_deform(c))
                for c in VISEME_CODES
            },
            oval=bool(face.get("oval", False)),
        )
    )
    teeth.scale = (mw * 0.92, mh * 1.15, 1.0)
    parts += [mouth, teeth]

    for obj in [*lids, *parts]:
        _bind_rigid(obj, armature, bone or "head")
    return lids, parts, viseme_keys


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


def _decimate_meshes(max_triangles: int) -> None:
    """Brings the scene's meshes down to about `max_triangles` triangles in total
    by collapsing the heavier ones by the same ratio. Bone weights are interpolated
    by the modifier, so a skinned mesh still deforms with its rig."""
    meshes = [o for o in bpy.data.objects if o.type == "MESH"]
    for mesh in meshes:
        if (
            mesh.data.users > 1
        ):  # instances of one mesh: a modifier cannot be applied to shared data
            mesh.data = mesh.data.copy()
    counts = {m.name: sum(len(p.vertices) - 2 for p in m.data.polygons) for m in meshes}
    total = sum(counts.values())
    if total <= max_triangles:
        return
    ratio = max_triangles / total
    for mesh in meshes:
        if counts[mesh.name] < 600 or mesh.data.shape_keys is not None:
            continue  # small parts keep their detail; a mesh with shape keys cannot be modified
        mod = mesh.modifiers.new("Decimate", "DECIMATE")
        mod.ratio = ratio
        bpy.context.view_layer.objects.active = mesh
        bpy.ops.object.modifier_move_to_index(
            modifier=mod.name, index=0
        )  # before the armature, on the bind shape
        bpy.ops.object.modifier_apply(modifier=mod.name)


def _names_of(mesh) -> str:
    """Lower-case names of a mesh's materials and textures, for telling parts apart."""
    names = [mesh.name]
    for slot in mesh.material_slots:
        if slot.material is not None:
            names.append(slot.material.name)
            if slot.material.node_tree is not None:
                names += [
                    n.image.name
                    for n in slot.material.node_tree.nodes
                    if n.type == "TEX_IMAGE" and n.image
                ]
    return " ".join(names).lower()


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
    for side in ([p for p in points if p.x < mid], [p for p in points if p.x >= mid]):
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


def run_face_landmarks(in_path: Path, out_path: Path) -> None:
    """Finds where a model's eyes and mouth are and writes the face sidecar for
    them (see "face overlay" above), as fractions of the model's box. The eyes are
    the mesh whose material or texture is named for them; the mouth is the crease
    between the lips, found in the face's side profile (the lips are two bulges,
    and the crease is the deepest point between them). For a realistic face with
    its own lips, so the sidecar draws the mouth over them rather than hiding them."""
    reset_scene()
    imported = build_imported_model(in_path, [])
    lo, hi = imported["body_bounds"]
    width, height = hi.x - lo.x, hi.z - lo.z
    eyes, eye_meshes, why_not = _find_eyes(imported)
    if eyes is None:
        raise ValueError(why_not)
    eye_z = sum(e[1] for e in eyes) / 2
    eye_w = sum(e[2] for e in eyes) / 2
    cx = (eyes[0][0] + eyes[1][0]) / 2

    # Side profile down the middle of the face: how far forward the surface is at each height.
    body_meshes = [m for m in imported["meshes"] if m not in eye_meshes]
    step = 0.002
    profile = []
    z = eye_z
    while z > eye_z - 0.45:
        y = _front_surface_y(body_meshes, cx, z)
        profile.append((z, y))
        z -= step
    ys = [y for _, y in profile]
    # The chin's underside: below it the surface jumps back to the neck.
    chin = next(
        (
            i
            for i in range(len(ys) - 5)
            if ys[i] is not None and ys[i + 5] is not None and ys[i + 5] - ys[i] > 0.04
        ),
        len(ys) - 1,
    )
    window = [(z, y) for (z, y) in profile[:chin] if y is not None]
    nose = (
        min(range(len(window)), key=lambda i: window[i][1]) if window else 0
    )  # tip of the nose
    span = window[nose:]
    # Lips: below the nose the profile dips forward to the upper lip, recedes to the
    # crease between the lips, and dips forward again to the lower lip. So the crease
    # is the first local maximum (furthest back) after the first local minimum.
    smooth = [
        sum(y for _, y in span[max(0, i - 2) : i + 3])
        / len(span[max(0, i - 2) : i + 3])
        for i in range(len(span))
    ]
    extrema = (
        []
    )  # (index, "min" | "max"): where the profile changes direction (y grows going back)
    heading = 0
    for i in range(1, len(smooth)):
        delta = smooth[i] - smooth[i - 1]
        now = 1 if delta > 0.0001 else -1 if delta < -0.0001 else 0
        if now == 0:
            continue
        if heading and now != heading:
            extrema.append((i - 1, "max" if heading == 1 else "min"))
        heading = now
    crease = None
    for (low, kind_low), (high, kind_high) in zip(extrema, extrema[1:]):
        if (
            kind_low == "min"
            and kind_high == "max"
            and smooth[high] - smooth[low] >= 0.001
        ):
            crease = high
            break
    chin_z = window[-1][0] if window else eye_z - 0.2
    along = lambda z: (eye_z - z) / max(
        eye_z - chin_z, 1e-6
    )  # noqa: E731 -- 0 at the eye line, 1 at the chin
    # Faces put the lips about two thirds of the way from the eyes to the chin. A found
    # crease far from there is the sulcus under the lower lip (a subtle lip leaves no clear
    # crease), so it is not trusted.
    if crease is not None and 0.55 <= along(span[crease][0]) <= 0.75:
        mouth_z = span[crease][0]
    else:
        crease = None
        mouth_z = eye_z - 0.667 * (eye_z - chin_z)
    if os.environ.get("DESKMATE_FACE_DEBUG"):
        print(
            "PROFILE",
            [
                (round(z, 3), round(y * 1000) if y is not None else None)
                for z, y in profile[::1]
                if eye_z - 0.18 < z < eye_z - 0.06
            ],
        )
        print(
            "DEBUG eye_z",
            eye_z,
            "chin_z",
            window[-1][0] if window else None,
            "nose_z",
            window[nose][0] if window else None,
            "mouth_z",
            mouth_z,
            "found_crease",
            crease is not None,
        )

    def frac_x(x):
        return (x - lo.x) / width

    def frac_z(z):
        return (z - lo.z) / height

    mouth_w = 1.6 * eye_w  # a mouth is about one and a half eye widths wide
    face = {
        "patch": False,  # keep the real lips; the mouth is drawn over them
        "oval": True,
        "mouth_color": "#1d0b0b",  # the dark inside of the mouth: at rest, the line between the lips
        "teeth_color": "#d9d2c6",  # a strip of upper teeth when it opens
        "eye_l": {
            "x": round(frac_x(eyes[0][0]), 4),
            "z": round(frac_z(eyes[0][1]), 4),
            "w": round(eyes[0][2] / width, 4),
            "h": round(0.6 * eyes[0][3] / height, 4),
        },
        "eye_r": {
            "x": round(frac_x(eyes[1][0]), 4),
            "z": round(frac_z(eyes[1][1]), 4),
            "w": round(eyes[1][2] / width, 4),
            "h": round(0.6 * eyes[1][3] / height, 4),
        },
        "mouth": {
            "x": round(frac_x(cx), 4),
            "z": round(frac_z(mouth_z), 4),
            "w": round(mouth_w / width, 4),
            "h": round(0.4 * mouth_w / height, 4),
        },
    }
    out_path.write_text(json.dumps(face, indent=1))
    print(f"DESKMATE_FACE_OK {out_path}")


def run_convert(
    in_path: Path, out_path: Path, max_texture: int, max_triangles: int = 0
) -> None:
    """Re-exports a model (a .gltf with its .bin and textures beside it, say) as
    one .glb with every texture shrunk to at most `max_texture` px and saved as JPEG
    (unless it needs alpha), and, if `max_triangles` is set, the meshes decimated to
    fit it. Models are often shipped with 2K-4K PNG textures and detail to match,
    which alone would exceed the size and triangle budgets."""
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.import_scene.gltf(filepath=str(in_path))
    # The importer makes a sphere to show each bone with; it is no part of the model, and re-exported it
    # would be a two-metre ball in the figure.
    for obj in {
        pb.custom_shape
        for a in bpy.data.objects
        if a.type == "ARMATURE"
        for pb in a.pose.bones
        if pb.custom_shape is not None
    }:
        bpy.data.objects.remove(obj, do_unlink=True)
    for image in bpy.data.images:
        width, height = image.size
        if max(width, height) > max_texture:
            ratio = max_texture / max(width, height)
            image.scale(max(1, round(width * ratio)), max(1, round(height * ratio)))
    if max_triangles:
        _decimate_meshes(max_triangles)
    bpy.ops.export_scene.gltf(
        filepath=str(out_path),
        export_format="GLB",
        export_image_format="JPEG",
        export_jpeg_quality=85,
    )
    print(f"DESKMATE_CONVERT_OK {out_path}")


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
        warnings.append(
            f"unrecognized file extension {in_path.suffix!r}, attempted no import"
        )

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
            if (
                kb.name in ("muscularity", "femininity")
                and kb.name not in supported_proportions
            ):
                supported_proportions.append(kb.name)
    if bone_names and any("spine" in b.lower() for b in bone_names):
        supported_proportions += ["height", "build"]

    lowered_bones = [b.lower() for b in bone_names]
    has_head = any("head" in b for b in lowered_bones)
    has_arms = any("arm" in b for b in lowered_bones)
    has_legs = any("leg" in b or "thigh" in b for b in lowered_bones)
    has_face = len(viseme_keys) > 0

    # A socket is carried where the rig has a bone for it. A model with no rig
    # is given the canonical one when it is built, so it carries that rig's sockets.
    socket_bones = (
        find_socket_bones(bone_names) if bone_names else canonical_socket_bones()
    )
    declared = [
        obj.name
        for obj in bpy.context.scene.objects
        if obj.type == "EMPTY" and obj.name in _SOCKET_NAMES
    ]
    supported_sockets = [
        name for name in _SOCKET_NAMES if name in socket_bones or name in declared
    ]
    has_head = (
        has_head or "socket_head_top" in socket_bones
    )  # a head bone, the canonical rig's, or a trunk standing in

    triangle_count = count_triangles()

    result = {
        "template_id": in_path.stem,
        "tier": "tier_1_custom",
        "has_face": has_face,
        "capabilities": {
            "has_head": has_head,
            "has_arms": has_arms,
            "has_legs": has_legs,
            "has_face": has_face,
        },
        "bone_names": bone_names,
        "supported_proportions": supported_proportions,
        "viseme_shape_keys": sorted(
            viseme_keys,
            key=lambda n: (
                VISEME_CODES.index(n[len(SHAPE_KEY_PREFIX) :])
                if n[len(SHAPE_KEY_PREFIX) :] in VISEME_CODES
                else 99
            ),
        ),
        "supported_sockets": supported_sockets,
        "socket_bones": socket_bones,
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

    if FACE_PATH is not None:
        run_face_landmarks(Path(FACE_PATH), OUT_PATH)
        return

    if CONVERT_PATH is not None:
        run_convert(Path(CONVERT_PATH), OUT_PATH, MAX_TEXTURE, MAX_TRIANGLES)
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
        except (
            Exception
        ) as exc:  # noqa: BLE001 -- an unusable asset must not fail the whole build
            warnings.append(
                f"could not use model {resolution['path'].name} ({exc}); building procedurally instead"
            )
            reset_scene()
            bpy.context.scene.render.fps = 30

    run_keys: list[str] = []
    library_plan: dict[str, tuple[Path, str]] = {}
    library_clips: dict[str, str] = {}
    socket_bones: dict = {}
    if imported is not None:
        body = imported["body"]
        head_loc, has_arms, has_legs = (
            imported["head_loc"],
            imported["has_arms"],
            imported["has_legs"],
        )
        viseme_keys = imported["viseme_keys"]
        has_face_requested = has_face
        if has_face and not viseme_keys and _face_sidecar(resolution["path"]) is None:
            warnings.append(
                "model has no viseme shape keys; building it without a face (talk_pulse speech indicator)"
            )
        has_face = bool(viseme_keys)
        eyes: list = []
        outfit = wardrobe.get("outfit", "none")
        outfit_file = outfit_model_path(DEPLOY_DIR, base_template, outfit)
        if (
            outfit != "none"
            and imported["armature"] is not None
            and outfit_file.is_file()
        ):
            wear_outfit_model(imported, outfit_file, warnings)
        elif outfit != "none":
            warnings.append(
                "wardrobe is not layered onto an imported model; it keeps its own clothes"
            )
        if wardrobe.get("footwear", "none") != "none":
            warnings.append(
                "footwear is not layered onto an imported model; it keeps its own"
            )
        if imported["armature"] is not None:
            rolls = _pose_sidecar(resolution["path"]).get("roll_bones")
            if rolls:
                roll_model_bones(imported, rolls, warnings)
            library_plan = plan_animation_library(
                DEPLOY_DIR, imported["armature"], resolution["path"]
            )
            if (
                library_plan
            ):  # its clips are relative to the rig's own rest pose: leave that alone
                imported["arms_relaxed"] = False
            else:
                imported["arms_relaxed"] = relax_arms(
                    imported
                )  # after the outfit, so it hangs with the body

        accessory_objs = []  # bound with automatic weights (no bone for their socket)
        bound_accessories: list = (
            []
        )  # (object, bone): rigidly attached to the socket's bone
        socket_bones = (
            find_socket_bones([b.name for b in imported["armature"].data.bones])
            if imported["armature"] is not None
            else canonical_socket_bones()
        )
        frames = imported_socket_frames(imported, socket_bones)
        for socket_name in _SOCKET_NAMES:
            item = accessories.get(socket_name)
            if not item:
                continue
            frame = frames.get(socket_name)
            built = build_socket_accessory(
                socket_name, item, head_loc, palette, DEPLOY_DIR, frame
            )
            if frame is None:
                warnings.append(
                    f"model has no bone for {socket_name}; {item} is placed approximately"
                )
                accessory_objs += built
            else:
                bound_accessories += [(obj, socket_bones[socket_name]) for obj in built]

        arm_obj = imported["armature"]
        meshes = accessory_objs
        if arm_obj is None:  # a static model: give it the canonical rig and bind it
            arm_obj = build_armature(has_arms, head_loc)
            meshes = [*imported["meshes"], *accessory_objs]
        else:
            if (
                "socket_head_top" not in socket_bones
            ):  # no head to turn: no skeletal idles either
                warnings.append(
                    "model rig has no head bone; its head and trunk idle animations are empty"
                )
        parent_to_armature(arm_obj, meshes)
        for obj, bone_name in bound_accessories:
            bind_rigid(arm_obj, [obj], bone_name)
        if imported["armature"] is None:
            bake_proportions(arm_obj, proportions)
        bake_morphological_proportions(body, proportions)

        face_map = _face_sidecar(resolution["path"])
        if face_map is not None and has_face_requested and not viseme_keys:
            lo, hi = imported[
                "body_bounds"
            ]  # not "bounds": an outfit worn over it (a hood) changes those
            centre = mathutils.Vector(
                (
                    lo.x + face_map["mouth"]["x"] * (hi.x - lo.x),
                    (lo.y + hi.y) / 2,
                    lo.z + face_map["mouth"]["z"] * (hi.z - lo.z),
                )
            )
            bone = (
                _face_bone(imported["meshes"], centre)
                if imported["armature"] is not None
                else "head"
            )
            eyes, _, viseme_keys = build_face_overlay(
                face_map, imported["meshes"], lo, hi, arm_obj, bone
            )
            has_face = True
    else:
        layout = None
        if base_template in ("standard_humanoid_female", "standard_humanoid_male"):
            body, head_loc, has_arms, has_legs, layout = build_body_humanoid(
                proportions, palette, surface_material, base_template
            )
        else:
            procedural_template = (
                base_template
                if base_template == "primitive_mechanical"
                else "primitive_blob"
            )
            body, head_loc, has_arms, has_legs = build_body_tier3(
                procedural_template, proportions, palette, surface_material
            )
        head = build_head(head_loc)
        eyes = build_eyes(head_loc, hex_to_rgba(palette["primary"]))
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

        outfit_objs = build_outfit(
            body, wardrobe.get("outfit", "none"), head_loc, palette, surface_material
        )
        footwear_objs = build_footwear(wardrobe.get("footwear", "none"), palette)

        accessory_objs = []
        for socket_name in _SOCKET_NAMES:
            item = accessories.get(socket_name)
            if item:
                accessory_objs += build_socket_accessory(
                    socket_name, item, head_loc, palette, DEPLOY_DIR
                )

        if has_legs and layout is not None:
            run_keys = add_run_shape_keys([body, *outfit_objs, *footwear_objs], layout)
        arm_obj = build_armature(has_arms, head_loc, has_legs=has_legs, layout=layout)
        meshes = [
            body,
            head,
            *eyes,
            mouth,
            teeth,
            *outfit_objs,
            *footwear_objs,
            *accessory_objs,
        ]
        parent_to_armature(arm_obj, meshes)

        bake_proportions(arm_obj, proportions)
        bake_morphological_proportions(body, proportions)

    gesture_keys: list[str] = []
    if library_plan:  # real clips move the limbs: no morph-target poses to double them
        library_clips = wear_animation_library(arm_obj, library_plan, warnings)
        if library_clips.get("sit_idle") in CHAIR_SIT_CLIPS:
            if build_sit_prop(
                arm_obj, library_clips["sit_idle"], library_clips["sit_enter"]
            ):
                library_clips["sit_prop"] = SIT_PROP_NODE
            else:  # nothing to sit on: the sit clips would have it sit on air
                warnings.append(
                    "could not place a chair for the sitting clips; the body does not sit down"
                )
                for role in ("sit_enter", "sit_idle", "sit_exit"):
                    library_clips.pop(role, None)
    elif imported is not None and imported["armature"] is not None:
        run_keys, gesture_keys = bake_limb_morphs(
            arm_obj,
            imported["meshes"],
            warnings,
            humanoid_arms=bool(imported.get("arms_relaxed")),
        )

    speech_indicator_mode = "viseme_shapekeys" if has_face else "talk_pulse"
    talk_pulse_clip = None
    if not has_face:
        talk_pulse_clip = build_talk_pulse(arm_obj)

    skeletal = False
    skeletal_clips: list[str] = []
    if imported is not None and imported["armature"] is not None and not library_clips:
        skeletal_clips = build_imported_clips(arm_obj, eyes, socket_bones)
        # All or nothing: a rig missing a chest bone gets the head clips but only empty
        # canonical stretch/sleepy/fidget, which the overlay must not prefer over its own.
        skeletal = all(name in skeletal_clips for name in _SKELETAL_CLIPS)
    # The whole catalog is always listed (the brain requires the core clips, and a rig the
    # skeletal clips do not suit keeps the canonical ones); the clips baked on the model's
    # own bones take the place of their canonical namesakes.
    baked_clips = build_all_clips(
        arm_obj,
        eyes,
        has_arms=has_arms,
        has_legs=has_legs,
        skip=frozenset(skeletal_clips),
    )
    baked_clips = list(dict.fromkeys([*skeletal_clips, *baked_clips]))
    if talk_pulse_clip:
        baked_clips.append(talk_pulse_clip)

    bpy.context.scene.frame_set(1)
    recalc_normals()
    validate_meshes()

    total_triangles = count_triangles()
    if total_triangles > TRIANGLE_BUDGET:
        fail(
            out_build_json,
            f"budget_exceeded: triangles={total_triangles} (max {TRIANGLE_BUDGET})",
        )
        return

    bounds = compute_bounds()
    export_glb(OUT_PATH)

    size_bytes = OUT_PATH.stat().st_size
    if size_bytes > SIZE_BUDGET_BYTES:
        fail(
            out_build_json,
            f"budget_exceeded: size_bytes={size_bytes} (max {SIZE_BUDGET_BYTES})",
        )
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
        "capabilities": {
            "has_head": True,
            "has_arms": has_arms,
            "has_legs": has_legs,
            "has_face": has_face,
        },
        "viseme_shape_keys": viseme_keys,
        "blink_shape_keys": [BLINK_SHAPE_KEY] if eyes else [],
        "run_shape_keys": run_keys,
        "gesture_shape_keys": gesture_keys,
        "skeletal_clips": skeletal,  # clips on the model's own bones: the overlay may play them
        "library_clips": library_clips,  # role -> clip, from an animation library worn by this body
        "animation_clips": clip_metadata(baked_clips),
        "bounds": bounds,
        "warnings": warnings,
    }
    out_build_json.write_text(json.dumps(build_result, indent=2))
    print(f"DESKMATE_BUILD_OK {OUT_PATH}")


if __name__ == "__main__":
    main()
