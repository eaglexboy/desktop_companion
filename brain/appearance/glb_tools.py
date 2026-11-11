"""
Small edits to a finished .glb that need no Blender: the JSON chunk is read,
changed and written back, the binary chunk is carried over as it is.
"""

from __future__ import annotations

import json
import struct
from pathlib import Path

_JSON = 0x4E4F534A


def _chunks(data: bytes) -> tuple[dict, list[tuple[int, bytes]]]:
    if data[:4] != b"glTF":
        raise ValueError("not a glTF binary")
    chunks, offset = [], 12
    while offset < len(data):
        length, kind = struct.unpack_from("<II", data, offset)
        chunks.append((kind, data[offset + 8 : offset + 8 + length]))
        offset += 8 + length
    gltf = json.loads(chunks[0][1])
    return gltf, chunks[1:]


def _write(path: Path, gltf: dict, rest: list[tuple[int, bytes]]) -> None:
    body = json.dumps(gltf, separators=(",", ":")).encode()
    body += b" " * (-len(body) % 4)
    parts = [(_JSON, body), *rest]
    total = 12 + sum(8 + len(blob) for _, blob in parts)
    out = b"glTF" + struct.pack("<II", 2, total)
    for kind, blob in parts:
        out += struct.pack("<II", len(blob), kind) + blob
    path.write_bytes(out)


def _moves_the_mesh(node: dict) -> bool:
    """A node whose own transform is not the identity (the transform of a skinned mesh's node is
    ignored by the glTF spec, but not by every loader)."""
    scale, rotation, translation = (
        node.get("scale"),
        node.get("rotation"),
        node.get("translation"),
    )
    return bool(
        "matrix" in node
        or (scale and any(abs(x - 1.0) > 1e-3 for x in scale))
        or (rotation and abs(abs(rotation[3]) - 1.0) > 1e-3)
        or (translation and any(abs(x) > 1e-6 for x in translation))
    )


def hoist_skinned_meshes(path: Path) -> list[str]:
    """Moves each skinned mesh that sits under a scaled, rotated or moved node (its own or a parent's) to
    the root of the scene with no transform of its own. The names of the meshes moved; the file is
    rewritten only if there are any.

    Per the glTF spec a skinned mesh is placed by its skeleton alone, so this changes nothing for a loader that
    follows it. Qt Quick 3D's runtime loader also applies the node chain above the mesh, so a model whose
    author left a scaled and rotated armature node over its meshes (centimetres, Z-up) came out a hundred times
    too large and turned on its side, its head out of view."""
    try:
        gltf, rest = _chunks(path.read_bytes())
    except (ValueError, IndexError, struct.error):
        return (
            []
        )  # not a glTF binary we can read: the build's own checks say what is wrong with it
    nodes = gltf.get("nodes", [])
    parent = {
        child: i
        for i, node in enumerate(nodes)
        for child in node.get("children", [])
    }
    moved: list[int] = []
    for index, node in enumerate(nodes):
        if "mesh" not in node or "skin" not in node:
            continue
        chain, k = [node], index
        while k in parent:
            k = parent[k]
            chain.append(nodes[k])
        if any(_moves_the_mesh(n) for n in chain):
            moved.append(index)
    if not moved:
        return []
    for index in moved:
        if index in parent:
            nodes[parent[index]]["children"].remove(index)
        for key in ("matrix", "translation", "rotation", "scale"):
            nodes[index].pop(key, None)
        roots = gltf["scenes"][gltf.get("scene", 0)]["nodes"]
        if index not in roots:
            roots.append(index)
    _write(path, gltf, rest)
    return [nodes[i].get("name", str(i)) for i in moved]
