"""Turns Mixamo files into Quaternius ones. Run headless, never imported:

    blender --background --factory-startup --python blender/mixamo_convert.py -- clips <spec.json>
    blender --background --factory-startup --python blender/mixamo_convert.py -- body <in.fbx> <out.glb> [<a Quaternius body .glb>]

`clips` retargets the animations of Mixamo .fbx files onto a Quaternius body's skeleton and writes
them as one animation library (.glb: the skeleton and its clips, the form `add-animations` installs).
`body` renames a Mixamo character's bones to the Quaternius names and exports it as a .glb that
`add-body` then installs like any other body. The reasons and the limits are in
docs/ADR_ACCESSORY_MODELS.md (decision 16); the name map and the clip processing, which need no
Blender, are in blender/animation_library.py.

The spec for `clips` is JSON:
    {"host": "<a Quaternius body .glb>", "out": "<library .glb>",
     "seat_reference": {"library": "<a library .glb>", "clip": "Sitting_Idle_Loop"} | null,
     "clips": [{"fbx": "<file>", "name": "Standing_Greeting", "options": {...}}, ...]}
(`options` as returned by `mixamo_clip_defaults`, then whatever the user overrode.)
"""

import json
import math
import sys
from pathlib import Path

import bpy  # pyright: ignore[reportMissingImports]
import mathutils  # pyright: ignore[reportMissingImports]

sys.path.insert(
    0, str(Path(__file__).resolve().parent.parent)
)  # repo root, for blender.animation_library
from blender.animation_library import (
    INTENSITY_VARIANTS,
    MIRROR_SUFFIX,
    MIXAMO_PREFIX,
    Clip,
    blend_through,
    finish_clips,
    in_place_clip,
    loop_clip,
    mirror_bone,
    mixamo_to_quaternius,
    offset_hips,
    ping_pong,
    q_angle_deg,
    reduce_keys,
    reverse_clip,
    scale_intensity,
    seat_offset,
    trim_clip,
)

ROTATION_TOLERANCE_DEG = (
    0.25  # a rotation key is dropped when the clip stays within this without it
)
HIPS_TOLERANCE_M = 0.002


def _assign(armature, action) -> None:
    armature.animation_data_create()
    armature.animation_data.action = action
    if (
        getattr(armature.animation_data, "action_slot", None) is None
        and action.slots
    ):
        armature.animation_data.action_slot = action.slots[0]


def _import_fbx(path: str):
    """(new objects, the imported action). Blender 5's `import_scene.fbx` fails when run without a
    file list, so the new importer is used."""
    before_objects, before_actions = (
        set(bpy.data.objects),
        set(bpy.data.actions),
    )
    bpy.ops.wm.fbx_import(filepath=path)
    objects = [o for o in bpy.data.objects if o not in before_objects]
    actions = [a for a in bpy.data.actions if a not in before_actions]
    return objects, (actions[0] if actions else None)


def _import_gltf(path: str):
    """(new objects, the imported actions by name)."""
    before_objects, before_actions = (
        set(bpy.data.objects),
        set(bpy.data.actions),
    )
    bpy.ops.import_scene.gltf(filepath=path)
    objects = [o for o in bpy.data.objects if o not in before_objects]
    actions = [a for a in bpy.data.actions if a not in before_actions]
    return objects, actions


def _remove(objects, actions=()) -> None:
    for obj in objects:
        bpy.data.objects.remove(obj, do_unlink=True)
    for action in actions:
        if action is not None:
            bpy.data.actions.remove(action)


