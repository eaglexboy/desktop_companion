"""TTSBackend interface + a priority/failover pool, mirroring brain/llm_pool.py.

`TTSPool` explicitly models N remote instances (priority-ordered, each with
its own 60s failure cooldown -- identical policy to llm_pool.py's
`LLMPool`) with local Piper as a separate, final fallback tier, tried only
once every remote is exhausted or cooled down.
"""

from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from brain.config import Settings
from brain.language import DEFAULT_LANGUAGE, languages_match, primary_language

logger = logging.getLogger("deskmate.voice.tts")

_FAILURE_COOLDOWN_S = 60.0


class TTSBackend(ABC):
    """Common interface every local/remote TTS integration implements.
    `voice_id`/`params` are accommodations for future voice-selection
    features: callers may pass them, but no backend requires them."""

    @abstractmethod
    async def synthesize(
        self,
        text: str,
        out_wav_path: Path,
        *,
        voice_id: str | None = None,
        params: dict[str, Any] | None = None,
        language: str | None = None,
    ) -> None:
        """Write synthesized speech for `text` to `out_wav_path`. Raises on failure."""

    async def health_check(self) -> bool:
        return True

    async def aclose(self) -> None:
        return None


class SpeechUnavailable(RuntimeError):
    """No engine can produce intelligible speech for the turn language,
    natively or accented (ADR_MULTI_LANGUAGE_CHAT 2.5.4)."""


@dataclass(frozen=True)
class TTSCapabilities:
    """An engine's language behaviour (ADR 2.5.1). The defaults read an engine
    as English-only, one language per instance, no foreign text."""

    languages: tuple[str, ...] = (DEFAULT_LANGUAGE,)
    selection: str = "fixed"  # fixed | per_request | inferred
    foreign_text: bool = False
    cloning: bool = False

    def speaks(self, language: str) -> bool:
        return any(languages_match(language, l) for l in self.languages)


@dataclass(frozen=True)
class SpeechPlan:
    """How a turn in `language` will be spoken. `mode` is "native" or
    "accented" (the text is read with the home language's phonetics)."""

    mode: str
    language: str  # the turn language
    voice_language: str  # the language the engine is asked to speak
    entries: tuple[str, ...]  # remote instance names, in order
    local: bool
    # Why an accented plan was chosen: "fluency" (the character's choice) or
    # "fallback" (no native engine exists). None for native plans.
    reason: str | None = None


@dataclass
class _PoolEntry:
    name: str
    backend: TTSBackend
    cooldown_until: float = field(default=0.0)
    capabilities: TTSCapabilities = field(default_factory=TTSCapabilities)

    def is_cooled_down(self, now: float) -> bool:
        return now >= self.cooldown_until


