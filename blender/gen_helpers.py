"""
Small helpers shared by the generator's parts: colours, materials, the scene
reset, triangle counts, the 3-tier template resolution.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import bmesh  # pyright: ignore[reportMissingImports]
import bpy  # pyright: ignore[reportMissingImports]


def reset_scene() -> None:
    bpy.ops.wm.read_factory_settings(use_empty=True)


def hex_to_rgba(hex_color: str) -> tuple[float, float, float, float]:
    h = hex_color.lstrip("#")
    r, g, b = (int(h[i : i + 2], 16) / 255.0 for i in (0, 2, 4))
    return (r, g, b, 1.0)


def make_material(
    name: str,
    color: tuple[float, float, float, float],
    *,
    material: str = "matte",
    emissive: bool = False,
) -> bpy.types.Material:
    mat = bpy.data.materials.new(name)
    mat.use_nodes = True
    bsdf = mat.node_tree.nodes.get("Principled BSDF")
    if bsdf:
        bsdf.inputs["Base Color"].default_value = color
        if material == "metallic":
            bsdf.inputs["Metallic"].default_value = 1.0
            bsdf.inputs["Roughness"].default_value = 0.3
        elif material == "glossy":
            bsdf.inputs["Roughness"].default_value = 0.05
        elif material == "glass":
            bsdf.inputs["Transmission Weight"].default_value = 1.0
            bsdf.inputs["Roughness"].default_value = 0.0
        if emissive or material == "emissive_circuit":
            if "Emission Color" in bsdf.inputs:
                bsdf.inputs["Emission Color"].default_value = color
            if "Emission Strength" in bsdf.inputs:
                bsdf.inputs["Emission Strength"].default_value = 2.0
    return mat


def write_build_failure(out_build_json: Path, reason: str) -> None:
    out_build_json.write_text(
        json.dumps(
            {"status": "fail", "reason": reason, "warnings": []}, indent=2
        )
    )


def fail(out_build_json: Path, reason: str) -> None:
    write_build_failure(out_build_json, reason)
    print(f"DESKMATE_BUILD_FAIL {reason}")
    sys.exit(1)


def resolve_template(base_template: str, deploy_dir: Path) -> dict:
    """Returns {"tier": ..., "imported": bool, "path": Path|None}. Tier 1/2
    import an existing asset if present; Tier 3 (or a missing Tier 1/2 file)
    means "build procedurally, zero files" -- the fallback that always
    works even before any Tier 1/2 asset has been authored."""
    if base_template.startswith("custom:"):
        name = base_template[len("custom:") :]
        custom_dir = deploy_dir / "assets" / "custom_templates"
        for ext in (".glb", ".blend"):
            path = custom_dir / f"{name}{ext}"
            if path.is_file():
                return {"tier": "tier_1_custom", "imported": True, "path": path}
        return {"tier": "tier_3_primitive", "imported": False, "path": None}

    if base_template in ("standard_humanoid_female", "standard_humanoid_male"):
        path = deploy_dir / "assets" / "base_templates" / f"{base_template}.glb"
        if path.is_file():
            return {"tier": "tier_2_standard", "imported": True, "path": path}
        return {"tier": "tier_3_primitive", "imported": False, "path": None}

    return {"tier": "tier_3_primitive", "imported": False, "path": None}


def count_triangles() -> int:
    depsgraph = bpy.context.evaluated_depsgraph_get()
    total = 0
    for obj in bpy.context.scene.objects:
        if obj.type != "MESH":
            continue
        eval_obj = obj.evaluated_get(depsgraph)
        me = eval_obj.to_mesh()
        bm = bmesh.new()
        bm.from_mesh(me)
        bmesh.ops.triangulate(bm, faces=bm.faces[:])
        total += len(bm.faces)
        bm.free()
        eval_obj.to_mesh_clear()
    return total


def _names_of(mesh) -> str:
    """Lower-case names of a mesh's materials and textures, for telling parts apart."""
    names = [mesh.name]
    for slot in mesh.material_slots:
        if slot.material is not None:
            names.append(slot.material.name)
            if slot.material.node_tree is not None:
                names += [
                    n.image.name
                    for n in slot.material.node_tree.nodes
                    if n.type == "TEX_IMAGE" and n.image
                ]
    return " ".join(names).lower()
