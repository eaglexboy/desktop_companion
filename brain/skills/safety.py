"""Vetting for third-party skills before they are installed.

Community skills are untrusted input: their markdown is instructions an agent
will follow, and their `scripts/` are code. Nothing here makes a skill safe --
it only refuses the obvious, labels the rest, and records where it came from.
Real containment is the Bubblewrap sandbox and permission flow in the ADR
(Track 1/3), which must exist before any imported skill is *run*.

Portions adapted from OpenJarvis (https://github.com/open-jarvis/OpenJarvis),
Copyright 2025 The OpenJarvis Authors, Apache License 2.0:
  * the injection patterns, from src/openjarvis/security/injection_scanner.py
  * the trust-tier model, from src/openjarvis/skills/security.py
  * the symlink refusal, from src/openjarvis/skills/importer.py
Modified: regexes kept, but the shell/code/exfiltration ones downgraded from
high to medium (they fire on ordinary tool documentation); the scanner reduced
to a pure function, tiers renamed to our provenance, symlinks checked over the
whole package. See NOTICE.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from brain.skills.agentskills import AgentSkill

# Tools whose mere mention in `allowed-tools` means the skill wants to change
# things on this machine. Names are the Claude Code vocabulary that community
# skills are written in.
DANGEROUS_TOOLS = frozenset({"Bash", "Write", "Edit", "NotebookEdit"})

_MAX_MATCH_CHARS = 100

# (regex, name, severity, description). "high" = text that tries to hijack the
# agent: refused on import. "medium" = command-shaped text (`curl ... | sh`,
# `POST http://`) that is ordinary in tool documentation (measured: 35 of 211
# Hermes skills trip it): labelled FLAGGED, not refused -- running anything is
# gated by the sandbox/permission flow, not by this scan.
_INJECTION_PATTERNS = [
    (r"(?i)ignore\s+(all\s+)?(previous|prior|above)\s+(instructions?|prompts?|rules?)",
     "prompt_override", "high", "Attempt to override system instructions"),
    (r"(?i)you\s+are\s+now\s+(?:a\s+)?(?:different|new|my)",
     "identity_override", "high", "Attempt to change AI identity"),
    (r"(?i)disregard\s+(?:all\s+)?(?:previous|prior|your)\s+(?:instructions?|programming|rules?)",
     "prompt_override", "high", "Attempt to disregard instructions"),
    (r"(?i)(?:execute|run|eval)\s*\(\s*['\"]",
     "code_injection", "medium", "Code execution attempt in prompt"),
    (r"(?:;|\||&&)\s*(?:rm|curl|wget|nc|ncat|bash|sh|python|perl)\s",
     "shell_injection", "medium", "Shell command injection"),
    (r"(?i)(?:send|post|upload|exfiltrate|transmit)\s+(?:(?:to|data|all|everything)\s+)*(?:to\s+)?(?:https?://|my\s+server)",
     "exfiltration", "medium", "Data exfiltration attempt"),
    (r"(?i)base64\s+encode\s+(?:and\s+)?(?:send|include|append)",
     "exfiltration", "medium", "Encoded exfiltration attempt"),
    (r"(?i)(?:DAN|do\s+anything\s+now)\s+(?:mode|prompt|jailbreak)",
     "jailbreak", "high", "DAN jailbreak attempt"),
    (r"(?i)pretend\s+(?:you\s+)?(?:have\s+)?no\s+(?:restrictions?|limitations?|rules?|filters?)",
     "jailbreak", "medium", "Restriction bypass attempt"),
    (r"```(?:system|assistant)\b",
     "delimiter_injection", "medium", "Role delimiter injection"),
    (r"<\|(?:im_start|im_end|system|assistant)\|>",
     "delimiter_injection", "high", "Chat template injection"),
]
_COMPILED = [(re.compile(p), n, s, d) for p, n, s, d in _INJECTION_PATTERNS]


@dataclass(frozen=True)
class Finding:
    name: str
    severity: str
    description: str
    where: str          # file, relative to the skill directory
    matched: str


class TrustTier(str, Enum):
    BUNDLED = "bundled"          # ships with the companion
    LOCAL = "local"              # the user wrote or copied it by hand
    IMPORTED = "imported"        # fetched from a named source; scanned, not reviewed
    FLAGGED = "flagged"          # imported, and something in it needs a human decision


def scan_text(text: str, where: str = "SKILL.md") -> list[Finding]:
    found = []
    for rx, name, severity, description in _COMPILED:
        m = rx.search(text)
        if m:
            found.append(Finding(name, severity, description, where, m.group(0)[:_MAX_MATCH_CHARS]))
    return found


def scan_skill(skill: AgentSkill) -> list[Finding]:
    """Scans the instruction body and the text references the agent would read.
    Never reads scripts/ as prose -- those are judged by being scripts at all."""
    findings = scan_text(skill.description, "description") + scan_text(skill.body)
    if skill.path is not None:
        for sub in ("references", "assets"):
            for f in sorted((skill.path / sub).rglob("*")) if (skill.path / sub).is_dir() else []:
                text_file = f.is_file() and not f.is_symlink() and f.suffix.lower() in {".md", ".txt", ".json", ".yaml", ".yml", ".toml"}
                if text_file and f.stat().st_size <= 256 * 1024:
                    findings += scan_text(f.read_text(encoding="utf-8", errors="replace"), f.relative_to(skill.path).as_posix())
    return findings


def dangerous_tools(skill: AgentSkill) -> list[str]:
    """`allowed-tools` entries (`Bash(git:*)` -> `Bash`) that can change this machine."""
    return sorted({t.partition("(")[0] for t in skill.allowed_tools if t.partition("(")[0] in DANGEROUS_TOOLS})


def find_symlinks(root: Path) -> list[Path]:
    """Symlinks anywhere below `root`, not followed. A skill package must not
    be able to point at files outside itself."""
    if root.is_symlink():
        return [root]
    return [p for p in root.rglob("*") if p.is_symlink()] if root.exists() else []


def classify(*, imported: bool, findings: list[Finding], dangerous: list[str], has_scripts: bool) -> TrustTier:
    if not imported:
        return TrustTier.LOCAL
    if findings or dangerous or has_scripts:
        return TrustTier.FLAGGED
    return TrustTier.IMPORTED
