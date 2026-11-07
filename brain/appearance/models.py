"""Which accessories a body can wear, and the tool for adding models.

A body (procedural, or an installed Tier 1/2 model) can carry some sockets and
not others, depending on the bones it has; an accessory model can need a head
or hands. Nothing here is hand-listed per model: the generator's `--inspect`
reads a body's rig into `<model>.caps.json`, an accessory declares what it
needs in `<item>.json` beside its `.glb`, and `effective_wardrobe_catalog`
joins the two so the designer is only offered combinations that work.
docs/ADR_ACCESSORY_MODELS.md has the reasoning.

    python -m brain.appearance.models list
    python -m brain.appearance.models add-body <file> --name standard_humanoid_female
    python -m brain.appearance.models add-body <file> --name custom:my_robot
    python -m brain.appearance.models add-accessory <file> --socket socket_head_top --name top_hat
    python -m brain.appearance.models add-outfit <file> --body standard_humanoid_female --name ranger
    python -m brain.appearance.models add-animations <file> --name ual1
    python -m brain.appearance.models add-animations Mixamo_Greeting.fbx Mixamo_Angry.fbx --name Mixamo
    python -m brain.appearance.models add-body Mixamo_Character.fbx --name custom:vanguard
    python -m brain.appearance.models face-sidecar standard_humanoid_female
    python -m brain.appearance.models inspect-body custom:my_robot
    python -m brain.appearance.models hold custom:my_robot --reason "licence not checked"
    python -m brain.appearance.models release custom:my_robot

A body, accessory or outfit may be given as a .gltf (with its .bin and textures
beside it): it is converted to one .glb with its textures shrunk, since the
size budget is 15 MB.

A body downloaded from Sketchfab (a .glb) is recognised and cleaned up first (its helper bones,
name suffixes and shape keys, its wrapper scale), and its author and licence are recorded in
`<model>.credits.json` and shown by `list`:

    python -m brain.appearance.models add-body "Some Download.glb" --name custom:hero
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from blender import rigs, sketchfab
from brain.appearance import bundle
from blender.animation_library import (
    CHAIR_SIT_CLIPS,
    is_retargeted,
    late_starts,
    library_folder,
    rig_sidecar,
    wanted_clips,
    mixamo_derived_clips,
    REQUIRED_BONES,
    library_contents,
    library_gaps,
    library_support,
)
from brain.appearance.spec import (
    hold_path,
    hold_reason,
    load_templates_catalog,
    load_wardrobe_catalog,
)

SOCKETS = (
    "socket_head_top",
    "socket_eyes",
    "socket_neck",
    "socket_chest",
    "socket_waist",
    "socket_hand_R",
    "socket_hand_L",
    "socket_orbit",
)

# What a body must have for a socket to make sense at all. A socket that needs
# nothing (chest, waist, orbit) only needs a torso, which every body has.
SOCKET_NEEDS = {
    "socket_head_top": "has_head",
    "socket_eyes": "has_head",
    "socket_neck": "has_head",
    "socket_hand_R": "has_arms",
    "socket_hand_L": "has_arms",
}

CAPABILITIES = ("has_head", "has_arms", "has_legs")
STANDARD_HUMANOIDS = ("standard_humanoid_female", "standard_humanoid_male")
INSPECT_TIMEOUT_S = 120.0
CONVERT_TIMEOUT_S = 300.0
DEFAULT_MAX_TEXTURE = 1024
# The build's budget is 30,000 triangles in all; the body's head and eyes take about
# 7,000 and accessories a couple of thousand more, so an outfit gets the rest.
OUTFIT_MAX_TRIANGLES = 18_000
# A body is the whole figure, so it may take most of the budget; above this it is decimated
# on install (a build of it plus accessories would otherwise fail the 30,000 limit).
BODY_MAX_TRIANGLES = 24_000


@dataclass(frozen=True)
class BodyProfile:
    """What one body can carry. `sockets`/`capabilities` are None when nothing
    is known about it (a model dropped in by hand and never inspected); callers
    then stay permissive, as they were before this existed."""

    imported: bool
    sockets: frozenset[str] | None
    capabilities: dict[str, bool] | None


@dataclass(frozen=True)
class AccessoryModel:
    socket: str
    item: str
    path: Path
    requires: tuple[str, ...]
    scale: float
    offset: tuple[float, float, float]
    rotation: tuple[float, float, float]


def _read_json(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _model_file(deploy_dir: Path, base_template: str) -> Path | None:
    """The installed model file for a base template id, or None if it is
    procedural."""
    assets = deploy_dir / "assets"
    if base_template.startswith("custom:"):
        folder, name = assets / "custom_templates", base_template[len("custom:") :]
    elif base_template in STANDARD_HUMANOIDS:
        folder, name = assets / "base_templates", base_template
    else:
        return None
    for ext in (".glb", ".blend"):
        if (folder / f"{name}{ext}").is_file():
            return folder / f"{name}{ext}"
    return None


def caps_path(model_file: Path) -> Path:
    return model_file.with_name(f"{model_file.stem}.caps.json")


def body_profile(
    deploy_dir: Path, base_template: str, templates: dict | None = None
) -> BodyProfile:
    """The capabilities of one base template. An installed model's own
    `.caps.json` (written by `add-body` from the generator's inspection) wins
    over the catalog, since the catalog describes the procedural stand-in."""
    model = _model_file(deploy_dir, base_template)
    if model is not None:
        caps = _read_json(caps_path(model))
        if caps is None:
            return BodyProfile(imported=True, sockets=None, capabilities=None)
        return BodyProfile(
            imported=True,
            sockets=frozenset(caps.get("supported_sockets", [])),
            capabilities={k: bool(v) for k, v in caps.get("capabilities", {}).items()},
        )
    if base_template.startswith("custom:"):
        return BodyProfile(
            imported=False, sockets=None, capabilities=None
        )  # file gone; validation reports it
    if templates is None:
        templates = load_templates_catalog(deploy_dir / "assets" / "catalogs")[
            "templates"
        ]
    entry = (templates or {}).get(base_template)
    if entry is None:
        return BodyProfile(imported=False, sockets=None, capabilities=None)
    return BodyProfile(
        imported=False,
        sockets=frozenset(entry.get("supported_sockets", [])),
        capabilities={k: bool(v) for k, v in entry.get("capabilities", {}).items()},
    )


def _number(value: Any, default: float) -> float:
    try:
        return float(value) if value is not None else default
    except (TypeError, ValueError):
        return default


def _triple(
    value: Any, default: tuple[float, float, float]
) -> tuple[float, float, float]:
    try:
        x, y, z = (float(v) for v in value)
    except (TypeError, ValueError):
        return default
    return (x, y, z)


def discover_accessory_models(deploy_dir: Path) -> list[AccessoryModel]:
    """Every `<deploy_dir>/assets/wardrobe/<socket>/<item>.glb`, with the
    settings from its `<item>.json` sidecar when there is one. A file with no
    sidecar still works: defaults put it on its socket at natural size."""
    root = deploy_dir / "assets" / "wardrobe"
    found: list[AccessoryModel] = []
    for socket in SOCKETS:
        folder = root / socket
        if not folder.is_dir():
            continue
        for glb in sorted(folder.glob("*.glb")):
            meta = _read_json(glb.with_suffix(".json")) or {}
            default_need = SOCKET_NEEDS.get(socket)
            requires = meta.get("requires", [default_need] if default_need else [])
            found.append(
                AccessoryModel(
                    socket=socket,
                    item=glb.stem,
                    path=glb,
                    requires=tuple(r for r in requires if r in CAPABILITIES),
                    scale=_number(meta.get("scale"), 1.0),
                    offset=_triple(meta.get("offset"), (0.0, 0.0, 0.0)),
                    rotation=_triple(meta.get("rotation"), (0.0, 0.0, 0.0)),
                )
            )
    return found


def outfit_model_path(deploy_dir: Path, base_template: str, outfit: str) -> Path:
    """Where the outfit model `outfit` for a body lives. Mirrored by
    blender/generator.py `outfit_model_path` (a test keeps them equal)."""
    return (
        deploy_dir
        / "assets"
        / "outfit_models"
        / base_template.replace(":", "_")
        / f"{outfit}.glb"
    )


def discover_outfit_models(deploy_dir: Path, base_template: str) -> list[str]:
    """The outfit models made for this body (`assets/outfit_models/<body>/*.glb`).
    They are skinned to the body's own skeleton, so a model belongs to one body."""
    folder = outfit_model_path(deploy_dir, base_template, "x").parent
    return sorted(p.stem for p in folder.glob("*.glb")) if folder.is_dir() else []


def effective_wardrobe_catalog(deploy_dir: Path, base_template: str) -> dict:
    """The wardrobe options that actually work on `base_template`: the catalog,
    plus every accessory model on disk, minus sockets the body cannot carry and
    models that need something it lacks. An installed model keeps its own
    clothes, so it is offered no footwear, and only the outfit models made for
    it (none by default)."""
    catalogs = deploy_dir / "assets" / "catalogs"
    wardrobe = load_wardrobe_catalog(catalogs)
    profile = body_profile(deploy_dir, base_template)

    accessories = {
        socket: list(items) for socket, items in wardrobe["accessories"].items()
    }
    needs: dict[tuple[str, str], tuple[str, ...]] = {}
    for model in discover_accessory_models(deploy_dir):
        items = accessories.setdefault(model.socket, [])
        if model.item not in items:
            items.append(model.item)
        needs[(model.socket, model.item)] = model.requires

    allowed: dict[str, list[str]] = {}
    for socket, items in accessories.items():
        if profile.sockets is not None and socket not in profile.sockets:
            continue
        kept = [
            item
            for item in items
            if profile.capabilities is None
            or all(
                profile.capabilities.get(r, False)
                for r in needs.get((socket, item), ())
            )
        ]
        if kept:
            allowed[socket] = kept

    return {
        "outfits": (
            ["none", *discover_outfit_models(deploy_dir, base_template)]
            if profile.imported
            else list(wardrobe["outfits"])
        ),
        "footwear": ["none"] if profile.imported else list(wardrobe["footwear"]),
        "accessories": allowed,
    }


# --- command line ---------------------------------------------------------------


def _run_inspect(model: Path, out_json: Path) -> dict:
    blender = shutil.which("blender")
    if blender is None:
        raise SystemExit("blender is not on PATH; it is needed to read the model's rig")
    generator = Path(__file__).resolve().parents[2] / "blender" / "generator.py"
    proc = subprocess.run(
        [
            blender,
            "--background",
            "--factory-startup",
            "--python",
            str(generator),
            "--",
            "--inspect",
            str(model),
            "--out",
            str(out_json),
        ],
        capture_output=True,
        text=True,
        timeout=INSPECT_TIMEOUT_S,
        check=False,
    )
    if proc.returncode != 0 or not out_json.is_file():
        raise SystemExit(
            f"inspecting {model.name} failed:\n{proc.stderr[-800:] or proc.stdout[-800:]}"
        )
    return json.loads(out_json.read_text())


def add_body(deploy_dir: Path, source: Path, name: str) -> dict:
    """Installs a body model and records what its rig can carry. `name` is a
    standard_humanoid_* id (replaces the procedural stand-in) or `custom:<n>`."""
    if source.suffix not in (".glb", ".blend"):
        raise SystemExit("a body must be a .glb or .blend file")
    if not source.is_file():
        raise SystemExit(f"{source} does not exist")
    if source.suffix == ".glb" and not _glb_json(source).get("meshes"):
        raise SystemExit(f"{source.name} has no mesh in it (is it a valid .glb?)")
    if name.startswith("custom:"):
        folder, stem = (
            deploy_dir / "assets" / "custom_templates",
            name[len("custom:") :],
        )
        if not stem.replace("_", "").isalnum():
            raise SystemExit(
                "a custom body's name may only use letters, digits and underscores"
            )
    elif name in STANDARD_HUMANOIDS:
        folder, stem = deploy_dir / "assets" / "base_templates", name
    else:
        raise SystemExit(
            f"name must be one of {', '.join(STANDARD_HUMANOIDS)} or custom:<name>"
        )
    folder.mkdir(parents=True, exist_ok=True)
    dest = folder / f"{stem}{source.suffix}"
    shutil.copy2(source, dest)
    report = _run_inspect(dest, caps_path(dest))
    return report


def _generator_run(
    args: list[str], timeout: float, script: str = "generator.py"
) -> subprocess.CompletedProcess:
    blender = shutil.which("blender")
    if blender is None:
        raise SystemExit(
            "blender is not on PATH; it is needed to read and convert models"
        )
    generator = Path(__file__).resolve().parents[2] / "blender" / script
    return subprocess.run(
        [
            blender,
            "--background",
            "--factory-startup",
            "--python",
            str(generator),
            "--",
            *args,
        ],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def convert_to_glb(
    source: Path,
    out_dir: Path,
    max_texture: int = DEFAULT_MAX_TEXTURE,
    max_triangles: int = 0,
    *,
    always: bool = False,
) -> Path:
    """A .glb for `source`: itself if it already is one (unless `always`),
    otherwise a converted copy in `out_dir` with every texture shrunk to
    `max_texture` px and, if `max_triangles` is set, the meshes decimated to fit."""
    if source.suffix == ".blend" or (source.suffix == ".glb" and not always):
        return source  # a .blend body is installed as it is
    if source.suffix not in (".gltf", ".glb"):
        raise SystemExit("a model must be a .glb or .gltf file")
    out_dir.mkdir(parents=True, exist_ok=True)
    dest = out_dir / f"{source.stem}.glb"
    args = [
        "--convert",
        str(source),
        "--out",
        str(dest),
        "--max-texture",
        str(max_texture),
        "--max-triangles",
        str(max_triangles),
    ]
    proc = _generator_run(args, CONVERT_TIMEOUT_S)
    if proc.returncode != 0 or not dest.is_file():
        raise SystemExit(
            f"converting {source.name} failed:\n{proc.stderr[-800:] or proc.stdout[-800:]}"
        )
    return dest


def add_outfit(deploy_dir: Path, source: Path, body: str, name: str) -> Path:
    """Installs an outfit model for one body. It must be skinned to that body's
    skeleton; the generator checks the bones when it wears it."""
    if source.suffix != ".glb":
        raise SystemExit("an outfit must be a .glb file (convert a .gltf first)")
    if not source.is_file():
        raise SystemExit(f"{source} does not exist")
    if not _glb_json(source).get("skins"):
        raise SystemExit(
            f"{source.name} is not skinned (it has no skeleton); an outfit has to be skinned to its body's skeleton"
        )
    if not name.replace("_", "").isalnum() or name == "none":
        raise SystemExit(
            "name may only use letters, digits and underscores, and cannot be 'none'"
        )
    if _model_file(deploy_dir, body) is None:
        raise SystemExit(
            f"{body} has no installed model; install the body first (an outfit is skinned to its skeleton)"
        )
    dest = outfit_model_path(deploy_dir, body, name)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, dest)
    return dest


def animation_library_dir(deploy_dir: Path) -> Path:
    return deploy_dir / "assets" / "animation_library"


MIXAMO_TIMEOUT_S = 900.0


def convert_mixamo_body(
    source: Path, out_dir: Path, deploy_dir: Path | None = None
) -> Path:
    """A Mixamo character (.fbx) as a Quaternius-rigged .glb in `out_dir`. With an installed
    standard humanoid to copy the bones' roll from (so that Quaternius clips turn the bones the
    right way); without one the bones are only renamed."""
    out_dir.mkdir(parents=True, exist_ok=True)
    dest = out_dir / f"{source.stem}.glb"
    host = next(
        (
            deploy_dir / "assets" / "base_templates" / f"{n}.glb"
            for n in STANDARD_HUMANOIDS
            if deploy_dir is not None
            and (deploy_dir / "assets" / "base_templates" / f"{n}.glb").is_file()
        ),
        None,
    )
    proc = _generator_run(
        ["body", str(source), str(dest), *([str(host)] if host else [])],
        MIXAMO_TIMEOUT_S,
        "mixamo_convert.py",
    )
    if proc.returncode != 0 or not dest.is_file():
        text = (proc.stderr + proc.stdout).strip().splitlines()
        reason = next(
            (
                line
                for line in reversed(text)
                if "SystemExit" in line or "Error" in line
            ),
            "conversion failed",
        )
        raise SystemExit(
            f"converting {source.name} failed: {reason.split(':', 1)[-1].strip()}"
        )
    return dest


SKETCHFAB_TIMEOUT_S = 900.0
# A cleaned Sketchfab body larger than this is made again with smaller textures: a model of 50
# materials has 50 textures, and the build (budget 15 MB) also has to carry its clips.
SKETCHFAB_MAX_BYTES = 8_000_000


def credits_path(model_file: Path) -> Path:
    return model_file.with_name(f"{model_file.stem}.credits.json")


def read_credits(model_file: Path | None) -> dict | None:
    """Who made an installed model and under what licence (recorded when it came from Sketchfab)."""
    if model_file is None:
        return None
    return _read_json(credits_path(model_file)) or None


def credit_line(credits: dict | None) -> str:
    """`Title by Author (licence)`, for a listing."""
    if not credits:
        return ""
    title, author = credits.get("title", ""), credits.get("author", "").split(" (")[0]
    return (
        f"{title} by {author} ({credits.get('license', '').split(' ')[0]})"
        if author
        else title
    )


def convert_sketchfab_body(
    source: Path, out_dir: Path, max_texture: int = DEFAULT_MAX_TEXTURE
) -> Path:
    """A Sketchfab download (.glb) cleaned up as a body (blender/sketchfab_convert.py, ADR 17): the
    wrapper transforms baked in, the helper bones gone, the bones named as the author named them, no
    shape keys, textures shrunk and the meshes decimated to the body budget. Made again with
    smaller textures while the file is over `SKETCHFAB_MAX_BYTES`."""
    out_dir.mkdir(parents=True, exist_ok=True)
    dest = out_dir / f"{source.stem}.glb"
    texture = max_texture
    while True:
        proc = _generator_run(
            [str(source), str(dest), str(texture), str(BODY_MAX_TRIANGLES)],
            SKETCHFAB_TIMEOUT_S,
            "sketchfab_convert.py",
        )
        if proc.returncode != 0 or not dest.is_file():
            text = (proc.stderr + proc.stdout).strip().splitlines()
            reason = next(
                (l for l in reversed(text) if "SystemExit" in l or "Error" in l),
                "conversion failed",
            )
            raise SystemExit(
                f"cleaning {source.name} up failed: {reason.split(':', 1)[-1].strip()}"
            )
        if dest.stat().st_size <= SKETCHFAB_MAX_BYTES or texture <= 256:
            return dest
        texture //= 2
        print(
            f"    {dest.stat().st_size / 1e6:.1f} MB: making it again with {texture} px textures"
        )


RIG_TIMEOUT_S = 900.0


def convert_rig_body(source: Path, out_dir: Path) -> tuple[Path, str]:
    """A body of a known rig (Character Creator, Auto-Rig Pro) renamed onto the Quaternius skeleton
    (blender/rig_convert.py, ADR 18): (the file, what was done)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    dest = out_dir / f"{source.stem}_quaternius.glb"
    proc = _generator_run(
        ["body", str(source), str(dest)], RIG_TIMEOUT_S, "rig_convert.py"
    )
    marker = next(
        (l for l in proc.stdout.splitlines() if l.startswith("RIG_BODY_OK")), None
    )
    if proc.returncode != 0 or marker is None or not dest.is_file():
        text = (proc.stderr + proc.stdout).strip().splitlines()
        reason = next(
            (l for l in reversed(text) if "SystemExit" in l or "Error" in l),
            "conversion failed",
        )
        raise SystemExit(
            f"converting {source.name}'s rig failed: {reason.split(':', 1)[-1].strip()}"
        )
    return dest, marker.split(" ", 2)[2]


def retarget_library_onto(deploy_dir: Path, stem: str, library: Path) -> list[str]:
    """The clips of `library` (a Quaternius one) that a role uses, retargeted onto the installed
    converted body `custom:<stem>` and written as that body's own library. The clip names.
    """
    model = _model_file(deploy_dir, f"custom:{stem}")
    if model is None:
        raise SystemExit(f"custom:{stem} is not installed")
    joints, clips = library_contents(library)
    if not REQUIRED_BONES <= joints:
        return []  # not made for the Quaternius skeleton
    wanted = wanted_clips(clips)
    if not wanted:
        return []
    dest = library_folder(deploy_dir, model) / library.name
    dest.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="deskmate-retarget-") as scratch:
        spec_path = Path(scratch) / "spec.json"
        spec_path.write_text(
            json.dumps(
                {
                    "body": str(model),
                    "library": str(library),
                    "out": str(dest),
                    "clips": wanted,
                }
            )
        )
        proc = _generator_run(
            ["clips", str(spec_path)], RIG_TIMEOUT_S, "rig_convert.py"
        )
    if proc.returncode != 0 or not dest.is_file():
        raise SystemExit(
            f"retargeting {library.stem} onto {stem} failed:\n{(proc.stderr + proc.stdout)[-600:]}"
        )
    return wanted


def converted_bodies(deploy_dir: Path) -> list[str]:
    """The stems of the installed bodies that were converted from another rig."""
    folder = deploy_dir / "assets" / "custom_templates"
    return sorted(
        p.stem for p in folder.glob("*.glb") if folder.is_dir() and is_retargeted(p)
    )


def retarget_all_libraries(deploy_dir: Path, stem: str) -> None:
    folder = animation_library_dir(deploy_dir)
    for library in sorted(folder.glob("*.glb")) if folder.is_dir() else []:
        clips = retarget_library_onto(deploy_dir, stem, library)
        if clips:
            print(f"    retargeted {library.stem}: {len(clips)} clips onto {stem}")


def _library_with_clip(deploy_dir: Path, clip: str) -> Path | None:
    folder = animation_library_dir(deploy_dir)
    for library in sorted(folder.glob("*.glb")) if folder.is_dir() else []:
        if any(a.get("name") == clip for a in _glb_json(library).get("animations", [])):
            return library
    return None


def convert_mixamo_clips(
    deploy_dir: Path, sources: list[Path], name: str, options: dict | None = None
) -> tuple[Path, list[str]]:
    """Retargets Mixamo clips (.fbx) onto the Quaternius skeleton and installs them as one animation
    library. Each file becomes a clip named for it (`Mixamo_Standing_Greeting.fbx` ->
    `Standing_Greeting`), processed as `mixamo_clip_defaults` says for such a clip unless
    `options` (clip name -> overrides) says otherwise. Seated clips are lined up with the seat of
    an installed library's `Sitting_Idle_Loop`."""
    from blender.animation_library import clip_name_from_file, mixamo_clip_defaults

    host = next(
        (
            deploy_dir / "assets" / "base_templates" / f"{n}.glb"
            for n in STANDARD_HUMANOIDS
            if (deploy_dir / "assets" / "base_templates" / f"{n}.glb").is_file()
        ),
        None,
    )
    if host is None:
        raise SystemExit(
            "install a standard_humanoid body first: the clips are retargeted onto its skeleton"
        )
    reference = _library_with_clip(deploy_dir, "Sitting_Idle_Loop")
    standing = _library_with_clip(deploy_dir, "Idle_Loop")
    clips = []
    for source in sources:
        if not source.is_file():
            raise SystemExit(f"{source} does not exist")
        clip = clip_name_from_file(source.stem)
        clips.append(
            {
                "fbx": str(source),
                "name": clip,
                "options": {
                    **mixamo_clip_defaults(clip),
                    **(options or {}).get(clip, {}),
                },
            }
        )
    clips += mixamo_derived_clips({c["name"] for c in clips})
    dest = animation_library_dir(deploy_dir) / f"{name}.glb"
    dest.parent.mkdir(parents=True, exist_ok=True)
    spec = {
        "host": str(host),
        "out": str(dest),
        "clips": clips,
        "stand_reference": (
            {"library": str(standing), "clip": "Idle_Loop"} if standing else None
        ),
        "seat_reference": (
            {"library": str(reference), "clip": "Sitting_Idle_Loop"}
            if reference
            else None
        ),
    }
    with tempfile.TemporaryDirectory(prefix="deskmate-mixamo-") as scratch:
        spec_path = Path(scratch) / "spec.json"
        spec_path.write_text(json.dumps(spec))
        proc = _generator_run(
            ["clips", str(spec_path)], MIXAMO_TIMEOUT_S, "mixamo_convert.py"
        )
    marker = next(
        (
            line
            for line in proc.stdout.splitlines()
            if line.startswith("MIXAMO_CLIPS_OK")
        ),
        None,
    )
    if proc.returncode != 0 or marker is None or not dest.is_file():
        raise SystemExit(
            f"converting the clips failed:\n{(proc.stderr + proc.stdout)[-800:]}"
        )
    late = late_starts(_glb_json(dest))
    if late:
        print(
            f"warning: {len(late)} converted clip(s) start after time 0 (a T-pose flash): {', '.join(late[:5])}",
            file=sys.stderr,
        )
    return dest, marker.split(" ", 2)[2].split(",")


def add_animation_library(
    deploy_dir: Path, source: Path, name: str
) -> tuple[Path, list[str]]:
    """Installs a pack of clips made for a rig (a .glb with that rig and its animations).
    Any installed body that has all of the rig's bones plays the clips it needs from it
    (`LIBRARY_ROLES` in blender/generator.py). Returns where it went and the clip names.
    """
    if source.suffix != ".glb":
        raise SystemExit("an animation library must be a .glb file")
    if not source.is_file():
        raise SystemExit(f"{source} does not exist")
    if not name.replace("_", "").isalnum():
        raise SystemExit("name may only use letters, digits and underscores")
    gltf = _glb_json(source)
    clips = [anim.get("name", "") for anim in gltf.get("animations", [])]
    if not clips:
        raise SystemExit(f"{source.name} has no animations in it")
    if not gltf.get("skins"):
        raise SystemExit(
            f"{source.name} has no skeleton; the clips need the rig they were made for"
        )
    late = late_starts(gltf)
    if late:
        print(
            f"warning: {len(late)} clip(s) start after time 0 and will flash the rest pose "
            f"(a T-pose) when they begin: {', '.join(late[:5])}",
            file=sys.stderr,
        )
    dest = animation_library_dir(deploy_dir) / f"{name}.glb"
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, dest)
    return dest, clips


