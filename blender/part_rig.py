"""Rigs a model that is made of separate rigid parts, with a Quaternius-named skeleton. Run headless,
never imported:

    blender --background --factory-startup --python blender/part_rig.py -- <in.glb> <out.glb> [--yaw DEG] [--arm-dx F] [--limbless] [--split-loose]

Many cartoon and mech models have no skeleton at all but are built from separate pieces: a head, a
torso, limbs of several pieces. This finds which piece is what from where it sits (the figure is
turned `--yaw` degrees about Z first, to face -Y as the Quaternius characters do, with its left on +X),
puts the limbs into the T-pose and the standing legs the animation libraries are measured from (each
chain turns rigidly about its joints), builds the Quaternius skeleton the body has (root, pelvis,
three spine bones, neck, head, and per side a clavicle, arm, hand, thigh, calf and foot), and binds
every piece whole to one bone. The result stands on the ground, centred, and is a body that
`rig_convert.py clips` can retarget the libraries onto (docs/ADR_ACCESSORY_MODELS.md, decision 23).

A piece is classified by its centre, as fractions of the figure's height:

* the head: in the top fifth and near the centre line;
* the torso: above 55 % of the height and within `--arm-dx` of the centre line;
* an arm: above 30 % of the height and further out than `--arm-dx` (the default 0.14 suits a figure whose
  arms stand out; 0.09 suits one whose arms hang close to the body);
* a leg: the rest, by side.

A piece that would be the torso but sits well in front of or behind the rest of it (a hand held out in front of the
body) goes to the arm on its side. An arm's shoulder is where it comes nearest the chest's corner on its side.

Each limb is cut in three where it bends (an arm at the point furthest from the line between its
shoulder and its hand; a leg at its middle) and where its last part begins (hand, foot).

`--split-loose` first cuts each mesh into its loose parts (pieces that share no vertex), for a model whose
parts were exported as one mesh.

`--limbless` is for a figure with no arms or legs (a robot on wheels): the head (above 60 % of the height),
the neck (40 to 60 %) and the body (the rest, wheels included) are told apart, the body goes to the pelvis,
and the arm and leg bones are still made, at the size a figure of that height would have them, with
nothing bound to them: the libraries need the bones, and their head and torso motion plays.
"""

import math
import sys

import bpy  # pyright: ignore[reportMissingImports]
import numpy as np
from mathutils import (
    Matrix,
    Quaternion,
    Vector,
)  # pyright: ignore[reportMissingImports]

DEFAULT_ARM_DX = 0.14
HEAD_TOP_FRACTION = 0.82
TORSO_FROM_FRACTION = 0.55
ARM_FROM_FRACTION = 0.30
HEAD_HALF_WIDTH = 0.12
STRAY_TORSO_DEPTH = 0.15
LIMBLESS_HEAD_FROM = 0.60
LIMBLESS_NECK_FROM = 0.40
SPINE_BANDS = (
    ("pelvis", -1e9, 0.18),
    ("spine_01", 0.18, 0.45),
    ("spine_02", 0.45, 0.72),
    ("spine_03", 0.72, 1e9),
)


def _argument(flag: str, default: float) -> float:
    if flag in sys.argv:
        return float(sys.argv[sys.argv.index(flag) + 1])
    return default


class Part:
    """One mesh of the model: its vertices in the canonical frame, its centre, what it is."""

    def __init__(self, obj, to_canonical: Matrix):
        self.obj = obj
        n = len(obj.data.vertices)
        flat = np.empty(n * 3, dtype=np.float64)
        obj.data.vertices.foreach_get("co", flat)
        m = np.array(to_canonical @ obj.matrix_world)
        self.v = flat.reshape(n, 3) @ m[:3, :3].T + m[:3, 3]
        self.c = self.v.mean(axis=0)
        self.cls = ""
        self.bone = ""


def rot_between(a, b) -> Quaternion:
    """The rotation that turns direction `a` onto `b` (none if either has no direction)."""
    a, b = Vector([float(x) for x in a]), Vector([float(x) for x in b])
    if a.length < 1e-6 or b.length < 1e-6:
        return Quaternion()
    return a.normalized().rotation_difference(b.normalized())


def _distance_to_segment(point, a, b) -> float:
    """How far `point` is from the line segment a-b."""
    ab = np.asarray(b, dtype=float) - np.asarray(a, dtype=float)
    t = float(np.clip((point - a) @ ab / max(float(ab @ ab), 1e-12), 0.0, 1.0))
    return float(np.linalg.norm(point - (a + t * ab)))