class TTSPool:
    """`remote_entries` must already be priority-ordered (highest priority
    first) -- same convention as `LLMPool`'s constructor. `local_backend`
    is optional only so a remote-only or local-only deployment doesn't need
    a dummy stand-in; `is_empty` is true only when neither is configured."""

    def __init__(
        self,
        remote_entries: list[tuple[str, TTSBackend]],
        local_backend: TTSBackend | None,
        *,
        cooldown_s: float = _FAILURE_COOLDOWN_S,
        capabilities: dict[str, TTSCapabilities] | None = None,
        local_languages: tuple[str, ...] = (DEFAULT_LANGUAGE,),
        home_language: str = DEFAULT_LANGUAGE,
    ) -> None:
        caps = capabilities or {}
        self._remote = [
            _PoolEntry(
                name, backend, capabilities=caps.get(name, TTSCapabilities())
            )
            for name, backend in remote_entries
        ]
        self._local = local_backend
        self._local_caps = TTSCapabilities(languages=local_languages)
        self._home_language = home_language
        self._warned_accent = False
        self._cooldown_s = cooldown_s

    @property
    def is_empty(self) -> bool:
        return not self._remote and self._local is None

    @property
    def has_remote(self) -> bool:
        return bool(self._remote)

    @property
    def is_degraded(self) -> bool:
        """True when remotes are configured but every one of them is
        currently cooled-down (forced onto the local fallback for now) --
        surfaced via `GET /health`'s `voice_degraded` field."""
        if not self._remote:
            return False
        now = time.monotonic()
        return not any(e.is_cooled_down(now) for e in self._remote)

    def speakable_languages(self) -> list[str]:
        """Every language the installed pipeline can speak natively, or `*`
        when an engine infers it from the text. Accented fallbacks are not
        listed: they are a stopgap, not a capability."""
        found: list[str] = []
        sources = [e.capabilities for e in self._remote]
        if self._local is not None:
            sources.append(self._local_caps)
        for caps in sources:
            for lang in caps.languages:
                if lang not in found:
                    found.append(lang)
        return found

    def plan(
        self,
        language: str,
        *,
        home_language: str | None = None,
        accented: bool = False,
    ) -> SpeechPlan:
        """Routes a turn in `language` (ADR 2.5.2-2.5.4). `accented` asks for
        the character's accent (fluency below the native threshold); with no
        engine able to do it the plan falls back to native routing. Raises
        `SpeechUnavailable` when nothing can speak it intelligibly."""
        home = home_language or self._home_language
        # Accented: an engine that speaks the home language and tolerates
        # foreign text, preferring one that keeps the character's voice.
        accent_engines = sorted(
            (
                e
                for e in self._remote
                if e.capabilities.foreign_text and e.capabilities.speaks(home)
            ),
            key=lambda e: not e.capabilities.cloning,  # stable: keeps priority
        )
        if accented and accent_engines:
            return SpeechPlan(
                "accented",
                language,
                home,
                tuple(e.name for e in accent_engines),
                False,
                "fluency",
            )
        if accented and not self._warned_accent:
            self._warned_accent = True
            logger.info(
                "the character speaks %r with an accent but no engine speaks %r with foreign_text; using native routing",
                language,
                home,
            )
        native = [e for e in self._remote if e.capabilities.speaks(language)]
        local_native = self._local is not None and self._local_caps.speaks(
            language
        )
        if native or local_native:
            return SpeechPlan(
                "native",
                language,
                language,
                tuple(e.name for e in native),
                local_native,
            )
        if accent_engines:
            if not self._warned_accent:
                self._warned_accent = True
                logger.info(
                    "no engine speaks %r natively; speaking it with the %r accent via %s",
                    language,
                    home,
                    [e.name for e in accent_engines],
                )
            return SpeechPlan(
                "accented",
                language,
                home,
                tuple(e.name for e in accent_engines),
                False,
                "fallback",
            )
        raise SpeechUnavailable(f"no speech engine for {language!r}")

    async def synthesize(
        self,
        text: str,
        out_wav_path: Path,
        *,
        voice_id: str | None = None,
        params: dict[str, Any] | None = None,
        remote_params: dict[str, dict[str, Any]] | None = None,
        language: str | None = None,
        home_language: str | None = None,
        accented: bool = False,
    ) -> str:
        """Tries each enabled remote in priority order (skipping any still
        in cooldown), then local Piper as the final fallback. Returns the
        name of whichever backend actually succeeded ("local" for Piper).

        `remote_params` maps a remote instance's name to params sent only to
        that instance (merged over `params`) -- e.g. a seed that is only
        meaningful for the one engine/model it was chosen for, and must not
        leak to a failover instance running something else."""
        now = time.monotonic()
        last_error: Exception | None = None

        plan = (
            self.plan(language, home_language=home_language, accented=accented)
            if language
            else None
        )
        candidates = (
            [e for e in self._remote if e.name in plan.entries]
            if plan
            else self._remote
        )
        if plan:  # keep the plan's order (accented prefers cloning engines)
            candidates.sort(key=lambda e: plan.entries.index(e.name))
        use_local = plan.local if plan else True
        available = [e for e in candidates if e.is_cooled_down(now)]
        if candidates and not available:
            # Every remote recently failed and is sitting out its cooldown, so
            # this request goes straight to Piper -- say so, or the voice just
            # changes with nothing in the log to explain it.
            remaining = max(e.cooldown_until - now for e in candidates)
            logger.warning(
                "TTS remote(s) %s cooling down for another %.0fs; using local Piper",
                [e.name for e in candidates],
                remaining,
            )
        for entry in available:
            try:
                entry_params = {
                    **(params or {}),
                    **(remote_params or {}).get(entry.name, {}),
                } or None
                if plan and entry.capabilities.selection == "per_request":
                    # Only per-request engines read the field (ADR 2.5.1).
                    entry_params = {
                        **(entry_params or {}),
                        "language": primary_language(plan.voice_language),
                    }
                await entry.backend.synthesize(
                    text, out_wav_path, voice_id=voice_id, params=entry_params
                )
                return entry.name
            except Exception as exc:
                last_error = exc
                entry.cooldown_until = time.monotonic() + self._cooldown_s
                # An httpx timeout stringifies to "", so always include the type.
                logger.warning(
                    "TTS remote %r failed (%s: %s); cooling down %.0fs, trying next / local Piper",
                    entry.name,
                    type(exc).__name__,
                    exc,
                    self._cooldown_s,
                )

        if self._local is not None and use_local:
            try:
                local_kwargs: dict[str, Any] = (
                    {"language": language} if language else {}
                )
                await self._local.synthesize(
                    text,
                    out_wav_path,
                    voice_id=voice_id,
                    params=params,
                    **local_kwargs,
                )
                return "local"
            except Exception as exc:
                last_error = exc

        raise RuntimeError(
            f"All TTS backends failed: {last_error}"
        ) from last_error

    async def warm_up(self, scratch_wav_path: Path) -> None:
        """Sends each remote a throwaway one-word request so an engine that went
        cold (idle or swapped out across a sleep) loads before the first real
        sentence needs it. Best effort: a failure is logged but never starts a
        cooldown -- the real request decides whether a remote is down."""
        for entry in self._remote:
            started = time.monotonic()
            try:
                await entry.backend.synthesize("Hi.", scratch_wav_path)
                logger.info(
                    "TTS remote %r warmed up in %.1fs",
                    entry.name,
                    time.monotonic() - started,
                )
            except Exception as exc:  # noqa: BLE001 -- warm-up must never raise
                logger.info(
                    "TTS remote %r warm-up failed after %.1fs (%s: %s)",
                    entry.name,
                    time.monotonic() - started,
                    type(exc).__name__,
                    exc,
                )

    async def aclose(self) -> None:
        for entry in self._remote:
            await entry.backend.aclose()
        if self._local is not None:
            await self._local.aclose()


