"""Which files the Build page's viewer may be given (brain/appearance/viewer_files.py)."""

from __future__ import annotations

import json
import struct
from pathlib import Path
from typing import Any

import pytest

from blender.animation_library import REQUIRED_BONES
from brain.appearance import models
from brain.appearance.viewer_files import (
    accessory_file,
    body_animations,
    body_file,
    library_file,
    outfit_file,
)

CORE = sorted(REQUIRED_BONES)


def _glb(doc: dict[str, Any]) -> bytes:
    body = json.dumps(doc).encode()
    body += b" " * (-len(body) % 4)
    return (
        b"glTF"
        + struct.pack("<II", 2, 20 + len(body))
        + struct.pack("<I", len(body))
        + b"JSON"
        + body
    )


def _library(
    deploy: Path, name: str, bones: list[str], clips: list[str]
) -> None:
    path = deploy / "assets" / "animation_library" / f"{name}.glb"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        _glb(
            {
                "asset": {"version": "2.0"},
                "nodes": [{"name": n} for n in bones],
                "skins": [{"joints": list(range(len(bones)))}],
                "animations": [{"name": c} for c in clips],
            }
        )
    )


def _body(deploy: Path, name: str, bones: list[str]) -> Path:
    path = deploy / "assets" / "custom_templates" / f"{name}.glb"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"glTF")
    models.caps_path(path).write_text(
        json.dumps(
            {
                "bone_names": bones,
                "supported_sockets": [],
                "capabilities": {"has_head": True},
            }
        )
    )
    return path


@pytest.fixture
def deploy(tmp_path: Path) -> Path:
    (tmp_path / "assets" / "catalogs").mkdir(parents=True)
    return tmp_path


def test_an_installed_body_has_a_file_and_a_procedural_one_does_not(
    deploy: Path,
) -> None:
    path = _body(deploy, "hero", CORE)
    assert body_file(deploy, "custom:hero") == path
    assert body_file(deploy, "primitive_blob") is None
    assert (
        body_file(deploy, "standard_humanoid_female") is None
    )  # not installed


def test_a_name_that_is_a_path_reaches_nothing(
    deploy: Path, tmp_path: Path
) -> None:
    secret = tmp_path.parent / "secret.glb"
    secret.write_bytes(b"glTF")
    _body(deploy, "hero", CORE)
    for name in (
        "custom:../../secret",
        "custom:../hero",
        "../secret",
        "custom:hero/../hero",
        "custom:",
        "",
    ):
        assert body_file(deploy, name) is None, name
        assert outfit_file(deploy, name, "x") is None
        assert library_file(deploy, name, "x") is None
    assert library_file(deploy, "custom:hero", "../../secret") is None
    assert outfit_file(deploy, "custom:hero", "../../../secret") is None


def test_a_held_body_is_not_served(deploy: Path) -> None:
    _body(deploy, "unchecked", CORE)
    models.set_hold(deploy, "custom:unchecked", "licence not checked")
    assert body_file(deploy, "custom:unchecked") is None
    assert body_animations(deploy, "custom:unchecked") == {
        "model": False,
        "libraries": [],
    }


def test_an_outfit_is_served_only_for_the_body_it_was_made_for(
    deploy: Path,
) -> None:
    _body(deploy, "hero", CORE)
    _body(deploy, "other", CORE)
    outfit = deploy / "assets" / "outfit_models" / "custom_hero" / "tunic.glb"
    outfit.parent.mkdir(parents=True)
    outfit.write_bytes(b"glTF")
    assert outfit_file(deploy, "custom:hero", "tunic") == outfit
    assert outfit_file(deploy, "custom:other", "tunic") is None
    assert outfit_file(deploy, "custom:hero", "missing") is None


def test_only_a_library_that_fits_the_rig_is_served(deploy: Path) -> None:
    _body(deploy, "hero", CORE)
    _library(deploy, "pack", CORE, ["Idle_Loop", "Walk_Loop"])
    _library(deploy, "foreign", ["a", "b"], ["Dance"])
    served = library_file(deploy, "custom:hero", "pack")
    assert served is not None and served.name == "pack.glb"
    assert library_file(deploy, "custom:hero", "foreign") is None
    assert library_file(deploy, "custom:hero", "nothing") is None


def test_the_clip_list_says_which_role_each_clip_fills(deploy: Path) -> None:
    _body(deploy, "hero", CORE)
    _library(
        deploy,
        "pack",
        CORE + ["finger_01_l"],
        ["Idle_Loop", "Walk_Loop", "Spinning"],
    )
    result = body_animations(deploy, "custom:hero")
    assert result["model"] is True
    (library,) = result["libraries"]
    assert library["name"] == "pack"
    assert library["gaps"] == ["finger_01_l"]
    clips = {c["name"]: c["roles"] for c in library["clips"]}
    assert clips["Idle_Loop"] == ["idle"]
    assert clips["Spinning"] == []
    speeds = {c["name"]: c["speed"] for c in library["clips"]}
    assert clips["Walk_Loop"] == ["walk"]
    assert speeds["Spinning"] == 1.0 and speeds["Walk_Loop"] == 1.0
    assert 0 < speeds["Idle_Loop"] < 1.0  # an idle is played slower than made


def test_a_building_block_of_a_role_clip_is_not_called_unused(
    deploy: Path,
) -> None:
    """A workout is made of other clips; those are part of it, not unused."""
    _body(deploy, "hero", CORE)
    _library(
        deploy,
        "floor",
        CORE,
        [
            "Lie_Down",
            "Bicycle_Crunch",
            "Stand_Up",
            "Workout_Crunch",
            "Standard_Run",
        ],
    )
    clips = {
        c["name"]: c
        for c in body_animations(deploy, "custom:hero")["libraries"][0]["clips"]
    }
    assert clips["Workout_Crunch"]["roles"] == ["workout_floor"]
    for part in ("Bicycle_Crunch", "Lie_Down", "Stand_Up"):
        assert clips[part]["part_of"] == [
            {"clip": "Workout_Crunch", "roles": ["workout_floor"]}
        ], part
    assert (
        clips["Standard_Run"]["roles"] == []
        and clips["Standard_Run"]["part_of"] == []
    )


def test_an_accessory_file_is_served_only_by_the_socket_and_item_it_is_listed_under(
    deploy: Path, tmp_path: Path
) -> None:
    hair = deploy / "assets" / "wardrobe" / "socket_head_top" / "Bob.glb"
    hair.parent.mkdir(parents=True)
    hair.write_bytes(b"glb")
    assert accessory_file(deploy, "socket_head_top", "Bob") == hair
    assert (
        accessory_file(deploy, "socket_neck", "Bob") is None
    )  # another socket
    assert (
        accessory_file(deploy, "socket_head_top", "antenna") is None
    )  # parametric
    for socket, item in (
        ("socket_head_top", "../socket_head_top/Bob"),
        ("../wardrobe/socket_head_top", "Bob"),
        ("socket_head_top", "/etc/passwd"),
        ("", ""),
    ):
        assert accessory_file(deploy, socket, item) is None, (socket, item)
