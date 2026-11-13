"""Tests for model bundles (brain/appearance/bundle.py): export, import, and what an import refuses."""

from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path

import pytest

from brain.appearance import bundle


def _deploy(root: Path) -> Path:
    """A deploy directory with a little of everything: models, sidecars, derived libraries, and the
    things a bundle must leave out."""
    files = {
        "assets/base_templates/standard_humanoid_female.glb": b"glb-female",
        "assets/base_templates/standard_humanoid_female.caps.json": b'{"bone_names": ["pelvis"]}',
        "assets/custom_templates/hero.glb": b"glb-hero",
        "assets/custom_templates/hero.caps.json": b"{}",
        "assets/custom_templates/hero.rig.json": b'{"retargeted": true}',
        "assets/custom_templates/hero.config.json": b'{"format": 1, "pose": {"face_yaw": 90}}',
        "assets/custom_templates/hero.credits.json": json.dumps(
            {
                "title": "Hero",
                "author": "Someone (https://x)",
                "license": "CC-BY-NC-4.0",
                "source": "https://x/hero",
                "notes": ["non-commercial use only"],
            }
        ).encode(),
        "assets/custom_templates/held.glb": b"glb-held",
        "assets/custom_templates/held.hold": b"too weird",
        "assets/outfit_models/standard_humanoid_female/ranger.glb": b"glb-ranger",
        "assets/wardrobe/socket_head_top/crown.glb": b"glb-crown",
        "assets/wardrobe/socket_head_top/crown.json": b'{"scale": 1.2}',
        "assets/animation_library/Pack.glb": b"glb-pack",
        "assets/animation_library_body/hero/Pack.glb": b"glb-pack-for-hero",
        # not models:
        "state.db": b"sqlite",
        "manifest.json": b"{}",
        "looks/look-v1.png": b"png",
        "assets/look-v1.glb": b"built look",
        "assets/catalogs/templates_catalog.json": b"{}",
        "assets/voices/voice.wav": b"wav",
        "tools/rhubarb": b"binary",
    }
    for rel, data in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return root


def _names(path: Path) -> set[str]:
    with zipfile.ZipFile(path) as z:
        return set(z.namelist())


def _rebuild(source: Path, target: Path, edit) -> None:
    """Copies a bundle, letting `edit(name, data, manifest)` change members and the manifest."""
    with zipfile.ZipFile(source) as src, zipfile.ZipFile(target, "w") as out:
        manifest = json.loads(src.read(bundle.MANIFEST))
        members = {
            n: src.read(n) for n in src.namelist() if n != bundle.MANIFEST
        }
        members = edit(members, manifest) or members
        for name, data in members.items():
            out.writestr(name, data)
        out.writestr(bundle.MANIFEST, json.dumps(manifest))


def test_a_bundle_holds_the_models_and_their_sidecars_and_nothing_else(
    tmp_path,
):
    deploy = _deploy(tmp_path / "deploy")
    report = bundle.export_bundle(deploy, tmp_path / "models.zip")

    names = _names(tmp_path / "models.zip")
    assert {
        "assets/base_templates/standard_humanoid_female.glb",
        "assets/custom_templates/hero.credits.json",
        "assets/custom_templates/hero.rig.json",
        "assets/custom_templates/hero.config.json",  # the model's settings travel with it
        "assets/outfit_models/standard_humanoid_female/ranger.glb",
        "assets/wardrobe/socket_head_top/crown.json",
        "assets/animation_library/Pack.glb",
        "assets/animation_library_body/hero/Pack.glb",
        bundle.MANIFEST,
        bundle.CREDITS,
    } <= names
    assert not any(
        n.startswith(("looks/", "tools/", "assets/voices/", "assets/catalogs/"))
        or n in ("state.db", "manifest.json", "assets/look-v1.glb")
        for n in names
    )
    assert (
        "assets/custom_templates/held.hold" not in names
    )  # state, not a model
    assert "assets/custom_templates/held.hold (state, not a model)" in " ".join(
        report.skipped
    ) or any("held.hold" in s for s in report.skipped)
    assert report.models["bodies"] == ["held", "hero"] and report.models[
        "outfits"
    ] == ["standard_humanoid_female/ranger"]
    with zipfile.ZipFile(tmp_path / "models.zip") as z:
        manifest = json.loads(z.read(bundle.MANIFEST))
        credits = z.read(bundle.CREDITS).decode()
    assert (
        manifest["format"] == bundle.FORMAT
        and manifest["derived_included"] is True
    )
    entry = manifest["files"]["assets/custom_templates/hero.glb"]
    assert entry == {
        "sha256": hashlib.sha256(b"glb-hero").hexdigest(),
        "size": 8,
    }
    assert "Hero by Someone" in credits and "non-commercial use only" in credits


