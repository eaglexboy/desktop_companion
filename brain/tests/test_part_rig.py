"""blender/part_rig.py rigs a model made of separate rigid parts, and `models rig-body` installs it.

The Blender tests are skipped if `blender` is not on PATH; the validation ones need nothing.
"""

from __future__ import annotations

import json
import shutil
import struct
import subprocess
from pathlib import Path

import pytest

from brain.appearance import models

REPO_ROOT = Path(__file__).resolve().parents[2]
needs_blender = pytest.mark.skipif(
    shutil.which("blender") is None, reason="blender binary not found on PATH"
)

# A stick figure of boxes, 2 m tall, facing -Y, its left on +X: a head, two torso pieces, three pieces
# to each arm (arms stretched out) and three to each leg.
FIGURE = """
import bpy, sys
for o in list(bpy.data.objects):
    bpy.data.objects.remove(o)
def box(name, x, z, sx, sz):
    bpy.ops.mesh.primitive_cube_add(size=1.0, location=(x, 0, z))
    o = bpy.context.active_object
    o.name = name
    o.scale = (sx, 0.2, sz)
    bpy.ops.object.transform_apply(scale=True)
box("head", 0, 1.85, 0.3, 0.3)
box("chest", 0, 1.5, 0.6, 0.35)
box("belly", 0, 1.15, 0.5, 0.3)
for s, sg in (("l", 1), ("r", -1)):
    for i, x in enumerate((0.55, 0.95, 1.35)):
        box(f"arm_{s}{i}", sg * x, 1.55, 0.35, 0.12)
    for i, z in enumerate((0.85, 0.5, 0.1)):
        box(f"leg_{s}{i}", sg * 0.15, z, 0.18, 0.3)
bpy.ops.export_scene.gltf(filepath=sys.argv[-1], export_format="GLB")
"""


def _gltf(path: Path) -> dict:
    data = path.read_bytes()
    return json.loads(data[20 : 20 + struct.unpack("<I", data[12:16])[0]])


def _blender(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["blender", "--background", "--factory-startup", *args],
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )


@pytest.fixture
def figure(tmp_path) -> Path:
    script = tmp_path / "figure.py"
    script.write_text(FIGURE)
    path = tmp_path / "figure.glb"
    proc = _blender("--python", str(script), "--", str(path))
    assert path.is_file(), proc.stderr + proc.stdout
    return path


@needs_blender
def test_part_rig_gives_a_figure_of_parts_the_quaternius_skeleton(
    figure, tmp_path
):
    out = tmp_path / "rigged.glb"
    proc = _blender(
        "--python",
        str(REPO_ROOT / "blender" / "part_rig.py"),
        "--",
        str(figure),
        str(out),
    )
    assert "PART_RIG_OK" in proc.stdout, proc.stderr + proc.stdout
    gltf = _gltf(out)
    names = {n["name"] for n in gltf["nodes"]}
    for bone in (
        "root",
        "pelvis",
        "spine_01",
        "spine_02",
        "spine_03",
        "neck_01",
        "Head",
        "clavicle_l",
        "upperarm_l",
        "lowerarm_l",
        "hand_l",
        "thigh_l",
        "calf_l",
        "foot_l",
        "clavicle_r",
        "upperarm_r",
        "lowerarm_r",
        "hand_r",
        "thigh_r",
        "calf_r",
        "foot_r",
    ):
        assert bone in names, bone
    assert len(gltf["skins"]) >= 1 and all(
        "JOINTS_0" in p["attributes"]
        for m in gltf["meshes"]
        for p in m["primitives"]
    )


