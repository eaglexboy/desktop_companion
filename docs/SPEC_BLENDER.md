# Spec: Parametric 3D Asset Generator (`blender/`)

## Component Role & Purpose

The Parametric Asset Generator is the headless 3D character pipeline for Deskmate. Executed as an isolated subprocess by `brain/appearance/blender_gen.py` as the asset compilation phase of the redesign flow defined in [DESIGN_AVATAR_EVOLUTION_FLOW.md](DESIGN_AVATAR_EVOLUTION_FLOW.md), it consumes a declarative `LookSpec` JSON definition and generates a fully rigged, skinned, animated, and viseme-capable 3D character exported in binary glTF format (`.glb`).

Key capabilities include:

* **3-Tier Asset Resolution:** Supports user-supplied custom meshes (Tier 1), standardized library base templates (Tier 2), and zero-file procedural mathematical primitives (Tier 3).
* **Build-Time Parametric Morphs:** Applies continuous proportion sliders at build time via bone transforms and vertex morph baking, leaving the exported `.glb` clean and runtime-optimized.
* **Hybrid Wardrobe Pipeline:** Fits body garments using procedural mesh extraction, flared silhouettes, and solidification directly from base topology (guaranteeing zero clipping across any body scale) while supporting modular socket-attached mesh accessories.
* **Dual Speech Indicators:** Drives canonical 9-target facial visemes for face-bearing avatars, and marks a faceless avatar `speech_indicator_mode: "sound_waves"`: nothing on the model moves for speech, and the overlay supplies the indicator.
* **State-Driven Animation Engine:** Bakes base idles, locomotion/evasion (`move`), sustained operational loops (`think`), personality-driven mood idles (bored, curious, sleepy, fidget, focused), and conversational delivery gestures (subdued, animated, explanatory, deadpan).
* **Deterministic Procedural Seeding:** Seeds both Python's PRNG and Blender's noise generator with the LookSpec `seed`, guaranteeing bit-exact reproducible meshes across builds.
* **Programmatic Budget Enforcement:** Evaluates total triangulated geometry ($\le 30{,}000$ triangles) and `.glb` binary disk size ($\le 10\,\text{MB}$), failing fast with diagnostic exit codes if ceilings are exceeded.
* **glTF 2.0 Binary Export:** Exports unified `.glb` containers and writes authoritative ground-truth build metadata (`<out>.build.json`).

---

### Module layout