class Host:
    """The Quaternius body the clips are retargeted onto (only its skeleton is used)."""

    def __init__(self, path: str) -> None:
        bpy.ops.import_scene.gltf(filepath=path)
        self.armature = next(
            o for o in bpy.data.objects if o.type == "ARMATURE"
        )
        self.world = self.armature.matrix_world.to_3x3().normalized()
        self.order: list = []

        def walk(bone) -> None:
            self.order.append(bone)
            for child in bone.children:
                walk(child)

        for bone in self.armature.data.bones:
            if bone.parent is None:
                walk(bone)
        self.rest = {
            b.name: b.matrix_local.to_3x3().normalized()
            for b in self.armature.data.bones
        }
        self.pelvis_world = (
            self.armature.matrix_world
            @ self.armature.data.bones["pelvis"].head_local
        )
        # where a foot bone sits with the body standing: the lowest the feet go
        self.floor = min(
            (
                self.armature.matrix_world
                @ self.armature.data.bones[f"foot_{s}"].head_local
            ).z
            for s in ("l", "r")
        )

    def retarget(self, fbx: str, mirror: bool = False) -> Clip:
        """Every frame of a Mixamo clip as the pose of this skeleton. Rotations are carried over
        as world-space changes from the rest pose (both rigs rest in a T-pose, so the same change
        of a bone's direction is the same movement), and the hips' travel is scaled by how tall
        the two hips stand. `mirror` swaps left and right (the clip as if made from the other side
        of the body: the world change of each bone is reflected in the plane that splits it).
        """
        objects, action = _import_fbx(fbx)
        try:
            marm = next(o for o in objects if o.type == "ARMATURE")
            if action is None:
                raise SystemExit(f"{fbx} has no animation")
            _assign(marm, action)
            world = marm.matrix_world.to_3x3().normalized()
            mixamo_hips = marm.data.bones[f"{MIXAMO_PREFIX}Hips"]
            ratio = (
                self.pelvis_world.z
                / (marm.matrix_world @ mixamo_hips.head_local).z
            )
            source = {
                mixamo_to_quaternius(b.name): b
                for b in marm.data.bones
                if mixamo_to_quaternius(b.name)
            }
            mixamo_rest = {
                name: world @ bone.matrix_local.to_3x3().normalized()
                for name, bone in source.items()
            }
            floor = min(
                (
                    marm.matrix_world
                    @ marm.data.bones[f"{MIXAMO_PREFIX}{s}Foot"].head_local
                ).z
                for s in ("Left", "Right")
            )
            first, last = int(action.frame_range[0]), int(action.frame_range[1])
            reflect = mathutils.Matrix.Diagonal(
                (-1.0, 1.0, 1.0)
            )  # the sagittal plane: the body's left and right are along x
            frames: list[float] = []
            quats: dict[str, list] = {
                str(bone.name): [] for bone in self.order if bone.name in source
            }
            hips = []
            clearance = 0.0
            for frame in range(first, last + 1):
                bpy.context.scene.frame_set(frame)
                bpy.context.view_layer.update()
                desired = {}
                changes = {
                    name: (
                        world
                        @ marm.pose.bones[bone.name]
                        .matrix.to_3x3()
                        .normalized()
                    )
                    @ mixamo_rest[name].inverted()
                    for name, bone in source.items()
                }
                for bone in self.order:
                    rest = self.rest[bone.name]
                    if bone.name in source:
                        change = changes[bone.name]
                        if mirror:
                            change = (
                                reflect
                                @ changes.get(
                                    mirror_bone(str(bone.name)), change
                                )
                                @ reflect
                            )
                        desired[bone.name] = (
                            self.world.inverted() @ change @ (self.world @ rest)
                        )
                    else:
                        desired[bone.name] = rest
                for bone in self.order:
                    if bone.name not in quats:
                        continue
                    rest = self.rest[bone.name]
                    if bone.parent:
                        basis = (
                            rest.inverted()
                            @ self.rest[bone.parent.name]
                            @ desired[bone.parent.name].inverted()
                            @ desired[bone.name]
                        )
                    else:
                        basis = rest.inverted() @ desired[bone.name]
                    q = basis.to_quaternion()
                    quats[bone.name].append((q.w, q.x, q.y, q.z))
                travel = (
                    marm.matrix_world
                    @ marm.pose.bones[f"{MIXAMO_PREFIX}Hips"].head
                ) - (marm.matrix_world @ mixamo_hips.head_local)
                hips.append(
                    (
                        -travel.x * ratio if mirror else travel.x * ratio,
                        travel.y * ratio,
                        travel.z * ratio,
                    )
                )
                if frame == first:
                    feet = min(
                        (
                            marm.matrix_world
                            @ marm.pose.bones[f"{MIXAMO_PREFIX}{s}Foot"].head
                        ).z
                        for s in ("Left", "Right")
                    )
                    clearance = (feet - floor) * ratio
                frames.append(float(frame))
            rest = (
                self.pelvis_world
            )  # the hips travel is applied to this skeleton's own hips
            up = (self.world @ self.rest["pelvis"]).to_quaternion()
            return Clip(
                frames,
                quats,
                hips,
                clearance,
                {
                    "hips_rest": (rest.x, rest.y, rest.z),
                    "pelvis_rest": (up.w, up.x, up.y, up.z),
                },
            )
        finally:
            _remove(objects, [action])

    def retarget_native(self, path: str, clip_name: str) -> Clip:
        """A clip from a .glb whose skeleton is the Quaternius one (bone names and all), as exported
        by a tool such as Mesh2Motion. Such a file's own rest pose is the pose it was exported in
        (a bow, a seat on the floor), so nothing is measured from it: every bone's *world
        orientation* in each frame is taken as the orientation to give the same bone here, which the
        two skeletons share. The hips' height is scaled by the leg lengths (the clip's skeleton may be
        a little shorter or taller than this one). The first sample is the clip's first key, so a
        clip that starts a frame late does not show the rest pose first."""
        objects, actions = _import_gltf(path)
        try:
            marm = next(o for o in objects if o.type == "ARMATURE")
            action = next(
                (a for a in actions if a.name == clip_name),
                next((a for a in actions if clip_name in a.name), None),
            )
            if action is None:
                raise SystemExit(
                    f"{path} has no clip {clip_name} ({[a.name for a in actions]})"
                )
            _assign(marm, action)
            source = {b.name: b for b in marm.data.bones if b.name in self.rest}
            if not {"pelvis", "thigh_l", "calf_l"} <= set(source):
                raise SystemExit(f"{path} is not on the Quaternius skeleton")

            def leg(armature) -> float:
                bones = armature.data.bones
                return sum(
                    bones[f"{n}_{side}"].length
                    for n in ("thigh", "calf")
                    for side in ("l", "r")
                )

            scale = leg(self.armature) / leg(marm)
            world = marm.matrix_world
            first, last = action.frame_range
            frames: list[float] = []
            quats: dict[str, list] = {
                str(bone.name): [] for bone in self.order if bone.name in source
            }
            hips = []
            frame = first
            while frame <= last + 1e-6:
                bpy.context.scene.frame_set(
                    int(frame), subframe=frame - int(frame)
                )
                bpy.context.view_layer.update()
                desired = {}
                for bone in self.order:
                    if bone.name in source:
                        desired[bone.name] = (
                            self.world.inverted()
                            @ world.to_3x3().normalized()
                            @ marm.pose.bones[bone.name]
                            .matrix.to_3x3()
                            .normalized()
                        )
                    else:
                        desired[bone.name] = self.rest[bone.name]
                for bone in self.order:
                    if bone.name not in quats:
                        continue
                    rest = self.rest[bone.name]
                    if bone.parent:
                        basis = (
                            rest.inverted()
                            @ self.rest[bone.parent.name]
                            @ desired[bone.parent.name].inverted()
                            @ desired[bone.name]
                        )
                    else:
                        basis = rest.inverted() @ desired[bone.name]
                    q = basis.to_quaternion()
                    quats[bone.name].append((q.w, q.x, q.y, q.z))
                pelvis = (world @ marm.pose.bones["pelvis"].head) * scale
                hips.append(
                    (
                        pelvis.x - self.pelvis_world.x,
                        pelvis.y - self.pelvis_world.y,
                        pelvis.z - self.pelvis_world.z,
                    )
                )
                frames.append(float(frame))
                frame += 1.0
            rest = self.pelvis_world
            up = (self.world @ self.rest["pelvis"]).to_quaternion()
            return Clip(
                frames,
                quats,
                hips,
                0.0,
                {
                    "hips_rest": (rest.x, rest.y, rest.z),
                    "pelvis_rest": (up.w, up.x, up.y, up.z),
                },
            )
        finally:
            _remove(objects, actions)

    def hips_to_basis(self, world_travel) -> mathutils.Vector:
        """A hips offset in world axes as the pelvis bone's pose-bone location."""
        rest = self.rest["pelvis"]
        return (
            rest.inverted()
            @ self.world.inverted()
            @ mathutils.Vector(world_travel)
        )

    def write(self, name: str, clip: Clip) -> None:
        """Keys a clip onto the skeleton as an action called `name` (1 frame = 1/30 s)."""
        action = bpy.data.actions.new(name)
        action.use_fake_user = True
        _assign(self.armature, action)
        # The first key goes at frame 0, which the glTF exporter writes as time 0 (frame / fps): a clip
        # whose first key is later shows the rest pose (a T-pose) until it, whenever the clip starts.
        origin = clip.frames[0]
        for bone_name, samples in clip.quats.items():
            pose_bone = self.armature.pose.bones[bone_name]
            pose_bone.rotation_mode = "QUATERNION"
            for i in reduce_keys(
                clip.frames, samples, q_angle_deg, ROTATION_TOLERANCE_DEG
            ):
                pose_bone.rotation_quaternion = mathutils.Quaternion(samples[i])
                pose_bone.keyframe_insert(
                    "rotation_quaternion", frame=clip.frames[i] - origin
                )
        pelvis = self.armature.pose.bones["pelvis"]
        travel = [self.hips_to_basis(h) for h in clip.hips]
        for i in reduce_keys(
            clip.frames,
            [tuple(t) for t in travel],
            lambda a, b: math.dist(a, b),
            HIPS_TOLERANCE_M,
        ):
            pelvis.location = travel[i]
            pelvis.keyframe_insert("location", frame=clip.frames[i] - origin)

    def plant_feet(
        self, clip: Clip, hips_move, feet_move, floor: float
    ) -> Clip:
        """The clip with its hips moved by `hips_move` (world axes) and its legs re-solved so the
        feet stay where the clip puts them, moved by `feet_move` and kept above `floor`
        (world z). Two-bone IK per leg, bending the knee the way the clip does, with the foot
        turned as the clip turns it: a seated clip made for a higher seat is lowered onto ours
        without the feet leaving the floor or sinking into it, so it needs no seat-height jump.
        """
        arm = self.armature
        bones = arm.pose.bones
        for bone in bones:
            bone.rotation_mode = "QUATERNION"
        world = arm.matrix_world
        move = mathutils.Vector(feet_move)
        out = clip.copy()
        legs = [(f"thigh_{s}", f"calf_{s}", f"foot_{s}") for s in ("l", "r")]

        def pose(i: int, hips) -> None:
            for name, samples in clip.quats.items():
                bones[name].rotation_quaternion = mathutils.Quaternion(
                    samples[i]
                )
            bones["pelvis"].location = self.hips_to_basis(
                tuple(a + b for a, b in zip(clip.hips[i], hips))
            )
            bpy.context.view_layer.update()

        def aim(bone, target) -> None:
            """Turns `bone` (about its head, by the least rotation) to point at world `target`."""
            m = bone.matrix
            head = m.translation.copy()
            now = m.to_3x3().col[1].normalized()
            want = ((world.inverted() @ target) - head).normalized()
            turned = now.rotation_difference(want).to_matrix() @ m.to_3x3()
            bone.matrix = mathutils.Matrix.Translation(head) @ turned.to_4x4()
            bpy.context.view_layer.update()

        previous: dict[str, mathutils.Quaternion] = {}
        for i in range(len(clip)):
            pose(i, (0.0, 0.0, 0.0))
            want = {}
            for thigh, calf, foot in legs:
                target = (world @ bones[foot].head) + move
                target.z = max(target.z, floor)
                want[thigh] = (
                    target,
                    world @ bones[calf].head,
                    bones[foot].matrix.to_3x3().copy(),
                )
            pose(i, hips_move)
            for thigh, calf, foot in legs:
                target, knee_before, foot_turn = want[thigh]
                hip = world @ bones[thigh].head
                upper = (world @ bones[calf].head - hip).length
                lower = (
                    world @ bones[foot].head - world @ bones[calf].head
                ).length
                reach = target - hip
                dist = min(
                    max(reach.length, abs(upper - lower) + 1e-4),
                    upper + lower - 1e-4,
                )
                axis = reach.normalized()
                along = (upper * upper - lower * lower + dist * dist) / (
                    2 * dist
                )
                height = math.sqrt(max(upper * upper - along * along, 0.0))
                bend = (knee_before - hip) - axis * (knee_before - hip).dot(
                    axis
                )
                bend = (
                    bend.normalized()
                    if bend.length > 1e-6
                    else mathutils.Vector((0, 1, 0))
                )
                knee = hip + axis * along + bend * height
                aim(bones[thigh], knee)
                aim(bones[calf], hip + axis * dist)
                bones[foot].matrix = (
                    mathutils.Matrix.Translation(bones[foot].matrix.translation)
                    @ foot_turn.to_4x4()
                )
                bpy.context.view_layer.update()
                for name in (thigh, calf, foot):
                    q = bones[name].rotation_quaternion.copy()
                    if name in previous and q.dot(previous[name]) < 0:
                        q.negate()  # the same rotation, the near way round, so a clip never spins
                    previous[name] = q
                    out.quats[name][i] = (q.w, q.x, q.y, q.z)
        out.hips = [
            tuple(a + b for a, b in zip(h, hips_move)) for h in clip.hips
        ]  # type: ignore[misc]
        return out

    def sit_reference(self, library: str, clip: str):
        """(where the pelvis sits, every bone's rotation, how far the hips are from rest) in a
        library's seated clip's first frame, for this skeleton's size."""
        before_objects, before_actions = (
            set(bpy.data.objects),
            set(bpy.data.actions),
        )
        bpy.ops.import_scene.gltf(filepath=library)
        objects = [o for o in bpy.data.objects if o not in before_objects]
        actions = [a for a in bpy.data.actions if a not in before_actions]
        try:
            rig = next(o for o in objects if o.type == "ARMATURE")
            action = bpy.data.actions[clip]
            _assign(rig, action)
            bpy.context.scene.frame_set(int(action.frame_range[0]))
            bpy.context.view_layer.update()
            position = rig.matrix_world @ rig.pose.bones["pelvis"].head
            ratio = (
                self.pelvis_world.z
                / (rig.matrix_world @ rig.data.bones["pelvis"].head_local).z
            )
            target = (
                position.x * ratio,
                position.y * ratio,
                position.z * ratio,
            )
            quats = {}
            for bone in rig.pose.bones:
                q = bone.rotation_quaternion
                quats[bone.name] = (q.w, q.x, q.y, q.z)
            hips = tuple(t - r for t, r in zip(target, self.pelvis_world))
            return target, quats, hips
        finally:
            _remove(objects, actions)