def test_the_derived_libraries_can_be_left_out(tmp_path):
    deploy = _deploy(tmp_path / "deploy")
    bundle.export_bundle(deploy, tmp_path / "slim.zip", derived=False)
    assert not any(
        n.startswith("assets/animation_library_body/")
        for n in _names(tmp_path / "slim.zip")
    )
    fresh = tmp_path / "fresh"
    report = bundle.import_bundle(tmp_path / "slim.zip", fresh)
    assert report.derived_missing == [
        "hero"
    ]  # the converted body has none: it says so


def test_a_bundle_imported_into_an_empty_install_gives_the_same_files(tmp_path):
    deploy = _deploy(tmp_path / "deploy")
    bundle.export_bundle(deploy, tmp_path / "models.zip")
    fresh = tmp_path / "fresh"

    report = bundle.import_bundle(tmp_path / "models.zip", fresh)

    assert (
        report.conflicts == []
        and report.derived_missing == []
        and len(report.added) == 13
    )
    for name in report.manifest["files"]:
        assert (fresh / name).read_bytes() == (deploy / name).read_bytes()
    assert not (fresh / "state.db").exists() and not list(
        (fresh / "assets").glob(".import-*")
    )
    assert (
        "hero: non-commercial use only" in report.notes
    )  # what the licences ask is told on import
    again = bundle.import_bundle(tmp_path / "models.zip", fresh)
    assert (
        again.added == [] and len(again.unchanged) == 13
    )  # a second import changes nothing


def test_a_model_already_there_is_kept_unless_told_to_replace_it(tmp_path):
    deploy = _deploy(tmp_path / "deploy")
    bundle.export_bundle(deploy, tmp_path / "models.zip")
    fresh = tmp_path / "fresh"
    (fresh / "assets" / "custom_templates").mkdir(parents=True)
    (fresh / "assets" / "custom_templates" / "hero.glb").write_bytes(
        b"my own hero"
    )

    kept = bundle.import_bundle(tmp_path / "models.zip", fresh)
    assert kept.conflicts == ["assets/custom_templates/hero.glb"]
    assert (
        fresh / "assets" / "custom_templates" / "hero.glb"
    ).read_bytes() == b"my own hero"

    replaced = bundle.import_bundle(
        tmp_path / "models.zip", fresh, replace=True
    )
    assert (
        replaced.replaced == ["assets/custom_templates/hero.glb"]
        and replaced.conflicts == []
    )
    assert (
        fresh / "assets" / "custom_templates" / "hero.glb"
    ).read_bytes() == b"glb-hero"


def test_a_name_that_differs_only_in_case_from_one_already_there_is_a_conflict(
    tmp_path,
):
    deploy = _deploy(tmp_path / "deploy")
    bundle.export_bundle(deploy, tmp_path / "models.zip")
    fresh = tmp_path / "fresh"
    (fresh / "assets" / "custom_templates").mkdir(parents=True)
    (fresh / "assets" / "custom_templates" / "Hero.glb").write_bytes(b"x")
    report = bundle.import_bundle(tmp_path / "models.zip", fresh)
    assert any("hero.glb" in c and "Hero.glb" in c for c in report.conflicts)