def inspect_body(deploy_dir: Path, base_template: str) -> dict:
    """(Re)reads an already installed body's rig into its `.caps.json`: for a model that
    was put in by hand before this existed, or whose file was replaced."""
    model = _model_file(deploy_dir, base_template)
    if model is None:
        raise SystemExit(f"{base_template} has no installed model")
    return _run_inspect(model, caps_path(model))


def set_hold(deploy_dir: Path, base_template: str, reason: str | None) -> None:
    """Holds an installed `custom:` model back (with `reason`) or, with None, releases it."""
    if (
        not base_template.startswith("custom:")
        or _model_file(deploy_dir, base_template) is None
    ):
        raise SystemExit(f"{base_template} is not an installed custom: model")
    marker = hold_path(deploy_dir, base_template[len("custom:") :])
    if reason is None:
        marker.unlink(missing_ok=True)
    else:
        marker.write_text(reason + "\n")


def face_sidecar_path(model_file: Path) -> Path:
    return model_file.with_name(f"{model_file.stem}.face.json")


def generate_face_sidecar(model_file: Path, *, force: bool = False) -> str:
    """Writes `<model>.face.json` (where the eyes and mouth are, for the face
    overlay that gives a model without mouth shape keys lip-sync and blinking),
    found from the model's own geometry. Returns "written", "exists" (a sidecar is
    there and `force` is off: it may be hand-made) or a reason it could not be
    found (no eye mesh, say)."""
    out = face_sidecar_path(model_file)
    if out.is_file() and not force:
        return "exists"
    proc = _generator_run(
        ["--face-landmarks", str(model_file), "--out", str(out)], INSPECT_TIMEOUT_S
    )
    if proc.returncode != 0 or not out.is_file():
        out.unlink(missing_ok=True)
        text = proc.stderr + proc.stdout
        reason = next(
            (line.strip() for line in reversed(text.splitlines()) if "Error" in line),
            "no face found",
        )
        return reason.split("Error:", 1)[-1].strip()
    return "written"


