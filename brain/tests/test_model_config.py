"""Tests for the per-model settings file (ADR_MODEL_SETTINGS_PAGE): format, validation, atomic save and
the pose fallback."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from brain.appearance import model_config as mc


@pytest.fixture
def model(tmp_path) -> Path:
    path = tmp_path / "toy.glb"
    path.write_bytes(b"glTF")
    return path


def doc(**sections) -> dict:
    return {"format": 1, **sections}


AVAILABLE = mc.Available(
    libraries={"UAL1": {"Idle_Loop", "Dance_Loop"}, "UAL2": {"Sit_Loop"}},
    sockets={"socket_neck", "socket_waist"},
    items={"socket_waist": {"belt", "pouch"}, "socket_neck": {"scarf"}},
)

FULL = doc(
    animations={
        "libraries": {"UAL2": False},
        "clips": {"Dance_Loop": False},
        "roles": {"idle": ["Idle_Loop"], "workout": None},
    },
    accessories={
        "sockets": {"socket_neck": False},
        "items": {"socket_waist": ["belt"]},
        "placement": {
            "socket_waist": {
                "belt": {
                    "offset": [0, 0, 0.1],
                    "scale": 0.6,
                    "rotation": [0, 0, 90],
                }
            }
        },
    },
    pose={
        "face_yaw": 180,
        "roll_bones": {"hand.L": 90},
        "emissive_gain": 2.0,
        "relax_arms": False,
        "arm_spread": 8,
    },
)


def test_the_documented_example_is_valid():
    assert mc.validate(FULL, AVAILABLE).errors == []
    assert mc.validate(mc.empty()).ok


@pytest.mark.parametrize(
    "bad",
    [
        [],
        {"format": 2},
        doc(extra={}),
        doc(animations={"speed": 2}),
        doc(animations={"libraries": {"UAL1": "no"}}),
        doc(animations={"roles": {"juggle": ["x"]}}),
        doc(animations={"roles": {"idle": []}}),
        doc(animations={"roles": {"idle": ["a", "a"]}}),
        doc(animations={"roles": {"idle": ["../etc/passwd"]}}),
        doc(animations={"roles": {"idle": [3]}}),
        doc(accessories={"items": {"socket_neck": "scarf"}}),
        doc(accessories={"placement": {"s": {"i": {"offset": [0, 0]}}}}),
        doc(accessories={"placement": {"s": {"i": {"offset": [0, 0, 9]}}}}),
        doc(accessories={"placement": {"s": {"i": {"scale": 0}}}}),
        doc(accessories={"placement": {"s": {"i": {"script": "x"}}}}),
        doc(pose={"face_yaw": True}),
        doc(pose={"arm_spread": 90}),
        doc(pose={"emissive_gain": -1}),
        doc(pose={"relax_arms": 1}),
        doc(pose={"roll_bones": {"b": "90"}}),
    ],
)
def test_bad_documents_are_refused(bad):
    assert not mc.validate(bad).ok


def test_a_role_cannot_use_a_clip_that_is_switched_off_for_the_body():
    report = mc.validate(
        doc(
            animations={
                "clips": {"Dance_Loop": False},
                "roles": {"idle": ["Dance_Loop"]},
            }
        ),
        AVAILABLE,
    )
    assert any("switched off" in e for e in report.errors)
    report = mc.validate(
        doc(
            animations={
                "libraries": {"UAL2": False},
                "roles": {"idle": ["Sit_Loop"]},
            }
        ),
        AVAILABLE,
    )
    assert any("library switched off" in e for e in report.errors)


def test_entries_naming_something_gone_are_stale_not_errors():
    report = mc.validate(
        doc(
            animations={
                "libraries": {"OLD": False},
                "clips": {"Gone": False},
                "roles": {"idle": ["Gone"]},
            },
            accessories={
                "sockets": {"socket_x": False},
                "items": {"socket_waist": ["cape"]},
            },
        ),
        AVAILABLE,
    )
    assert report.ok
    assert len(report.stale) == 5


def test_without_availability_nothing_is_called_stale():
    assert mc.validate(FULL).stale == []


def test_no_file_is_no_settings(model):
    assert mc.load(model) == (mc.empty(), [])


def test_save_then_load_round_trips_and_leaves_no_temp_files(model):
    assert mc.save(model, FULL, AVAILABLE).ok
    assert mc.load(model) == (FULL, [])
    assert sorted(p.name for p in model.parent.iterdir()) == [
        "toy.config.json",
        "toy.glb",
    ]


def test_a_refused_save_changes_nothing(model):
    mc.save(model, FULL)
    before = mc.config_path(model).read_text()
    assert not mc.save(model, doc(pose={"arm_spread": 99})).ok
    assert mc.config_path(model).read_text() == before


def test_saving_nothing_removes_the_file(model):
    mc.save(model, FULL)
    assert mc.save(model, mc.empty()).ok
    assert not mc.config_path(model).exists()
    assert mc.save(model, doc(pose={})).ok  # and again, with no file there


@pytest.mark.parametrize(
    "text",
    ["not json", "[]", '{"format": 2}', '{"format": 1, "pose": {"x": 1}}'],
)
def test_a_bad_file_is_ignored_with_a_warning_never_an_error(model, text):
    mc.config_path(model).write_text(text)
    settings, warnings = mc.load(model)
    assert settings == mc.empty() and len(warnings) == 1


def test_a_huge_file_is_ignored(model):
    mc.config_path(model).write_text(" " * (mc.MAX_BYTES + 1))
    assert mc.load(model)[1]


def test_pose_falls_back_to_the_old_sidecar_and_the_config_wins_key_by_key(
    model,
):
    assert mc.pose(model) == {}
    mc.pose_sidecar_path(model).write_text(
        json.dumps({"face_yaw": 90, "emissive_gain": 3})
    )
    assert mc.pose(model) == {"face_yaw": 90, "emissive_gain": 3}
    mc.save(model, doc(pose={"face_yaw": 180}))
    assert mc.pose(model) == {"face_yaw": 180, "emissive_gain": 3}
    assert (
        json.loads(mc.pose_sidecar_path(model).read_text())["face_yaw"] == 90
    )  # left alone


# --- roles: the animations section reaches the plan -----------------------------------------------

from blender import animation_library as al
from blender import pack_profiles as pp

LIB1, LIB2 = Path("/x/UAL1.glb"), Path("/x/UAL2.glb")


@pytest.fixture
def quaternius_roles():
    shipped, _ = pp.shipped_profiles()
    with pp.use_profiles([p.data for p in shipped]):
        yield


def _fitting():
    roles = al.library_roles()
    first = {role: cands[0] for role, cands in roles.items() if cands}
    return {
        LIB1: {first["idle"], first["talk"], first["run"]},
        LIB2: {first["walk"]},
    }, first


def test_no_settings_plan_is_unchanged(quaternius_roles):
    fitting, first = _fitting()
    plan = al.plan_libraries(fitting)
    assert plan["idle"] == (LIB1, first["idle"]) and plan["walk"] == (
        LIB2,
        first["walk"],
    )
    assert al.plan_libraries(fitting, {}) == plan


def test_a_library_or_clip_switched_off_is_not_offered(quaternius_roles):
    fitting, first = _fitting()
    plan = al.plan_libraries(fitting, {"libraries": {"UAL2": False}})
    assert "walk" not in plan and "idle" in plan
    plan = al.plan_libraries(fitting, {"clips": {first["idle"]: False}})
    assert "idle" not in plan and "talk" in plan


def test_a_role_is_filled_with_the_chosen_clips_or_switched_off(
    quaternius_roles,
):
    fitting, first = _fitting()
    fitting[LIB1] |= {"Mine", "Mine_Low", "Mine_Mirror", "Other"}
    warn: list[str] = []
    plan = al.plan_libraries(
        fitting,
        {
            "roles": {
                "idle": ["Mine", "Other"],
                "talk": None,
                "walk": ["Gone", first["walk"]],
            }
        },
        warn,
    )
    assert plan["idle"] == (LIB1, "Mine")
    assert plan["idle.low"] == (LIB1, "Mine_Low")
    assert plan["idle.mirror"] == (LIB1, "Mine_Mirror")
    assert plan["idle.alt1"] == (LIB1, "Other")
    assert not [k for k in plan if k.split(".")[0] == "talk"]
    assert plan["walk"] == (
        LIB2,
        first["walk"],
    )  # the stale clip is skipped, the next one is the role's own
    assert len(warn) == 1 and "Gone" in warn[0]
    assert plan["run"] == (
        LIB1,
        first["run"],
    )  # a role not mentioned stays automatic


def test_a_role_set_for_a_clip_in_no_library_still_builds(quaternius_roles):
    fitting, _ = _fitting()
    warn: list[str] = []
    plan = al.plan_libraries(fitting, {"roles": {"idle": ["Nope"]}}, warn)
    assert "idle" not in plan and warn


# --- accessories and placement ----------------------------------------------------------------------

from brain.appearance import models


@pytest.fixture
def deploy(tmp_path):
    (tmp_path / "assets" / "catalogs").mkdir(parents=True)
    folder = tmp_path / "assets" / "custom_templates"
    folder.mkdir(parents=True)
    (folder / "toy.glb").write_bytes(b"glb")
    return tmp_path


def _toy(deploy) -> Path:
    return deploy / "assets" / "custom_templates" / "toy.glb"


def test_sockets_and_items_narrow_what_the_body_offers(deploy):
    before = models.effective_wardrobe_catalog(deploy, "custom:toy")[
        "accessories"
    ]
    assert "socket_neck" in before and len(before["socket_head_top"]) > 1
    keep = before["socket_head_top"][:1]
    mc.save(
        _toy(deploy),
        doc(
            accessories={
                "sockets": {"socket_neck": False, "socket_nowhere": False},
                "items": {
                    "socket_head_top": [keep[0], "not_an_item"],
                    "socket_eyes": [],
                },
            }
        ),
    )
    after = models.effective_wardrobe_catalog(deploy, "custom:toy")[
        "accessories"
    ]
    assert "socket_neck" not in after and "socket_eyes" not in after
    assert after["socket_head_top"] == keep
    assert after["socket_waist"] == before["socket_waist"]  # untouched


def test_options_and_validation_agree_with_the_settings(deploy):
    """The page's options (build_options) and validate_against_catalogs read the same file."""
    from brain.appearance import spec as specs
    from brain.appearance.build_options import build_options

    mc.save(
        _toy(deploy),
        doc(
            accessories={
                "sockets": {"socket_neck": False},
                "items": {"socket_eyes": ["glasses"]},
            }
        ),
    )
    palette = specs.PaletteSpec(
        primary="#112233",
        secondary="#445566",
        accent="#778899",
        emissive="#AABBCC",
    )

    def look(**accessories):
        return specs.LookSpec(
            base_template="custom:toy",
            has_face=False,
            palette=palette,
            wardrobe=specs.WardrobeSpec(),
            accessories=specs.AccessoriesSpec(**accessories),
        )

    body = next(
        b for b in build_options(deploy)["bodies"] if b["id"] == "custom:toy"
    )
    controls = body["controls"]["accessories"]
    assert (
        not controls["socket_neck"]["available"]
        and not controls["socket_neck"]["choices"]
    )
    assert controls["socket_eyes"]["choices"] == ["glasses"]
    specs.validate_against_catalogs(look(socket_eyes="glasses"), deploy)
    for refused in ({"socket_neck": "scarf"}, {"socket_eyes": "visor"}):
        with pytest.raises(specs.CatalogError):
            specs.validate_against_catalogs(look(**refused), deploy)


def test_a_procedural_body_has_no_settings(deploy):
    assert (
        "socket_neck"
        in models.effective_wardrobe_catalog(deploy, "standard_humanoid_male")[
            "accessories"
        ]
    )


def test_placement_defaults_to_the_identity_and_reads_the_entry():
    s = doc(
        accessories={"placement": {"socket_waist": {"belt": {"scale": 0.6}}}}
    )
    assert mc.placement(s, "socket_waist", "belt") == {
        "offset": (0.0, 0.0, 0.0),
        "scale": 0.6,
        "rotation": (0.0, 0.0, 0.0),
    }
    assert mc.placement(s, "socket_waist", "pouch")["scale"] == 1.0
    assert mc.placement({}, "socket_waist", "belt")["scale"] == 1.0