def _process(
    clip: Clip, options: dict, seat, host: "Host | None" = None
) -> Clip:
    seat_target = seat[0] if seat else None
    if options.get("trim"):
        clip = trim_clip(clip, *options["trim"])
    limit = options.get("max_seconds")
    if limit and len(clip) > limit * 30:
        clip = trim_clip(clip, clip.frames[0], clip.frames[0] + limit * 30 - 1)
    if options.get("in_place"):
        clip = in_place_clip(clip)
    if options.get("reverse"):
        clip = reverse_clip(clip)
    if options.get("ping_pong"):
        clip = ping_pong(clip, float(options["ping_pong"]))
    if options.get("loop"):
        clip = loop_clip(clip)
    if options.get("seat") and seat_target is not None:
        rest = clip.meta["hips_rest"]
        first = tuple(r + h for r, h in zip(rest, clip.hips[0]))
        offset = seat_offset(first, seat_target, clip.foot_clearance)
        print(
            f"MIXAMO seat: hips moved by ({offset[0]:+.2f}, {offset[1]:+.2f}, {offset[2]:+.2f}) m; feet clear of the floor by {clip.foot_clearance:.2f} m"
        )
        if host is not None and options.get("plant_feet", True):
            # the hips go the whole way to our seat height and the legs follow, feet where they were
            full = seat_offset(first, seat_target, float("inf"))
            print(
                f"MIXAMO seat: legs re-solved, hips lowered by {-full[2]:+.2f} m, feet kept"
            )
            clip = host.plant_feet(clip, full, offset, host.floor)
        else:
            clip = offset_hips(clip, offset)
        if options.get("blend"):
            clip = blend_through(clip, seat[1], seat[2], int(options["blend"]))
    return clip


