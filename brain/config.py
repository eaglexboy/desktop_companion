"""Deskmate brain configuration: TOML schema, loading, and hot-reload.

No machine-specific values live in this repository. Every path, host, and
secret comes from an external TOML file, by default
`$XDG_CONFIG_HOME/deskmate/config.toml` (overridable via
`DESKMATE_CONFIG_FILE`). `config.example.toml` is the template; copy it
there and fill it in.
"""

from __future__ import annotations

import logging
import os
import threading
import tomllib
from collections.abc import Callable
from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    model_validator,
)
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer
from watchdog.observers.api import BaseObserver

logger = logging.getLogger(__name__)


class ConfigError(Exception):
    """Raised when config.toml is missing, malformed, or fails validation."""


def _resolve_secret(value: str) -> str:
    """Resolve an "env:VAR_NAME" indirection to the real environment value.

    A plain string (including an empty one, or an obvious placeholder like
    "REPLACE_ME_sk-ant-...") passes through unchanged -- only the "env:"
    prefix triggers a lookup. Raises ValueError (not ConfigError) so pydantic
    collects it into the surrounding ValidationError.
    """
    if value.startswith("env:"):
        var_name = value[len("env:") :]
        resolved = os.environ.get(var_name)
        if resolved is None:
            raise ValueError(f"environment variable '{var_name}' referenced by config is not set")
        return resolved
    return value


class StrictModel(BaseModel):
    """Base for every config table: unknown keys are a loud error, not a silent drop."""

    model_config = ConfigDict(extra="forbid")


class LogLevel(StrEnum):
    """Matches the standard library's named logging levels exactly, so
    LogLevel.INFO.value is always a valid argument to logging.getLogger(...).setLevel()."""

    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


class GeneralConfig(StrictModel):
    log_level: LogLevel = LogLevel.INFO
    log_to_stdout: bool = True


class ServerConfig(StrictModel):
    host: str = "127.0.0.1"
    port: int = 8765
    allowed_origins: list[str] = Field(default_factory=list)
    # Reserved for a future network/remote-access auth mechanism -- not consumed yet.
    auth_token_file: str = ""
    tls_cert: str = ""
    tls_key: str = ""


class StorageConfig(StrictModel):
    workspace_dir: str
    deploy_dir: str
    # Where deploy/backup_state.sh mirrors deploy_dir (a path on a different disk or remote mount). Read by that
    # script, not by the daemon; blank = the script's same-machine fallback, with a warning.
    backup_dir: str = ""


class LLMLocalConfig(StrictModel):
    base_url: str
    model: str
    timeout_s: float = 30.0
    # Reserved for a future inference-concurrency governor -- parsed now,
    # not consumed yet.
    max_parallel_slots: int = 1
    # The model's context window in tokens. Skills (and, later, session compaction) size themselves from it,
    # so set it to the real value for the loaded/selected model. The same field exists on every backend.
    context_tokens: int = Field(default=8192, ge=512)
    extra_body: dict = Field(default_factory=dict)
    # Let the model think before it answers. Turns thinking back on (overriding
    # an `enable_thinking = false` in extra_body) and adds `reasoning_extra_tokens`
    # to each call's max_tokens, since thinking spends from that budget. The
    # reasoning is never spoken or stored as chat; it is only streamed to the
    # status page's Thinking tab. Replies get slower -- off by default.
    reasoning: bool = False
    reasoning_extra_tokens: int = Field(default=2048, ge=0, le=32768)


class LLMCloudConfig(StrictModel):
    """One entry of the keyed [llm.cloud.<provider_name>] table."""

    provider_type: str
    base_url: str
    model: str
    api_key: str
    timeout_s: float = 60.0
    # Reserved for a future inference-concurrency governor -- parsed now,
    # not consumed yet.
    max_concurrency: int = 4
    # The model's context window in tokens. Skills (and, later, session compaction) size themselves from it,
    # so set it to the real value for the loaded/selected model. The same field exists on every backend.
    context_tokens: int = Field(default=8192, ge=512)
    capabilities: list[str] = Field(default_factory=list)
    priority: int = 100
    enabled: bool = True

    @model_validator(mode="after")
    def _resolve_api_key_if_enabled(self) -> LLMCloudConfig:
        # Only resolve (and require) a real secret for providers actually in
        # use -- a disabled provider's placeholder "env:..." value must not
        # block the whole daemon from starting.
        if self.enabled:
            self.api_key = _resolve_secret(self.api_key)
        return self


