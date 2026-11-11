import json
import struct

from brain.appearance.glb_tools import _chunks, hoist_skinned_meshes


def _glb(path, nodes, roots):
    gltf = {
        "asset": {"version": "2.0"},
        "scene": 0,
        "scenes": [{"nodes": roots}],
        "nodes": nodes,
    }
    body = json.dumps(gltf).encode()
    body += b" " * (-len(body) % 4)
    blob = b"\x01\x02\x03\x04"
    out = b"glTF" + struct.pack("<II", 2, 12 + 8 + len(body) + 8 + len(blob))
    out += (
        struct.pack("<II", len(body), 0x4E4F534A)
        + body
        + struct.pack("<II", len(blob), 0x004E4942)
        + blob
    )
    path.write_bytes(out)
    return path


def test_a_skinned_mesh_under_a_scaled_rotated_armature_goes_to_the_scene_root_without_a_transform(
    tmp_path,
):
    glb = _glb(
        tmp_path / "a.glb",
        [
            {
                "name": "Armature",
                "scale": [100, 100, 100],
                "rotation": [-0.7071, 0, 0, 0.7071],
                "children": [1, 2],
            },
            {"name": "Body", "mesh": 0, "skin": 0},
            {"name": "Prop", "mesh": 1},  # not skinned: stays where it is
        ],
        [0],
    )
    assert hoist_skinned_meshes(glb) == ["Body"]
    gltf, rest = _chunks(glb.read_bytes())
    assert gltf["scenes"][0]["nodes"] == [0, 1]
    assert gltf["nodes"][0]["children"] == [2]
    assert set(gltf["nodes"][1]) == {"name", "mesh", "skin"}
    assert rest[0][1] == b"\x01\x02\x03\x04"  # the binary chunk is carried over


def test_a_file_whose_skinned_meshes_have_no_transform_above_them_is_left_alone(
    tmp_path,
):
    glb = _glb(
        tmp_path / "b.glb",
        [{"name": "Armature", "children": [1]}, {"name": "Body", "mesh": 0, "skin": 0}],
        [0],
    )
    before = glb.read_bytes()
    assert hoist_skinned_meshes(glb) == []
    assert glb.read_bytes() == before


def test_a_file_that_is_not_a_readable_glb_is_left_alone(tmp_path):
    path = tmp_path / "c.glb"
    path.write_bytes(b"glTF" + b"\x00" * 20)
    before = path.read_bytes()
    assert hoist_skinned_meshes(path) == []
    assert path.read_bytes() == before