# The same figure with its right arm hanging, the forearm and a hand held out in front of the body: the
# hand is near the centre line (a torso piece by position alone), and the arm's piece nearest the centre
# line is the hand, not the shoulder.
BENT_ARM_FIGURE = FIGURE.split("for s, sg in")[0] + """
def block(name, x, y, z, sx, sy, sz):
    bpy.ops.mesh.primitive_cube_add(size=1.0, location=(x, y, z))
    o = bpy.context.active_object
    o.name = name
    o.scale = (sx, sy, sz)
    bpy.ops.object.transform_apply(scale=True)
for i, x in enumerate((0.55, 0.95, 1.35)):
    box(f"arm_l{i}", x, 1.55, 0.35, 0.12)
block("shoulder_r", -0.5, 0, 1.72, 0.2, 0.2, 0.12)
block("upperarm_r", -0.6, 0, 1.4, 0.15, 0.2, 0.5)
block("forearm_r", -0.6, -0.5, 1.1, 0.15, 0.7, 0.15)
block("hand_r", -0.12, -1.0, 1.1, 0.2, 0.2, 0.2)
for s, sg in (("l", 1), ("r", -1)):
    for i, z in enumerate((0.85, 0.5, 0.1)):
        box(f"leg_{s}{i}", sg * 0.15, z, 0.18, 0.3)
bpy.ops.export_scene.gltf(filepath=sys.argv[-1], export_format="GLB")
"""


def _depth_of_the_body(gltf: dict) -> float:
    """How far front to back the meshes bound to the head, spine and pelvis reach together."""
    low, high = [], []
    for node in gltf["nodes"]:
        if node.get("name") in (
            "Head",
            "pelvis",
            "spine_01",
            "spine_02",
            "spine_03",
        ):
            if "mesh" not in node:
                continue
            for prim in gltf["meshes"][node["mesh"]]["primitives"]:
                accessor = gltf["accessors"][prim["attributes"]["POSITION"]]
                low.append(accessor["min"][2])
                high.append(accessor["max"][2])
    return max(high) - min(low)


@needs_blender
def test_part_rig_an_arm_bent_forward_leaves_no_piece_of_it_on_the_torso(
    tmp_path,
):
    script = tmp_path / "bent.py"
    script.write_text(BENT_ARM_FIGURE)
    source = tmp_path / "bent.glb"
    proc = _blender("--python", str(script), "--", str(source))
    assert source.is_file(), proc.stderr + proc.stdout
    out = tmp_path / "rigged.glb"
    proc = _blender(
        "--python",
        str(REPO_ROOT / "blender" / "part_rig.py"),
        "--",
        str(source),
        str(out),
    )
    assert "PART_RIG_OK" in proc.stdout, proc.stderr + proc.stdout
    # the body is 0.2 deep; a hand left on the torso, a metre in front of it, would make it 1.2
    assert _depth_of_the_body(_gltf(out)) < 0.5


@needs_blender
def test_part_rig_refuses_a_model_with_too_few_parts(tmp_path):
    script = tmp_path / "one.py"
    script.write_text(
        "import bpy, sys\nfor o in list(bpy.data.objects): bpy.data.objects.remove(o)\n"
        "bpy.ops.mesh.primitive_cube_add()\nbpy.ops.export_scene.gltf(filepath=sys.argv[-1], export_format='GLB')\n"
    )
    single = tmp_path / "one.glb"
    _blender("--python", str(script), "--", str(single))
    proc = _blender(
        "--python",
        str(REPO_ROOT / "blender" / "part_rig.py"),
        "--",
        str(single),
        str(tmp_path / "out.glb"),
    )
    assert "too few" in proc.stderr + proc.stdout
    assert not (tmp_path / "out.glb").exists()


def test_rig_body_validates_before_doing_any_work(tmp_path):
    deploy = tmp_path / "deploy"
    (deploy / "assets" / "custom_templates").mkdir(parents=True)
    glb = tmp_path / "m.glb"
    glb.write_bytes(b"glTF")
    with pytest.raises(SystemExit, match="custom:"):
        models.rig_body(deploy, glb, "standard_humanoid_female")
    with pytest.raises(SystemExit, match=r"\.glb"):
        models.rig_body(deploy, tmp_path / "m.fbx", "custom:m")
    (deploy / "assets" / "custom_templates" / "m.glb").write_bytes(b"glTF")
    with pytest.raises(SystemExit, match="already installed"):
        models.rig_body(deploy, glb, "custom:m")


