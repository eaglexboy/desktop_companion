"""The command line of brain.appearance.models (`python -m brain.appearance.models`)."""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path
from blender import pack_profiles, rigs, sketchfab
from blender.animation_library import (
    is_retargeted,
    library_contents,
    rig_sidecar,
)
from brain.appearance import bundle
from brain.appearance.spec import (
    hold_reason,
    load_templates_catalog,
)

from brain.appearance import models as core


def _print_matrix(deploy_dir: Path) -> None:
    templates = load_templates_catalog(deploy_dir / "assets" / "catalogs")[
        "templates"
    ]
    custom_dir = deploy_dir / "assets" / "custom_templates"
    customs = sorted(
        {
            p.stem
            for suffix in (".glb", ".blend")
            for p in custom_dir.glob(f"*{suffix}")
        }
    )
    bodies = sorted(templates) + [f"custom:{stem}" for stem in customs]
    for body in bodies:
        profile = core.body_profile(deploy_dir, body, templates)
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
            else ", ".join(s for s in core.SOCKETS if s in profile.sockets)
            or "none"
        )
        print(f"{body}  [{kind}]\n    sockets: {sockets}")
        credit = core.credit_line(
            core.read_credits(core._model_file(deploy_dir, body))
        )
        if credit:
            print(f"    credit: {credit}")
        if is_retargeted(core._model_file(deploy_dir, body)):
            print(
                "    rig: converted to the Quaternius skeleton; the libraries are retargeted onto it"
            )
        support = core.animation_support(deploy_dir, body)
        if support:
            print(
                f"    animations: {core._describe_animations(support, core.animation_gaps(deploy_dir, body))}"
            )
        elif support is None:
            print(
                "    animations: unknown (the rig was never inspected)"
                if profile.imported
                else "    animations: none (a built-in body has its own)"
            )
        catalog = core.effective_wardrobe_catalog(deploy_dir, body)
        if profile.imported and len(catalog["outfits"]) > 1:
            print(f"    outfits: {', '.join(catalog['outfits'][1:])}")
        for socket, items in catalog["accessories"].items():
            print(f"    {socket}: {', '.join(items)}")
    libraries = (
        sorted(core.animation_library_dir(deploy_dir).glob("*.glb"))
        if core.animation_library_dir(deploy_dir).is_dir()
        else []
    )
    print(f"\nanimation libraries: {len(libraries)}")
    for lib in libraries:
        print(
            f"    {lib.stem}  {len(core._glb_json(lib).get('animations', []))} clips"
        )
    models = core.discover_accessory_models(deploy_dir)
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
        stems = core.converted_bodies(deploy_dir)
        if args.regenerate:
            for stem in stems:
                core.retarget_all_libraries(deploy_dir, stem)
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
    body = sub.add_parser(
        "add-body", help="install a body model and inspect its rig"
    )
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
    body.add_argument(
        "--replace",
        action="store_true",
        help="replace a body of this name that is installed: it is removed, with its sidecars and retargeted libraries, once the new one is ready (the default refuses)",
    )
    rig_parser = sub.add_parser(
        "rig-body",
        help="rig a .glb made of separate rigid parts (no skeleton) with the Quaternius skeleton and install it",
    )
    rig_parser.add_argument("file", type=Path)
    rig_parser.add_argument("--name", required=True, help="custom:<name>")
    rig_parser.add_argument(
        "--yaw",
        type=float,
        help="degrees to turn the figure about the vertical so it faces the front (default: face_yaw in <file>.pose.json, else 0)",
    )
    rig_parser.add_argument(
        "--arm-offset",
        type=float,
        default=core.DEFAULT_ARM_OFFSET,
        help="how far from the centre line, as a fraction of the height, a part is an arm (0.14; 0.09 for arms that hang close to the body)",
    )
    rig_parser.add_argument(
        "--limbless",
        action="store_true",
        help="for a figure with no arms or legs (a robot on wheels): head, neck and body, with the limb bones made empty so the libraries' head and body motion plays",
    )
    rig_parser.add_argument(
        "--split-loose",
        action="store_true",
        help="first cut each mesh into its loose parts, for a model whose parts were exported as one mesh",
    )
    rig_parser.add_argument(
        "--replace",
        action="store_true",
        help="replace a body of this name that is installed",
    )
    rig_parser.add_argument(
        "--max-texture",
        type=int,
        default=core.DEFAULT_MAX_TEXTURE,
        help="shrink textures to this many px",
    )
    remove_parser = sub.add_parser(
        "remove-body",
        help="delete an installed body with its sidecars and retargeted libraries",
    )
    remove_parser.add_argument(
        "body",
        help="standard_humanoid_female, standard_humanoid_male or custom:<name>",
    )
    remove_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="list what would be deleted and delete nothing",
    )
    remove_parser.add_argument(
        "--force",
        action="store_true",
        help="remove the body of the look the companion wears now as well",
    )
    meshes_parser = sub.add_parser(
        "meshes",
        help="list the skinned meshes of an installed body and the bones that move them",
    )
    meshes_parser.add_argument(
        "body", help="custom:<name> or a standard_humanoid_* id"
    )
    rebind_parser = sub.add_parser(
        "rebind-mesh",
        help="bind one mesh of an installed body wholly to one bone (bones that share its weight stop dragging it) and/or rename it",
    )
    rebind_parser.add_argument(
        "body", help="custom:<name> or a standard_humanoid_* id"
    )
    rebind_parser.add_argument(
        "--mesh", required=True, help="the mesh, as `meshes` names it"
    )
    rebind_parser.add_argument(
        "--bone",
        help="the bone it belongs to (a Quaternius name on a converted body)",
    )
    rebind_parser.add_argument(
        "--bone-left",
        help="for a mesh that holds a pair (both shoes): the bone for its left half (+X)",
    )
    rebind_parser.add_argument(
        "--bone-right", help="the bone for its right half"
    )
    rebind_parser.add_argument(
        "--ramp",
        nargs=4,
        metavar=("UPPER_LEFT", "UPPER_RIGHT", "Z0", "Z1"),
        help="with --bone-left/--bone-right: from height Z0 to Z1 (metres) the vertices pass to these bones above (a shoe: sole on the foot, opening on the calf)",
    )
    rebind_parser.add_argument(
        "--offset",
        type=float,
        nargs=3,
        metavar=("X", "Y", "Z"),
        help="move the mesh, in metres in the model's own axes (it faces -Y, left is +X, up is Z): +Y is back",
    )
    rebind_parser.add_argument(
        "--rename",
        help="a name that says what the mesh is (a download's are often `Object_72`)",
    )
    inspect = sub.add_parser(
        "inspect-body",
        help="read an installed body's rig into its .caps.json (no file is copied)",
    )
    inspect.add_argument(
        "body",
        help="standard_humanoid_female, standard_humanoid_male or custom:<name>",
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
        "body",
        help="standard_humanoid_female, standard_humanoid_male or custom:<name>",
    )
    face.add_argument(
        "--force", action="store_true", help="replace an existing sidecar"
    )
    outfit = sub.add_parser(
        "add-outfit",
        help="install an outfit model (skinned to a body's skeleton)",
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
        default=core.OUTFIT_MAX_TRIANGLES,
        help="decimate the outfit to about this many triangles (0 = keep as is)",
    )
    sub.add_parser(
        "profiles",
        help="show the animation profiles in use (which clips fill which role, how they are converted)",
    )
    check = sub.add_parser(
        "check-profile",
        help="check an animation profile file and say what it adds",
    )
    check.add_argument("file", type=Path)
    prof = sub.add_parser(
        "add-profile",
        help="install an animation profile for this install (layered over the shipped ones)",
    )
    prof.add_argument("file", type=Path)
    prof.add_argument("--replace", action="store_true")
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
        "--retarget",
        action="store_true",
        help="the .glb files are single clips on the Quaternius skeleton (Mesh2Motion...), not a library: they are converted with the .fbx files, if any",
    )
    anim.add_argument(
        "--credits",
        type=Path,
        help="a JSON file ({title, author, source, license}) kept as <name>.credits.json beside the library",
    )
    anim.add_argument(
        "--options",
        type=Path,
        help="JSON {clip name: {loop, seat, in_place, variants, trim: [first, last], extract: [...]}} overriding what a Mixamo clip is given by default",
    )
    acc = sub.add_parser(
        "add-accessory", help="install an accessory model for a socket"
    )
    acc.add_argument("file", type=Path)
    acc.add_argument("--socket", required=True, choices=core.SOCKETS)
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
        choices=core.CAPABILITIES,
        help="override what the body must have",
    )
    for command in (body, outfit, acc):
        command.add_argument(
            "--max-texture",
            type=int,
            default=core.DEFAULT_MAX_TEXTURE,
            help="shrink a .gltf's textures to this many px",
        )
    args = parser.parse_args(argv)

    deploy_dir = get_settings().deploy_dir
    pack_profiles.configure(deploy_dir)
    if args.command == "list":
        _print_matrix(deploy_dir)
        return 0
    if args.command == "profiles":
        info = core.profiles_in_use(deploy_dir)
        for entry in info["profiles"]:
            where = "yours" if entry["user"] else "shipped"
            print(
                f"{entry['name']}  [{where}]  {entry['roles']} roles, {entry['rules']} conversion rules, {entry['derived']} derived clips"
            )
            print(f"    {entry['origin']}")
        for problem in info["problems"]:
            print(f"skipped: {problem}", file=sys.stderr)
        return 1 if info["problems"] else 0
    if args.command == "check-profile":
        try:
            profile = pack_profiles.load_file(args.file)
        except pack_profiles.ProfileError as exc:
            print(f"not valid: {exc}", file=sys.stderr)
            return 1
        d = profile.data
        print(
            f"{profile.name}: valid. roles {sorted(d.get('roles', {}))}, extras {sorted(d.get('extras', {}))}, "
            f"{len(d.get('clips', []))} conversion rules, {len(d.get('derived', []))} derived clips, "
            f"{len(d.get('sit_sets', []))} sit sets, disables {d.get('disable', [])}"
        )
        return 0
    if args.command == "add-profile":
        dest = core.add_profile(deploy_dir, args.file, args.replace)
        print(f"installed {dest}")
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
        core.set_hold(
            deploy_dir,
            args.body,
            args.reason if args.command == "hold" else None,
        )
        print(
            f"{args.body}: {'held (' + args.reason + ')' if args.command == 'hold' else 'released'}"
        )
        return 0
    if args.command == "rig-body":
        report = core.rig_body(
            deploy_dir,
            args.file,
            args.name,
            yaw=args.yaw,
            arm_offset=args.arm_offset,
            replace=args.replace,
            limbless=args.limbless,
            split_loose=args.split_loose,
            max_texture=args.max_texture,
        )
        caps = (
            ", ".join(k for k, v in report["capabilities"].items() if v)
            or "none"
        )
        print(
            f"installed {args.name} (rigged with the Quaternius skeleton)\n    capabilities: {caps}"
        )
        return 0
    if args.command == "meshes":
        for line in core.body_meshes(deploy_dir, args.body):
            print(line)
        return 0
    if args.command == "rebind-mesh":
        print(
            core.rebind_mesh(
                deploy_dir,
                args.body,
                args.mesh,
                args.bone,
                args.rename,
                tuple(args.offset) if args.offset else None,
                (
                    (args.bone_left, args.bone_right)
                    if args.bone_left and args.bone_right
                    else None
                ),
                (
                    (
                        args.ramp[0],
                        args.ramp[1],
                        float(args.ramp[2]),
                        float(args.ramp[3]),
                    )
                    if args.ramp
                    else None
                ),
            )
        )
        return 0
    if args.command == "remove-body":
        gone = core.remove_body(
            deploy_dir, args.body, force=args.force, dry_run=args.dry_run
        )
        for path in gone:
            print(f"{'would delete' if args.dry_run else 'deleted'} {path}")
        return 0
    if args.command == "inspect-body":
        report = core.inspect_body(deploy_dir, args.body)
        caps = (
            ", ".join(k for k, v in report["capabilities"].items() if v)
            or "none"
        )
        print(
            f"{args.body}\n    capabilities: {caps}\n    sockets: {', '.join(report['supported_sockets']) or 'none'}"
        )
        for warning in report.get("warnings", []):
            print(f"    warning: {warning}")
        return 0
    if args.command == "face-sidecar":
        model = core._model_file(deploy_dir, args.body)
        if model is None:
            raise SystemExit(f"{args.body} has no installed model")
        print(
            f"{args.body}: {core.generate_face_sidecar(model, force=args.force)}"
        )
        return 0
    if args.command == "add-animations":
        if args.retarget or args.file[0].suffix == ".fbx":
            options = (
                json.loads(args.options.read_text()) if args.options else None
            )
            dest, clips = core.convert_mixamo_clips(
                deploy_dir, args.file, args.name, options
            )
        else:
            if len(args.file) != 1:
                raise SystemExit(
                    "give one .glb library, or any number of .fbx clips"
                )
            dest, clips = core.add_animation_library(
                deploy_dir, args.file[0], args.name
            )
        if args.credits:
            core.credits_path(dest).write_text(
                json.dumps(json.loads(args.credits.read_text()), indent=2)
                + "\n"
            )
        print(
            f"installed {dest.name}: {len(clips)} clips ({', '.join(clips[:8])}{'...' if len(clips) > 8 else ''})"
        )
        for stem in core.converted_bodies(
            deploy_dir
        ):  # a body converted from another rig gets it retargeted
            done = core.retarget_library_onto(deploy_dir, stem, dest)
            if done:
                print(
                    f"    retargeted {dest.stem}: {len(done)} clips onto {stem}"
                )
        return 0
    if (
        args.command == "add-body"
        and not args.replace
        and core._model_file(deploy_dir, args.name) is not None
    ):
        raise SystemExit(
            f"{args.name} is already installed (--replace removes it and installs this one)"
        )
    with tempfile.TemporaryDirectory(prefix="deskmate-convert-") as scratch:
        from_fbx = (
            args.command == "add-body" and args.file.suffix == ".fbx"
        )  # its textures are not yet shrunk
        sketch = None
        if (
            args.command in ("add-body", "add-outfit", "add-accessory")
            and args.file.suffix == ".glb"
        ):
            gltf = core._glb_json(args.file)
            if sketchfab.is_sketchfab(gltf):
                sketch = sketchfab.credits(gltf)
                print(
                    f"{args.file.name}: a Sketchfab model, {core.credit_line(sketch)}"
                )
                for note in sketch["notes"]:
                    print(f"    note: {note}")
                if args.command == "add-body":
                    args.file = core.convert_sketchfab_body(
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
                args.file, summary = core.convert_rig_body(
                    args.file, Path(scratch) / "rig"
                )
                rig = {"family": family, "retargeted": True}
                print(
                    f"    {family} rig converted to the Quaternius skeleton: {summary}"
                )
        if from_fbx:
            print(
                f"{args.file.name}: converting a Mixamo character to the Quaternius skeleton"
            )
            args.file = core.convert_mixamo_body(
                args.file, Path(scratch) / "mixamo", deploy_dir
            )
        if (
            args.command == "add-outfit"
        ):  # always re-encoded: the budgets apply to a .glb outfit too
            args.file = core.convert_to_glb(
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
                and (
                    core.glb_triangles(args.file) > core.BODY_MAX_TRIANGLES
                    or from_fbx
                )
            )
            if heavy:
                print(
                    f"{args.file.name} has {core.glb_triangles(args.file):,} triangles; decimating to about {core.BODY_MAX_TRIANGLES:,}"
                )
            args.file = core.convert_to_glb(
                args.file,
                Path(scratch),
                args.max_texture,
                core.BODY_MAX_TRIANGLES if args.command == "add-body" else 0,
                always=heavy,
            )
        if args.command == "add-body":
            if (
                args.replace
                and core._model_file(deploy_dir, args.name) is not None
            ):
                for path in core.remove_body(deploy_dir, args.name, force=True):
                    print(f"replaced: deleted {path}")
            report = core.add_body(deploy_dir, args.file, args.name)
            if sketch is not None:
                installed = core._model_file(deploy_dir, args.name)
                if installed is not None:
                    core.credits_path(installed).write_text(
                        json.dumps(sketch, indent=2) + "\n"
                    )
            if rig is not None:
                installed = core._model_file(deploy_dir, args.name)
                if installed is not None:
                    rig_sidecar(installed).write_text(
                        json.dumps(rig, indent=2) + "\n"
                    )
                    core.retarget_all_libraries(deploy_dir, installed.stem)
            caps = (
                ", ".join(k for k, v in report["capabilities"].items() if v)
                or "none"
            )
            print(
                f"installed {args.name}\n    capabilities: {caps}\n    sockets: {', '.join(report['supported_sockets']) or 'none'}"
            )
            for warning in report.get("warnings", []):
                print(f"    warning: {warning}")
            if not report.get(
                "viseme_shape_keys"
            ):  # no lip-sync of its own: try to give it a face overlay
                installed = core._model_file(deploy_dir, args.name)
                if installed is not None:
                    print(
                        f"    face sidecar: {core.generate_face_sidecar(installed)}"
                    )
        elif args.command == "add-outfit":
            dest = core.add_outfit(deploy_dir, args.file, args.body, args.name)
            if sketch is not None:
                core.credits_path(dest).write_text(
                    json.dumps(sketch, indent=2) + "\n"
                )
            print(f"installed {dest}")
        else:
            dest = core.add_accessory(
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
                core.credits_path(dest).write_text(
                    json.dumps(sketch, indent=2) + "\n"
                )
            print(f"installed {dest}")
            for warning in core.accessory_warnings(args.file):
                print(f"    warning: {warning}")
    return 0