def build_tts_pool(settings: Settings) -> TTSPool:
    """Wire a real TTSPool from Settings -- mirrors brain.router.build_router()'s
    role for the LLM side. Lazy imports avoid a hard import cycle (tts_local.py/
    tts_remote.py both import TTSBackend from this module)."""
    from brain.voice.tts_local import PiperTTSBackend
    from brain.voice.tts_remote import build_remote_tts_backend

    tts_config = settings.voice.tts
    # active_backend == "local" means remotes are skipped entirely, same as
    # STTCoordinator's policy -- otherwise this field would be parsed but
    # silently meaningless for TTS, unlike STT where it's load-bearing.
    # "remote" uses the normal N-remote-priority-then-local-fallback pool
    # (not "remote only, no fallback" -- local stays the safety net either way).
    if tts_config.active_backend == "local":
        remote_entries: list[tuple[str, Any]] = []
    else:
        # TTSConfig.enabled_remote_instances() already applies the right
        # enabled+priority filter/sort; it just doesn't carry each entry's
        # name (the dict key), which TTSPool needs for logging/failover
        # attribution, so look it up by identity rather than re-deriving
        # the same filter here.
        entries_by_id = {
            id(entry): name for name, entry in tts_config.remote.items()
        }
        remote_entries = [
            (entries_by_id[id(entry)], build_remote_tts_backend(entry))
            for entry in tts_config.enabled_remote_instances()
        ]
    default_language = settings.language.default
    local_backend = (
        PiperTTSBackend(
            tts_config.local,
            Path(tts_config.voices_dir),
            piper_bin=tts_config.local.piper_bin,
            default_language=default_language,
        )
        if tts_config.voices_dir
        else None
    )
    capabilities = {
        name: TTSCapabilities(
            languages=tuple(entry.languages),
            selection=entry.selection,
            foreign_text=entry.foreign_text,
            cloning=entry.cloning or bool(entry.voice_sample),
        )
        for name, entry in tts_config.remote.items()
    }
    return TTSPool(
        remote_entries,
        local_backend,
        capabilities=capabilities,
        local_languages=(
            local_backend.languages if local_backend else (default_language,)
        ),
        home_language=settings.language.home,
    )