def _glb_json(path: Path) -> dict:
    """The JSON chunk of a .glb, or {} if it cannot be read."""
    try:
        with path.open(
            "rb"
        ) as f:  # the header and the JSON chunk only: a model can be hundreds of MB
            head = f.read(20)
            length = int.from_bytes(head[12:16], "little")
            return json.loads(f.read(length))
    except (OSError, ValueError):
        return {}


def glb_triangles(path: Path) -> int:
    """Triangles in a .glb, from its accessors (0 if it cannot be read)."""
    gltf = _glb_json(path)
    accessors = gltf.get("accessors", [])
    return sum(
        accessors[prim["indices"]]["count"] // 3
        for mesh in gltf.get("meshes", [])
        for prim in mesh.get("primitives", [])
        if "indices" in prim
    )


def accessory_warnings(source: Path) -> list[str]:
    """Problems an accessory file is likely to have, found without Blender."""
    gltf = _glb_json(source)
    warnings = []
    if gltf.get("skins"):
        warnings.append(
            "it is rigged; an accessory is attached to one bone as a rigid piece, so its own rig and skin are dropped (a static mesh is best)"
        )
    if not gltf.get("meshes"):
        warnings.append("no meshes found in it")
    return warnings


def add_accessory(
    deploy_dir: Path,
    source: Path,
    socket: str,
    name: str,
    *,
    scale: float = 1.0,
    offset: tuple[float, float, float] = (0.0, 0.0, 0.0),
    rotation: tuple[float, float, float] = (0.0, 0.0, 0.0),
    requires: list[str] | None = None,
) -> Path:
    if socket not in SOCKETS:
        raise SystemExit(f"socket must be one of {', '.join(SOCKETS)}")
    if source.suffix != ".glb":
        raise SystemExit("an accessory must be a .glb file")
    if not source.is_file():
        raise SystemExit(f"{source} does not exist")
    if not _glb_json(source).get("meshes"):
        raise SystemExit(f"{source.name} has no mesh in it; nothing to attach")
    if not name.replace("_", "").isalnum():
        raise SystemExit("name may only use letters, digits and underscores")
    folder = deploy_dir / "assets" / "wardrobe" / socket
    folder.mkdir(parents=True, exist_ok=True)
    dest = folder / f"{name}.glb"
    shutil.copy2(source, dest)
    default_need = SOCKET_NEEDS.get(socket)
    meta = {
        "requires": (
            requires
            if requires is not None
            else ([default_need] if default_need else [])
        ),
        "scale": scale,
        "offset": list(offset),
        "rotation": list(rotation),
    }
    dest.with_suffix(".json").write_text(json.dumps(meta, indent=2) + "\n")
    return dest