class ClassifierConfig(StrictModel):
    """[llm.routing.classifier]. Blank fields cascade to [llm.local]'s values --
    see LLMConfig.resolved_classifier()."""

    base_url: str = ""
    model: str = ""
    timeout_s: float = 0.0
    extra_body: dict = Field(default_factory=dict)


class LLMRoutingConfig(StrictModel):
    escalation_threshold: Literal["auto", "always_local", "always_cloud"] = "auto"
    classification_timeout_s: float = 3.0
    classifier: ClassifierConfig = Field(default_factory=ClassifierConfig)


class LLMConfig(StrictModel):
    local: LLMLocalConfig
    cloud: dict[str, LLMCloudConfig] = Field(default_factory=dict)
    routing: LLMRoutingConfig = Field(default_factory=LLMRoutingConfig)

    def smallest_context_tokens(self) -> int:
        """The smallest window among the backends that may answer (local and enabled cloud). Prompt parts that
        must fit wherever the router sends the turn are sized to this."""
        return min([self.local.context_tokens, *(p.context_tokens for p in self.enabled_cloud_providers())])

    def enabled_cloud_providers(self) -> list[LLMCloudConfig]:
        """Enabled cloud providers, highest priority first."""
        return sorted((p for p in self.cloud.values() if p.enabled), key=lambda p: -p.priority)

    def resolved_classifier(self) -> ClassifierConfig:
        """Classifier settings with blank fields cascaded from [llm.local]."""
        c = self.routing.classifier
        return ClassifierConfig(
            base_url=c.base_url or self.local.base_url,
            model=c.model or self.local.model,
            timeout_s=c.timeout_s or self.routing.classification_timeout_s or self.local.timeout_s,
            extra_body=c.extra_body or self.local.extra_body,
        )


class STTLocalConfig(StrictModel):
    # Only faster-whisper is currently implemented (brain/voice/stt_local.py
    # wraps it directly) -- a named field rather than removed since more
    # local engines may be added later, same pattern as TTSLocalConfig.engine
    # below. Literal (not bare str) so a typo is a loud config error rather
    # than an unrecognized-engine error surfacing deep in the STT code path.
    engine: Literal["faster-whisper"] = "faster-whisper"
    model_size: str = "base.en"
    device: str = "auto"
    compute_type: str = "int8"


class STTRemoteConfig(StrictModel):
    base_url: str = ""
    endpoint: str = "/v1/audio/transcriptions"
    model: str = "whisper-1"
    timeout_s: float = 10.0
    api_key: str = ""
    params: dict = Field(default_factory=dict)


class STTConfig(StrictModel):
    active_backend: Literal["local", "remote"] = "local"
    silence_threshold_ms: int = 800
    local: STTLocalConfig = Field(default_factory=STTLocalConfig)
    remote: STTRemoteConfig = Field(default_factory=STTRemoteConfig)

    @model_validator(mode="after")
    def _resolve_remote_api_key_if_active(self) -> STTConfig:
        # Mirrors LLMCloudConfig's same fix: only resolve (and require) a
        # real secret when the remote STT backend is actually selected --
        # an unused "env:..." placeholder in [voice.stt.remote] while
        # active_backend is "local" must not be able to block the whole
        # daemon from starting. A field-level validator can't see
        # active_backend at all, since that lives on this parent model,
        # not on STTRemoteConfig itself.
        if self.active_backend == "remote" and self.remote.api_key:
            self.remote.api_key = _resolve_secret(self.remote.api_key)
        return self


