"""Lists the meshes of a body, or binds one of them wholly to one bone. Run headless, never imported:

    blender --background --factory-startup --python blender/rebind.py -- --list <model.glb>
    blender --background --factory-startup --python blender/rebind.py -- <in.glb> <out.glb> --mesh NAME [--bone BONE | --bone-left BONE --bone-right BONE [--ramp LEFT RIGHT Z0 Z1]] [--rename NEW] [--offset X Y Z]

Skin weights made by a tool are often wider than the part moves: a rigid disc or plate may share some of
its weight with the shoulders and the thighs beside it, which costs nothing while those bones stand still
and drags the part about as soon as an animation library moves them. Binding the part wholly to the one
bone it belongs to (`--mesh`, as `--list` names it) stops that. A mesh that holds a pair (both shoes, both hands)
is bound by side instead: `--bone-left` takes the vertices on the model's left (+X), `--bone-right` the rest. `--ramp UPPER_LEFT UPPER_RIGHT Z0 Z1` then hands a share of each
vertex between heights Z0 and Z1 (metres, 0 at Z0 to all of it at Z1) to the bone above (a shoe: the sole follows
the foot and stays level, its opening follows the leg). `--rename` gives the mesh a name that says
what it is (a download's meshes are often called `Object_72`). `--offset` moves it (metres, in the
model's own axes: it faces -Y, its left is +X, up is Z), for a part that shows through another when a
clip moves the bones between them. Only the mesh named is changed.
"""

import sys

import bpy  # pyright: ignore[reportMissingImports]
import mathutils  # pyright: ignore[reportMissingImports]


def _argument(flag: str) -> str | None:
    if flag in sys.argv:
        return sys.argv[sys.argv.index(flag) + 1]
    return None


def _skinned_meshes() -> list:
    return [
        o
        for o in bpy.data.objects
        if o.type == "MESH" and any(m.type == "ARMATURE" for m in o.modifiers)
    ]


def list_meshes(source: str) -> None:
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.import_scene.gltf(filepath=source)
    for mesh in _skinned_meshes():
        names = {g.index: g.name for g in mesh.vertex_groups}
        totals: dict[str, float] = {}
        for vertex in mesh.data.vertices:
            for g in vertex.groups:
                totals[names[g.group]] = (
                    totals.get(names[g.group], 0.0) + g.weight
                )
        top = sorted(totals.items(), key=lambda item: -item[1])[:4]
        print(
            f"MESH {mesh.name} vertices={len(mesh.data.vertices)} bones={', '.join(f'{b} ({w:.0f})' for b, w in top)}"
        )
    print("LIST_OK")


