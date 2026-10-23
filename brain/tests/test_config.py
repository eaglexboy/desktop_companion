"""Tests for brain.config: schema round-trips, hot-reload, graceful degradation."""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from pydantic import ValidationError

from brain.config import (
    MOOD_NAMES,
    TRAIT_NAMES,
    ConfigError,
    ConfigWatcher,
    LogLevel,
    Settings,
    TTSConfig,
)

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

MINIMAL_TOML = """
[storage]
workspace_dir = "/tmp/deskmate-test-workspace"
deploy_dir = "/tmp/deskmate-test-deploy"

[llm.local]
base_url = "http://127.0.0.1:11434/v1"
model = "test-model"
"""


def _write(tmp_path: Path, content: str, name: str = "config.toml") -> Path:
    path = tmp_path / name
    path.write_text(content)
    return path


# --- Schema round-trips ---------------------------------------------------


def test_minimal_config_parses_with_defaults(tmp_path: Path) -> None:
    path = _write(tmp_path, MINIMAL_TOML)
    settings = Settings.model_validate(_load_toml(path))

    assert settings.general.log_level == "INFO"
    assert settings.server.port == 8765
    assert settings.storage.workspace_dir == "/tmp/deskmate-test-workspace"
    assert settings.llm.local.model == "test-model"
    assert settings.llm.local.max_parallel_slots == 1  # reserved V2 key, parses, unused
    assert settings.presence.dbus_service_name == "org.deskmate.Presence"


def test_trait_and_mood_names_are_internal_constants_not_config() -> None:
    """TRAIT_NAMES/MOOD_NAMES deliberately aren't a PersonalityConfig field --
    see brain/config.py's comment. This just locks in the constant itself."""
    assert len(TRAIT_NAMES) == 13
    assert "honesty_humility" in TRAIT_NAMES
    assert MOOD_NAMES == ("energy", "valence", "stress")


def test_personality_config_rejects_trait_names_as_a_config_key(tmp_path: Path) -> None:
    toml = MINIMAL_TOML + '\n[personality]\ntrait_names = ["openness"]\n'
    path = _write(tmp_path, toml)
    with pytest.raises(ValidationError):
        Settings.model_validate(_load_toml(path))


def test_log_level_accepts_every_valid_level(tmp_path: Path) -> None:
    for level in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
        toml = MINIMAL_TOML + f'\n[general]\nlog_level = "{level}"\n'
        path = _write(tmp_path, toml, name=f"config-{level}.toml")
        settings = Settings.model_validate(_load_toml(path))
        assert settings.general.log_level == LogLevel(level)
        assert settings.general.log_level == level  # StrEnum compares equal to its raw string


def test_log_level_rejects_invalid_value(tmp_path: Path) -> None:
    toml = MINIMAL_TOML + '\n[general]\nlog_level = "NOTSHOUTY"\n'
    path = _write(tmp_path, toml)
    with pytest.raises(ValidationError):
        Settings.model_validate(_load_toml(path))


def test_stt_local_engine_rejects_unsupported_value(tmp_path: Path) -> None:
    toml = MINIMAL_TOML + '\n[voice.stt.local]\nengine = "faster_whisper"\n'  # underscore typo
    path = _write(tmp_path, toml)
    with pytest.raises(ValidationError):
        Settings.model_validate(_load_toml(path))


def test_tts_local_engine_rejects_unsupported_value(tmp_path: Path) -> None:
    toml = MINIMAL_TOML + '\n[voice.tts.local]\nengine = "espeak"\n'
    path = _write(tmp_path, toml)
    with pytest.raises(ValidationError):
        Settings.model_validate(_load_toml(path))


def test_repo_config_example_toml_round_trips() -> None:
    """The actual config.example.toml shipped in the repo must parse cleanly."""
    example_path = REPO_ROOT / "config.example.toml"
    settings = Settings.model_validate(_load_toml(example_path))

    assert settings.storage.workspace_dir
    assert settings.llm.local.model
    # The keyed voice.tts.remote table has at least one real example entry
    assert settings.voice.tts.remote
    assert any(entry.enabled for entry in settings.voice.tts.remote.values())


