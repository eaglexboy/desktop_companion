"""Rig maps (`blender/rig_maps/*.json`): skeletons that are mapped onto the Quaternius bones by data."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from blender import rigs

SHIPPED = sorted((Path(rigs.__file__).parent / "rig_maps").glob("*.json"))
FINGERS = [
    f"{f}{j}.{s}"
    for f in ("Pinky", "Ring", "Index", "Thumb")
    for j in (1, 2)
    for s in "LR"
]
MECH = (
    [
        "Body",
        "Torso",
        "Chest",
        "Neck",
        "Head",
        "PoleTarget.L",
        "Foot.L",
        "Foot.R",
    ]
    + [
        f"{b}.{s}"
        for b in ("Shoulder", "UpperArm", "LowerArm", "UpperLeg", "LowerLeg")
        for s in "LR"
    ]
    + FINGERS
)


def _write(tmp_path: Path, **changes) -> Path:
    data = {
        "format": 1,
        "name": "t",
        "signature": ["a"],
        "bones": {"a": "pelvis"},
        **changes,
    }
    path = tmp_path / "t.json"
    path.write_text(json.dumps(data))
    return path


def test_every_shipped_map_is_valid_and_maps_to_quaternius_names_once():
    assert {p.stem for p in SHIPPED} >= {
        "quaternius_mech_2leg",
        "quaternius_mech_3leg",
        "toon_basic",
        "rigify_basic",
        "valvebiped",
    }
    for path in SHIPPED:
        name, _to, signature, absent, _prune, _fold, spread = rigs.load_rig_map(
            path
        )
        data = json.loads(path.read_text())
        assert (
            name == path.stem
            and signature <= set(data["bones"])
            and not (absent & signature)
            and 0 <= spread <= rigs.MAX_ARM_SPREAD
        )
        targets = list(data["bones"].values())
        assert len(targets) == len(
            set(targets)
        ), f"{name} sends two bones to one Quaternius bone"
        assert {
            "pelvis",
            "thigh_l",
            "calf_l",
            "upperarm_l",
            "lowerarm_l",
        } <= set(targets)


def test_families_are_told_apart_by_signature_and_absent_bones():
    two_leg = [b for b in MECH]
    assert rigs.rig_family(two_leg) == "quaternius_mech_2leg"
    assert (
        rigs.rig_family([*two_leg, "MidLeg.L", "MidLeg.R"])
        == "quaternius_mech_3leg"
    )
    assert (
        rigs.rig_family(
            [
                b
                for b in MECH
                if not b.startswith(("Shoulder", "UpperArm", "LowerArm"))
            ]
        )
        is None
    )  # no arms
    toon = [
        "Root",
        "Body",
        "Hips",
        "Abdomen",
        "Torso",
        "Neck",
        "Head",
        "Shoulder.L",
        "UpperArm.L",
        "UpperLeg.L",
    ]
    assert rigs.rig_family(toon) == "toon_basic"
    rigify = ["Body", "upper_arm.L", "forearm.L", "thigh.L", "shin.L", "hand.L"]
    assert rigs.rig_family(rigify) == "rigify_basic"


def test_a_family_maps_its_bones_and_leaves_the_rest():
    names = [*MECH, "MidLeg.L", "MidLeg.R"]
    mapping = rigs.quaternius_names("quaternius_mech_3leg", names)
    assert (
        mapping["Torso"] == "pelvis"
        and mapping["UpperArm.L"] == "upperarm_l"
        and mapping["MidLeg.R"] == "calf_r"
    )
    assert mapping["Index2.L"] == "index_02_l"
    assert "PoleTarget.L" not in mapping and "Body" not in mapping


def test_the_three_leg_mech_folds_its_ik_feet_into_the_leg():
    """The feet of that mech are weighted to control bones the libraries do not drive: left alone they
    stay on the ground while the legs walk."""
    assert rigs.folded_bones("quaternius_mech_3leg") == {
        "Foot.L": "foot_l",
        "Foot.R": "foot_r",
    }
    assert rigs.folded_bones("quaternius_mech_2leg") == {}


def test_the_mechs_hold_their_arms_out_so_they_hang_clear_of_the_legs():
    assert rigs.arm_spread("quaternius_mech_2leg") > 0
    assert rigs.arm_spread("toon_basic") == 0 and rigs.arm_spread(None) == 0


@pytest.mark.parametrize(
    "changes, message",
    [
        ({"format": 2}, "format"),
        ({"name": ""}, "name"),
        ({"bones": {}}, "bones"),
        ({"bones": {"a": 3}}, "bones"),
        ({"signature": ["zzz"]}, "signature"),
        ({"absent": ["a"]}, "absent"),
        ({"prune": "yes"}, "prune"),
        ({"fold": {"Foot.L": "no_such_bone"}}, "fold"),
        ({"fold": {"a": "b"}}, "fold"),
        ({"fold": ["Foot.L"]}, "fold"),
        ({"arm_spread": -5}, "arm_spread"),
        ({"arm_spread": 90}, "arm_spread"),
        ({"arm_spread": "wide"}, "arm_spread"),
        ({"arm_spread": True}, "arm_spread"),
    ],
)
def test_an_invalid_rig_map_is_refused_saying_why(tmp_path, changes, message):
    with pytest.raises(rigs.RigMapError, match=message):
        rigs.load_rig_map(_write(tmp_path, **changes))


def test_an_unreadable_rig_map_is_refused(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("{")
    with pytest.raises(rigs.RigMapError, match="not readable"):
        rigs.load_rig_map(path)


def test_a_valvebiped_skeleton_is_a_family_with_its_extra_bones_left_alone():
    names = [
        "Pelvis",
        "ValveBiped.Bip01_Spine",
        "Spine1",
        "Spine2",
        "Spine3",
        "ValveBiped.Bip01_Head1",
        "TVRoot.L",
        "Sharp1.L",
    ]
    names += [
        f"{s}_{b}"
        for s in "RL"
        for b in (
            "Clavicle",
            "UpperArm",
            "Forearm",
            "Hand",
            "Thigh",
            "Calf",
            "Foot",
            "Toe",
        )
    ]
    names += ["ValveBiped.Bip01_R_Finger21", "ValveBiped.Bip01_L_Finger0"]
    assert rigs.rig_family(names) == "valvebiped"
    mapping = rigs.quaternius_names("valvebiped", names)
    assert (
        mapping["R_Forearm"] == "lowerarm_r"
        and mapping["L_Calf"] == "calf_l"
        and mapping["Spine1"] == "spine_02"
    )
    assert (
        mapping["ValveBiped.Bip01_R_Finger21"] == "middle_02_r"
        and mapping["ValveBiped.Bip01_L_Finger0"] == "thumb_01_l"
    )
    assert "TVRoot.L" not in mapping and "Spine3" not in mapping


def test_only_a_map_that_asks_for_it_prunes_its_extra_bones():
    assert rigs.prunes_extra_bones("valvebiped")
    assert not rigs.prunes_extra_bones(
        "toon_basic"
    ) and not rigs.prunes_extra_bones("character_creator")
