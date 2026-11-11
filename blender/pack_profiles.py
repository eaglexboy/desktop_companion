"""Pack profiles: what the clips of an animation pack are for, and how to convert them.

Pure Python (no bpy), read by `blender/animation_library.py`, which the generator and
`brain.appearance.models` share. A profile is a JSON file that knows clips by name:

    {
      "format": 1,
      "name": "mypack",
      "description": "...",
      "roles":    {"greet": ["Standing_Greeting"]},         # role -> clips that fill it, best first
      "extras":   {"greet": ["Waving", "Waving_1"]},        # role -> more clips, played as options
      "clips":    [{"match": ["Waving", "*Greeting*"],      # conversion options for clips by name
                    "options": {"variants": true}}],        #   (glob patterns; later rules win)
      "derived":  [{"name": "Workout_Crunch",               # a clip made of others, when all of
                    "requires": ["Bicycle_Crunch", "Stand_Up"],  # `requires` are in the pack
                    "options": {"compose": ["Lie_Down", ["Bicycle_Crunch", 5], "Stand_Up"]}}],
      "sit_sets": [{"chair": false,                         # a way to sit down: enter, idle, exit
                    "clips": {"sit_enter": ["..."], "sit_idle": ["..."], "sit_exit": ["..."]}}],
      "time_scale": {"idle": 2.5},                          # role -> how much slower to play it
      "disable": ["Bow"]                                    # clips not to use for any role
    }

Profiles are layered: the ones shipped with the repository (`blender/packs/`, the Quaternius
one first), then the user's (`<deploy_dir>/assets/animation_profiles/*.json`). Clip names are
what ties a profile to a pack, so a profile for a pack that is not installed does nothing. A
role's candidates are the union over the layers (the user's first, so their preference wins),
conversion rules apply in layer order (the user's last, so they win), a derived clip by name
(a later layer replaces an earlier), and `disable` removes a clip from every role. A profile
that does not validate is skipped, with the reason, and never stops the rest.
"""

from __future__ import annotations

import fnmatch
import json
import sys
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

FORMAT = 1

PACKS_DIR = Path(__file__).resolve().parent / "packs"
USER_PROFILES_DIR = ("assets", "animation_profiles")
FIRST_PROFILE = (
    "quaternius"  # the supported source: the others are layered over it
)

# The roles the overlay and the generator know (SPEC_OVERLAY, ADR_ACCESSORY_MODELS 14). A
# profile may only fill these: a typo would otherwise be a role nothing plays.
KNOWN_ROLES = frozenset(
    {
        "idle",
        "talk",
        "run",
        "walk",
        "idle_alt",
        "celebrate",
        "greet",
        "angry",
        "hip_thrust",
        "sit_alt",
        "sit_talk",
        "workout",
        "workout_floor",
        "lie_down",
        "sleep",
        "sleep_alt",
        "wake",
        "wake_slow",
        "idle_look",
        "idle_sad",
        "attention",
        "think_long",
        "floor_sit_enter",
        "floor_sit_idle",
        "floor_sit_exit",
    }
)
SIT_ROLES = ("sit_enter", "sit_idle", "sit_exit")

# What the converter (`blender/mixamo_convert.py`, `finish_clips`) understands per clip.
CLIP_OPTION_KEYS = frozenset(
    {
        "loop",
        "variants",
        "mirror",
        "mirror_main",
        "seat",
        "blend",
        "max_seconds",
        "in_place",
        "trim",
        "extract",
        "plant_feet",
        "emit",
        "reverse",
        "ping_pong",
        "ease_from",
        "ease_to",
        "blend_ref",
        "ease_frames",
        "ease",
        "compose",
        "hold",
    }
)
# ... and what a part cut out of a clip (`extract`) may say besides.
EXTRACT_KEYS = CLIP_OPTION_KEYS | {"name"}

# The options every clip starts from.
BASE_OPTIONS: dict = {
    "loop": False,
    "variants": False,
    "mirror": True,
    "seat": False,
    "in_place": False,
    "trim": None,
    "extract": [],
}

