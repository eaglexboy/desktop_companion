#!/usr/bin/env python3
"""Works out the next release version from the latest `vX.Y.Z` git tag and
writes it into pyproject.toml.

    deploy/bump_version.py --bump minor            # print the next version
    deploy/bump_version.py --bump minor --write    # ...and update pyproject.toml
    deploy/bump_version.py --title "feat: skills" --labels "release:patch" --write   # the PR way

The base is always the latest *tag*, never the number currently in the file, so
running it twice (a PR re-labelled, a workflow re-run) gives the same answer
instead of bumping again. Used by .github/workflows/version-bump.yml.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

PYPROJECT = Path(__file__).resolve().parent.parent / "pyproject.toml"
_TAG = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")
# Conventional Commits, as python-semantic-release parses them by default:
#   feat -> minor; fix, perf -> patch; `type!:` or a BREAKING CHANGE footer -> major.
# Other types (docs, chore, ci, refactor, test, style, build) do not release there.
_CONVENTIONAL = re.compile(r"^(?P<type>[a-z]+)(?:\([^)\n]*\))?(?P<bang>!)?: \S")
_BREAKING_FOOTER = re.compile(r"^BREAKING[ -]CHANGE: \S", re.MULTILINE)
_RELEASING = {"feat": "minor", "fix": "patch", "perf": "patch"}
_VERSION_LINE = re.compile(r'^(version\s*=\s*)"[^"]*"', re.MULTILINE)


def latest_tag(tags: list[str]) -> tuple[int, int, int]:
    found = [tuple(int(g) for g in m.groups()) for t in tags if (m := _TAG.match(t.strip()))]
    return max(found) if found else (0, 0, 0)  # type: ignore[return-value]


def bump(version: tuple[int, int, int], kind: str) -> str:
    major, minor, patch = version
    if kind == "major":
        return f"{major + 1}.0.0"
    if kind == "minor":
        return f"{major}.{minor + 1}.0"
    if kind == "patch":
        return f"{major}.{minor}.{patch + 1}"
    raise ValueError(f"unknown bump {kind!r}")


def choose_kind(title: str = "", labels: str = "", body: str = "") -> str:
    """The bump a PR asks for.

    1. The title, written as a Conventional Commit, because that is what a squash
       merge turns into the commit message and what python-semantic-release reads:
       `feat:` minor, `fix:`/`perf:` patch, `type!:` or a `BREAKING CHANGE:` line in
       the body major. Types that do not release there (docs, chore, ...) fall through.
    2. A `release:major|minor|patch` label (the highest, if several).
    3. Otherwise patch: every merged PR gets a version here.
    """
    m = _CONVENTIONAL.match(title.strip())
    if m and (m.group("bang") or _BREAKING_FOOTER.search(body)):
        return "major"
    if m and m.group("type") in _RELEASING:
        return _RELEASING[m.group("type")]
    wanted = {x.strip().lower().removeprefix("release:") for x in labels.split(",") if x.strip().lower().startswith("release:")}
    for kind in ("major", "minor", "patch"):
        if kind in wanted:
            return kind
    return "patch"


def write_version(text: str, new: str) -> str:
    out, n = _VERSION_LINE.subn(lambda m: f'{m.group(1)}"{new}"', text, count=1)
    if n != 1:
        raise ValueError("no `version = \"...\"` line found in pyproject.toml")
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    ap.add_argument("--bump", choices=["major", "minor", "patch"], help="explicit kind (overrides --title/--labels/--body)")
    ap.add_argument("--title", default="", help="PR title; a Conventional Commit prefix (feat, fix, perf, `!`) picks the bump")
    ap.add_argument("--labels", default="", help="comma-separated PR labels; `release:major|minor|patch` picks the bump")
    ap.add_argument("--body", default="", help="PR body; a `BREAKING CHANGE:` line makes the bump major")
    ap.add_argument("--write", action="store_true", help="update pyproject.toml (default: only print)")
    args = ap.parse_args(argv)

    tags = subprocess.run(["git", "tag", "--list", "v*"], check=True, capture_output=True, text=True).stdout.split()
    new = bump(latest_tag(tags), args.bump or choose_kind(args.title, args.labels, args.body))
    if args.write:
        PYPROJECT.write_text(write_version(PYPROJECT.read_text(encoding="utf-8"), new), encoding="utf-8")
    print(new)
    return 0


if __name__ == "__main__":
    sys.exit(main())
