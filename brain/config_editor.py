"""The graphical alternative to editing `config.toml` by hand (the Admin app's Settings page).

`describe()` turns the settings schema (brain/config.py) plus the file into groups of fields a
form can show; `apply()` validates a set of changes and writes them to the file with every
comment and the layout kept (tomlkit). It runs in the Admin app's own process, not the brain's,
so it still works when the brain is down, which is when a broken config most needs fixing.

Safety, in the order a change passes through it:

* A change must name a field this module offers. Secrets (API keys, tokens), the `[server]`
  and `[storage]` tables (a wrong value locks you out or moves your data) and `[language]`
  (it has its own page) are not editable here.
* The whole result is validated as `Settings` before anything is written; a refusal lists
  every problem and the file is untouched.
* The result is re-read and must equal the old file plus exactly the requested changes, so an
  editing slip cannot alter anything else.
* The file is replaced atomically with its permissions kept, after the previous version is
  copied to `config.toml.bak`. A change to the file since the page loaded it is refused.
* No secret is ever returned: a secret field shows only whether it is set (or its `env:` name).

The running brain notices the new file itself (its config watcher). Only the speech volume and
the bubble toggle take effect live; the rest are read at startup, which `apply()` reports.
"""

from __future__ import annotations

import copy
import enum
import hashlib
import os
import re
import shutil
import tempfile
import tomllib
import types
from pathlib import Path
from typing import Any, Literal, Union, get_args, get_origin

from pydantic import BaseModel, ValidationError

from brain.config import Settings

LOCKED_TABLES = {
    "server": "Network binding and authentication: a wrong value locks you out. Edit config.toml.",
    "storage": "Where your data lives: moving it needs the files moved too. Edit config.toml.",
}
HIDDEN_TABLES = {"language"}  # has its own page
SECRET_NAMES = re.compile(r"(^|_)(api_key|secret|password|token)$")
LIVE = {("voice", "tts", "volume"), ("presence", "show_speech_bubble")}

HELP: dict[str, str] = {
    "general.log_level": "How much the brain writes to its log.",
    "llm.local.base_url": "The local OpenAI-compatible server (vLLM, Ollama...).",
    "llm.local.model": "The model name that server serves.",
    "llm.local.context_tokens": "The model's real context window; skills size their prompt from it.",
    "llm.local.extra_body": "Extra fields sent with every request, as JSON.",
    "voice.tts.active_backend": "local = Piper only; remote = the engines below, Piper as the fallback.",
    "voice.tts.volume": "Companion voice level, 0 (silent) to 1 (full). Applies at once.",
    "voice.stt.local.model_size": "Whisper model. A name ending .en is English-only; use a plain name for other languages.",
    "voice.stt.silence_threshold_ms": "How long a pause ends your sentence in toggle mode.",
    "voice.lipsync_recognizer": "phonetic is fast and language-independent; pocketSphinx is English-only and slow.",
    "presence.show_speech_bubble": "Show the sentence being spoken next to the avatar. Applies at once.",
    "presence.avatar_width_px": "Avatar size. Restart the brain and the overlay after changing.",
    "presence.avatar_height_px": "Avatar size. Restart the brain and the overlay after changing.",
    "presence.idle_after_s": "Seconds without input before the companion treats you as away.",
    "shortcuts.push_to_talk_mode": "hold = talk while the keys are held; toggle = press to start and stop.",
    "personality.reflection_interval_s": "How often it reflects on recent chats (trait and mood drift, memories).",
    "personality.mood_half_life_s": "How quickly mood returns to its baseline.",
    "skills.enabled": "Off makes every reply plain dialogue with no actions.",
    "skills.disabled": "Skill names to switch off individually.",
    "status.units": "systemd user units shown as log panes in the Admin app.",
}


class ConfigEditError(ValueError):
    """Changes that cannot be saved, with the reasons in plain words."""


class ConfigConflict(ConfigEditError):
    """The file changed after the page loaded it."""