SECTIONS = frozenset(
    {
        "format",
        "name",
        "description",
        "source",
        "license",
        "roles",
        "extras",
        "clips",
        "derived",
        "sit_sets",
        "time_scale",
        "disable",
    }
)


class ProfileError(ValueError):
    """A profile that does not validate; the message says every problem."""


@dataclass(frozen=True)
class Profile:
    name: str
    origin: str  # the file it came from, or "<built in>"
    data: dict = field(repr=False)


def _names(value, where: str, problems: list[str]) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(
        isinstance(v, str) and v for v in value
    ):
        problems.append(f"{where}: a list of clip names is expected")
        return ()
    return tuple(value)


def _options(
    value, where: str, problems: list[str], allowed=CLIP_OPTION_KEYS
) -> None:
    if not isinstance(value, dict):
        problems.append(f"{where}: options (an object) are expected")
        return
    for key in value:
        if key not in allowed:
            problems.append(f"{where}: unknown option '{key}'")
    for part in (
        value.get("extract", [])
        if isinstance(value.get("extract"), list)
        else []
    ):
        if not isinstance(part, dict) or not isinstance(part.get("name"), str):
            problems.append(f"{where}: an extract needs a name")
        else:
            _options(
                {k: v for k, v in part.items()},
                f"{where} extract '{part['name']}'",
                problems,
                EXTRACT_KEYS,
            )
    compose = value.get("compose")
    if compose is not None:
        if not isinstance(compose, list) or not compose:
            problems.append(f"{where}: compose is a list of clips")
        else:
            for item in compose:
                ok = isinstance(item, str) or (
                    isinstance(item, list)
                    and len(item) == 2
                    and isinstance(item[0], str)
                    and isinstance(item[1], int)
                    and item[1] >= 1
                )
                if not ok:
                    problems.append(
                        f"{where}: compose has '{item}': a clip, or [clip, repeat]"
                    )


def validate(data, origin: str = "<profile>") -> Profile:
    """The profile, or a `ProfileError` that lists everything wrong with `data`."""
    problems: list[str] = []
    if not isinstance(data, dict):
        raise ProfileError(f"{origin}: a profile is a JSON object")
    if data.get("format") != FORMAT:
        problems.append(
            f"format must be {FORMAT} (this is {data.get('format')!r})"
        )
    name = data.get("name")
    if (
        not isinstance(name, str)
        or not name.replace("_", "").replace("-", "").isalnum()
    ):
        problems.append("name: letters, digits, '-' and '_' only")
        name = str(name)
    for key in data:
        if key not in SECTIONS:
            problems.append(f"unknown section '{key}'")
    for section in ("roles", "extras"):
        table = data.get(section, {})
        if not isinstance(table, dict):
            problems.append(f"{section}: an object (role -> clips) is expected")
            continue
        for role, clips in table.items():
            if role not in KNOWN_ROLES:
                problems.append(
                    f"{section}: '{role}' is not a role (known: {', '.join(sorted(KNOWN_ROLES))})"
                )
            _names(clips, f"{section}.{role}", problems)
    rules = data.get("clips", [])
    if not isinstance(rules, list):
        problems.append("clips: a list of {match, options} is expected")
        rules = []
    for n, rule in enumerate(rules):
        where = f"clips[{n}]"
        if not isinstance(rule, dict) or set(rule) - {"match", "options"}:
            problems.append(f"{where}: {{match, options}} is expected")
            continue
        match = rule.get("match")
        if isinstance(match, str):
            match = [match]
        _names(match, f"{where}.match", problems)
        _options(rule.get("options"), where, problems)
    seen: set[str] = set()
    derived = data.get("derived", [])
    if not isinstance(derived, list):
        problems.append("derived: a list is expected")
        derived = []
    for n, entry in enumerate(derived):
        where = f"derived[{n}]"
        if not isinstance(entry, dict) or set(entry) - {
            "name",
            "requires",
            "options",
        }:
            problems.append(f"{where}: {{name, requires, options}} is expected")
            continue
        if not isinstance(entry.get("name"), str) or not entry["name"]:
            problems.append(f"{where}: a name is needed")
        elif entry["name"] in seen:
            problems.append(f"{where}: '{entry['name']}' is made twice")
        else:
            seen.add(entry["name"])
        _names(entry.get("requires", []), f"{where}.requires", problems)
        _options(entry.get("options"), where, problems)
        opts = entry.get("options")
        if isinstance(opts, dict) and not (
            opts.get("compose") or opts.get("hold")
        ):
            problems.append(f"{where}: a derived clip is composed or held")
    sit_sets = data.get("sit_sets", [])
    if not isinstance(sit_sets, list):
        problems.append("sit_sets: a list is expected")
        sit_sets = []
    for n, entry in enumerate(sit_sets):
        where = f"sit_sets[{n}]"
        if not isinstance(entry, dict) or set(entry) - {"chair", "clips"}:
            problems.append(f"{where}: {{chair, clips}} is expected")
            continue
        if not isinstance(entry.get("chair", False), bool):
            problems.append(f"{where}: chair is true or false")
        clips = entry.get("clips")
        if not isinstance(clips, dict) or set(clips) != set(SIT_ROLES):
            problems.append(
                f"{where}: clips needs exactly {', '.join(SIT_ROLES)}"
            )
        else:
            for role, names in clips.items():
                _names(names, f"{where}.{role}", problems)
    scale = data.get("time_scale", {})
    if not isinstance(scale, dict):
        problems.append("time_scale: an object (role -> factor) is expected")
    else:
        for role, factor in scale.items():
            if role not in KNOWN_ROLES and role not in SIT_ROLES:
                problems.append(f"time_scale: '{role}' is not a role")
            if (
                not isinstance(factor, (int, float))
                or isinstance(factor, bool)
                or factor <= 0
            ):
                problems.append(
                    f"time_scale.{role}: a number above 0 is expected"
                )
    if "disable" in data:
        _names(data["disable"], "disable", problems)
    if problems:
        raise ProfileError(f"{origin}: " + "; ".join(problems))
    return Profile(name, origin, data)


