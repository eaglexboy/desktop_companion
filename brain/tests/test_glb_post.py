"""Changes made to a .glb after Blender wrote it (blender/glb_post.py)."""

import json
import struct
from pathlib import Path

from blender.glb_post import brighten_emissive


def _glb(path: Path, materials: list[dict], extensions_used=None) -> None:
    gltf: dict = {"asset": {"version": "2.0"}, "materials": materials}
    if extensions_used:
        gltf["extensionsUsed"] = extensions_used
    body = json.dumps(gltf).encode()
    body += b" " * (-len(body) % 4)
    binary = b"\x01\x02\x03\x04"
    chunks = (
        struct.pack("<I", len(body))
        + b"JSON"
        + body
        + struct.pack("<I", len(binary))
        + b"BIN\x00"
        + binary
    )
    path.write_bytes(
        b"glTF" + struct.pack("<II", 2, 12 + len(chunks)) + chunks
    )


def _read(path: Path) -> tuple[dict, bytes]:
    data = path.read_bytes()
    length = struct.unpack("<I", data[12:16])[0]
    assert struct.unpack("<I", data[8:12])[0] == len(data)
    return json.loads(data[20 : 20 + length]), data[20 + length :]


def test_a_glow_above_one_is_multiplied_into_the_factor_qt_does_take(tmp_path):
    path = tmp_path / "tv.glb"
    _glb(
        path,
        [
            {
                "name": "screen",
                "emissiveFactor": [1, 1, 1],
                "emissiveTexture": {"index": 0},
                "extensions": {
                    "KHR_materials_emissive_strength": {"emissiveStrength": 2.5},
                    "KHR_materials_specular": {"specularFactor": 0.2},
                },
            },
            {"name": "plain", "pbrMetallicRoughness": {}},
        ],
        ["KHR_materials_emissive_strength", "KHR_materials_specular"],
    )
    assert brighten_emissive(path) == 1
    gltf, rest = _read(path)
    screen, plain = gltf["materials"]
    assert screen["emissiveFactor"] == [2.5, 2.5, 2.5]
    assert screen["extensions"] == {
        "KHR_materials_specular": {"specularFactor": 0.2}
    }  # the strength is not counted twice
    assert gltf["extensionsUsed"] == ["KHR_materials_specular"]
    assert "emissiveFactor" not in plain
    assert rest == struct.pack("<I", 4) + b"BIN\x00" + b"\x01\x02\x03\x04"  # the data is untouched


def test_a_model_can_ask_for_more_glow_than_its_file_has(tmp_path):
    path = tmp_path / "tv.glb"
    _glb(
        path,
        [
            {"name": "a", "emissiveTexture": {"index": 0}},
            {"name": "b", "emissiveFactor": [0.5, 0.5, 0], "emissiveTexture": {"index": 1}},
            {"name": "dark", "pbrMetallicRoughness": {}},
        ],
    )
    assert brighten_emissive(path, 3.0) == 2
    gltf, _ = _read(path)
    assert gltf["materials"][0]["emissiveFactor"] == [3.0, 3.0, 3.0]
    assert gltf["materials"][1]["emissiveFactor"] == [1.5, 1.5, 0.0]
    assert "emissiveFactor" not in gltf["materials"][2]


def test_a_file_with_nothing_to_brighten_is_left_alone(tmp_path):
    path = tmp_path / "plain.glb"
    _glb(path, [{"name": "a", "emissiveFactor": [1, 1, 1]}])
    before = path.read_bytes()
    assert brighten_emissive(path) == 0
    assert path.read_bytes() == before
