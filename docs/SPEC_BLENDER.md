# Spec: Parametric 3D Asset Generator (`blender/`)

## Component Role & Purpose

The Parametric Asset Generator is the headless 3D character pipeline for Deskmate. Executed as an isolated subprocess by `brain/appearance/blender_gen.py` as the asset compilation phase of the redesign flow defined in `docs/DESIGN_AVATAR_EVOLUTION_FLOW.md`, it consumes a declarative `LookSpec` JSON definition and generates a fully rigged, skinned, animated, and viseme-capable 3D character exported in binary glTF format (`.glb`).

Key capabilities include:

* **3-Tier Asset Resolution:** Supports user-supplied custom meshes (Tier 1), standardized library base templates (Tier 2), and zero-file procedural mathematical primitives (Tier 3).
* **Build-Time Parametric Morphs:** Applies continuous proportion sliders at build time via bone transforms and vertex morph baking, leaving the exported `.glb` clean and runtime-optimized.
* **Hybrid Wardrobe Pipeline:** Fits body garments using procedural mesh extraction, flared silhouettes, and solidification directly from base topology (guaranteeing zero clipping across any body scale) while supporting modular socket-attached mesh accessories.
* **Dual Speech Indicators:** Drives canonical 9-target facial visemes for face-bearing avatars, and bakes rhythmically pulsed armature actions (`talk_pulse`) for faceless avatars (with real-time emissive glow modulated client-side).
* **State-Driven Animation Engine:** Bakes base idles, locomotion/evasion (`move`), sustained operational loops (`think`), personality-driven mood idles (bored, curious, sleepy, fidget, focused), and conversational delivery gestures (subdued, animated, explanatory, deadpan).
* **Deterministic Procedural Seeding:** Seeds both Python's PRNG and Blender's noise generator with the LookSpec `seed`, guaranteeing bit-exact reproducible meshes across builds.
* **Programmatic Budget Enforcement:** Evaluates total triangulated geometry ($\le 30{,}000$ triangles) and `.glb` binary disk size ($\le 10\,\text{MB}$), failing fast with diagnostic exit codes if ceilings are exceeded.
* **glTF 2.0 Binary Export:** Exports unified `.glb` containers and writes authoritative ground-truth build metadata (`<out>.build.json`).

---

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
│ • Bound to canonical Tier 2 skeleton via modifier deform (arbitrary retargeting deferred). │
└─────────────────────────────────────────────────────────────────────────────────────────────┘
                                                 │ (If requested template not found in Tier 1)
                                                 ▼
┌─────────────────────────────────────────────────────────────────────────────────────────────┐
│ Tier 2: Standard Base Templates (Curated In-Repo / Release Bundle)                         │
│ Path: <deploy_dir>/assets/base_templates/                                                   │
│ • Bundled reference models in V1: standard_humanoid_female, standard_humanoid_male          │
│ • Standardized bone naming, canonical visemes, and proportion morph targets.                │
└─────────────────────────────────────────────────────────────────────────────────────────────┘
                                                 │ (If base files missing or "primitive" requested)
                                                 ▼
┌─────────────────────────────────────────────────────────────────────────────────────────────┐
│ Tier 3: Zero-Dependency Procedural Math Fallback (Default Bootstrap)                        │
│ Engine: Pure bpy procedural mesh synthesis in blender/generator.py                          │
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

### Template Resolution Contract
* **Tier 1/2 assets.** `resolve_template()` recognizes `custom:<name>`, `standard_humanoid_female`, and `standard_humanoid_male`. A located Tier 1/2 file is imported, its skeleton, skinning, and proportion shape keys are used as the body, and viseme shape keys are bound against it. A requested identifier whose asset is missing or unusable must degrade to the Tier 3 procedural body with a warning in `<out>.build.json`, never fail the build.
* **V1 limitation:** no Tier 1/2 asset has been authored yet, so the import-and-bind path has nothing real to test against. V1 therefore implements only the degradation behavior: even when a Tier 1/2 file is found and imported, the generator discards it, builds Tier 3, and records the warning. Full integration (skinning transfer, shape-key baking) should be built alongside the first authored template asset, when it can be verified against real content.
* **Scene selection state is explicit.** Every operator whose result depends on exact selection (outfit duplication, parenting meshes to the armature) is preceded by `bpy.ops.object.select_all(action="DESELECT")`, so a previously selected mesh is never silently duplicated or parented.