LIMBLESS = """
import bpy, sys
for o in list(bpy.data.objects):
    bpy.data.objects.remove(o)
def box(name, x, y, z, sx, sy, sz):
    bpy.ops.mesh.primitive_cube_add(size=1.0, location=(x, y, z))
    o = bpy.context.active_object
    o.name = name
    o.scale = (sx, sy, sz)
    bpy.ops.object.transform_apply(scale=True)
box("head", 0, 0, 1.6, 0.5, 0.4, 0.4)
box("neck", 0, 0, 1.2, 0.15, 0.15, 0.2)
box("body", 0, 0, 0.7, 0.6, 0.5, 0.6)
for i, (x, y) in enumerate(((0.35, 0.3), (-0.35, 0.3), (0.35, -0.3), (-0.35, -0.3))):
    box(f"wheel{i}", x, y, 0.15, 0.1, 0.3, 0.3)
bpy.ops.export_scene.gltf(filepath=sys.argv[-1], export_format="GLB")
"""


@needs_blender
def test_part_rig_limbless_rigs_a_figure_with_no_arms_or_legs(tmp_path):
    script = tmp_path / "wheels.py"
    script.write_text(LIMBLESS)
    robot = tmp_path / "wheels.glb"
    _blender("--python", str(script), "--", str(robot))
    out = tmp_path / "rigged.glb"
    proc = _blender(
        "--python",
        str(REPO_ROOT / "blender" / "part_rig.py"),
        "--",
        str(robot),
        str(out),
        "--limbless",
    )
    assert "PART_RIG_OK" in proc.stdout, proc.stderr + proc.stdout
    gltf = _gltf(out)
    names = {n["name"] for n in gltf["nodes"]}
    assert {
        "pelvis",
        "Head",
        "neck_01",
        "thigh_l",
        "calf_r",
        "upperarm_l",
        "lowerarm_r",
    } <= names
    meshes = {n["name"] for n in gltf["nodes"] if "mesh" in n}
    assert meshes == {
        "pelvis",
        "neck_01",
        "Head",
    }  # the wheels ride the body; no limb has anything on it


@needs_blender
def test_part_rig_split_loose_cuts_a_single_mesh_into_its_parts(
    figure, tmp_path
):
    joined = tmp_path / "joined.py"
    joined.write_text(
        "import bpy, sys\nbpy.ops.import_scene.gltf(filepath=sys.argv[-2])\n"
        "ms = [o for o in bpy.data.objects if o.type == 'MESH']\n"
        "bpy.ops.object.select_all(action='DESELECT')\n"
        "for o in ms: o.select_set(True)\n"
        "bpy.context.view_layer.objects.active = ms[0]\nbpy.ops.object.join()\n"
        "bpy.ops.export_scene.gltf(filepath=sys.argv[-1], export_format='GLB')\n"
    )
    one = tmp_path / "one_mesh.glb"
    _blender("--python", str(joined), "--", str(figure), str(one))
    assert len(_gltf(one)["meshes"]) == 1
    refused = _blender(
        "--python",
        str(REPO_ROOT / "blender" / "part_rig.py"),
        "--",
        str(one),
        str(tmp_path / "a.glb"),
    )
    assert "too few" in refused.stderr + refused.stdout
    proc = _blender(
        "--python",
        str(REPO_ROOT / "blender" / "part_rig.py"),
        "--",
        str(one),
        str(tmp_path / "b.glb"),
        "--split-loose",
    )
    assert "PART_RIG_OK" in proc.stdout, proc.stderr + proc.stdout