def rotate_parts(parts, pivot, q: Quaternion) -> np.ndarray:
    """Turns `parts` about `pivot` by `q`; returns the rotation matrix."""
    m = np.array(q.to_matrix())
    for p in parts:
        p.v = (p.v - pivot) @ m.T + pivot
        p.c = (p.c - pivot) @ m.T + pivot
    return m


def classify(
    parts, zlo: float, height: float, xm: float, arm_dx: float
) -> None:
    for p in parts:
        t = (p.c[2] - zlo) / height
        dx = (p.c[0] - xm) / height
        if t > HEAD_TOP_FRACTION and abs(dx) < HEAD_HALF_WIDTH:
            p.cls = "head"
        elif t > TORSO_FROM_FRACTION and abs(dx) <= arm_dx:
            p.cls = "torso"
        elif t > ARM_FROM_FRACTION and dx > arm_dx:
            p.cls = "armL"
        elif t > ARM_FROM_FRACTION and dx < -arm_dx:
            p.cls = "armR"
        elif t <= TORSO_FROM_FRACTION and dx > 0.02:
            p.cls = "legL"
        elif t <= TORSO_FROM_FRACTION and dx < -0.02:
            p.cls = "legR"
        else:
            p.cls = "torso"


def rig_limbless(parts, zlo: float, height: float, xm: float):
    """The skeleton of a figure with no arms or legs: head, neck and body told apart by height, the body
    (wheels and all) on the pelvis, the limb bones made at the size a figure of this height has them and
    left empty. Returns (bones, lowest point, centre line y)."""
    for p in parts:
        t = (p.c[2] - zlo) / height
        p.cls = (
            "head"
            if t > LIMBLESS_HEAD_FROM
            else ("neck" if t > LIMBLESS_NECK_FROM else "body")
        )
    for cls in ("head", "body"):
        if not any(p.cls == cls for p in parts):
            raise SystemExit(
                f"no {cls} found: the parts do not stand as a head above a body"
            )
    for p in parts:
        p.bone = {"head": "Head", "neck": "neck_01", "body": "pelvis"}[p.cls]
    allv = np.vstack([p.v for p in parts])
    body_v = np.vstack([p.v for p in parts if p.cls == "body"])
    head_v = np.vstack([p.v for p in parts if p.cls == "head"])
    ground = float(allv[:, 2].min())
    ym = float(np.median(body_v[:, 1]))
    top = float(allv[:, 2].max())
    fig = top - ground
    body_lo, body_hi = float(body_v[:, 2].min()), float(body_v[:, 2].max())
    pelvis_z = body_lo + 0.45 * (body_hi - body_lo)
    shoulder_z = body_hi - 0.15 * (body_hi - body_lo)
    neck_z = float(head_v[:, 2].min())
    half_w = float((body_v[:, 0].max() - body_v[:, 0].min()) / 2)
    bones: dict[str, tuple[str | None, np.ndarray, np.ndarray]] = {}

    def bone(name, parent, head, tail):
        bones[name] = (parent, np.array(head, float), np.array(tail, float))

    third = (shoulder_z - pelvis_z) / 3
    bone("root", None, [xm, ym, ground], [xm, ym, ground + 0.05 * fig])
    bone("pelvis", "root", [xm, ym, pelvis_z], [xm, ym, pelvis_z + third])
    bone(
        "spine_01",
        "pelvis",
        [xm, ym, pelvis_z + third],
        [xm, ym, pelvis_z + 2 * third],
    )
    bone(
        "spine_02",
        "spine_01",
        [xm, ym, pelvis_z + 2 * third],
        [xm, ym, shoulder_z],
    )
    bone("spine_03", "spine_02", [xm, ym, shoulder_z], [xm, ym, neck_z])
    bone("neck_01", "spine_03", [xm, ym, neck_z], [xm, ym, neck_z + 0.01 * fig])
    bone("Head", "neck_01", [xm, ym, neck_z + 0.01 * fig], [xm, ym, top])
    arm = 0.28 * fig
    for side in "lr":
        sfx, sign = "_" + side, (1 if side == "l" else -1)
        shoulder = [xm + sign * half_w, ym, shoulder_z]
        bone(
            "clavicle" + sfx,
            "spine_03",
            [xm + sign * 0.01, ym, shoulder_z],
            shoulder,
        )
        bone(
            "upperarm" + sfx,
            "clavicle" + sfx,
            shoulder,
            [shoulder[0] + sign * 0.45 * arm, ym, shoulder_z],
        )
        bone(
            "lowerarm" + sfx,
            "upperarm" + sfx,
            bones["upperarm" + sfx][2],
            [shoulder[0] + sign * 0.85 * arm, ym, shoulder_z],
        )
        bone(
            "hand" + sfx,
            "lowerarm" + sfx,
            bones["lowerarm" + sfx][2],
            [shoulder[0] + sign * arm, ym, shoulder_z],
        )
        hip = [xm + sign * 0.25 * half_w, ym, pelvis_z]
        leg = pelvis_z - ground
        bone("thigh" + sfx, "pelvis", hip, [hip[0], ym, pelvis_z - 0.5 * leg])
        bone(
            "calf" + sfx,
            "thigh" + sfx,
            bones["thigh" + sfx][2],
            [hip[0], ym, ground + 0.08 * leg],
        )
        bone(
            "foot" + sfx,
            "calf" + sfx,
            bones["calf" + sfx][2],
            [hip[0], ym - 0.15 * leg, ground + 0.02 * leg],
        )
    return bones, ground, ym


