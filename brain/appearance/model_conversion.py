"""
Converting a downloaded rig into an installed body: Mixamo, Sketchfab, mapped
rigs and part-rigged models.
"""

from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path
from blender.animation_library import (
    rig_sidecar,
)
from brain.appearance.model_animations import retarget_all_libraries

from brain.appearance import models as core


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
            for n in core.STANDARD_HUMANOIDS
            if deploy_dir is not None
            and (
                deploy_dir / "assets" / "base_templates" / f"{n}.glb"
            ).is_file()
        ),
        None,
    )
    proc = core._generator_run(
        ["body", str(source), str(dest), *([str(host)] if host else [])],
        core.MIXAMO_TIMEOUT_S,
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


def convert_sketchfab_body(
    source: Path, out_dir: Path, max_texture: int = core.DEFAULT_MAX_TEXTURE
) -> Path:
    """A Sketchfab download (.glb) cleaned up as a body (blender/sketchfab_convert.py, ADR 17): the
    wrapper transforms baked in, the helper bones gone, the bones named as the author named them, no
    shape keys, textures shrunk and the meshes decimated to the body budget. Made again with
    smaller textures while the file is over `SKETCHFAB_MAX_BYTES`."""
    out_dir.mkdir(parents=True, exist_ok=True)
    dest = out_dir / f"{source.stem}.glb"
    texture = max_texture
    while True:
        proc = core._generator_run(
            [
                str(source),
                str(dest),
                str(texture),
                str(core.BODY_MAX_TRIANGLES),
            ],
            core.SKETCHFAB_TIMEOUT_S,
            "sketchfab_convert.py",
        )
        if proc.returncode != 0 or not dest.is_file():
            text = (proc.stderr + proc.stdout).strip().splitlines()
            reason = next(
                (
                    l
                    for l in reversed(text)
                    if "SystemExit" in l or "Error" in l
                ),
                "conversion failed",
            )
            raise SystemExit(
                f"cleaning {source.name} up failed: {reason.split(':', 1)[-1].strip()}"
            )
        if dest.stat().st_size <= core.SKETCHFAB_MAX_BYTES or texture <= 256:
            return dest
        texture //= 2
        print(
            f"    {dest.stat().st_size / 1e6:.1f} MB: making it again with {texture} px textures"
        )


def convert_rig_body(source: Path, out_dir: Path) -> tuple[Path, str]:
    """A body of a known rig (Character Creator, Auto-Rig Pro) renamed onto the Quaternius skeleton
    (blender/rig_convert.py, ADR 18): (the file, what was done)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    dest = out_dir / f"{source.stem}_quaternius.glb"
    proc = core._generator_run(
        ["body", str(source), str(dest)], core.RIG_TIMEOUT_S, "rig_convert.py"
    )
    marker = next(
        (l for l in proc.stdout.splitlines() if l.startswith("RIG_BODY_OK")),
        None,
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


def rig_parts_body(
    source: Path,
    out_dir: Path,
    yaw: float,
    arm_offset: float,
    limbless: bool = False,
    split_loose: bool = False,
) -> Path:
    """A body made of separate rigid parts, given a Quaternius skeleton with each part bound to one
    bone (blender/part_rig.py, ADR 23): the new file."""
    out_dir.mkdir(parents=True, exist_ok=True)
    dest = out_dir / f"{source.stem}_rigged.glb"
    proc = core._generator_run(
        [
            str(source),
            str(dest),
            "--yaw",
            str(yaw),
            "--arm-dx",
            str(arm_offset),
            *(["--limbless"] if limbless else []),
            *(["--split-loose"] if split_loose else []),
        ],
        core.RIG_TIMEOUT_S,
        "part_rig.py",
    )
    if (
        proc.returncode != 0
        or "PART_RIG_OK" not in proc.stdout
        or not dest.is_file()
    ):
        text = (proc.stderr + proc.stdout).strip().splitlines()
        reason = next(
            (l for l in reversed(text) if "SystemExit" in l or "Error" in l),
            "rigging failed",
        )
        raise SystemExit(
            f"rigging {source.name} failed: {reason.split(':', 1)[-1].strip()}"
        )
    return dest


def rig_body(
    deploy_dir: Path,
    source: Path,
    name: str,
    *,
    yaw: float | None = None,
    arm_offset: float = core.DEFAULT_ARM_OFFSET,
    replace: bool = False,
    limbless: bool = False,
    split_loose: bool = False,
    max_texture: int = core.DEFAULT_MAX_TEXTURE,
) -> dict:
    """Installs `source`, a .glb made of separate rigid parts with no skeleton, as the `custom:` body
    `name`, rigged with the Quaternius skeleton and with the animation libraries retargeted onto it.
    `yaw` turns the figure to face -Y (default: the `face_yaw` of a `<source>.pose.json` beside it).
    """
    if not name.startswith("custom:"):
        raise SystemExit("rig-body installs a custom: body (custom:<name>)")
    if source.suffix != ".glb" or not source.is_file():
        raise SystemExit("rig-body takes a .glb file")
    if core._model_file(deploy_dir, name) is not None and not replace:
        raise SystemExit(
            f"{name} is already installed (--replace removes it and installs this one)"
        )
    if yaw is None:
        try:
            yaw = float(
                json.loads(
                    source.with_name(f"{source.stem}.pose.json").read_text()
                ).get("face_yaw", 0.0)
            )
        except (OSError, ValueError, AttributeError, TypeError):
            yaw = 0.0
    with tempfile.TemporaryDirectory(prefix="deskmate-rig-") as scratch:
        rigged = rig_parts_body(
            source,
            Path(scratch) / "rig",
            yaw,
            arm_offset,
            limbless,
            split_loose,
        )
        heavy = core.glb_triangles(rigged) > core.BODY_MAX_TRIANGLES
        ready = core.convert_to_glb(
            rigged,
            Path(scratch) / "glb",
            max_texture,
            core.BODY_MAX_TRIANGLES,
            always=heavy,
        )
        if replace and core._model_file(deploy_dir, name) is not None:
            core.remove_body(deploy_dir, name, force=True)
        report = core.add_body(deploy_dir, ready, name)
    installed = core._model_file(deploy_dir, name)
    if installed is not None:
        rig_sidecar(installed).write_text(
            json.dumps({"family": "part_rig", "retargeted": True}, indent=2)
            + "\n"
        )
        retarget_all_libraries(deploy_dir, installed.stem)
    return report


def body_meshes(deploy_dir: Path, name: str) -> list[str]:
    """The skinned meshes of an installed body, one line each: its name, its vertices and the bones with
    most of its weight (`blender/rebind.py --list`)."""
    model = core._model_file(deploy_dir, name)
    if model is None:
        raise SystemExit(f"{name} is not an installed body")
    proc = core._generator_run(
        ["--list", str(model)], core.RIG_TIMEOUT_S, "rebind.py"
    )
    if proc.returncode != 0 or "LIST_OK" not in proc.stdout:
        raise SystemExit(
            f"could not read {model.name}: {(proc.stderr + proc.stdout).strip().splitlines()[-1:]}"
        )
    return [
        line[len("MESH ") :]
        for line in proc.stdout.splitlines()
        if line.startswith("MESH ")
    ]


def rebind_mesh(
    deploy_dir: Path,
    name: str,
    mesh: str,
    bone: str | None = None,
    new_name: str | None = None,
    offset: tuple[float, float, float] | None = None,
    pair: tuple[str, str] | None = None,
    ramp: tuple[str, str, float, float] | None = None,
) -> str:
    """Binds one mesh of an installed body wholly to one bone and/or renames it, in place
    (`blender/rebind.py`). The libraries retargeted onto the body are clips, not weights, so they stay.
    """
    if bone is None and pair is None and new_name is None and offset is None:
        raise SystemExit(
            "give a bone to bind it to (or a left and a right one), a new name, an offset, or any of them"
        )
    model = core._model_file(deploy_dir, name)
    if model is None:
        raise SystemExit(f"{name} is not an installed body")
    with tempfile.TemporaryDirectory(prefix="deskmate-rebind-") as scratch:
        done = Path(scratch) / model.name
        extra = [
            *(["--bone", bone] if bone else []),
            *(
                ["--bone-left", pair[0], "--bone-right", pair[1]]
                if pair
                else []
            ),
            *(
                ["--ramp", ramp[0], ramp[1], str(ramp[2]), str(ramp[3])]
                if ramp
                else []
            ),
            *(["--rename", new_name] if new_name else []),
            *(["--offset", *(str(v) for v in offset)] if offset else []),
        ]
        proc = core._generator_run(
            [str(model), str(done), "--mesh", mesh, *extra],
            core.RIG_TIMEOUT_S,
            "rebind.py",
        )
        marker = next(
            (
                line
                for line in proc.stdout.splitlines()
                if line.startswith("REBIND_OK")
            ),
            None,
        )
        if proc.returncode != 0 or marker is None or not done.is_file():
            text = (proc.stderr + proc.stdout).strip().splitlines()
            reason = next(
                (
                    line
                    for line in reversed(text)
                    if "SystemExit" in line or "Error" in line
                ),
                "failed",
            )
            raise SystemExit(
                f"changing {mesh} failed: {reason.split(':', 1)[-1].strip()}"
            )
        shutil.copy2(done, model)
    return marker[len("REBIND_OK ") :]