### CLI Arguments & Modes
* `--spec <path> --out <path>`: Compiles a `LookSpec` JSON into a `.glb` container and `<out>.build.json`.
* `--inspect <path> --out <path>`: Analyzes bone hierarchy, morph targets, and facial capabilities of a custom asset without compiling a scene, outputting metadata conforming to the template inspection schema.

### Subprocess Execution Rules
1. **Isolated Execution:** Relies exclusively on Blender's bundled `bpy` and Python standard library.
2. **Environment Sanitization:** Strips all host virtual environment variables (`VIRTUAL_ENV`, `PYTHONPATH`, `PYTHONHOME`) and removes the active venv path from `PATH`.
3. **Subprocess Supervision & Timeout Semantics (120s):**
   * Spawns asynchronously via `asyncio.create_subprocess_exec` within `brain/appearance/blender_gen.py`.
   * Enforces a hard **120-second wall-clock timeout** (`asyncio.wait_for(timeout=120.0)`).
   * **Timeout Handling:** If the timeout expires, the driver dispatches `SIGKILL` to the Blender process tree, logs the timeout diagnostic to `workspace_dir/build_error.log`, discards scratch build artifacts in `workspace_dir`, preserves the active `.glb` and `manifest.json` in `deploy_dir`, and returns `DESKMATE_BUILD_TIMEOUT` to the caller.
4. **Exit Code & Output Contract:**
   * On success: exits with code `0`, emitting `DESKMATE_BUILD_OK <path>` on the final stdout line.
   * On failure: exits with code `1`, emitting `DESKMATE_BUILD_FAIL <reason>` on the final stdout line.
   * On budget breach: exits with code `1`, emitting:
     ```
     DESKMATE_BUILD_FAIL budget_exceeded: triangles={n} (max 30000) or size_bytes={s} (max 10485760)
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
      "description": "Template identifier: 'standard_humanoid_female', 'standard_humanoid_male', 'primitive_blob', 'primitive_mechanical', or 'custom:<name>'. Selecting a humanoid base is an explicit choice of starting mesh, not inferred from `proportions.femininity` -- `femininity` is then applied as a morph on top of whichever base was chosen, so e.g. a `standard_humanoid_male` base with a high `femininity` value is valid."
    },
    "has_face": {
      "type": "boolean",
      "description": "Enables canonical Rhubarb facial visemes when true; enables talk_pulse armature oscillation when false"
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
Procedural Tier 3 geometries and scatter operations must produce structurally identical results given the same `seed` (same triangle count, shape-key names, animation-clip list, and bounds). Byte-identical `.glb` output is **not** guaranteed: Blender's glTF exporter is itself nondeterministic for scenes with two or more meshes (a single-mesh export is byte-stable), independent of `PYTHONHASHSEED` and thread count. Upon execution, `generator.py` seeds both Python's standard PRNG and Blender's internal mathematical noise libraries:

```python
import random
import mathutils

seed_val = look_spec.get("seed", 42)
random.seed(seed_val)
mathutils.noise.seed_set(seed_val)
```

`seed` must drive every stochastic choice in procedural geometry (scatter, accessory detailing). V1's Tier 3 body construction is fully analytic and draws no random numbers, so `seed` has no visible effect there yet; the seeding is kept in place so future randomized geometry is deterministic from the start.

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
    "standard_humanoid_male": {
      "tier": "tier_2_standard",
      "path": "base_templates/standard_humanoid_male.glb",
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
    "animation_clips": [{"name": "idle_lookaround", "loop": true, "duration_s": 4.0}],
    "bounds": {"height_m": 1.65, "width_m": 0.55, "depth_m": 0.32},
    "warnings": []
  }
  ```