class TTSLocalConfig(StrictModel):
    # Only Piper is currently implemented (brain/voice/tts_local.py wraps
    # it directly) -- same reasoning as STTLocalConfig.engine above.
    engine: Literal["piper"] = "piper"
    # `pip install piper-tts` puts `piper` in the *venv's* bin/ dir, which
    # is only on PATH in an activated shell -- a systemd unit invoking
    # `<venv>/bin/python -m brain.main` directly won't have it on PATH
    # (subprocess exec doesn't search next to the running interpreter).
    # Same override pattern as AppearanceConfig.blender_bin/VoiceConfig.rhubarb_bin;
    # point this at an absolute path if PATH isn't set up for the daemon's
    # actual runtime environment.
    piper_bin: str = "piper"
    # Relative to TTSConfig.voices_dir, not a standalone path -- voices are
    # expected to be downloaded (e.g. via `python -m piper.download_voices`)
    # straight into voices_dir, so these names join onto that directory
    # rather than carrying their own "voices/" prefix.
    voice_model_path: str = "default.onnx"
    voice_config_path: str = "default.onnx.json"


class SeedVoices(StrictModel):
    """Seed pools for engines whose voice is chosen by a numeric seed (e.g.
    tts-serve's VoxCPM, which has no reference voice unless one is supplied,
    so each seed yields a different but repeatable voice). The companion
    picks one seed from the group matching its current look and keeps it
    until a redesign moves it to a different group -- see
    brain/voice/voice_select.py. Seeds are only meaningful for the one
    engine/model this remote entry points at, which is why the pools live
    on the entry rather than at [voice] level. Empty groups are skipped."""

    male: list[int] = Field(default_factory=list)
    female: list[int] = Field(default_factory=list)
    male_robot: list[int] = Field(default_factory=list)
    female_robot: list[int] = Field(default_factory=list)
    # Seed used (instead of a random pick) while the companion's look still
    # has the default femininity, i.e. before any customization -- so a fresh
    # install always starts with the same, known-good voice. Applied only if
    # it is in the matching pool; omitted = always pick at random.
    default_seed: int | None = None

    def is_empty(self) -> bool:
        return not (self.male or self.female or self.male_robot or self.female_robot)


class TTSRemoteEntry(StrictModel):
    """One remote TTS instance. Keyed table: [voice.tts.remote.<name>] --
    mirrors [llm.cloud.<provider_name>]'s own pattern, so multiple remote
    instances (different engines, different hosts, brought online
    opportunistically) can coexist with automatic priority-ordered failover.

    provider_type names the wire contract this instance speaks, mirroring
    LLMCloudConfig.provider_type's role exactly:
      - "tts_serve": https://github.com/scorbo2/tts-serve -- POST <endpoint>
        {text, audio_base64?, reference_text?, language?, seed?, **params}
        -> {audio_base64, sample_rate, seed, time_used, rtf} JSON.
        capabilities_endpoint is meaningful (GET /capabilities discovery).
      - "generic_openai": the OpenAI /v1/audio/speech contract --
        POST <endpoint> {model, input, voice, response_format, **params} ->
        raw audio bytes directly in the response body (no JSON wrapper, no
        capabilities discovery -- set capabilities_endpoint = "").

    Voice precedence, per request, limited to what the engine supports:
    1. `voice_sample` (this entry's reference WAV), 2. a seed (only from this
    entry's `seed_voices`), 3. the engine's own default voice. A sample
    therefore suppresses seed selection for this entry.
    brain/voice/tts_remote.py dispatches on this field the same way
    brain/llm_pool.py's build_cloud_client() dispatches on provider_type.

    capabilities_endpoint is a plain str (not str | None): TOML has no null
    literal, so a field a user can only omit (never explicitly "unset") must
    use a sentinel value instead, matching this file's existing convention
    for optional endpoints (e.g. ServerConfig.auth_token_file,
    STTRemoteConfig.base_url) -- "" means "not available," not the default.
    """

    provider_type: Literal["tts_serve", "generic_openai"] = "tts_serve"
    base_url: str
    endpoint: str = "/synthesize"
    capabilities_endpoint: str = "/capabilities"
    priority: int = 100
    enabled: bool = True
    timeout_s: float = 10.0
    api_key: str = ""
    params: dict = Field(default_factory=dict)
    seed_voices: SeedVoices = Field(default_factory=SeedVoices)
    # Path to a reference-voice WAV, sent to tts_serve as audio_base64.
    # Empty = none. "~" is expanded.
    voice_sample: str = ""
    # Transcript of voice_sample; tts_serve only (sent as reference_text).
    voice_sample_text: str = ""

    @model_validator(mode="after")
    def _check_voice_sample_if_enabled(self) -> TTSRemoteEntry:
        if self.enabled and self.voice_sample:
            if self.provider_type == "generic_openai":
                raise ValueError("voice_sample is not supported by provider_type 'generic_openai'")
            if not Path(self.voice_sample).expanduser().is_file():
                raise ValueError(f"voice_sample file not found: {self.voice_sample}")
        return self

    @model_validator(mode="after")
    def _resolve_api_key_if_enabled(self) -> TTSRemoteEntry:
        # Same gating as LLMCloudConfig: a disabled instance's placeholder
        # env:... value must never be able to block daemon startup.
        if self.enabled and self.api_key:
            self.api_key = _resolve_secret(self.api_key)
        return self