# --- reading the schema --------------------------------------------------------


def _unwrap(annotation: Any) -> tuple[Any, bool]:
    """`X | None` -> (X, True)."""
    origin = get_origin(annotation)
    if origin in (Union, types.UnionType):
        args = [a for a in get_args(annotation) if a is not type(None)]
        if len(args) == 1:
            return args[0], True
    return annotation, False


def _is_model_dict(annotation: Any) -> bool:
    inner, _ = _unwrap(annotation)
    if get_origin(inner) is dict:
        args = get_args(inner)
        return (
            len(args) == 2
            and isinstance(args[1], type)
            and issubclass(args[1], BaseModel)
        )
    return False


def _kind(annotation: Any) -> tuple[str, list[str] | None, bool]:
    """(kind, choices, nullable): bool | int | float | text | choice | list | json | unsupported."""
    inner, nullable = _unwrap(annotation)
    origin = get_origin(inner)
    if inner is bool:
        return "bool", None, nullable
    if inner is int:
        return "int", None, nullable
    if inner is float:
        return "float", None, nullable
    if inner is str:
        return "text", None, nullable
    if isinstance(inner, type) and issubclass(inner, enum.Enum):
        return "choice", [str(m.value) for m in inner], nullable
    if origin is Literal:
        return "choice", [str(a) for a in get_args(inner)], nullable
    if origin is list:
        item = get_args(inner)[0] if get_args(inner) else str
        return ("list" if item in (str, int) else "unsupported"), None, nullable
    if origin is dict or inner is dict:
        return "json", None, nullable
    return "unsupported", None, nullable


def _bounds(info: Any) -> dict[str, float]:
    out: dict[str, float] = {}
    for meta in info.metadata:
        for attr, key in (
            ("ge", "min"),
            ("gt", "min_exclusive"),
            ("le", "max"),
            ("lt", "max_exclusive"),
        ):
            if hasattr(meta, attr):
                out[key] = getattr(meta, attr)
    return out


def _json_value(value: Any) -> Any:
    if isinstance(value, enum.Enum):
        return value.value
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    return value


def _default(info: Any) -> Any:
    if info.is_required():
        return None  # no default: the setting must be given
    if info.default_factory is not None:
        return _json_value(info.default_factory())  # type: ignore[call-arg]
    return _json_value(info.default)


def _is_secret(name: str) -> bool:
    # api_key, auth_secret, token... but not context_tokens or auth_token_file (a path).
    return bool(SECRET_NAMES.search(name))


def _lookup(raw: dict, path: list[str]) -> tuple[bool, Any]:
    node: Any = raw
    for part in path:
        if not isinstance(node, dict) or part not in node:
            return False, None
        node = node[part]
    return True, node


def _restart_for(path: tuple[str, ...]) -> str:
    if path in LIVE:
        return "live"
    return "status" if path[0] == "status" else "brain"


