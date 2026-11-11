"""Renders one PNG of a built look. Run headless, never imported:

    blender --background --factory-startup --python blender/thumbnail.py -- <in.glb> <out.png> [size]

Uses the Workbench engine (no GPU, ~3 s) and frames the camera from the
model's own bounding box, so a tall and a short avatar both fill the shot.
"""

import math
import sys

import bpy  # pyright: ignore[reportMissingImports]
from mathutils import Vector  # pyright: ignore[reportMissingImports]

glb, out = sys.argv[sys.argv.index("--") + 1 :][:2]
extra = sys.argv[sys.argv.index("--") + 3 :]
size = int(extra[0]) if extra else 320

bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.import_scene.gltf(filepath=glb)
scene = bpy.context.scene

# The imported animations would pose the model at frame 0 of whatever clip is
# active, so drop them. The pose itself stays: a model whose bind shape is not
# its standing shape (common in converted files) is only upright when posed.
# A body that wears an animation library is only in its standing pose while its idle clip
# plays (its rest pose is the library's T-pose), so that clip's first frame is the picture.
idle = bpy.data.actions.get("Idle_Loop")
for obj in bpy.data.objects:
    if obj.animation_data:
        if idle is not None and obj.type == "ARMATURE":
            obj.animation_data.action = idle
            if (
                getattr(obj.animation_data, "action_slot", None) is None
                and idle.slots
            ):
                obj.animation_data.action_slot = idle.slots[0]
        else:
            obj.animation_data_clear()
    if (
        obj.name.startswith("Icosphere") or obj.name == "SitProp"
    ):  # a bone shape; a chair shown only while sitting
        obj.hide_render = True
    # Blink and mouth shapes come in at their file defaults; the overlay zeroes
    # them on load, so a resting face is the representative picture.
    if obj.type == "MESH" and obj.data.shape_keys is not None:
        for key in obj.data.shape_keys.key_blocks:
            key.value = 0.0

if idle is not None:
    scene.frame_set(int(idle.frame_range[0]))
bpy.context.view_layer.update()
depsgraph = bpy.context.evaluated_depsgraph_get()
low, high = Vector((1e9,) * 3), Vector((-1e9,) * 3)
for obj in bpy.data.objects:
    # The glTF importer adds an "Icosphere" bone-display helper that is not in
    # the file. Bounds come from the evaluated (deformed) mesh: a skinned
    # mesh's own vertices and bound_box are its undeformed shape.
    if (
        obj.type != "MESH"
        or obj.name.startswith("Icosphere")
        or obj.name == "SitProp"
    ):
        continue
    evaluated = obj.evaluated_get(depsgraph)
    mesh = evaluated.to_mesh()
    for vertex in mesh.vertices:
        world = evaluated.matrix_world @ vertex.co
        low = Vector(map(min, low, world))
        high = Vector(map(max, high, world))
    evaluated.to_mesh_clear()
center = (low + high) / 2
height = max(high.z - low.z, 0.01)
width = max(high.x - low.x, high.y - low.y, 0.01)

scene.render.engine = "BLENDER_WORKBENCH"
scene.display.shading.light = "STUDIO"
scene.display.shading.color_type = (
    "TEXTURE"  # shows image textures, and falls back to the material colour
)
scene.render.resolution_x = scene.render.resolution_y = size
scene.render.film_transparent = True

camera = bpy.data.objects.new("cam", bpy.data.cameras.new("cam"))
scene.collection.objects.link(camera)
scene.camera = camera
camera.data.type = "ORTHO"
camera.data.ortho_scale = max(height, width) * 1.12
camera.location = (center.x, center.y - 10.0, center.z)
camera.rotation_euler = (math.radians(90), 0, 0)

scene.render.filepath = out
bpy.ops.render.render(write_still=True)
print("THUMB_OK")
