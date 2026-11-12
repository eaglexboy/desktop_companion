"""Which accessories a body can wear: rig reading (blender/sockets.py), the
accessory sidecars, and the catalog the designer sees (brain/appearance/models.py).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from blender.animation_library import REQUIRED_BONES, library_gaps
from blender.sockets import canonical_socket_bones, find_socket_bones
from brain.appearance import models
from brain.appearance.spec import (
    CatalogError,
    LookSpec,
    example_look_spec,
    validate_against_catalogs,
)

MIXAMO = [
    "mixamorig:Hips",
    "mixamorig:Spine",
    "mixamorig:Spine1",
    "mixamorig:Spine2",
    "mixamorig:Neck",
    "mixamorig:Head",
    "mixamorig:HeadTop_End",
    "mixamorig:RightShoulder",
    "mixamorig:RightHand",
    "mixamorig:RightHandThumb1",
    "mixamorig:RightHandIndex1",
    "mixamorig:LeftHand",
    "mixamorig:LeftHandThumb1",
]
BLENDER_STYLE = [
    "root",
    "spine",
    "spine.001",
    "spine.002",
    "neck",
    "head",
    "upper_arm.L",
    "hand.L",
    "thumb.01.L",
    "hand.R",
]


def test_mixamo_rig_gets_every_socket_on_the_right_bones():
    sockets = find_socket_bones(MIXAMO)
    assert (
        sockets["socket_head_top"] == "mixamorig:Head"
        and sockets["socket_eyes"] == "mixamorig:Head"
    )
    assert sockets["socket_neck"] == "mixamorig:Neck"
    assert (
        sockets["socket_chest"] == "mixamorig:Spine2"
        and sockets["socket_waist"] == "mixamorig:Hips"
    )
    assert (
        sockets["socket_hand_R"] == "mixamorig:RightHand"
        and sockets["socket_hand_L"] == "mixamorig:LeftHand"
    )
    assert set(sockets) == set(models.SOCKETS)


def test_blender_style_names_and_fingers_are_not_hands():
    sockets = find_socket_bones(BLENDER_STYLE)
    assert (
        sockets["socket_hand_R"] == "hand.R"
        and sockets["socket_hand_L"] == "hand.L"
    )
    assert (
        sockets["socket_chest"] == "spine.002"
        and sockets["socket_waist"] == "spine"
    )


def test_rig_without_hands_or_neck_loses_only_those_sockets():
    sockets = find_socket_bones(["root", "spine", "head"])
    assert "socket_hand_R" not in sockets and "socket_hand_L" not in sockets
    assert sockets["socket_neck"] == "head"  # no neck bone: the head carries it
    assert sockets["socket_head_top"] == "head"
    assert sockets == canonical_socket_bones()


def test_arm_ending_in_palm_segments_carries_hands_on_the_forearm():
    mech = [
        "Body",
        "Torso",
        "Chest",
        "Neck",
        "Head",
        "UpperArm.L",
        "LowerArm.L",
        "PalmP.L",
        "Pinky1.L",
        "UpperArm.R",
        "LowerArm.R",
        "Thumb1.R",
    ]
    sockets = find_socket_bones(mech)
    assert (
        sockets["socket_hand_L"] == "LowerArm.L"
        and sockets["socket_hand_R"] == "LowerArm.R"
    )
    assert (
        find_socket_bones(["root", "spine", "head", "hand.L"])["socket_hand_L"]
        == "hand.L"
    )  # a real hand bone wins
    assert "socket_hand_R" not in find_socket_bones(
        ["Body", "Head", "LowerArm.L"]
    )  # only the side that has an arm


def test_no_bones_means_no_sockets():
    assert find_socket_bones([]) == {}


@pytest.fixture
def deploy(tmp_path):
    (tmp_path / "assets" / "catalogs").mkdir(parents=True)
    return tmp_path


def _model(
    deploy: Path, folder: str, name: str, caps: dict | None = None
) -> Path:
    path = deploy / "assets" / folder / f"{name}.glb"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"glb")
    if caps is not None:
        models.caps_path(path).write_text(json.dumps(caps))
    return path


def _accessory(
    deploy: Path, socket: str, item: str, meta: dict | None = None
) -> None:
    path = deploy / "assets" / "wardrobe" / socket / f"{item}.glb"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"glb")
    if meta is not None:
        path.with_suffix(".json").write_text(json.dumps(meta))


def test_procedural_body_offers_the_catalog_and_clothes(deploy):
    catalog = models.effective_wardrobe_catalog(
        deploy, "standard_humanoid_female"
    )
    assert "hoodie" in catalog["outfits"] and "sneakers" in catalog["footwear"]
    assert "glasses" in catalog["accessories"]["socket_eyes"]
    blob = models.effective_wardrobe_catalog(deploy, "primitive_blob")
    assert "socket_hand_R" not in blob["accessories"]  # a blob has no hands


def test_installed_body_keeps_its_clothes_and_only_wears_what_its_rig_carries(
    deploy,
):
    _model(
        deploy,
        "custom_templates",
        "robot",
        {
            "capabilities": {
                "has_head": True,
                "has_arms": False,
                "has_legs": False,
            },
            "supported_sockets": [
                "socket_head_top",
                "socket_eyes",
                "socket_chest",
            ],
        },
    )
    catalog = models.effective_wardrobe_catalog(deploy, "custom:robot")
    assert catalog["outfits"] == ["none"] and catalog["footwear"] == ["none"]
    assert set(catalog["accessories"]) == {
        "socket_head_top",
        "socket_eyes",
        "socket_chest",
    }


def test_uninspected_model_is_permissive_so_existing_installs_keep_working(
    deploy,
):
    _model(deploy, "custom_templates", "legacy")  # no .caps.json
    profile = models.body_profile(deploy, "custom:legacy")
    assert profile.imported and profile.sockets is None
    catalog = models.effective_wardrobe_catalog(deploy, "custom:legacy")
    assert "socket_hand_R" in catalog["accessories"] and catalog["outfits"] == [
        "none"
    ]


def test_installed_standard_humanoid_replaces_the_procedural_profile(deploy):
    _model(
        deploy,
        "base_templates",
        "standard_humanoid_male",
        {
            "capabilities": {
                "has_head": True,
                "has_arms": True,
                "has_legs": True,
            },
            "supported_sockets": ["socket_head_top", "socket_hand_R"],
        },
    )
    assert models.body_profile(deploy, "standard_humanoid_male").imported
    assert set(
        models.effective_wardrobe_catalog(deploy, "standard_humanoid_male")[
            "accessories"
        ]
    ) == {
        "socket_head_top",
        "socket_hand_R",
    }
    assert not models.body_profile(
        deploy, "standard_humanoid_female"
    ).imported  # the other is still procedural


def test_accessory_model_on_disk_joins_the_catalog_with_sidecar_settings(
    deploy,
):
    _accessory(
        deploy,
        "socket_head_top",
        "top_hat",
        {"scale": 0.5, "offset": [0, 0, 0.1]},
    )
    found = models.discover_accessory_models(deploy)
    assert [
        (m.socket, m.item, m.scale, m.offset, m.requires) for m in found
    ] == [("socket_head_top", "top_hat", 0.5, (0.0, 0.0, 0.1), ("has_head",))]
    assert (
        "top_hat"
        in models.effective_wardrobe_catalog(
            deploy, "standard_humanoid_female"
        )["accessories"]["socket_head_top"]
    )


def test_accessory_without_a_sidecar_gets_defaults_from_its_socket(deploy):
    _accessory(deploy, "socket_hand_L", "lantern")
    (model,) = models.discover_accessory_models(deploy)
    assert model.scale == 1.0 and model.requires == ("has_arms",)


def test_a_garbled_sidecar_value_falls_back_instead_of_breaking_every_validation(
    deploy,
):
    _accessory(
        deploy, "socket_head_top", "odd_hat", {"scale": "big", "offset": "up"}
    )
    (model,) = models.discover_accessory_models(deploy)
    assert model.scale == 1.0 and model.offset == (0.0, 0.0, 0.0)


def test_accessory_needing_something_the_body_lacks_is_not_offered(deploy):
    _accessory(deploy, "socket_chest", "jetpack", {"requires": ["has_legs"]})
    assert (
        "jetpack"
        in models.effective_wardrobe_catalog(
            deploy, "standard_humanoid_female"
        )["accessories"]["socket_chest"]
    )
    assert (
        "jetpack"
        not in models.effective_wardrobe_catalog(
            deploy, "primitive_mechanical"
        )["accessories"]["socket_chest"]
    )


def _spec(template: str, accessories: dict) -> LookSpec:
    return example_look_spec().model_copy(
        update={"base_template": template, "accessories": accessories}
    )


def test_validation_rejects_an_accessory_the_body_cannot_carry(deploy):
    models_catalogs = deploy / "assets" / "catalogs"
    from brain.appearance.spec import ensure_catalogs_seeded

    ensure_catalogs_seeded(deploy)
    assert models_catalogs.is_dir()
    validate_against_catalogs(
        _spec("standard_humanoid_female", {"socket_hand_R": "wand"}), deploy
    )
    with pytest.raises(
        CatalogError, match="not available for 'primitive_blob'"
    ):
        validate_against_catalogs(
            _spec("primitive_blob", {"socket_hand_R": "wand"}), deploy
        )


def test_add_accessory_installs_the_file_and_sidecar(deploy, tmp_path):
    source = tmp_path / "hat.glb"
    source.write_bytes(_glb({"asset": {"version": "2.0"}, "meshes": [{}]}))
    dest = models.add_accessory(
        deploy,
        source,
        "socket_head_top",
        "top_hat",
        scale=0.4,
        offset=(0, 0, 0.05),
    )
    assert dest.is_file()
    assert json.loads(dest.with_suffix(".json").read_text())["requires"] == [
        "has_head"
    ]
    with pytest.raises(SystemExit):
        models.add_accessory(deploy, source, "socket_nowhere", "x")


def test_rig_with_only_a_named_trunk_and_limbs_carries_head_sockets_on_the_trunk():
    mascot = [
        "_rootJoint",
        "Body_00",
        "upper_arm.L_02",
        "hand.L_04",
        "hand.R_023",
        "thigh.L_039",
    ]
    sockets = find_socket_bones(mascot)
    assert (
        sockets["socket_head_top"] == "Body_00"
        and sockets["socket_chest"] == "Body_00"
    )
    assert (
        sockets["socket_hand_L"] == "hand.L_04"
        and sockets["socket_hand_R"] == "hand.R_023"
    )


def test_limbs_only_rig_never_guesses_a_trunk():
    sockets = find_socket_bones(["root", "upper_arm.L", "hand.L"])
    assert set(sockets) == {"socket_hand_L"}


def _glb(gltf: dict) -> bytes:
    body = json.dumps(gltf).encode()
    body += b" " * (-len(body) % 4)
    return (
        b"glTF"
        + (2).to_bytes(4, "little")
        + (20 + len(body)).to_bytes(4, "little")
        + len(body).to_bytes(4, "little")
        + b"JSON"
        + body
    )


def test_accessory_warnings_flag_a_rigged_or_empty_file(tmp_path):
    rigged = tmp_path / "rigged.glb"
    rigged.write_bytes(
        _glb({"asset": {"version": "2.0"}, "meshes": [{}], "skins": [{}]})
    )
    assert any("rigged" in w for w in models.accessory_warnings(rigged))
    empty = tmp_path / "empty.glb"
    empty.write_bytes(_glb({"asset": {"version": "2.0"}}))
    assert any("no meshes" in w for w in models.accessory_warnings(empty))
    clean = tmp_path / "clean.glb"
    clean.write_bytes(_glb({"asset": {"version": "2.0"}, "meshes": [{}]}))
    assert models.accessory_warnings(clean) == []


# --- outfit models -----------------------------------------------------------------

_CAPS = {
    "supported_sockets": list(models.SOCKETS),
    "capabilities": {"has_head": True, "has_arms": True, "has_legs": True},
}


def _outfit(deploy: Path, body: str, name: str) -> Path:
    path = models.outfit_model_path(deploy, body, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"glb")
    return path


def test_outfit_model_is_offered_only_on_the_body_it_was_made_for(deploy):
    _model(deploy, "base_templates", "standard_humanoid_female", _CAPS)
    _model(deploy, "base_templates", "standard_humanoid_male", _CAPS)
    _outfit(deploy, "standard_humanoid_female", "ranger")
    female = models.effective_wardrobe_catalog(
        deploy, "standard_humanoid_female"
    )
    male = models.effective_wardrobe_catalog(deploy, "standard_humanoid_male")
    assert female["outfits"] == ["none", "ranger"] and female["footwear"] == [
        "none"
    ]
    assert male["outfits"] == ["none"]
    assert (
        "ranger"
        not in models.effective_wardrobe_catalog(deploy, "primitive_blob")[
            "outfits"
        ]
    )


def test_outfit_models_follow_a_custom_body_by_name(deploy):
    _model(deploy, "custom_templates", "robot", _CAPS)
    _outfit(deploy, "custom:robot", "suit")
    assert models.effective_wardrobe_catalog(deploy, "custom:robot")[
        "outfits"
    ] == [
        "none",
        "suit",
    ]


def _outfit_spec(base: str, outfit: str) -> LookSpec:
    spec = example_look_spec().model_copy(deep=True)
    return spec.model_copy(
        update={
            "base_template": base,
            "wardrobe": spec.wardrobe.model_copy(update={"outfit": outfit}),
        }
    )


def test_validation_accepts_an_installed_outfit_and_rejects_an_unknown_or_foreign_one(
    deploy,
):
    _model(deploy, "base_templates", "standard_humanoid_female", _CAPS)
    _model(deploy, "base_templates", "standard_humanoid_male", _CAPS)
    _outfit(deploy, "standard_humanoid_female", "ranger")
    validate_against_catalogs(
        _outfit_spec("standard_humanoid_female", "ranger"), deploy
    )
    with pytest.raises(CatalogError, match="ranger"):
        validate_against_catalogs(
            _outfit_spec("standard_humanoid_male", "ranger"), deploy
        )  # made for the female body
    with pytest.raises(CatalogError, match="options"):
        validate_against_catalogs(
            _outfit_spec("standard_humanoid_female", "no_such_outfit"), deploy
        )


def test_a_generic_garment_on_an_installed_body_is_still_tolerated(deploy):
    _model(deploy, "base_templates", "standard_humanoid_female", _CAPS)
    validate_against_catalogs(
        _outfit_spec("standard_humanoid_female", "hoodie"), deploy
    )  # ignored by the generator, with a warning


def test_add_outfit_needs_an_installed_body_and_a_sane_name(deploy, tmp_path):
    source = tmp_path / "src.glb"
    source.write_bytes(
        _glb({"asset": {"version": "2.0"}, "meshes": [{}], "skins": [{}]})
    )
    with pytest.raises(SystemExit, match="install the body first"):
        models.add_outfit(deploy, source, "standard_humanoid_female", "ranger")
    _model(deploy, "base_templates", "standard_humanoid_female", _CAPS)
    for bad in ("none", "has space", "../x"):
        with pytest.raises(SystemExit):
            models.add_outfit(deploy, source, "standard_humanoid_female", bad)
    dest = models.add_outfit(
        deploy, source, "standard_humanoid_female", "ranger"
    )
    assert (
        dest
        == deploy
        / "assets"
        / "outfit_models"
        / "standard_humanoid_female"
        / "ranger.glb"
        and dest.is_file()
    )


def test_the_generator_and_the_brain_agree_on_where_an_outfit_lives(deploy):
    import ast

    # the generator is split into parts (generator.py and blender/gen_*.py): it is in one of them
    sources = [
        p.read_text()
        for p in sorted(
            (Path(__file__).resolve().parents[2] / "blender").glob("gen*.py")
        )
    ]
    func = next(
        n
        for source in sources
        for n in ast.parse(source).body
        if isinstance(n, ast.FunctionDef) and n.name == "outfit_model_path"
    )
    namespace: dict = {"Path": Path}
    exec(  # noqa: S102 -- our own source, no bpy needed
        compile(ast.Module([func], []), "generator", "exec"), namespace
    )
    for body in ("standard_humanoid_female", "custom:robot"):
        assert namespace["outfit_model_path"](
            deploy, body, "ranger"
        ) == models.outfit_model_path(deploy, body, "ranger")


def test_designer_is_told_which_outfits_each_installed_body_can_wear(deploy):
    from brain.evolution import describe_wearables

    _model(deploy, "base_templates", "standard_humanoid_female", _CAPS)
    _model(deploy, "custom_templates", "mascot", _CAPS)
    _outfit(deploy, "standard_humanoid_female", "ranger")
    notes, _ = describe_wearables(
        deploy, ["primitive_blob", "standard_humanoid_female", "custom:mascot"]
    )
    assert (
        "standard_humanoid_female: ranger" in notes
        and "custom:mascot keep their own clothes" in notes
    )


def test_generate_face_sidecar_reports_why_it_could_not(
    deploy, tmp_path, monkeypatch
):
    model = _model(deploy, "base_templates", "standard_humanoid_female")

    class Done:
        returncode, stdout, stderr = (
            0,
            "",
            "ValueError: no eye mesh found (one whose material is named for the eyes)\n",
        )

    monkeypatch.setattr(models, "_generator_run", lambda args, timeout: Done())
    assert models.generate_face_sidecar(model).startswith("no eye mesh found")
    assert not models.face_sidecar_path(model).exists()


def test_generate_face_sidecar_never_overwrites_a_hand_made_one_unless_forced(
    deploy, monkeypatch
):
    model = _model(deploy, "base_templates", "standard_humanoid_female")
    models.face_sidecar_path(model).write_text('{"hand": "made"}')
    calls = []

    def fake(args, timeout):
        calls.append(args)
        models.face_sidecar_path(model).write_text('{"found": true}')

        class Done:
            returncode, stdout, stderr = 0, "", ""

        return Done()

    monkeypatch.setattr(models, "_generator_run", fake)
    assert models.generate_face_sidecar(model) == "exists" and not calls
    assert json.loads(models.face_sidecar_path(model).read_text()) == {
        "hand": "made"
    }
    assert (
        models.generate_face_sidecar(model, force=True) == "written"
        and len(calls) == 1
    )


def test_inspect_body_reads_an_installed_models_rig_into_its_caps_without_copying_it(
    deploy, monkeypatch
):
    model = _model(deploy, "custom_templates", "mascot")
    seen = []

    def fake_inspect(path, out):
        seen.append((path, out))
        report = {
            "capabilities": {"has_head": True},
            "supported_sockets": ["socket_head_top"],
        }
        out.write_text(json.dumps(report))
        return report

    monkeypatch.setattr(models, "_run_inspect", fake_inspect)
    assert models.inspect_body(deploy, "custom:mascot")[
        "supported_sockets"
    ] == ["socket_head_top"]
    assert seen == [(model, models.caps_path(model))]
    assert models.effective_wardrobe_catalog(deploy, "custom:mascot")[
        "accessories"
    ].keys() == {"socket_head_top"}
    with pytest.raises(SystemExit, match="no installed model"):
        models.inspect_body(deploy, "custom:nothing")


def test_a_held_model_stays_installed_but_is_not_offered_or_accepted_until_released(
    deploy,
):
    from brain.appearance.spec import available_custom_templates

    _model(deploy, "custom_templates", "bmo", _CAPS)
    _model(deploy, "custom_templates", "mech", _CAPS)
    models.set_hold(deploy, "custom:bmo", "licence not checked")
    assert available_custom_templates(deploy) == ["custom:mech"]
    assert (deploy / "assets" / "custom_templates" / "bmo.glb").is_file()
    with pytest.raises(CatalogError, match="held back .licence not checked"):
        validate_against_catalogs(_outfit_spec("custom:bmo", "none"), deploy)
    validate_against_catalogs(_outfit_spec("custom:mech", "none"), deploy)

    models.set_hold(deploy, "custom:bmo", None)
    assert available_custom_templates(deploy) == ["custom:bmo", "custom:mech"]
    validate_against_catalogs(_outfit_spec("custom:bmo", "none"), deploy)
    with pytest.raises(SystemExit, match="not an installed custom"):
        models.set_hold(deploy, "standard_humanoid_female", "x")


def test_install_commands_refuse_files_that_could_never_build(deploy, tmp_path):
    empty = tmp_path / "empty.glb"
    empty.write_bytes(_glb({"asset": {"version": "2.0"}}))
    unskinned = tmp_path / "unskinned.glb"
    unskinned.write_bytes(_glb({"asset": {"version": "2.0"}, "meshes": [{}]}))
    _model(deploy, "base_templates", "standard_humanoid_female", _CAPS)

    with pytest.raises(SystemExit, match="no mesh"):
        models.add_body(deploy, empty, "custom:nothing")
    with pytest.raises(SystemExit, match="no mesh"):
        models.add_accessory(deploy, empty, "socket_head_top", "nothing")
    with pytest.raises(SystemExit, match="not skinned"):
        models.add_outfit(deploy, unskinned, "standard_humanoid_female", "cube")
    with pytest.raises(SystemExit, match="does not exist"):
        models.add_body(deploy, tmp_path / "missing.glb", "custom:missing")
    with pytest.raises(SystemExit, match="letters, digits"):
        models.add_body(deploy, unskinned, "custom:Bad Name")
    assert not (deploy / "assets" / "custom_templates").exists() or not list(
        (deploy / "assets" / "custom_templates").glob("*.glb")
    )


def test_animation_libraries_are_installed_only_if_they_have_clips_and_a_rig(
    deploy, tmp_path
):
    pack = tmp_path / "pack.glb"
    pack.write_bytes(
        _glb(
            {
                "asset": {"version": "2.0"},
                "skins": [{}],
                "animations": [{"name": "Idle_Loop"}, {"name": "Walk_Loop"}],
            }
        )
    )
    dest, clips = models.add_animation_library(deploy, pack, "pack")
    assert (
        dest == deploy / "assets" / "animation_library" / "pack.glb"
        and dest.is_file()
    )
    assert clips == ["Idle_Loop", "Walk_Loop"]

    no_clips = tmp_path / "no_clips.glb"
    no_clips.write_bytes(_glb({"asset": {"version": "2.0"}, "skins": [{}]}))
    with pytest.raises(SystemExit, match="no animations"):
        models.add_animation_library(deploy, no_clips, "no_clips")
    no_rig = tmp_path / "no_rig.glb"
    no_rig.write_bytes(
        _glb({"asset": {"version": "2.0"}, "animations": [{"name": "x"}]})
    )
    with pytest.raises(SystemExit, match="no skeleton"):
        models.add_animation_library(deploy, no_rig, "no_rig")
    with pytest.raises(SystemExit, match="letters, digits"):
        models.add_animation_library(deploy, pack, "bad name")


def _library(path: Path, bones: list[str], clips: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        _glb(
            {
                "asset": {"version": "2.0"},
                "nodes": [{"name": name} for name in bones],
                "skins": [{"joints": list(range(len(bones)))}],
                "animations": [{"name": clip} for clip in clips],
            }
        )
    )


def test_a_body_is_told_which_libraries_fit_its_rig_and_what_they_give(deploy):
    folder = deploy / "assets" / "animation_library"
    core = sorted(REQUIRED_BONES)
    _library(
        folder / "pack.glb",
        ["root", *core],
        [
            "Idle_Loop",
            "Sprint_Loop",
            "Sitting_Enter",
            "Sitting_Idle_Loop",
            "Sitting_Exit",
            "Other",
        ],
    )
    _model(
        deploy,
        "base_templates",
        "standard_humanoid_female",
        {**_CAPS, "bone_names": ["root", *core, "head"]},
    )
    _model(
        deploy,
        "custom_templates",
        "mech",
        {**_CAPS, "bone_names": ["Body", "Arm"]},
    )
    _model(deploy, "custom_templates", "statue", {**_CAPS, "bone_names": []})

    fits = models.animation_support(deploy, "standard_humanoid_female")
    assert fits == {
        "pack": {
            "idle": "Idle_Loop",
            "run": "Sprint_Loop",
            "sit_enter": "Sitting_Enter",
            "sit_idle": "Sitting_Idle_Loop",
            "sit_exit": "Sitting_Exit",
        }
    }
    assert "sit (on a chair it brings)" in models._describe_animations(
        fits or {}
    )
    assert models.animation_support(deploy, "custom:mech") == {
        "pack": {}
    }  # another rig
    assert "does not fit" in models._describe_animations(
        models.animation_support(deploy, "custom:mech") or {}
    )
    assert models.animation_support(deploy, "custom:statue") == {
        "": {}
    }  # no rig of its own


def test_a_body_missing_some_bones_still_wears_a_library_without_their_tracks(
    deploy,
):
    folder = deploy / "assets" / "animation_library"
    core = sorted(REQUIRED_BONES)
    _library(
        folder / "pack.glb",
        ["root", *core, "index_01_l", "ball_l", "spine_02"],
        ["Idle_Loop"],
    )
    _model(
        deploy,
        "custom_templates",
        "handless",
        {**_CAPS, "bone_names": ["root", *core, "spine_02"]},
    )
    without_arm = [b for b in core if b != "upperarm_l"]
    _model(
        deploy,
        "custom_templates",
        "armless",
        {**_CAPS, "bone_names": ["root", *without_arm]},
    )

    assert models.animation_support(deploy, "custom:handless") == {
        "pack": {"idle": "Idle_Loop"}
    }
    assert models.animation_gaps(deploy, "custom:handless") == {
        "pack": ["ball_l", "index_01_l"]
    }
    assert (
        "(no fingers, toes: those stay still)"
        in models._describe_animations(
            models.animation_support(deploy, "custom:handless") or {},
            models.animation_gaps(deploy, "custom:handless"),
        )
    )
    assert models.animation_support(deploy, "custom:armless") == {
        "pack": {}
    }  # a bone it cannot do without
    assert models.animation_gaps(deploy, "custom:armless") == {}


def test_library_gaps_names_what_a_body_lacks_and_refuses_a_body_without_the_core():
    joints = set(REQUIRED_BONES) | {"thumb_01_l", "ball_r"}
    assert library_gaps(joints, set(REQUIRED_BONES)) == {"thumb_01_l", "ball_r"}
    assert library_gaps(joints, joints) == set()
    assert library_gaps(joints, joints - {"pelvis"}) is None
    assert library_gaps(set(), joints) is None  # a library with no rig
    assert (
        library_gaps(set(REQUIRED_BONES) - {"calf_l"}, joints) is None
    )  # nor a library without a leg


def test_the_animation_matrix_lists_libraries_and_what_each_body_gets(deploy):
    folder = deploy / "assets" / "animation_library"
    core = sorted(REQUIRED_BONES)
    _library(
        folder / "pack.glb",
        ["root", *core, "ball_l"],
        ["Idle_Loop", "Sitting_Enter", "Sitting_Idle_Loop", "Sitting_Exit"],
    )
    _model(
        deploy,
        "base_templates",
        "standard_humanoid_female",
        {**_CAPS, "bone_names": ["root", *core]},
    )
    _model(
        deploy, "custom_templates", "mech", {**_CAPS, "bone_names": ["Body"]}
    )

    matrix = models.animation_matrix(deploy)
    assert matrix["libraries"] == [
        {
            "name": "pack",
            "clips": [
                "Idle_Loop",
                "Sitting_Enter",
                "Sitting_Exit",
                "Sitting_Idle_Loop",
            ],
            "joints": len(core) + 2,
        }
    ]
    bodies = {b["body"]: b for b in matrix["bodies"]}
    assert bodies["standard_humanoid_female"]["libraries"]["pack"] == {
        "fits": True,
        "roles": ["idle"],
        "sit": "chair",
        "gaps": ["ball_l"],
    }
    assert bodies["custom:mech"]["libraries"]["pack"]["fits"] is False


def test_a_model_with_no_armature_is_inspected_and_not_reported_as_never_inspected(
    deploy,
):
    core = sorted(REQUIRED_BONES)
    _library(
        deploy / "assets" / "animation_library" / "pack.glb",
        ["root", *core],
        ["Idle_Loop"],
    )
    _model(
        deploy,
        "custom_templates",
        "statue",
        {**_CAPS, "bone_names": [], "warnings": ["no armature found"]},
    )
    _model(
        deploy, "custom_templates", "unread", None
    )  # no .caps.json: this one never was
    bodies = {b["body"]: b for b in models.animation_matrix(deploy)["bodies"]}
    assert (
        bodies["custom:statue"]["libraries"] == {}
    )  # a static mesh: no library fits, and we know it
    assert bodies["custom:unread"]["libraries"] is None  # not analysed yet


def test_ground_sitting_clips_are_preferred_to_the_chair_ones_and_only_a_whole_set_counts(
    deploy,
):
    from blender.animation_library import pick_clips

    both = {
        "Sitting_Enter",
        "Sitting_Idle_Loop",
        "Sitting_Exit",
        "GroundSit_Enter",
        "GroundSit_Idle",
        "GroundSit_Exit",
    }
    assert pick_clips(both)["sit_idle"] == "GroundSit_Idle"
    assert (
        pick_clips({"GroundSit_Enter", "Sitting_Idle_Loop", "Sitting_Exit"})
        == {}
    )  # a mix would sit on air
    assert (
        pick_clips({"Sitting_Enter", "Sitting_Idle_Loop", "Sitting_Exit"})[
            "sit_enter"
        ]
        == "Sitting_Enter"
    )


# --- Mixamo conversion (the parts that need no Blender) ------------------------------------------


def _quaternius_bones() -> set[str]:
    names = {
        "root",
        "pelvis",
        "spine_01",
        "spine_02",
        "spine_03",
        "neck_01",
        "Head",
    }
    for side in ("l", "r"):
        names |= {
            f"{part}_{side}"
            for part in (
                "clavicle",
                "upperarm",
                "lowerarm",
                "hand",
                "thigh",
                "calf",
                "foot",
                "ball",
                "ball_leaf",
            )
        }
        for finger in ("thumb", "index", "middle", "ring", "pinky"):
            names |= {f"{finger}_0{k}_{side}" for k in (1, 2, 3)} | {
                f"{finger}_04_leaf_{side}"
            }
    return names


def _mixamo_bones() -> list[str]:
    names = ["Hips", "Spine", "Spine1", "Spine2", "Neck", "Head", "HeadTop_End"]
    for side in ("Left", "Right"):
        names += [
            f"{side}{p}"
            for p in (
                "Shoulder",
                "Arm",
                "ForeArm",
                "Hand",
                "UpLeg",
                "Leg",
                "Foot",
                "ToeBase",
                "Toe_End",
            )
        ]
        names += [
            f"{side}Hand{f}{k}"
            for f in ("Thumb", "Index", "Middle", "Ring", "Pinky")
            for k in (1, 2, 3, 4)
        ]
    return [f"mixamorig:{n}" for n in names]


def test_the_mixamo_skeleton_maps_one_to_one_onto_the_quaternius_one():
    from blender.animation_library import is_mixamo_rig, mixamo_to_quaternius

    mixamo = _mixamo_bones()
    assert (
        len(mixamo) == 65
        and is_mixamo_rig(mixamo)
        and not is_mixamo_rig(sorted(_quaternius_bones()))
    )
    mapped = {bone: mixamo_to_quaternius(bone) for bone in mixamo}
    assert (
        mapped["mixamorig:HeadTop_End"] is None
    )  # the tip bone has no counterpart
    targets = [t for t in mapped.values() if t]
    assert len(targets) == len(set(targets)) == 64
    assert set(targets) == _quaternius_bones() - {
        "root"
    }  # and `root` is added above the hips
    assert (
        mapped["mixamorig:LeftForeArm"],
        mapped["mixamorig:RightHandPinky4"],
        mapped["mixamorig:Spine1"],
    ) == ("lowerarm_l", "pinky_04_leaf_r", "spine_02")


def test_mixamo_clips_are_named_and_given_defaults_by_what_they_are():
    from blender.animation_library import (
        clip_name_from_file,
        mixamo_clip_defaults,
    )

    assert (
        clip_name_from_file("Mixamo_Standing_Greeting") == "Standing_Greeting"
        and clip_name_from_file("Maximo_Sitting_Rubbing_Arm")
        == "Sitting_Rubbing_Arm"
    )
    assert mixamo_clip_defaults("Standing_Greeting")["variants"] is True
    seated = mixamo_clip_defaults("Sitting_Talking")
    assert seated["seat"] is True and seated["blend"] and seated["max_seconds"]
    angry = mixamo_clip_defaults("Angry")
    assert (
        angry["extract"][0]["name"] == "Angry_Hip_Thrust"
        and angry["in_place"] is False
    )  # the sway is the point
    assert mixamo_clip_defaults("Wave") == {
        "loop": False,
        "variants": False,
        "mirror": True,
        "seat": False,
        "in_place": False,
        "trim": None,
        "extract": [],
    }


def _toy_clip(n: int = 20):
    from blender.animation_library import Clip

    swing = (
        [(1.0, 0.0, 0.0, 0.0)] * 5
        + [(0.9239, 0.3827, 0.0, 0.0)] * 10
        + [(1.0, 0.0, 0.0, 0.0)] * (n - 15)
    )
    return Clip(
        [float(i + 1) for i in range(n)],
        {"upperarm_l": swing},
        ([(0.0, 0.0, 0.0)] * 5 + [(0.2, 0.0, 0.0)] * (n - 5)),
    )


def test_clip_processing_trims_loops_scales_and_blends():
    from blender.animation_library import (
        blend_through,
        in_place_clip,
        loop_clip,
        q_angle_deg,
        scale_intensity,
        seat_offset,
        trim_clip,
    )

    clip = _toy_clip()
    cut = trim_clip(clip, 6, 15)
    assert (
        len(cut) == 10
        and cut.frames[0] == 0
        and cut.quats["upperarm_l"][0][1] > 0.3
    )
    with pytest.raises(ValueError):
        trim_clip(clip, 100, 200)

    looped = loop_clip(cut, blend=3)
    assert (
        q_angle_deg(
            looped.quats["upperarm_l"][-1], looped.quats["upperarm_l"][0]
        )
        < 1e-3
    )  # ends where it started
    assert looped.hips[-1] == looped.hips[0]

    calm, eager = scale_intensity(clip, 0.5, 0.8), scale_intensity(
        clip, 1.4, 1.25
    )
    reach = lambda c: max(
        q_angle_deg(q, c.quats["upperarm_l"][0]) for q in c.quats["upperarm_l"]
    )
    assert reach(calm) < reach(clip) < reach(eager)
    assert (
        calm.frames[-1] > clip.frames[-1] > eager.frames[-1]
    )  # slower and faster
    assert in_place_clip(clip).hips[-1][0] == clip.hips[0][0]

    rest = (1.0, 0.0, 0.0, 0.0)
    eased = blend_through(
        cut, {"upperarm_l": rest}, (0.0, 0.0, 0.0), 4
    )  # `cut` starts away from rest
    assert len(eased) == len(cut) + 8
    assert q_angle_deg(eased.quats["upperarm_l"][0], rest) < q_angle_deg(
        eased.quats["upperarm_l"][3], rest
    )  # eases in
    assert eased.hips[0][0] < eased.hips[4][0]

    assert seat_offset((0.0, 0.0, 0.7), (0.0, 0.3, 0.55), 0.3) == (
        0.0,
        0.3,
        pytest.approx(-0.15),
    )  # lowered to the seat
    assert seat_offset((0.0, 0.0, 0.7), (0.0, 0.3, 0.55), 0.05)[
        2
    ] == pytest.approx(
        -0.05
    )  # not through the floor
    assert seat_offset((0.0, 0.0, 0.5), (0.0, 0.3, 0.55), 0.0)[
        2
    ] == pytest.approx(
        0.05
    )  # raised if it sits lower


def test_key_reduction_keeps_the_shape_of_the_motion():
    from blender.animation_library import q_angle_deg, reduce_keys

    clip = _toy_clip(40)
    frames, values = clip.frames, clip.quats["upperarm_l"]
    kept = reduce_keys(frames, values, q_angle_deg, 0.25)
    assert (
        kept[0] == 0 and kept[-1] == len(frames) - 1 and len(kept) < len(frames)
    )
    flat = reduce_keys(
        frames[:8], [(1.0, 0.0, 0.0, 0.0)] * 8, q_angle_deg, 0.25
    )
    assert flat == [0, 7]


def test_mirrored_clips_are_listed_beside_the_clip_they_mirror():
    from blender.animation_library import mirror_bone, pick_clips

    clips = {
        "Standing_Greeting",
        "Standing_Greeting_Low",
        "Standing_Greeting_Mirror",
        "Standing_Greeting_Low_Mirror",
        "Angry",
        "Idle_Loop",
    }
    picked = pick_clips(clips)
    assert picked["greet.mirror"] == "Standing_Greeting_Mirror"
    assert picked["greet.low.mirror"] == "Standing_Greeting_Low_Mirror"
    assert (
        "greet.high.mirror" not in picked and "angry.mirror" not in picked
    )  # only where the copy exists
    assert "idle.mirror" not in picked
    assert (
        mirror_bone("hand_l"),
        mirror_bone("thigh_r"),
        mirror_bone("spine_02"),
    ) == (
        "hand_r",
        "thigh_l",
        "spine_02",
    )


# --- floor clips: sleeping, working out --------------------------------------------------------


def _clip(frames: int, quat, z: float = 0.0):
    from blender.animation_library import Clip

    return Clip(
        [float(i) for i in range(frames)],
        {"x": [quat] * frames},
        [(0.0, 0.0, z)] * frames,
    )


def test_a_clip_can_be_reversed_eased_and_joined_to_another():
    from blender.animation_library import (
        ease_ends,
        join_clips,
        pose_at,
        reverse_clip,
    )

    rising = _clip(3, (1.0, 0.0, 0.0, 0.0))
    rising.hips = [(0.0, 0.0, 0.0), (0.0, 0.0, 0.5), (0.0, 0.0, 1.0)]
    back = reverse_clip(rising)
    assert [h[2] for h in back.hips] == [1.0, 0.5, 0.0] and back.frames == [
        0.0,
        1.0,
        2.0,
    ]

    turned = _clip(2, (0.7071, 0.7071, 0.0, 0.0), z=2.0)
    eased = ease_ends(turned, pose_at(rising, 0), pose_at(rising, -1), 2)
    assert len(eased) == 6 and eased.frames == [0.0, 1.0, 2.0, 3.0, 4.0, 5.0]
    assert [round(h[2], 3) for h in eased.hips] == [
        0.667,
        1.333,
        2.0,
        2.0,
        1.667,
        1.333,
    ]  # in from 0, out to 1 (the rising clip's first and last)
    assert (
        ease_ends(turned, None, None, 5).frames == turned.frames
    )  # nothing to ease to

    joined = join_clips([rising, turned], 2)
    assert len(joined) == 3 + 2 + 2
    assert [round(h[2], 3) for h in joined.hips] == [
        0.0,
        0.5,
        1.0,
        1.333,
        1.667,
        2.0,
        2.0,
    ]


def test_finish_clips_eases_each_clip_to_the_clips_it_follows_and_composes_the_workouts():
    from blender.animation_library import Clip, finish_clips, pose_at

    lying = _clip(4, (1.0, 0.0, 0.0, 0.0), z=0.2)
    stand = _clip(4, (1.0, 0.0, 0.0, 0.0), z=1.0)
    idle = pose_at(_clip(2, (1.0, 0.0, 0.0, 0.0), z=0.9), 0)
    made: dict[str, tuple[Clip, dict]] = {
        "Sleeping_Idle": (lying, {}),
        "Stand_Up": (
            stand,
            {
                "ease_from": "Sleeping_Idle",
                "ease_to": "@stand",
                "ease_frames": 2,
            },
        ),
        "Laying_Idle": (
            _clip(3, (1.0, 0.0, 0.0, 0.0), z=0.3),
            {"blend_ref": "Sleeping_Idle", "ease_frames": 2},
        ),
        "Lonely": (
            _clip(3, (1.0, 0.0, 0.0, 0.0)),
            {"ease_from": "Not_There", "ease_to": "@nothing"},
        ),
    }
    derived = [
        {
            "name": "Workout",
            "options": {"compose": ["Stand_Up", ["Laying_Idle", 2]], "ease": 1},
        },
        {"name": "Missing", "options": {"compose": ["Stand_Up", "Not_There"]}},
    ]
    done = finish_clips(made, derived, {"@stand": idle})

    assert (
        len(done["Stand_Up"][0]) == 4 + 2 + 2
    )  # eased in from the sleeping pose and out to the idle
    assert done["Stand_Up"][0].hips[0][2] == pytest.approx(
        0.2 + (1.0 - 0.2) / 3
    )
    assert done["Stand_Up"][0].hips[-1][2] == pytest.approx(
        1.0 + (0.9 - 1.0) * 2 / 3
    )
    assert (
        len(done["Laying_Idle"][0]) == 3 + 2 + 2
    )  # both ends to the sleeping pose
    assert (
        len(done["Lonely"][0]) == 3
    )  # references that are not there are ignored
    assert (
        len(done["Workout"][0]) == 4 + 1 + 3 + 1 + 3
    )  # made of the raw clips: Stand_Up, Laying_Idle twice
    assert "Missing" not in done  # a part is not there: no workout


def test_floor_clips_have_the_options_and_roles_they_need():
    from blender.animation_library import (
        mixamo_clip_defaults,
        mixamo_derived_clips,
        pick_clips,
    )

    stand_up = mixamo_clip_defaults("Stand_Up")
    assert (
        stand_up["in_place"]
        and stand_up["ease_to"] == "@stand"
        and stand_up["ease_from"] == "Sleeping_Idle"
    )
    (lie_down,) = stand_up["extract"]
    assert (
        lie_down["name"] == "Lie_Down"
        and lie_down["reverse"]
        and lie_down["ease_from"] == "@stand"
    )
    assert mixamo_clip_defaults("Sleeping_Idle_1")["loop"]
    sleeping = mixamo_clip_defaults("Sleeping_Idle")
    assert not sleeping["loop"]  # its end is a roll right over: no loop
    (roll,) = sleeping["extract"]
    assert (
        roll["name"] == "Sleeping_Roll"
        and roll["ping_pong"] > 1
        and roll["max_seconds"] < 7
    )
    assert mixamo_clip_defaults("Laying_Idle")["blend_ref"] == "Sleeping_Idle"
    assert (
        mixamo_clip_defaults("Bicycle_Crunch")["loop"]
        and not mixamo_clip_defaults("Burpee")["loop"]
    )

    assert [
        d["name"]
        for d in mixamo_derived_clips({"Stand_Up", "Burpee", "Bicycle_Crunch"})
    ] == ["Workout_Burpee", "Workout_Crunch"]
    assert mixamo_derived_clips({"Burpee"}) == []  # no way to get up

    picked = pick_clips(
        {
            "Workout_Burpee",
            "Workout_Crunch",
            "Lie_Down",
            "Sleeping_Idle_1",
            "Sleeping_Roll",
            "Laying_Idle",
            "Stand_Up",
            "Standard_Run",
        }
    )
    assert picked == {
        "workout": "Workout_Burpee",
        "workout_floor": "Workout_Crunch",
        "lie_down": "Lie_Down",
        "sleep": "Sleeping_Idle_1",
        "sleep_alt": "Sleeping_Roll",
        "wake": "Stand_Up",
    }


def test_a_ping_pong_clip_ends_where_it_began_and_is_still_at_the_turn():
    import math

    from blender.animation_library import Clip, ping_pong, q_angle_deg

    def about_x(deg):
        h = math.radians(deg) / 2
        return (math.cos(h), math.sin(h), 0.0, 0.0)

    degrees = [0.0, 10.0, 25.0, 45.0, 60.0]  # turns away, one sample per frame
    clip = Clip(
        [float(i) for i in range(5)],
        {"pelvis": [about_x(d) for d in degrees]},
        [(0.0, 0.0, z) for z in (0.0, 0.1, 0.2, 0.3, 0.4)],
    )
    out = ping_pong(clip, 2.0)
    angle = [
        q_angle_deg(out.quats["pelvis"][0], q) for q in out.quats["pelvis"]
    ]

    assert len(out) == 2 * 4 * 2 + 1  # out and back, twice as slow
    assert out.frames == [
        float(i) for i in range(len(out))
    ]  # evenly spaced from 0
    assert (
        angle[0] == 0 and angle[-1] < 1e-6
    )  # back in the first pose, not a snap to it
    assert (
        abs(max(angle) - 60) < 1e-6 and angle.index(max(angle)) == len(out) // 2
    )
    assert (
        out.hips[0] == out.hips[-1]
        and abs(out.hips[len(out) // 2][2] - 0.4) < 1e-9
    )
    # eased: it moves least at the start and at the turn, so it does not bounce off the turn
    steps = [abs(b - a) for a, b in zip(angle, angle[1:])]
    middle = len(out) // 2
    assert steps[0] < max(steps) / 5 and steps[middle - 1] < max(steps) / 5
    # the same path both ways
    assert all(abs(angle[i] - angle[-1 - i]) < 1e-6 for i in range(len(out)))
    assert (
        len(ping_pong(clip, 1.0)) == 9
        and len(ping_pong(clip.copy(), 3.0)) == 25
    )


def test_a_body_that_lies_down_turns_on_the_floor_instead_of_over_its_head():
    import math

    from blender.animation_library import (
        IDENTITY_Q,
        lying_turn,
        q_dot,
        q_rotate,
        q_slerp,
    )

    def about(axis, deg):
        h, s = math.radians(deg) / 2, math.sin(math.radians(deg) / 2)
        return (math.cos(h), axis[0] * s, axis[1] * s, axis[2] * s)

    # the bone's y is the body's length and lies along the floor (z is up); the second pose has it
    # turned right round, as from a push-up position with the head one way to lying on the back
    # with the head the other
    start, end = IDENTITY_Q, about((1, 0, 0), 180)
    turn = lying_turn(start, end, IDENTITY_Q)
    assert turn is not None

    def height(q):
        return abs(q_rotate(q, (0.0, 1.0, 0.0))[2])

    ts = [k / 40 for k in range(41)]
    plain = max(height(q_slerp(start, end, t)) for t in ts)
    assert plain > 0.99  # the slerp stands the body on its head half way
    assert max(height(turn(t)) for t in ts) < 0.15  # this stays on the floor
    assert abs(abs(q_dot(turn(0.0), start)) - 1) < 1e-9
    assert abs(abs(q_dot(turn(1.0), end)) - 1) < 1e-9
    # and it is a turn, not a jump: no step is large
    steps = [
        2 * math.degrees(math.acos(min(1, abs(q_dot(turn(a), turn(b))))))
        for a, b in zip(ts, ts[1:])
    ]
    assert max(steps) < 12

    # the body's own rest orientation says which way is up: bone y up when standing
    up = about((1, 0, 0), 90)
    standing = lying_turn(IDENTITY_Q, about((0, 1, 0), 90), up)
    assert standing is None  # standing in the first pose: the slerp will do
    assert lying_turn(start, end, None) is None


# --- native clips on the Quaternius skeleton (Mesh2Motion...) ---------------------------------


def test_native_clips_are_eased_looped_and_joined_to_the_floor_work():
    from blender.animation_library import (
        mixamo_clip_defaults,
        mixamo_derived_clips,
    )

    for gesture in ("Bow", "Help_One_Arm", "Help_Two_Arms", "Confused"):
        defaults = mixamo_clip_defaults(gesture)
        assert defaults["blend_ref"] == "@stand" and not defaults["mirror"]
    assert mixamo_clip_defaults("Sleeping")["loop"]
    pushup = mixamo_clip_defaults("Pushup")
    assert pushup["loop"] and pushup["in_place"]
    meditate = mixamo_clip_defaults("Meditate")
    assert (
        meditate["loop"] and meditate["emit"] is False
    )  # only its derived clips are used

    derived = {
        d["name"]: d["options"]
        for d in mixamo_derived_clips(
            {"Pushup", "Sleeping", "Meditate", "Stand_Up"}
        )
    }
    assert derived["Workout_Pushup"]["compose"] == [
        "Lie_Down",
        ["Pushup", 6],
        "Stand_Up",
    ]
    assert (
        derived["Lie_Down_Back"]["ease_to"] == "Sleeping"
    )  # lies down into its pose
    assert (
        derived["Stand_Up_Back"]["ease_from"] == "Sleeping"
    )  # and gets up from it
    assert derived["Meditate_Enter"]["hold"] == "Meditate"
    assert derived["Meditate_Exit"]["ease_to"] == "@stand"
    # no way to get up, no floor work; the meditation needs nothing else
    names = {
        d["name"]
        for d in mixamo_derived_clips({"Pushup", "Sleeping", "Meditate"})
    }
    assert names == {"Meditate_Idle", "Meditate_Enter", "Meditate_Exit"}


def test_native_clips_fill_the_roles_they_are_for():
    from blender.animation_library import pick_clips

    picked = pick_clips(
        {
            "Bow",
            "Help_One_Arm",
            "Help_Two_Arms",
            "Confused",
            "Sleeping",
            "Lie_Down_Back",
            "Stand_Up_Back",
            "Workout_Pushup",
            "Meditate_Enter",
            "Meditate_Idle",
            "Meditate_Exit",
        }
    )
    assert picked["attention"] == "Help_One_Arm"
    assert picked["attention.alt1"] == "Help_Two_Arms"
    assert picked["think_long"] == "Confused"
    assert picked["greet"] == "Bow"  # no greeting of its own: Bow stands in
    assert (picked["sleep"], picked["lie_down"], picked["wake"]) == (
        "Sleeping",
        "Lie_Down_Back",
        "Stand_Up_Back",
    )
    assert picked["workout_floor"] == "Workout_Pushup"
    assert picked["floor_sit_idle"] == "Meditate_Idle"


def test_the_clips_of_a_set_keep_their_option_numbers_whichever_library_has_them():
    from blender.animation_library import merge_picks, pick_clips

    side = (
        {  # a way of sleeping with a shift in it, and the way to get up slowly
            "Lie_Down",
            "Sleeping_Idle_1",
            "Sleeping_Roll",
            "Stand_Up",
            "Standing_Up",
            "FloorSit_Enter",
            "FloorSit_Idle",
            "FloorSit_Exit",
        }
    )
    back = {  # another, with neither
        "Lie_Down_Back",
        "Sleeping",
        "Stand_Up_Back",
        "Meditate_Enter",
        "Meditate_Idle",
        "Meditate_Exit",
    }
    plan: dict = {}
    merge_picks(plan, pick_clips(back), "back")  # (a library that sorts first)
    merge_picks(plan, pick_clips(side), "side")
    assert plan["sleep"] == ("back", "Sleeping")
    assert plan["sleep.alt1"] == ("side", "Sleeping_Idle_1")
    assert (
        plan["lie_down"][1] == "Lie_Down_Back"
        and plan["lie_down.alt1"][1] == "Lie_Down"
    )
    assert (
        plan["wake"][1] == "Stand_Up_Back"
        and plan["wake.alt1"][1] == "Stand_Up"
    )
    # the shift and the slow get-up belong to the second set, not the first
    assert (
        "sleep_alt" not in plan and plan["sleep_alt.alt1"][1] == "Sleeping_Roll"
    )
    assert (
        "wake_slow" not in plan and plan["wake_slow.alt1"][1] == "Standing_Up"
    )
    # the same for the floor sit
    assert plan["floor_sit_idle"][1] == "Meditate_Idle"
    assert plan["floor_sit_enter.alt1"][1] == "FloorSit_Enter"
    assert plan["floor_sit_exit.alt1"][1] == "FloorSit_Exit"

    again: dict = {}  # in the other order the same sets are the same numbers
    merge_picks(again, pick_clips(side), "side")
    merge_picks(again, pick_clips(back), "back")
    assert (
        again["sleep"][1] == "Sleeping_Idle_1"
        and again["sleep.alt1"][1] == "Sleeping"
    )
    assert (
        again["sleep_alt"][1] == "Sleeping_Roll"
        and "sleep_alt.alt1" not in again
    )
    assert again["wake.alt1"][1] == "Stand_Up_Back"


# --- clips that are options for an action -------------------------------------------------------


def test_a_role_with_several_clips_lists_them_as_options_with_their_variants():
    from blender.animation_library import pick_clips

    picked = pick_clips(
        {
            "Idle_Talking_Loop",
            "Talking",
            "Talking_1",
            "Waving",
            "Waving_Low",
            "Waving_Mirror",
            "Looking_Around",
            "Sleeping_Idle",
            "Sleeping_Idle_1",
        }
    )
    assert (picked["talk"], picked["talk.alt1"], picked["talk.alt2"]) == (
        "Idle_Talking_Loop",
        "Talking",
        "Talking_1",
    )
    assert (
        picked["greet"] == "Waving"
        and picked["greet.low"] == "Waving_Low"
        and picked["greet.mirror"] == "Waving_Mirror"
    )
    assert (
        picked["idle_look"] == "Looking_Around"
    )  # the role's own clip is not there: the extra stands in
    assert picked["sleep"] == "Sleeping_Idle_1"
    assert "sleep.alt1" not in picked and "sleep_alt" not in picked
    assert (
        pick_clips({"Waving", "Waving_Low"}, {"greet"}) == {}
    )  # a role another library has is left to it


def test_the_options_of_several_libraries_are_all_kept():
    from blender.animation_library import merge_picks, pick_clips

    plan: dict = {}
    merge_picks(
        plan,
        pick_clips(
            {"Talking", "Talking_1", "Waving", "Waving_Low", "Standing_Up"}
        ),
        "floor",
    )
    held = {
        key
        for key in plan
        if key.split(".")[0]
        not in ("talk", "greet", "sleep", "sleep_alt", "idle_look")
    }
    merge_picks(
        plan,
        pick_clips(
            {
                "Idle_Talking_Loop",
                "Idle_Loop",
                "Standing_Greeting",
                "Standing_Greeting_Low",
                "Stand_Up",
            },
            held,
        ),
        "ual",
    )
    assert (
        plan["talk"] == ("floor", "Talking")
        and plan["talk.alt1"] == ("floor", "Talking_1")
        and plan["talk.alt2"] == ("ual", "Idle_Talking_Loop")
    )
    assert plan["greet"] == ("floor", "Waving") and plan["greet.low"] == (
        "floor",
        "Waving_Low",
    )
    assert plan["greet.alt1"] == ("ual", "Standing_Greeting") and plan[
        "greet.alt1.low"
    ] == ("ual", "Standing_Greeting_Low")
    assert plan["idle"] == ("ual", "Idle_Loop") and plan["wake_slow"] == (
        "floor",
        "Standing_Up",
    )
    assert plan["wake"] == ("ual", "Stand_Up")


def test_new_floor_clips_have_the_options_they_need():
    from blender.animation_library import (
        finish_clips,
        mixamo_clip_defaults,
        mixamo_derived_clips,
        pose_at,
    )

    assert (
        mixamo_clip_defaults("Talking_1")["loop"]
        and mixamo_clip_defaults("Waving")["variants"]
    )
    assert (
        mixamo_clip_defaults("Looking")["blend_ref"] == "@stand"
        and mixamo_clip_defaults("Standing_Up")["ease_from"] == "Sleeping_Idle"
    )
    names = {d["name"] for d in mixamo_derived_clips({"Ground_Sitting_Idle"})}
    assert names == {"FloorSit_Idle", "FloorSit_Enter", "FloorSit_Exit"}

    seated = _clip(4, (1.0, 0.0, 0.0, 0.0), z=0.14)
    stand = pose_at(_clip(2, (0.7071, 0.7071, 0.0, 0.0), z=1.0), 0)
    done = finish_clips(
        {"Ground_Sitting_Idle": (seated, {})},
        mixamo_derived_clips({"Ground_Sitting_Idle"}),
        {"@stand": stand},
    )
    enter, leave = done["FloorSit_Enter"][0], done["FloorSit_Exit"][0]
    assert len(enter) == 2 + 36 and len(leave) == 2 + 36
    assert enter.hips[0][2] > enter.hips[-1][2] and enter.hips[-1][
        2
    ] == pytest.approx(
        0.14
    )  # down from standing to the floor
    assert (
        leave.hips[0][2] == pytest.approx(0.14)
        and leave.hips[-1][2] > leave.hips[0][2]
    )  # and back up
    assert len(done["FloorSit_Idle"][0]) == 4


def test_clips_that_start_after_time_zero_are_reported_because_they_flash_the_rest_pose():
    from blender.animation_library import late_starts

    gltf = {
        "accessors": [{"min": [0.0]}, {"min": [0.033]}, {"min": [0.0]}, {}],
        "animations": [
            {"name": "Fine", "samplers": [{"input": 0}, {"input": 2}]},
            {"name": "Late", "samplers": [{"input": 0}, {"input": 1}]},
            {"name": "Unknown", "samplers": [{"input": 3}]},
        ],
    }
    assert late_starts(gltf) == ["Late"]
    assert late_starts({}) == []


def test_installing_a_library_whose_clips_start_late_warns(
    deploy, tmp_path, capsys
):
    pack = tmp_path / "late.glb"
    pack.write_bytes(
        _glb(
            {
                "asset": {"version": "2.0"},
                "skins": [{}],
                "accessors": [{"min": [0.067]}],
                "animations": [
                    {"name": "Late_Loop", "samplers": [{"input": 0}]}
                ],
            }
        )
    )
    models.add_animation_library(deploy, pack, "late")
    assert "Late_Loop" in capsys.readouterr().err


def _install_body(deploy: Path, stem: str = "toy") -> Path:
    """A custom body as `add-body` leaves one: the model, its sidecars and a retargeted library."""
    folder = deploy / "assets" / "custom_templates"
    folder.mkdir(parents=True, exist_ok=True)
    model = folder / f"{stem}.glb"
    model.write_bytes(b"glTF")
    for suffix in (
        ".caps.json",
        ".credits.json",
        ".face.json",
        ".pose.json",
        ".rig.json",
        ".hold",
    ):
        (folder / f"{stem}{suffix}").write_text("{}")
    derived = deploy / "assets" / "animation_library_body" / stem
    derived.mkdir(parents=True)
    (derived / "UAL1_Standard.glb").write_bytes(b"glTF")
    return model


def test_remove_body_deletes_the_model_and_what_belongs_to_it_only(deploy):
    _install_body(deploy, "toy")
    other = _install_body(deploy, "toy_two")
    (deploy / "assets" / "custom_templates" / "toy.glb.keep").write_text(
        "not its own"
    )
    gone = models.remove_body(deploy, "custom:toy")
    assert {p.name for p in gone} == {
        "toy.glb",
        "toy.caps.json",
        "toy.credits.json",
        "toy.face.json",
        "toy.pose.json",
        "toy.rig.json",
        "toy.hold",
        "UAL1_Standard.glb",
        "toy",
    }
    assert not (deploy / "assets" / "animation_library_body" / "toy").exists()
    assert not (deploy / "assets" / "custom_templates" / "toy.glb").exists()
    assert (deploy / "assets" / "custom_templates" / "toy.glb.keep").is_file()
    assert (
        other.is_file()
        and (deploy / "assets" / "animation_library_body" / "toy_two").is_dir()
    )


def test_remove_body_dry_run_deletes_nothing_and_unknown_is_refused(deploy):
    model = _install_body(deploy)
    assert model in models.remove_body(deploy, "custom:toy", dry_run=True)
    assert model.is_file()
    with pytest.raises(SystemExit, match="not an installed body"):
        models.remove_body(deploy, "custom:nope")


def test_remove_body_refuses_the_body_being_worn_unless_forced(deploy):
    model = _install_body(deploy)
    (deploy / "manifest.json").write_text(
        json.dumps(
            {
                "current_version": 2,
                "versions": [
                    {
                        "version": 1,
                        "look_spec": {"base_template": "primitive_blob"},
                    },
                    {
                        "version": 2,
                        "look_spec": {"base_template": "custom:toy"},
                    },
                ],
            }
        )
    )
    with pytest.raises(SystemExit, match="wears now"):
        models.remove_body(deploy, "custom:toy")
    assert model.is_file()
    models.remove_body(deploy, "custom:toy", force=True)
    assert not model.exists()


def test_an_accessory_made_for_one_body_is_offered_only_on_it(deploy):
    """A hairstyle fits the head it was authored for: the male cut leaves a female skull bare."""
    _accessory(
        deploy,
        "socket_head_top",
        "FemaleCut",
        {"bodies": ["standard_humanoid_female"]},
    )
    _accessory(deploy, "socket_head_top", "AnyCut")
    female = models.effective_wardrobe_catalog(
        deploy, "standard_humanoid_female"
    )
    male = models.effective_wardrobe_catalog(deploy, "standard_humanoid_male")
    assert {"FemaleCut", "AnyCut"} <= set(
        female["accessories"]["socket_head_top"]
    )
    assert "AnyCut" in male["accessories"]["socket_head_top"]
    assert "FemaleCut" not in male["accessories"]["socket_head_top"]


def test_add_accessory_records_the_bodies_it_is_for(deploy, tmp_path):
    source = tmp_path / "hair.glb"
    source.write_bytes(_glb({"asset": {"version": "2.0"}, "meshes": [{}]}))
    dest = models.add_accessory(
        deploy,
        source,
        "socket_head_top",
        "Cut",
        bodies=["standard_humanoid_male"],
    )
    assert json.loads(dest.with_suffix(".json").read_text())["bodies"] == [
        "standard_humanoid_male"
    ]
    everyone = models.add_accessory(deploy, source, "socket_head_top", "Any")
    assert "bodies" not in json.loads(everyone.with_suffix(".json").read_text())


def test_add_accessory_records_skin_and_units_only_when_asked(deploy, tmp_path):
    source = tmp_path / "hair.glb"
    source.write_bytes(_glb({"asset": {"version": "2.0"}, "meshes": [{}]}))
    plain = models.add_accessory(deploy, source, "socket_head_top", "Plain")
    meta = json.loads(plain.with_suffix(".json").read_text())
    assert "skin" not in meta and "units" not in meta
    hair = models.add_accessory(
        deploy, source, "socket_head_top", "Cut", skin="body", units="body"
    )
    meta = json.loads(hair.with_suffix(".json").read_text())
    assert meta["skin"] == "body" and meta["units"] == "body"
    found = {m.item: m for m in models.discover_accessory_models(deploy)}
    assert found["Cut"].skin == "body" and found["Cut"].units == "body"
    assert found["Plain"].skin == "" and found["Plain"].units == ""