def animation_support(
    deploy_dir: Path, base_template: str
) -> dict[str, dict[str, str]] | None:
    """What each installed animation library gives this body: {library: {role: clip}}, with an
    empty dict for a library whose bones the body's rig lacks. None when nothing is known
    (a procedural body, which has no rig of the libraries', or a model never inspected).
    """
    model = _model_file(deploy_dir, base_template)
    folder = library_folder(
        deploy_dir, model
    )  # a converted body plays libraries retargeted onto it
    libraries = sorted(folder.glob("*.glb")) if folder.is_dir() else []
    if not libraries:
        return {}
    caps = _read_json(caps_path(model)) if model is not None else None
    if not caps:
        return None
    if not caps.get("bone_names"):
        return {
            "": {}
        }  # a model with no rig of its own is given the built-in one, which no library fits
    bones = set(caps["bone_names"])
    return {lib.stem: library_support(bones, lib) for lib in libraries}


def animation_gaps(deploy_dir: Path, base_template: str) -> dict[str, list[str]]:
    """{library: the bones of its rig this body lacks} for the libraries it can wear: those clips
    play without the tracks of those bones (a body with no fingers keeps still hands). A fully
    fitting library is not listed."""
    model = _model_file(deploy_dir, base_template)
    folder = library_folder(deploy_dir, model)
    caps = _read_json(caps_path(model)) if model is not None else None
    if not caps or not caps.get("bone_names") or not folder.is_dir():
        return {}
    bones = set(caps["bone_names"])
    gaps: dict[str, list[str]] = {}
    for lib in sorted(folder.glob("*.glb")):
        try:
            missing = library_gaps(library_contents(lib)[0], bones)
        except (OSError, ValueError):
            continue
        if missing:
            gaps[lib.stem] = sorted(missing)
    return gaps


