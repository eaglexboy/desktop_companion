"""Integration tests for blender/generator.py -- all invoke the real `blender`
binary as a subprocess (bpy only exists inside Blender's own interpreter, so
this can't be imported and unit-tested directly). Skipped automatically if
`blender` isn't on PATH.

Note on determinism: Blender's glTF exporter does not produce
byte-identical .glb output across separate process runs once a scene has
more than one mesh object, even with PYTHONHASHSEED pinned and --threads 1
(a single-mesh scene exports byte-identical every time; a two-mesh scene
does not). This is exporter-internal reordering, not something
generator.py controls, so test_seed_determinism below asserts
structural/semantic equality (triangle count, shape keys, clip names,
bounds) rather than a raw SHA256 match.
"""

from __future__ import annotations

import json
import math
import shutil
import struct
import subprocess
from pathlib import Path
from typing import Any

import pytest

from brain.tests.example_packs import install_example_packs

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
GENERATOR = REPO_ROOT / "blender" / "generator.py"
SAMPLE_LOOK = REPO_ROOT / "examples" / "sample_look.json"

BLENDER_BIN = shutil.which("blender") or "blender"
pytestmark = pytest.mark.skipif(
    shutil.which("blender") is None, reason="blender binary not found on PATH"
)


def _run_generator(
    *args: str, timeout: float = 90.0
) -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            BLENDER_BIN,
            "--background",
            "--factory-startup",
            "--python",
            str(GENERATOR),
            "--",
            *args,
        ],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _compile(
    spec: dict, tmp_path: Path, name: str = "look"
) -> tuple[subprocess.CompletedProcess, Path, dict]:
    spec_path = tmp_path / f"{name}.json"
    spec_path.write_text(json.dumps(spec))
    out_path = tmp_path / f"{name}.glb"
    deploy_dir = tmp_path / "deploy"
    result = _run_generator(
        "--spec",
        str(spec_path),
        "--out",
        str(out_path),
        "--deploy-dir",
        str(deploy_dir),
    )
    build_json = json.loads(out_path.with_suffix(".build.json").read_text())
    return result, out_path, build_json


def _minimal_valid_glb_bytes() -> bytes:
    """The smallest glTF binary Blender's importer will actually accept:
    a correct 12-byte header (magic, version=2, total length) followed by
    one minimal JSON chunk. Used to test the import path without needing a
    real authored asset."""
    content = json.dumps({"asset": {"version": "2.0"}}).encode()
    content += b" " * (
        (-len(content)) % 4
    )  # glTF chunks must be 4-byte aligned
    chunk_header = struct.pack("<I", len(content)) + b"JSON"
    total_length = 12 + len(chunk_header) + len(content)
    header = b"glTF" + struct.pack("<II", 2, total_length)
    return header + chunk_header + content


def _minimal_spec(**overrides) -> dict:
    spec = {
        "base_template": "primitive_blob",
        "has_face": True,
        "seed": 42,
        "proportions": {
            "muscularity": 0.5,
            "femininity": 0.5,
            "height": 1.0,
            "build": 1.0,
        },
        "palette": {
            "primary": "#4A90D9",
            "secondary": "#FFFFFF",
            "accent": "#F5A623",
            "emissive": "#7FD1FF",
        },
        "surface_material": "matte",
        "wardrobe": {"outfit": "none", "footwear": "none"},
        "accessories": {},
    }
    spec.update(overrides)
    return spec


def test_tier3_generation(tmp_path):
    result, out_path, build_json = _compile(_minimal_spec(), tmp_path)
    assert "DESKMATE_BUILD_OK" in result.stdout
    assert result.returncode == 0
    assert out_path.is_file()
    assert build_json["status"] == "ok"
    assert build_json["evaluated_triangles"] > 0


def test_generated_eyes_carry_a_blink_morph_target(tmp_path):
    import json
    import struct

    _, out_path, build_json = _compile(_minimal_spec(), tmp_path)
    assert build_json["blink_shape_keys"] == ["blink"]
    data = out_path.read_bytes()
    gltf = json.loads(data[20 : 20 + struct.unpack("<I", data[12:16])[0]])
    by_name = {n["name"]: n for n in gltf["nodes"] if "mesh" in n}
    for eye in ("Eye_L", "Eye_R"):
        mesh = gltf["meshes"][by_name[eye]["mesh"]]
        assert (
            len(mesh["primitives"][0]["targets"]) == 1
        )  # one morph target: blink
    # the mouth keeps its nine visemes; blink must not leak into it
    mouth = gltf["meshes"][by_name["Mouth"]["mesh"]]
    assert len(mouth["primitives"][0]["targets"]) == 9


def test_legged_avatars_get_run_pose_morph_targets(tmp_path):
    import json
    import struct

    _, out_path, build_json = _compile(
        _minimal_spec(base_template="standard_humanoid_male"), tmp_path
    )
    assert build_json["run_shape_keys"] == ["run_a", "run_b"]
    data = out_path.read_bytes()
    gltf = json.loads(data[20 : 20 + struct.unpack("<I", data[12:16])[0]])
    body = next(n for n in gltf["nodes"] if n.get("name") == "Body")
    assert len(gltf["meshes"][body["mesh"]]["primitives"][0]["targets"]) == 2

    # legless avatars keep the plain hop: no run keys
    _, _, blob = _compile(_minimal_spec(), tmp_path, "blob")
    assert blob["run_shape_keys"] == []


def test_male_and_female_humanoids_are_visibly_different(tmp_path):
    _, _, male = _compile(
        _minimal_spec(base_template="standard_humanoid_male"), tmp_path, "male"
    )
    _, _, female = _compile(
        _minimal_spec(base_template="standard_humanoid_female"),
        tmp_path,
        "female",
    )
    # broad shoulders vs narrow ones: the figure's overall width differs by well over 10%
    assert male["bounds"]["width_m"] > female["bounds"]["width_m"] * 1.1


def test_humanoid_base_templates_build_a_procedural_humanoid_when_no_model_exists(
    tmp_path,
):
    """With no standard_humanoid_{female,male}.glb installed, the generator
    builds its own humanoid: a head, two arms and two legs, rigged -- not the
    blob it used to quietly substitute."""
    for base_template in ("standard_humanoid_female", "standard_humanoid_male"):
        result, out_path, build_json = _compile(
            _minimal_spec(base_template=base_template), tmp_path, base_template
        )
        assert result.returncode == 0, result.stderr
        assert out_path.is_file()
        assert build_json["status"] == "ok"
        assert build_json["capabilities"] == {
            "has_head": True,
            "has_arms": True,
            "has_legs": True,
            "has_face": True,
        }
        assert build_json["warnings"] == []


def test_blob_has_no_limbs(tmp_path):
    _, _, build_json = _compile(_minimal_spec(), tmp_path)
    assert build_json["capabilities"]["has_arms"] is False
    assert build_json["capabilities"]["has_legs"] is False


def test_installed_model_is_used_not_discarded(tmp_path):
    """A rigged model dropped in as standard_humanoid_male.glb becomes the
    body: it keeps its own limbs and lip-sync, and is scaled to house height."""
    _, source_glb, _ = _compile(
        _minimal_spec(base_template="standard_humanoid_female"),
        tmp_path,
        "source",
    )
    base_templates_dir = tmp_path / "deploy" / "assets" / "base_templates"
    base_templates_dir.mkdir(parents=True)
    shutil.copy(source_glb, base_templates_dir / "standard_humanoid_male.glb")

    result, _, build_json = _compile(
        _minimal_spec(base_template="standard_humanoid_male"), tmp_path, "used"
    )
    assert result.returncode == 0, result.stderr
    assert build_json["status"] == "ok"
    assert build_json["capabilities"] == {
        "has_head": True,
        "has_arms": True,
        "has_legs": True,
        "has_face": True,
    }
    assert len(build_json["viseme_shape_keys"]) == 9
    assert (
        build_json["blink_shape_keys"] == []
    )  # an imported model has no generated eye meshes
    assert build_json["bounds"]["height_m"] == pytest.approx(2.5, abs=0.01)
    assert not any("procedurally" in w for w in build_json["warnings"])


