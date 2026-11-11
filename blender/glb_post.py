"""Changes to a .glb after Blender has written it (pure Python, no bpy)."""

import json
import struct
from pathlib import Path

_STRENGTH = "KHR_materials_emissive_strength"


def _is_emissive(material: dict) -> bool:
    return "emissiveTexture" in material or any(
        material.get("emissiveFactor", [0, 0, 0])
    )


def brighten_emissive(glb_path: Path, gain: float = 1.0) -> int:
    """Makes the overlay's renderer draw glowing materials as bright as the file says. glTF keeps a
    glow above 1 in `KHR_materials_emissive_strength` (a TV screen at 2.3), which Qt Quick 3D ignores,
    so the screen came out as flat grey; the strength is multiplied into the material's own
    `emissiveFactor` (which Qt does take above 1) and the extension dropped. `gain` is a further
    multiplier for a model that wants its screens or lights brighter than the file says (its
    `emissive_gain` in `<model>.pose.json`). Returns how many materials changed.
    """
    data = glb_path.read_bytes()
    length = struct.unpack("<I", data[12:16])[0]
    gltf = json.loads(data[20 : 20 + length])
    changed = 0
    for material in gltf.get("materials", []):
        extension = material.get("extensions", {}).get(_STRENGTH)
        strength = (
            float(extension.get("emissiveStrength", 1.0)) if extension else 1.0
        )
        if not _is_emissive(material) or (strength == 1.0 and gain == 1.0):
            continue
        factor = material.get("emissiveFactor", [1.0, 1.0, 1.0])
        material["emissiveFactor"] = [c * strength * gain for c in factor]
        if extension is not None:
            del material["extensions"][_STRENGTH]
            if not material["extensions"]:
                del material["extensions"]
        changed += 1
    if not changed:
        return 0
    used = gltf.get("extensionsUsed")
    if used and _STRENGTH in used:
        used.remove(_STRENGTH)
        if not used:
            del gltf["extensionsUsed"]
    body = json.dumps(gltf, separators=(",", ":")).encode()
    body += b" " * (-len(body) % 4)
    rest = data[20 + length :]
    chunk = struct.pack("<I", len(body)) + b"JSON" + body
    glb_path.write_bytes(
        data[:8] + struct.pack("<I", 12 + len(chunk) + len(rest)) + chunk + rest
    )
    return changed