class TTSConfig(StrictModel):
    active_backend: Literal["local", "remote"] = "local"
    voices_dir: str = ""
    # Companion voice level, 0.0 (silent) to 1.0 (full): a linear amplitude
    # multiplier the overlay applies to its audio output. It stacks with the
    # system mixer's volume for the overlay's stream. Applied live on config
    # reload (brain pushes `config_update`), and in every `welcome`.
    volume: float = Field(default=1.0, ge=0.0, le=1.0)
    rate: float = 1.0
    local: TTSLocalConfig = Field(default_factory=TTSLocalConfig)
    remote: dict[str, TTSRemoteEntry] = Field(default_factory=dict)

    def enabled_remote_instances(self) -> list[TTSRemoteEntry]:
        """Enabled remote TTS instances, highest priority number tried first --
        matches [llm.cloud.<provider_name>]'s convention exactly, since this
        keyed table was explicitly designed to mirror it."""
        return sorted((r for r in self.remote.values() if r.enabled), key=lambda r: -r.priority)


class VoiceConfig(StrictModel):
    stt: STTConfig = Field(default_factory=STTConfig)
    tts: TTSConfig = Field(default_factory=TTSConfig)
    # No system package typically provides Rhubarb Lip Sync -- it's usually
    # a manual binary install, so unlike blender_bin's bare "blender" (a
    # commonly packaged binary, reliably on PATH), this needs an explicit
    # default a fresh install is expected to override. Same "bin path,
    # string not Path" convention as AppearanceConfig.blender_bin.
    rhubarb_bin: str = "rhubarb"
    # Rhubarb's two recognizers trade accuracy for speed. "pocketSphinx" is
    # tuned for English speech but ran at roughly real time (6.4s for a 5.8s
    # clip), and it runs once per sentence before that sentence can play --
    # adding seconds of delay and a gap between sentences. "phonetic" took
    # ~1.3s for the same clip with a similar spread of mouth shapes, which is
    # plenty for a small stylized mouth, so it is the default.
    lipsync_recognizer: Literal["phonetic", "pocketSphinx"] = "phonetic"


class PresenceConfig(StrictModel):
    dbus_service_name: str = "org.deskmate.Presence"
    dbus_object_path: str = "/org/deskmate/Presence"
    # Avatar footprint in px. Used by the daemon for corner placement AND sent
    # to the overlay in `welcome`, which sizes its window from the same
    # values -- the single source of truth for how big the companion is.
    # Bounds guard against a typo producing an unusable (or screen-filling) window.
    avatar_width_px: int = Field(default=240, ge=96, le=1024)
    avatar_height_px: int = Field(default=240, ge=96, le=1024)
    # Show the sentence being spoken in a bubble next to the avatar.
    show_speech_bubble: bool = True
    margin_px: int = 24
    display_affinity: Literal["primary", "secondary", "auto"] = "secondary"
    idle_after_s: int = 600