def _box_glb_under_rotated_parent(path: Path) -> None:
    """A rigless 1 x 2 x 1 box (tall in glTF's Y) whose mesh node hangs under a
    parent rotated 90 degrees about X -- how converted models often carry their
    orientation. In the world the box lies down: 2 units deep, 1 high."""
    positions = [
        (x, y, z) for x in (-0.5, 0.5) for y in (-1.0, 1.0) for z in (-0.5, 0.5)
    ]
    indices = [
        0,
        1,
        3,
        0,
        3,
        2,
        4,
        6,
        7,
        4,
        7,
        5,
        0,
        4,
        5,
        0,
        5,
        1,
        2,
        3,
        7,
        2,
        7,
        6,
        0,
        2,
        6,
        0,
        6,
        4,
        1,
        5,
        7,
        1,
        7,
        3,
    ]
    pos_bytes = b"".join(struct.pack("<3f", *p) for p in positions)
    idx_bytes = struct.pack(f"<{len(indices)}H", *indices)
    idx_bytes += b"\x00" * ((-len(idx_bytes)) % 4)
    blob = pos_bytes + idx_bytes
    quarter_turn = math.sin(math.pi / 4)
    doc = {
        "asset": {"version": "2.0"},
        "scene": 0,
        "scenes": [{"nodes": [0]}],
        "nodes": [
            {
                "name": "Parent",
                "rotation": [quarter_turn, 0, 0, quarter_turn],
                "children": [1],
            },
            {"name": "Box", "mesh": 0},
        ],
        "meshes": [
            {"primitives": [{"attributes": {"POSITION": 0}, "indices": 1}]}
        ],
        "accessors": [
            {
                "bufferView": 0,
                "componentType": 5126,
                "count": 8,
                "type": "VEC3",
                "min": [-0.5, -1, -0.5],
                "max": [0.5, 1, 0.5],
            },
            {
                "bufferView": 1,
                "componentType": 5123,
                "count": len(indices),
                "type": "SCALAR",
            },
        ],
        "bufferViews": [
            {"buffer": 0, "byteOffset": 0, "byteLength": len(pos_bytes)},
            {
                "buffer": 0,
                "byteOffset": len(pos_bytes),
                "byteLength": len(indices) * 2,
            },
        ],
        "buffers": [{"byteLength": len(blob)}],
    }
    json_chunk = json.dumps(doc).encode()
    json_chunk += b" " * ((-len(json_chunk)) % 4)
    total = 12 + 8 + len(json_chunk) + 8 + len(blob)
    path.write_bytes(
        b"glTF"
        + struct.pack("<II", 2, total)
        + struct.pack("<I", len(json_chunk))
        + b"JSON"
        + json_chunk
        + struct.pack("<I", len(blob))
        + b"BIN\x00"
        + blob
    )


def test_rigless_model_keeps_the_orientation_its_parent_nodes_carry(tmp_path):
    """The model's world shape, not its raw vertices, is what gets scaled and
    bound to the rig; losing a parent's rotation stood this box up instead of
    laying it down (and flattened real models: a mascot came out a disc)."""
    custom_dir = tmp_path / "deploy" / "assets" / "custom_templates"
    custom_dir.mkdir(parents=True)
    _box_glb_under_rotated_parent(custom_dir / "box.glb")

    result, _, build_json = _compile(
        _minimal_spec(base_template="custom:box", has_face=False),
        tmp_path,
        "box",
    )
    assert result.returncode == 0, result.stderr
    assert build_json["warnings"] == []
    assert build_json["bounds"]["height_m"] == pytest.approx(2.5, abs=0.02)
    assert build_json["bounds"]["depth_m"] == pytest.approx(
        5.0, abs=0.05
    )  # lying down: twice as deep as high
    assert build_json["bounds"]["width_m"] == pytest.approx(2.5, abs=0.02)


def test_model_with_a_face_map_gets_blink_and_lip_sync_overlays(tmp_path):
    """A model whose face is painted on has nothing to move. A `.face.json`
    beside it says where the eyes and mouth are, and the generator adds its
    own eyelids (blink) and nine-viseme mouth over them."""
    custom_dir = tmp_path / "deploy" / "assets" / "custom_templates"
    custom_dir.mkdir(parents=True)
    _box_glb_under_rotated_parent(custom_dir / "painted.glb")
    (custom_dir / "painted.face.json").write_text(
        json.dumps(
            {
                "skin": "#D37454",
                "eye_l": {"x": 0.3, "z": 0.7, "w": 0.15, "h": 0.2},
                "eye_r": {"x": 0.7, "z": 0.7, "w": 0.15, "h": 0.2},
                "mouth": {"x": 0.5, "z": 0.4, "w": 0.3, "h": 0.1},
            }
        )
    )
    result, out_path, build_json = _compile(
        _minimal_spec(base_template="custom:painted", has_face=True),
        tmp_path,
        "painted",
    )
    assert result.returncode == 0, result.stderr
    assert build_json["has_face"] is True
    assert build_json["speech_indicator_mode"] == "viseme_shapekeys"
    assert len(build_json["viseme_shape_keys"]) == 9
    assert build_json["blink_shape_keys"] == ["blink"]
    assert build_json["warnings"] == []

    data = out_path.read_bytes()
    gltf = json.loads(data[20 : 20 + struct.unpack("<I", data[12:16])[0]])
    by_name = {n.get("name"): n for n in gltf["nodes"]}
    for eye in ("Eye_L", "Eye_R"):
        assert (
            len(
                gltf["meshes"][by_name[eye]["mesh"]]["primitives"][0]["targets"]
            )
            == 1
        )
    assert (
        len(
            gltf["meshes"][by_name["Mouth"]["mesh"]]["primitives"][0]["targets"]
        )
        == 9
    )


def test_a_face_map_can_add_a_tongue_and_a_plain_inside_for_a_cartoon_mouth(
    tmp_path,
):
    """`tongue_color` adds a third viseme-driven mesh (the overlay drives every mesh with nine
    targets, so it follows the lip-sync), and `fill` makes the mouth's inside a plain fill with an
    oval outline instead of a strip of teeth."""
    custom_dir = tmp_path / "deploy" / "assets" / "custom_templates"
    custom_dir.mkdir(parents=True)
    _box_glb_under_rotated_parent(custom_dir / "toon.glb")
    (custom_dir / "toon.face.json").write_text(
        json.dumps(
            {
                "skin": "#4E7F78",
                "patch": False,
                "oval": True,
                "fill": True,
                "teeth_color": "#0B4F42",
                "tongue_color": "#D9534F",
                "smile": 0.6,
                "eye_l": {"x": 0.3, "z": 0.7, "w": 0.15, "h": 0.2},
                "eye_r": {"x": 0.7, "z": 0.7, "w": 0.15, "h": 0.2},
                "mouth": {"x": 0.5, "z": 0.4, "w": 0.3, "h": 0.1},
            }
        )
    )
    result, out_path, build_json = _compile(
        _minimal_spec(base_template="custom:toon", has_face=True),
        tmp_path,
        "toon",
    )
    assert result.returncode == 0, result.stderr
    assert build_json["warnings"] == []
    assert len(build_json["viseme_shape_keys"]) == 9
    data = out_path.read_bytes()
    gltf = json.loads(data[20 : 20 + struct.unpack("<I", data[12:16])[0]])
    by_name = {n.get("name"): n for n in gltf["nodes"]}
    for name in ("Mouth", "Teeth", "Tongue"):
        assert (
            len(
                gltf["meshes"][by_name[name]["mesh"]]["primitives"][0][
                    "targets"
                ]
            )
            == 9
        ), name
    assert "MouthPatch" not in by_name


def test_model_without_a_face_map_still_builds_faceless(tmp_path):
    custom_dir = tmp_path / "deploy" / "assets" / "custom_templates"
    custom_dir.mkdir(parents=True)
    _box_glb_under_rotated_parent(custom_dir / "plain.glb")
    _, _, build_json = _compile(
        _minimal_spec(base_template="custom:plain", has_face=True),
        tmp_path,
        "plain",
    )
    assert build_json["has_face"] is False
    assert build_json["blink_shape_keys"] == []