def _describe_gaps(missing: list[str]) -> str:
    """`fingers and toes` style summary of a body's missing library bones."""
    kinds = []
    for word, test in (
        (
            "fingers",
            lambda b: b.startswith(("thumb", "index", "middle", "ring", "pinky")),
        ),
        ("toes", lambda b: b.startswith("ball")),
        ("clavicles", lambda b: b.startswith("clavicle")),
        ("spine", lambda b: b.startswith("spine")),
        ("neck and head", lambda b: b in ("neck_01", "Head")),
    ):
        if any(test(b) for b in missing):
            kinds.append(word)
    other = [
        b
        for b in missing
        if not any(
            b.startswith(p)
            for p in (
                "thumb",
                "index",
                "middle",
                "ring",
                "pinky",
                "ball",
                "clavicle",
                "spine",
            )
        )
        and b not in ("neck_01", "Head")
    ]
    return ", ".join(kinds + other)


def _describe_animations(
    support: dict[str, dict[str, str]], gaps: dict[str, list[str]] | None = None
) -> str:
    if "" in support:
        return "none (it has no rig of its own, so it gets the built-in one)"
    parts = []
    for library, roles in support.items():
        if not roles:
            parts.append(f"{library}: does not fit this rig")
            continue
        names = [role for role in roles if not role.startswith("sit_")]
        if "sit_idle" in roles:
            names.append(
                "sit (on a chair it brings)"
                if roles["sit_idle"] in CHAIR_SIT_CLIPS
                else "sit (on the ground)"
            )
        note = (
            f" (no {_describe_gaps(gaps[library])}: those stay still)"
            if gaps and library in gaps
            else ""
        )
        parts.append(f"{library}: {', '.join(names)}{note}")
    return "; ".join(parts)


