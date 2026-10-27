"""deploy/bump_version.py: the release-version arithmetic the PR workflow relies on."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location("bump_version", Path(__file__).resolve().parents[2] / "deploy" / "bump_version.py")
assert _spec is not None and _spec.loader is not None
bv = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bv)


def test_latest_tag_is_numeric_not_alphabetical_and_ignores_other_tags():
    assert bv.latest_tag(["v1.0.2", "v1.0.10", "v1.0.9", "nightly", "v2.0", "release-1"]) == (1, 0, 10)
    assert bv.latest_tag([]) == (0, 0, 0)


@pytest.mark.parametrize("kind,expected", [("patch", "1.0.3"), ("minor", "1.1.0"), ("major", "2.0.0")])
def test_bump_kinds(kind, expected):
    assert bv.bump((1, 0, 2), kind) == expected


def test_bumping_is_idempotent_because_the_base_is_the_tag_not_the_file():
    tags = ["v1.0.2"]
    assert bv.bump(bv.latest_tag(tags), "minor") == bv.bump(bv.latest_tag(tags), "minor") == "1.1.0"


def test_write_version_changes_only_the_project_version_line():
    text = '[project]\nname = "x"\nversion = "0.1.0"\n[tool.other]\nversion = "9"\n'
    out = bv.write_version(text, "1.1.0")
    assert 'version = "1.1.0"' in out and 'version = "9"' in out and out.count("1.1.0") == 1
    with pytest.raises(ValueError):
        bv.write_version("[project]\nname = 'x'\n", "1.0.0")


@pytest.mark.parametrize(
    "title,labels,body,expected",
    [
        ("feat: skills standard", "", "", "minor"),
        ("feat(skills): importer", "", "", "minor"),
        ("fix: pin the cap", "", "", "patch"),
        ("perf: faster index", "", "", "patch"),
        ("feat!: new state schema", "", "", "major"),
        ("refactor(api)!: drop old route", "", "", "major"),          # `!` is breaking whatever the type
        ("fix: tidy", "", "Details\n\nBREAKING CHANGE: state moved", "major"),
        ("fix: tidy", "", "Details\n\nBREAKING-CHANGE: state moved", "major"),
        ("feat: x", "release:major", "", "minor"),                    # a conventional title wins over a label
        ("docs: update ADR", "release:minor", "", "minor"),           # non-releasing type: the label decides
        ("chore: tidy", "", "", "patch"),                             # ...and with no label, patch
        ("Skills standard", "release:minor", "", "minor"),            # not conventional: the label decides
        ("Skills standard", "bug, release:patch ,release:major", "", "major"),  # several labels: the highest
        ("Skills standard", "", "BREAKING CHANGE: ignored", "patch"), # a footer only counts with a conventional title
        ("", "", "", "patch"),
        ("Feat: wrong case", "", "", "patch"),                        # python-semantic-release is case-sensitive too
        ("feat:no space", "", "", "patch"),
        ("feature: x", "", "", "patch"),
    ],
)
def test_choose_kind_follows_conventional_commits_then_labels_then_patch(title, labels, body, expected):
    assert bv.choose_kind(title, labels, body) == expected