* **In `manifest.json` (`versions[]` entry):** Mirrors `<out>.build.json` with the addition of `version: N` and the full serialized `look_spec: {...}` (used to guarantee zero-recompile rollback).
* **Over WebSocket IPC (`hot_reload_asset` & `welcome`):** The daemon broadcasts the active version entry **stripped of `look_spec` and `warnings`** to minimize payload overhead.

---

## Build-Time Proportions & Hybrid Wardrobe Architecture

### 1. Proportion Baking Procedure
To guarantee zero geometry clipping and avoid runtime morph overhead:
0. For Tier 3 procedural bodies, proportion baking is a no-op for the morphological keys: a procedural primitive carries no `muscularity`/`femininity` shape keys. The bake step checks for key presence rather than assuming it, so it does real work as soon as a Tier 2 asset that has them is used.
1. Dimensional proportions (`height`, `build`) are applied by scaling bones in Pose mode: `height` scales the $Z$-axis of spine and limb chains; `build` scales lateral ($X/Y$) bone axes. The generator applies **Pose as Rest Pose**.
2. Template morphological proportion shape keys (`muscularity`, `femininity`) are evaluated at target weights, baked permanently into the `Basis` mesh, and removed from the active shape key list.
3. Viseme shape keys are re-bound as relative vertex deltas against the newly established rest shape.
4. The exported `.glb` contains **only the 9 canonical Rhubarb viseme shape keys** as runtime morph targets.

