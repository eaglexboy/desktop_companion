"""Agent Skills (https://agentskills.io/specification): the open `SKILL.md` standard.

A skill is a directory holding a `SKILL.md` -- YAML frontmatter (`name`,
`description`, optional `license`, `compatibility`, `metadata`,
`allowed-tools`) followed by a markdown body of instructions -- plus optional
`scripts/`, `references/` and `assets/`. This lets the companion use skills
other people wrote. See docs/ADR_SKILLS_AND_AGENT_ORCHESTRATION.md.

This module only *reads and validates* skills. It never runs anything: the
instructions are for an agent loop (Track 3), which does not exist yet, so
installed skills are inert catalog entries for now. The companion's own typed
actions (brain/skills/registry.py) are a separate, older mechanism.

Our own settings ride in the spec's free-form `metadata` map under
`deskmate.*` keys (string values, as the spec requires), e.g.

    metadata:
      deskmate.track: agent          # script | tool | agent (default: agent)
      deskmate.allow_network: "false"

Absent keys mean the most restrictive reading; community skills never need them.

Portions adapted from OpenJarvis (https://github.com/open-jarvis/OpenJarvis,
src/openjarvis/skills/{parser,loader}.py), Copyright 2025 The OpenJarvis
Authors, Apache License 2.0 -- the frontmatter splitting, the strict
name/description rules and the one-or-two-levels directory discovery. See
NOTICE.
"""

from __future__ import annotations

import logging
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger("deskmate.skills.agentskills")

MAX_NAME_LENGTH = 64
MAX_DESCRIPTION_LENGTH = 1024
MAX_COMPATIBILITY_LENGTH = 500
MAX_SKILL_MD_BYTES = 256 * 1024  # a SKILL.md bigger than this is not a skill, it is a payload
NAMESPACE = "deskmate."
TRACKS = ("script", "tool", "agent")

# lowercase alphanumerics and single hyphens; no leading/trailing hyphen.
_NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


class SkillParseError(ValueError):
    """A SKILL.md that does not follow the specification."""


@dataclass(frozen=True)
class AgentSkill:
    name: str
    description: str
    body: str                                   # markdown instructions (loaded on activation, not at startup)
    path: Path | None = None                    # the skill directory
    license: str = ""
    compatibility: str = ""
    allowed_tools: tuple[str, ...] = ()         # as declared; experimental in the spec, never trusted by us
    metadata: dict[str, Any] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()              # spec deviations we tolerated

    @property
    def track(self) -> str:
        value = str(self.metadata.get(NAMESPACE + "track", "agent")).strip().lower()
        return value if value in TRACKS else "agent"

    def deskmate_flag(self, key: str) -> bool:
        """A `deskmate.<key>` metadata flag; anything but an explicit "true" is False."""
        return str(self.metadata.get(NAMESPACE + key, "")).strip().lower() == "true"

    @property
    def has_scripts(self) -> bool:
        return self.path is not None and (self.path / "scripts").is_dir()


def validate_name(name: str) -> None:
    if not 1 <= len(name) <= MAX_NAME_LENGTH:
        raise SkillParseError(f"name must be 1-{MAX_NAME_LENGTH} characters, got {len(name)}")
    if not _NAME_RE.fullmatch(name):
        raise SkillParseError(
            f"name {name!r} must be lowercase letters, digits and single hyphens, "
            "not starting or ending with a hyphen"
        )


