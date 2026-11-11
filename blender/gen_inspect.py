"""
`--inspect` mode: what a model's rig can carry (capabilities, sockets, face).
"""

from __future__ import annotations

import json
import os
from itertools import pairwise
from pathlib import Path

import bpy  # pyright: ignore[reportMissingImports]

from blender.gen_helpers import (
    count_triangles,
    reset_scene,
)
from blender.gen_imported import (
    _find_eyes,
    _front_surface_y,
    build_imported_model,
)
from blender.sockets import canonical_socket_bones, find_socket_bones
from blender.viseme_shapekeys import (
    SHAPE_KEY_PREFIX,
    VISEME_CODES,
)

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
    counts = {
        m.name: sum(len(p.vertices) - 2 for p in m.data.polygons)
        for m in meshes
    }
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


def run_face_landmarks(in_path: Path, out_path: Path) -> None:
    """Finds where a model's eyes and mouth are and writes the face sidecar for
    them (see "face overlay" above), as fractions of the model's box. The eyes are
    the mesh whose material or texture is named for them; the mouth is the crease
    between the lips, found in the face's side profile (the lips are two bulges,
    and the crease is the deepest point between them). For a realistic face with
    its own lips, so the sidecar draws the mouth over them rather than hiding them.
    """
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
    def _jumps_back(i: int) -> bool:
        here, ahead = ys[i], ys[i + 5]
        return here is not None and ahead is not None and ahead - here > 0.04

    chin = next((i for i in range(len(ys) - 5) if _jumps_back(i)), len(ys) - 1)
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
    # (index, "min" | "max"): where the profile changes direction (y grows going back)
    extrema = []
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
    for (low, kind_low), (high, kind_high) in pairwise(extrema):
        if (
            kind_low == "min"
            and kind_high == "max"
            and smooth[high] - smooth[low] >= 0.001
        ):
            crease = high
            break
    chin_z = window[-1][0] if window else eye_z - 0.2

    def along(z: float) -> float:  # 0 at the eye line, 1 at the chin
        return (eye_z - z) / max(eye_z - chin_z, 1e-6)

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
            image.scale(
                max(1, round(width * ratio)), max(1, round(height * ratio))
            )
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
        with bpy.data.libraries.load(str(in_path), link=False) as (
            data_from,
            data_to,
        ):
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
        find_socket_bones(bone_names)
        if bone_names
        else canonical_socket_bones()
    )
    declared = [
        obj.name
        for obj in bpy.context.scene.objects
        if obj.type == "EMPTY" and obj.name in _SOCKET_NAMES
    ]
    supported_sockets = [
        name
        for name in _SOCKET_NAMES
        if name in socket_bones or name in declared
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