class ShortcutsConfig(StrictModel):
    toggle_interaction: str = "Meta+Alt+D"
    push_to_talk: str = "Meta+Alt+M"
    push_to_talk_mode: Literal["hold", "toggle"] = "hold"


# Internal constants, NOT config -- deliberately not a [personality] TOML
# field: brain/personality/prompts.py pairs each of these *exact* names
# with a hand-written low/high descriptive phrase (e.g. "sarcasm" -> ("earnest and
# literal", "biting and sardonic")) and brain/personality/state.py seeds one
# SQLite row per name -- neither is generic over an arbitrary user-supplied
# list. Making this "configurable" would only ever safely support reordering
# or dropping entries; adding a genuinely new trait name would silently have
# no phrase mapping and no seed-row handling, passing Settings validation
# (it's just list[str]) while quietly breaking prompt construction. Keeping
# it an internal constant next to the code that's actually coupled to it
# avoids that exact class of footgun. identity_name stays in config below --
# it's the one genuinely user-customizable value here, with nothing else
# hardcoding "Deskmate" to match against.
TRAIT_NAMES: tuple[str, ...] = (
    "openness",
    "conscientiousness",
    "extraversion",
    "agreeableness",
    "neuroticism",
    "honesty_humility",
    "humor",
    "formality",
    "boldness",
    "affection",
    "patience",
    "sarcasm",
    "loyalty",
)
MOOD_NAMES: tuple[str, ...] = ("energy", "valence", "stress")


class PersonalityConfig(StrictModel):
    identity_name: str = "Deskmate"
    reflection_interval_s: float = 3600.0
    mood_decay_interval_s: float = 60.0
    mood_half_life_s: float = 1800.0


class AppearanceConfig(StrictModel):
    blender_bin: str = "blender"
    default_look_spec: str = "examples/sample_look.json"
    keep_recent_versions: int = 3
    redesign_cooldown_hours_min: float = 12.0
    redesign_cooldown_hours_max: float = 24.0
    autonomous_redesign_probability: float = 0.5

    @model_validator(mode="after")
    def _check_cooldown_range(self) -> AppearanceConfig:
        if self.redesign_cooldown_hours_min > self.redesign_cooldown_hours_max:
            raise ValueError("redesign_cooldown_hours_min must be <= redesign_cooldown_hours_max")
        return self


class SkillsConfig(StrictModel):
    """What the companion may *do* (brain/skills/). With `enabled = false` the
    model just talks and every reply is plain dialogue."""

    enabled: bool = True
    disabled: list[str] = Field(default_factory=list)   # skill names to switch off individually
    max_actions_per_reply: int = Field(default=3, ge=1, le=10)
    # Where Agent Skills (SKILL.md packages, see brain/skills/agentskills.py) are installed.
    # Blank = <deploy_dir>/skills. Installed skills are catalogued but inert until the agent runtime exists.
    dir: str = ""
    # Context budget for third-party skills (docs/ADR_SKILLS_AND_AGENT_ORCHESTRATION.md, "Skill Activation &
    # Context Budget"): stubs may use min(catalog_token_cap, catalog_fraction * context window).
    catalog_fraction: float = Field(default=0.03, gt=0, le=0.5)
    catalog_token_cap: int = Field(default=400, ge=50)
    activation_fraction: float = Field(default=0.25, gt=0, le=0.8)
    max_pinned: int = Field(default=8, ge=0, le=50)
    max_active_skills: int = Field(default=1, ge=1, le=5)
    search_top_k: int = Field(default=5, ge=1, le=20)
    winner_margin: float = Field(default=1.5, ge=1.0)
    max_auto_candidates: int = Field(default=3, ge=1, le=10)
    max_hint_categories: int = Field(default=12, ge=0, le=50)
    # Reserved for the Track 1 script pipelines in docs/ADR_SKILLS_AND_AGENT_ORCHESTRATION.md
    # -- parsed now, not consumed yet.
    max_script_workers: int = Field(default=4, ge=1)


