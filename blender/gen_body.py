"""
The procedural body, head, eyes and mouth; the armature and its binding;
proportion baking; the faceless speech indicator.
"""

from __future__ import annotations

import math

import bpy  # pyright: ignore[reportMissingImports]
import mathutils  # pyright: ignore[reportMissingImports]

from blender.gen_constants import (
    VISEME_MOUTH_MATRIX,
)
from blender.gen_garments import (
    _assemble,
    _prim,
)
from blender.gen_helpers import (
    hex_to_rgba,
    make_material,
)
from blender.viseme_shapekeys import (
    VISEME_CODES,
    shape_key_name,
)


def build_body_tier3(
    base_template: str, proportions: dict, palette: dict, surface_material: str
):
    """Returns (body_obj, head_location, has_arms, has_legs). Tier 3 only:
    primitive_blob (sphere, no limbs) or primitive_mechanical (boxy torso +
    arms, no legs -- a simple hovering/treaded bot)."""
    primary = hex_to_rgba(palette["primary"])
    height_scale = proportions.get("height", 1.0)
    build_scale = proportions.get("build", 1.0)
    head_loc: tuple[float, float, float]

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
    proportions: dict,
    palette: dict,
    surface_material: str,
    base_template: str = "",
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
        0.46
        * b
        * (1.0 + 0.30 * (mus - 0.5) - 0.20 * (fem - 0.5))
        * char["shoulder"]
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
            _prim(
                "sphere", (0, 0, 1.22 * h), scale=(shoulder, 0.32 * b, 0.46 * h)
            ),
            mat,
        ),  # torso
        (
            _prim(
                "cylinder",
                (0, 0, 1.66 * h),
                r=0.13 * b * char["neck"],
                depth=0.2 * h,
            ),
            mat,
        ),  # neck
    ]
    for x in (-leg_x, leg_x):
        parts.append(
            (_prim("cylinder", (x, 0, 0.45 * h), r=leg_r, depth=0.9 * h), mat)
        )
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
        parts.append(
            (_prim("sphere", (x, 0, hand_z), r=arm_r * 1.3), mat)
        )  # hand

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
    luminance = (
        0.2126 * head_color[0] + 0.7152 * head_color[1] + 0.0722 * head_color[2]
    )
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
            co.x, co.y = (
                half * u * math.sqrt(1 - v * v / 2),
                half * v * math.sqrt(1 - u * u / 2),
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
    arm_x, arm_r, shoulder_z = (
        layout["arm_x"],
        layout["arm_r"],
        layout["shoulder_z"],
    )
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
        w_side * w_sh
        if abs(co.x) > arm_x - 2.0 * arm_r and co.z > hip_z * 0.7
        else 0.0
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
    has_arms: bool,
    head_loc,
    *,
    has_legs: bool = False,
    layout: dict | None = None,
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
    head_bone.head, head_bone.tail = (
        (0, 0, head_loc[2] - 0.4),
        (
            0,
            0,
            head_loc[2] + 0.3,
        ),
    )
    head_bone.parent = spine

    if has_arms:
        for side, x in (("L", -0.7), ("R", 0.7)):
            arm = eb.new(f"arm_{side}")
            if (
                layout is not None
            ):  # limbs that hang straight down from the shoulder
                ax = x / 0.7 * layout["arm_x"]
                arm.head, arm.tail = (
                    (ax, 0, layout["shoulder_z"]),
                    (
                        ax,
                        0,
                        layout["hand_z"],
                    ),
                )
            else:
                arm.head, arm.tail = (
                    (0, 0, head_loc[2] - 0.6),
                    (
                        x,
                        0,
                        head_loc[2] - 1.0,
                    ),
                )
            arm.parent = spine

    if has_legs and layout is not None:
        for side, sign in (("L", -1), ("R", 1)):
            leg = eb.new(f"leg_{side}")
            leg.head, leg.tail = (
                (sign * layout["leg_x"], 0, layout["hip_z"]),
                (
                    sign * layout["leg_x"],
                    0,
                    0.05,
                ),
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
            sum(
                (mathutils.Vector(c) for c in mesh.bound_box),
                mathutils.Vector(),
            )
            / 8
        )
        bone = min(
            bones,
            key=lambda item: _distance_to_segment(centre, item[1], item[2]),
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
