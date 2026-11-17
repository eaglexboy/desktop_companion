"""The Admin app's Language settings page: the character's language fields and the
detection thresholds, editable without touching `config.toml`.

`config.toml` stays the default (docs/ADR_PERSONALITY_PROFILES.md: "Nothing writes
config.toml"). What the page saves is one JSON file in the deploy directory holding a
whole `LanguageConfig`; while it exists it wins over the config file, and removing it
("reset") returns to the config file's values. These are the same keys a personality
profile will carry under `language`, so a profile loader can later read and write them
in place of this file.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from pydantic import ValidationError

from brain.config import LanguageConfig, Settings

FILE_NAME = "language_settings.json"


class LanguageSettingsError(ValueError):
    """A settings document that cannot be saved, with the reasons in plain words."""


def path_for(deploy_dir: Path) -> Path:
    return deploy_dir / FILE_NAME


def load_saved(deploy_dir: Path) -> LanguageConfig | None:
    """The page's saved settings, or None when none are saved (or the file is unusable:
    a damaged file must not stop the brain starting, so the config file's values apply).
    """
    path = path_for(deploy_dir)
    if not path.is_file():
        return None
    try:
        return LanguageConfig.model_validate(json.loads(path.read_text()))
    except (OSError, ValueError, ValidationError):
        return None


def effective(settings: Settings) -> LanguageConfig:
    """What the brain runs with: the saved settings if any, else `config.toml`'s."""
    return load_saved(settings.deploy_dir) or settings.language


def parse(document: object) -> LanguageConfig:
    """Validate a document from the page. Raises `LanguageSettingsError` listing every problem."""
    if not isinstance(document, dict):
        raise LanguageSettingsError("a JSON object is expected")
    try:
        config = LanguageConfig.model_validate(document)
    except ValidationError as exc:
        raise LanguageSettingsError(
            "; ".join(
                f"{'.'.join(str(p) for p in e['loc']) or 'settings'}: {e['msg'].removeprefix('Value error, ')}"
                for e in exc.errors()
            )
        ) from None
    if config.wording_floor > config.native_threshold:
        raise LanguageSettingsError(
            "wording_floor: must not be above native_threshold"
        )
    return config


def save(deploy_dir: Path, config: LanguageConfig) -> None:
    """Write atomically: a failed write leaves the previous file intact."""
    deploy_dir.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=deploy_dir, prefix=".language_settings.")
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(config.model_dump(mode="json"), handle, indent=2)
        os.replace(tmp, path_for(deploy_dir))
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def reset(deploy_dir: Path) -> bool:
    """Forget the saved settings so `config.toml` applies again. True if there were any."""
    path = path_for(deploy_dir)
    if not path.is_file():
        return False
    path.unlink()
    return True


def describe(settings: Settings, pin: str | None, speakable: list[str]) -> dict:
    """Everything the page needs in one answer."""
    saved = load_saved(settings.deploy_dir)
    return {
        "settings": (saved or settings.language).model_dump(mode="json"),
        "config_toml": settings.language.model_dump(mode="json"),
        "source": "page" if saved else "config.toml",
        "pin": pin,
        "speakable_languages": speakable,
    }