def test_a_dry_run_checks_the_bundle_and_writes_nothing(tmp_path):
    deploy = _deploy(tmp_path / "deploy")
    bundle.export_bundle(deploy, tmp_path / "models.zip")
    fresh = tmp_path / "fresh"
    report = bundle.import_bundle(tmp_path / "models.zip", fresh, dry_run=True)
    assert len(report.added) == 13 and not fresh.exists()


def test_names_windows_cannot_keep_stop_an_export(tmp_path):
    deploy = _deploy(tmp_path / "deploy")
    (deploy / "assets" / "custom_templates" / "CON.glb").write_bytes(b"x")
    with pytest.raises(bundle.BundleError, match="reserved name"):
        bundle.export_bundle(deploy, tmp_path / "a.zip")
    assert not (tmp_path / "a.zip").exists()
    (deploy / "assets" / "custom_templates" / "CON.glb").unlink()

    (deploy / "assets" / "custom_templates" / "HERO.glb").write_bytes(
        b"x"
    )  # next to hero.glb
    with pytest.raises(bundle.BundleError, match="upper and lower case"):
        bundle.export_bundle(deploy, tmp_path / "b.zip")
    (deploy / "assets" / "custom_templates" / "HERO.glb").unlink()

    (deploy / "assets" / "custom_templates" / ("x" * 160 + ".glb")).write_bytes(
        b"x"
    )
    with pytest.raises(bundle.BundleError, match="too long"):
        bundle.export_bundle(deploy, tmp_path / "c.zip")


def test_an_install_with_no_models_has_nothing_to_export(tmp_path):
    with pytest.raises(bundle.BundleError, match="does not exist"):
        bundle.export_bundle(tmp_path / "none", tmp_path / "x.zip")
    (tmp_path / "empty" / "assets").mkdir(parents=True)
    with pytest.raises(bundle.BundleError, match="no models"):
        bundle.export_bundle(tmp_path / "empty", tmp_path / "x.zip")
    assert not (tmp_path / "x.zip").exists()


def test_a_changed_catalog_is_reported_not_exported(tmp_path):
    deploy = _deploy(tmp_path / "deploy")
    seed = tmp_path / "seed"
    seed.mkdir()
    (seed / "templates_catalog.json").write_text('{"different": 1}')
    report = bundle.export_bundle(
        deploy, tmp_path / "models.zip", catalogs_seed=seed
    )
    assert any("templates_catalog.json differs" in w for w in report.warnings)


# --- what an import refuses ---------------------------------------------------------------------


def _bundle(tmp_path: Path) -> Path:
    bundle.export_bundle(_deploy(tmp_path / "deploy"), tmp_path / "good.zip")
    return tmp_path / "good.zip"


def test_a_file_that_was_altered_is_found_by_its_hash_and_nothing_is_written(
    tmp_path,
):
    good = _bundle(tmp_path)
    _rebuild(
        good,
        tmp_path / "bad.zip",
        lambda members, manifest: {
            **members,
            "assets/custom_templates/hero.glb": b"glb-HERO",
        },
    )
    fresh = tmp_path / "fresh"
    with pytest.raises(bundle.BundleError, match="do not match the hash"):
        bundle.import_bundle(tmp_path / "bad.zip", fresh)
    assert not list(fresh.rglob("*.glb"))  # not even the files before it
    with pytest.raises(bundle.BundleError, match="do not match the hash"):
        bundle.import_bundle(
            tmp_path / "bad.zip", tmp_path / "other", dry_run=True
        )