`blender/generator.py` is the entry point (`blender --python blender/generator.py -- ...`): it reads the command line, resolves the template, enforces the budgets, exports, and runs `main`. Its parts are modules beside it, layered so that each imports only from the ones before it (nothing imports `generator.py`, which runs as a script and keeps the shared state of one run): `gen_constants` (fixed tables), `gen_helpers` (colours, materials, scene reset, triangle counts), `gen_garments` (outfits, footwear and the primitives they and the accessories are made of), `gen_wardrobe` (the procedural accessories for the sockets), `gen_accessory_models` (accessory models worn on the sockets: sidecar settings, import, placement, binding), `gen_body` (the procedural body, armature, proportions, faceless speech indicator), `gen_clips` (generated clips, an imported rig's skeletal clips, limb poses as morph targets), `gen_imported` (imported models, pose fixes, relaxed arms), `gen_models` (animation libraries and outfit models), `gen_face` (the face overlay) and `gen_inspect` (`--inspect`). Tests that patch a budget patch `generator.py`'s own constant, which is why the budgets and their enforcement stay there. The brain's model tools are split the same way (`brain/appearance/models.py` with `models_cli.py`, `model_animations.py`, `model_matrix.py` and `model_conversion.py`).

## 3-Tier Template Resolution Hierarchy

```
                      LookSpec Request (e.g., standard_humanoid_female or custom:cyber_cat)
                                                 │
                                                 ▼
┌─────────────────────────────────────────────────────────────────────────────────────────────┐
│ Tier 1: User-Supplied Custom Meshes (Shape & Skinning Transfer)                             │
│ Path: <deploy_dir>/assets/custom_templates/                                                 │
│ • User drops in custom .glb or .blend files.                                                │
│ • Inspected headlessly via --inspect: discovers mesh geometry, materials, and face markers. │
│ • Bound to canonical Tier 2 skeleton via modifier deform (arbitrary retargeting deferred).  │
└─────────────────────────────────────────────────────────────────────────────────────────────┘
                                                 │ (If requested template not found in Tier 1)
                                                 ▼
┌─────────────────────────────────────────────────────────────────────────────────────────────┐
│ Tier 2: Standard Base Templates (Curated In-Repo / Release Bundle)                          │
│ Path: <deploy_dir>/assets/base_templates/                                                   │
│ • Bundled reference model in V1: standard_humanoid_female                                   │
│ • Standardized bone naming, canonical visemes, and proportion morph targets.                │
└─────────────────────────────────────────────────────────────────────────────────────────────┘
                                                 │ (If base files missing or "primitive" requested)
                                                 ▼
┌─────────────────────────────────────────────────────────────────────────────────────────────┐
│ Tier 3: Zero-Dependency Procedural Math Fallback (Default Bootstrap)                        │
│ Engine: Pure bpy procedural mesh synthesis in blender/generator.py                          │
│ • primitive_blob, primitive_mechanical, and a procedural standard_humanoid_* (head, arms,   │
│   legs, rig) -- so a humanoid is always buildable with no model installed.                  │
│ • An installed Tier 1/2 model is used as the body (scaled to 2.5 m, own rig and visemes     │
│   kept, wardrobe not layered on); a static mesh is bound to the canonical rig; a file that  │
│   cannot be imported falls back to the procedural body with a build warning.                │
│ • Generates blobs, polyhedra, abstract floating crystals, and mechanical hard-surface bots. │
│ • Requires ZERO downloaded files; 100% synthesized via code and math primitives.            │
└─────────────────────────────────────────────────────────────────────────────────────────────┘
```

---

## Invocation Contract

The generator script executes headlessly within **Blender 5.0.1** (verified host environment; **Blender 4.5 LTS+ compatibility floor**). Invocation strictly passes `--factory-startup` to guarantee an isolated, sterile execution environment immune to local user preferences, default startup blend files, or third-party addons installed in `~/.config/blender/`:

```bash
blender --background --factory-startup --python blender/generator.py -- --spec <spec_path> --out <out_path>
```

### CLI Arguments & Modes
* `--spec <path> --out <path>`: Compiles a `LookSpec` JSON into a `.glb` container and `<out>.build.json`.
* `--inspect <path> --out <path>`: Analyzes bone hierarchy, morph targets, and facial capabilities of a custom asset without compiling a scene, outputting metadata conforming to the template inspection schema.

### Thumbnail renderer (`blender/thumbnail.py`)
A second, separate headless script, run by `brain/appearance/thumbnails.py` and never imported:

```bash
blender --background --factory-startup --python blender/thumbnail.py -- <in.glb> <out.png> [size]
```
Imports the `.glb`, clears animation and forces the rest pose, frames an orthographic camera from the rest-pose vertex bounds of the model's meshes (ignoring the glTF importer's `Icosphere` bone-display helper, and not trusting a skinned mesh's `bound_box`), and renders a transparent `size` x `size` PNG (default 320) with the Workbench engine. Prints `THUMB_OK` on success. The picture is the Looks tab's per-version image ([ADR_LOOK_HISTORY.md](ADR_LOOK_HISTORY.md)); it is derived and never part of the manifest.

### Imported models: what the generator does to them
A model under `custom_templates/` (Tier 1) is imported, then:
* **Cleaned:** cameras, lights and the importer's bone-display sphere are dropped. A model with no rig of its own has the transforms carried by its parent nodes (size, rotation, axis flip) baked into its vertices, so it is not stretched or laid flat when bound to the canonical rig. A model with its own rig has its imported pose committed as the rest pose (the mesh is skinned to the pose first), because converted files often store a lying-down bind shape and put the standing figure in the joint transforms, and the exporter writes the rest pose.
* **Measured:** size, scaling to 2.5 m and `bounds` come from the deformed meshes, not the undeformed vertices.
* **Face map (optional):** a model whose face is painted into a texture has no eyelids or mouth to move. A sidecar `<model>.face.json` next to it gives the eyes' and mouth's position and size as fractions of the model's front bounding box (`x` across, `z` up, `w`/`h` the same way), plus optional `skin_at` (where to sample plain skin; default midway between the eyes), `mouth_color` (the outline, default near-black), `teeth_color` (the mouth's inside, default light grey), `smile` (0 = flat, 1+ = a visible curve), `skin` (a flat sRGB fallback for a model with no textures), `patch` (false: no skin patch over a painted mouth, for a mouth drawn on a blank screen), `oval` (a round-edged mouth), `fill` (with `oval`: the mouth's inside is a plain fill in `teeth_color`, not a strip of upper teeth, for a cartoon mouth) and `tongue_color` (adds a third viseme-driven mesh, `Tongue`, lying on the floor of the inside; it follows the same nine visemes, and a closed mouth hides it). The generator lays its own parts over the painted ones, each skinned wholly to the bone that moves the face (or `head` on the canonical rig):
  * an **eyelid** per eye with the `blink` shape key: an ellipse that is a hairline at the top edge when open and covers the eye when closed (`blink_shape_keys` reports it);
  * a **patch** hiding the painted mouth;
  * the **mouth**: a black outline shape and, over it, a lighter inside (the object named `Teeth`, the second viseme-driven mesh the overlay expects) inset from the outline, both with the nine viseme shape keys and a smile curve, so a closed mouth is the outline line and an open one a light interior with a dark rim. `has_face` becomes true and `speech_indicator_mode` is `viseme_shapekeys`.

  The eyelids and patch use **the model's own material**, with every texture coordinate pointed at a spot of plain skin, so they are lit and coloured exactly like the painted face around them (a flat colour matched by eye never rendered the same shade). Two constraints learned the hard way: those coordinates are spread over a few texels rather than identical, because a renderer that derives tangents from a degenerate map shades the part black; and no overlay shape may be a collapsed zero-area line at rest, because the normals pass cannot give it a facing and it is culled once it opens. With no sidecar the model is built faceless as before. Skinned objects lose their scale on export, so overlay parts have it baked into the geometry first.

* **Limb motion (rigged models):** the core clips drive bones named `root`/`spine`/`head`, which an imported rig does not have, and a skeletal clip on the rig's own limbs is not applied by the glTF loader the overlay uses (checked: the legs and arms did not move), so a rigged model's limb motion is baked as **morph targets**, which the loader does honour. The generator finds the limb bones by name (`thigh`, `shin`, `upper_arm`, `forearm`, `foot`; the two sides told apart by position), poses the rig with rotations given about world axes and converted into each bone's own frame (so bone orientation does not matter), records every skinned mesh's deformed vertices, and adds them as shape keys on each mesh a pose moves by more than 1 cm:
  * `run_a`, `run_b` (reported as `run_shape_keys`): the two mirrored extremes of a stride, one leg forward with a bent knee and the other trailing with the heel kicked up. A foot bone not parented to the shin is carried to the ankle's new position so the shoe does not stretch. Driven like the generated avatars' run poses.
  * `gesture_l_out`, `gesture_l_in`, `gesture_r_out`, `gesture_r_in` (reported as `gesture_shape_keys`): each arm's two extremes either side of its rest pose, so an arm swings between them. A mesh the legs and the arms both move gets the run targets only (the overlay tells a model's targets apart by their count: 1 blink, 2 run, 4 gesture, 9 viseme), with a warning.
  A rig without such bones keeps the plain hop and gets a warning.

