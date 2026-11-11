"""Model bundles: every model the companion has, in one file that can be carried to another install.

`export_bundle` writes the models of a deploy directory (bodies, outfits, accessories, animation
libraries, with their sidecars: sockets, faces, rig, credits) as one .zip with a manifest of the
files' hashes; `import_bundle` checks a bundle and puts its files into another deploy directory. A
bundle holds no companion: nothing of the database, the look history, the voices, the catalogs (they
come from the repository) or the tools. It is made to be put on a thumb drive and opened on a machine
that has nothing else of this one: every name in it is safe on Windows, every file is hashed, and an
import refuses anything that is not what the manifest says. Decision 19 of
docs/ADR_ACCESSORY_MODELS.md has the reasons.

    python -m brain.appearance.models export models.zip [--include-look]
    python -m brain.appearance.models import models.zip [--dry-run] [--replace] [--regenerate]

With `--include-look` the bundle also carries one finished look (a built .glb and its record), which an
import into an install that has no looks yet sets up as version 1 and the current look: the companion
starts with it and needs no Blender until the first time it is asked for another.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from pydantic import ValidationError

from brain.appearance.manifest import (
    AssetManifest,
    AssetVersionEntry,
    atomic_write,
    glb_filename_for,
    load_manifest,
    manifest_path,
    save_manifest,
)

FORMAT = 1
MANIFEST = "bundle.json"
CREDITS = "CREDITS.txt"

# The directories under <deploy_dir>/assets that hold models the user installed.
MODEL_DIRS = (
    "base_templates",
    "custom_templates",
    "outfit_models",
    "wardrobe",
    "animation_library",
    "animation_profiles",  # the user's animation profiles (blender/pack_profiles.py)
)
# What is made from them (a library retargeted onto a body converted from another rig): in the bundle by
# default, so that a machine without Blender can use it, and made again on request.
DERIVED_DIRS = ("animation_library_body",)
ALLOWED_SUFFIXES = (".glb", ".blend", ".json")
LOOK_MEMBERS = (
    "look/look.glb",
    "look/entry.json",
    "look/look.png",
)  # the one finished look a bundle may carry

FAT32_LIMIT = 4 * 1024**3 - 1  # a thumb drive formatted FAT32 cannot hold a bigger file
MAX_UNPACKED_BYTES = (
    8 * 1024**3
)  # an import refuses a bundle that unpacks to more than this
MAX_MANIFEST_BYTES = 8 * 1024**2
MAX_PATH_CHARS = (
    150  # a Windows path is 260 characters in all, the deploy directory's included
)

_RESERVED = {
    "con",
    "prn",
    "aux",
    "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}
_BAD_CHARS = re.compile(r'[<>:"\\|?*\x00-\x1f]')


class BundleError(Exception):
    """A bundle that cannot be made or must not be opened (the message says why)."""


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def windows_problem(rel: str) -> str | None:
    """Why a relative path (with `/` separators) cannot be a file on Windows, None if it can."""
    if len(rel) > MAX_PATH_CHARS:
        return f"path is {len(rel)} characters (more than {MAX_PATH_CHARS}: too long once the install directory is added)"
    for part in rel.split("/"):
        if _BAD_CHARS.search(part):
            return f"{part!r} has a character Windows does not allow in a name"
        if part.endswith((" ", ".")):
            return f"{part!r} ends in a space or a dot"
        if part.split(".")[0].lower() in _RESERVED:
            return f"{part!r} is a reserved name on Windows"
    return None


def safe_member(name: str) -> str | None:
    """Why a name inside a bundle may not be unpacked (it could land outside the install, or is not a
    model file), None if it may."""
    if (
        name.startswith("/")
        or "\\" in name
        or re.match(r"^[A-Za-z]:", name)
        or "\x00" in name
    ):
        return "an absolute or Windows-style path"
    path = PurePosixPath(name)
    if any(part in ("", ".", "..") for part in path.parts):
        return "a path that climbs out of the install"
    if name in LOOK_MEMBERS:
        return None
    allowed = MODEL_DIRS + DERIVED_DIRS
    if len(path.parts) < 3 or path.parts[0] != "assets" or path.parts[1] not in allowed:
        return "not under a model folder"
    if path.suffix.lower() not in ALLOWED_SUFFIXES:
        return f"a {path.suffix or 'file without a suffix'} is not a model file"
    return windows_problem(name)


@dataclass
class ExportReport:
    out: Path
    files: int = 0
    bytes: int = 0
    by_folder: dict[str, int] = field(default_factory=dict)
    skipped: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    models: dict[str, list[str]] = field(default_factory=dict)
    look: int | None = None  # the version of the look the bundle carries, if it does


def _gather(assets: Path, derived: bool) -> tuple[list[tuple[str, Path]], list[str]]:
    """[(name in the bundle, file)] for every model file, and what was left out."""
    folders = MODEL_DIRS + (DERIVED_DIRS if derived else ())
    found, skipped = [], []
    for folder in folders:
        root = assets / folder
        if not root.is_dir():
            continue
        for path in sorted(p for p in root.rglob("*") if p.is_file()):
            rel = path.relative_to(assets.parent).as_posix()
            if path.suffix.lower() not in ALLOWED_SUFFIXES:
                skipped.append(f"{rel} (not a model file)")
            elif path.name.endswith(".hold") or path.name.startswith("."):
                skipped.append(f"{rel} (state, not a model)")
            else:
                found.append((rel, path))
    return found, skipped


def _kind(rel: str) -> str:
    folder = PurePosixPath(rel).parts[1]
    return {
        "base_templates": "bodies (standard)",
        "custom_templates": "bodies",
        "outfit_models": "outfits",
        "wardrobe": "accessories",
        "animation_library": "animation libraries",
        "animation_profiles": "animation profiles",
        "animation_library_body": "retargeted libraries",
    }.get(folder, folder)


def _tool_version(command: list[str]) -> str:
    try:
        out = subprocess.run(
            command, capture_output=True, text=True, timeout=15, check=False
        ).stdout
        return out.strip().splitlines()[0] if out.strip() else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def _credits_text(files: list[tuple[str, Path]]) -> str:
    lines = ["Credits and licences of the models in this bundle", "=" * 49, ""]
    credited = 0
    for rel, path in files:
        if not rel.endswith(".credits.json"):
            continue
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        credited += 1
        model = rel.removesuffix(".credits.json")
        lines.append(f"{model}.glb")
        lines.append(f"  {data.get('title', '')} by {data.get('author', '')}")
        lines.append(f"  licence: {data.get('license', '')}")
        if data.get("source"):
            lines.append(f"  source: {data['source']}")
        for note in data.get("notes", []):
            lines.append(f"  - {note}")
        lines.append("")
    if not credited:
        lines.append("No credits were recorded for these models.")
    lines += [
        "",
        "A model with no credit recorded here came with its own terms (the Quaternius packs are CC0);",
        "read them before sharing a bundle with anyone else.",
    ]
    return "\n".join(lines) + "\n"


def _look_to_carry(
    deploy_dir: Path, version: int | None
) -> tuple[AssetVersionEntry, Path, Path | None]:
    """(the record of a look, its built .glb, its picture or None): the current look unless `version`."""
    try:
        manifest = load_manifest(deploy_dir)
    except (ValidationError, ValueError) as exc:
        raise BundleError(f"the manifest cannot be read: {exc}") from None
    if manifest is None:
        raise BundleError("this install has no looks yet: there is none to include")
    entry = manifest.get_version(
        version if version is not None else manifest.current_version
    )
    if entry is None:
        raise BundleError(f"there is no look version {version}")
    if entry.status != "ok":
        raise BundleError(
            f"look version {entry.version} is {entry.status}: it cannot be carried"
        )
    glb = deploy_dir / "assets" / entry.glb_filename
    if not glb.is_file():
        raise BundleError(
            f"look version {entry.version}'s file is not kept (only the newest few are): pick another with --look-version"
        )
    if sha256_of(glb) != entry.sha256:
        raise BundleError(
            f"look version {entry.version}'s file does not match its record (damaged?)"
        )
    picture = deploy_dir / "looks" / f"look-v{entry.version}.png"
    return entry, glb, picture if picture.is_file() else None


def export_bundle(
    deploy_dir: Path,
    out: Path,
    *,
    derived: bool = True,
    catalogs_seed: Path | None = None,
    include_look: bool = False,
    look_version: int | None = None,
) -> ExportReport:
    """Writes every model of `deploy_dir` to the .zip `out` (atomically: a failed export leaves no file),
    and with `include_look` one finished look (the current one, or `look_version`)."""
    assets = deploy_dir / "assets"
    if not assets.is_dir():
        raise BundleError(f"{assets} does not exist: nothing to export")
    files, skipped = _gather(assets, derived)
    if not files:
        raise BundleError("there are no models to export")
    report = ExportReport(out=out, skipped=skipped)
    look = _look_to_carry(deploy_dir, look_version) if include_look else None

    seen: dict[str, str] = {}
    for rel, _path in files:
        problem = windows_problem(rel)
        if problem:
            raise BundleError(f"{rel}: {problem}")
        if rel.lower() in seen:
            raise BundleError(
                f"{rel} and {seen[rel.lower()]} differ only in upper and lower case: Windows cannot keep both"
            )
        seen[rel.lower()] = rel

    if (
        catalogs_seed is not None
    ):  # the catalogs are not exported: say so if they were changed
        for name in ("templates_catalog.json", "wardrobe_catalog.json"):
            have, seed = assets / "catalogs" / name, catalogs_seed / name
            if (
                have.is_file()
                and seed.is_file()
                and have.read_bytes() != seed.read_bytes()
            ):
                report.warnings.append(
                    f"assets/catalogs/{name} differs from the repository's seed and is not exported"
                )

    manifest_files: dict[str, dict] = {}
    models: dict[str, list[str]] = {}
    out.parent.mkdir(parents=True, exist_ok=True)
    handle, partial_name = tempfile.mkstemp(
        prefix=out.name + ".", suffix=".partial", dir=out.parent
    )
    os.close(handle)
    partial = Path(partial_name)
    try:
        with zipfile.ZipFile(
            partial, "w", zipfile.ZIP_DEFLATED, compresslevel=6
        ) as bundle:
            for rel, path in files:
                bundle.write(path, rel)
                size = path.stat().st_size
                manifest_files[rel] = {"sha256": sha256_of(path), "size": size}
                report.files += 1
                report.bytes += size
                kind = _kind(rel)
                report.by_folder[kind] = report.by_folder.get(kind, 0) + size
                if rel.endswith((".glb", ".blend")):
                    parts = PurePosixPath(
                        rel
                    ).parts  # an outfit or a retargeted library is named with its body
                    label = (
                        PurePosixPath(rel).stem
                        if len(parts) == 3
                        else f"{parts[2]}/{PurePosixPath(rel).stem}"
                    )
                    models.setdefault(kind, []).append(label)
            look_info = None
            if look is not None:
                entry, glb, picture = look
                look_files = {
                    "look/look.glb": glb.read_bytes(),
                    "look/entry.json": (
                        entry.model_dump_json(indent=2) + "\n"
                    ).encode(),
                }
                if picture is not None:
                    look_files["look/look.png"] = picture.read_bytes()
                for name, data in look_files.items():
                    bundle.writestr(name, data)
                    manifest_files[name] = {
                        "sha256": hashlib.sha256(data).hexdigest(),
                        "size": len(data),
                    }
                    report.bytes += len(data)
                look_info = {
                    "from_version": entry.version,
                    "base_template": entry.look_spec.get("base_template", ""),
                }
                report.look = entry.version
            manifest = {
                "format": FORMAT,
                "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "derived_included": derived,
                "blender": _tool_version(["blender", "--version"]),
                "revision": _tool_version(
                    [
                        "git",
                        "-C",
                        str(Path(__file__).resolve().parents[2]),
                        "rev-parse",
                        "HEAD",
                    ]
                ),
                "models": {kind: sorted(names) for kind, names in models.items()},
                "look": look_info,
                "files": manifest_files,
            }
            bundle.writestr(MANIFEST, json.dumps(manifest, indent=2) + "\n")
            bundle.writestr(CREDITS, _credits_text(files))
        if partial.stat().st_size > FAT32_LIMIT:
            report.warnings.append(
                "the bundle is over 4 GB: a FAT32 thumb drive cannot hold it (exFAT and NTFS can)"
            )
        os.replace(partial, out)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
    report.models = models
    return report


# --- import ---------------------------------------------------------------------------------------


@dataclass
class ImportReport:
    added: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    replaced: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)  # licence terms of what came in
    derived_missing: list[str] = field(default_factory=list)
    manifest: dict = field(default_factory=dict)
    look: str = ""  # what became of the look the bundle carries ("" if it carries none)


def _look_in(bundle: zipfile.ZipFile, listed: dict) -> AssetVersionEntry | None:
    """The record of the look a bundle carries, checked against its file, None if it carries none."""
    if "look/look.glb" not in listed or "look/entry.json" not in listed:
        return None
    try:
        entry = AssetVersionEntry.model_validate_json(bundle.read("look/entry.json"))
    except (ValidationError, ValueError) as exc:
        raise BundleError(f"look/entry.json is not a look record: {exc}") from None
    if entry.sha256 != listed["look/look.glb"]["sha256"]:
        raise BundleError("the look's file is not the one its record describes")
    return entry


def _seed_look(
    bundle: zipfile.ZipFile, entry: AssetVersionEntry, deploy_dir: Path
) -> None:
    """Sets the look up as version 1 and the current look, the way a first publish does: the file into
    assets/ and data/current/, then the manifest, which is what points at them."""
    data = bundle.read("look/look.glb")
    one = entry.model_copy(update={"version": 1, "glb_filename": glb_filename_for(1)})
    for folder in (deploy_dir / "assets", deploy_dir / "data" / "current"):
        folder.mkdir(
            parents=True, exist_ok=True
        )  # a fresh install has neither until the brain first runs
    atomic_write(deploy_dir / "assets" / one.glb_filename, data)
    atomic_write(deploy_dir / "data" / "current" / one.glb_filename, data)
    if "look/look.png" in bundle.namelist():
        (deploy_dir / "looks").mkdir(parents=True, exist_ok=True)
        atomic_write(deploy_dir / "looks" / "look-v1.png", bundle.read("look/look.png"))
    save_manifest(deploy_dir, AssetManifest(current_version=1, versions=[one]))


def read_manifest(bundle: zipfile.ZipFile) -> dict:
    try:
        info = bundle.getinfo(MANIFEST)
    except KeyError:
        raise BundleError(f"this is not a model bundle: it has no {MANIFEST}") from None
    if info.file_size > MAX_MANIFEST_BYTES:
        raise BundleError(f"{MANIFEST} is {info.file_size} bytes: not a model bundle")
    try:
        manifest = json.loads(bundle.read(MANIFEST))
    except ValueError:
        raise BundleError(f"{MANIFEST} is not valid JSON") from None
    if not isinstance(manifest, dict) or not isinstance(manifest.get("files"), dict):
        raise BundleError(f"{MANIFEST} has no list of files")
    if not isinstance(manifest.get("format"), int) or manifest["format"] > FORMAT:
        raise BundleError(
            f"this bundle is format {manifest.get('format')!r}; this install reads format {FORMAT} and earlier: update it"
        )
    return manifest


def _verify(bundle: zipfile.ZipFile, manifest: dict) -> None:
    """Every member is in the manifest, is a safe model file, and has the size and hash the manifest says."""
    listed = manifest["files"]
    members = {i.filename: i for i in bundle.infolist() if not i.is_dir()}
    extra = sorted(set(members) - set(listed) - {MANIFEST, CREDITS})
    if extra:
        raise BundleError(
            f"the bundle holds files its manifest does not list: {', '.join(extra[:3])}"
        )
    total = 0
    for name, entry in listed.items():
        problem = safe_member(name)
        if problem:
            raise BundleError(f"{name}: {problem}")
        if name not in members:
            raise BundleError(f"{name} is in the manifest but not in the bundle")
        if (
            not isinstance(entry, dict)
            or not isinstance(entry.get("size"), int)
            or not isinstance(entry.get("sha256"), str)
        ):
            raise BundleError(f"{name}: a bad entry in the manifest")
        if members[name].file_size != entry["size"]:
            raise BundleError(
                f"{name}: its size is {members[name].file_size}, the manifest says {entry['size']}"
            )
        total += entry["size"]
    if total > MAX_UNPACKED_BYTES:
        raise BundleError(
            f"the bundle unpacks to {total / 1e9:.1f} GB, more than the {MAX_UNPACKED_BYTES / 1e9:.0f} GB allowed"
        )


def _extract(bundle: zipfile.ZipFile, name: str, dest: Path, expected: str) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    with bundle.open(name) as source, dest.open("wb") as target:
        for chunk in iter(lambda: source.read(1 << 20), b""):
            digest.update(chunk)
            target.write(chunk)
    if digest.hexdigest() != expected:
        dest.unlink(missing_ok=True)
        raise BundleError(
            f"{name}: its contents do not match the hash in the manifest (a damaged or altered bundle)"
        )


def _licence_notes(bundle: zipfile.ZipFile, manifest: dict) -> list[str]:
    notes = []
    for name in sorted(manifest["files"]):
        if not name.endswith(".credits.json"):
            continue
        try:
            data = json.loads(bundle.read(name))
        except (KeyError, ValueError):
            continue
        for note in data.get("notes", []):
            notes.append(f"{PurePosixPath(name).stem.removesuffix('.credits')}: {note}")
    return notes


def import_bundle(
    bundle_path: Path, deploy_dir: Path, *, replace: bool = False, dry_run: bool = False
) -> ImportReport:
    """Puts the models of a bundle into `deploy_dir`. A file that is already there with the same contents is
    left; one that differs is a conflict and is left too, unless `replace`. Nothing is written until every
    file in the bundle has been checked against its hash; with `dry_run` nothing is written at all.
    """
    try:
        zipped = zipfile.ZipFile(bundle_path)
    except (OSError, zipfile.BadZipFile) as exc:
        raise BundleError(f"cannot open {bundle_path.name}: {exc}") from None
    with zipped as bundle:
        manifest = read_manifest(bundle)
        _verify(bundle, manifest)
        report = ImportReport(manifest=manifest)
        report.notes = _licence_notes(bundle, manifest)
        listed = manifest["files"]
        assets = deploy_dir / "assets"
        plan: list[tuple[str, Path]] = []
        carried = _look_in(bundle, listed)
        seed_look = carried is not None and not manifest_path(deploy_dir).is_file()
        if carried is not None:
            report.look = (
                f"set up as version 1 and the current look (it was version {carried.version})"
                if seed_look
                else "not used: this install already has looks (the bundle's is left alone)"
            )
        for name in sorted(n for n in listed if n not in LOOK_MEMBERS):
            dest = deploy_dir / PurePosixPath(name)
            clash = None
            if (
                dest.parent.is_dir()
            ):  # a name that differs only in case from one already there
                clash = next(
                    (
                        p
                        for p in dest.parent.iterdir()
                        if p.name.lower() == dest.name.lower() and p.name != dest.name
                    ),
                    None,
                )
            if clash is not None:
                report.conflicts.append(f"{name} (there is already {clash.name})")
            elif dest.is_file():
                if sha256_of(dest) == listed[name]["sha256"]:
                    report.unchanged.append(name)
                elif replace:
                    report.replaced.append(name)
                    plan.append((name, dest))
                else:
                    report.conflicts.append(name)
            else:
                report.added.append(name)
                plan.append((name, dest))

        bodies = {
            PurePosixPath(n).name.removesuffix(".rig.json")
            for n in listed
            if n.endswith(".rig.json")
        }
        have_derived = {
            PurePosixPath(n).parts[2]
            for n in listed
            if n.startswith("assets/animation_library_body/")
        }
        report.derived_missing = sorted(b for b in bodies if b not in have_derived)

        if dry_run:  # the contents are still read, so a damaged bundle is found
            for name in sorted(listed):
                digest = hashlib.sha256()
                with bundle.open(name) as source:
                    for chunk in iter(lambda: source.read(1 << 20), b""):
                        digest.update(chunk)
                if digest.hexdigest() != listed[name]["sha256"]:
                    raise BundleError(
                        f"{name}: its contents do not match the hash in the manifest (a damaged or altered bundle)"
                    )
            return report

        assets.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=".import-", dir=assets))
        try:
            staged = []
            for name, dest in plan:
                part = staging / PurePosixPath(name)
                _extract(bundle, name, part, listed[name]["sha256"])
                staged.append((part, dest))
            for part, dest in staged:
                dest.parent.mkdir(parents=True, exist_ok=True)
                os.replace(part, dest)
        finally:
            shutil.rmtree(staging, ignore_errors=True)
        if (
            seed_look and carried is not None
        ):  # after the models its spec names are in place
            _seed_look(bundle, carried, deploy_dir)
    return report