@pytest.mark.parametrize(
    "name",
    [
        "../evil.glb",
        "assets/custom_templates/../../evil.glb",
        "/etc/evil.glb",
        "assets\\custom_templates\\evil.glb",
        "C:/evil.glb",
        "assets/custom_templates/evil.exe",
        "assets/catalogs/evil.json",
        "state.db",
        "assets/custom_templates/x.glb/../../../y.glb",
    ],
)
def test_a_name_that_could_land_outside_the_models_is_refused(tmp_path, name):
    good = _bundle(tmp_path)

    def edit(members, manifest):
        members[name] = b"evil"
        manifest["files"][name] = {
            "sha256": hashlib.sha256(b"evil").hexdigest(),
            "size": 4,
        }

    _rebuild(good, tmp_path / "evil.zip", edit)
    fresh = tmp_path / "fresh"
    with pytest.raises(bundle.BundleError):
        bundle.import_bundle(tmp_path / "evil.zip", fresh)
    assert not fresh.exists() and not (tmp_path / "evil.glb").exists()


def test_a_file_the_manifest_does_not_list_is_refused(tmp_path):
    good = _bundle(tmp_path)
    _rebuild(
        good,
        tmp_path / "extra.zip",
        lambda members, manifest: {
            **members,
            "assets/custom_templates/extra.glb": b"x",
        },
    )
    with pytest.raises(bundle.BundleError, match="does not list"):
        bundle.import_bundle(tmp_path / "extra.zip", tmp_path / "fresh")


def test_a_file_the_manifest_lists_but_the_bundle_lacks_is_refused(tmp_path):
    good = _bundle(tmp_path)
    _rebuild(
        good,
        tmp_path / "short.zip",
        lambda members, manifest: {
            k: v
            for k, v in members.items()
            if k != "assets/custom_templates/hero.glb"
        },
    )
    with pytest.raises(bundle.BundleError, match="not in the bundle"):
        bundle.import_bundle(tmp_path / "short.zip", tmp_path / "fresh")


def test_a_bundle_of_a_newer_format_or_no_bundle_at_all_is_refused(tmp_path):
    good = _bundle(tmp_path)

    def newer(members, manifest):
        manifest["format"] = bundle.FORMAT + 1

    _rebuild(good, tmp_path / "newer.zip", newer)
    with pytest.raises(bundle.BundleError, match="update it"):
        bundle.import_bundle(tmp_path / "newer.zip", tmp_path / "fresh")

    plain = tmp_path / "plain.zip"
    with zipfile.ZipFile(plain, "w") as z:
        z.writestr("hello.txt", "hi")
    with pytest.raises(bundle.BundleError, match="not a model bundle"):
        bundle.import_bundle(plain, tmp_path / "fresh")
    (tmp_path / "junk.zip").write_bytes(b"not a zip")
    with pytest.raises(bundle.BundleError, match="cannot open"):
        bundle.import_bundle(tmp_path / "junk.zip", tmp_path / "fresh")


def test_a_bundle_that_unpacks_to_far_too_much_is_refused(
    tmp_path, monkeypatch
):
    good = _bundle(tmp_path)
    monkeypatch.setattr(bundle, "MAX_UNPACKED_BYTES", 10)
    with pytest.raises(bundle.BundleError, match="GB allowed"):
        bundle.import_bundle(good, tmp_path / "fresh")


def test_a_size_that_lies_is_refused(tmp_path):
    good = _bundle(tmp_path)

    def edit(members, manifest):
        manifest["files"]["assets/custom_templates/hero.glb"]["size"] = 9999

    _rebuild(good, tmp_path / "lies.zip", edit)
    with pytest.raises(bundle.BundleError, match="the manifest says 9999"):
        bundle.import_bundle(tmp_path / "lies.zip", tmp_path / "fresh")


# --- carrying a finished look --------------------------------------------------------------------