def convert_clips(spec_path: str) -> None:
    spec = json.loads(Path(spec_path).read_text())
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.context.scene.render.fps = 30
    host = Host(spec["host"])
    seat = None
    reference = spec.get("seat_reference")
    if reference:
        seat = host.sit_reference(reference["library"], reference["clip"])
        print(
            f"MIXAMO seat reference: pelvis at ({seat[0][0]:+.2f}, {seat[0][1]:+.2f}, {seat[0][2]:+.2f})"
        )
    references = {}
    stand = spec.get("stand_reference")
    if stand:
        _, quats, hips = host.sit_reference(stand["library"], stand["clip"])
        references["@stand"] = (quats, hips)
    made: dict[str, tuple[Clip, dict]] = {}
    derived = [e for e in spec["clips"] if "fbx" not in e and "glb" not in e]
    for entry in spec["clips"]:
        if "fbx" not in entry and "glb" not in entry:
            continue
        options = entry.get("options", {})
        if "glb" in entry:
            sources = [("", host.retarget_native(entry["glb"], entry["clip"]))]
        else:
            sources = [("", host.retarget(entry["fbx"]))]
            if options.get("mirror"):
                sources.append(
                    (MIRROR_SUFFIX, host.retarget(entry["fbx"], mirror=True))
                )
        print(f"MIXAMO {entry['name']}: {len(sources[0][1])} frames")
        for suffix, source in sources:
            products = []
            if not suffix or options.get(
                "mirror_main", True
            ):  # (a long clip may be mirrored only in its parts)
                products.append(
                    (
                        entry["name"],
                        _process(source, options, seat, host),
                        options,
                    )
                )
            for extra in options.get(
                "extract", []
            ):  # a part of the clip as a clip of its own
                sub = {**options, **extra, "extract": []}
                products.append(
                    (extra["name"], _process(source, sub, seat, host), sub)
                )
            for name, clip, used in products:
                made[name + suffix] = (clip, used)
    written = []
    for name, (clip, used) in finish_clips(made, derived, references).items():
        if not used.get("emit", True):  # a clip only made to build others from
            continue
        host.write(name, clip)
        written.append(name)
        if used.get("variants") and not name.endswith(
            MIRROR_SUFFIX
        ):  # (a mirrored copy has no variants of its own: size)
            for label, (amount, speed) in INTENSITY_VARIANTS.items():
                host.write(
                    f"{name}_{label}", scale_intensity(clip, amount, speed)
                )
                written.append(f"{name}_{label}")
    # The library is a skeleton with something skinned to it (a glTF has no skin without a mesh),
    # and its clips. The mesh is one small triangle.
    for obj in [o for o in bpy.data.objects if o.type == "MESH"]:
        bpy.data.objects.remove(obj, do_unlink=True)
    mesh = bpy.data.meshes.new("library_triangle")
    mesh.from_pydata([(0, 0, 0), (0.01, 0, 0), (0, 0, 0.01)], [], [(0, 1, 2)])
    triangle = bpy.data.objects.new("library_triangle", mesh)
    bpy.context.scene.collection.objects.link(triangle)
    group = triangle.vertex_groups.new(name="pelvis")
    group.add([0, 1, 2], 1.0, "REPLACE")
    triangle.parent = host.armature
    modifier = triangle.modifiers.new("Armature", "ARMATURE")
    modifier.object = host.armature
    bpy.ops.export_scene.gltf(
        filepath=spec["out"],
        export_format="GLB",
        export_yup=True,
        export_animations=True,
        export_animation_mode="ACTIONS",
        export_skins=True,
        export_draco_mesh_compression_enable=False,
    )
    print("MIXAMO_CLIPS_OK", spec["out"], ",".join(written))


