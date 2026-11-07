"""Tests for the Sketchfab importer: the rules in blender/sketchfab.py (pure Python), and
blender/sketchfab_convert.py run on a small model made to look like a Sketchfab download.
"""

from __future__ import annotations

import json
import shutil
import struct
import subprocess
from pathlib import Path

import pytest

from blender import sketchfab
from brain.appearance import models

REPO_ROOT = Path(__file__).resolve().parents[2]
BLENDER = shutil.which("blender")


# --- the rules --------------------------------------------------------------------------------


def test_a_sketchfab_export_is_recognised_by_its_generator():
    assert sketchfab.is_sketchfab(
        {"asset": {"generator": "Sketchfab-16.87.0", "version": "2.0"}}
    )
    assert not sketchfab.is_sketchfab(
        {"asset": {"generator": "Khronos glTF Blender I/O v4.2", "version": "2.0"}}
    )
    assert not sketchfab.is_sketchfab({})


def test_sketchfabs_name_suffixes_and_the_importers_helper_bones_are_taken_off():
    assert sketchfab.clean_name("CC_Base_Head_038") == "CC_Base_Head"
    assert (
        sketchfab.clean_name("CC_Base_L_Hand_055_scaleCompensation") == "CC_Base_L_Hand"
    )
    assert (
        sketchfab.clean_name("spine_01_x_04") == "spine_01_x"
    )  # one suffix only: spine_01 is the author's
    assert sketchfab.clean_name("RL_BoneRoot_01") == "RL_BoneRoot"
    assert sketchfab.clean_name("c_subneck_1_x_09") == "c_subneck_1_x"
    assert sketchfab.clean_name("CC_Base_L_ThighTwist01_00") == "CC_Base_L_ThighTwist01"
    assert sketchfab.clean_name("Head") == "Head"  # nothing to take off
    assert sketchfab.is_helper_bone(
        "Hip_02_scaleCompensation"
    ) and sketchfab.is_helper_bone("_rootJoint")
    assert not sketchfab.is_helper_bone("Hip_02")


def test_cleaned_names_that_would_collide_keep_sketchfabs_names():
    assert sketchfab.clean_names(["Arm_01", "Arm_02", "Head_03"]) == {
        "Arm_01": "Arm_01",
        "Arm_02": "Arm_02",
        "Head_03": "Head",
    }
    assert sketchfab.clean_names(["Hand", "Hand_05"]) == {
        "Hand": "Hand",
        "Hand_05": "Hand_05",
    }  # that name is taken


@pytest.mark.parametrize(
    ("text", "attribution", "commercial_ok", "notes"),
    [
        (
            "CC-BY-4.0 (http://creativecommons.org/licenses/by/4.0/)",
            True,
            True,
            ["credit the author"],
        ),
        (
            "CC-BY-NC-4.0 (http://creativecommons.org/licenses/by-nc/4.0/)",
            True,
            False,
            ["non-commercial"],
        ),
        ("CC0-1.0", False, True, []),
        ("CC-BY-ND-4.0", True, True, ["no derivatives"]),
        ("CC-BY-SA-4.0", True, True, ["same licence"]),
    ],
)
def test_licences_are_read_and_their_conditions_listed(
    text, attribution, commercial_ok, notes
):
    licence = sketchfab.parse_licence(text)
    assert (
        licence.attribution is attribution
        and (not licence.non_commercial) is commercial_ok
    )
    joined = " ".join(licence.notes())
    assert all(note in joined for note in notes)
    if not notes:
        assert licence.notes() == []


def test_a_licence_that_is_not_creative_commons_is_flagged_for_reading():
    licence = sketchfab.parse_licence("Standard")
    assert not licence.free and "not a Creative Commons" in licence.notes()[0]
    assert not sketchfab.parse_licence("").free


