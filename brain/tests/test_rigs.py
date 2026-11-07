"""Tests for converting a body of another rig to the Quaternius skeleton and retargeting the libraries
onto it: the maps in blender/rigs.py (pure Python), and blender/rig_convert.py run on small synthetic
models."""

from __future__ import annotations

import json
import shutil
import struct
import subprocess
from pathlib import Path

import pytest

from blender import animation_library as al
from blender import rigs
from brain.appearance import models

BLENDER = shutil.which("blender")

CC_NAMES = [
    "RL_BoneRoot",
    "CC_Base_Hip",
    "CC_Base_Pelvis",
    "CC_Base_Waist",
    "CC_Base_Spine01",
    "CC_Base_Spine02",
    "CC_Base_NeckTwist01",
    "CC_Base_NeckTwist02",
    "CC_Base_Head",
    "CC_Base_L_Clavicle",
    "CC_Base_L_Upperarm",
    "CC_Base_L_UpperarmTwist01",
    "CC_Base_L_Forearm",
    "CC_Base_L_Hand",
    "CC_Base_L_Mid1",
    "CC_Base_L_Thigh",
    "CC_Base_L_Calf",
    "CC_Base_L_Foot",
    "CC_Base_L_ToeBase",
    "CC_Base_L_Eye",
]
ARP_NAMES = [
    "RL_BoneRoot",
    "root",
    "root_x",
    "spine_01_x",
    "spine_02_x",
    "spine_03_x",
    "spine_04_x",
    "spine_05_x",
    "neck_x",
    "head_x",
    "shoulder_l",
    "arm_stretch_l",
    "arm_twist_l",
    "forearm_stretch_l",
    "hand_l",
    "middle1_l",
    "middle1_base_l",
    "thigh_stretch_l",
    "leg_stretch_l",
    "foot_l",
    "toes_01_l",
]


def test_a_skeleton_is_told_by_the_names_its_tool_gives_its_bones():
    assert rigs.rig_family(CC_NAMES) == rigs.CHARACTER_CREATOR
    assert rigs.rig_family(ARP_NAMES) == rigs.AUTO_RIG_PRO
    assert (
        rigs.rig_family(["pelvis", "thigh_l", "upperarm_l"]) is None
    )  # already Quaternius
    assert rigs.rig_family(["mixamorig:Hips", "mixamorig:LeftArm"]) is None
    assert rigs.rig_family([]) is None


@pytest.mark.parametrize(
    ("name", "target"),
    [
        ("RL_BoneRoot", "root"),
        ("CC_Base_Hip", "pelvis"),
        ("CC_Base_Waist", "spine_01"),
        ("CC_Base_Spine01", "spine_02"),
        ("CC_Base_Spine02", "spine_03"),
        ("CC_Base_NeckTwist01", "neck_01"),
        ("CC_Base_Head", "Head"),
        ("CC_Base_L_Clavicle", "clavicle_l"),
        ("CC_Base_R_Upperarm", "upperarm_r"),
        ("CC_Base_L_Forearm", "lowerarm_l"),
        ("CC_Base_L_Hand", "hand_l"),
        ("CC_Base_L_Mid2", "middle_02_l"),
        ("CC_Base_R_Thumb1", "thumb_01_r"),
        ("CC_Base_L_Pinky3", "pinky_03_l"),
        ("CC_Base_L_Thigh", "thigh_l"),
        ("CC_Base_R_Calf", "calf_r"),
        ("CC_Base_L_Foot", "foot_l"),
        ("CC_Base_L_ToeBase", "ball_l"),
        ("CC_Base_Pelvis", None),
        ("CC_Base_NeckTwist02", None),
        ("CC_Base_L_UpperarmTwist01", None),
        ("CC_Base_L_ForearmTwist02", None),
        ("CC_Base_L_Eye", None),
        ("CC_Base_L_Index4", None),
        ("Hips", None),
    ],
)
def test_character_creator_bones_map_to_their_quaternius_equivalents(name, target):
    assert rigs.character_creator_to_quaternius(name) == target