def _quaternius_axes(host_glb: str) -> dict:
    """Each bone's Z axis at rest, in world space, on a Quaternius body. A clip turns a bone about
    its own axes, so a body only plays Quaternius clips right if its bones are rolled the same.
    """
    bpy.ops.import_scene.gltf(filepath=host_glb)
    rig = next(o for o in bpy.data.objects if o.type == "ARMATURE")
    world = rig.matrix_world.to_3x3().normalized()
    axes = {
        b.name: (
            world
            @ b.matrix_local.to_3x3().normalized()
            @ mathutils.Vector((0, 0, 1))
        ).normalized()
        for b in rig.data.bones
    }
    for obj in list(bpy.data.objects):
        bpy.data.objects.remove(obj, do_unlink=True)
    return axes


def convert_body(fbx: str, out: str, host_glb: str | None = None) -> None:
    """A Mixamo character as a Quaternius-rigged .glb: bones renamed (and the vertex groups that
    go with them) and rolled like the Quaternius bones (so that Quaternius clips turn them the right
    way), the tip bone dropped, the Quaternius `root` bone added above the hips, and the FBX's
    centimetre scale and rotation applied so it is one metre-scale, Z-up object.
    """
    bpy.ops.wm.read_factory_settings(use_empty=True)
    axes = _quaternius_axes(host_glb) if host_glb else {}
    objects, action = _import_fbx(fbx)
    arm = next(o for o in objects if o.type == "ARMATURE")
    if f"{MIXAMO_PREFIX}Hips" not in {b.name for b in arm.data.bones}:
        raise SystemExit(
            "this is not a Mixamo character: its skeleton has no mixamorig:Hips"
        )
    meshes = [o for o in objects if o.type == "MESH"]
    bpy.context.view_layer.objects.active = arm
    bpy.ops.object.mode_set(mode="EDIT")
    edit = arm.data.edit_bones
    for bone in list(edit):
        target = mixamo_to_quaternius(bone.name)
        if target is None:
            edit.remove(bone)
        else:
            bone.name = target
            if (
                target in axes
            ):  # the same roll as the Quaternius bone (the vector in this armature's own axes)
                bone.align_roll(
                    arm.matrix_world.to_3x3().normalized().inverted()
                    @ axes[target]
                )
    root = edit.new("root")
    root.head, root.tail = (0.0, 0.0, 0.0), (0.0, 0.0, 0.1)
    edit["pelvis"].parent = root
    bpy.ops.object.mode_set(mode="OBJECT")
    for mesh in meshes:
        for group in mesh.vertex_groups:
            target = mixamo_to_quaternius(group.name)
            if target:
                group.name = target
            elif group.name.startswith(MIXAMO_PREFIX):
                mesh.vertex_groups.remove(group)
    if action is not None:
        bpy.data.actions.remove(action)  # the T-pose it holds is the rest pose
    bpy.ops.object.select_all(action="DESELECT")
    for obj in (arm, *meshes):
        obj.select_set(True)
    bpy.context.view_layer.objects.active = arm
    bpy.ops.object.transform_apply(location=False, rotation=True, scale=True)
    bpy.ops.export_scene.gltf(
        filepath=out,
        export_format="GLB",
        export_yup=True,
        export_animations=False,
        export_skins=True,
        export_draco_mesh_compression_enable=False,
    )
    print("MIXAMO_BODY_OK", out)


def main() -> None:
    args = sys.argv[sys.argv.index("--") + 1 :]
    if args[0] == "clips":
        convert_clips(args[1])
    elif args[0] == "body":
        convert_body(args[1], args[2], args[3] if len(args) > 3 else None)
    else:
        raise SystemExit(f"unknown mode {args[0]!r}")


if __name__ == "__main__":
    main()