### 2. Hybrid Wardrobe Pipeline
* **Procedural Body Garments:**
  * Duplicates vertex groups for the chest, torso, arms, and legs.
  * Extrudes loop cuts outward to create garment silhouettes that differ by flare and thickness per outfit (e.g. `wizard_robe` flares outward $15\,\text{cm}$) and by vertical extent (a floor-length robe versus a cropped vest, open front slits).
  * `hoodie` additionally gets a hood (a shell larger than the head, shifted back so the face stays clear), hood trim and a front pocket as extra geometry; the other outfits get none yet, so they remain a recoloured, flared body.
  * V1 limitation: outfits differ only by flare and thickness (plus the hoodie's details), and every outfit wraps the full body mesh. Vertical-extent cropping needs crop geometry that can be rendered and checked against, and no per-outfit extent parameter is consumed anywhere yet; add it together with visual verification of each outfit.
  * Applies `Solidify` ($2\text{–}4\,\text{mm}$ thickness) and `Displace`.
  * Garment vertices inherit 100% exact bone weights from the base body, guaranteeing zero clipping during animation.
* **Modular Socket Attachments:**
  * Sockets: `socket_head_top`, `socket_eyes`, `socket_neck`, `socket_chest`, `socket_waist`, `socket_hand_R`, `socket_hand_L`, `socket_orbit`.
  * Discovers pre-modeled assets under `<deploy_dir>/assets/wardrobe/<socket>/<item>.glb`. If absent, builds a parametric model of the *named* item. Every catalog item has its own shape; they are not interchangeable primitives (the companion is told what it is wearing via the `[Appearance: ...]` summary, so an item that renders as something else makes it describe a look nobody can see):
    * `socket_eyes`: `glasses` (two thin rings + bridge), `sunglasses` (dark lenses), `goggles` (thick rings, glowing lenses, head strap), `visor` (one wide glowing band). Rings are rotated to face the viewer (a torus is built with its axis on Z, which is a flat line across the eyes from the front).
    * `socket_head_top`: `pointy_hat`, `crown`, `horns`, `antenna`. Kept short on purpose: the overlay's fixed camera frames the head near the top edge, so anything much over ~0.3 m above it is cropped.
    * `socket_neck`: `scarf`, `tie`, `collar`. `socket_chest`: `badge`, `backpack`. `socket_waist`: `belt`, `pouch`. `socket_hand_R/L`: `wand`, `tool`, `shield`, `orb` (floating beside the body, since Tier 3 bodies have no arms). `socket_orbit`: `crystal_ring` (a halo), `floating_cube`.
    * Placement is derived from the real body (ray casts for the front/back surface and width at a given height), not from template constants, so items sit on the body whatever its shape. Each accessory's parts are joined into one mesh before armature binding.
  * **Footwear (`sneakers`, `boots`, `sandals`):** a pair planted at the bottom of the body from its real bounds, toes toward the viewer. Neither Tier 3 template has legs, so the shoes sit directly under the body; placing them at fixed coordinates hid them inside a round body.

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

**Mouth construction contract.** The mouth and teeth are planes built flat in local XY and turned to face forward by the object transform, so local X is width, local **Y is height** (the opening), and local Z is forward (pucker). Viseme deformation must scale `y` for opening — scaling `z` is a no-op on a flat plane and leaves every open viseme looking identical. The mesh (and therefore the `Basis` shape key, which is what a client that leaves all weights at 0 renders) must be set to the **rest viseme (`X`) shape**, so a resting avatar shows a closed mouth and the `X` key carries a zero delta; each other viseme key is an absolute shape deformed from the original plane. The teeth strip's visibility scale applies to both width and height.

### Mode B: Faceless Speech Reactions (`has_face: false`)
1. **`talk_pulse` Armature Action:** Bakes vertical squash-and-stretch oscillation on the `spine` bone ($Z$-scale pulses between $0.95$ and $1.08$ at $4.5\,\text{Hz}$).
2. **Client-Side Emissive Modulation:** Blender exports the static emissive material (`mat_emissive`, `emissiveFactor` = palette color). The presentation client modulates intensity in real time in sync with audio RMS energy. V1 limitation: the client pulses at a fixed ~4.5 Hz while audio plays, because Qt's `QMediaPlayer` exposes no amplitude tap (see `docs/SPEC_OVERLAY.md`).

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
* `has_face == false`: Facial visemes omitted; `talk_pulse` baked for speech.

---

## glTF 2.0 Export Specification & Budget Enforcement

Immediately prior to binary export, `generator.py` programmatically evaluates mesh budgets to protect presentation rendering performance:

```python
import bpy
import bmesh
import os

# 0. Mesh validation: obj.data.validate() on every mesh. Automatic armature weighting on small
#    planes (mouth, teeth) can leave a vertex-group weight a hair over 1.0, which the exporter
#    would otherwise silently clamp while logging "Mesh is not valid" on every build.
for obj in bpy.context.scene.objects:
    if obj.type == 'MESH':
        obj.data.validate()

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

# 4. Programmatic File Size Assertion (filesize <= 10,485,760 bytes)
file_size_bytes = os.path.getsize(out_path)
if file_size_bytes > 10485760:
    error_msg = f"budget_exceeded: size_bytes={file_size_bytes} (max 10485760)"
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
* **`test_humanoid_base_templates_fall_back_to_tier3_when_no_asset_exists` / `test_imported_tier2_asset_falls_back_with_warning`:** Request both humanoid base identifiers with no asset on disk, and with an importable asset present; assert the build succeeds via the Tier 3 fallback and, in the latter case, records the stub warning in `<out>.build.json`.
* **`test_inspection_cli`:** Runs `--inspect`; asserts JSON hierarchy and viseme detection matching template schema.
* **`test_proportion_baking`:** Asserts bones scaled, proportion shape keys removed, and visemes intact.
* **`test_wardrobe_fit`:** Asserts garment vertices maintain signed distance $\ge 0$ in rest pose.
* **`test_faceless_pulse`:** Asserts `talk_pulse` baked and `viseme_shape_keys` empty when `has_face: false`.
* **`test_state_loops`:** Asserts `move` and `think` exist across all compiled avatars.
* **`test_environment_isolation`:** Asserts clean build execution when host venv has mismatched packages and asserts `--factory-startup` is passed.
* **`test_budget_enforcement`:** Injects high-subdivision mesh (>30k triangles) or oversized texture payload (>10MB); asserts generator aborts with exit code `1`, writes failure details to `<out>.build.json`, and outputs `DESKMATE_BUILD_FAIL budget_exceeded`.
* **`test_seed_determinism`:** Compiles procedural primitive twice with identical seed integer; asserts structural equality (triangle count, shape-key names, animation-clip list, bounds) between the two builds. SHA256 equality is deliberately not asserted because the exporter is nondeterministic for multi-mesh scenes.