def _limb_rigged_glb(path: Path) -> None:
    """A rigged model made of two skinned meshes, `Legs` and `Arms`, on a rig with
    the usual limb bones (thigh/shin, upper_arm/forearm, left and right) under a root.
    Each box is weighted to the upper bone above its middle and the lower bone below.
    """
    joints = [
        "thigh.L",
        "shin.L",
        "thigh.R",
        "shin.R",
        "upper_arm.L",
        "forearm.L",
        "upper_arm.R",
        "forearm.R",
        "root",
    ]
    world = {
        "thigh.L": (-0.2, 1.0, 0),
        "shin.L": (-0.2, 0.5, 0),
        "thigh.R": (0.2, 1.0, 0),
        "shin.R": (0.2, 0.5, 0),
        "upper_arm.L": (-0.4, 1.8, 0),
        "forearm.L": (-0.4, 1.4, 0),
        "upper_arm.R": (0.4, 1.8, 0),
        "forearm.R": (0.4, 1.4, 0),
        "root": (0, 0, 0),
    }
    parents = {
        "thigh.L": "root",
        "thigh.R": "root",
        "upper_arm.L": "root",
        "upper_arm.R": "root",
        "shin.L": "thigh.L",
        "shin.R": "thigh.R",
        "forearm.L": "upper_arm.L",
        "forearm.R": "upper_arm.R",
    }
    nodes: list[dict[str, Any]] = [
        {"name": name} for name in ("root", *[j for j in joints if j != "root"])
    ]
    index = {node["name"]: i for i, node in enumerate(nodes)}
    for name, node in zip((n["name"] for n in nodes), nodes):
        parent = parents.get(name)
        origin = world[parent] if parent else (0, 0, 0)
        node["translation"] = [world[name][k] - origin[k] for k in range(3)]
    for name, parent in parents.items():
        nodes[index[parent]].setdefault("children", []).append(index[name])
    triangles = [
        0,
        1,
        3,
        0,
        3,
        2,
        4,
        6,
        7,
        4,
        7,
        5,
        0,
        4,
        5,
        0,
        5,
        1,
        2,
        3,
        7,
        2,
        7,
        6,
        0,
        2,
        6,
        0,
        6,
        4,
        1,
        5,
        7,
        1,
        7,
        3,
    ]

    blob = bytearray()
    views, accessors = [], []

    def add(data: bytes, **accessor) -> int:
        while len(blob) % 4:
            blob.append(0)
        views.append(
            {"buffer": 0, "byteOffset": len(blob), "byteLength": len(data)}
        )
        blob.extend(data)
        accessors.append({"bufferView": len(views) - 1, **accessor})
        return len(accessors) - 1

    def mesh_of(boxes: list) -> dict:
        positions, joint_ids = [], []
        for cx, y0, y1, upper, lower in boxes:
            for x in (cx - 0.05, cx + 0.05):
                for y in (y0, y1):
                    for z in (-0.05, 0.05):
                        positions.append((x, y, z))
                        joint_ids.append(
                            joints.index(upper if y > (y0 + y1) / 2 else lower)
                        )
        count = len(positions)
        all_triangles = [
            t + 8 * b for b in range(len(boxes)) for t in triangles
        ]
        pos = add(
            b"".join(struct.pack("<3f", *p) for p in positions),
            componentType=5126,
            count=count,
            type="VEC3",
            min=[min(p[k] for p in positions) for k in range(3)],
            max=[max(p[k] for p in positions) for k in range(3)],
        )
        idx = add(
            struct.pack(f"<{len(all_triangles)}H", *all_triangles),
            componentType=5123,
            count=len(all_triangles),
            type="SCALAR",
        )
        jnt = add(
            b"".join(struct.pack("<4B", j, 0, 0, 0) for j in joint_ids),
            componentType=5121,
            count=count,
            type="VEC4",
        )
        wgt = add(
            b"".join(struct.pack("<4f", 1, 0, 0, 0) for _ in positions),
            componentType=5126,
            count=count,
            type="VEC4",
        )
        return {
            "primitives": [
                {
                    "attributes": {
                        "POSITION": pos,
                        "JOINTS_0": jnt,
                        "WEIGHTS_0": wgt,
                    },
                    "indices": idx,
                }
            ]
        }

    meshes = [
        mesh_of(
            [
                (-0.2, 0.0, 1.0, "thigh.L", "shin.L"),
                (0.2, 0.0, 1.0, "thigh.R", "shin.R"),
            ]
        ),
        mesh_of(
            [
                (-0.4, 1.0, 1.8, "upper_arm.L", "forearm.L"),
                (0.4, 1.0, 1.8, "upper_arm.R", "forearm.R"),
            ]
        ),
    ]
    inverse_bind = add(
        b"".join(
            struct.pack(
                "<16f",
                1,
                0,
                0,
                0,
                0,
                1,
                0,
                0,
                0,
                0,
                1,
                0,
                *(-c for c in world[j]),
                1,
            )
            for j in joints
        ),
        componentType=5126,
        count=len(joints),
        type="MAT4",
    )
    nodes += [
        {"name": "Legs", "mesh": 0, "skin": 0},
        {"name": "Arms", "mesh": 1, "skin": 0},
    ]
    doc = {
        "asset": {"version": "2.0"},
        "scene": 0,
        "scenes": [{"nodes": [0, len(nodes) - 2, len(nodes) - 1]}],
        "nodes": nodes,
        "meshes": meshes,
        "skins": [
            {
                "joints": [index[j] for j in joints],
                "inverseBindMatrices": inverse_bind,
            }
        ],
        "accessors": accessors,
        "bufferViews": views,
        "buffers": [{"byteLength": len(blob)}],
    }
    body = json.dumps(doc).encode()
    body += b" " * ((-len(body)) % 4)
    total = 12 + 8 + len(body) + 8 + len(blob)
    path.write_bytes(
        b"glTF"
        + struct.pack("<II", 2, total)
        + struct.pack("<I", len(body))
        + b"JSON"
        + body
        + struct.pack("<I", len(blob))
        + b"BIN\x00"
        + bytes(blob)
    )


def test_rigged_model_gets_run_and_gesture_targets_from_its_limb_bones(
    tmp_path,
):
    """The loader the overlay uses does not apply skeletal clips, so a rig's limb
    motion is baked as morph targets: run poses on the meshes the legs deform and
    arm gestures on the ones the arms deform, found by bone name."""
    custom_dir = tmp_path / "deploy" / "assets" / "custom_templates"
    custom_dir.mkdir(parents=True)
    _limb_rigged_glb(custom_dir / "walker.glb")

    result, out_path, build_json = _compile(
        _minimal_spec(base_template="custom:walker", has_face=False),
        tmp_path,
        "walker",
    )
    assert result.returncode == 0, result.stderr
    assert build_json["run_shape_keys"] == ["run_a", "run_b"]
    assert build_json["gesture_shape_keys"] == [
        "gesture_l_out",
        "gesture_l_in",
        "gesture_r_out",
        "gesture_r_in",
    ]

    data = out_path.read_bytes()
    gltf = json.loads(data[20 : 20 + struct.unpack("<I", data[12:16])[0]])
    by_name = {n.get("name"): n for n in gltf["nodes"]}
    targets = lambda name: len(
        gltf["meshes"][by_name[name]["mesh"]]["primitives"][0].get(
            "targets", []
        )
    )
    assert targets("Legs") == 2
    # This rig's arms are modelled straight out, so they are relaxed to hang and then swing in the
    # run poses: the arms mesh carries the two run targets and then the four gesture targets.
    assert targets("Arms") == 6
    names = next(
        m["extras"]["targetNames"]
        for m in gltf["meshes"]
        if len(m.get("extras", {}).get("targetNames", [])) == 6
    )
    assert names == [
        "run_a",
        "run_b",
        "gesture_l_out",
        "gesture_l_in",
        "gesture_r_out",
        "gesture_r_in",
    ]


def test_rigged_model_without_limb_bones_gets_no_limb_targets(tmp_path):
    """A rig whose bones are not named like limbs (this generator's own rigs have one
    `arm_L`/`leg_L` bone each) is left as it is, with a warning."""
    _, source_glb, _ = _compile(
        _minimal_spec(base_template="standard_humanoid_female"),
        tmp_path,
        "source",
    )
    custom_dir = tmp_path / "deploy" / "assets" / "custom_templates"
    custom_dir.mkdir(parents=True)
    shutil.copy(source_glb, custom_dir / "robot.glb")

    _, _, build_json = _compile(
        _minimal_spec(base_template="custom:robot"), tmp_path, "robot"
    )
    assert build_json["run_shape_keys"] == []
    assert build_json["gesture_shape_keys"] == []
    assert any("thigh/shin" in w for w in build_json["warnings"])


def test_installed_model_without_a_face_builds_faceless(tmp_path):
    """A model with no viseme shape keys is built with the sound_waves speech
    indicator, and the build's own has_face says so."""
    _, source_glb, _ = _compile(
        _minimal_spec(base_template="primitive_mechanical", has_face=False),
        tmp_path,
        "source",
    )
    custom_dir = tmp_path / "deploy" / "assets" / "custom_templates"
    custom_dir.mkdir(parents=True)
    shutil.copy(source_glb, custom_dir / "bot.glb")

    result, _, build_json = _compile(
        _minimal_spec(base_template="custom:bot", has_face=True),
        tmp_path,
        "used",
    )
    assert result.returncode == 0, result.stderr
    assert build_json["has_face"] is False
    assert build_json["speech_indicator_mode"] == "sound_waves"
    assert any("no viseme shape keys" in w for w in build_json["warnings"])


def test_unusable_installed_model_falls_back_to_procedural_with_warning(
    tmp_path,
):
    """A model file with no mesh in it must not fail the build."""
    base_templates_dir = tmp_path / "deploy" / "assets" / "base_templates"
    base_templates_dir.mkdir(parents=True)
    (base_templates_dir / "standard_humanoid_male.glb").write_bytes(
        _minimal_valid_glb_bytes()
    )

    result, out_path, build_json = _compile(
        _minimal_spec(base_template="standard_humanoid_male"), tmp_path
    )
    assert result.returncode == 0, result.stderr
    assert build_json["status"] == "ok"
    assert any(
        "building procedurally instead" in w for w in build_json["warnings"]
    )
    assert build_json["capabilities"]["has_legs"] is True
    assert build_json["size_bytes"] == out_path.stat().st_size


