"""STTBackend interface, local/remote coordinator, and the streaming audio
buffer that turns `audio_stream_chunk` frames into finalized WAV files
ready for transcription.

Unlike `TTSPool` (N remote instances, priority-ordered, local as final
fallback), STT has exactly one *active* backend at a time
(`STTConfig.active_backend`): "local" never touches the network at all;
"remote" tries the network first and falls back to local only on error --
the same local-failover-on-error policy TTS uses, just with a single
designated remote instead of a priority list.
"""

from __future__ import annotations

import array
import logging
import math
import tempfile
import time
import wave
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from brain.config import Settings

logger = logging.getLogger("deskmate.voice.stt")

# A deliberately simple, fixed energy gate (simple energy-based VAD, not a
# tunable/ML one) -- roughly -36 dBFS for 16-bit PCM, well above digital
# silence/room noise, well below normal speech level. Not exposed as a
# config field; revisit if real usage shows it needs tuning.
_VOICE_ACTIVITY_RMS_THRESHOLD = 500.0

_STREAM_SAMPLE_WIDTH_BYTES = 2  # PCM16LE, per the Microphone Wire Format


class STTBackend(ABC):
    @abstractmethod
    async def transcribe(self, wav_path: Path, *, language: str | None = None) -> str:
        """Transcribe the audio at `wav_path` to text. Raises on failure."""

    async def health_check(self) -> bool:
        return True

    async def aclose(self) -> None:
        return None


class STTCoordinator:
    """Picks local vs. remote per `active_backend`, with remote-fails ->
    local fallback (never the reverse: a configured "local" backend never
    makes a network call at all)."""

    def __init__(self, active_backend: str, local: STTBackend, remote: STTBackend | None) -> None:
        self._active_backend = active_backend
        self._local = local
        self._remote = remote

    async def transcribe(self, wav_path: Path, *, language: str | None = None) -> tuple[str, str]:
        """Returns (backend_name, text)."""
        if self._active_backend == "remote" and self._remote is not None:
            try:
                return "remote", await self._remote.transcribe(wav_path, language=language)
            except Exception as exc:  # noqa: BLE001 -- fall back to local, same policy as TTS
                logger.warning("remote STT failed (%s); falling back to local", exc)
        return "local", await self._local.transcribe(wav_path, language=language)

    async def aclose(self) -> None:
        await self._local.aclose()
        if self._remote is not None:
            await self._remote.aclose()


def _pcm16le_rms(data: bytes) -> float:
    usable_len = len(data) - (len(data) % 2)
    if usable_len <= 0:
        return 0.0
    samples = array.array("h")
    samples.frombytes(data[:usable_len])
    if not samples:
        return 0.0
    return math.sqrt(sum(s * s for s in samples) / len(samples))


def _write_pcm16_wav(path: Path, raw_pcm: bytes, *, sample_rate: int) -> None:
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(_STREAM_SAMPLE_WIDTH_BYTES)
        wf.setframerate(sample_rate)
        wf.writeframes(raw_pcm)


@dataclass
class _StreamState:
    sample_rate: int
    chunks: list[bytes] = field(default_factory=list)
    last_voice_activity: float = 0.0


class UnknownStreamError(ValueError):
    pass


class StreamingSTT:
    """Buffers `audio_stream_chunk` binary frames per `stream_id` -- the
    JSON header/sequencing and WebSocket plumbing itself belong to
    `brain/ipc/server.py`; this class only owns the buffering/VAD/
    finalization policy. Finalizes on `audio_stream_end` (push-to-talk
    release) or, in toggle mode, when
    `is_trailing_silence_exceeded()` reports `silence_threshold_ms` of
    sustained low-energy audio -- the caller is responsible for polling
    that and calling `end_stream()` itself; this class has no timer loop
    of its own."""

    def __init__(
        self, coordinator: STTCoordinator, *, silence_threshold_ms: int, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self._coordinator = coordinator
        self._silence_threshold_s = silence_threshold_ms / 1000.0
        self._clock = clock
        self._streams: dict[str, _StreamState] = {}

    def start_stream(self, stream_id: str, *, sample_rate: int = 16000) -> None:
        self._streams[stream_id] = _StreamState(sample_rate=sample_rate, last_voice_activity=self._clock())

    def feed_chunk(self, stream_id: str, data: bytes) -> None:
        state = self._streams.get(stream_id)
        if state is None:
            raise UnknownStreamError(f"no active stream {stream_id!r} (missing audio_stream_start?)")
        state.chunks.append(data)
        if _pcm16le_rms(data) >= _VOICE_ACTIVITY_RMS_THRESHOLD:
            state.last_voice_activity = self._clock()

    def is_trailing_silence_exceeded(self, stream_id: str) -> bool:
        """For toggle mode only -- push-to-talk finalizes on the client's
        explicit `audio_stream_end` instead, regardless of this."""
        state = self._streams.get(stream_id)
        if state is None:
            return False
        return (self._clock() - state.last_voice_activity) >= self._silence_threshold_s

    def discard_stream(self, stream_id: str) -> None:
        self._streams.pop(stream_id, None)

    async def end_stream(self, stream_id: str, *, language: str | None = None) -> tuple[str, str]:
        """Finalizes and transcribes the buffered audio, then discards the
        stream's state either way (a stream is finalized at most once)."""
        state = self._streams.pop(stream_id, None)
        if state is None:
            raise UnknownStreamError(f"no active stream {stream_id!r}")

        raw_pcm = b"".join(state.chunks)
        with tempfile.TemporaryDirectory() as tmp:
            wav_path = Path(tmp) / "utterance.wav"
            _write_pcm16_wav(wav_path, raw_pcm, sample_rate=state.sample_rate)
            return await self._coordinator.transcribe(wav_path, language=language)


def build_stt_coordinator(settings: Settings) -> STTCoordinator:
    """Wire a real STTCoordinator from Settings -- mirrors
    brain.voice.tts.build_tts_pool()'s role for TTS. Lazy imports avoid a
    hard import cycle (stt_local.py/stt_remote.py both import STTBackend
    from this module)."""
    from brain.voice.stt_local import FasterWhisperSTTBackend
    from brain.voice.stt_remote import RemoteSTTBackend

    stt_config = settings.voice.stt
    local = FasterWhisperSTTBackend(stt_config.local)
    remote = RemoteSTTBackend(stt_config.remote) if stt_config.remote.base_url else None
    return STTCoordinator(stt_config.active_backend, local, remote)