def animation_matrix(deploy_dir: Path) -> dict:
    """What animations each installed body gets, for the status page (read-only):
    {"libraries": [{name, clips, joints}], "bodies": [{body, installed, held, libraries:
    {library: {fits, roles, sit, gaps}} | None}]}. `libraries` is None for a body whose rig was
    never inspected, `{}` for one that has no rig of its own."""
    folder = animation_library_dir(deploy_dir)
    libraries = []
    for lib in sorted(folder.glob("*.glb")) if folder.is_dir() else []:
        try:
            joints, clips = library_contents(lib)
        except (OSError, ValueError):
            continue
        libraries.append(
            {
                "name": lib.stem,
                "clips": sorted(c for c in clips if c),
                "joints": len(joints),
            }
        )
    templates = load_templates_catalog(deploy_dir / "assets" / "catalogs")["templates"]
    custom_dir = deploy_dir / "assets" / "custom_templates"
    customs = sorted(
        {p.stem for suffix in (".glb", ".blend") for p in custom_dir.glob(f"*{suffix}")}
    )
    bodies = []
    for body in sorted(templates) + [f"custom:{stem}" for stem in customs]:
        profile = body_profile(deploy_dir, body, templates)
        held = (
            hold_reason(deploy_dir, body[len("custom:") :])
            if body.startswith("custom:")
            else None
        )
        support = animation_support(deploy_dir, body) if profile.imported else {}
        gaps = animation_gaps(deploy_dir, body) if support else {}
        fits = None
        if support is not None and "" not in support:
            fits = {
                library: {
                    "fits": bool(roles),
                    "roles": [
                        r for r in roles if not r.startswith("sit_") and "." not in r
                    ],
                    "sit": (
                        ("chair" if roles["sit_idle"] in CHAIR_SIT_CLIPS else "ground")
                        if "sit_idle" in roles
                        else None
                    ),
                    "gaps": gaps.get(library, []),
                }
                for library, roles in support.items()
            }
        bodies.append(
            {
                "body": body,
                "installed": profile.imported,
                "held": held,
                "credit": credit_line(read_credits(_model_file(deploy_dir, body))),
                "libraries": fits,
            }
        )
    return {"libraries": libraries, "bodies": bodies}


def _print_matrix(deploy_dir: Path) -> None:
    templates = load_templates_catalog(deploy_dir / "assets" / "catalogs")["templates"]
    custom_dir = deploy_dir / "assets" / "custom_templates"
    customs = sorted(
        {p.stem for suffix in (".glb", ".blend") for p in custom_dir.glob(f"*{suffix}")}
    )
    bodies = sorted(templates) + [f"custom:{stem}" for stem in customs]
    for body in bodies:
        profile = body_profile(deploy_dir, body, templates)
        held = (
            hold_reason(deploy_dir, body[len("custom:") :])
            if body.startswith("custom:")
            else None
        )
        kind = ("installed model" if profile.imported else "procedural") + (
            f", HELD: {held}" if held else ""
        )
        sockets = (
            "unknown (never inspected)"
            if profile.sockets is None
            else ", ".join(s for s in SOCKETS if s in profile.sockets) or "none"
        )
        print(f"{body}  [{kind}]\n    sockets: {sockets}")
        credit = credit_line(read_credits(_model_file(deploy_dir, body)))
        if credit:
            print(f"    credit: {credit}")
        if is_retargeted(_model_file(deploy_dir, body)):
            print(
                "    rig: converted to the Quaternius skeleton; the libraries are retargeted onto it"
            )
        support = animation_support(deploy_dir, body)
        if support:
            print(
                f"    animations: {_describe_animations(support, animation_gaps(deploy_dir, body))}"
            )
        elif support is None:
            print(
                "    animations: unknown (the rig was never inspected)"
                if profile.imported
                else "    animations: none (a built-in body has its own)"
            )
        catalog = effective_wardrobe_catalog(deploy_dir, body)
        if profile.imported and len(catalog["outfits"]) > 1:
            print(f"    outfits: {', '.join(catalog['outfits'][1:])}")
        for socket, items in catalog["accessories"].items():
            print(f"    {socket}: {', '.join(items)}")
    libraries = (
        sorted(animation_library_dir(deploy_dir).glob("*.glb"))
        if animation_library_dir(deploy_dir).is_dir()
        else []
    )
    print(f"\nanimation libraries: {len(libraries)}")
    for lib in libraries:
        print(f"    {lib.stem}  {len(_glb_json(lib).get('animations', []))} clips")
    models = discover_accessory_models(deploy_dir)
    print(f"\naccessory models on disk: {len(models)}")
    for m in models:
        print(
            f"    {m.socket}/{m.item}  needs {', '.join(m.requires) or 'nothing'}  scale {m.scale}"
        )