def test_inspection_cli(tmp_path):
    # First compile a real .glb, then --inspect it as if it were a Tier-1 custom asset.
    _, glb_path, _ = _compile(_minimal_spec(), tmp_path)
    inspect_out = tmp_path / "capabilities.json"
    result = _run_generator(
        "--inspect", str(glb_path), "--out", str(inspect_out)
    )
    assert result.returncode == 0, result.stderr
    assert "DESKMATE_BUILD_OK" in result.stdout
    payload = json.loads(inspect_out.read_text())
    assert payload["tier"] == "tier_1_custom"
    assert payload["has_face"] is True
    assert set(payload["viseme_shape_keys"]) == {
        f"viseme_{c}" for c in "ABCDEFGHX"
    }
    assert "spine" in payload["bone_names"]
    assert payload["triangle_count"] > 0


def test_proportion_baking(tmp_path):
    base = _compile(
        _minimal_spec(
            proportions={
                "muscularity": 0.5,
                "femininity": 0.5,
                "height": 1.0,
                "build": 1.0,
            }
        ),
        tmp_path,
        "base",
    )[2]
    tall = _compile(
        _minimal_spec(
            proportions={
                "muscularity": 0.5,
                "femininity": 0.5,
                "height": 1.4,
                "build": 1.0,
            }
        ),
        tmp_path,
        "tall",
    )[2]
    assert tall["bounds"]["height_m"] > base["bounds"]["height_m"]


def test_wardrobe_fit(tmp_path):
    spec = _minimal_spec(
        wardrobe={"outfit": "hoodie", "footwear": "sneakers"},
        accessories={"socket_eyes": "glasses", "socket_head_top": "pointy_hat"},
    )
    result, _, build_json = _compile(spec, tmp_path)
    assert result.returncode == 0, result.stderr
    assert build_json["status"] == "ok"
    # accessories/wardrobe aren't named individually in build.json, but a
    # higher triangle count than the bare-body case confirms geometry was
    # actually added, not silently skipped.
    bare = _compile(_minimal_spec(), tmp_path, "bare")[2]
    assert build_json["evaluated_triangles"] > bare["evaluated_triangles"]


def test_faceless_model_gets_sound_waves_and_no_pulse_clip(tmp_path):
    spec = _minimal_spec(base_template="primitive_mechanical", has_face=False)
    result, _, build_json = _compile(spec, tmp_path)
    assert result.returncode == 0, result.stderr
    assert build_json["has_face"] is False
    assert build_json["speech_indicator_mode"] == "sound_waves"
    assert "talk_pulse" not in {
        c["name"] for c in build_json["animation_clips"]
    }
    assert build_json["viseme_shape_keys"] == []


def test_state_loops(tmp_path):
    # primitive_blob: no arms, no legs -- move/think must use their
    # degraded (hop / spine-pulse) fallbacks, and arm-only gestures must be omitted.
    _, _, blob_json = _compile(_minimal_spec(), tmp_path, "blob")
    clip_names = {c["name"] for c in blob_json["animation_clips"]}
    assert "move" in clip_names
    assert "think" in clip_names
    assert "greet" not in clip_names
    assert "shrug" not in clip_names

    # primitive_mechanical: has arms, no legs -- greet/shrug must be present.
    _, _, mech_json = _compile(
        _minimal_spec(base_template="primitive_mechanical"), tmp_path, "mech"
    )
    mech_clip_names = {c["name"] for c in mech_json["animation_clips"]}
    assert "greet" in mech_clip_names
    assert "shrug" in mech_clip_names