### Subprocess Execution Rules
1. **Isolated Execution:** Relies exclusively on Blender's bundled `bpy` and Python standard library.
2. **Environment Sanitization:** Strips all host virtual environment variables (`VIRTUAL_ENV`, `PYTHONPATH`, `PYTHONHOME`) and removes the active venv path from `PATH`.
3. **Subprocess Supervision & Timeout Semantics (160s):**
   * Spawns asynchronously via `asyncio.create_subprocess_exec` within `brain/appearance/blender_gen.py`.
   * Enforces a hard **160-second wall-clock timeout** (`asyncio.wait_for(timeout=160.0)`; it was 120 s until a look that wears every animation library took 110 s to build).
   * **Timeout Handling:** If the timeout expires, the driver dispatches `SIGKILL` to the Blender process tree, logs the timeout diagnostic to `workspace_dir/build_error.log`, discards scratch build artifacts in `workspace_dir`, preserves the active `.glb` and `manifest.json` in `deploy_dir`, and returns `DESKMATE_BUILD_TIMEOUT` to the caller.
4. **Exit Code & Output Contract:**
   * On success: exits with code `0`, emitting `DESKMATE_BUILD_OK <path>` on the final stdout line.
   * On failure: exits with code `1`, emitting `DESKMATE_BUILD_FAIL <reason>` on the final stdout line.
   * On budget breach: exits with code `1`, emitting:
     ```
     DESKMATE_BUILD_FAIL budget_exceeded: triangles={n} (max 30000) or size_bytes={s} (max 15728640)
     ```
   * Non-fatal warnings and diagnostic logs are written into `<out>.build.json` under `warnings`.

---

## Canonical LookSpec Schema (`examples/sample_look.json`)

