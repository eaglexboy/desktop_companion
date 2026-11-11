"""
What each installed body can do: its animation libraries, the clips each one
lacks, and the gaps that leave a body still.
"""

from __future__ import annotations

from pathlib import Path
from blender.animation_library import (
    chair_sit_clips,
    library_contents,
    library_folder,
    library_gaps,
    library_support,
)
from brain.appearance.spec import (
    hold_reason,
    load_templates_catalog,
)

from brain.appearance import models as core


@core._with_profiles
def animation_support(
    deploy_dir: Path, base_template: str
) -> dict[str, dict[str, str]] | None:
    """What each installed animation library gives this body: {library: {role: clip}}, with an
    empty dict for a library whose bones the body's rig lacks. None when nothing is known
    (a procedural body, which has no rig of the libraries', or a model never inspected).
    """
    model = core._model_file(deploy_dir, base_template)
    folder = library_folder(
        deploy_dir, model
    )  # a converted body plays libraries retargeted onto it
    libraries = sorted(folder.glob("*.glb")) if folder.is_dir() else []
    if not libraries:
        return {}
    caps = core._read_json(core.caps_path(model)) if model is not None else None
    if not caps:
        return None
    if not caps.get("bone_names"):
        return {
            "": {}
        }  # a model with no rig of its own is given the built-in one, which no library fits
    bones = set(caps["bone_names"])
    return {lib.stem: library_support(bones, lib) for lib in libraries}


@core._with_profiles
def animation_gaps(
    deploy_dir: Path, base_template: str
) -> dict[str, list[str]]:
    """{library: the bones of its rig this body lacks} for the libraries it can wear: those clips
    play without the tracks of those bones (a body with no fingers keeps still hands). A fully
    fitting library is not listed."""
    model = core._model_file(deploy_dir, base_template)
    folder = library_folder(deploy_dir, model)
    caps = core._read_json(core.caps_path(model)) if model is not None else None
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
            lambda b: b.startswith(
                ("thumb", "index", "middle", "ring", "pinky")
            ),
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
                if roles["sit_idle"] in chair_sit_clips()
                else "sit (on the ground)"
            )
        note = (
            f" (no {_describe_gaps(gaps[library])}: those stay still)"
            if gaps and library in gaps
            else ""
        )
        parts.append(f"{library}: {', '.join(names)}{note}")
    return "; ".join(parts)


@core._with_profiles
def animation_matrix(deploy_dir: Path) -> dict:
    """What animations each installed body gets, for the status page (read-only):
    {"libraries": [{name, clips, joints}], "bodies": [{body, installed, held, libraries:
    {library: {fits, roles, sit, gaps}} | None}]}. `libraries` is None for a body whose rig was
    never inspected, `{}` for one that has no rig of its own."""
    folder = core.animation_library_dir(deploy_dir)
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
    bodies = []
    for body in sorted(templates) + [f"custom:{stem}" for stem in customs]:
        profile = core.body_profile(deploy_dir, body, templates)
        held = (
            hold_reason(deploy_dir, body[len("custom:") :])
            if body.startswith("custom:")
            else None
        )
        support = (
            animation_support(deploy_dir, body) if profile.imported else {}
        )
        gaps = animation_gaps(deploy_dir, body) if support else {}
        fits = None
        if support is not None and "" in support:
            fits = (
                {}
            )  # inspected, and it has no rig of its own: no library fits (not "never inspected")
        elif support is not None:
            fits = {
                library: {
                    "fits": bool(roles),
                    "roles": [
                        r
                        for r in roles
                        if not r.startswith("sit_") and "." not in r
                    ],
                    "sit": (
                        (
                            "chair"
                            if roles["sit_idle"] in chair_sit_clips()
                            else "ground"
                        )
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
                "credit": core.credit_line(
                    core.read_credits(core._model_file(deploy_dir, body))
                ),
                "libraries": fits,
            }
        )
    return {"libraries": libraries, "bodies": bodies}
