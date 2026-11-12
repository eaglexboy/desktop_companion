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
    python -m brain.appearance.models add-body <file> --name custom:my_robot --replace
    python -m brain.appearance.models remove-body custom:my_robot [--dry-run] [--force]
    python -m brain.appearance.models meshes custom:my_robot
    python -m brain.appearance.models rebind-mesh custom:my_robot --mesh <name> [--bone pelvis] [--rename body]
    python -m brain.appearance.models rig-body <parts.glb> --name custom:my_robot [--yaw 180] [--arm-offset 0.09] [--limbless] [--split-loose] [--replace]
    python -m brain.appearance.models add-accessory <file> --socket socket_head_top --name top_hat
    python -m brain.appearance.models add-outfit <file> --body standard_humanoid_female --name ranger
    python -m brain.appearance.models add-animations <file> --name ual1
    python -m brain.appearance.models add-animations Mixamo_Greeting.fbx Mixamo_Angry.fbx --name Mixamo
    python -m brain.appearance.models add-animations Bow.glb Meditate.glb --retarget --name Mesh2Motion
    python -m brain.appearance.models profiles
    python -m brain.appearance.models check-profile my_pack.json
    python -m brain.appearance.models add-profile my_pack.json
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

import functools
import json
import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Concatenate, ParamSpec, TypeVar

