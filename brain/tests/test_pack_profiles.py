"""Tests for animation pack profiles: which clips fill which role and how clips are converted is data
(blender/pack_profiles.py), shipped in blender/packs/ and layered with the user's, so nothing in the
engine depends on a particular pack's clip names."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from blender import animation_library as al
from blender import pack_profiles as pp
from brain.appearance import bundle, models


def profile(name: str = "demo", **sections) -> dict:
    return {"format": 1, "name": name, **sections}


# --- what ships ---------------------------------------------------------------------------------


@pytest.mark.real_profiles
def test_the_only_shipped_profile_is_the_supported_source():
    shipped, problems = pp.shipped_profiles()
    assert problems == []
    assert [p.name for p in shipped] == [
        "quaternius"
    ]  # every other pack is the user's, in their deploy directory
    roles = {
        r for p in shipped for sec in ("roles", "extras") for r in p.data.get(sec, {})
    }
    assert roles <= pp.KNOWN_ROLES


def test_with_only_the_supported_source_the_base_roles_are_all_there_is():
    with pp.use_profiles([json.loads((pp.PACKS_DIR / "quaternius.json").read_text())]):
        assert set(al.library_roles()) == {
            "idle",
            "talk",
            "run",
            "walk",
            "idle_alt",
            "celebrate",
        }
        assert al.library_extras() == {}
        assert al.mixamo_derived_clips({"Stand_Up", "Burpee"}) == []  # no recipe for it
        assert al.chair_sit_clips() == {
            "Sitting_Enter",
            "Sitting_Idle_Loop",
            "Sitting_Exit",
        }
        assert al.library_time_scale()["idle"] == 2.5


# --- validation ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ({"format": 2, "name": "x"}, "format must be 1"),
        (profile("a b"), "name:"),
        (profile(banana=1), "unknown section 'banana'"),
        (profile(roles={"dance_party": ["X"]}), "'dance_party' is not a role"),
        (profile(roles={"greet": "Wave"}), "a list of clip names"),
        (profile(extras={"greet": [""]}), "a list of clip names"),
        (
            profile(clips=[{"match": "X", "options": {"loopp": True}}]),
            "unknown option 'loopp'",
        ),
        (profile(clips=[{"match": "X"}]), "options (an object)"),
        (profile(clips=[{"options": {}}]), "match"),
        (
            profile(clips=[{"match": "X", "options": {"extract": [{"trim": [1, 2]}]}}]),
            "extract needs a name",
        ),
        (
            profile(
                clips=[
                    {"match": "X", "options": {"extract": [{"name": "Y", "speed": 2}]}}
                ]
            ),
            "unknown option 'speed'",
        ),
        (
            profile(derived=[{"name": "D", "options": {"loop": True}}]),
            "composed or held",
        ),
        (
            profile(
                derived=[
                    {"name": "D", "options": {"compose": ["A"]}},
                    {"name": "D", "options": {"hold": "A"}},
                ]
            ),
            "made twice",
        ),
        (
            profile(derived=[{"name": "D", "options": {"compose": [["A", 0]]}}]),
            "[clip, repeat]",
        ),
        (
            profile(sit_sets=[{"clips": {"sit_enter": ["A"]}}]),
            "exactly sit_enter, sit_idle, sit_exit",
        ),
        (profile(time_scale={"idle": 0}), "above 0"),
        (profile(time_scale={"nap": 1.0}), "'nap' is not a role"),
        (profile(disable="Bow"), "a list of clip names"),
    ],
)
def test_a_profile_that_is_wrong_says_what_is_wrong(data, message):
    with pytest.raises(pp.ProfileError, match=re.escape(message)):
        pp.validate(data)


def test_every_problem_is_listed_at_once():
    with pytest.raises(pp.ProfileError) as info:
        pp.validate(
            profile(
                roles={"nope": ["A"]}, clips=[{"match": "X", "options": {"bad": 1}}]
            )
        )
    text = str(info.value)
    assert "'nope' is not a role" in text and "unknown option 'bad'" in text


# --- the engine, with names that belong to no pack ---------------------------------------------


def test_roles_options_and_conversion_follow_the_profiles_not_the_code():
    mine = profile(
        roles={"greet": ["Hello_One"], "sleep": ["Nap_Loop"]},
        extras={"greet": ["Hello_Two"]},
        clips=[
            {"match": "Hello*", "options": {"variants": True, "blend_ref": "@stand"}},
            {"match": "Hello_Two", "options": {"variants": False}},
            {"match": ["Nap_Loop"], "options": {"loop": True, "mirror": False}},
        ],
        derived=[
            {
                "name": "Nap_Down",
                "requires": ["Nap_Loop", "Get_Up"],
                "options": {"compose": ["Get_Up", "Nap_Loop"], "ease": 6},
            }
        ],
    )
    with pp.use_profiles([mine]):
        picked = al.pick_clips(
            {"Hello_One", "Hello_Two", "Nap_Loop", "Standing_Greeting"}
        )
        assert picked == {
            "greet": "Hello_One",
            "greet.alt1": "Hello_Two",
            "sleep": "Nap_Loop",
        }
        assert al.mixamo_clip_defaults("Hello_One")["variants"] is True
        assert (
            al.mixamo_clip_defaults("Hello_Two")["variants"] is False
        )  # a later rule wins
        assert al.mixamo_clip_defaults("Hello_Two")["blend_ref"] == "@stand"
        nap = al.mixamo_clip_defaults("Nap_Loop")
        assert nap["loop"] and not nap["mirror"] and nap["extract"] == []
        assert al.mixamo_clip_defaults("Something_Else") == pp.BASE_OPTIONS
        assert [d["name"] for d in al.mixamo_derived_clips({"Nap_Loop", "Get_Up"})] == [
            "Nap_Down"
        ]
        assert (
            al.mixamo_derived_clips({"Nap_Loop"}) == []
        )  # a recipe needs every clip it names


def test_a_clip_that_is_a_roles_own_clip_and_an_extra_is_one_option_not_two():
    shipped = profile(
        "shipped",
        roles={"workout": ["Crunch_Workout"]},
        extras={"workout": ["Pushup_Workout"]},
    )
    mine = profile(
        "mine", roles={"workout": ["Pushup_Workout"]}
    )  # the user prefers the other
    both = {"Crunch_Workout", "Pushup_Workout"}
    with pp.use_profiles([shipped, mine]):
        # candidates are alternatives (the first there is the role's clip), extras are the options
        assert al.pick_clips(both) == {"workout": "Pushup_Workout"}
    keep = profile(
        "keep",
        roles={"workout": ["Pushup_Workout"]},
        extras={"workout": ["Crunch_Workout"]},
    )
    with pp.use_profiles([shipped, keep]):
        picked = al.pick_clips(both)
        assert picked == {"workout": "Pushup_Workout", "workout.alt1": "Crunch_Workout"}


def test_a_clip_options_copy_can_be_changed_without_changing_the_profile():
    with pp.use_profiles(
        [profile(clips=[{"match": "X", "options": {"extract": [{"name": "Y"}]}}])]
    ):
        first = al.mixamo_clip_defaults("X")
        first["extract"].append({"name": "Z"})
        assert al.mixamo_clip_defaults("X")["extract"] == [{"name": "Y"}]


def test_profiles_are_layered_the_last_ones_preferences_first():
    shipped = profile(
        "shipped",
        roles={"greet": ["Shipped_Wave"], "angry": ["Shipped_Angry"]},
        extras={"greet": ["Shipped_Bow"]},
        clips=[{"match": "*", "options": {"mirror": False}}],
        sit_sets=[
            {
                "chair": True,
                "clips": {
                    "sit_enter": ["S_In"],
                    "sit_idle": ["S_Idle"],
                    "sit_exit": ["S_Out"],
                },
            }
        ],
        time_scale={"idle": 2.5},
        derived=[{"name": "D", "requires": ["A"], "options": {"compose": ["A"]}}],
    )
    mine = profile(
        "mine",
        roles={"greet": ["My_Wave"]},
        extras={"greet": ["My_Bow"]},
        clips=[{"match": "Keep*", "options": {"mirror": True}}],
        sit_sets=[
            {
                "clips": {
                    "sit_enter": ["M_In"],
                    "sit_idle": ["M_Idle"],
                    "sit_exit": ["M_Out"],
                }
            }
        ],
        time_scale={"idle": 1.5, "sit_idle": 3},
        derived=[{"name": "D", "requires": ["B"], "options": {"hold": "B"}}],
        disable=["Shipped_Bow"],
    )
    with pp.use_profiles([shipped, mine]):
        assert al.library_roles()["greet"] == (
            "My_Wave",
            "Shipped_Wave",
        )  # the user's choice first
        assert al.library_roles()["angry"] == ("Shipped_Angry",)
        assert al.library_extras()["greet"] == ("My_Bow",)  # disabled in every role
        assert al.mixamo_clip_defaults("Other")["mirror"] is False
        assert (
            al.mixamo_clip_defaults("Keep_This")["mirror"] is True
        )  # the user's rule is last: it wins
        assert al.library_sit_sets()[0]["sit_idle"] == (
            "M_Idle",
        )  # the user's way of sitting first
        assert al.chair_sit_clips() == {"S_In", "S_Idle", "S_Out"}
        assert al.library_time_scale() == {"idle": 1.5, "sit_idle": 3.0}
        derived = al.mixamo_derived_clips({"A", "B"})
        assert [(d["name"], d["options"]) for d in derived] == [
            ("D", {"hold": "B"})
        ]  # replaced by name
        assert al.mixamo_derived_clips({"A"}) == []


# --- the user's profiles ------------------------------------------------------------------------


def write_profile(deploy: Path, name: str, data) -> Path:
    path = deploy.joinpath(*pp.USER_PROFILES_DIR) / f"{name}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data if isinstance(data, str) else json.dumps(data))
    return path


@pytest.mark.real_profiles
def test_the_users_profiles_are_read_from_the_deploy_directory(tmp_path):
    write_profile(tmp_path, "mine", profile("mine", roles={"greet": ["My_Wave"]}))
    try:
        pp.configure(tmp_path)
        assert al.pick_clips({"My_Wave"}) == {"greet": "My_Wave"}
        assert {p.name for p in pp.merged().profiles} >= {"quaternius", "mine"}
        info = models.profiles_in_use(tmp_path)
        assert {e["name"]: e["user"] for e in info["profiles"]}["mine"] is True
        assert {e["name"]: e["user"] for e in info["profiles"]}["quaternius"] is False
        assert info["problems"] == []
    finally:
        pp.configure(None)
    assert al.pick_clips({"My_Wave"}) == {}  # not this install's: nothing uses it


@pytest.mark.real_profiles
def test_a_broken_profile_is_skipped_and_the_rest_still_work(tmp_path, capsys):
    write_profile(
        tmp_path, "broken", '{"format": 1, "name": "broken", "roles": {"nope": ["A"]}'
    )
    write_profile(tmp_path, "typo", profile("typo", roles={"nope": ["A"]}))
    write_profile(tmp_path, "good", profile("good", roles={"greet": ["Good_Wave"]}))
    try:
        pp.configure(tmp_path)
        assert al.pick_clips({"Good_Wave"}) == {"greet": "Good_Wave"}
        assert al.pick_clips({"Idle_Loop"}) == {
            "idle": "Idle_Loop"
        }  # the shipped ones too
        problems = pp.merged().problems
        assert len(problems) == 2 and any("cannot be read" in p for p in problems)
        assert any("'nope' is not a role" in p for p in problems)
        assert capsys.readouterr().err.count("animation profile skipped") == 2
    finally:
        pp.configure(None)


@pytest.mark.real_profiles
def test_a_profile_that_changes_is_read_again(tmp_path):
    path = write_profile(tmp_path, "mine", profile("mine", roles={"greet": ["One"]}))
    try:
        pp.configure(tmp_path)
        assert al.library_roles()["greet"][0] == "One"
        path.write_text(json.dumps(profile("mine", roles={"greet": ["Two", "Three"]})))
        assert al.library_roles()["greet"][:2] == ("Two", "Three")
        path.unlink()
        assert (
            "greet" not in al.library_roles() or al.library_roles()["greet"][0] != "Two"
        )
    finally:
        pp.configure(None)


# --- installing and carrying --------------------------------------------------------------------


def test_add_profile_checks_it_and_will_not_overwrite_without_being_told(tmp_path):
    deploy = tmp_path / "deploy"
    source = tmp_path / "mine.json"
    source.write_text(json.dumps(profile("mine", roles={"greet": ["My_Wave"]})))
    dest = models.add_profile(deploy, source)
    assert dest == deploy / "assets" / "animation_profiles" / "mine.json"
    assert json.loads(dest.read_text())["roles"] == {"greet": ["My_Wave"]}
    with pytest.raises(SystemExit, match="already installed"):
        models.add_profile(deploy, source)
    source.write_text(json.dumps(profile("mine", roles={"greet": ["Other"]})))
    models.add_profile(deploy, source, replace=True)
    assert json.loads(dest.read_text())["roles"] == {"greet": ["Other"]}
    source.write_text(json.dumps(profile("bad", roles={"nope": ["A"]})))
    with pytest.raises(SystemExit, match="'nope' is not a role"):
        models.add_profile(deploy, source)
    assert not (deploy / "assets" / "animation_profiles" / "bad.json").exists()


def test_the_users_profiles_go_in_a_bundle_with_the_models(tmp_path):
    source = tmp_path / "a"
    write_profile(source, "mine", profile("mine", roles={"greet": ["My_Wave"]}))
    models_dir = source / "assets" / "custom_templates"
    models_dir.mkdir(parents=True)
    (models_dir / "hero.glb").write_bytes(b"glb")
    out = tmp_path / "bundle.zip"
    bundle.export_bundle(source, out)
    target = tmp_path / "b"
    target.mkdir()
    bundle.import_bundle(out, target)
    copied = target / "assets" / "animation_profiles" / "mine.json"
    assert json.loads(copied.read_text())["roles"] == {"greet": ["My_Wave"]}