def rebind(
    source: str,
    out: str,
    mesh_name: str,
    bone: str | None,
    new_name: str | None,
    offset: list[float] | None = None,
    pair: tuple[str, str] | None = None,
    ramp: tuple[str, str, float, float] | None = None,
) -> None:
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.import_scene.gltf(filepath=source)
    meshes = {m.name: m for m in _skinned_meshes()}
    if mesh_name not in meshes:
        raise SystemExit(
            f"no skinned mesh called {mesh_name}: it has {', '.join(sorted(meshes)) or 'none'}"
        )
    mesh = meshes[mesh_name]
    if new_name and new_name != mesh_name and new_name in meshes:
        raise SystemExit(f"there is already a mesh called {new_name}")
    done = []
    if bone:
        armature = next(o for o in bpy.data.objects if o.type == "ARMATURE")
        if bone not in armature.data.bones:
            raise SystemExit(f"the skeleton has no bone called {bone}")
        for group in list(mesh.vertex_groups):
            mesh.vertex_groups.remove(group)
        mesh.vertex_groups.new(name=bone).add(
            list(range(len(mesh.data.vertices))), 1.0, "REPLACE"
        )
        done.append(
            f"{mesh_name} -> {bone} ({len(mesh.data.vertices)} vertices)"
        )
    if pair:
        armature = next(o for o in bpy.data.objects if o.type == "ARMATURE")
        for side_bone in pair:
            if side_bone not in armature.data.bones:
                raise SystemExit(f"the skeleton has no bone called {side_bone}")
        for group in list(mesh.vertex_groups):
            mesh.vertex_groups.remove(group)
        left: list[int] = []
        right: list[int] = []
        for vertex in mesh.data.vertices:
            (left if (mesh.matrix_world @ vertex.co).x > 0 else right).append(
                vertex.index
            )
        if not left or not right:
            raise SystemExit(
                f"{mesh_name} is all on one side: bind it with --bone"
            )
        for side_bone, indices, upper in (
            (pair[0], left, ramp[0] if ramp else None),
            (pair[1], right, ramp[1] if ramp else None),
        ):
            group = mesh.vertex_groups.new(name=side_bone)
            if upper is None or ramp is None:
                group.add(indices, 1.0, "REPLACE")
                continue
            if upper not in armature.data.bones:
                raise SystemExit(f"the skeleton has no bone called {upper}")
            above = mesh.vertex_groups.new(name=upper)
            for index in indices:
                z = (mesh.matrix_world @ mesh.data.vertices[index].co).z
                t = min(max((z - ramp[2]) / (ramp[3] - ramp[2]), 0.0), 1.0)
                if t < 1.0:
                    group.add([index], 1.0 - t, "REPLACE")
                if t > 0.0:
                    above.add([index], t, "REPLACE")
        done.append(
            f"{mesh_name}: {len(left)} vertices -> {pair[0]}, {len(right)} -> {pair[1]}"
            + (
                f", blending into {ramp[0]}/{ramp[1]} above {ramp[2]:.3f} m"
                if ramp
                else ""
            )
        )
    if offset:
        local = mesh.matrix_world.to_3x3().inverted() @ mathutils.Vector(offset)
        mesh.data.transform(mathutils.Matrix.Translation(local))
        done.append(
            f"{mesh_name} moved by ({offset[0]:+.3f}, {offset[1]:+.3f}, {offset[2]:+.3f})"
        )
    if new_name:
        mesh.name = new_name
        mesh.data.name = new_name
        done.append(f"{mesh_name} renamed {new_name}")
    bpy.ops.export_scene.gltf(
        filepath=out,
        export_format="GLB",
        export_skins=True,
        export_animations=False,
        export_draco_mesh_compression_enable=False,
    )
    print(f"REBIND_OK {'; '.join(done)}")


def main() -> None:
    args = sys.argv[sys.argv.index("--") + 1 :]
    if args[0] == "--list":
        list_meshes(args[1])
        return
    mesh, bone, new_name = (
        _argument("--mesh"),
        _argument("--bone"),
        _argument("--rename"),
    )
    left, right = _argument("--bone-left"), _argument("--bone-right")
    offset = (
        [
            float(v)
            for v in sys.argv[
                sys.argv.index("--offset") + 1 : sys.argv.index("--offset") + 4
            ]
        ]
        if "--offset" in sys.argv
        else None
    )
    ramp = None
    if "--ramp" in sys.argv:
        r = sys.argv[
            sys.argv.index("--ramp") + 1 : sys.argv.index("--ramp") + 5
        ]
        ramp = (r[0], r[1], float(r[2]), float(r[3]))
        if not (left and right):
            raise SystemExit("--ramp goes with --bone-left and --bone-right")
    if (left is None) != (right is None):
        raise SystemExit("give --bone-left and --bone-right together")
    if bone and left:
        raise SystemExit(
            "give --bone, or --bone-left and --bone-right, not both"
        )
    if mesh is None or (
        bone is None and left is None and new_name is None and offset is None
    ):
        raise SystemExit(
            "give --mesh and --bone, --bone-left/--bone-right, --rename, --offset, or any of them"
        )
    rebind(
        args[0],
        args[1],
        mesh,
        bone,
        new_name,
        offset,
        (left, right) if left and right else None,
        ramp,
    )


if __name__ == "__main__":
    main()