@pytest.mark.parametrize(
    ("name", "target"),
    [
        ("root", "root"),
        ("root_x", "pelvis"),
        ("spine_01_x", "spine_01"),
        ("spine_03_x", "spine_02"),
        ("spine_05_x", "spine_03"),
        ("neck_x", "neck_01"),
        ("head_x", "Head"),
        ("shoulder_r", "clavicle_r"),
        ("arm_stretch_l", "upperarm_l"),
        ("forearm_stretch_r", "lowerarm_r"),
        ("hand_l", "hand_l"),
        ("middle2_l", "middle_02_l"),
        ("thumb1_r", "thumb_01_r"),
        ("thigh_stretch_l", "thigh_l"),
        ("leg_stretch_r", "calf_r"),
        ("foot_l", "foot_l"),
        ("toes_01_l", "ball_l"),
        ("spine_02_x", None),
        ("spine_04_x", None),
        ("arm_twist_l", None),
        ("middle1_base_l", None),
        ("c_subneck_1_x", None),
    ],
)
def test_auto_rig_pro_bones_map_to_their_quaternius_equivalents(name, target):
    assert rigs.auto_rig_pro_to_quaternius(name) == target


def test_only_the_bones_with_an_equivalent_are_renamed():
    mapped = rigs.quaternius_names(rigs.CHARACTER_CREATOR, CC_NAMES)
    assert (
        mapped["CC_Base_Hip"] == "pelvis"
        and "CC_Base_Pelvis" not in mapped
        and "CC_Base_L_Eye" not in mapped
    )
    assert len(mapped) == 16
    assert rigs.quaternius_names(rigs.AUTO_RIG_PRO, ["spine_03_x", "spine_02_x"]) == {
        "spine_03_x": "spine_02"
    }  # spine_02_x has no equivalent


def test_a_body_converted_from_another_rig_plays_its_own_retargeted_libraries(tmp_path):
    model = tmp_path / "assets" / "custom_templates" / "hero.glb"
    model.parent.mkdir(parents=True)
    model.write_bytes(b"glTF")
    assert (
        al.library_folder(tmp_path, model) == tmp_path / "assets" / "animation_library"
    )
    assert not al.is_retargeted(model) and not al.is_retargeted(None)
    al.rig_sidecar(model).write_text(
        json.dumps({"family": "auto_rig_pro", "retargeted": True})
    )
    assert al.is_retargeted(model)
    assert (
        al.library_folder(tmp_path, model)
        == tmp_path / "assets" / "animation_library_body" / "hero"
    )
    assert (
        al.library_folder(tmp_path, None) == tmp_path / "assets" / "animation_library"
    )
    assert models.converted_bodies(tmp_path) == ["hero"]


def test_only_the_clips_a_role_uses_are_retargeted():
    clips = {
        "Idle_Loop",
        "Walk_Loop",
        "Swim_Fwd_Loop",
        "Sword_Attack",
        "Standing_Greeting",
        "Standing_Greeting_Low",
    }
    assert al.wanted_clips(clips) == [
        "Idle_Loop",
        "Standing_Greeting",
        "Standing_Greeting_Low",
        "Walk_Loop",
    ]


# --- on small synthetic models -------------------------------------------------------------------

_BUILD = """
import bpy, bmesh, json, struct, math
from mathutils import Vector

def make(bones, mesh_name, groups, out, action=None):
    bpy.ops.wm.read_factory_settings(use_empty=True)
    data = bpy.data.armatures.new("Armature")
    arm = bpy.data.objects.new("Armature", data)
    bpy.context.scene.collection.objects.link(arm)
    bpy.context.view_layer.objects.active = arm
    bpy.ops.object.mode_set(mode="EDIT")
    made = {}
    for name, head, tail, parent in bones:
        b = data.edit_bones.new(name)
        b.head, b.tail = head, tail
        if parent: b.parent = made[parent]
        made[name] = b
    bpy.ops.object.mode_set(mode="OBJECT")
    mesh = bpy.data.meshes.new(mesh_name)
    bm = bmesh.new(); bmesh.ops.create_cube(bm, size=0.3); bm.to_mesh(mesh); bm.free()
    obj = bpy.data.objects.new(mesh_name, mesh)
    bpy.context.scene.collection.objects.link(obj)
    obj.parent = arm
    for g in groups: obj.vertex_groups.new(name=g)
    for v in mesh.vertices: obj.vertex_groups[groups[0]].add([v.index], 1.0, "REPLACE")
    obj.modifiers.new("Armature", "ARMATURE").object = arm
    if action:
        arm.animation_data_create()
        act = bpy.data.actions.new(action["name"]); arm.animation_data.action = act
        pb = arm.pose.bones[action["bone"]]; pb.rotation_mode = "XYZ"
        for frame, angle in ((0, 0.0), (10, action["angle"])):
            pb.rotation_euler = action["axis"] * angle if False else (angle if action["axis"] == "x" else 0, angle if action["axis"] == "y" else 0, angle if action["axis"] == "z" else 0)
            pb.keyframe_insert("rotation_euler", frame=frame)
    bpy.ops.export_scene.gltf(filepath=out, export_format="GLB", export_animations=bool(action))
"""