def _groups(
    model: BaseModel, path: list[str], raw: dict, out: list[dict]
) -> None:
    fields: list[dict] = []
    children: list[tuple[list[str], BaseModel]] = []
    top = path[0] if path else ""
    locked_reason = LOCKED_TABLES.get(top)
    for name, info in type(model).model_fields.items():
        value = getattr(model, name)
        if isinstance(value, BaseModel):
            children.append((path + [name], value))
            continue
        if _is_model_dict(info.annotation):
            for key, sub in (value or {}).items():
                children.append((path + [name, key], sub))
            continue
        kind, choices, nullable = _kind(info.annotation)
        if kind == "unsupported":
            continue
        field_path = path + [name]
        dotted = ".".join(field_path)
        present, raw_value = _lookup(raw, field_path)
        secret = _is_secret(name)
        entry: dict[str, Any] = {
            "path": field_path,
            "key": name,
            "kind": kind,
            "set": present,
            "default": None if secret else _default(info),
            "choices": choices,
            "nullable": nullable,
            "help": HELP.get(dotted, ""),
            "apply": _restart_for(tuple(field_path)),
            "secret": secret,
            "locked": bool(locked_reason) or secret,
            "locked_reason": (
                locked_reason
                or (
                    "Secrets are not shown or edited here: use an env:NAME reference in config.toml."
                    if secret
                    else ""
                )
            ),
            **_bounds(info),
        }
        if secret:
            text = str(raw_value) if present and raw_value is not None else ""
            entry["value"] = text if text.startswith("env:") else ""
            entry["secret_set"] = bool(text)
        else:
            entry["value"] = _json_value(value)
        fields.append(entry)
    if fields or not children:
        out.append(
            {
                "path": path,
                "title": " › ".join(path) or "general",
                "locked": bool(locked_reason),
                "locked_reason": locked_reason or "",
                "fields": fields,
            }
        )
    for child_path, child in children:
        if child_path[0] in HIDDEN_TABLES:
            continue
        _groups(child, child_path, raw, out)


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def describe(path: Path) -> dict:
    """The file as groups of fields for a form."""
    try:
        text = path.read_text()
        raw = tomllib.loads(text)
        settings = Settings.model_validate(raw)
    except (
        OSError,
        tomllib.TOMLDecodeError,
        ValidationError,
        ValueError,
    ) as exc:
        raise ConfigEditError(
            f"the config file cannot be read: {exc}"
        ) from None
    groups: list[dict] = []
    for name in type(settings).model_fields:
        value = getattr(settings, name)
        if name in HIDDEN_TABLES or not isinstance(value, BaseModel):
            continue
        _groups(value, [name], raw, groups)
    return {
        "path": str(path),
        "hash": file_hash(path),
        "backup": path.with_name(path.name + ".bak").is_file(),
        "groups": groups,
    }


# --- applying changes ----------------------------------------------------------


def _index(path: Path) -> dict[tuple[str, ...], dict]:
    return {
        tuple(f["path"]): f
        for g in describe(path)["groups"]
        for f in g["fields"]
    }


def _coerce(field: dict, value: Any) -> Any:
    kind = field["kind"]
    label = ".".join(field["path"])
    if value is None and field["nullable"]:
        return None
    if kind == "bool" and isinstance(value, bool):
        return value
    if kind == "int" and isinstance(value, int) and not isinstance(value, bool):
        return value
    if (
        kind == "float"
        and isinstance(value, (int, float))
        and not isinstance(value, bool)
    ):
        return float(value)
    if kind == "text" and isinstance(value, str):
        return value
    if (
        kind == "choice"
        and isinstance(value, str)
        and value in (field["choices"] or [])
    ):
        return value
    if (
        kind == "list"
        and isinstance(value, list)
        and all(
            isinstance(i, (str, int)) and not isinstance(i, bool) for i in value
        )
    ):
        return value
    if kind == "json" and isinstance(value, dict):
        return value
    raise ConfigEditError(f"{label}: not a valid {kind} value")


def _set(doc: Any, path: list[str], value: Any, *, inline: bool) -> None:
    import tomlkit

    node = doc
    for part in path[:-1]:
        if part not in node:
            node[part] = tomlkit.table()
        node = node[part]
    if inline and isinstance(value, dict):
        item = tomlkit.inline_table()
        item.update(value)
        value = item
    node[path[-1]] = value


def _unset(doc: Any, path: list[str]) -> None:
    node = doc
    for part in path[:-1]:
        if part not in node:
            return
        node = node[part]
    if path[-1] in node:
        del node[path[-1]]


def _expected(raw: dict, changes: list[tuple[list[str], bool, Any]]) -> dict:
    out = copy.deepcopy(raw)
    for path, unset, value in changes:
        node = out
        for part in path[:-1]:
            node = node.setdefault(part, {})
        if unset:
            node.pop(path[-1], None)
        else:
            node[path[-1]] = copy.deepcopy(value)
    return out