def load_file(path: Path) -> Profile:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise ProfileError(f"{path}: cannot be read ({exc})") from None
    return validate(data, str(path))


@dataclass(frozen=True)
class Merged:
    """The layers put together: what `animation_library` reads."""

    profiles: tuple[Profile, ...]
    roles: dict[str, tuple[str, ...]]
    extras: dict[str, tuple[str, ...]]
    rules: tuple[tuple[tuple[str, ...], dict], ...]
    derived: tuple[dict, ...]
    sit_sets: tuple[dict[str, tuple[str, ...]], ...]
    chair_clips: frozenset[str]
    time_scale: dict[str, float]
    disabled: frozenset[str]
    problems: tuple[str, ...]


def merge(profiles: list[Profile], problems: tuple[str, ...] = ()) -> Merged:
    layers = [p.data for p in profiles]
    disabled = frozenset(c for d in layers for c in d.get("disable", []))

    def union(section: str) -> dict[str, tuple[str, ...]]:
        order: list[str] = []
        for d in layers:  # the roles in the order they first appear
            order += [r for r in d.get(section, {}) if r not in order]
        out = {}
        for role in order:
            clips: list[str] = []
            for d in reversed(layers):  # the clips, the last layer's first
                clips += [
                    c
                    for c in d.get(section, {}).get(role, [])
                    if c not in clips and c not in disabled
                ]
            out[role] = tuple(clips)
        return {r: c for r, c in out.items() if c}

    rules = []
    for d in layers:
        for rule in d.get("clips", []):
            match = rule["match"]
            rules.append(
                (
                    tuple([match] if isinstance(match, str) else match),
                    rule["options"],
                )
            )
    derived: dict[str, dict] = {}
    for d in layers:
        for entry in d.get("derived", []):
            derived.pop(
                entry["name"], None
            )  # replaced: it goes where the later layer puts it
            derived[entry["name"]] = {
                "name": entry["name"],
                "requires": list(entry.get("requires", [])),
                "options": entry["options"],
            }
    sit_sets: list[dict[str, tuple[str, ...]]] = []
    chair: set[str] = set()
    for d in reversed(layers):  # best first: the last layer's sets come first
        for entry in d.get("sit_sets", []):
            sit_sets.append({r: tuple(c) for r, c in entry["clips"].items()})
            if entry.get("chair"):
                chair |= {c for names in entry["clips"].values() for c in names}
    scale: dict[str, float] = {}
    for d in layers:
        scale.update({r: float(f) for r, f in d.get("time_scale", {}).items()})
    return Merged(
        tuple(profiles),
        union("roles"),
        union("extras"),
        tuple(rules),
        tuple(derived.values()),
        tuple(sit_sets),
        frozenset(chair),
        scale,
        disabled,
        problems,
    )