_BODY = _BUILD + """
# a Character Creator-style body with its arms hanging down, facing -Y (its left along +X)
bones = [
    ("RL_BoneRoot", (0, 0, 0), (0, 0, 0.1), None),
    ("CC_Base_Hip", (0, 0, 1.0), (0, 0, 1.1), "RL_BoneRoot"),
    ("CC_Base_Pelvis", (0, 0, 1.0), (0, 0, 1.1), "CC_Base_Hip"),
    ("CC_Base_Waist", (0, 0, 1.1), (0, 0, 1.2), "CC_Base_Hip"),
    ("CC_Base_Spine01", (0, 0, 1.2), (0, 0, 1.3), "CC_Base_Waist"),
    ("CC_Base_Spine02", (0, 0, 1.3), (0, 0, 1.4), "CC_Base_Spine01"),
    ("CC_Base_NeckTwist01", (0, 0, 1.4), (0, 0, 1.5), "CC_Base_Spine02"),
    ("CC_Base_Head", (0, 0, 1.5), (0, 0, 1.7), "CC_Base_NeckTwist01"),
]
for s, x in (("L", 1), ("R", -1)):
    bones += [
        (f"CC_Base_{s}_Clavicle", (x * 0.02, 0, 1.4), (x * 0.15, 0, 1.4), "CC_Base_Spine02"),
        (f"CC_Base_{s}_Upperarm", (x * 0.15, 0, 1.4), (x * 0.35, 0, 1.15), f"CC_Base_{s}_Clavicle"),     # hanging down and out
        (f"CC_Base_{s}_UpperarmTwist01", (x * 0.25, 0, 1.28), (x * 0.3, 0, 1.2), f"CC_Base_{s}_Upperarm"),
        (f"CC_Base_{s}_Forearm", (x * 0.35, 0, 1.15), (x * 0.45, 0, 0.9), f"CC_Base_{s}_Upperarm"),
        (f"CC_Base_{s}_Hand", (x * 0.45, 0, 0.9), (x * 0.47, 0, 0.8), f"CC_Base_{s}_Forearm"),
        (f"CC_Base_{s}_Thigh", (x * 0.1, 0, 1.0), (x * 0.1, 0, 0.55), "CC_Base_Pelvis"),
        (f"CC_Base_{s}_Calf", (x * 0.1, 0, 0.55), (x * 0.1, 0, 0.1), f"CC_Base_{s}_Thigh"),
        (f"CC_Base_{s}_Foot", (x * 0.1, 0, 0.1), (x * 0.1, -0.15, 0.0), f"CC_Base_{s}_Calf"),
        (f"CC_Base_{s}_ToeBase", (x * 0.1, -0.15, 0.0), (x * 0.1, -0.25, 0.0), f"CC_Base_{s}_Foot"),
    ]
make(bones, "Body", ["CC_Base_Hip", "CC_Base_L_Upperarm", "CC_Base_L_Forearm"], __OUT__)
"""

_LIBRARY = _BUILD + """
# the Quaternius skeleton in a T-pose, with a clip that raises the left arm (turning its upper arm about Y)
bones = [
    ("root", (0, 0, 0), (0, 0, 0.1), None),
    ("pelvis", (0, 0, 1.0), (0, 0, 1.1), "root"),
    ("spine_01", (0, 0, 1.1), (0, 0, 1.2), "pelvis"),
    ("spine_02", (0, 0, 1.2), (0, 0, 1.3), "spine_01"),
    ("spine_03", (0, 0, 1.3), (0, 0, 1.4), "spine_02"),
    ("neck_01", (0, 0, 1.4), (0, 0, 1.5), "spine_03"),
    ("Head", (0, 0, 1.5), (0, 0, 1.7), "neck_01"),
]
for s, x in (("l", 1), ("r", -1)):
    bones += [
        (f"clavicle_{s}", (x * 0.02, 0, 1.4), (x * 0.15, 0, 1.4), "spine_03"),
        (f"upperarm_{s}", (x * 0.15, 0, 1.4), (x * 0.4, 0, 1.4), f"clavicle_{s}"),
        (f"lowerarm_{s}", (x * 0.4, 0, 1.4), (x * 0.65, 0, 1.4), f"upperarm_{s}"),
        (f"hand_{s}", (x * 0.65, 0, 1.4), (x * 0.75, 0, 1.4), f"lowerarm_{s}"),
        (f"thigh_{s}", (x * 0.1, 0, 1.0), (x * 0.1, 0, 0.55), "pelvis"),
        (f"calf_{s}", (x * 0.1, 0, 0.55), (x * 0.1, 0, 0.1), f"thigh_{s}"),
        (f"foot_{s}", (x * 0.1, 0, 0.1), (x * 0.1, -0.15, 0.0), f"calf_{s}"),
        (f"ball_{s}", (x * 0.1, -0.15, 0.0), (x * 0.1, -0.25, 0.0), f"foot_{s}"),
    ]
make(bones, "Mesh", ["pelvis"], __OUT__, action={"name": "Wave_Loop", "bone": "upperarm_l", "axis": "y", "angle": -1.0})
"""