def _problems(exc: ValidationError) -> str:
    return "; ".join(
        f"{'.'.join(str(p) for p in e['loc']) or 'settings'}: {e['msg'].removeprefix('Value error, ')}"
        for e in exc.errors()
    )


def apply(path: Path, changes: list[dict], base_hash: str) -> dict:
    """Validate `changes` ({path: [..], value: x} or {path: [..], unset: true}) and write them.

    Returns {"describe": ..., "applied": [{path, apply}, ...]}. Raises `ConfigConflict` if the
    file changed since `base_hash`, `ConfigEditError` for anything that cannot be saved.
    """
    import tomlkit

    path = path.resolve()
    if file_hash(path) != base_hash:
        raise ConfigConflict(
            "config.toml changed since this page loaded it (edited by hand or by another page); reload and redo the change"
        )
    if not changes:
        raise ConfigEditError("nothing to change")
    index = _index(path)
    text = path.read_text()
    raw = tomllib.loads(text)
    doc = tomlkit.parse(text)
    planned: list[tuple[list[str], bool, Any]] = []
    for change in changes:
        if not isinstance(change, dict) or not isinstance(
            change.get("path"), list
        ):
            raise ConfigEditError("each change needs a path")
        key = tuple(str(p) for p in change["path"])
        field = index.get(key)
        if field is None:
            raise ConfigEditError(
                f"{'.'.join(key)}: not a setting this page can change"
            )
        if field["locked"]:
            raise ConfigEditError(
                f"{'.'.join(key)}: {field['locked_reason'] or 'locked'}"
            )
        if change.get("unset"):
            planned.append((list(key), True, None))
            continue
        if "value" not in change:
            raise ConfigEditError(
                f"{'.'.join(key)}: a value or unset is needed"
            )
        planned.append((list(key), False, _coerce(field, change["value"])))
    seen = [tuple(p) for p, _, _ in planned]
    if len(set(seen)) != len(seen):
        raise ConfigEditError("the same setting is changed twice")

    for key_path, unset, value in planned:
        if unset:
            _unset(doc, key_path)
        else:
            _set(
                doc,
                key_path,
                value,
                inline=index[tuple(key_path)]["kind"] == "json",
            )
    new_text = tomlkit.dumps(doc)
    try:
        new_raw = tomllib.loads(new_text)
    except tomllib.TOMLDecodeError as exc:  # a writer bug, never a user error
        raise ConfigEditError(
            f"the edited file would not parse ({exc}); nothing was changed"
        ) from None
    if new_raw != _expected(raw, planned):
        raise ConfigEditError(
            "the edit would have changed more than asked; nothing was changed"
        )
    try:
        Settings.model_validate(new_raw)
    except ValidationError as exc:
        raise ConfigEditError(_problems(exc)) from None
    except ValueError as exc:
        raise ConfigEditError(str(exc)) from None

    mode = path.stat().st_mode & 0o777
    backup = path.with_name(path.name + ".bak")
    shutil.copy2(path, backup)
    os.chmod(backup, mode)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".config.")
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(new_text)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return {
        "describe": describe(path),
        "applied": [
            {"path": p, "apply": index[tuple(p)]["apply"], "unset": u}
            for p, u, _ in planned
        ],
    }


def restore_backup(path: Path) -> dict:
    """Put back the file as it was before the last save here (the .bak)."""
    path = path.resolve()
    backup = path.with_name(path.name + ".bak")
    if not backup.is_file():
        raise ConfigEditError("there is no earlier version to go back to")
    try:
        Settings.model_validate(tomllib.loads(backup.read_text()))
    except (tomllib.TOMLDecodeError, ValidationError, ValueError) as exc:
        raise ConfigEditError(
            f"the earlier version is not usable either: {exc}"
        ) from None
    mode = path.stat().st_mode & 0o777
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".config.")
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(backup.read_text())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return describe(path)