@needs_blender
def test_rebind_lists_binds_and_renames_a_mesh(figure, tmp_path):
    rigged = tmp_path / "rigged.glb"
    _blender(
        "--python",
        str(REPO_ROOT / "blender" / "part_rig.py"),
        "--",
        str(figure),
        str(rigged),
    )
    rebind = str(REPO_ROOT / "blender" / "rebind.py")
    listing = _blender("--python", rebind, "--", "--list", str(rigged))
    assert (
        "LIST_OK" in listing.stdout and "MESH Head vertices=" in listing.stdout
    )
    out = tmp_path / "out.glb"
    done = _blender(
        "--python",
        rebind,
        "--",
        str(rigged),
        str(out),
        "--mesh",
        "Head",
        "--bone",
        "pelvis",
        "--rename",
        "skull",
    )
    assert "REBIND_OK" in done.stdout, done.stderr + done.stdout
    gltf = _gltf(out)
    names = {n["name"] for n in gltf["nodes"]}
    assert "skull" in names and "Head" in {
        n["name"] for n in gltf["nodes"] if "mesh" not in n
    }  # the bone is still there
    skull = next(n for n in gltf["nodes"] if n["name"] == "skull")
    joints = gltf["skins"][skull["skin"]]["joints"]
    primitive = gltf["meshes"][skull["mesh"]]["primitives"][0]
    assert "JOINTS_0" in primitive["attributes"]
    assert gltf["nodes"][joints[0]]["name"] in {
        "pelvis",
        "Head",
        "root",
    }  # bound; the weights are checked below
    refused = _blender(
        "--python",
        rebind,
        "--",
        str(rigged),
        str(tmp_path / "x.glb"),
        "--mesh",
        "nope",
        "--bone",
        "pelvis",
    )
    assert "no skinned mesh called nope" in refused.stderr + refused.stdout


@needs_blender
def test_rebind_binds_a_pair_by_side_with_a_ramp_and_moves_a_mesh(
    figure, tmp_path
):
    rigged = tmp_path / "rigged.glb"
    _blender(
        "--python",
        str(REPO_ROOT / "blender" / "part_rig.py"),
        "--",
        str(figure),
        str(rigged),
    )
    pair = tmp_path / "pair.py"
    pair.write_text(
        "import bpy, sys\nbpy.ops.import_scene.gltf(filepath=sys.argv[-2])\n"
        "ms = [o for o in bpy.data.objects if o.name in ('thigh_l', 'thigh_r')]\n"
        "bpy.ops.object.select_all(action='DESELECT')\n"
        "for o in ms: o.select_set(True)\nbpy.context.view_layer.objects.active = ms[0]\nbpy.ops.object.join()\n"
        "bpy.context.active_object.name = 'thighs'\n"
        "bpy.ops.export_scene.gltf(filepath=sys.argv[-1], export_format='GLB', export_skins=True)\n"
    )
    joined = tmp_path / "joined.glb"
    _blender("--python", str(pair), "--", str(rigged), str(joined))
    rebind = str(REPO_ROOT / "blender" / "rebind.py")
    out = tmp_path / "out.glb"
    done = _blender(
        "--python",
        rebind,
        "--",
        str(joined),
        str(out),
        "--mesh",
        "thighs",
        "--bone-left",
        "thigh_l",
        "--bone-right",
        "thigh_r",
        "--ramp",
        "calf_l",
        "calf_r",
        "0.7",
        "1.2",
        "--offset",
        "0",
        "0.25",
        "0",
    )
    assert "REBIND_OK" in done.stdout, done.stderr + done.stdout
    listing = _blender("--python", rebind, "--", "--list", str(out)).stdout
    line = next(
        row for row in listing.splitlines() if row.startswith("MESH thighs")
    )
    assert (
        "thigh_l" in line
        and "thigh_r" in line
        and "calf_l" in line
        and "calf_r" in line
    )  # each side, and the ramp's upper bones
    before, after = (_gltf(p) for p in (joined, out))

    def lowest_corner(g):
        node = next(n for n in g["nodes"] if n.get("name") == "thighs")
        return g["accessors"][
            g["meshes"][node["mesh"]]["primitives"][0]["attributes"]["POSITION"]
        ]["min"]

    moved = max(
        abs(a - b) for a, b in zip(lowest_corner(after), lowest_corner(before))
    )
    assert (
        abs(moved - 0.25) < 0.02
    ), moved  # the mesh moved 0.25 m, along one axis

    refused = _blender(
        "--python",
        rebind,
        "--",
        str(joined),
        str(tmp_path / "x.glb"),
        "--mesh",
        "thighs",
        "--bone-left",
        "thigh_l",
    )
    assert "together" in refused.stderr + refused.stdout