The authoritative `LookSpec` JSON schema:

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "title": "LookSpec",
  "type": "object",
  "required": ["base_template", "has_face", "palette", "wardrobe"],
  "properties": {
    "base_template": {
      "type": "string",
      "description": "Template identifier: 'standard_humanoid_female', 'primitive_blob', 'primitive_mechanical', or 'custom:<name>'"
    },
    "has_face": {
      "type": "boolean",
      "description": "Enables canonical Rhubarb facial visemes when true; marks the build `sound_waves` when false"
    },
    "seed": {
      "type": "integer",
      "default": 42,
      "description": "Deterministic integer seed for procedural math primitives and accessory detailing"
    },
    "proportions": {
      "type": "object",
      "properties": {
        "muscularity": {"type": "number", "minimum": 0.0, "maximum": 1.0, "default": 0.5},
        "femininity": {"type": "number", "minimum": 0.0, "maximum": 1.0, "default": 0.5},
        "height": {"type": "number", "minimum": 0.5, "maximum": 1.5, "default": 1.0},
        "build": {"type": "number", "minimum": 0.5, "maximum": 1.5, "default": 1.0}
      },
      "additionalProperties": false
    },
    "palette": {
      "type": "object",
      "required": ["primary", "secondary", "accent", "emissive"],
      "properties": {
        "primary": {"type": "string", "pattern": "^#[0-9A-Fa-f]{6}$"},
        "secondary": {"type": "string", "pattern": "^#[0-9A-Fa-f]{6}$"},
        "accent": {"type": "string", "pattern": "^#[0-9A-Fa-f]{6}$"},
        "emissive": {"type": "string", "pattern": "^#[0-9A-Fa-f]{6}$"}
      },
      "additionalProperties": false
    },
    "surface_material": {
      "type": "string",
      "enum": ["matte", "metallic", "glossy", "glass", "emissive_circuit"],
      "default": "matte"
    },
    "wardrobe": {
      "type": "object",
      "properties": {
        "outfit": {"type": "string", "enum": ["none", "hoodie", "wizard_robe", "tshirt_jeans", "lab_coat", "vest", "armor_light"]},
        "footwear": {"type": "string", "enum": ["none", "sneakers", "boots", "sandals"]}
      },
      "additionalProperties": false
    },
    "accessories": {
      "type": "object",
      "properties": {
        "socket_head_top": {"type": ["string", "null"]},
        "socket_eyes": {"type": ["string", "null"]},
        "socket_neck": {"type": ["string", "null"]},
        "socket_chest": {"type": ["string", "null"]},
        "socket_waist": {"type": ["string", "null"]},
        "socket_hand_R": {"type": ["string", "null"]},
        "socket_hand_L": {"type": ["string", "null"]},
        "socket_orbit": {"type": ["string", "null"]}
      },
      "additionalProperties": false
    }
  },
  "additionalProperties": false
}
```

### Deterministic Procedural Seeding
Procedural Tier 3 geometries and scatter operations must produce bit-exact identical meshes given the same `seed`. Upon execution, `generator.py` seeds both Python's standard PRNG and Blender's internal mathematical noise libraries:

```python
import random
import mathutils

