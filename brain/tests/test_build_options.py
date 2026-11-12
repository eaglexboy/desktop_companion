"""What the Build page may offer (brain/appearance/build_options.py)."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, cast

import pytest

from brain.appearance import models
from brain.appearance.build_options import HAIR_MATERIAL, build_options
from brain.appearance.spec import (
    AccessoriesSpec,
    Footwear,
    LookSpec,
    PaletteSpec,
    WardrobeSpec,
    validate_against_catalogs,
)

CAPS_FULL = {
    "supported_sockets": list(models.SOCKETS),
    "capabilities": {
        "has_head": True,
        "has_arms": True,
        "has_legs": True,
        "has_face": True,
    },
}
CAPS_NO_HANDS = {
    "supported_sockets": ["socket_head_top", "socket_eyes", "socket_neck"],
    "capabilities": {
        "has_head": True,
        "has_arms": False,
        "has_legs": True,
        "has_face": False,
    },
}


@pytest.fixture
def deploy(tmp_path: Path) -> Path:
    (tmp_path / "assets" / "catalogs").mkdir(parents=True)
    return tmp_path


def _install(
    deploy: Path, folder: str, name: str, caps: dict[str, Any] | None
) -> Path:
    path = deploy / "assets" / folder / f"{name}.glb"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"glb")
    if caps is not None:
        models.caps_path(path).write_text(json.dumps(caps))
    return path


def _bodies(deploy: Path) -> dict[str, dict[str, Any]]:
    return {b["id"]: b for b in build_options(deploy)["bodies"]}


def test_a_fresh_install_offers_the_procedural_bodies(deploy: Path) -> None:
    result = build_options(deploy)
    bodies = _bodies(deploy)
    assert set(bodies) == {
        "standard_humanoid_female",
        "standard_humanoid_male",
        "primitive_blob",
        "primitive_mechanical",
    }
    assert {b["kind"] for b in bodies.values()} == {"procedural"}
    female = bodies["standard_humanoid_female"]["controls"]
    assert female["proportions"]["choices"] == [
        "muscularity",
        "femininity",
        "height",
        "build",
    ]
    assert "sneakers" in female["footwear"]["choices"]
    assert bodies["primitive_blob"]["controls"]["proportions"]["choices"] == [
        "height",
        "build",
    ]
    assert (
        "matte" in result["materials"]
        and "emissive_circuit" in result["materials"]
    )
    assert bodies["primitive_blob"]["note"] is None


def test_an_installed_quaternius_body_is_labelled_and_gives_reasons(
    deploy: Path,
) -> None:
    _install(deploy, "base_templates", "standard_humanoid_female", CAPS_FULL)
    options = {b["id"]: b for b in build_options(deploy)["bodies"]}
    assert options["standard_humanoid_female"]["kind"] == "quaternius"
    controls = options["standard_humanoid_female"]["controls"]
    for name in ("footwear", "proportions", "outfit"):
        assert controls[name]["available"] is False
        assert controls[name]["choices"] == []
        assert controls[name]["reason"]
    assert options["standard_humanoid_female"][
        "note"
    ]  # a textured body says it keeps its own colors
    assert options["standard_humanoid_female"]["has_face"] is True


def test_an_outfit_model_made_for_the_body_makes_outfit_available(
    deploy: Path,
) -> None:
    _install(deploy, "base_templates", "standard_humanoid_male", CAPS_FULL)
    _install(deploy, "outfit_models/standard_humanoid_male", "tunic", None)
    outfit = _bodies(deploy)["standard_humanoid_male"]["controls"]["outfit"]
    assert outfit["available"] is True
    assert outfit["choices"] == ["none", "tunic"]


def test_a_socket_the_rig_lacks_says_which_part_is_missing(
    deploy: Path,
) -> None:
    _install(deploy, "custom_templates", "mascot", CAPS_NO_HANDS)
    accessories = _bodies(deploy)["custom:mascot"]["controls"]["accessories"]
    hand = accessories["socket_hand_R"]
    assert hand["available"] is False
    assert "right hand" in hand["reason"]
    assert accessories["socket_eyes"]["available"] is True
    assert "glasses" in accessories["socket_eyes"]["choices"]


def test_a_held_body_is_left_out_and_release_brings_it_back(
    deploy: Path,
) -> None:
    _install(deploy, "custom_templates", "unchecked", CAPS_FULL)
    models.set_hold(deploy, "custom:unchecked", "licence not checked")
    assert "custom:unchecked" not in _bodies(deploy)
    models.set_hold(deploy, "custom:unchecked", None)
    assert _bodies(deploy)["custom:unchecked"]["kind"] == "installed"


def test_a_converted_body_says_so(deploy: Path) -> None:
    model = _install(deploy, "custom_templates", "hero", CAPS_FULL)
    model.with_name("hero.rig.json").write_text("{}")
    assert _bodies(deploy)["custom:hero"]["kind"] == "converted"


def test_a_body_without_a_credit_file_has_an_empty_credit(deploy: Path) -> None:
    _install(deploy, "custom_templates", "plain", CAPS_FULL)
    assert not _bodies(deploy)["custom:plain"]["credit"]


def test_nothing_offered_is_rejected_by_validation(deploy: Path) -> None:
    """The page offers only what `validate_against_catalogs` accepts."""
    _install(deploy, "base_templates", "standard_humanoid_female", CAPS_FULL)
    _install(deploy, "outfit_models/standard_humanoid_female", "tunic", None)
    _install(deploy, "custom_templates", "mascot", CAPS_NO_HANDS)
    palette = PaletteSpec(
        primary="#112233",
        secondary="#445566",
        accent="#778899",
        emissive="#AABBCC",
    )

    def spec(
        body: dict[str, Any],
        outfit: str = "none",
        footwear: str = "none",
        socket: str | None = None,
        item: str | None = None,
    ) -> LookSpec:
        return LookSpec(
            base_template=body["id"],
            has_face=body["has_face"],
            palette=palette,
            wardrobe=WardrobeSpec(
                outfit=outfit, footwear=cast(Footwear, footwear)
            ),
            accessories=AccessoriesSpec(**({socket: item} if socket else {})),
        )

    checked = 0
    for body in build_options(deploy)["bodies"]:
        controls = body["controls"]
        for outfit in controls["outfit"]["choices"]:
            validate_against_catalogs(spec(body, outfit=outfit), deploy)
            checked += 1
        for footwear in controls["footwear"]["choices"]:
            validate_against_catalogs(spec(body, footwear=footwear), deploy)
            checked += 1
        for socket, control in controls["accessories"].items():
            for item in control["choices"]:
                validate_against_catalogs(
                    spec(body, socket=socket, item=item), deploy
                )
                checked += 1
    assert checked > 20


def _glb_with_materials(names: list[str]) -> bytes:
    """The smallest .glb whose JSON names these materials."""
    body = json.dumps(
        {"asset": {"version": "2.0"}, "materials": [{"name": n} for n in names]}
    ).encode()
    body += b" " * (-len(body) % 4)
    total = 12 + 8 + len(body)
    return (
        b"glTF"
        + (2).to_bytes(4, "little")
        + total.to_bytes(4, "little")
        + len(body).to_bytes(4, "little")
        + b"JSON"
        + body
    )


def test_a_body_with_hair_or_brow_materials_can_have_its_hair_colored(
    deploy: Path,
) -> None:
    path = _install(
        deploy, "base_templates", "standard_humanoid_female", CAPS_FULL
    )
    path.write_bytes(_glb_with_materials(["MI_Hair_2", "MI_Eyes", "MI_Body"]))
    _install(deploy, "custom_templates", "bald", CAPS_FULL).write_bytes(
        _glb_with_materials(["Skin", "Eyes"])
    )
    bodies = _bodies(deploy)
    with_hair = bodies["standard_humanoid_female"]["colors"]
    assert with_hair["body"] == [
        "hair"
    ]  # the body's own colors stay, the hair is tintable
    assert bodies["custom:bald"]["colors"]["body"] == []
    assert bodies["primitive_blob"]["colors"]["body"] == ["primary"]
    assert with_hair["reasons"]["hair"]


def test_a_hair_or_beard_model_file_can_be_colored_but_a_hat_cannot(
    deploy: Path,
) -> None:
    for item, material in (
        ("Hair_Long", "MI_Hair_2.001"),
        ("brownhat", "Brown"),
    ):
        path = (
            deploy / "assets" / "wardrobe" / "socket_head_top" / f"{item}.glb"
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(_glb_with_materials([material]))
    colors = _bodies(deploy)["primitive_blob"]["colors"]
    assert colors["hair_models"] == {"socket_head_top": ["Hair_Long"]}
    assert colors["reasons"]["hairstyle"]


def test_the_page_and_the_build_agree_on_which_materials_are_hair() -> None:
    root = Path(__file__).resolve().parents[2]
    ours = re.search(
        r'HAIR_MATERIAL = re\.compile\(r"([^"]+)"',
        (root / "blender" / "gen_imported.py").read_text(),
    )
    viewer = re.search(
        r"HAIR_MATERIAL = /([^/]+)/i",
        (root / "status_page/static/js/viewer.js").read_text(),
    )
    assert ours and viewer
    assert ours.group(1) == viewer.group(1) == HAIR_MATERIAL.pattern


def test_a_look_without_a_hair_color_is_white_so_it_is_unchanged() -> None:
    palette = PaletteSpec(
        primary="#4A90D9",
        secondary="#FFFFFF",
        accent="#F5A623",
        emissive="#7FD1FF",
    )
    assert palette.hair == palette.hairstyle == "#ffffff"
    with pytest.raises(ValueError):
        PaletteSpec(
            primary="#4A90D9",
            secondary="#FFFFFF",
            accent="#F5A623",
            emissive="#7FD1FF",
            hair="brown",
        )


def test_an_installed_body_keeps_its_own_colors_and_a_built_in_one_does_not(
    deploy: Path,
) -> None:
    _install(deploy, "base_templates", "standard_humanoid_female", CAPS_FULL)
    bodies = _bodies(deploy)
    installed = bodies["standard_humanoid_female"]["colors"]
    assert installed["body"] == [] and installed["surface"] is False
    assert installed["outfit"] == [] and installed["footwear"] == []
    built_in = bodies["primitive_blob"]["colors"]
    assert built_in["body"] == ["primary"] and built_in["surface"] is True
    assert "secondary" in built_in["outfit"]
    # the reasons are what the page shows beside a color nothing uses
    assert built_in["reasons"]["installed"] and built_in["reasons"]["unused"]


def test_every_socket_says_which_colors_its_accessories_take(
    deploy: Path,
) -> None:
    colors = _bodies(deploy)["primitive_blob"]["colors"]
    assert set(colors["accessories"]) == set(models.SOCKETS)
    assert all(
        set(keys) <= {"primary", "secondary", "accent", "emissive"}
        for keys in colors["accessories"].values()
    )
    assert "emissive" in colors["accessories"]["socket_orbit"]


def test_an_accessory_that_is_a_model_file_is_listed_as_using_its_own_colors(
    deploy: Path,
) -> None:
    hat = deploy / "assets" / "wardrobe" / "socket_head_top" / "tophat.glb"
    hat.parent.mkdir(parents=True)
    hat.write_bytes(b"glb")
    colors = _bodies(deploy)["primitive_blob"]["colors"]
    assert colors["models"] == {"socket_head_top": ["tophat"]}


def test_bodies_are_listed_primitives_then_humanoids_then_installed_alphabetically(
    deploy: Path,
) -> None:
    _install(deploy, "custom_templates", "zebra", CAPS_FULL)
    _install(deploy, "custom_templates", "apple", CAPS_FULL)
    _install(deploy, "base_templates", "standard_humanoid_female", CAPS_FULL)
    assert [b["id"] for b in build_options(deploy)["bodies"]] == [
        "primitive_blob",
        "primitive_mechanical",
        "standard_humanoid_female",
        "standard_humanoid_male",
        "custom:apple",
        "custom:zebra",
    ]


def test_accessory_model_files_are_listed_with_their_placement(
    deploy: Path,
) -> None:
    hair = deploy / "assets" / "wardrobe" / "socket_head_top" / "Bob.glb"
    hair.parent.mkdir(parents=True)
    hair.write_bytes(b"glb")
    hair.with_suffix(".json").write_text(
        json.dumps(
            {"offset": [0, 0.01, 0.02], "scale": 0.9, "rotation": [0, 0, 0]}
        )
    )
    accessories = _bodies(deploy)["primitive_blob"]["controls"]["accessories"]
    assert accessories["socket_head_top"]["models"] == {
        "Bob": {
            "offset": [0.0, 0.01, 0.02],
            "scale": 0.9,
            "rotation": [0.0, 0.0, 0.0],
            "skin": "",
        }
    }
    assert "Bob" in accessories["socket_head_top"]["choices"]
    # a parametric item is not a model file: the page cannot draw it
    assert "antenna" not in accessories["socket_head_top"]["models"]
    assert accessories["socket_eyes"]["models"] == {}


def test_an_accessory_skinned_like_the_body_says_so(deploy: Path) -> None:
    hair = deploy / "assets" / "wardrobe" / "socket_head_top" / "Bob.glb"
    hair.parent.mkdir(parents=True)
    hair.write_bytes(b"glb")
    hair.with_suffix(".json").write_text(
        json.dumps({"skin": "body", "units": "body"})
    )
    models_ = _bodies(deploy)["primitive_blob"]["controls"]["accessories"][
        "socket_head_top"
    ]["models"]
    assert models_["Bob"]["skin"] == "body"