def test_keyed_llm_cloud_table_with_multiple_providers(tmp_path: Path) -> None:
    toml = (
        MINIMAL_TOML
        + """
[llm.cloud.alpha]
provider_type = "generic_openai"
base_url = "https://alpha.example.com/v1"
model = "alpha-model"
api_key = "sk-alpha"
priority = 5
enabled = true

[llm.cloud.beta]
provider_type = "anthropic"
base_url = "https://beta.example.com"
model = "beta-model"
api_key = "sk-beta"
priority = 50
enabled = true

[llm.cloud.disabled_one]
provider_type = "generic_openai"
base_url = "https://disabled.example.com/v1"
model = "x"
api_key = "sk-x"
priority = 999
enabled = false
"""
    )
    path = _write(tmp_path, toml)
    settings = Settings.model_validate(_load_toml(path))

    assert set(settings.llm.cloud) == {"alpha", "beta", "disabled_one"}
    enabled = settings.llm.enabled_cloud_providers()
    assert [p.provider_type for p in enabled] == ["anthropic", "generic_openai"]  # beta (50) before alpha (5)


def test_keyed_tts_remote_table_with_multiple_instances_priority_order(tmp_path: Path) -> None:
    toml = (
        MINIMAL_TOML
        + """
[voice.tts.remote.voxcpm_elsrig]
base_url = "http://127.0.0.1:7500"
priority = 10
enabled = true

[voice.tts.remote.indextts_elsairig]
base_url = "http://192.168.0.21:7500"
priority = 20
enabled = true

[voice.tts.remote.disabled_one]
base_url = "http://example.com:7500"
priority = 1
enabled = false
"""
    )
    path = _write(tmp_path, toml)
    settings = Settings.model_validate(_load_toml(path))

    assert len(settings.voice.tts.remote) == 3
    enabled = settings.voice.tts.enabled_remote_instances()
    # Higher priority number tried first -- matches [llm.cloud.<provider_name>]'s
    # convention exactly, since this keyed table mirrors it (priority=20 before priority=10).
    assert [r.base_url for r in enabled] == ["http://192.168.0.21:7500", "http://127.0.0.1:7500"]


def test_tts_remote_provider_type_defaults_to_tts_serve(tmp_path: Path) -> None:
    toml = (
        MINIMAL_TOML
        + """
[voice.tts.remote.voxcpm_elsrig]
base_url = "http://127.0.0.1:7500"
"""
    )
    path = _write(tmp_path, toml)
    settings = Settings.model_validate(_load_toml(path))
    assert settings.voice.tts.remote["voxcpm_elsrig"].provider_type == "tts_serve"


def test_tts_remote_generic_openai_provider_type_with_api_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DESKMATE_TTS_TEST_KEY", "sk-tts-resolved")
    toml = (
        MINIMAL_TOML
        + """
[voice.tts.remote.openai_cloud]
provider_type = "generic_openai"
base_url = "https://api.openai.com"
endpoint = "/v1/audio/speech"
capabilities_endpoint = ""
api_key = "env:DESKMATE_TTS_TEST_KEY"
params = { model = "tts-1", voice = "alloy" }
"""
    )
    path = _write(tmp_path, toml)
    settings = Settings.model_validate(_load_toml(path))
    entry = settings.voice.tts.remote["openai_cloud"]
    assert entry.provider_type == "generic_openai"
    assert entry.capabilities_endpoint == ""
    assert entry.api_key == "sk-tts-resolved"
    assert entry.params == {"model": "tts-1", "voice": "alloy"}


def test_tts_remote_invalid_provider_type_rejected(tmp_path: Path) -> None:
    toml = (
        MINIMAL_TOML
        + """
[voice.tts.remote.bogus]
provider_type = "some_other_engine"
base_url = "http://example.com:7500"
"""
    )
    path = _write(tmp_path, toml)
    with pytest.raises(ValidationError):
        Settings.model_validate(_load_toml(path))