def _with_looks(deploy: Path) -> Path:
    """Two published looks (versions 1 and 2, the second current), the first one's file pruned."""
    from brain.appearance.manifest import append_version, save_manifest

    manifest = None
    for version, data in ((1, b"look one"), (2, b"look two")):
        build = {
            "glb_filename": "look.glb",
            "sha256": hashlib.sha256(data).hexdigest(),
            "created_at": 1000.0 + version,
            "status": "ok",
            "seed": version,
            "evaluated_triangles": 100,
            "size_bytes": len(data),
            "has_face": False,
            "speech_indicator_mode": "talk_pulse",
            "capabilities": {"has_head": True},
            "viseme_shape_keys": [],
            "library_clips": {"idle": "Idle_Loop"},
            "animation_clips": [],
            "bounds": {"height": 1.7},
            "warnings": [],
        }
        manifest = append_version(
            manifest, build, {"base_template": "custom:hero", "seed": version}
        )
        (deploy / "assets").mkdir(parents=True, exist_ok=True)
        if version == 2:
            (deploy / "assets" / f"look-v{version}.glb").write_bytes(
                data
            )  # version 1's file was pruned
    assert manifest is not None
    save_manifest(deploy, manifest)
    (deploy / "assets" / "look-v1.glb").unlink(
        missing_ok=True
    )  # (the fixture's stands for a built look that is not a record)
    (deploy / "looks").mkdir(exist_ok=True)
    (deploy / "looks" / "look-v2.png").write_bytes(b"picture")
    return deploy


def test_an_install_with_no_looks_starts_with_the_look_a_bundle_carries_as_version_1(
    tmp_path,
):
    from brain.appearance.manifest import load_manifest

    deploy = _with_looks(_deploy(tmp_path / "deploy"))
    report = bundle.export_bundle(
        deploy, tmp_path / "models.zip", include_look=True
    )
    assert report.look == 2
    fresh = tmp_path / "fresh"

    imported = bundle.import_bundle(tmp_path / "models.zip", fresh)

    assert (
        "version 1 and the current look" in imported.look
        and "was version 2" in imported.look
    )
    manifest = load_manifest(fresh)
    assert (
        manifest is not None
        and manifest.current_version == 1
        and [v.version for v in manifest.versions] == [1]
    )
    entry = manifest.current()
    assert (
        entry is not None
        and entry.glb_filename == "look-v1.glb"
        and entry.look_spec == {"base_template": "custom:hero", "seed": 2}
    )
    assert (
        entry.library_clips == {"idle": "Idle_Loop"}
        and entry.sha256 == hashlib.sha256(b"look two").hexdigest()
    )
    assert (
        fresh / "assets" / "look-v1.glb"
    ).read_bytes() == b"look two"  # where a publish puts it, in both places
    assert (
        fresh / "data" / "current" / "look-v1.glb"
    ).read_bytes() == b"look two"
    assert (fresh / "looks" / "look-v1.png").read_bytes() == b"picture"
    assert not (
        fresh / "look"
    ).exists()  # the carried files are not unpacked as they are
    assert (
        fresh / "assets" / "custom_templates" / "hero.glb"
    ).read_bytes() == b"glb-hero"  # and the models it needs are there
    assert len(imported.added) == 13  # the look is not counted as a model


def test_an_install_that_already_has_looks_is_left_alone(tmp_path):
    deploy = _with_looks(_deploy(tmp_path / "deploy"))
    bundle.export_bundle(deploy, tmp_path / "models.zip", include_look=True)
    other = _with_looks(_deploy(tmp_path / "other"))
    before = (other / "manifest.json").read_bytes()

    imported = bundle.import_bundle(tmp_path / "models.zip", other)

    assert (
        "not used" in imported.look
        and (other / "manifest.json").read_bytes() == before
    )
    assert not (other / "data").exists()


def test_a_dry_run_says_what_would_happen_to_the_look_and_writes_nothing(
    tmp_path,
):
    deploy = _with_looks(_deploy(tmp_path / "deploy"))
    bundle.export_bundle(deploy, tmp_path / "models.zip", include_look=True)
    fresh = tmp_path / "fresh"
    report = bundle.import_bundle(tmp_path / "models.zip", fresh, dry_run=True)
    assert "version 1" in report.look and not fresh.exists()


