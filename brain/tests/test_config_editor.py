"""The graphical alternative to editing config.toml (brain/config_editor.py)."""

from __future__ import annotations

import json
import os
import stat
import tomllib

import pytest

from brain import config_editor as ce

CONFIG = """\
# Deskmate test config -- this comment must survive every edit.
[general]
log_level = "INFO"   # trailing comment stays

[storage]
workspace_dir = "/tmp/ws"
deploy_dir = "/tmp/deploy"

[llm.local]
base_url = "http://127.0.0.1:11434/v1"
model = "test-model"

[llm.cloud.claude]
provider_type = "anthropic"
base_url = "https://api.anthropic.com"
model = "claude-x"
api_key = "sk-super-secret-value"
enabled = false

[voice.tts]
volume = 1.0

[voice.tts.remote.pocket]
base_url = "http://127.0.0.1:7501"
params = { seed = 1 }   # pinned by ear
languages = ["en"]

[presence]
dbus_service_name = ""
show_speech_bubble = true

# skills left at their defaults
"""


@pytest.fixture
def cfg(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(CONFIG)
    os.chmod(path, 0o600)
    return path


def _field(d, *path):
    return next(
        f for g in d["groups"] for f in g["fields"] if f["path"] == list(path)
    )


def test_describe_offers_every_table_with_values_and_defaults(cfg):
    d = ce.describe(cfg)
    titles = [g["title"] for g in d["groups"]]
    for expected in (
        "general",
        "llm › local",
        "llm › cloud › claude",
        "voice › tts",
        "voice › tts › remote › pocket",
        "presence",
        "skills",
    ):
        assert expected in titles
    assert "language" not in " ".join(titles)  # has its own page
    vol = _field(d, "voice", "tts", "volume")
    assert (vol["value"], vol["default"], vol["set"], vol["apply"]) == (
        1.0,
        1.0,
        True,
        "live",
    )
    assert vol["min"] == 0.0 and vol["max"] == 1.0
    skills = _field(d, "skills", "max_actions_per_reply")
    assert skills["set"] is False and skills["value"] == 3
    level = _field(d, "general", "log_level")
    assert level["kind"] == "choice" and "DEBUG" in level["choices"]
    assert (
        _field(d, "voice", "tts", "remote", "pocket", "params")["kind"]
        == "json"
    )
    assert _field(d, "voice", "tts", "remote", "pocket", "languages")[
        "value"
    ] == ["en"]


def test_secrets_are_never_returned(cfg):
    d = ce.describe(cfg)
    blob = json.dumps(d)
    assert "sk-super-secret-value" not in blob
    key = _field(d, "llm", "cloud", "claude", "api_key")
    assert key["locked"] and key["secret_set"] is True and key["value"] == ""


def test_server_and_storage_are_locked(cfg):
    d = ce.describe(cfg)
    assert all(
        g["locked"]
        for g in d["groups"]
        if g["path"][0] in ("server", "storage")
    )
    with pytest.raises(ce.ConfigEditError, match="storage.deploy_dir"):
        ce.apply(
            cfg,
            [{"path": ["storage", "deploy_dir"], "value": "/elsewhere"}],
            d["hash"],
        )
    with pytest.raises(ce.ConfigEditError, match="api_key"):
        ce.apply(
            cfg,
            [{"path": ["llm", "cloud", "claude", "api_key"], "value": "x"}],
            d["hash"],
        )
    assert cfg.read_text() == CONFIG


def test_apply_changes_values_and_keeps_everything_else(cfg):
    d = ce.describe(cfg)
    result = ce.apply(
        cfg,
        [
            {"path": ["voice", "tts", "volume"], "value": 0.5},
            {"path": ["general", "log_level"], "value": "DEBUG"},
            {
                "path": ["voice", "tts", "remote", "pocket", "params"],
                "value": {"seed": 7},
            },
            {
                "path": ["voice", "tts", "remote", "pocket", "languages"],
                "value": ["en", "es"],
            },
            {"path": ["skills", "max_actions_per_reply"], "value": 5},
        ],
        d["hash"],
    )
    text = cfg.read_text()
    # Comments and layout are kept.
    assert (
        "# Deskmate test config -- this comment must survive every edit."
        in text
    )
    assert "# trailing comment stays" in text and "# pinned by ear" in text
    assert "# skills left at their defaults" in text
    raw = tomllib.loads(text)
    assert (
        raw["voice"]["tts"]["volume"] == 0.5
        and raw["general"]["log_level"] == "DEBUG"
    )
    assert raw["voice"]["tts"]["remote"]["pocket"]["params"] == {"seed": 7}
    assert raw["voice"]["tts"]["remote"]["pocket"]["languages"] == ["en", "es"]
    assert raw["skills"]["max_actions_per_reply"] == 5
    assert (
        raw["llm"]["cloud"]["claude"]["api_key"] == "sk-super-secret-value"
    )  # untouched
    # Reported per change: live vs needs a restart.
    applied = {tuple(a["path"]): a["apply"] for a in result["applied"]}
    assert applied[("voice", "tts", "volume")] == "live"
    assert applied[("general", "log_level")] == "brain"
    # The result of the save is the new state, with a new hash.
    assert result["describe"]["hash"] != d["hash"]
    assert (
        _field(result["describe"], "skills", "max_actions_per_reply")["set"]
        is True
    )


def test_unset_returns_a_setting_to_its_default(cfg):
    d = ce.describe(cfg)
    ce.apply(
        cfg, [{"path": ["general", "log_level"], "unset": True}], d["hash"]
    )
    assert "log_level" not in tomllib.loads(cfg.read_text())["general"]
    assert _field(ce.describe(cfg), "general", "log_level")["set"] is False


def test_invalid_values_are_refused_with_the_reason_and_write_nothing(cfg):
    d = ce.describe(cfg)
    for change, match in (
        ({"path": ["voice", "tts", "volume"], "value": 1.5}, "volume"),
        (
            {"path": ["general", "log_level"], "value": "LOUD"},
            "not a valid choice",
        ),
        (
            {"path": ["skills", "max_actions_per_reply"], "value": "many"},
            "not a valid int",
        ),
        (
            {"path": ["skills", "max_actions_per_reply"], "value": 99},
            "max_actions_per_reply",
        ),
        ({"path": ["nope", "nothing"], "value": 1}, "not a setting"),
    ):
        with pytest.raises(ce.ConfigEditError, match=match):
            ce.apply(cfg, [change], d["hash"])
    assert cfg.read_text() == CONFIG
    assert not cfg.with_name("config.toml.bak").exists()


def test_a_file_changed_since_it_was_loaded_is_refused(cfg):
    d = ce.describe(cfg)
    cfg.write_text(CONFIG + "\n# someone edited by hand\n")
    with pytest.raises(ce.ConfigConflict):
        ce.apply(
            cfg, [{"path": ["voice", "tts", "volume"], "value": 0.2}], d["hash"]
        )


def test_save_keeps_a_backup_and_the_file_mode(cfg):
    d = ce.describe(cfg)
    ce.apply(
        cfg, [{"path": ["voice", "tts", "volume"], "value": 0.3}], d["hash"]
    )
    backup = cfg.with_name("config.toml.bak")
    assert backup.read_text() == CONFIG
    assert (
        stat.S_IMODE(cfg.stat().st_mode) == 0o600
        and stat.S_IMODE(backup.stat().st_mode) == 0o600
    )
    restored = ce.restore_backup(cfg)
    assert cfg.read_text() == CONFIG
    assert _field(restored, "voice", "tts", "volume")["value"] == 1.0


def test_restore_without_a_backup(cfg):
    with pytest.raises(ce.ConfigEditError, match="no earlier version"):
        ce.restore_backup(cfg)


def test_duplicate_and_empty_changes_are_refused(cfg):
    d = ce.describe(cfg)
    with pytest.raises(ce.ConfigEditError, match="nothing to change"):
        ce.apply(cfg, [], d["hash"])
    twice = {"path": ["voice", "tts", "volume"], "value": 0.4}
    with pytest.raises(ce.ConfigEditError, match="twice"):
        ce.apply(cfg, [twice, twice], d["hash"])


def test_an_unreadable_config_is_reported(tmp_path):
    bad = tmp_path / "config.toml"
    bad.write_text("[general\nbroken")
    with pytest.raises(ce.ConfigEditError, match="cannot be read"):
        ce.describe(bad)


def test_only_real_secrets_are_hidden(cfg):
    d = ce.describe(cfg)
    assert _field(d, "llm", "cloud", "claude", "api_key")["secret"] is True
    # "context_tokens" is a number of tokens, not a credential.
    assert _field(d, "llm", "local", "context_tokens")["secret"] is False
    assert _field(d, "llm", "local", "context_tokens")["locked"] is False