def test_environment_isolation(tmp_path):
    """--factory-startup must mean the generator doesn't depend on (or get
    polluted by) a user's local Blender preferences/addons -- verified here
    by confirming a clean run from an isolated subprocess environment with
    PYTHONPATH/VIRTUAL_ENV stripped produces the same successful result as a
    normal run, i.e. the script carries everything it needs via its own
    repo-root sys.path.insert()."""
    import os

    env = {
        k: v
        for k, v in os.environ.items()
        if k not in ("PYTHONPATH", "VIRTUAL_ENV")
    }
    spec_path = tmp_path / "look.json"
    spec_path.write_text(json.dumps(_minimal_spec()))
    out_path = tmp_path / "look.glb"
    result = subprocess.run(
        [
            BLENDER_BIN,
            "--background",
            "--factory-startup",
            "--python",
            str(GENERATOR),
            "--",
            "--spec",
            str(spec_path),
            "--out",
            str(out_path),
            "--deploy-dir",
            str(tmp_path / "deploy"),
        ],
        capture_output=True,
        text=True,
        timeout=90,
        env=env,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert out_path.is_file()


def test_budget_enforcement(tmp_path, monkeypatch):
    """Rather than authoring a 30k+ triangle spec (expensive and fragile),
    confirms the budget-exceeded path is real by temporarily lowering
    generator.py's own TRIANGLE_BUDGET via an env-free, file-level patch:
    a tiny wrapper script that imports the module and overrides the constant
    before calling main() -- the generator has no CLI flag for this (budgets
    are deliberately fixed per spec), so this is the only way to exercise the
    failure path without an enormous, slow real mesh."""
    wrapper = tmp_path / "budget_wrapper.py"
    wrapper.write_text(
        "import sys\n"
        f"sys.path.insert(0, {str(REPO_ROOT)!r})\n"
        "import blender.generator as g\n"
        "g.TRIANGLE_BUDGET = 10\n"
        "g.main()\n"
    )
    spec_path = tmp_path / "look.json"
    spec_path.write_text(json.dumps(_minimal_spec()))
    out_path = tmp_path / "look.glb"
    result = subprocess.run(
        [
            BLENDER_BIN,
            "--background",
            "--factory-startup",
            "--python",
            str(wrapper),
            "--",
            "--spec",
            str(spec_path),
            "--out",
            str(out_path),
            "--deploy-dir",
            str(tmp_path / "deploy"),
        ],
        capture_output=True,
        text=True,
        timeout=90,
        check=False,
    )
    assert "DESKMATE_BUILD_FAIL budget_exceeded" in result.stdout
    assert result.returncode == 1
    build_json = json.loads(out_path.with_suffix(".build.json").read_text())
    assert build_json["status"] == "fail"
    assert not out_path.is_file()  # export_glb never ran


def test_seed_determinism(tmp_path):
    """See module docstring: Blender's exporter reorders multi-mesh buffer
    data non-deterministically across process runs, so this asserts
    structural/semantic equality rather than a byte-exact SHA256 match."""
    _, _, a = _compile(_minimal_spec(seed=99), tmp_path, "det_a")
    _, _, b = _compile(_minimal_spec(seed=99), tmp_path, "det_b")

    assert a["evaluated_triangles"] == b["evaluated_triangles"]
    assert a["viseme_shape_keys"] == b["viseme_shape_keys"]
    assert a["animation_clips"] == b["animation_clips"]
    assert a["bounds"] == b["bounds"]
    assert a["capabilities"] == b["capabilities"]


def _install_body(tmp_path: Path, name: str = "robot") -> Path:
    """A body that is an installed model: this generator's own humanoid build, put
    under custom_templates (its bones are named root/spine/head)."""
    _, source_glb, _ = _compile(
        _minimal_spec(base_template="standard_humanoid_female"),
        tmp_path,
        "source",
    )
    custom_dir = tmp_path / "deploy" / "assets" / "custom_templates"
    custom_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy(source_glb, custom_dir / f"{name}.glb")
    return source_glb


def test_inspect_reports_which_bone_carries_each_socket(tmp_path):
    source = _install_body(tmp_path)
    out = tmp_path / "caps.json"
    result = _run_generator("--inspect", str(source), "--out", str(out))
    assert result.returncode == 0, result.stderr
    caps = json.loads(out.read_text())
    assert (
        caps["socket_bones"]["socket_head_top"] == "head"
        and caps["socket_bones"]["socket_chest"] == "spine"
    )
    assert (
        "socket_hand_R" not in caps["supported_sockets"]
    )  # no hand bones in this rig
    assert caps["capabilities"]["has_head"] is True


def test_installed_body_wears_accessories_on_their_bones_and_warns_for_missing_ones(
    tmp_path,
):
    _install_body(tmp_path)
    spec = _minimal_spec(
        base_template="custom:robot",
        accessories={
            "socket_head_top": "pointy_hat",
            "socket_eyes": "glasses",
            "socket_hand_R": "wand",
        },
    )
    result, out_path, build_json = _compile(spec, tmp_path, "worn")
    assert result.returncode == 0, result.stderr
    warnings = " ".join(build_json["warnings"])
    assert (
        "socket_hand_R" in warnings
        and "socket_head_top" not in warnings
        and "socket_eyes" not in warnings
    )

    data = out_path.read_bytes()
    gltf = json.loads(data[20 : 20 + struct.unpack("<I", data[12:16])[0]])
    nodes = {n.get("name"): n for n in gltf["nodes"]}
    assert (
        "skin" in nodes["Accessory_socket_head_top_pointy_hat"]
    )  # bound to the rig, not loose


def _static_model_glb(path: Path, size: float) -> None:
    """A plain one-mesh model (a cube `size` metres wide, origin at its base) as an
    accessory author would export it."""
    code = (
        "import bpy;"
        "bpy.ops.wm.read_factory_settings(use_empty=True);"
        f"bpy.ops.mesh.primitive_cube_add(size={size}, location=(0, 0, {size / 2}));"
        f"bpy.ops.export_scene.gltf(filepath={str(path)!r})"
    )
    subprocess.run(
        [
            BLENDER_BIN,
            "--background",
            "--factory-startup",
            "--python-expr",
            code,
        ],
        capture_output=True,
        check=True,
        timeout=60,
    )


def test_accessory_model_is_placed_scaled_and_bound_from_its_sidecar(tmp_path):
    _install_body(tmp_path)
    folder = tmp_path / "deploy" / "assets" / "wardrobe" / "socket_head_top"
    folder.mkdir(parents=True)
    _static_model_glb(
        folder / "big_hat.glb", size=4.0
    )  # as large as the whole figure
    (folder / "big_hat.json").write_text(json.dumps({"scale": 0.1}))

    spec = _minimal_spec(
        base_template="custom:robot", accessories={"socket_head_top": "big_hat"}
    )
    result, out_path, build_json = _compile(spec, tmp_path, "modelhat")
    assert result.returncode == 0, result.stderr
    data = out_path.read_bytes()
    gltf = json.loads(data[20 : 20 + struct.unpack("<I", data[12:16])[0]])
    nodes = {n.get("name"): n for n in gltf["nodes"]}
    assert "skin" in nodes["Accessory_socket_head_top_big_hat"]
    # scale 0.1 makes the 4 m cube a 0.4 m hat on a 2.5 m figure; unscaled it would double the height
    assert build_json["bounds"]["height_m"] < 3.2


def _glb_gltf(path: Path) -> dict:
    data = path.read_bytes()
    return json.loads(data[20 : 20 + struct.unpack("<I", data[12:16])[0]])


def _skinned_mesh_nodes(path: Path) -> list[str]:
    return [
        n["name"]
        for n in _glb_gltf(path)["nodes"]
        if "skin" in n and "mesh" in n
    ]


def _install_outfit(
    tmp_path: Path,
    source_glb: Path,
    name: str = "suit",
    body: str = "custom_robot",
) -> None:
    folder = tmp_path / "deploy" / "assets" / "outfit_models" / body
    folder.mkdir(parents=True, exist_ok=True)
    shutil.copy(source_glb, folder / f"{name}.glb")


def test_installed_body_wears_an_outfit_model_on_its_own_skeleton(tmp_path):
    source = _install_body(tmp_path)
    _install_outfit(
        tmp_path, source
    )  # an outfit skinned to the same rig (here, the body itself)
    bare, bare_path, _ = _compile(
        _minimal_spec(base_template="custom:robot"), tmp_path, "bare"
    )
    assert bare.returncode == 0, bare.stderr

    spec = _minimal_spec(
        base_template="custom:robot",
        wardrobe={"outfit": "suit", "footwear": "none"},
    )
    result, out_path, build_json = _compile(spec, tmp_path, "dressed")
    assert result.returncode == 0, result.stderr
    assert not [
        w for w in build_json["warnings"] if "outfit" in w or "not layered" in w
    ], build_json["warnings"]
    assert len(_skinned_mesh_nodes(out_path)) > len(
        _skinned_mesh_nodes(bare_path)
    )
    assert (
        len(_glb_gltf(out_path)["skins"]) == 1
    )  # all of it hangs on the body's one skeleton


def test_outfit_that_cannot_be_worn_is_skipped_with_a_warning(tmp_path):
    _install_body(tmp_path)
    static = tmp_path / "cube.glb"
    _static_model_glb(
        static, size=1.0
    )  # no rig, so it cannot be bound to the body's skeleton
    _install_outfit(tmp_path, static)

    spec = _minimal_spec(
        base_template="custom:robot",
        wardrobe={"outfit": "suit", "footwear": "none"},
    )
    result, _, build_json = _compile(spec, tmp_path, "skipped")
    assert result.returncode == 0, result.stderr
    assert any(
        "outfit suit" in w and "not worn" in w for w in build_json["warnings"]
    ), build_json["warnings"]


def _triangles(path: Path) -> int:
    gltf = _glb_gltf(path)
    return sum(
        gltf["accessors"][prim["indices"]]["count"] // 3
        for mesh in gltf["meshes"]
        for prim in mesh["primitives"]
        if "indices" in prim
    )


def test_convert_mode_reencodes_a_model_and_decimates_to_a_triangle_cap(
    tmp_path,
):
    source = _install_body(tmp_path)
    out = tmp_path / "converted.glb"
    full = _run_generator("--convert", str(source), "--out", str(out))
    assert full.returncode == 0 and out.is_file(), full.stderr
    full_triangles = _triangles(out)
    assert (
        full_triangles > 1_500
    )  # a cap below this will have something to remove

    capped = tmp_path / "capped.glb"
    result = _run_generator(
        "--convert",
        str(source),
        "--out",
        str(capped),
        "--max-triangles",
        "1500",
    )
    assert result.returncode == 0 and capped.is_file(), result.stderr
    assert _triangles(capped) < full_triangles


def test_face_landmarks_mode_finds_the_eyes_and_puts_the_mouth_below_them(
    tmp_path,
):
    source = _install_body(tmp_path)  # this generator's own figure: it has eyes
    out = tmp_path / "face.json"
    result = _run_generator("--face-landmarks", str(source), "--out", str(out))
    assert result.returncode == 0, result.stderr
    face = json.loads(out.read_text())
    assert (
        face["eye_l"]["x"] < face["eye_r"]["x"]
    )  # the same left/right convention as a hand-made sidecar
    assert (
        face["mouth"]["z"] < face["eye_l"]["z"] and 0 < face["mouth"]["z"] < 1
    )
    assert (
        abs(face["mouth"]["x"] - (face["eye_l"]["x"] + face["eye_r"]["x"]) / 2)
        < 0.02
    )  # on the face's midline
    assert (
        face["patch"] is False
    )  # real lips are kept; the mouth is drawn over them


def test_face_landmarks_mode_says_so_when_the_model_has_no_eyes(tmp_path):
    static = tmp_path / "cube.glb"
    _static_model_glb(static, size=1.0)
    result = _run_generator(
        "--face-landmarks", str(static), "--out", str(tmp_path / "face.json")
    )
    assert not (
        tmp_path / "face.json"
    ).exists()  # (blender exits 0 when a script raises; no file is the signal)
    assert "no eye mesh found" in result.stderr + result.stdout


def test_a_sidecar_with_no_patch_keeps_the_lips_and_draws_an_oval_mouth(
    tmp_path,
):
    source = _install_body(tmp_path)
    out = tmp_path / "face.json"
    assert (
        _run_generator(
            "--face-landmarks", str(source), "--out", str(out)
        ).returncode
        == 0
    )
    body = tmp_path / "deploy" / "assets" / "custom_templates" / "robot.glb"
    shutil.copy(out, body.with_name("robot.face.json"))
    # the installed copy has visemes of its own, so build it without them via a spec that asks for a face
    spec = _minimal_spec(base_template="custom:robot", has_face=True)
    result, out_path, build_json = _compile(spec, tmp_path, "lips")
    assert result.returncode == 0, result.stderr
    assert (
        build_json["has_face"] is True
        and len(build_json["viseme_shape_keys"]) == 9
    )
    names = {n.get("name") for n in _glb_gltf(out_path)["nodes"]}
    assert "MouthPatch" not in names and "Mouth" in names


def test_glasses_on_an_installed_body_are_centred_on_its_eyes_and_sized_by_their_spacing(
    tmp_path,
):
    source = _install_body(tmp_path)  # a figure with a pair of eyes
    face = tmp_path / "face.json"
    assert (
        _run_generator(
            "--face-landmarks", str(source), "--out", str(face)
        ).returncode
        == 0
    )
    eyes = json.loads(face.read_text())

    spec = _minimal_spec(
        base_template="custom:robot", accessories={"socket_eyes": "glasses"}
    )
    result, out_path, build_json = _compile(spec, tmp_path, "specs")
    assert result.returncode == 0, result.stderr
    assert not [w for w in build_json["warnings"] if "socket_eyes" in w]

    # Both lenses must lie within the box the two eyes span, not float off to the sides:
    # the accessory's width is about the eyes' spacing plus a lens.
    gltf = _glb_gltf(out_path)
    node = next(
        n
        for n in gltf["nodes"]
        if (n.get("name") or "").startswith("Accessory_socket_eyes")
    )
    accessor = gltf["accessors"][
        gltf["meshes"][node["mesh"]]["primitives"][0]["attributes"]["POSITION"]
    ]
    width = accessor["max"][0] - accessor["min"][0]
    figure = build_json["bounds"]["width_m"]
    spacing = (eyes["eye_r"]["x"] - eyes["eye_l"]["x"]) * figure
    assert 1.0 * spacing < width < 1.8 * spacing, (width, spacing)


_LIMBED_MODEL_SCRIPT = """
import bpy
bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.object.armature_add(location=(0, 0, 0))
arm = bpy.context.active_object
arm.name = "Rig"
bpy.ops.object.mode_set(mode="EDIT")
eb = arm.data.edit_bones
for b in list(eb):
    eb.remove(b)
def bone(name, head, tail, parent=None):
    b = eb.new(name); b.head, b.tail = head, tail
    if parent: b.parent = eb[parent]; b.use_connect = False
    return b
bone("root", (0, 0, 0.9), (0, 0, 1.0))
bone("spine", (0, 0, 1.0), (0, 0, 1.5), "root")
bone("head", (0, 0, 1.5), (0, 0, 1.75), "spine")
for side, x in (("l", 0.25), ("r", -0.25)):
    bone(f"upperarm_{side}", (x, 0, 1.45), (x * 2.2, 0, 1.45), "spine")
    bone(f"lowerarm_{side}", (x * 2.2, 0, 1.45), (x * 3.2, 0, 1.45), f"upperarm_{side}")
    bone(f"thigh_{side}", (x * 0.4, 0, 0.9), (x * 0.4, 0, 0.5), "root")
    bone(f"calf_{side}", (x * 0.4, 0, 0.5), (x * 0.4, 0, 0.08), f"thigh_{side}")
bpy.ops.object.mode_set(mode="OBJECT")
bpy.ops.mesh.primitive_cube_add(size=1, location=(0, 0, 0.9))
body = bpy.context.active_object
body.name = "Body"
body.scale = (1.4, 0.3, 1.8)
bpy.ops.object.transform_apply(scale=True)
bpy.ops.object.mode_set(mode="EDIT")
bpy.ops.mesh.subdivide(number_cuts=6)
bpy.ops.object.mode_set(mode="OBJECT")
bpy.ops.object.select_all(action="DESELECT")
body.select_set(True); arm.select_set(True); bpy.context.view_layer.objects.active = arm
bpy.ops.object.parent_set(type="ARMATURE_AUTO")
bpy.ops.export_scene.gltf(filepath=%r)
"""


def _limbed_model_glb(
    path: Path, trunk_only: bool = False, hands: bool = False
) -> None:
    """One mesh for the whole body, skinned to a rig with arm and leg bones: the shape
    of most downloaded characters (and of the free Quaternius bodies). `trunk_only` swaps
    the spine and head for a single bone called Body, like a floating mascot's rig.
    """
    script = _LIMBED_MODEL_SCRIPT
    if hands:  # a hand bone at the end of each forearm
        script = script.replace(
            '    bone(f"thigh_{side}"',
            '    bone(f"hand_{side}", (x * 3.2, 0, 1.45), (x * 3.7, 0, 1.45), f"lowerarm_{side}")\n    bone(f"thigh_{side}"',
        )
    if trunk_only:
        script = (
            script.replace(
                'bone("spine", (0, 0, 1.0), (0, 0, 1.5), "root")',
                'bone("Body", (0, 0, 1.0), (0, 0, 1.75), "root")',
            )
            .replace('bone("head", (0, 0, 1.5), (0, 0, 1.75), "spine")\n', "")
            .replace(
                '"spine")\n    bone(f"lowerarm', '"Body")\n    bone(f"lowerarm'
            )
        )
    subprocess.run(
        [
            BLENDER_BIN,
            "--background",
            "--factory-startup",
            "--python-expr",
            script % str(path),
        ],
        capture_output=True,
        check=True,
        timeout=120,
    )


_LIBRARY_ACTIONS = """
arm.animation_data_create()
for clip in ("Idle_Loop", "Sprint_Loop", "Dance_Loop", "Sitting_Enter", "Sitting_Idle_Loop", "Sitting_Exit", "Standing_Greeting", "Standing_Greeting_Low", "Standing_Greeting_High", "Unwanted_Clip"):
    action = bpy.data.actions.new(clip)
    action.use_fake_user = True
    arm.animation_data.action = action
    pose_bone = arm.pose.bones["upperarm_l"]
    pose_bone.rotation_mode = "XYZ"
    for frame, angle in ((1, 0.0), (20, 0.6)):
        pose_bone.rotation_euler = (0.0, angle, 0.0)
        pose_bone.keyframe_insert("rotation_euler", frame=frame)
bpy.ops.export_scene.gltf(filepath=%r, export_animation_mode="ACTIONS")
"""


def _animation_library_glb(
    path: Path, trunk_only: bool = False, extra_bone: bool = False
) -> None:
    """A pack of clips made for the limbed test rig: a skeleton with four actions."""
    script = _LIMBED_MODEL_SCRIPT.replace(
        "bpy.ops.export_scene.gltf(filepath=%r)", _LIBRARY_ACTIONS.strip()
    )
    script = script.replace(
        'bone("spine", (0, 0, 1.0), (0, 0, 1.5), "root")',
        'bone("pelvis", (0, 0, 0.9), (0, 0, 1.0), "root")\nbone("spine", (0, 0, 1.0), (0, 0, 1.5), "root")',
    )
    if (
        extra_bone
    ):  # a bone the body lacks, with a track of its own on every clip
        script = script.replace(
            'bone("head", (0, 0, 1.5), (0, 0, 1.75), "spine")',
            'bone("head", (0, 0, 1.5), (0, 0, 1.75), "spine")\nbone("tail", (0, 0, 1.0), (0, -0.3, 1.0), "spine")',
        )
        script = script.replace(
            "    for frame, angle in ((1, 0.0), (20, 0.6)):",
            '    tail = arm.pose.bones["tail"]\n    tail.rotation_mode = "XYZ"\n    for frame, angle in ((1, 0.0), (20, 0.6)):\n        tail.rotation_euler = (angle, 0.0, 0.0)\n        tail.keyframe_insert("rotation_euler", frame=frame)',
        )
    if trunk_only:  # a different rig (a Body bone for the spine and head)
        script = (
            script.replace(
                'bone("spine", (0, 0, 1.0), (0, 0, 1.5), "root")',
                'bone("Body", (0, 0, 1.0), (0, 0, 1.75), "root")',
            )
            .replace('bone("head", (0, 0, 1.5), (0, 0, 1.75), "spine")\n', "")
            .replace(
                '"spine")\n    bone(f"lowerarm', '"Body")\n    bone(f"lowerarm'
            )
        )
    subprocess.run(
        [
            BLENDER_BIN,
            "--background",
            "--factory-startup",
            "--python-expr",
            script % str(path),
        ],
        capture_output=True,
        check=True,
        timeout=120,
    )


def _limbed_model_with_pelvis_glb(path: Path) -> None:
    """The limbed test body plus a pelvis bone, which a chair is placed from."""
    script = _LIMBED_MODEL_SCRIPT.replace(
        'bone("spine", (0, 0, 1.0), (0, 0, 1.5), "root")',
        'bone("pelvis", (0, 0, 0.9), (0, 0, 1.0), "root")\nbone("spine", (0, 0, 1.0), (0, 0, 1.5), "root")',
    )
    subprocess.run(
        [
            BLENDER_BIN,
            "--background",
            "--factory-startup",
            "--python-expr",
            script % str(path),
        ],
        capture_output=True,
        check=True,
        timeout=120,
    )


def test_a_body_with_a_librarys_bones_plays_the_clips_it_gives_and_gets_no_morph_limb_poses(
    tmp_path,
):
    deploy = tmp_path / "deploy" / "assets"
    (deploy / "custom_templates").mkdir(parents=True)
    (deploy / "animation_library").mkdir()
    _limbed_model_with_pelvis_glb(deploy / "custom_templates" / "limbed.glb")
    _animation_library_glb(deploy / "animation_library" / "pack.glb")
    install_example_packs(
        deploy.parent
    )  # the example packs are the install's own profiles

    result, out_path, build_json = _compile(
        _minimal_spec(base_template="custom:limbed", has_face=False),
        tmp_path,
        "library",
    )
    assert result.returncode == 0, result.stderr
    assert build_json["library_clips"] == {
        "idle": "Idle_Loop",
        "run": "Sprint_Loop",
        "celebrate": "Dance_Loop",
        "sit_enter": "Sitting_Enter",
        "sit_idle": "Sitting_Idle_Loop",
        "sit_exit": "Sitting_Exit",
        "sit_prop": "SitProp",
        "greet": "Standing_Greeting",
        "greet.low": "Standing_Greeting_Low",
        "greet.high": "Standing_Greeting_High",  # a clip with intensity variants
    }
    gltf_nodes = {node.get("name") for node in _glb_gltf(out_path)["nodes"]}
    assert "SitProp" in gltf_nodes  # the chair the sitting clips are for
    clips = {anim["name"] for anim in _glb_gltf(out_path)["animations"]}
    assert {"Idle_Loop", "Sprint_Loop", "Dance_Loop"} <= clips
    assert (
        "Unwanted_Clip" not in clips
    )  # only the clips that fill a role are taken
    assert (
        build_json["run_shape_keys"] == []
        and build_json["gesture_shape_keys"] == []
    )  # the clips move the limbs
    assert (
        build_json["skeletal_clips"] is False
    )  # and they replace the baked head idles
    assert not [
        w for w in build_json["warnings"] if "library" in w or "chair" in w
    ], build_json["warnings"]

    # the idle loop is played slower than it was made (the pack's breathes about 48 times a minute)
    gltf = _glb_gltf(out_path)
    seconds = {
        anim["name"]: max(
            gltf["accessors"][sampler["input"]]["max"][0]
            for sampler in anim["samplers"]
        )
        for anim in gltf["animations"]
    }
    assert seconds["Idle_Loop"] == pytest.approx(
        seconds["Sprint_Loop"] * 2.5, rel=0.05
    ), seconds


def test_a_body_without_the_core_bones_gets_no_library_at_all(tmp_path):
    deploy = tmp_path / "deploy" / "assets"
    (deploy / "custom_templates").mkdir(parents=True)
    (deploy / "animation_library").mkdir()
    _limbed_model_glb(
        deploy / "custom_templates" / "limbed.glb"
    )  # no pelvis bone
    _animation_library_glb(deploy / "animation_library" / "pack.glb")

    result, out_path, build_json = _compile(
        _minimal_spec(base_template="custom:limbed", has_face=False),
        tmp_path,
        "no_chair",
    )
    assert result.returncode == 0, result.stderr
    assert (
        build_json["library_clips"] == {}
    )  # no pelvis: nothing to hang the hips' clips on, and no chair to place
    assert "SitProp" not in {
        node.get("name") for node in _glb_gltf(out_path)["nodes"]
    }


def test_a_body_missing_a_bone_of_the_library_plays_it_without_that_bones_track(
    tmp_path,
):
    deploy = tmp_path / "deploy" / "assets"
    (deploy / "custom_templates").mkdir(parents=True)
    (deploy / "animation_library").mkdir()
    _limbed_model_with_pelvis_glb(
        deploy / "custom_templates" / "limbed.glb"
    )  # no tail
    _animation_library_glb(
        deploy / "animation_library" / "pack.glb", extra_bone=True
    )

    result, out_path, build_json = _compile(
        _minimal_spec(base_template="custom:limbed", has_face=False),
        tmp_path,
        "partial",
    )
    assert result.returncode == 0, result.stderr
    assert build_json["library_clips"]["idle"] == "Idle_Loop"
    gltf = _glb_gltf(out_path)
    names = [node.get("name") for node in gltf["nodes"]]
    idle = next(a for a in gltf["animations"] if a["name"] == "Idle_Loop")
    animated = {
        names[channel["target"]["node"]] for channel in idle["channels"]
    }
    assert (
        "upperarm_l" in animated
        and "tail" not in animated
        and "tail" not in names
    )


def test_a_library_made_for_another_rig_is_left_alone(tmp_path):
    deploy = tmp_path / "deploy" / "assets"
    (deploy / "custom_templates").mkdir(parents=True)
    (deploy / "animation_library").mkdir()
    _limbed_model_glb(deploy / "custom_templates" / "limbed.glb")
    _animation_library_glb(
        deploy / "animation_library" / "pack.glb", trunk_only=True
    )

    result, out_path, build_json = _compile(
        _minimal_spec(base_template="custom:limbed", has_face=False),
        tmp_path,
        "other_rig",
    )
    assert result.returncode == 0, result.stderr
    assert build_json["library_clips"] == {}
    assert "Idle_Loop" not in {
        anim["name"] for anim in _glb_gltf(out_path)["animations"]
    }
    assert build_json["run_shape_keys"] == [
        "run_a",
        "run_b",
    ]  # it keeps the morph-target limb poses


def test_a_single_mesh_body_gets_run_and_gesture_targets_in_that_order(
    tmp_path,
):
    folder = tmp_path / "deploy" / "assets" / "custom_templates"
    folder.mkdir(parents=True)
    _limbed_model_glb(folder / "limbed.glb")

    result, out_path, build_json = _compile(
        _minimal_spec(base_template="custom:limbed", has_face=False),
        tmp_path,
        "limbed",
    )
    assert result.returncode == 0, result.stderr
    assert build_json["run_shape_keys"] == ["run_a", "run_b"]
    assert build_json["gesture_shape_keys"] == [
        "gesture_l_out",
        "gesture_l_in",
        "gesture_r_out",
        "gesture_r_in",
    ]
    assert not [
        w for w in build_json["warnings"] if "left out" in w
    ], build_json["warnings"]

    gltf = _glb_gltf(out_path)
    # the overlay reads a mesh's targets by count and order: 2 run, then 4 gesture
    counts = {
        len(prim["targets"])
        for mesh in gltf["meshes"]
        for prim in mesh["primitives"]
        if prim.get("targets")
    }
    assert 6 in counts, counts
    names = next(
        mesh["extras"]["targetNames"]
        for mesh in gltf["meshes"]
        if mesh.get("extras", {}).get("targetNames")
        and len(mesh["extras"]["targetNames"]) == 6
    )
    assert names == [
        "run_a",
        "run_b",
        "gesture_l_out",
        "gesture_l_in",
        "gesture_r_out",
        "gesture_r_in",
    ]


def _animation_targets(gltf: dict) -> dict[str, set[str]]:
    """{clip name: names of the nodes (bones) it animates}."""
    return {
        anim["name"]: {
            gltf["nodes"][ch["target"]["node"]].get("name")
            for ch in anim["channels"]
        }
        for anim in gltf.get("animations", [])
    }


def test_an_imported_rig_gets_idle_clips_on_its_own_head_bone_and_says_so(
    tmp_path,
):
    folder = tmp_path / "deploy" / "assets" / "custom_templates"
    folder.mkdir(parents=True)
    _limbed_model_glb(
        folder / "limbed.glb"
    )  # bones root, spine, head, arms and legs

    result, out_path, build_json = _compile(
        _minimal_spec(base_template="custom:limbed", has_face=False),
        tmp_path,
        "skeletal",
    )
    assert result.returncode == 0, result.stderr
    assert build_json["skeletal_clips"] is True
    names = {c["name"] for c in build_json["animation_clips"]}
    assert {"idle_lookaround", "idle_peek", "agree", "disagree"} <= names
    assert not [
        w for w in build_json["warnings"] if "animations are empty" in w
    ], build_json["warnings"]
    targets = _animation_targets(_glb_gltf(out_path))
    assert (
        "head" in targets["idle_lookaround"]
    )  # the model's own head bone, not a canonical stand-in
    assert (
        len(
            [
                a
                for a in _glb_gltf(out_path)["animations"]
                if a["name"] == "idle_lookaround"
            ]
        )
        == 1
    )  # not also the canonical one

    # the brain refuses a build that lacks any core clip (it once rejected exactly this build)
    from brain.appearance.blender_gen import _verify_build_output

    _verify_build_output(out_path, build_json, build_json["has_face"])


def test_a_generated_body_keeps_its_canonical_clips_and_is_not_marked_skeletal(
    tmp_path,
):
    result, _, build_json = _compile(_minimal_spec(), tmp_path, "generated")
    assert result.returncode == 0, result.stderr
    assert build_json["skeletal_clips"] is False
    assert "idle_lookaround" in {
        c["name"] for c in build_json["animation_clips"]
    }


def test_a_rig_whose_trunk_stands_in_for_its_head_gets_no_skeletal_idles(
    tmp_path,
):
    """A mascot rig can have a bone called Body and no head: turning it would swivel the whole figure."""
    folder = tmp_path / "deploy" / "assets" / "custom_templates"
    folder.mkdir(parents=True)
    _limbed_model_glb(folder / "mascot.glb", trunk_only=True)

    result, out_path, build_json = _compile(
        _minimal_spec(base_template="custom:mascot", has_face=False),
        tmp_path,
        "mascot",
    )
    assert result.returncode == 0, result.stderr
    assert build_json["skeletal_clips"] is False
    assert "idle_lookaround" in {
        c["name"] for c in build_json["animation_clips"]
    }  # the canonical catalog is still listed
    targets = _animation_targets(_glb_gltf(out_path))
    assert "Body" not in targets.get(
        "idle_lookaround", set()
    )  # and nothing turns the trunk


def _node_world_position(
    gltf: dict, index: int, parents: dict[int, int]
) -> tuple[float, float, float]:
    """World position of a node from the glTF's own translations and rotations (no scale
    handling beyond what the nodes carry), composed up the parent chain."""

    def rotate(q, v):
        x, y, z, w = q
        # v' = v + 2w(q x v) + 2 q x (q x v)
        cx, cy, cz = (
            y * v[2] - z * v[1],
            z * v[0] - x * v[2],
            x * v[1] - y * v[0],
        )
        dx, dy, dz = y * cz - z * cy, z * cx - x * cz, x * cy - y * cx
        return (
            v[0] + 2 * (w * cx + dx),
            v[1] + 2 * (w * cy + dy),
            v[2] + 2 * (w * cz + dz),
        )

    chain = [index]
    while chain[-1] in parents:
        chain.append(parents[chain[-1]])
    position = (0.0, 0.0, 0.0)
    for (
        i
    ) in chain:  # from the node up to the root: apply each node's own transform
        node = gltf["nodes"][i]
        scale = node.get("scale", (1, 1, 1))
        position = tuple(position[k] * scale[k] for k in range(3))
        position = rotate(node.get("rotation", (0, 0, 0, 1)), position)
        t = node.get("translation", (0, 0, 0))
        position = tuple(position[k] + t[k] for k in range(3))
    return position


def test_arms_held_out_are_relaxed_to_hang_and_that_is_the_rest_pose(tmp_path):
    folder = tmp_path / "deploy" / "assets" / "custom_templates"
    folder.mkdir(parents=True)
    _limbed_model_glb(
        folder / "limbed.glb"
    )  # arms modelled straight out sideways, a T-pose

    result, out_path, build_json = _compile(
        _minimal_spec(base_template="custom:limbed", has_face=False),
        tmp_path,
        "relaxed",
    )
    assert result.returncode == 0, result.stderr
    assert not [w for w in build_json["warnings"] if "relax" in w], build_json[
        "warnings"
    ]

    gltf = _glb_gltf(out_path)
    parents = {
        child: i
        for i, node in enumerate(gltf["nodes"])
        for child in node.get("children", [])
    }
    names = {node.get("name"): i for i, node in enumerate(gltf["nodes"])}
    for side in ("l", "r"):
        shoulder = _node_world_position(
            gltf, names[f"upperarm_{side}"], parents
        )
        elbow = _node_world_position(gltf, names[f"lowerarm_{side}"], parents)
        arm = tuple(elbow[k] - shoulder[k] for k in range(3))
        length = sum(c * c for c in arm) ** 0.5
        assert -arm[1] / length > 0.7, (
            side,
            arm,
        )  # glTF is Y-up: more than ~45 degrees below level, hanging

    # and the gestures and run poses that start from it are still there
    assert (
        build_json["run_shape_keys"] == ["run_a", "run_b"]
        and len(build_json["gesture_shape_keys"]) == 4
    )


def _target_extent(
    gltf: dict, mesh: dict, name: str
) -> tuple[float, float, float]:
    """The largest offset a morph target makes along each glTF axis (x sideways, y up, z
    toward the viewer, i.e. forward for a figure facing -Y in Blender), from its accessor's bounds.
    """
    index = mesh["extras"]["targetNames"].index(name)
    accessor = gltf["accessors"][
        mesh["primitives"][0]["targets"][index]["POSITION"]
    ]
    return tuple(
        max(abs(accessor["min"][k]), abs(accessor["max"][k])) for k in range(3)
    )  # type: ignore[return-value]


def test_a_relaxed_body_gestures_with_the_elbow_bending_forward_and_both_arms_alike(
    tmp_path,
):
    """The gestures were tuned on a mascot's own arms (one raised, one on its hip), where
    the forearm swings in the plane of the body. On a person that is an elbow hinging sideways,
    and the overlay blends each arm between its two poses about once a second, which reads as
    flopping. A body whose arms were relaxed gets talking gestures: the forearm comes forward,
    the same for both arms, the "out" pose bigger than the "in" pose."""
    folder = tmp_path / "deploy" / "assets" / "custom_templates"
    folder.mkdir(parents=True)
    _limbed_model_glb(folder / "limbed.glb")

    result, out_path, build_json = _compile(
        _minimal_spec(base_template="custom:limbed", has_face=False),
        tmp_path,
        "mirror",
    )
    assert result.returncode == 0, result.stderr
    gltf = _glb_gltf(out_path)
    mesh = next(
        m
        for m in gltf["meshes"]
        if "gesture_l_out" in m.get("extras", {}).get("targetNames", [])
    )
    extent = {
        name: _target_extent(gltf, mesh, name)
        for name in (
            "gesture_l_out",
            "gesture_r_out",
            "gesture_l_in",
            "gesture_r_in",
        )
    }

    for side in ("l", "r"):
        side_reach, up_reach, forward_reach = extent[f"gesture_{side}_out"]
        assert forward_reach > 0.5 * up_reach, (
            side,
            extent,
        )  # the forearm comes forward (it was 0 sideways-only)
        assert side_reach < up_reach, (
            side,
            extent,
        )  # and the upper arm does not swing far out
    assert max(extent["gesture_l_out"]) == pytest.approx(
        max(extent["gesture_r_out"]), rel=0.12
    ), extent
    assert max(extent["gesture_l_out"]) > 1.3 * max(
        extent["gesture_l_in"]
    ), extent


def _node_rotation(gltf: dict, name: str) -> tuple[float, float, float, float]:
    node = next(n for n in gltf["nodes"] if n.get("name") == name)
    return tuple(node.get("rotation", (0, 0, 0, 1)))  # type: ignore[return-value]


def test_a_pose_sidecar_turns_a_hand_about_its_own_length_in_the_rest_pose(
    tmp_path,
):
    """A hand modelled with its fingers turned outward; `<model>.pose.json` rolls the bone."""
    folder = tmp_path / "deploy" / "assets" / "custom_templates"
    folder.mkdir(parents=True)
    _limbed_model_glb(folder / "limbed.glb", hands=True)

    result, plain_path, plain_build = _compile(
        _minimal_spec(base_template="custom:limbed", has_face=False),
        tmp_path,
        "plain",
    )
    assert result.returncode == 0, result.stderr
    (folder / "limbed.pose.json").write_text(
        json.dumps({"roll_bones": {"hand_l": 180}})
    )
    result, rolled_path, rolled_build = _compile(
        _minimal_spec(base_template="custom:limbed", has_face=False),
        tmp_path,
        "rolled",
    )
    assert result.returncode == 0, result.stderr
    assert not [
        w for w in rolled_build["warnings"] if "pose.json" in w
    ], rolled_build["warnings"]

    def turn_between(a, b):
        dot = abs(
            sum(x * y for x, y in zip(a, b))
        )  # |cos(half the angle between the two orientations)|
        return 2 * __import__("math").degrees(
            __import__("math").acos(min(1.0, dot))
        )

    plain, rolled = _glb_gltf(plain_path), _glb_gltf(rolled_path)
    assert turn_between(
        _node_rotation(plain, "hand_l"), _node_rotation(rolled, "hand_l")
    ) == pytest.approx(180, abs=3)
    assert turn_between(
        _node_rotation(plain, "hand_r"), _node_rotation(rolled, "hand_r")
    ) == pytest.approx(
        0, abs=3
    )  # only the named one


def test_a_pose_sidecar_can_turn_a_model_that_does_not_face_the_front(tmp_path):
    """The limbed model is wide (arms along X) and shallow; a quarter turn swaps that."""
    folder = tmp_path / "deploy" / "assets" / "custom_templates"
    folder.mkdir(parents=True)
    _limbed_model_glb(folder / "limbed.glb")
    result, _, plain = _compile(
        _minimal_spec(base_template="custom:limbed", has_face=False),
        tmp_path,
        "faces_front",
    )
    assert result.returncode == 0, result.stderr
    (folder / "limbed.pose.json").write_text(json.dumps({"face_yaw": 90}))
    result, _, turned = _compile(
        _minimal_spec(base_template="custom:limbed", has_face=False),
        tmp_path,
        "turned",
    )
    assert result.returncode == 0, result.stderr
    assert plain["bounds"]["width_m"] > plain["bounds"]["depth_m"]
    assert turned["bounds"]["depth_m"] > turned["bounds"]["width_m"]


def test_a_pose_sidecar_naming_a_missing_bone_warns_and_builds_anyway(tmp_path):
    folder = tmp_path / "deploy" / "assets" / "custom_templates"
    folder.mkdir(parents=True)
    _limbed_model_glb(folder / "limbed.glb")
    (folder / "limbed.pose.json").write_text(
        json.dumps({"roll_bones": {"no_such_bone": 90}})
    )

    result, _, build_json = _compile(
        _minimal_spec(base_template="custom:limbed", has_face=False),
        tmp_path,
        "missing",
    )
    assert result.returncode == 0, result.stderr
    assert any("no_such_bone" in w for w in build_json["warnings"]), build_json[
        "warnings"
    ]