def test_a_bundle_without_a_look_has_none_to_set_up(tmp_path):
    deploy = _with_looks(_deploy(tmp_path / "deploy"))
    bundle.export_bundle(deploy, tmp_path / "models.zip")
    assert (
        bundle.import_bundle(tmp_path / "models.zip", tmp_path / "fresh").look
        == ""
    )
    assert not (tmp_path / "fresh" / "manifest.json").exists()


def test_a_look_that_cannot_be_carried_stops_the_export(tmp_path):
    deploy = _deploy(tmp_path / "deploy")
    (
        deploy / "manifest.json"
    ).unlink()  # (the fixture's stands for a state file)
    with pytest.raises(bundle.BundleError, match="no looks yet"):
        bundle.export_bundle(deploy, tmp_path / "a.zip", include_look=True)
    _with_looks(deploy)
    with pytest.raises(bundle.BundleError, match="is not kept"):
        bundle.export_bundle(
            deploy, tmp_path / "b.zip", include_look=True, look_version=1
        )  # its file was pruned
    with pytest.raises(bundle.BundleError, match="no look version 9"):
        bundle.export_bundle(
            deploy, tmp_path / "c.zip", include_look=True, look_version=9
        )
    (deploy / "assets" / "look-v2.glb").write_bytes(b"damaged")
    with pytest.raises(bundle.BundleError, match="does not match its record"):
        bundle.export_bundle(deploy, tmp_path / "d.zip", include_look=True)
    assert not list(tmp_path.glob("*.zip"))


def test_a_look_whose_file_is_not_the_one_its_record_describes_is_refused_and_nothing_is_written(
    tmp_path,
):
    deploy = _with_looks(_deploy(tmp_path / "deploy"))
    bundle.export_bundle(deploy, tmp_path / "models.zip", include_look=True)

    def swap(members, manifest):
        members["look/look.glb"] = b"another look"
        manifest["files"]["look/look.glb"] = {
            "sha256": hashlib.sha256(b"another look").hexdigest(),
            "size": 12,
        }

    _rebuild(tmp_path / "models.zip", tmp_path / "swapped.zip", swap)
    fresh = tmp_path / "fresh"
    with pytest.raises(
        bundle.BundleError, match="not the one its record describes"
    ):
        bundle.import_bundle(tmp_path / "swapped.zip", fresh)
    assert not fresh.exists()


def test_a_record_that_is_not_a_look_is_refused(tmp_path):
    deploy = _with_looks(_deploy(tmp_path / "deploy"))
    bundle.export_bundle(deploy, tmp_path / "models.zip", include_look=True)

    def junk(members, manifest):
        members["look/entry.json"] = b'{"nonsense": true}'
        manifest["files"]["look/entry.json"] = {
            "sha256": hashlib.sha256(b'{"nonsense": true}').hexdigest(),
            "size": 18,
        }

    _rebuild(tmp_path / "models.zip", tmp_path / "junk.zip", junk)
    with pytest.raises(bundle.BundleError, match="not a look record"):
        bundle.import_bundle(tmp_path / "junk.zip", tmp_path / "fresh")


def test_only_the_three_look_files_may_be_in_a_look_folder(tmp_path):
    deploy = _with_looks(_deploy(tmp_path / "deploy"))
    bundle.export_bundle(deploy, tmp_path / "models.zip", include_look=True)

    def extra(members, manifest):
        members["look/evil.glb"] = b"x"
        manifest["files"]["look/evil.glb"] = {
            "sha256": hashlib.sha256(b"x").hexdigest(),
            "size": 1,
        }

    _rebuild(tmp_path / "models.zip", tmp_path / "extra.zip", extra)
    with pytest.raises(bundle.BundleError, match="not under a model folder"):
        bundle.import_bundle(tmp_path / "extra.zip", tmp_path / "fresh")