class StatusConfig(StrictModel):
    """The standalone status page (status_page/), a separate process from the
    brain so it stays up when the brain is down. Read by that service only."""

    host: str = "127.0.0.1"
    port: int = Field(default=8780, ge=1, le=65535)
    # journalctl --user units shown as log panes.
    units: list[str] = Field(default_factory=lambda: ["deskmate-brain", "deskmate-overlay"])
    # name -> file path, shown as extra log panes (e.g. tts-serve's server.log).
    extra_logs: dict[str, str] = Field(default_factory=dict)


class Settings(StrictModel):
    general: GeneralConfig = Field(default_factory=GeneralConfig)
    server: ServerConfig = Field(default_factory=ServerConfig)
    storage: StorageConfig
    llm: LLMConfig
    voice: VoiceConfig = Field(default_factory=VoiceConfig)
    presence: PresenceConfig = Field(default_factory=PresenceConfig)
    shortcuts: ShortcutsConfig = Field(default_factory=ShortcutsConfig)
    personality: PersonalityConfig = Field(default_factory=PersonalityConfig)
    appearance: AppearanceConfig = Field(default_factory=AppearanceConfig)
    status: StatusConfig = Field(default_factory=StatusConfig)
    skills: SkillsConfig = Field(default_factory=SkillsConfig)

    # --- Derived paths (storage.workspace_dir = ephemeral scratch,
    # storage.deploy_dir = persistent state -- true siblings, not nested,
    # so clearing workspace_dir can never touch persistent state; keep
    # deploy_dir off slow or network-mounted storage -- besides the latency
    # hit on every state write, SQLite's advisory locking and WAL mode are
    # both unreliable over network filesystems). ---
    @property
    def workspace_dir(self) -> Path:
        return Path(self.storage.workspace_dir)

    @property
    def deploy_dir(self) -> Path:
        return Path(self.storage.deploy_dir)

    @property
    def data_dir(self) -> Path:
        return self.deploy_dir / "data"

    @property
    def current_dir(self) -> Path:
        return self.data_dir / "current"

    @property
    def assets_dir(self) -> Path:
        return self.deploy_dir / "assets"

    @property
    def catalogs_dir(self) -> Path:
        return self.assets_dir / "catalogs"

    @property
    def staged_dir(self) -> Path:
        return self.assets_dir / "staged"

    @property
    def voice_cache_dir(self) -> Path:
        return self.workspace_dir / "voice_cache"

    @property
    def blender_build_dir(self) -> Path:
        return self.workspace_dir / "blender_build"

    @property
    def personality_db_path(self) -> Path:
        return self.deploy_dir / "state.db"

    @property
    def logs_dir(self) -> Path:
        return self.deploy_dir / "logs"

    @property
    def skills_dir(self) -> Path:
        return Path(self.skills.dir).expanduser() if self.skills.dir else self.deploy_dir / "skills"

    def ensure_dirs(self) -> None:
        for d in (
            self.workspace_dir,
            self.deploy_dir,
            self.data_dir,
            self.current_dir,
            self.assets_dir,
            self.catalogs_dir,
            self.staged_dir,
            self.voice_cache_dir,
            self.blender_build_dir,
            self.logs_dir,
            self.skills_dir,
        ):
            d.mkdir(parents=True, exist_ok=True)


def _default_config_path() -> Path:
    env_path = os.environ.get("DESKMATE_CONFIG_FILE")
    if env_path:
        return Path(env_path)
    xdg_config_home = os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))
    return Path(xdg_config_home) / "deskmate" / "config.toml"


def _load_and_validate(path: Path) -> Settings:
    if not path.is_file():
        raise ConfigError(
            f"No config file at {path}.\n"
            f"Copy config.example.toml there and fill it in, e.g.:\n"
            f"  mkdir -p {path.parent}\n"
            f"  cp config.example.toml {path}\n"
            f"  chmod 600 {path}\n"
            f"  $EDITOR {path}"
        )
    try:
        with path.open("rb") as f:
            raw = tomllib.load(f)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"Invalid TOML syntax in {path}: {exc}") from exc
    try:
        return Settings.model_validate(raw)
    except ValidationError as exc:
        raise ConfigError(f"Invalid configuration in {path}:\n{exc}") from exc