seed_val = look_spec.get("seed", 42)
random.seed(seed_val)
mathutils.noise.seed_set(seed_val)
```

The resolved seed integer is persisted in `<out>.build.json` under `"seed"`.

---

## Appearance Catalog Schemas & Inspection Contract

To allow `brain/appearance/spec.py` to validate candidate LookSpecs without invoking Blender, catalog files are stored in JSON format under `<deploy_dir>/assets/catalogs/` (or generated dynamically).

### 1. Standalone `--inspect` Output Schema

Running `blender --background --factory-startup --python blender/generator.py -- --inspect <in_path> --out <out_path>` inspects a custom `.glb` or `.blend` asset and outputs the following standalone JSON payload:

```json
{
  "template_id": "cyber_cat",
  "tier": "tier_1_custom",
  "has_face": true,
  "capabilities": {
    "has_head": true,
    "has_arms": true,
    "has_legs": true,
    "has_face": true
  },
  "bone_names": [
    "root", "spine", "chest", "neck", "head",
    "arm_left", "forearm_left", "hand_left",
    "arm_right", "forearm_right", "hand_right",
    "thigh_left", "shin_left", "foot_left",
    "thigh_right", "shin_right", "foot_right"
  ],
  "supported_proportions": ["height", "build"],
  "viseme_shape_keys": [
    "viseme_A", "viseme_B", "viseme_C", "viseme_D",
    "viseme_E", "viseme_F", "viseme_G", "viseme_H", "viseme_X"
  ],
  "supported_sockets": ["socket_head_top", "socket_eyes", "socket_neck"],
  "triangle_count": 14200,
  "warnings": []
}
```

### 2. `templates_catalog.json`

Indexes all available base templates across Tier 1, 2, and 3:

```json
{
  "templates": {
    "standard_humanoid_female": {
      "tier": "tier_2_standard",
      "path": "base_templates/standard_humanoid_female.glb",
      "has_face": true,
      "capabilities": {
        "has_head": true,
        "has_arms": true,
        "has_legs": true,
        "has_face": true
      },
      "supported_proportions": ["muscularity", "femininity", "height", "build"],
      "viseme_shape_keys": [
        "viseme_A", "viseme_B", "viseme_C", "viseme_D",
        "viseme_E", "viseme_F", "viseme_G", "viseme_H", "viseme_X"
      ],
      "supported_sockets": [
        "socket_head_top", "socket_eyes", "socket_neck",
        "socket_chest", "socket_waist", "socket_hand_R", "socket_hand_L", "socket_orbit"
      ]
    },
    "primitive_blob": {
      "tier": "tier_3_primitive",
      "path": null,
      "has_face": false,
      "capabilities": {
        "has_head": true,
        "has_arms": false,
        "has_legs": false,
        "has_face": false
      },
      "supported_proportions": ["height", "build"],
      "viseme_shape_keys": [],
      "supported_sockets": ["socket_head_top", "socket_eyes", "socket_orbit"]
    }
  }
}
```

### 3. `wardrobe_catalog.json`

Declares all allowable outfits, footwear options, and socket-mounted accessories:

```json
{
  "outfits": ["none", "hoodie", "wizard_robe", "tshirt_jeans", "lab_coat", "vest", "armor_light"],
  "footwear": ["none", "sneakers", "boots", "sandals"],
  "accessories": {
    "socket_head_top": ["pointy_hat", "crown", "horns", "antenna"],
    "socket_eyes": ["glasses", "sunglasses", "goggles", "visor"],
    "socket_neck": ["scarf", "tie", "collar"],
    "socket_chest": ["badge", "backpack"],
    "socket_waist": ["belt", "pouch"],
    "socket_hand_R": ["wand", "tool"],
    "socket_hand_L": ["shield", "orb"],
    "socket_orbit": ["crystal_ring", "floating_cube"]
  }
}
```

### 4. `<out>.build.json` vs. `AssetManifest` Version Entry

`<out>.build.json` is the ground-truth metadata artifact output by `generator.py`. `manifest.json` wraps this metadata into the versioned deployment manifest:

* **In `<out>.build.json`:** Contains build-specific diagnostic data:
  ```json
  {
    "glb_filename": "look-v3.glb",
    "sha256": "4a7b9c...",
    "created_at": 1718000000.0,
    "status": "ok",
    "seed": 42,
    "evaluated_triangles": 18450,
    "size_bytes": 4829120,
    "has_face": true,
    "speech_indicator_mode": "viseme_shapekeys",
    "capabilities": {"has_head": true, "has_arms": true, "has_legs": true, "has_face": true},
    "viseme_shape_keys": ["viseme_A", "..."],
    "blink_shape_keys": ["blink"],
    "run_shape_keys": [],
    "gesture_shape_keys": [],
    "animation_clips": [{"name": "idle_lookaround", "loop": true, "duration_s": 4.0}],
    "bounds": {"height_m": 1.65, "width_m": 0.55, "depth_m": 0.32},
    "warnings": []
  }
  ```

  Every imported material is drawn from both sides (the overlay's renderer drops the faces of a one-sided material that point away, which shows as holes in a low-poly model).

  `bounds` also carries `fit_scale` (below 1; left out when 1) when the overlay is to draw the body smaller so that its gestures stay in the window.
* **In `manifest.json` (`versions[]` entry):** Mirrors `<out>.build.json` with the addition of `version: N` and the full serialized `look_spec: {...}` (used to guarantee zero-recompile rollback for kept versions, and to rebuild a pruned version on request or for its thumbnail; builds are deterministic from `seed`).
* **Over WebSocket IPC (`hot_reload_asset` & `welcome`):** The daemon broadcasts the active version entry **stripped of `look_spec` and `warnings`** to minimize payload overhead.

---

## Build-Time Proportions & Hybrid Wardrobe Architecture

### 1. Proportion Baking Procedure
To guarantee zero geometry clipping and avoid runtime morph overhead:
1. Dimensional proportions (`height`, `build`) are applied by scaling bones in Pose mode: `height` scales the $Z$-axis of spine and limb chains; `build` scales lateral ($X/Y$) bone axes. The generator applies **Pose as Rest Pose**.
2. Template morphological proportion shape keys (`muscularity`, `femininity`) are evaluated at target weights, baked permanently into the `Basis` mesh, and removed from the active shape key list.
3. Viseme shape keys are re-bound as relative vertex deltas against the newly established rest shape.
4. The exported `.glb` contains **only the 9 canonical Rhubarb viseme shape keys** as runtime morph targets.

### 2. Hybrid Wardrobe Pipeline
* **Procedural Body Garments:**
  * Duplicates vertex groups for the chest, torso, arms, and legs.
  * Extrudes loop cuts outward and downward to create realistic garment silhouettes (e.g. `wizard_robe` flares outward $15\,\text{cm}$ and extends past knees; `lab_coat` extends below waist with an open front slit).
  * Applies `Solidify` ($2\text{–}4\,\text{mm}$ thickness) and `Displace`.
  * Garment vertices inherit 100% exact bone weights from the base body, guaranteeing zero clipping during animation.
* **Modular Socket Attachments:**
  * Sockets: `socket_head_top`, `socket_eyes`, `socket_neck`, `socket_chest`, `socket_waist`, `socket_hand_R`, `socket_hand_L`, `socket_orbit`.
  * Discovers pre-modeled assets under `<deploy_dir>/assets/wardrobe/<socket>/<item>.glb`. If absent, builds parametric primitives (beveled rings for glasses, extruded cones for pointy hats).
  * Which sockets a body can carry is read from its rig (`blender/sockets.py`, `--inspect`), an accessory model's `<item>.json` sidecar sets its scale/offset/rotation and what it needs, and on an installed body accessories are bound rigidly to the socket's bone. See [ADR_ACCESSORY_MODELS.md](ADR_ACCESSORY_MODELS.md); Outfit models made for a body's skeleton (`assets/outfit_models/<body>/`) are re-bound to it, replacing the body below the neck; `--convert` shrinks and decimates a downloaded model to the budgets, and `--face-landmarks` writes the face sidecar (lip-sync/blink) for a body with real eyes and lips (`"patch": false`, `"oval": true` in the sidecar keep the lips and draw a round mouth). **Supported models: Quaternius first.** The pipeline is built and tested for Quaternius content: the Universal Base Characters (bodies), the Modular Character Outfits, the Universal Animation Library and the Animated Mech Pack. Its conventions are the reference: the bone names (`pelvis`, `spine_01`..`spine_03`, `neck_01`, `Head`, `clavicle_l`, `upperarm_l`, `lowerarm_l`, `hand_l`, `thigh_l`, `calf_l`, `foot_l`, `ball_l`, the `_r` side, fingers), the T-pose rest pose, the proportions the clip hips are scaled against, and the clip names in `blender/packs/quaternius.json`. Models and animations from anywhere else (Mixamo, Poly Pizza, a hand-made rig) are only as supported as they follow that convention: each source needs its own identifier (recognise it by its rig) and its own converter that renames or retargets its bones, rest pose and clips to the Quaternius ones before an outfit, an accessory or a clip can use it. Converters: Mixamo is built (`blender/mixamo_convert.py`, ADR_ACCESSORY_MODELS 16: `add-body` and `add-animations` take .fbx; it can also reverse, ease, join and loop clips, for work on the floor such as lying down, sleeping and working out); Sketchfab downloads are imported (ADR_ACCESSORY_MODELS 17: `add-body` cleans one up with `blender/sketchfab_convert.py` and records its licence in `<model>.credits.json`), and a body of a known rig family (Character Creator, Auto-Rig Pro) is converted to the Quaternius skeleton with the libraries retargeted onto it (ADR 18: `blender/rig_convert.py`, `blender/rigs.py`; the retargeted libraries are in `assets/animation_library_body/<body>/`, `<body>.rig.json` marks the body), clips already on the Quaternius skeleton from a tool such as Mesh2Motion are converted with `add-animations --retarget` (ADR 21: read by world orientation, so the file's own exported pose does not matter), a skeleton that is the Quaternius one under other names is converted by a rig map (`blender/rig_maps/*.json`, ADR 24: Quaternius mechs, a stylised toon rig, a Rigify-like rig), a model of separate rigid parts with no skeleton is given the Quaternius skeleton by `models rig-body` (ADR 23: `blender/part_rig.py`), and no other source is. What works for any rig is limited to what is read from the rig itself: the socket bones (`blender/sockets.py`), the face sidecar, the baked limb poses and the skeletal head idles; an outfit or an animation library only fits a body with the Quaternius bones.

An animation library in `assets/animation_library/` (`add-animations`) gives a body that has the core of its bones (hips, legs, arms; the tracks of any other bone it lacks are dropped) the clips for the roles the animation profiles name (`blender/pack_profiles.py`: `blender/packs/` and `<deploy_dir>/assets/animation_profiles/`), as actions named for the clip; the build reports them as `library_clips`, and such a body is not relaxed and gets no baked head idles or morph-target limb poses, and, with the sit clips meant for a chair, a chair named `SitProp` (ADR_ACCESSORY_MODELS 14). A mesh that heat-map weighting leaves unweighted is bound whole to its nearest bone, and `<model>.pose.json` (`roll_bones`, `face_yaw`) corrects one model's rest pose and facing. `python -m brain.appearance.models list|add-body|remove-body|add-outfit|add-accessory` manages them (`add-body --replace` swaps an installed body, `remove-body` deletes one with its sidecars and retargeted libraries, `meshes` and `rebind-mesh` list a body's meshes and bind or rename one; ADR_ACCESSORY_MODELS, consequences), and `export <file.zip>` / `import <file.zip>` carry all of them to another install (ADR_ACCESSORY_MODELS 19: a hashed bundle of models only, no companion). Which clip fills which role and how a pack's clips are converted are the animation profiles (ADR_ACCESSORY_MODELS 22: JSON: `blender/packs/quaternius.json` for the supported source, the only one shipped, and `<deploy_dir>/assets/animation_profiles/` for the user's own packs; `models profiles`, `check-profile` and `add-profile` list, check and install them).

---

## Dual Speech Indicators

### Mode A: Canonical Facial Visemes (`has_face: true`)
Implements the 9-target viseme taxonomy matching **Rhubarb Lip Sync**:

| Viseme Key | Rhubarb Code | Acoustic / Phonetic Class | Procedural Mouth Matrix (Open, Width, Round, Teeth) |
|---|---|---|---|
| `viseme_X` | `X` | Rest / Silent idle | `[0.0, 1.0, 0.0, 0]` |
| `viseme_A` | `A` | Closed mouth (`M`, `B`, `P`) | `[0.0, 0.9, 0.0, 0]` |
| `viseme_B` | `B` | Consonants / Teeth (`K`, `S`, `T`, `EE`) | `[0.2, 1.1, 0.0, 1]` |
| `viseme_C` | `C` | Open mouth (`EH`, `AE`) | `[0.5, 1.0, 0.1, 0]` |
| `viseme_D` | `D` | Wide open mouth (`AA`) | `[1.0, 1.0, 0.2, 0]` |
| `viseme_E` | `E` | Rounded open (`AO`, `ER`) | `[0.7, 0.7, 0.8, 0]` |
| `viseme_F` | `F` | Pursed lips (`UW`, `OW`, `W`) | `[0.3, 0.5, 1.0, 0]` |
| `viseme_G` | `G` | Dental-labial (`F`, `V`) | `[0.2, 1.0, 0.0, 1]` |
| `viseme_H` | `H` | Tongue behind teeth (`L`) | `[0.5, 0.9, 0.0, 0]` |

### Mode B: Faceless Speech Reactions (`has_face: false`)
A model with no mouth must not pulse when it talks: it looks wrong. It is built with `speech_indicator_mode: "sound_waves"` and no speech clip at all, and the overlay supplies the speech indicator (see SPEC_OVERLAY.md). Versions built before this are marked `talk_pulse` in the manifest, which still loads; the overlay treats any build with `has_face: false` the same way.

---

## Animation System & Capability Matrix

Clips are baked directly into the `.glb` container with metadata recorded in `<out>.build.json`:

* **Core Idles:** `idle_lookaround` (4.0s, loop), `idle_stretch` (3.0s, loop), `idle_peek` (2.5s, non-loop), `idle_blink` (3.5s, loop).
* **State Loops:** `move` (1.0s, loop), `think` (3.0s, loop).
* **Mood Idles:** `idle_bored` (5.0s, loop), `idle_curious` (4.0s, loop), `idle_sleepy` (6.0s, loop), `idle_fidget` (3.0s, loop), `idle_focused` (4.0s, loop).
* **Conversational Delivery Gestures:** `talk_subdued` (2.0s, loop), `talk_animated` (2.0s, loop), `talk_explanatory` (2.5s, loop), `talk_deadpan` (2.0s, loop).
* **Auxiliary Gestures:** `greet` (1.8s, non-loop), `celebrate` (2.2s, non-loop), `shrug` (1.5s, non-loop), `agree` (1.2s, non-loop), `disagree` (1.2s, non-loop).

### Capability Degradation Fallbacks
* `has_head == false`: Head-tilt and looking gestures drive the `spine` bone.
* `has_arms == false`: Arm-dependent clips (`greet`, `celebrate`, `shrug`) are omitted cleanly; `think` engages a pulsing spine oscillation.
* `has_legs == false`: `move` falls back to squash-and-stretch hopping or hover-gliding.
* `has_face == false`: Facial visemes omitted; the build is marked `sound_waves` (no speech clip is baked).

---

## glTF 2.0 Export Specification & Budget Enforcement

Immediately prior to binary export, `generator.py` programmatically evaluates mesh budgets to protect presentation rendering performance:

```python
import bpy
import bmesh
import os

# 1. Programmatic Polygon Budget Assertion (evaluated triangles <= 30,000)
depsgraph = bpy.context.evaluated_depsgraph_get()
total_triangles = 0
for obj in bpy.context.scene.objects:
    if obj.type == 'MESH':
        eval_obj = obj.evaluated_get(depsgraph)
        me = eval_obj.to_mesh()
        bm = bmesh.new()
        bm.from_mesh(me)
        bmesh.ops.triangulate(bm, faces=bm.faces[:])
        total_triangles += len(bm.faces)
        bm.free()
        eval_obj.to_mesh_clear()

if total_triangles > 30000:
    error_msg = f"budget_exceeded: triangles={total_triangles} (max 30000)"
    write_build_failure(out_build_json, error_msg)
    print(f"DESKMATE_BUILD_FAIL {error_msg}")
    sys.exit(1)

# 2. Mark animations as permanent users
for action in bpy.data.actions:
    action.use_fake_user = True

# 3. Export GLB
bpy.ops.export_scene.gltf(
    filepath=out_path,
    export_format="GLB",
    export_yup=True,
    export_animations=True,
    export_animation_mode="ACTIONS",
    export_morph=True,
    export_morph_normal=True,
    export_skins=True,
    export_all_influences=False,
    export_draco_mesh_compression_enable=False
)

# 4. Programmatic File Size Assertion (filesize <= 15,728,640 bytes)
file_size_bytes = os.path.getsize(out_path)
if file_size_bytes > 15728640:
    error_msg = f"budget_exceeded: size_bytes={file_size_bytes} (max 15728640)"
    write_build_failure(out_build_json, error_msg)
    print(f"DESKMATE_BUILD_FAIL {error_msg}")
    sys.exit(1)
```

* **Orientation & Scale:** $1\,\text{unit} = 1\,\text{meter}$. Origin at floor center. Faces $-Y$ in Blender ($+Z$-forward in glTF).
* **Budgets:** $\le 30{,}000$ triangles, $\le 10\,\text{MB}$ ($10{,}485{,}760\,\text{bytes}$) file size.
* **Normals:** Recalculates normals via `bmesh.ops.recalc_face_normals` before export.

---

## Subsystem Verification & Testing

* **`test_lookspec_parsing`:** Rejects invalid hex codes, proportions, or unknown sockets.
* **`test_tier3_generation`:** Compiles `primitive_blob`; asserts exit code `0`, `DESKMATE_BUILD_OK`, and valid `.glb` magic bytes.
* **`test_inspection_cli`:** Runs `--inspect`; asserts JSON hierarchy and viseme detection matching template schema.
* **`test_proportion_baking`:** Asserts bones scaled, proportion shape keys removed, and visemes intact.
* **`test_wardrobe_fit`:** Asserts garment vertices maintain signed distance $\ge 0$ in rest pose.
* **`test_faceless_model_gets_sound_waves_and_no_pulse_clip`:** Asserts `sound_waves`, no `talk_pulse` clip, and `viseme_shape_keys` empty when `has_face: false`.
* **`test_state_loops`:** Asserts `move` and `think` exist across all compiled avatars.
* **`test_environment_isolation`:** Asserts clean build execution when host venv has mismatched packages and asserts `--factory-startup` is passed.
* **`test_budget_enforcement`:** Injects high-subdivision mesh (>30k triangles) or oversized texture payload (>15MB); asserts generator aborts with exit code `1`, writes failure details to `<out>.build.json`, and outputs `DESKMATE_BUILD_FAIL budget_exceeded`.
* **`test_seed_determinism`:** Compiles procedural primitive twice with identical seed integer; asserts generated `.glb` SHA256 hashes match bit-for-bit.