def test_tts_remote_disabled_instance_unset_env_var_does_not_block_startup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DESKMATE_TTS_TOTALLY_UNSET_VAR", raising=False)
    toml = (
        MINIMAL_TOML
        + """
[voice.tts.remote.disabled_cloud]
provider_type = "generic_openai"
base_url = "https://api.openai.com"
enabled = false
api_key = "env:DESKMATE_TTS_TOTALLY_UNSET_VAR"
"""
    )
    path = _write(tmp_path, toml)
    settings = Settings.model_validate(_load_toml(path))  # must not raise
    assert settings.voice.tts.remote["disabled_cloud"].api_key == "env:DESKMATE_TTS_TOTALLY_UNSET_VAR"


def test_classifier_cascades_from_llm_local_when_blank(tmp_path: Path) -> None:
    path = _write(tmp_path, MINIMAL_TOML)
    settings = Settings.model_validate(_load_toml(path))

    resolved = settings.llm.resolved_classifier()
    assert resolved.base_url == settings.llm.local.base_url
    assert resolved.model == settings.llm.local.model


def test_classifier_explicit_values_not_overridden(tmp_path: Path) -> None:
    toml = (
        MINIMAL_TOML
        + """
[llm.routing.classifier]
base_url = "http://classifier.example.com/v1"
model = "small-fast"
timeout_s = 2.0
"""
    )
    path = _write(tmp_path, toml)
    settings = Settings.model_validate(_load_toml(path))

    resolved = settings.llm.resolved_classifier()
    assert resolved.base_url == "http://classifier.example.com/v1"
    assert resolved.model == "small-fast"
    assert resolved.timeout_s == 2.0