def test_credits_come_from_the_assets_extras():
    gltf = {
        "asset": {
            "generator": "Sketchfab-17",
            "extras": {
                "title": "Mech",
                "author": "Someone (https://sketchfab.com/someone)",
                "license": "CC-BY-NC-4.0 (http://creativecommons.org/licenses/by-nc/4.0/)",
                "source": "https://sketchfab.com/3d-models/mech-1",
            },
        }
    }
    credits = sketchfab.credits(gltf)
    assert (
        credits["title"] == "Mech"
        and credits["attribution_required"]
        and credits["non_commercial"]
    )
    assert models.credit_line(credits) == "Mech by Someone (CC-BY-NC-4.0)"
    assert models.credit_line(None) == "" and sketchfab.credits({})["notes"][
        0
    ].startswith("licence")


def test_installed_models_credits_are_recorded_beside_them(tmp_path):
    model = tmp_path / "mech.glb"
    models.credits_path(model).write_text(
        json.dumps({"title": "Mech", "author": "Someone", "license": "CC-BY-4.0"})
    )
    assert models.read_credits(model) == {
        "title": "Mech",
        "author": "Someone",
        "license": "CC-BY-4.0",
    }
    assert (
        models.read_credits(tmp_path / "other.glb") is None
        and models.read_credits(None) is None
    )


# --- the cleaning, on a model made to look like a download ---------------------------------------

_MAKE = """
import bpy, json, struct
bpy.ops.wm.read_factory_settings(use_empty=True)
from mathutils import Vector

wrapper = bpy.data.objects.new("Sketchfab_model", None)       # the author's centimetre scale, above everything
wrapper.scale = (0.01, 0.01, 0.01)
bpy.context.scene.collection.objects.link(wrapper)

arm_data = bpy.data.armatures.new("Armature")
arm = bpy.data.objects.new("Armature", arm_data)
bpy.context.scene.collection.objects.link(arm)
arm.parent = wrapper
bpy.context.view_layer.objects.active = arm
bpy.ops.object.mode_set(mode="EDIT")
def bone(name, head, tail, parent=None):
    b = arm_data.edit_bones.new(name)
    b.head, b.tail = head, tail
    if parent: b.parent = parent
    return b
root = bone("_rootJoint", (0, 0, 0), (0, 0, 10))
hips_h = bone("Hips_02_scaleCompensation", (0, 0, 90), (0, 0, 100), root)
hips = bone("Hips_02", (0, 0, 90), (0, 0, 100), hips_h)
spine_h = bone("Spine_03_scaleCompensation", (0, 0, 100), (0, 0, 130), hips)
spine = bone("Spine_03", (0, 0, 100), (0, 0, 130), spine_h)
head_h = bone("Head_04_scaleCompensation", (0, 0, 130), (0, 0, 170), spine)
head = bone("Head_04", (0, 0, 130), (0, 0, 170), head_h)
bpy.ops.object.mode_set(mode="OBJECT")

import bmesh
mesh = bpy.data.meshes.new("Body")
bm = bmesh.new()
bmesh.ops.create_cube(bm, size=1.0)
for v in bm.verts:
    v.co = (v.co.x * 40, v.co.y * 20, 90 + (v.co.z + 0.5) * 80)    # a box from the hips to the head, in cm
bm.to_mesh(mesh); bm.free()
body = bpy.data.objects.new("Body", mesh)
bpy.context.scene.collection.objects.link(body)
body.parent = arm
for name in ("Hips_02", "Spine_03", "Head_04"):
    body.vertex_groups.new(name=name)
for v in mesh.vertices:
    body.vertex_groups["Hips_02" if v.co.z < 120 else "Head_04"].add([v.index], 1.0, "REPLACE")
body.modifiers.new("Armature", "ARMATURE").object = arm
body.shape_key_add(name="Basis")
smile = body.shape_key_add(name="Smile")

action = bpy.data.actions.new("TempMotion")                   # the motion it was exported with
arm.animation_data_create().action = action
pb = arm.pose.bones["Head_04"]
pb.rotation_mode = "XYZ"
for frame, angle in ((1, 0.0), (10, 0.3)):
    pb.rotation_euler = (angle, 0, 0); pb.keyframe_insert("rotation_euler", frame=frame)

bpy.ops.export_scene.gltf(filepath=__OUT__, export_format="GLB", export_animations=True)
data = open(__OUT__, "rb").read()
doc = json.loads(data[20:20 + struct.unpack("<I", data[12:16])[0]])
doc["asset"]["generator"] = "Sketchfab-16.87.0"
doc["asset"]["extras"] = {"title": "Test Hero", "author": "Someone (https://sketchfab.com/someone)", "license": "CC-BY-4.0 (http://creativecommons.org/licenses/by/4.0/)", "source": "https://sketchfab.com/3d-models/test"}
chunk = json.dumps(doc).encode(); chunk += b" " * (-len(chunk) % 4)
rest = data[20 + struct.unpack("<I", data[12:16])[0]:]
open(__OUT__, "wb").write(b"glTF" + struct.pack("<II", 2, 20 + len(chunk) + len(rest)) + struct.pack("<I", len(chunk)) + b"JSON" + chunk + rest)
"""


