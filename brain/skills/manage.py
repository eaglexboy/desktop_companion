"""Enable, disable and pin installed Agent Skills; see what they cost in context.

    python -m brain.skills.manage list
    python -m brain.skills.manage enable|disable|pin|unpin <skill> 
    python -m brain.skills.manage alias <skill> <short-name>

`<skill>` is `source/name`, a unique bare name, or an alias. The rules here are
shared by the CLI and `POST /api/skills/state` so they cannot drift apart:

  * Imported skills start disabled; the user enables each one.
  * Enabling needs a confirmation (typed or clicked, never spoken). A `flagged`
    skill needs its name typed. Disabling needs none.
  * Pinning needs the skill to be enabled, and there is a cap on pins.
  * Nothing the model says reaches this module (docs/ADR_SKILLS_AND_AGENT_ORCHESTRATION.md,
    invariant 3), and a test keeps it that way.
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from brain.skills import context
from brain.skills.agentskills import (
    AgentSkill,
    discover,
    display_names,
    read_provenance,
    skill_id,
)
from brain.skills.state import SkillState, SkillStateStore

CHANGERS = ("cli", "api", "overlay")
_ALIAS_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")


class StateChangeError(ValueError):
    """A requested change that is refused; the message says why."""


@dataclass(frozen=True)
class SkillView:
    id: str
    skill: AgentSkill
    display: str
    tier: str
    enabled: bool
    pinned: bool
    alias: str | None
    changed_by: str

    @property
    def exposure(self) -> str:
        return "stub" if self.enabled and self.pinned else "discoverable" if self.enabled else "absent"

    @property
    def stub(self) -> str:
        return context.stub(self.alias or self.display, self.skill.description)


def default_enabled(tier: str) -> bool:
    """Imports (clean or flagged) start disabled; only skills the user placed or that ship with us start enabled."""
    return tier in ("local", "bundled")


def build_views(root: Path, skills: list[AgentSkill], states: dict[str, SkillState]) -> list[SkillView]:
    names = display_names(root, skills)
    views = []
    for k in skills:
        sid = skill_id(root, k)
        tier = str(read_provenance(k.path).get("trust_tier") or "local")
        row = states.get(sid)
        views.append(
            SkillView(
                id=sid, skill=k, display=names[sid], tier=tier,
                enabled=row.enabled if row else default_enabled(tier),
                pinned=row.pinned if row else False,
                alias=row.alias if row else None,
                changed_by=row.changed_by if row else "default",
            )
        )
    return views


def resolve(views: list[SkillView], ref: str) -> SkillView:
    """`source/name`, an alias, a display name, or a bare name that is unique."""
    ref = ref.strip()
    for match in (
        [v for v in views if v.id == ref],
        [v for v in views if v.alias == ref],
        [v for v in views if v.display == ref],
        [v for v in views if v.skill.name == ref],
    ):
        if len(match) == 1:
            return match[0]
        if len(match) > 1:
            raise StateChangeError(f"{ref!r} is ambiguous; use one of: " + ", ".join(sorted(v.id for v in match)))
    raise StateChangeError(f"no installed skill {ref!r}")


def change_state(
    store: SkillStateStore,
    views: list[SkillView],
    ref: str,
    *,
    enabled: bool | None = None,
    pinned: bool | None = None,
    alias: str | None = None,
    confirmed: bool = False,
    confirm_text: str = "",
    changed_by: str,
    max_pinned: int,
) -> SkillState:
    if changed_by not in CHANGERS:
        raise StateChangeError(f"unknown source of change {changed_by!r}")
    view = resolve(views, ref)
    new_enabled = view.enabled if enabled is None else enabled
    new_pinned = view.pinned if pinned is None else pinned

    if new_enabled and not view.enabled:
        if not confirmed:
            raise StateChangeError("enabling a skill needs your confirmation (typed or clicked, not spoken)")
        if view.tier == "flagged" and confirm_text.strip().lower() != view.skill.name.lower():
            raise StateChangeError(
                f"{view.id} is flagged (scripts, dangerous tools or scan findings): type its name, "
                f"{view.skill.name!r}, to enable it"
            )
    if pinned and not new_enabled:
        raise StateChangeError("only an enabled skill can be pinned")
    if not new_enabled:
        new_pinned = False  # disabling a skill also un-pins it
    if new_pinned and not view.pinned and sum(1 for v in views if v.enabled and v.pinned and v.id != view.id) >= max_pinned:
        raise StateChangeError(f"at most {max_pinned} skills can be pinned")

    new_alias = view.alias
    if alias is not None:
        if alias == "":
            new_alias = None
        else:
            if not _ALIAS_RE.fullmatch(alias):
                raise StateChangeError("an alias is lowercase letters, digits and hyphens")
            if any(v.alias == alias and v.id != view.id for v in views):
                raise StateChangeError(f"alias {alias!r} is already used")
            new_alias = alias
    return store.put(view.id, enabled=new_enabled, pinned=new_pinned, alias=new_alias, changed_by=changed_by)


def summarize(skills_cfg: Any, context_tokens: int, views: list[SkillView]) -> dict[str, Any]:
    """The budget meter: what the pinned stubs would cost against what fits.
    Imported skills are inert in V1, so none of this is in a prompt yet."""
    budget = context.budget_tokens(context_tokens, skills_cfg.catalog_fraction, skills_cfg.catalog_token_cap)
    pinned = sorted((v for v in views if v.enabled and v.pinned), key=lambda v: v.id)
    plan = context.plan_stubs([(v.id, v.stub) for v in pinned], budget)
    return {
        "context_tokens": context_tokens,
        "budget_tokens": budget,
        "tokens_used": plan.tokens_used,
        "pinned": len(pinned),
        "max_pinned": skills_cfg.max_pinned,
        "dropped": plan.dropped,
        "enabled": sum(1 for v in views if v.enabled),
        "installed": len(views),
        "in_prompt": False,
    }


def view_json(v: SkillView) -> dict[str, Any]:
    prov = read_provenance(v.skill.path)
    keys = ("source", "origin", "commit", "category", "scripts_in_source", "scripts_installed", "dangerous_tools", "findings", "installed_at")
    return {
        "id": v.id, "name": v.skill.name, "display": v.display, "alias": v.alias,
        "description": v.skill.description, "track": v.skill.track, "has_scripts": v.skill.has_scripts,
        "license": v.skill.license, "path": str(v.skill.path), "warnings": list(v.skill.warnings),
        "tier": v.tier, "enabled": v.enabled, "pinned": v.pinned, "exposure": v.exposure,
        "stub_tokens": context.estimate_tokens(v.stub + "\n") if v.exposure == "stub" else 0,
        "changed_by": v.changed_by, "status": "inert",
        "provenance": {k: prov.get(k) for k in keys} if prov else None,
    }


# --- command line ----------------------------------------------------------------


def _ask(prompt: str) -> str:
    try:
        return input(prompt)
    except EOFError:  # no terminal to answer on: that is a "no", never a "yes"
        return ""


def _confirm(view: SkillView) -> tuple[bool, str]:
    if view.tier == "flagged":
        print(f"{view.id} is FLAGGED: " + _why_flagged(view))
        typed = _ask(f"Type the skill's name ({view.skill.name}) to enable it, anything else cancels: ")
        return typed.strip().lower() == view.skill.name.lower(), typed
    typed = _ask(f"Enable {view.id} ({view.skill.description[:80]})? Type 'yes' to confirm: ")
    return typed.strip().lower() == "yes", typed


def _why_flagged(view: SkillView) -> str:
    prov = read_provenance(view.skill.path)
    bits = list(prov.get("dangerous_tools") or []) + list(prov.get("findings") or [])
    if prov.get("scripts_in_source"):
        bits.append("has scripts/")
    return ", ".join(bits) or "see .source"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m brain.skills.manage", description=(__doc__ or "").split("\n\n")[0])
    ap.add_argument("action", choices=["list", "enable", "disable", "pin", "unpin", "alias"])
    ap.add_argument("skill", nargs="?")
    ap.add_argument("alias_name", nargs="?", help="for `alias`; an empty string clears it")
    args = ap.parse_args(argv)

    from brain.config import load_settings

    settings = load_settings()
    skills, _rejected = discover(settings.skills_dir)
    store = SkillStateStore(settings.personality_db_path)
    views = build_views(settings.skills_dir, skills, store.all())
    try:
        if args.action == "list":
            meter = summarize(settings.skills, settings.llm.smallest_context_tokens(), views)
            for v in sorted(views, key=lambda x: x.id):
                print(f"{'on ' if v.enabled else 'off'} {'pin' if v.pinned else '   '} {v.tier:9} {v.id}" + (f"  (alias {v.alias})" if v.alias else ""))
            print(f"\n{meter['enabled']}/{meter['installed']} enabled, {meter['pinned']}/{meter['max_pinned']} pinned, "
                  f"stubs {meter['tokens_used']}/{meter['budget_tokens']} tokens (context {meter['context_tokens']}); imported skills are inert for now")
            return 0
        if not args.skill:
            ap.error("a skill is required")
        kwargs: dict[str, Any] = {}
        if args.action == "enable":
            view = resolve(views, args.skill)
            if not view.enabled:
                ok, typed = _confirm(view)
                if not ok:
                    print("cancelled")
                    return 1
                kwargs = {"confirmed": True, "confirm_text": typed}
            kwargs["enabled"] = True
        elif args.action == "disable":
            kwargs["enabled"] = False
        elif args.action in ("pin", "unpin"):
            kwargs["pinned"] = args.action == "pin"
        else:
            if args.alias_name is None:
                ap.error("alias needs a name (or '' to clear)")
            kwargs["alias"] = args.alias_name
        state = change_state(store, views, args.skill, changed_by="cli", max_pinned=settings.skills.max_pinned, **kwargs)
        print(f"{state.skill_id}: enabled={state.enabled} pinned={state.pinned}" + (f" alias={state.alias}" if state.alias else ""))
        return 0
    except StateChangeError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
