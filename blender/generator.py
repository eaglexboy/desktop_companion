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
has_face; a model with no mouth is marked `sound_waves`, which the overlay shows as arcs), requested wardrobe/
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

import bpy  # pyright: ignore[reportMissingImports]
import mathutils  # pyright: ignore[reportMissingImports]

sys.path.insert(
    0, str(Path(__file__).resolve().parent.parent)
)  # repo root, for blender.viseme_shapekeys
from blender import pack_profiles
from blender.animation_library import (
    SIT_PROP_NODE,
    chair_sit_clips,
    rig_family_of,
)
from blender.gen_accessory_models import (
    accessory_skin,
    bind_like_body,
    bind_rigid,
    build_socket_accessory,
)
from blender.gen_body import (
    BLINK_SHAPE_KEY,
    add_run_shape_keys,
    bake_morphological_proportions,
    bake_proportions,
    build_armature,
    build_body_humanoid,
    build_body_tier3,
    build_eyes,
    build_head,
    build_mouth,
    parent_to_armature,
)
from blender.gen_clips import (
    _SKELETAL_CLIPS,
    bake_limb_morphs,
    build_all_clips,
    build_imported_clips,
    clip_metadata,
)
from blender.gen_face import (
    _face_bone,
    _face_sidecar,
    build_face_overlay,
)
from blender.gen_garments import (
    _BODY,
    build_footwear,
    build_outfit,
)
from blender.gen_helpers import (
    count_triangles,
    fail,
    hex_to_rgba,
    reset_scene,
    resolve_template,
)
from blender.gen_imported import (
    _pose_sidecar,
    build_imported_model,
    imported_socket_frames,
    relax_arms,
    roll_model_bones,
    tint_hair,
)
from blender.gen_inspect import (
    _SOCKET_NAMES,
    run_convert,
    run_face_landmarks,
    run_inspect,
)
from blender.gen_models import (
    build_sit_prop,
    fit_clips_to_the_frame,
    outfit_model_path,
    plan_animation_library,
    wear_animation_library,
    wear_outfit_model,
)
from blender.glb_post import brighten_emissive
from blender.rigs import arm_spread
from blender.sockets import canonical_socket_bones, find_socket_bones

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
pack_profiles.configure(
    DEPLOY_DIR
)  # the user's animation profiles are in the deploy directory
INSPECT_PATH = _arg("--inspect")
CONVERT_PATH = _arg("--convert")
FACE_PATH = _arg("--face-landmarks")
MAX_TEXTURE = int(_arg_or("--max-texture", "1024"))
MAX_TRIANGLES = int(_arg_or("--max-triangles", "0"))

TRIANGLE_BUDGET = 30_000
SIZE_BUDGET_BYTES = (
    15_728_640  # 15 MiB: the body, its textures and every clip it wears
)


# --- wardrobe: procedural garments + socket attachments -----------------------
#
# Blender space: +Z up, the avatar faces -Y (the overlay camera looks at it
# from -Y), +X is the avatar's left as seen by the viewer. Every catalog item
# below is its own recognisable shape -- the companion is told what it is
# wearing (see brain/appearance/spec.py's look_summary), so an item that renders
# as something else, or not at all, makes it describe a look nobody can see.


# --- proportion baking ---------------------------------------------------------


# Baking an evaluated shape-key mix into Basis and removing the
# intermediate keys is a mesh-data operation (bpy.ops.object.shape_key_*
# doesn't have a direct "bake all to basis" op) -- deferred to when a
# real Tier 2 asset exists to implement and test this against for real,
# per the same reasoning as above.


# --- skeletal clips for an imported rig -------------------------------------------
#
# The clips above drive bones named root/spine/head, which an imported rig does not
# have. For one that does have a head (and usually a neck and spine), the head and
# trunk idles are baked on its own bones instead. Each rotation is given about world
# axes (yaw about Z, pitch about X, roll about Y) and converted into the bone's own
# rest frame, so it does not depend on how the rig's bones are oriented. The overlay
# plays them by name (avatar_controller.h `playClip`), one at a time.


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


# --- budget enforcement + export -----------------------------------------------


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


def export_glb(out_path: Path, *, sample_every_frame: bool = False) -> None:
    """`sample_every_frame` is for a model's own generated clips (a head idle, a nod), which the exporter
    only writes when it samples them; the clips of an animation library are exported from their own keys,
    which is five times faster and the same to 0.002 (see docs/LEARNINGS.md)."""
    for action in bpy.data.actions:
        action.use_fake_user = True
    out_path.parent.mkdir(parents=True, exist_ok=True)
    bpy.ops.export_scene.gltf(
        filepath=str(out_path),
        export_format="GLB",
        export_yup=True,
        export_animations=True,
        export_animation_mode="ACTIONS",
        export_force_sampling=sample_every_frame,
        export_morph=True,
        export_morph_normal=True,
        export_skins=True,
        export_all_influences=False,
        export_draco_mesh_compression_enable=False,
    )


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


# --- outfit models -----------------------------------------------------------------
#
# An outfit model is a set of skinned meshes made for the same skeleton as its body
# (assets/outfit_models/<body>/<outfit>.glb; the modular outfit packs are built that
# way). It is worn by re-binding its meshes to the body's armature, so they follow
# every pose the body does, and by deleting the part of the body the outfit replaces:
# left in, the body clips through the cloth. The head and neck stay; the outfit
# supplies the rest, hands and feet included.


