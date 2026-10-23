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

logger = logging.getLogger("deskmate.voice.tts")

_FAILURE_COOLDOWN_S = 60.0


class TTSBackend(ABC):
    """Common interface every local/remote TTS integration implements.
    `voice_id`/`params` are accommodations for future voice-selection
    features: callers may pass them, but no backend requires them."""

    @abstractmethod
    async def synthesize(
        self, text: str, out_wav_path: Path, *, voice_id: str | None = None, params: dict[str, Any] | None = None
    ) -> None:
        """Write synthesized speech for `text` to `out_wav_path`. Raises on failure."""

    async def health_check(self) -> bool:
        return True

    async def aclose(self) -> None:
        return None


@dataclass
class _PoolEntry:
    name: str
    backend: TTSBackend
    cooldown_until: float = field(default=0.0)

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
    ) -> None:
        self._remote = [_PoolEntry(name, backend) for name, backend in remote_entries]
        self._local = local_backend
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

    async def synthesize(
        self,
        text: str,
        out_wav_path: Path,
        *,
        voice_id: str | None = None,
        params: dict[str, Any] | None = None,
        remote_params: dict[str, dict[str, Any]] | None = None,
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

        available = [e for e in self._remote if e.is_cooled_down(now)]
        if self._remote and not available:
            # Every remote recently failed and is sitting out its cooldown, so
            # this request goes straight to Piper -- say so, or the voice just
            # changes with nothing in the log to explain it.
            remaining = max(e.cooldown_until - now for e in self._remote)
            logger.warning(
                "TTS remote(s) %s cooling down for another %.0fs; using local Piper",
                [e.name for e in self._remote],
                remaining,
            )
        for entry in available:
            try:
                entry_params = {**(params or {}), **(remote_params or {}).get(entry.name, {})} or None
                await entry.backend.synthesize(text, out_wav_path, voice_id=voice_id, params=entry_params)
                return entry.name
            except Exception as exc:  # noqa: BLE001 -- want to try the next backend, then local
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

        if self._local is not None:
            try:
                await self._local.synthesize(text, out_wav_path, voice_id=voice_id, params=params)
                return "local"
            except Exception as exc:  # noqa: BLE001 -- all options exhausted; raise below
                last_error = exc

        raise RuntimeError(f"All TTS backends failed: {last_error}") from last_error

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
        entries_by_id = {id(entry): name for name, entry in tts_config.remote.items()}
        remote_entries = [
            (entries_by_id[id(entry)], build_remote_tts_backend(entry))
            for entry in tts_config.enabled_remote_instances()
        ]
    local_backend = (
        PiperTTSBackend(tts_config.local, Path(tts_config.voices_dir), piper_bin=tts_config.local.piper_bin)
        if tts_config.voices_dir
        else None
    )
    return TTSPool(remote_entries, local_backend)