def test_env_var_api_key_resolved(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DESKMATE_TEST_KEY", "sk-resolved-value")
    toml = (
        MINIMAL_TOML
        + """
[llm.cloud.cloudy]
provider_type = "anthropic"
base_url = "https://example.com"
model = "m"
api_key = "env:DESKMATE_TEST_KEY"
"""
    )
    path = _write(tmp_path, toml)
    settings = Settings.model_validate(_load_toml(path))
    assert settings.llm.cloud["cloudy"].api_key == "sk-resolved-value"


def test_env_var_api_key_missing_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DESKMATE_TOTALLY_UNSET_VAR", raising=False)
    toml = (
        MINIMAL_TOML
        + """
[llm.cloud.cloudy]
provider_type = "anthropic"
base_url = "https://example.com"
model = "m"
api_key = "env:DESKMATE_TOTALLY_UNSET_VAR"
"""
    )
    path = _write(tmp_path, toml)
    with pytest.raises(ValidationError):
        Settings.model_validate(_load_toml(path))


def test_stt_remote_api_key_resolved_when_backend_is_remote(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DESKMATE_STT_TEST_KEY", "sk-stt-resolved")
    toml = (
        MINIMAL_TOML
        + """
[voice.stt]
active_backend = "remote"

[voice.stt.remote]
api_key = "env:DESKMATE_STT_TEST_KEY"
"""
    )
    path = _write(tmp_path, toml)
    settings = Settings.model_validate(_load_toml(path))
    assert settings.voice.stt.remote.api_key == "sk-stt-resolved"


def test_stt_remote_unset_env_var_does_not_block_startup_when_backend_is_local(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mirrors the LLMCloudConfig fix: an unused env:VAR placeholder for a
    backend that isn't selected must never block the whole daemon from
    starting."""
    monkeypatch.delenv("DESKMATE_STT_TOTALLY_UNSET_VAR", raising=False)
    toml = (
        MINIMAL_TOML
        + """
[voice.stt]
active_backend = "local"

[voice.stt.remote]
api_key = "env:DESKMATE_STT_TOTALLY_UNSET_VAR"
"""
    )
    path = _write(tmp_path, toml)
    settings = Settings.model_validate(_load_toml(path))  # must not raise
    assert settings.voice.stt.remote.api_key == "env:DESKMATE_STT_TOTALLY_UNSET_VAR"


def test_stt_remote_unset_env_var_raises_when_backend_is_remote(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DESKMATE_STT_TOTALLY_UNSET_VAR", raising=False)
    toml = (
        MINIMAL_TOML
        + """
[voice.stt]
active_backend = "remote"

[voice.stt.remote]
api_key = "env:DESKMATE_STT_TOTALLY_UNSET_VAR"
"""
    )
    path = _write(tmp_path, toml)
    with pytest.raises(ValidationError):
        Settings.model_validate(_load_toml(path))


def test_unknown_key_rejected_loudly(tmp_path: Path) -> None:
    toml = MINIMAL_TOML + "\n[general]\nlog_level = \"INFO\"\ntypo_field = true\n"
    path = _write(tmp_path, toml)
    with pytest.raises(ValidationError):
        Settings.model_validate(_load_toml(path))


def test_appearance_cooldown_range_validated(tmp_path: Path) -> None:
    toml = (
        MINIMAL_TOML
        + """
[appearance]
redesign_cooldown_hours_min = 30
redesign_cooldown_hours_max = 12
"""
    )
    path = _write(tmp_path, toml)
    with pytest.raises(ValidationError):
        Settings.model_validate(_load_toml(path))


# --- ConfigWatcher: load, hot-reload, graceful degradation ----------------


def test_config_watcher_loads_initial_settings(tmp_path: Path) -> None:
    path = _write(tmp_path, MINIMAL_TOML)
    watcher = ConfigWatcher(path)
    assert watcher.settings.llm.local.model == "test-model"


def test_config_watcher_missing_file_raises_config_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        ConfigWatcher(tmp_path / "does_not_exist.toml")


def test_config_watcher_reload_picks_up_changes(tmp_path: Path) -> None:
    path = _write(tmp_path, MINIMAL_TOML)
    watcher = ConfigWatcher(path)
    assert watcher.settings.llm.local.model == "test-model"

    path.write_text(MINIMAL_TOML.replace("test-model", "updated-model"))
    watcher.reload()

    assert watcher.settings.llm.local.model == "updated-model"


def test_config_watcher_reload_on_invalid_toml_keeps_last_good(tmp_path: Path) -> None:
    path = _write(tmp_path, MINIMAL_TOML)
    watcher = ConfigWatcher(path)
    good_settings = watcher.settings

    path.write_text("this is not [ valid toml")
    watcher.reload()

    assert watcher.settings is good_settings
    assert watcher.settings.llm.local.model == "test-model"


def test_config_watcher_reload_on_validation_failure_keeps_last_good(tmp_path: Path) -> None:
    path = _write(tmp_path, MINIMAL_TOML)
    watcher = ConfigWatcher(path)
    good_settings = watcher.settings

    # storage.workspace_dir is required -- remove it to force a validation error.
    path.write_text(
        """
[storage]
deploy_dir = "/tmp/deskmate-test-deploy"

[llm.local]
base_url = "http://127.0.0.1:11434/v1"
model = "test-model"
"""
    )
    watcher.reload()

    assert watcher.settings is good_settings


def test_config_watcher_subscribers_notified_on_successful_reload(tmp_path: Path) -> None:
    path = _write(tmp_path, MINIMAL_TOML)
    watcher = ConfigWatcher(path)
    seen: list[Settings] = []
    watcher.subscribe(seen.append)

    path.write_text(MINIMAL_TOML.replace("test-model", "updated-model"))
    watcher.reload()

    assert len(seen) == 1
    assert seen[0].llm.local.model == "updated-model"


def test_config_watcher_unsubscribe_stops_notifications(tmp_path: Path) -> None:
    path = _write(tmp_path, MINIMAL_TOML)
    watcher = ConfigWatcher(path)
    seen: list[Settings] = []
    watcher.subscribe(seen.append)
    watcher.unsubscribe(seen.append)
    watcher.unsubscribe(seen.append)  # unknown callback: harmless

    path.write_text(MINIMAL_TOML.replace("test-model", "updated-model"))
    watcher.reload()

    assert seen == []


def test_subscribe_if_running_is_a_noop_without_a_watcher(monkeypatch) -> None:
    import brain.config as config_module

    monkeypatch.setattr(config_module, "_watcher", None)
    calls: list[Settings] = []

    unsubscribe = config_module.subscribe_if_running(calls.append)

    assert config_module._watcher is None  # must not start one (or read a real config)
    unsubscribe()  # callable, does nothing
    assert calls == []


def test_subscribe_if_running_subscribes_and_unsubscribes(tmp_path: Path, monkeypatch) -> None:
    import brain.config as config_module

    path = _write(tmp_path, MINIMAL_TOML)
    watcher = ConfigWatcher(path)
    monkeypatch.setattr(config_module, "_watcher", watcher)
    seen: list[Settings] = []

    unsubscribe = config_module.subscribe_if_running(seen.append)
    watcher.reload()
    assert len(seen) == 1

    unsubscribe()
    watcher.reload()
    assert len(seen) == 1


@pytest.mark.parametrize("volume", [0.0, 0.5, 1.0])
def test_tts_volume_accepts_the_zero_to_one_range(volume: float) -> None:
    assert TTSConfig(volume=volume).volume == volume


@pytest.mark.parametrize("volume", [-0.1, 1.01, 2.0])
def test_tts_volume_outside_zero_to_one_is_rejected(volume: float) -> None:
    with pytest.raises(ValueError):
        TTSConfig(volume=volume)


def test_config_watcher_subscribers_not_notified_on_failed_reload(tmp_path: Path) -> None:
    path = _write(tmp_path, MINIMAL_TOML)
    watcher = ConfigWatcher(path)
    seen: list[Settings] = []
    watcher.subscribe(seen.append)

    path.write_text("not valid toml [[[")
    watcher.reload()

    assert seen == []


def test_config_watcher_live_filesystem_event_triggers_reload(tmp_path: Path) -> None:
    """End-to-end: a real watchdog Observer picks up a real file write, no manual reload() call."""
    path = _write(tmp_path, MINIMAL_TOML)
    watcher = ConfigWatcher(path)
    watcher.start()
    try:
        path.write_text(MINIMAL_TOML.replace("test-model", "updated-model"))
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if watcher.settings.llm.local.model == "updated-model":
                break
            time.sleep(0.05)
        assert watcher.settings.llm.local.model == "updated-model"
    finally:
        watcher.stop()


# --- helpers ---------------------------------------------------------------


def _load_toml(path: Path) -> dict:
    import tomllib

    with path.open("rb") as f:
        return tomllib.load(f)


def test_presence_avatar_size_defaults_and_bounds():
    from pydantic import ValidationError

    from brain.config import PresenceConfig

    cfg = PresenceConfig()
    assert (cfg.avatar_width_px, cfg.avatar_height_px) == (240, 240)
    assert cfg.show_speech_bubble is True
    assert PresenceConfig(avatar_width_px=96, avatar_height_px=1024).avatar_width_px == 96
    for bad in (0, 95, 1025, -240):
        with pytest.raises(ValidationError):
            PresenceConfig(avatar_width_px=bad)
        with pytest.raises(ValidationError):
            PresenceConfig(avatar_height_px=bad)
    assert PresenceConfig(show_speech_bubble=False).show_speech_bubble is False


def test_voice_lipsync_recognizer_defaults_to_the_fast_one_and_rejects_typos():
    from brain.config import VoiceConfig

    assert VoiceConfig().lipsync_recognizer == "phonetic"
    assert VoiceConfig(lipsync_recognizer="pocketSphinx").lipsync_recognizer == "pocketSphinx"
    with pytest.raises(ValidationError):
        VoiceConfig(lipsync_recognizer="pocketsphynx")