def split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Splits `---` delimited YAML frontmatter from the markdown body."""
    text = text.lstrip("﻿")
    if not text.startswith("---"):
        raise SkillParseError("SKILL.md must begin with YAML frontmatter (---)")
    rest = text[3:]
    rest = rest[1:] if rest.startswith("\n") else rest.removeprefix("\r\n")
    end = re.search(r"^---[ \t]*\r?$", rest, flags=re.MULTILINE)
    if end is None:
        raise SkillParseError("frontmatter is not closed with ---")
    try:
        front = yaml.safe_load(rest[: end.start()]) or {}
    except yaml.YAMLError as exc:
        raise SkillParseError(f"frontmatter is not valid YAML: {exc}") from exc
    if not isinstance(front, dict):
        raise SkillParseError("frontmatter must be a mapping")
    return front, rest[end.end():].lstrip("\r\n")


def parse_skill_md(text: str, *, dirname: str | None = None, path: Path | None = None) -> AgentSkill:
    """Strictly validates `text` against the spec. Raises SkillParseError.

    A name that differs from the directory name is tolerated with a warning:
    community repositories often get this wrong and the skill is still usable.
    """
    front, body = split_frontmatter(text)
    warnings: list[str] = []

    name, description = front.get("name"), front.get("description")
    if not isinstance(name, str) or not name:
        raise SkillParseError("missing required field: name")
    if not isinstance(description, str) or not description.strip():
        raise SkillParseError("missing required field: description")
    validate_name(name)
    description = description.strip()
    if len(description) > MAX_DESCRIPTION_LENGTH:
        raise SkillParseError(f"description is longer than {MAX_DESCRIPTION_LENGTH} characters")

    compat = front.get("compatibility", "")
    if compat is None:
        compat = ""
    if not isinstance(compat, str):
        raise SkillParseError("compatibility must be text")
    if len(compat) > MAX_COMPATIBILITY_LENGTH:
        raise SkillParseError(f"compatibility is longer than {MAX_COMPATIBILITY_LENGTH} characters")

    licence = front.get("license", "")
    licence = "" if licence is None else str(licence)

    metadata = front.get("metadata") or {}
    if not isinstance(metadata, dict):
        warnings.append("metadata is not a mapping; ignored")
        metadata = {}

    allowed = front.get("allowed-tools", "")
    if isinstance(allowed, list):  # some vendors write a YAML list; the spec says space-separated text
        allowed = " ".join(str(a) for a in allowed)
        warnings.append("allowed-tools should be space-separated text, not a list")
    allowed_tools = tuple(str(allowed).split()) if allowed else ()

    if dirname is not None and dirname != name:
        warnings.append(f"name {name!r} does not match its directory {dirname!r}")

    return AgentSkill(
        name=name,
        description=description,
        body=body,
        path=path,
        license=licence,
        compatibility=compat,
        allowed_tools=allowed_tools,
        metadata=metadata,
        warnings=tuple(warnings),
    )


def load_skill_dir(path: Path) -> AgentSkill:
    """Loads `<path>/SKILL.md` (or `skill.md`). Raises SkillParseError / OSError."""
    md = next((p for p in (path / "SKILL.md", path / "skill.md") if p.is_file() and not p.is_symlink()), None)
    if md is None:
        raise SkillParseError(f"no SKILL.md in {path}")
    if md.stat().st_size > MAX_SKILL_MD_BYTES:
        raise SkillParseError(f"SKILL.md is larger than {MAX_SKILL_MD_BYTES // 1024} KiB")
    return parse_skill_md(md.read_text(encoding="utf-8"), dirname=path.name, path=path)


def discover(root: Path) -> tuple[list[AgentSkill], list[dict[str, str]]]:
    """Finds installed skills under `root`: `<root>/<name>/` and
    `<root>/<source>/<name>/` (the importer's layout). Returns the valid
    skills and a list of `{"path", "reason"}` for directories that were skipped;
    one bad skill never hides the others."""
    skills: list[AgentSkill] = []
    rejected: list[dict[str, str]] = []
    if not root.is_dir():
        return skills, rejected

    def has_md(d: Path) -> bool:
        return (d / "SKILL.md").exists() or (d / "skill.md").exists()

    def load(d: Path) -> None:
        try:
            skills.append(load_skill_dir(d))
        except (SkillParseError, OSError, UnicodeDecodeError) as exc:
            logger.warning("skipping skill at %s: %s", d, exc)
            rejected.append({"path": str(d), "reason": str(exc)})

    for child in sorted(p for p in root.iterdir() if p.is_dir() and not p.is_symlink() and not p.name.startswith(".")):
        if has_md(child):
            load(child)
            continue
        for grandchild in sorted(p for p in child.iterdir() if p.is_dir() and not p.is_symlink() and not p.name.startswith(".")):
            if has_md(grandchild):
                load(grandchild)
    return skills, rejected


def read_provenance(path: Path | None) -> dict[str, Any]:
    """The importer's `.source` file for an installed skill, or {} for a skill
    that was placed by hand (or whose file is unreadable)."""
    if path is None:
        return {}
    try:
        return tomllib.loads((path / ".source").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


LOCAL_SOURCE = "local"


def source_of(root: Path, skill: AgentSkill) -> str:
    """Where a skill came from: the importer's `<source>/` directory, or "local"
    for one placed directly under the skills directory."""
    if skill.path is None or skill.path.parent == root:
        return LOCAL_SOURCE
    return skill.path.parent.name


def skill_id(root: Path, skill: AgentSkill) -> str:
    """The canonical, never-changing identifier: `<source>/<name>`."""
    return f"{source_of(root, skill)}/{skill.name}"


def display_names(root: Path, skills: list[AgentSkill]) -> dict[str, str]:
    """skill id -> the short form people and the model use: the bare name while
    it is unique among installed skills, `name@source` when two share it."""
    counts: dict[str, int] = {}
    for k in skills:
        counts[k.name] = counts.get(k.name, 0) + 1
    return {skill_id(root, k): (k.name if counts[k.name] == 1 else f"{k.name}@{source_of(root, k)}") for k in skills}