def shipped_profiles() -> tuple[list[Profile], list[str]]:
    files = sorted(
        PACKS_DIR.glob("*.json"),
        key=lambda p: (p.stem != FIRST_PROFILE, p.stem),
    )
    return _load_all(files)


def user_profiles(deploy_dir: Path) -> tuple[list[Profile], list[str]]:
    folder = deploy_dir.joinpath(*USER_PROFILES_DIR)
    return _load_all(sorted(folder.glob("*.json")) if folder.is_dir() else [])


def _load_all(files: list[Path]) -> tuple[list[Profile], list[str]]:
    loaded, problems = [], []
    for path in files:
        try:
            loaded.append(load_file(path))
        except ProfileError as exc:
            problems.append(str(exc))
    return loaded, problems


# --- the profiles in use ----------------------------------------------------------------------

_deploy_dir: Path | None = None
_override: list[Profile] | None = None
_cache: tuple[object, Merged] | None = None


def configure(deploy_dir: Path | None) -> None:
    """Says where the user's profiles are (the install's deploy directory); None, only the shipped
    ones. Called by the generator and the model tools before they read a role or an option.
    """
    global _deploy_dir, _cache
    _deploy_dir = deploy_dir
    _cache = None


def _stamp(files: list[Path]) -> tuple:
    out = []
    for path in files:
        try:
            st = path.stat()
            out.append((str(path), st.st_mtime_ns, st.st_size))
        except OSError:
            out.append((str(path), 0, 0))
    return tuple(out)


def merged() -> Merged:
    """The layers in use: loaded again when a profile file has changed."""
    global _cache
    if _override is not None:
        key: object = ("override", id(_override))
        if _cache is None or _cache[0] != key:
            _cache = (key, merge(_override))
        return _cache[1]
    user_dir = _deploy_dir.joinpath(*USER_PROFILES_DIR) if _deploy_dir else None
    files = sorted(PACKS_DIR.glob("*.json")) + (
        sorted(user_dir.glob("*.json"))
        if user_dir and user_dir.is_dir()
        else []
    )
    key = (str(_deploy_dir), _stamp(files))
    if _cache is None or _cache[0] != key:
        shipped, bad = shipped_profiles()
        user, bad_user = user_profiles(_deploy_dir) if _deploy_dir else ([], [])
        for problem in bad_user:
            print(
                f"warning: animation profile skipped: {problem}",
                file=sys.stderr,
            )
        _cache = (key, merge(shipped + user, tuple(bad + bad_user)))
    return _cache[1]


@contextmanager
def use_profiles(profiles: list[dict]):
    """For tests: only these profiles (as dicts, validated) are in use, whatever is installed."""
    global _override, _cache
    before = (_override, _cache)
    _override = [validate(p, f"<test {i}>") for i, p in enumerate(profiles)]
    _cache = None
    try:
        yield
    finally:
        _override, _cache = before


def clip_options(name: str) -> dict:
    """What a converted clip called `name` is given: the base options, then every rule whose
    pattern matches, in layer order."""
    options = {**BASE_OPTIONS, "extract": []}
    for patterns, rule in merged().rules:
        if any(fnmatch.fnmatchcase(name, p) for p in patterns):
            options.update(
                json.loads(json.dumps(rule))
            )  # (a copy: callers may change it)
    return options


def derived_clips(names: set[str] | list[str]) -> list[dict]:
    """The clips the profiles make of others, for the clips present: `{"name", "options"}`."""
    have = set(names)
    return [
        {"name": d["name"], "options": json.loads(json.dumps(d["options"]))}
        for d in merged().derived
        if set(d["requires"]) <= have
    ]