class _ReloadHandler(FileSystemEventHandler):
    """Watches one specific file by resolved path, inside a watched directory.

    Watching the *directory* (not the file) rather than the file directly is
    deliberate: editors that save via write-to-tempfile-then-rename replace
    the inode watchdog would otherwise be watching, which can silently stop
    delivering events for some backends.
    """

    def __init__(self, path: Path, on_change: Callable[[], None]) -> None:
        self._path = path.resolve()
        self._on_change = on_change

    def _matches(self, raw_path: str | bytes) -> bool:
        # watchdog types event paths as `bytes | str`; some backends hand over bytes.
        return Path(os.fsdecode(raw_path)).resolve() == self._path

    def on_modified(self, event) -> None:
        if not event.is_directory and self._matches(event.src_path):
            self._on_change()

    def on_created(self, event) -> None:
        if not event.is_directory and self._matches(event.src_path):
            self._on_change()

    def on_moved(self, event) -> None:
        if not event.is_directory and self._matches(getattr(event, "dest_path", "")):
            self._on_change()


class ConfigWatcher:
    """Loads config.toml once, then hot-reloads it on change.

    On a reload that fails validation, the last-good Settings keeps serving
    and the failure is only logged -- a typo in config.toml must never take
    the daemon down.
    """

    def __init__(self, path: Path):
        self._path = path
        self._lock = threading.Lock()
        self._settings = _load_and_validate(path)
        self._subscribers: list[Callable[[Settings], None]] = []
        self._observer: BaseObserver | None = None

    @property
    def path(self) -> Path:
        return self._path

    @property
    def settings(self) -> Settings:
        with self._lock:
            return self._settings

    def subscribe(self, callback: Callable[[Settings], None]) -> None:
        self._subscribers.append(callback)

    def unsubscribe(self, callback: Callable[[Settings], None]) -> None:
        if callback in self._subscribers:
            self._subscribers.remove(callback)

    def start(self) -> None:
        if self._observer is not None:
            return
        handler = _ReloadHandler(self._path, self.reload)
        observer = Observer()
        observer.schedule(handler, str(self._path.parent), recursive=False)
        observer.start()
        self._observer = observer

    def stop(self) -> None:
        if self._observer is not None:
            self._observer.stop()
            self._observer.join()
            self._observer = None

    def reload(self) -> None:
        """Re-read and re-validate config.toml now. Safe to call directly (e.g. from tests)."""
        try:
            new_settings = _load_and_validate(self._path)
        except ConfigError as exc:
            logger.warning("Config reload failed, keeping last-good config: %s", exc)
            return
        with self._lock:
            self._settings = new_settings
        logger.info("Config reloaded from %s", self._path)
        for callback in list(self._subscribers):
            callback(new_settings)


_watcher: ConfigWatcher | None = None


def load_settings(path: str | Path | None = None) -> Settings:
    """Load settings and start the hot-reload watcher. Idempotent after the first call."""
    global _watcher
    if _watcher is None:
        resolved_path = Path(path) if path is not None else _default_config_path()
        _watcher = ConfigWatcher(resolved_path)
        _watcher.start()
    return _watcher.settings


def get_settings() -> Settings:
    """Return the live settings instance, loading it first if necessary."""
    if _watcher is None:
        return load_settings()
    return _watcher.settings


def subscribe_if_running(callback: Callable[[Settings], None]) -> Callable[[], None]:
    """Like `subscribe`, but never starts the watcher (so it can't pull in the
    user's real config as a side effect, e.g. under test); returns a function
    that removes the subscription again -- a no-op when no watcher is running.
    The callback runs on the watcher's thread."""
    watcher = _watcher
    if watcher is None:
        return lambda: None
    watcher.subscribe(callback)
    return lambda: watcher.unsubscribe(callback)


def subscribe(callback: Callable[[Settings], None]) -> None:
    """Register a callback invoked with the new Settings whenever config.toml changes."""
    if _watcher is None:
        load_settings()
    assert _watcher is not None
    _watcher.subscribe(callback)