def finish(
    parts, bones, xm: float, ym: float, min_foot: float, out: str
) -> None:
    """Stands the figure on the ground (its lowest foot at 0), centred, builds the armature, binds
    every part whole to its bone (one mesh per bone) and exports."""
    shift = np.array([-xm, -ym, -min_foot])
    for p in parts:
        p.v = p.v + shift
    for name, (parent, head, tail) in list(bones.items()):
        bones[name] = (parent, head + shift, tail + shift)

    armature_data = bpy.data.armatures.new("Armature")
    armature = bpy.data.objects.new("Armature", armature_data)
    bpy.context.scene.collection.objects.link(armature)
    bpy.context.view_layer.objects.active = armature
    bpy.ops.object.mode_set(mode="EDIT")
    for name, (parent, head, tail) in bones.items():
        edit_bone = armature_data.edit_bones.new(name)
        edit_bone.head, edit_bone.tail = Vector(head), Vector(tail)
        if (edit_bone.tail - edit_bone.head).length < 1e-4:
            edit_bone.tail = edit_bone.head + Vector((0, 0, 0.01))
    for name, (parent, head, tail) in bones.items():
        if parent:
            armature_data.edit_bones[name].parent = armature_data.edit_bones[
                parent
            ]
    bpy.ops.object.mode_set(mode="OBJECT")

    # --- one mesh per bone, every vertex wholly on it -----------------------------------------------------------
    by_bone: dict[str, list[Part]] = {}
    for p in parts:
        by_bone.setdefault(p.bone, []).append(p)
    for name, members in by_bone.items():
        coords: list[tuple[float, float, float]] = []
        faces: list[tuple[list[int], int]] = []
        materials: list = []
        uvs: list[np.ndarray] = []
        offset = 0
        for p in members:
            mesh = p.obj.data
            uv = np.zeros(len(mesh.loops) * 2)
            if mesh.uv_layers.active is not None:
                mesh.uv_layers.active.data.foreach_get("uv", uv)
            uvs.append(uv)
            coords.extend(tuple(v) for v in p.v)
            for m in mesh.materials:
                if m not in materials:
                    materials.append(m)
            for poly in mesh.polygons:
                slot = (
                    mesh.materials[poly.material_index]
                    if poly.material_index < len(mesh.materials)
                    else None
                )
                faces.append(
                    (
                        [offset + i for i in poly.vertices],
                        materials.index(slot) if slot in materials else 0,
                    )
                )
            offset += len(p.v)
        new_mesh = bpy.data.meshes.new(name + "_mesh")
        new_mesh.from_pydata(coords, [], [f for f, _ in faces])
        for m in materials:
            new_mesh.materials.append(m)
        for poly, (_, slot) in zip(new_mesh.polygons, faces):
            poly.material_index = slot
            poly.use_smooth = True
        if any(
            len(u) for u in uvs
        ):  # a textured part keeps its texture coordinates
            new_mesh.uv_layers.new(name="UVMap").data.foreach_set(
                "uv", np.concatenate(uvs)
            )
        new_mesh.update()
        obj = bpy.data.objects.new(name, new_mesh)
        bpy.context.scene.collection.objects.link(obj)
        obj.vertex_groups.new(name=name).add(
            list(range(len(coords))), 1.0, "REPLACE"
        )
        obj.parent = armature
        obj.modifiers.new("Armature", "ARMATURE").object = (
            armature  # pyright: ignore[reportAttributeAccessIssue]
        )
    for p in parts:
        bpy.data.objects.remove(p.obj)
    bpy.ops.export_scene.gltf(
        filepath=out,
        export_format="GLB",
        export_skins=True,
        export_animations=False,
    )
    print("PART_RIG_OK", len(parts), "parts", len(bones), "bones")