def accessors_of(gltf: dict, animation: dict, channel: dict) -> int:
    """How many keys a channel has."""
    return gltf["accessors"][animation["samplers"][channel["sampler"]]["input"]][
        "count"
    ]


def _gltf(path: Path) -> dict:
    data = path.read_bytes()
    return json.loads(data[20 : 20 + struct.unpack("<I", data[12:16])[0]])


def _run(script: str, out: Path) -> None:
    subprocess.run(
        [
            BLENDER or "blender",
            "--background",
            "--factory-startup",
            "--python-expr",
            script.replace("__OUT__", repr(str(out))),
        ],
        capture_output=True,
        check=True,
        timeout=120,
    )


@pytest.mark.skipif(BLENDER is None, reason="blender binary not found on PATH")
def test_a_character_creator_body_is_converted_and_a_library_is_retargeted_onto_it(
    tmp_path, monkeypatch
):
    body, library = tmp_path / "body.glb", tmp_path / "Pack.glb"
    _run(_BODY, body)
    _run(_LIBRARY, library)
    assert rigs.rig_family(al.library_contents(body)[0]) == rigs.CHARACTER_CREATOR

    converted, done = models.convert_rig_body(body, tmp_path / "rig")
    assert (
        "family=character_creator" in done
        and "mapped=23" in done
        and "tposed=1" in done
    )  # the hanging arms were posed out
    joints = al.library_contents(converted)[0]
    assert {
        "pelvis",
        "spine_01",
        "upperarm_l",
        "lowerarm_l",
        "thigh_r",
        "calf_r",
        "ball_l",
        "Head",
        "root",
    } <= joints
    assert (
        "CC_Base_L_UpperarmTwist01" in joints and "CC_Base_Pelvis" in joints
    )  # the other bones stay as they were
    assert not al.library_gaps(joints, joints)  # a body the libraries fit

    deploy = tmp_path / "deploy"
    installed = deploy / "assets" / "custom_templates" / "hero.glb"
    installed.parent.mkdir(parents=True)
    shutil.copy(converted, installed)
    al.rig_sidecar(installed).write_text(
        json.dumps({"family": "character_creator", "retargeted": True})
    )
    shutil.copy(library, tmp_path / "Pack_source.glb")
    library_dir = deploy / "assets" / "animation_library"
    library_dir.mkdir(parents=True)
    shutil.copy(library, library_dir / "Pack.glb")

    # the clip is not one a role uses, so nothing is retargeted until it is: Idle_Loop is
    monkeypatch.setattr(models, "wanted_clips", lambda names: sorted(names))
    clips = models.retarget_library_onto(deploy, "hero", library_dir / "Pack.glb")
    assert clips == ["Wave_Loop"]

    result = deploy / "assets" / "animation_library_body" / "hero" / "Pack.glb"
    gltf = _gltf(result)
    names = [n.get("name") for n in gltf["nodes"]]
    wave = next(a for a in gltf["animations"] if a["name"] == "Wave_Loop")
    animated = {
        names[c["target"]["node"]]
        for c in wave["channels"]
        if c["target"]["path"] == "rotation"
    }
    assert "upperarm_l" in animated  # the body's own bone turns
    twist = next(
        c
        for c in wave["channels"]
        if c["target"]["path"] == "rotation"
        and names[c["target"]["node"]] == "CC_Base_L_UpperarmTwist01"
    )
    assert (
        accessors_of(gltf, wave, twist) <= 2
    )  # a bone with no equivalent is not animated
    arm = next(
        c
        for c in wave["channels"]
        if c["target"]["path"] == "rotation"
        and names[c["target"]["node"]] == "upperarm_l"
    )
    assert accessors_of(gltf, wave, arm) > 2
    accessors = gltf["accessors"]
    first_key = max(accessors[s["input"]]["min"][0] for s in wave["samplers"])
    assert first_key == pytest.approx(0.0)  # starts at time 0
    assert al.late_starts(gltf) == []
