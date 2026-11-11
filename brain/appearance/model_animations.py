"""
Animation libraries and rig profiles for installed bodies: adding, retargeting
and converting their clips.
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path
from blender import pack_profiles
from blender.animation_library import (
    REQUIRED_BONES,
    is_retargeted,
    late_starts,
    library_contents,
    library_folder,
    mixamo_derived_clips,
)

from brain.appearance import models as core


@core._with_profiles
def retarget_library_onto(
    deploy_dir: Path, stem: str, library: Path
) -> list[str]:
    """The clips of `library` (a Quaternius one) that a role uses, retargeted onto the installed
    converted body `custom:<stem>` and written as that body's own library. The clip names.
    """
    model = core._model_file(deploy_dir, f"custom:{stem}")
    if model is None:
        raise SystemExit(f"custom:{stem} is not installed")
    joints, clips = library_contents(library)
    if not REQUIRED_BONES <= joints:
        return []  # not made for the Quaternius skeleton
    wanted = core.wanted_clips(clips)
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
        proc = core._generator_run(
            ["clips", str(spec_path)], core.RIG_TIMEOUT_S, "rig_convert.py"
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
        p.stem
        for p in folder.glob("*.glb")
        if folder.is_dir() and is_retargeted(p)
    )


@core._with_profiles
def retarget_all_libraries(deploy_dir: Path, stem: str) -> None:
    folder = core.animation_library_dir(deploy_dir)
    for library in sorted(folder.glob("*.glb")) if folder.is_dir() else []:
        clips = retarget_library_onto(deploy_dir, stem, library)
        if clips:
            print(
                f"    retargeted {library.stem}: {len(clips)} clips onto {stem}"
            )


def _library_with_clip(deploy_dir: Path, clip: str) -> Path | None:
    folder = core.animation_library_dir(deploy_dir)
    for library in sorted(folder.glob("*.glb")) if folder.is_dir() else []:
        if any(
            a.get("name") == clip
            for a in core._glb_json(library).get("animations", [])
        ):
            return library
    return None


@core._with_profiles
def convert_mixamo_clips(
    deploy_dir: Path,
    sources: list[Path],
    name: str,
    options: dict | None = None,
) -> tuple[Path, list[str]]:
    """Retargets Mixamo clips (.fbx) onto the Quaternius skeleton and installs them as one animation
    library. Each file becomes a clip named for it (`Mixamo_Standing_Greeting.fbx` ->
    `Standing_Greeting`), processed as `mixamo_clip_defaults` says for such a clip unless
    `options` (clip name -> overrides) says otherwise. Seated clips are lined up with the seat of
    an installed library's `Sitting_Idle_Loop`."""
    from blender.animation_library import (
        clip_name_from_file,
        mixamo_clip_defaults,
        read_glb_json,
    )

    host = next(
        (
            deploy_dir / "assets" / "base_templates" / f"{n}.glb"
            for n in core.STANDARD_HUMANOIDS
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
        if (
            source.suffix == ".glb"
        ):  # one or more clips on the Quaternius skeleton
            names = [
                a.get("name", "")
                for a in read_glb_json(source).get("animations", [])
            ]
            if not names:
                raise SystemExit(f"{source.name} has no animations in it")
            entries = [({"glb": str(source), "clip": n}, n) for n in names]
        else:
            clip = clip_name_from_file(source.stem)
            entries = [({"fbx": str(source)}, clip)]
        for origin, clip in entries:
            clips.append(
                {
                    **origin,
                    "name": clip,
                    "options": {
                        **mixamo_clip_defaults(clip),
                        **(options or {}).get(clip, {}),
                    },
                }
            )
    clips += mixamo_derived_clips({c["name"] for c in clips})
    dest = core.animation_library_dir(deploy_dir) / f"{name}.glb"
    dest.parent.mkdir(parents=True, exist_ok=True)
    spec = {
        "host": str(host),
        "out": str(dest),
        "clips": clips,
        "stand_reference": (
            {"library": str(standing), "clip": "Idle_Loop"}
            if standing
            else None
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
        proc = core._generator_run(
            ["clips", str(spec_path)],
            core.MIXAMO_TIMEOUT_S,
            "mixamo_convert.py",
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
    late = late_starts(core._glb_json(dest))
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
    (the roles in the animation profiles, `blender/pack_profiles.py`). Returns where it went and the clip names.
    """
    if source.suffix != ".glb":
        raise SystemExit("an animation library must be a .glb file")
    if not source.is_file():
        raise SystemExit(f"{source} does not exist")
    if not name.replace("_", "").isalnum():
        raise SystemExit("name may only use letters, digits and underscores")
    gltf = core._glb_json(source)
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
    dest = core.animation_library_dir(deploy_dir) / f"{name}.glb"
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, dest)
    return dest, clips


def profiles_in_use(deploy_dir: Path) -> dict:
    """The animation profiles in use, for `profiles` and the status page: {"profiles": [{name,
    origin, user, roles, extras, rules, derived}], "problems": [...]}. The user's are in
    `<deploy_dir>/assets/animation_profiles/`."""
    pack_profiles.configure(deploy_dir)
    merged = pack_profiles.merged()
    user_dir = str(deploy_dir.joinpath(*pack_profiles.USER_PROFILES_DIR))
    return {
        "profiles": [
            {
                "name": p.name,
                "origin": p.origin,
                "user": p.origin.startswith(user_dir),
                "roles": len(p.data.get("roles", {}))
                + len(p.data.get("extras", {})),
                "rules": len(p.data.get("clips", [])),
                "derived": len(p.data.get("derived", [])),
            }
            for p in merged.profiles
        ],
        "problems": list(merged.problems),
    }


def add_profile(deploy_dir: Path, source: Path, replace: bool = False) -> Path:
    """Validates an animation profile and installs it for this install (it is layered over the
    shipped ones). Refuses one that does not validate, and, without `replace`, one that is there.
    """
    try:
        profile = pack_profiles.load_file(source)
    except pack_profiles.ProfileError as exc:
        raise SystemExit(str(exc)) from None
    dest = (
        deploy_dir.joinpath(*pack_profiles.USER_PROFILES_DIR)
        / f"{profile.name}.json"
    )
    if dest.exists() and not replace:
        raise SystemExit(
            f"{dest.name} is already installed (--replace to overwrite it)"
        )
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(profile.data, indent=2) + "\n")
    return dest