def _export(
    deploy_dir: Path,
    out: Path,
    *,
    derived: bool,
    include_look: bool = False,
    look_version: int | None = None,
) -> int:
    seed = Path(__file__).resolve().parent / "catalogs_seed"
    try:
        report = bundle.export_bundle(
            deploy_dir,
            out,
            derived=derived,
            catalogs_seed=seed,
            include_look=include_look,
            look_version=look_version,
        )
    except bundle.BundleError as exc:
        print(f"export failed: {exc}", file=sys.stderr)
        return 1
    print(
        f"wrote {out}: {report.files} files, {report.bytes / 1e6:.1f} MB unpacked, {out.stat().st_size / 1e6:.1f} MB packed"
    )
    for kind, names in report.models.items():
        print(
            f"    {kind}: {len(names)} ({', '.join(names[:6])}{'...' if len(names) > 6 else ''})"
        )
    if report.look is not None:
        print(
            f"    look: version {report.look}, built (an install with no looks starts with it)"
        )
    print(
        "    not in it: the database, look history, voices, catalogs and tools (a bundle holds models only)"
    )
    for line in report.skipped:
        print(f"    left out: {line}")
    for line in report.warnings:
        print(f"    warning: {line}")
    return 0


def _import(deploy_dir: Path, args: argparse.Namespace) -> int:
    try:
        report = bundle.import_bundle(
            args.bundle, deploy_dir, replace=args.replace, dry_run=args.dry_run
        )
    except bundle.BundleError as exc:
        print(f"import failed: {exc}", file=sys.stderr)
        return 1
    prefix = "would " if args.dry_run else ""
    print(
        f"{prefix}add {len(report.added)}, replace {len(report.replaced)}, keep {len(report.unchanged)} already here"
    )
    for name in report.conflicts:
        print(f"    conflict, left as it is: {name} (--replace overwrites)")
    if report.look:
        print(f"    look: {report.look}")
    for note in report.notes:
        print(f"    licence: {note}")
    if args.dry_run:
        return 0 if not report.conflicts else 2
    if args.regenerate or report.derived_missing:
        stems = converted_bodies(deploy_dir)
        if args.regenerate:
            for stem in stems:
                retarget_all_libraries(deploy_dir, stem)
        elif report.derived_missing:
            print(
                f"    the libraries for {', '.join(report.derived_missing)} are not in the bundle: "
                "import again with --regenerate (needs Blender), or add-animations makes them"
            )
    return 0 if not report.conflicts else 2