from blender import pack_profiles
from blender.animation_library import (  # noqa: F401 -- model_animations reaches it through here
    wanted_clips,
)
from brain.appearance.spec import (
    hold_path,
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
    # the base templates it is made for; empty means every body (a hairstyle fits the head it was
    # authored for: the male cut leaves the back of a female skull bare)
    bodies: tuple[str, ...]
    units: str  # "body": authored at the size of the body's own source model, so scaled with it
    skin: str  # "body": skinned with the body's own weights (hair); "" rigid on its socket's bone
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
        folder, name = (
            assets / "custom_templates",
            base_template[len("custom:") :],
        )
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
            capabilities={
                k: bool(v) for k, v in caps.get("capabilities", {}).items()
            },
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
        capabilities={
            k: bool(v) for k, v in entry.get("capabilities", {}).items()
        },
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
            requires = meta.get(
                "requires", [default_need] if default_need else []
            )
            found.append(
                AccessoryModel(
                    socket=socket,
                    item=glb.stem,
                    path=glb,
                    requires=tuple(r for r in requires if r in CAPABILITIES),
                    bodies=tuple(
                        b for b in meta.get("bodies", []) if isinstance(b, str)
                    ),
                    units="body" if meta.get("units") == "body" else "",
                    skin="body" if meta.get("skin") == "body" else "",
                    scale=_number(meta.get("scale"), 1.0),
                    offset=_triple(meta.get("offset"), (0.0, 0.0, 0.0)),
                    rotation=_triple(meta.get("rotation"), (0.0, 0.0, 0.0)),
                )
            )
    return found


def outfit_model_path(
    deploy_dir: Path, base_template: str, outfit: str
) -> Path:
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
    They are skinned to the body's own skeleton, so a model belongs to one body.
    """
    folder = outfit_model_path(deploy_dir, base_template, "x").parent
    return (
        sorted(p.stem for p in folder.glob("*.glb")) if folder.is_dir() else []
    )


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
        if model.bodies and base_template not in model.bodies:
            continue  # made for another body
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
        "footwear": (
            ["none"] if profile.imported else list(wardrobe["footwear"])
        ),
        "accessories": allowed,
    }


# --- command line ---------------------------------------------------------------


def _run_inspect(model: Path, out_json: Path) -> dict:
    blender = shutil.which("blender")
    if blender is None:
        raise SystemExit(
            "blender is not on PATH; it is needed to read the model's rig"
        )
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
    standard_humanoid_* id (replaces the procedural stand-in) or `custom:<n>`.
    """
    if source.suffix not in (".glb", ".blend"):
        raise SystemExit("a body must be a .glb or .blend file")
    if not source.is_file():
        raise SystemExit(f"{source} does not exist")
    if source.suffix == ".glb" and not _glb_json(source).get("meshes"):
        raise SystemExit(
            f"{source.name} has no mesh in it (is it a valid .glb?)"
        )
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


_BODY_SIDECARS = (
    ".caps.json",
    ".credits.json",
    ".face.json",
    ".pose.json",
    ".rig.json",
    ".hold",
)


def _body_files(deploy_dir: Path, name: str) -> list[Path]:
    """Everything an installed body owns: the model, the files named after it beside it, and the
    folder of animation libraries retargeted onto it. Only what exists, and only by exact name:
    outfits made for the body, and the other models, are not touched."""
    model = _model_file(deploy_dir, name)
    if model is None:
        return []
    files = [model]
    files += [
        p
        for p in (model.with_name(model.stem + s) for s in _BODY_SIDECARS)
        if p.is_file()
    ]
    derived = deploy_dir / "assets" / "animation_library_body" / model.stem
    if derived.is_dir() and not derived.is_symlink():
        files += sorted(p for p in derived.iterdir() if p.is_file())
        files.append(derived)
    return files


def current_base_template(deploy_dir: Path) -> str | None:
    """The base template of the look the companion wears now, or None if there is no look yet."""
    try:
        manifest = json.loads((deploy_dir / "manifest.json").read_text())
        current = manifest.get("current_version")
        for entry in manifest.get("versions", []):
            if entry.get("version") == current:
                return entry.get("look_spec", {}).get("base_template")
    except (OSError, ValueError, AttributeError):
        pass
    return None


def remove_body(
    deploy_dir: Path, name: str, *, force: bool = False, dry_run: bool = False
) -> list[Path]:
    """Deletes an installed body (`custom:<name>` or a standard_humanoid_* id, which falls back to
    the procedural stand-in) and what belongs to it, and returns what went (or, with `dry_run`,
    what would). Refuses the body the companion is wearing unless `force`: its look stays as it was
    built, but the next one could not use the model."""
    files = _body_files(deploy_dir, name)
    if not files:
        raise SystemExit(f"{name} is not an installed body")
    if not force and current_base_template(deploy_dir) == name:
        raise SystemExit(
            f"{name} is the body of the look the companion wears now (--force removes it anyway)"
        )
    if not dry_run:
        for path in files:
            if path.is_dir():
                try:
                    path.rmdir()  # last, once its files have gone
                except OSError:
                    pass  # something else was put in it: left alone
            else:
                path.unlink(missing_ok=True)
    return files


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
    `max_texture` px and, if `max_triangles` is set, the meshes decimated to fit.
    """
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
        raise SystemExit(
            "an outfit must be a .glb file (convert a .gltf first)"
        )
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
    title, author = (
        credits.get("title", ""),
        credits.get("author", "").split(" (")[0],
    )
    return (
        f"{title} by {author} ({credits.get('license', '').split(' ')[0]})"
        if author
        else title
    )


RIG_TIMEOUT_S = 900.0


DEFAULT_ARM_OFFSET = 0.14


P = ParamSpec("P")
R = TypeVar("R")


def _with_profiles(
    fn: Callable[Concatenate[Path, P], R],
) -> Callable[Concatenate[Path, P], R]:
    """The function reads the animation profiles of this install (the user's are in its deploy
    directory), so they are pointed at before it runs."""

    @functools.wraps(fn)
    def wrapper(deploy_dir: Path, *args: P.args, **kwargs: P.kwargs) -> R:
        pack_profiles.configure(deploy_dir)
        return fn(deploy_dir, *args, **kwargs)

    return wrapper


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
        ["--face-landmarks", str(model_file), "--out", str(out)],
        INSPECT_TIMEOUT_S,
    )
    if proc.returncode != 0 or not out.is_file():
        out.unlink(missing_ok=True)
        text = proc.stderr + proc.stdout
        reason = next(
            (
                line.strip()
                for line in reversed(text.splitlines())
                if "Error" in line
            ),
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
    bodies: list[str] | None = None,
    skin: str | None = None,
    units: str | None = None,
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
    if bodies:
        meta["bodies"] = bodies
    if skin == "body":
        meta["skin"] = "body"
    if units == "body":
        meta["units"] = "body"
    dest.with_suffix(".json").write_text(json.dumps(meta, indent=2) + "\n")
    return dest


def main(argv: list[str] | None = None) -> int:
    """The command line (`python -m brain.appearance.models`); it lives in models_cli.py."""
    from brain.appearance.models_cli import main as cli_main

    return cli_main(argv)


if __name__ == "__main__":
    # `python -m brain.appearance.models`: run the package's own copy of this module, which loads
    # the parts below in the usual order, instead of this `__main__` copy (a second one).
    from brain.appearance.models import main as _package_main

    sys.exit(_package_main())


from brain.appearance.model_animations import (  # noqa: F401 -- re-exported
    add_animation_library,
    add_profile,
    convert_mixamo_clips,
    converted_bodies,
    profiles_in_use,
    retarget_all_libraries,
    retarget_library_onto,
)

from brain.appearance.model_matrix import (  # noqa: F401 -- re-exported
    _describe_animations,
    animation_gaps,
    animation_matrix,
    animation_support,
)

from brain.appearance.model_conversion import (  # noqa: F401 -- re-exported
    body_meshes,
    rebind_mesh,
    convert_mixamo_body,
    convert_rig_body,
    convert_sketchfab_body,
    rig_body,
)