def _gltf(path: Path) -> dict:
    data = path.read_bytes()
    return json.loads(data[20 : 20 + struct.unpack("<I", data[12:16])[0]])


@pytest.mark.skipif(BLENDER is None, reason="blender binary not found on PATH")
def test_a_sketchfab_download_is_cleaned_up_for_use_as_a_body(tmp_path):
    source = tmp_path / "download.glb"
    subprocess.run(
        [
            BLENDER or "blender",
            "--background",
            "--factory-startup",
            "--python-expr",
            _MAKE.replace("__OUT__", repr(str(source))),
        ],
        capture_output=True,
        check=True,
        timeout=120,
    )
    assert sketchfab.is_sketchfab(_gltf(source))
    before = _gltf(source)
    assert any(
        n.get("name", "").endswith("_scaleCompensation") for n in before["nodes"]
    )

    cleaned = models.convert_sketchfab_body(source, tmp_path / "out")
    gltf = _gltf(cleaned)
    names = [n.get("name", "") for n in gltf["nodes"]]
    joints = {
        gltf["nodes"][j]["name"] for skin in gltf["skins"] for j in skin["joints"]
    }

    assert joints == {
        "Hips",
        "Spine",
        "Head",
    }  # the author's names, no helpers, no `_rootJoint`
    assert not any(n.endswith("_scaleCompensation") or n == "_rootJoint" for n in names)
    assert (
        "Icosphere" not in names and len(gltf["meshes"]) == 1
    )  # not the sphere the bones were shown with
    assert not any(
        prim.get("targets") for mesh in gltf["meshes"] for prim in mesh["primitives"]
    )  # no shape keys
    assert (
        "animations" not in gltf or not gltf["animations"]
    )  # not the motion it was exported with
    assert not any(
        n.get("name") in ("Sketchfab_model", "Armature") and n.get("scale")
        for n in gltf["nodes"]
    )  # nothing scaled above the figure
    accessors = gltf["accessors"]
    top = max(
        accessors[p["attributes"]["POSITION"]]["max"][1]
        for m in gltf["meshes"]
        for p in m["primitives"]
    )
    assert top == pytest.approx(1.7, abs=0.05)  # 170 cm, as metres, in glTF's Y-up
    assert sketchfab.credits(before)["attribution_required"]


def test_an_installed_models_credit_is_read_from_its_sidecar_for_the_listing(tmp_path):
    model = tmp_path / "hero.glb"
    model.write_bytes(b"glTF")
    models.credits_path(model).write_text(
        json.dumps(
            {
                "title": "Hero",
                "author": "Someone (https://x)",
                "license": "CC-BY-4.0 (http://y)",
            }
        )
    )
    assert (
        models.credit_line(models.read_credits(model)) == "Hero by Someone (CC-BY-4.0)"
    )


def test_a_big_glb_is_read_without_loading_all_of_it(tmp_path):
    body = json.dumps({"asset": {"generator": "Sketchfab-1"}}).encode()
    body += b" " * (-len(body) % 4)
    path = tmp_path / "big.glb"
    path.write_bytes(
        b"glTF"
        + struct.pack("<II", 2, 20 + len(body) + 4096)
        + struct.pack("<I", len(body))
        + b"JSON"
        + body
        + b"\0" * 4096
    )
    assert sketchfab.is_sketchfab(models._glb_json(path))
    assert models._glb_json(tmp_path / "missing.glb") == {}