def main(argv: list[str] | None = None) -> int:
    from brain.config import get_settings

    parser = argparse.ArgumentParser(
        prog="python -m brain.appearance.models",
        description=(__doc__ or "").split("\n\n")[0],
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser(
        "list",
        help="show every body, what it can carry, and the accessory models on disk",
    )
    export = sub.add_parser(
        "export",
        help="write every installed model to one .zip, to put on another install (a thumb drive)",
    )
    export.add_argument("out", type=Path)
    export.add_argument(
        "--include-look",
        action="store_true",
        help="also carry a finished look: an install with no looks starts with it as version 1, and needs no Blender until it is asked for another",
    )
    export.add_argument(
        "--look-version",
        type=int,
        help="the look to carry (default: the current one; only the newest few are kept)",
    )
    export.add_argument(
        "--no-derived",
        action="store_true",
        help="leave out the libraries retargeted onto converted bodies (import --regenerate makes them again; it needs Blender)",
    )
    bring = sub.add_parser(
        "import", help="put the models of an exported .zip into this install"
    )
    bring.add_argument("bundle", type=Path)
    bring.add_argument(
        "--replace",
        action="store_true",
        help="overwrite a model that is here with a different file (the default leaves it and says so)",
    )
    bring.add_argument(
        "--dry-run",
        action="store_true",
        help="check the bundle and say what would happen, writing nothing",
    )
    bring.add_argument(
        "--regenerate",
        action="store_true",
        help="make the retargeted libraries again on this machine (needs Blender) instead of trusting the bundle's",
    )
    body = sub.add_parser("add-body", help="install a body model and inspect its rig")
    body.add_argument("file", type=Path)
    body.add_argument(
        "--name",
        required=True,
        help="standard_humanoid_female, standard_humanoid_male or custom:<name>",
    )
    body.add_argument(
        "--no-rig",
        action="store_true",
        help="do not convert a Character Creator or Auto-Rig Pro skeleton to the Quaternius one (it then plays no animation libraries)",
    )
    inspect = sub.add_parser(
        "inspect-body",
        help="read an installed body's rig into its .caps.json (no file is copied)",
    )
    inspect.add_argument(
        "body", help="standard_humanoid_female, standard_humanoid_male or custom:<name>"
    )
    hold = sub.add_parser(
        "hold",
        help="hold an installed custom: model back (not offered, not accepted) until it is released",
    )
    hold.add_argument("body")
    hold.add_argument("--reason", default="held")
    release = sub.add_parser("release", help="release a held model")
    release.add_argument("body")
    face = sub.add_parser(
        "face-sidecar",
        help="find an installed body's eyes and mouth and write its face sidecar (lip-sync, blink)",
    )
    face.add_argument(
        "body", help="standard_humanoid_female, standard_humanoid_male or custom:<name>"
    )
    face.add_argument(
        "--force", action="store_true", help="replace an existing sidecar"
    )
    outfit = sub.add_parser(
        "add-outfit", help="install an outfit model (skinned to a body's skeleton)"
    )
    outfit.add_argument("file", type=Path)
    outfit.add_argument(
        "--body",
        required=True,
        help="the body it is for: standard_humanoid_female, standard_humanoid_male or custom:<name>",
    )
    outfit.add_argument("--name", required=True)
    outfit.add_argument(
        "--max-triangles",
        type=int,
        default=OUTFIT_MAX_TRIANGLES,
        help="decimate the outfit to about this many triangles (0 = keep as is)",
    )
    anim = sub.add_parser(
        "add-animations",
        help="install an animation library (clips for a rig; bodies with that rig play them)",
    )
    anim.add_argument(
        "file",
        type=Path,
        nargs="+",
        help="one .glb library, or Mixamo .fbx clips to retarget onto the Quaternius skeleton",
    )
    anim.add_argument("--name", required=True)
    anim.add_argument(
        "--options",
        type=Path,
        help="JSON {clip name: {loop, seat, in_place, variants, trim: [first, last], extract: [...]}} overriding what a Mixamo clip is given by default",
    )
    acc = sub.add_parser(
        "add-accessory", help="install an accessory model for a socket"
    )
    acc.add_argument("file", type=Path)
    acc.add_argument("--socket", required=True, choices=SOCKETS)
    acc.add_argument("--name", required=True)
    acc.add_argument("--scale", type=float, default=1.0)
    acc.add_argument(
        "--offset",
        type=float,
        nargs=3,
        default=(0.0, 0.0, 0.0),
        metavar=("X", "Y", "Z"),
    )
    acc.add_argument(
        "--rotation",
        type=float,
        nargs=3,
        default=(0.0, 0.0, 0.0),
        metavar=("RX", "RY", "RZ"),
        help="radians",
    )
    acc.add_argument(
        "--requires",
        nargs="*",
        choices=CAPABILITIES,
        help="override what the body must have",
    )
    for command in (body, outfit, acc):
        command.add_argument(
            "--max-texture",
            type=int,
            default=DEFAULT_MAX_TEXTURE,
            help="shrink a .gltf's textures to this many px",
        )
    args = parser.parse_args(argv)

    deploy_dir = get_settings().deploy_dir
    if args.command == "list":
        _print_matrix(deploy_dir)
        return 0
    if args.command == "export":
        return _export(
            deploy_dir,
            args.out,
            derived=not args.no_derived,
            include_look=args.include_look,
            look_version=args.look_version,
        )
    if args.command == "import":
        return _import(deploy_dir, args)
    if args.command in ("hold", "release"):
        set_hold(deploy_dir, args.body, args.reason if args.command == "hold" else None)
        print(
            f"{args.body}: {'held (' + args.reason + ')' if args.command == 'hold' else 'released'}"
        )
        return 0
    if args.command == "inspect-body":
        report = inspect_body(deploy_dir, args.body)
        caps = ", ".join(k for k, v in report["capabilities"].items() if v) or "none"
        print(
            f"{args.body}\n    capabilities: {caps}\n    sockets: {', '.join(report['supported_sockets']) or 'none'}"
        )
        for warning in report.get("warnings", []):
            print(f"    warning: {warning}")
        return 0
    if args.command == "face-sidecar":
        model = _model_file(deploy_dir, args.body)
        if model is None:
            raise SystemExit(f"{args.body} has no installed model")
        print(f"{args.body}: {generate_face_sidecar(model, force=args.force)}")
        return 0
    if args.command == "add-animations":
        if args.file[0].suffix == ".fbx":
            options = json.loads(args.options.read_text()) if args.options else None
            dest, clips = convert_mixamo_clips(
                deploy_dir, args.file, args.name, options
            )
        else:
            if len(args.file) != 1:
                raise SystemExit("give one .glb library, or any number of .fbx clips")
            dest, clips = add_animation_library(deploy_dir, args.file[0], args.name)
        print(
            f"installed {dest.name}: {len(clips)} clips ({', '.join(clips[:8])}{'...' if len(clips) > 8 else ''})"
        )
        for stem in converted_bodies(
            deploy_dir
        ):  # a body converted from another rig gets it retargeted
            done = retarget_library_onto(deploy_dir, stem, dest)
            if done:
                print(f"    retargeted {dest.stem}: {len(done)} clips onto {stem}")
        return 0
    with tempfile.TemporaryDirectory(prefix="deskmate-convert-") as scratch:
        from_fbx = (
            args.command == "add-body" and args.file.suffix == ".fbx"
        )  # its textures are not yet shrunk
        sketch = None
        if (
            args.command in ("add-body", "add-outfit", "add-accessory")
            and args.file.suffix == ".glb"
        ):
            gltf = _glb_json(args.file)
            if sketchfab.is_sketchfab(gltf):
                sketch = sketchfab.credits(gltf)
                print(f"{args.file.name}: a Sketchfab model, {credit_line(sketch)}")
                for note in sketch["notes"]:
                    print(f"    note: {note}")
                if args.command == "add-body":
                    args.file = convert_sketchfab_body(
                        args.file, Path(scratch) / "sketchfab", args.max_texture
                    )
        rig = None
        if (
            args.command == "add-body"
            and args.file.suffix == ".glb"
            and not args.no_rig
            and args.name.startswith("custom:")
        ):
            family = rigs.rig_family(library_contents(args.file)[0])
            if family is not None:
                args.file, done = convert_rig_body(args.file, Path(scratch) / "rig")
                rig = {"family": family, "retargeted": True}
                print(f"    {family} rig converted to the Quaternius skeleton: {done}")
        if from_fbx:
            print(
                f"{args.file.name}: converting a Mixamo character to the Quaternius skeleton"
            )
            args.file = convert_mixamo_body(
                args.file, Path(scratch) / "mixamo", deploy_dir
            )
        if (
            args.command == "add-outfit"
        ):  # always re-encoded: the budgets apply to a .glb outfit too
            args.file = convert_to_glb(
                args.file,
                Path(scratch),
                args.max_texture,
                args.max_triangles,
                always=True,
            )
        else:
            heavy = (
                args.command == "add-body"
                and args.file.suffix == ".glb"
                and (glb_triangles(args.file) > BODY_MAX_TRIANGLES or from_fbx)
            )
            if heavy:
                print(
                    f"{args.file.name} has {glb_triangles(args.file):,} triangles; decimating to about {BODY_MAX_TRIANGLES:,}"
                )
            args.file = convert_to_glb(
                args.file,
                Path(scratch),
                args.max_texture,
                BODY_MAX_TRIANGLES if args.command == "add-body" else 0,
                always=heavy,
            )
        if args.command == "add-body":
            report = add_body(deploy_dir, args.file, args.name)
            if sketch is not None:
                installed = _model_file(deploy_dir, args.name)
                if installed is not None:
                    credits_path(installed).write_text(
                        json.dumps(sketch, indent=2) + "\n"
                    )
            if rig is not None:
                installed = _model_file(deploy_dir, args.name)
                if installed is not None:
                    rig_sidecar(installed).write_text(json.dumps(rig, indent=2) + "\n")
                    retarget_all_libraries(deploy_dir, installed.stem)
            caps = (
                ", ".join(k for k, v in report["capabilities"].items() if v) or "none"
            )
            print(
                f"installed {args.name}\n    capabilities: {caps}\n    sockets: {', '.join(report['supported_sockets']) or 'none'}"
            )
            for warning in report.get("warnings", []):
                print(f"    warning: {warning}")
            if not report.get(
                "viseme_shape_keys"
            ):  # no lip-sync of its own: try to give it a face overlay
                installed = _model_file(deploy_dir, args.name)
                if installed is not None:
                    print(f"    face sidecar: {generate_face_sidecar(installed)}")
        elif args.command == "add-outfit":
            dest = add_outfit(deploy_dir, args.file, args.body, args.name)
            if sketch is not None:
                credits_path(dest).write_text(json.dumps(sketch, indent=2) + "\n")
            print(f"installed {dest}")
        else:
            dest = add_accessory(
                deploy_dir,
                args.file,
                args.socket,
                args.name,
                scale=args.scale,
                offset=tuple(args.offset),
                rotation=tuple(args.rotation),
                requires=args.requires,
            )
            if sketch is not None:
                credits_path(dest).write_text(json.dumps(sketch, indent=2) + "\n")
            print(f"installed {dest}")
            for warning in accessory_warnings(args.file):
                print(f"    warning: {warning}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