# --- per-model pose fixes ----------------------------------------------------------
#
# A model may come with a hand that faces the wrong way for the poses we give it (Miss
# Minutes' fist has its fingers turned outward, so an arm that bends reads as folding the
# wrong way). `<model>.pose.json` beside it says which bones to turn about their own length,
# in degrees: {"roll_bones": {"hand.L_04": 180}}. It is applied to the rest pose, before the
# arms are relaxed, so everything after starts from the corrected hand.


# --- relaxed arms -------------------------------------------------------------------
#
# Character bases are modelled in a T-pose, arms straight out, and that stays their rest
# pose: the avatar would stand with its arms out through every idle. Arms held out are the
# wrong starting point for the gestures too, which swing a limb sideways from hanging (see
# bake_limb_morphs). So an arm held well off hanging (RELAX_IF_HANGING_LESS_THAN) is brought down to
# RELAXED_ARM_DEG off vertical, and that becomes the rest pose; gestures and the running
# swing then start from, and return to, a natural stance.

# vertical) is brought down: a T-pose arm is at 0, a raised or bent arm at about 0.6; the mechs' (0.8+) are not


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
        ) as exc:  # noqa: BLE001 -- an unusable asset must not fail the build
            warnings.append(
                f"could not use model {resolution['path'].name} ({exc}); building procedurally instead"
            )
            reset_scene()
            bpy.context.scene.render.fps = 30
    if imported is not None:
        tinted = tint_hair(imported["meshes"], palette.get("hair", "#ffffff"))
        if tinted:
            warnings.append(
                f"tinted {tinted} hair material(s) {palette['hair']}"
            )

    run_keys: list[str] = []
    library_plan: dict[str, tuple[Path, str]] = {}
    library_clips: dict[str, str] = {}
    fit_scale = (
        1.0  # how big the overlay draws the body (see fit_clips_to_the_frame)
    )
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
        if (
            has_face
            and not viseme_keys
            and _face_sidecar(resolution["path"]) is None
        ):
            warnings.append(
                "model has no viseme shape keys; building it without a face (sound_waves speech indicator)"
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

        # bound with automatic weights (no bone for their socket)
        accessory_objs = []
        bound_accessories: list = (
            []
        )  # (object, bone): rigidly attached to the socket's bone
        skinned_accessories: list = []  # skinned like the body (hair, a beard)
        socket_bones = (
            find_socket_bones([b.name for b in imported["armature"].data.bones])
            if imported["armature"] is not None
            else canonical_socket_bones()
        )
        frames = imported_socket_frames(imported, socket_bones)
        for frame_ in frames.values():
            frame_["body_scale"] = imported["import_scale"]
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
            elif (
                imported["armature"] is not None
                and accessory_skin(DEPLOY_DIR, socket_name, item) == "body"
            ):
                skinned_accessories += built
            else:
                bound_accessories += [
                    (obj, socket_bones[socket_name]) for obj in built
                ]

        arm_obj = imported["armature"]
        meshes = accessory_objs
        if (
            arm_obj is None
        ):  # a static model: give it the canonical rig and bind it
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
        bind_like_body(arm_obj, skinned_accessories, imported["body"])
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
                _face_bone(imported["meshes"], centre, imported["armature"])
                if imported["armature"] is not None
                else "head"
            )
            eyes, _, viseme_keys = build_face_overlay(
                face_map, imported["meshes"], lo, hi, arm_obj, bone
            )
            has_face = True
    else:
        layout = None
        if base_template in (
            "standard_humanoid_female",
            "standard_humanoid_male",
        ):
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
            body,
            wardrobe.get("outfit", "none"),
            head_loc,
            palette,
            surface_material,
        )
        footwear_objs = build_footwear(
            wardrobe.get("footwear", "none"), palette
        )

        accessory_objs = []
        for socket_name in _SOCKET_NAMES:
            item = accessories.get(socket_name)
            if item:
                accessory_objs += build_socket_accessory(
                    socket_name, item, head_loc, palette, DEPLOY_DIR
                )

        if has_legs and layout is not None:
            run_keys = add_run_shape_keys(
                [body, *outfit_objs, *footwear_objs], layout
            )
        arm_obj = build_armature(
            has_arms, head_loc, has_legs=has_legs, layout=layout
        )
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
    if (
        library_plan
    ):  # real clips move the limbs: no morph-target poses to double them
        library_clips = wear_animation_library(
            arm_obj,
            library_plan,
            warnings,
            arm_spread=arm_spread(rig_family_of(resolution["path"])),
        )
        fit_scale = fit_clips_to_the_frame(arm_obj, library_clips, warnings)
        if library_clips.get("sit_idle") in chair_sit_clips():
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

    speech_indicator_mode = "viseme_shapekeys" if has_face else "sound_waves"

    skeletal = False
    skeletal_clips: list[str] = []
    if (
        imported is not None
        and imported["armature"] is not None
        and not library_clips
    ):
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
    if fit_scale < 1.0:
        bounds["fit_scale"] = round(fit_scale, 3)
    export_glb(OUT_PATH, sample_every_frame=bool(skeletal) or not library_clips)
    brighten_emissive(
        OUT_PATH,
        (
            _pose_sidecar(resolution["path"]).get("emissive_gain", 1.0)
            if resolution.get("path")
            else 1.0
        ),
    )

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