def split_loose() -> None:
    """Cuts every mesh into its loose parts, which keep their materials and texture coordinates."""
    for obj in [o for o in bpy.data.objects if o.type == "MESH"]:
        bpy.ops.object.select_all(action="DESELECT")
        obj.select_set(True)
        bpy.context.view_layer.objects.active = obj
        bpy.ops.object.mode_set(mode="EDIT")
        bpy.ops.mesh.select_all(action="SELECT")
        bpy.ops.mesh.remove_doubles(
            threshold=1e-5
        )  # a flat-shaded export gives each face its own vertices
        bpy.ops.mesh.separate(type="LOOSE")
        bpy.ops.object.mode_set(mode="OBJECT")


def main() -> None:
    src, out = sys.argv[sys.argv.index("--") + 1 : sys.argv.index("--") + 3]
    yaw = _argument("--yaw", 0.0)
    arm_dx = _argument("--arm-dx", DEFAULT_ARM_DX)
    for o in list(bpy.data.objects):
        bpy.data.objects.remove(o)
    bpy.ops.import_scene.gltf(filepath=src)
    if "--split-loose" in sys.argv:
        split_loose()
    to_canonical = Matrix.Rotation(math.radians(yaw), 4, "Z")

    parts = [
        Part(o, to_canonical)
        for o in bpy.data.objects
        if o.type == "MESH" and len(o.data.vertices) > 0
    ]
    if len(parts) < 6:
        raise SystemExit(
            f"{len(parts)} separate parts: too few to tell a head, torso, arms and legs apart"
        )
    centres_z = sorted(p.c[2] for p in parts)
    zlo, zhi = (
        centres_z[1],
        centres_z[-2],
    )  # the second lowest and highest: a stray part does not set the height
    height = zhi - zlo
    xm = float(np.median([p.c[0] for p in parts]))
    if "--limbless" in sys.argv:
        limbless_bones, limbless_foot, limbless_ym = rig_limbless(
            parts, zlo, height, xm
        )
        finish(parts, limbless_bones, xm, limbless_ym, limbless_foot, out)
        return
    classify(parts, zlo, height, xm, arm_dx)
    # a piece of a hand held out in front of the body sits near the centre line, so it was taken for part
    # of the torso: a torso piece far from the rest of the torso (front to back) goes to the arm on its side
    core_y = float(
        np.median([p.c[1] for p in parts if p.cls in ("head", "torso")])
    )
    for p in parts:
        if (
            p.cls == "torso"
            and abs(p.c[1] - core_y) > STRAY_TORSO_DEPTH * height
        ):
            p.cls = "armL" if p.c[0] > xm else "armR"

    def of(cls):
        return [p for p in parts if p.cls == cls]

    def verts(cls):
        return np.vstack([p.v for p in of(cls)])

    for cls in ("head", "torso", "armL", "armR", "legL", "legR"):
        if not of(cls):
            raise SystemExit(f"no {cls} found: try another --yaw or --arm-dx")
    torso_v, head_v = verts("torso"), verts("head")
    ym = float(np.median(torso_v[:, 1]))
    ground = min(verts("legL")[:, 2].min(), verts("legR")[:, 2].min())

    def leg_joints(side):
        v = verts("leg" + side)
        top = v[:, 2].max()
        upper = v[v[:, 2] > top - 0.08 * height]
        hip = np.array(
            [upper[:, 0].mean(), upper[:, 1].mean(), top - 0.02 * height]
        )
        foot_top = ground + 0.14 * height
        feet = [p for p in of("leg" + side) if p.c[2] < foot_top]
        foot_v = (
            np.vstack([p.v for p in feet]) if feet else v[v[:, 2] < foot_top]
        )
        low = v[v[:, 2] < foot_top + 0.03 * height]
        ankle = np.array(
            [foot_v[:, 0].mean(), low[:, 1].mean(), foot_top - 0.03 * height]
        )
        mid = (hip[2] + ankle[2]) / 2
        band = v[abs(v[:, 2] - mid) < 0.05 * height]
        if not len(
            band
        ):  # a very coarse mesh has no vertex in the slice: the nearest few
            band = v[np.argsort(abs(v[:, 2] - mid))[:8]]
        knee = np.array([band[:, 0].mean(), band[:, 1].mean(), mid])
        return hip, knee, ankle, foot_v[:, 1].min()  # forward is -Y

    def arm_joints(side):
        v = verts("arm" + side)
        # the shoulder: the arm's vertices nearest the chest's corner on its side. (Not those nearest the
        # centre line: an arm bent forward has its hand there, and the shoulder was then put at the
        # hand's height, which left the shoulder piece to the forearm.)
        chest = torso_v[torso_v[:, 2] > torso_v[:, 2].max() - 0.15 * height]
        edge = chest[:, 0].max() if side == "L" else chest[:, 0].min()
        corner = np.array(
            [edge, chest[:, 1].mean(), torso_v[:, 2].max() - 0.08 * height]
        )
        near_corner = np.linalg.norm(v - corner, axis=1)
        shoulder = v[near_corner < near_corner.min() + 0.04 * height].mean(
            axis=0
        )
        dist = np.linalg.norm(v - shoulder, axis=1)
        length = dist.max()
        tip = v[dist.argmax()]
        line = (tip - shoulder) / np.linalg.norm(tip - shoulder)
        best, f_elbow, elbow = (
            0.0,
            0.5,
            None,
        )  # the elbow: where the arm bends furthest from the line
        for f in np.arange(0.2, 0.81, 0.05):
            near = v[abs(dist / length - f) < 0.04]
            if not len(near):
                continue
            centre = near.mean(axis=0)
            off = centre - shoulder
            deviation = np.linalg.norm(off - (off @ line) * line)
            if deviation > best:
                best, f_elbow, elbow = deviation, float(f), centre
        if best < 0.06 * length or elbow is None:  # straight enough: halfway
            f_elbow = 0.5
            near = v[abs(dist / length - 0.5) < 0.05]
            elbow = (
                near.mean(axis=0)
                if len(near)
                else shoulder + (tip - shoulder) / 2
            )
        f_wrist = f_elbow + 0.7 * (1.0 - f_elbow)
        near = v[abs(dist / length - f_wrist) < 0.05]
        wrist = (
            near.mean(axis=0)
            if len(near)
            else shoulder + (tip - shoulder) * f_wrist
        )
        return shoulder, elbow, wrist, tip

    hip_l, knee_l, ankle_l, toe_l = leg_joints("L")
    hip_r, knee_r, ankle_r, toe_r = leg_joints("R")
    legs = {
        "L": (hip_l, knee_l, ankle_l, toe_l),
        "R": (hip_r, knee_r, ankle_r, toe_r),
    }
    pelvis_z = (hip_l[2] + hip_r[2]) / 2
    spine_top = torso_v[:, 2].max()
    span = spine_top - pelvis_z

    # --- which bone each part follows ---------------------------------------------------------------
    for p in of("torso"):
        for name, lo, hi in SPINE_BANDS:
            if pelvis_z + lo * span <= p.c[2] < pelvis_z + hi * span:
                p.bone = name
        if not p.bone:
            p.bone = "spine_03"
    for p in of("head"):
        p.bone = "Head"
    arms = {}
    for side in "LR":
        sfx = "_" + side.lower()
        arms[side] = arm_joints(side)
        chain = list(zip(arms[side], arms[side][1:]))
        for p in of("arm" + side):
            # the segment of the arm the piece lies nearest: (not its distance from the shoulder, which
            # does not grow along an arm that bends back towards the body)
            nearest = min(
                range(3), key=lambda i: _distance_to_segment(p.c, *chain[i])
            )
            p.bone = ("upperarm", "lowerarm", "hand")[nearest] + sfx
        hip, knee, ankle, _ = legs[side]
        for p in of("leg" + side):
            z = p.c[2]
            p.bone = (
                "foot" + sfx
                if z < ankle[2] + 0.02 * height
                else ("calf" + sfx if z < knee[2] else "thigh" + sfx)
            )

    def following(*names):
        return [p for p in parts if p.bone in names]

    # --- the T-pose and the standing legs: each chain turns rigidly about its joints ----------------------
    joints = {}
    for side in "LR":
        sfx = "_" + side.lower()
        sign = 1 if side == "L" else -1
        sh, el, wr, tip = [np.array(x, dtype=float) for x in arms[side]]
        m1 = rotate_parts(
            following("upperarm" + sfx, "lowerarm" + sfx, "hand" + sfx),
            sh,
            rot_between(el - sh, (sign, 0, 0)),
        )
        el2, wr2, tip2 = ((x - sh) @ m1.T + sh for x in (el, wr, tip))
        m2 = rotate_parts(
            following("lowerarm" + sfx, "hand" + sfx),
            el2,
            rot_between(wr2 - el2, (sign, 0, 0)),
        )
        wr3, tip3 = ((x - el2) @ m2.T + el2 for x in (wr2, tip2))
        joints["arm" + side] = (sh, el2, wr3, tip3)

        hip, knee, ankle, toe_y = legs[side]
        q1 = rot_between(knee - hip, (0, 0, -1))
        m1 = rotate_parts(
            following("thigh" + sfx, "calf" + sfx, "foot" + sfx), hip, q1
        )
        knee2, ankle2 = ((x - hip) @ m1.T + hip for x in (knee, ankle))
        q2 = rot_between(ankle2 - knee2, (0, 0, -1))
        m2 = rotate_parts(following("calf" + sfx, "foot" + sfx), knee2, q2)
        ankle3 = (ankle2 - knee2) @ m2.T + knee2
        rotate_parts(
            following("foot" + sfx), ankle3, (q2 @ q1).inverted()
        )  # the foot stays as it stood
        joints["leg" + side] = (hip, knee2, ankle3, toe_y)

    # --- the skeleton -----------------------------------------------------------------------------------
    neck_z, head_top = head_v[:, 2].min(), head_v[:, 2].max()
    bones: dict[str, tuple[str | None, np.ndarray, np.ndarray]] = {}

    def bone(name, parent, head, tail):
        bones[name] = (parent, np.array(head, float), np.array(tail, float))

    third = span / 3
    bone("root", None, [xm, ym, ground], [xm, ym, ground + 0.05 * height])
    bone("pelvis", "root", [xm, ym, pelvis_z], [xm, ym, pelvis_z + third])
    bone(
        "spine_01",
        "pelvis",
        [xm, ym, pelvis_z + third],
        [xm, ym, pelvis_z + 2 * third],
    )
    bone(
        "spine_02",
        "spine_01",
        [xm, ym, pelvis_z + 2 * third],
        [xm, ym, spine_top - 0.01 * span],
    )
    bone(
        "spine_03",
        "spine_02",
        [xm, ym, spine_top - 0.01 * span],
        [xm, ym, neck_z],
    )
    bone(
        "neck_01",
        "spine_03",
        [xm, ym, neck_z],
        [xm, ym, neck_z + 0.01 * height],
    )
    bone(
        "Head", "neck_01", [xm, ym, neck_z + 0.01 * height], [xm, ym, head_top]
    )
    for side in "LR":
        sfx = "_" + side.lower()
        sign = 1 if side == "L" else -1
        sh, el, wr, tip = joints["arm" + side]
        bone("clavicle" + sfx, "spine_03", [xm + sign * 0.01, sh[1], sh[2]], sh)
        bone("upperarm" + sfx, "clavicle" + sfx, sh, el)
        bone("lowerarm" + sfx, "upperarm" + sfx, el, wr)
        bone("hand" + sfx, "lowerarm" + sfx, wr, tip)
        hip, knee, ankle, toe_y = joints["leg" + side]
        bone("thigh" + sfx, "pelvis", hip, knee)
        bone("calf" + sfx, "thigh" + sfx, knee, ankle)
        bone(
            "foot" + sfx,
            "calf" + sfx,
            ankle,
            [
                ankle[0],
                ankle[1] - max(ankle[1] - toe_y, 0.02 * height),
                ankle[2] - (ankle[2] - ground) * 0.9,
            ],
        )

    min_foot = min(p.v[:, 2].min() for p in following("foot_l", "foot_r"))
    finish(parts, bones, xm, ym, min_foot, out)


if __name__ == "__main__":
    main()
