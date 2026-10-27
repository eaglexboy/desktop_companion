"""Installs third-party Agent Skills (SKILL.md packages) into the skills directory.

    python -m brain.skills.importer <https-git-url | local-dir> [--skill NAME]
        [--dest DIR] [--source LABEL] [--with-scripts] [--force] [--accept-findings]

What it does, in order: fetch (shallow, https only, symlinks never
materialised) -> find every SKILL.md -> validate -> scan -> refuse or label ->
copy to `<dest>/<source>/<name>/` with a `.source` provenance file. What it
deliberately does not do: run anything, follow symlinks, install `scripts/`
without `--with-scripts`, or overwrite an installed skill without `--force`.
The upstream LICENSE files are copied along, because the skills are other
people's work.

Installed skills are inert for now (no agent runtime yet); see the ADR.

Portions adapted from OpenJarvis (https://github.com/open-jarvis/OpenJarvis),
Copyright 2025 The OpenJarvis Authors, Apache License 2.0 -- the install flow
and layout (`<source>/<name>/`, `.source` provenance, scripts opt-in, symlink
refusal) from src/openjarvis/skills/importer.py, and the recursive SKILL.md
search from src/openjarvis/skills/sources/github.py. Rewritten for our
validator and trust model. See NOTICE.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from brain.skills.agentskills import AgentSkill, SkillParseError, load_skill_dir
from brain.skills.safety import (
    Finding,
    TrustTier,
    classify,
    dangerous_tools,
    find_symlinks,
    scan_skill,
)

MAX_PACKAGE_BYTES = 20 * 1024 * 1024
GIT_TIMEOUT_S = 180
_SOURCE_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")


@dataclass
class ImportResult:
    name: str
    ok: bool = False
    skipped: bool = False
    target: Path | None = None
    tier: TrustTier | None = None
    findings: list[Finding] = field(default_factory=list)
    dangerous_tools: list[str] = field(default_factory=list)
    scripts_installed: bool = False
    messages: list[str] = field(default_factory=list)


def fetch_git(url: str, into: Path) -> str:
    """Shallow-clones an https repository into `into` and returns the commit.
    Symlinks are checked out as plain files, so a hostile repo cannot make a
    skill point outside its own directory."""
    if not url.startswith("https://"):
        raise ValueError("only https:// repositories are accepted")
    env = {k: v for k, v in os.environ.items() if k in {"PATH", "HOME", "LANG", "SSL_CERT_FILE", "SSL_CERT_DIR"}}
    env["GIT_TERMINAL_PROMPT"] = "0"
    cmd = ["git", "-c", "core.symlinks=false", "-c", "protocol.allow=never", "-c", "protocol.https.allow=always",
           "clone", "--depth", "1", "--no-tags", "--", url, str(into)]
    subprocess.run(cmd, check=True, env=env, timeout=GIT_TIMEOUT_S, capture_output=True, text=True)
    out = subprocess.run(["git", "-C", str(into), "rev-parse", "HEAD"], check=True, env=env, capture_output=True, text=True)
    return out.stdout.strip()


def find_skill_dirs(root: Path) -> list[Path]:
    """Every directory under `root` that holds a SKILL.md, outside `.git`."""
    seen: dict[Path, None] = {}
    for pattern in ("SKILL.md", "skill.md"):
        for md in sorted(root.rglob(pattern)):
            if ".git" not in md.parts and not md.is_symlink():
                seen.setdefault(md.parent, None)
    return list(seen)


def _package_size(root: Path) -> int:
    return sum(f.stat().st_size for f in root.rglob("*") if f.is_file())


def _toml_str(value: object) -> str:
    return json.dumps(str(value))  # a JSON string is a valid TOML basic string


def install(
    skill_dir: Path,
    dest_root: Path,
    *,
    source: str,
    commit: str = "",
    origin: str = "",
    category: str = "",
    with_scripts: bool = False,
    force: bool = False,
    accept_findings: bool = False,
) -> ImportResult:
    if not _SOURCE_RE.fullmatch(source):
        raise ValueError(f"invalid source label {source!r}")
    try:
        skill = load_skill_dir(skill_dir)
    except (SkillParseError, OSError, UnicodeDecodeError) as exc:
        r = ImportResult(skill_dir.name)
        r.messages.append(f"not a valid skill: {exc}")
        return r

    r = ImportResult(skill.name)
    r.messages += [f"warning: {w}" for w in skill.warnings]
    target = dest_root / source / skill.name  # skill.name is regex-validated: no separators, no '..'
    r.target = target

    if target.exists() and not force:
        r.skipped = True
        r.messages.append(f"already installed at {target} (use --force to replace)")
        return r
    links = find_symlinks(skill_dir)
    if links:
        r.messages.append("refused: package contains symlinks (" + ", ".join(p.relative_to(skill_dir).as_posix() for p in links[:3]) + ")")
        return r
    if _package_size(skill_dir) > MAX_PACKAGE_BYTES:
        r.messages.append(f"refused: package is larger than {MAX_PACKAGE_BYTES // (1024 * 1024)} MiB")
        return r

    r.findings = scan_skill(skill)
    r.dangerous_tools = dangerous_tools(skill)
    r.tier = classify(imported=True, findings=r.findings, dangerous=r.dangerous_tools, has_scripts=skill.has_scripts)
    if any(f.severity == "high" for f in r.findings) and not accept_findings:
        r.messages.append(
            "refused: instructions look like a prompt-injection attempt ("
            + "; ".join(f"{f.name} in {f.where}: {f.matched.strip()!r}" for f in r.findings if f.severity == "high")
            + "); review the skill and re-run with --accept-findings if you trust it"
        )
        return r

    if target.exists():
        shutil.rmtree(target)
    ignore = shutil.ignore_patterns(".git", ".github", *(() if with_scripts else ("scripts",)))
    shutil.copytree(skill_dir, target, ignore=ignore, symlinks=True)
    r.scripts_installed = with_scripts and skill.has_scripts
    if skill.has_scripts and not with_scripts:
        r.messages.append("scripts/ not installed (use --with-scripts); the instructions may reference it")
    _write_provenance(target, skill, r, source=source, commit=commit, origin=origin, category=category)
    r.ok = True
    return r


def _write_provenance(
    target: Path, skill: AgentSkill, r: ImportResult, *, source: str, commit: str, origin: str, category: str
) -> None:
    lines = [
        f"source = {_toml_str(source)}",
        f"origin = {_toml_str(origin)}",
        f"commit = {_toml_str(commit)}",
        f"category = {_toml_str(category)}",
        f"license = {_toml_str(skill.license)}",
        f"installed_at = {_toml_str(datetime.now(UTC).strftime('%Y-%m-%dT%H:%M:%SZ'))}",
        f"trust_tier = {_toml_str(r.tier.value if r.tier else '')}",
        f"scripts_in_source = {'true' if skill.has_scripts else 'false'}",
        f"scripts_installed = {'true' if r.scripts_installed else 'false'}",
        "dangerous_tools = [" + ", ".join(_toml_str(t) for t in r.dangerous_tools) + "]",
        "findings = [" + ", ".join(_toml_str(f"{f.severity}:{f.name}@{f.where}") for f in r.findings) + "]",
    ]
    (target / ".source").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9._-]+", "-", text.lower()).strip("-.")


def _label_from(origin: str, dest: Path | None = None) -> str:
    """The source label (the `@suffix` and the install subdirectory): the
    repository name alone (`hermes-agent`), or `owner-repo` if a different
    repository already installed under that name."""
    parts = [p for p in origin.rstrip("/").removesuffix(".git").split("/") if p]
    repo = _slug(parts[-1]) if parts else ""
    if not _SOURCE_RE.fullmatch(repo):
        return "local"
    if dest is not None and (dest / repo).is_dir():
        for src in (dest / repo).glob("*/.source"):
            try:
                existing = tomllib.loads(src.read_text(encoding="utf-8")).get("origin", "")
            except (OSError, ValueError):
                continue
            if existing and existing != origin:
                qualified = _slug("-".join(parts[-2:]))
                return qualified if _SOURCE_RE.fullmatch(qualified) else repo
            break
    return repo


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m brain.skills.importer", description=(__doc__ or "").split("\n\n")[0])
    ap.add_argument("source", help="https git URL, or a local directory containing SKILL.md packages")
    ap.add_argument("--skill", action="append", help="install only this skill name (repeatable); default: all found")
    ap.add_argument("--dest", type=Path, help="skills directory (default: from config)")
    ap.add_argument("--source-label", help="subdirectory label (default: derived from the URL)")
    ap.add_argument("--with-scripts", action="store_true")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--accept-findings", action="store_true")
    args = ap.parse_args(argv)

    dest = args.dest
    if dest is None:
        from brain.config import load_settings

        dest = load_settings().skills_dir
    dest = dest.expanduser()

    with tempfile.TemporaryDirectory(prefix="deskmate-skill-import-") as tmp:
        if args.source.startswith("https://"):
            clone = Path(tmp) / "repo"
            try:
                commit = fetch_git(args.source, clone)
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired, ValueError) as exc:
                print(f"fetch failed: {getattr(exc, 'stderr', None) or exc}", file=sys.stderr)
                return 2
            root, origin = clone, args.source
        else:
            root, commit, origin = Path(args.source).expanduser().resolve(), "", str(Path(args.source).expanduser().resolve())
            if not root.is_dir():
                print(f"not a directory: {root}", file=sys.stderr)
                return 2
        label = args.source_label or _label_from(origin, dest)

        dirs = find_skill_dirs(root)
        if args.skill:
            dirs = [d for d in dirs if d.name in args.skill]
        if not dirs:
            print("no SKILL.md packages found", file=sys.stderr)
            return 1
        failures = 0
        for d in dirs:
            r = install(d, dest, source=label, commit=commit, origin=origin,
                        category=d.parent.relative_to(root).as_posix() if d.parent != root else "",
                        with_scripts=args.with_scripts, force=args.force, accept_findings=args.accept_findings)
            status = "installed" if r.ok else "skipped" if r.skipped else "REFUSED"
            tier = f" [{r.tier.value}]" if r.tier else ""
            print(f"{status:9} {r.name}{tier}")
            for m in r.messages:
                print(f"          {m}")
            failures += not (r.ok or r.skipped)
        return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
