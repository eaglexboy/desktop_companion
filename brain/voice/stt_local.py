"""Local STT backend: faster-whisper (https://github.com/SYSTRAN/faster-whisper).

Decodes the WAV ourselves and hands faster-whisper a numpy float32 array
rather than a file path. This isn't just a style choice: `WhisperModel.transcribe()`
given a path calls its own `decode_audio()`, which shells out to PyAV's
`av.open(..., metadata_errors="ignore")` -- some PyAV/faster-whisper version
combinations raise `TypeError: open() got an unexpected keyword argument
'metadata_errors'` there, since faster-whisper's PyAV version constraint has
no upper bound and PyAV has changed that function's signature across
releases. Decoding the WAV directly (stdlib `wave` + a plain
linear-interpolation resample to the 16kHz Whisper expects) sidesteps that
code path entirely.

`WhisperModel.transcribe()` is a blocking, CPU/GPU-bound call (not async),
so every call here runs inside `asyncio.to_thread` -- same pattern
brain/personality/state.py's own docstring prescribes for its sync sqlite3
calls.

The model itself is lazily constructed on first use, not in `__init__`:
`WhisperModel(...)` makes real synchronous HTTP calls out to the
HuggingFace Hub (to resolve/cache the model files) the moment it's
constructed -- doing that eagerly would mean every caller that merely
*wires up* an `STTCoordinator` (`brain.voice.stt.build_stt_coordinator()`,
called from `brain.main.build_app_state()`) pays a real network round-trip
whether or not STT is ever actually used, which also makes any hermetic
test that builds `AppState` quietly dependent on network access.
"""

from __future__ import annotations

import asyncio
import logging
import wave
from pathlib import Path
from typing import Any

import numpy as np

from brain.config import STTLocalConfig
from brain.language import normalize_language, primary_language
from brain.voice.stt import STTBackend, Transcript

_WHISPER_SAMPLE_RATE = 16000
logger = logging.getLogger("deskmate.voice.stt_local")


def _read_wav_as_float32(wav_path: Path) -> tuple[np.ndarray, int]:
    with wave.open(str(wav_path), "rb") as wf:
        n_channels = wf.getnchannels()
        sample_rate = wf.getframerate()
        raw = wf.readframes(wf.getnframes())
    samples = np.frombuffer(raw, dtype=np.int16)
    mono: np.ndarray = (
        samples.reshape(-1, n_channels).mean(axis=1)
        if n_channels > 1
        else samples
    )
    return mono.astype(np.float32) / 32768.0, sample_rate


def _resample_to_whisper_rate(
    audio: np.ndarray, sample_rate: int
) -> np.ndarray:
    if sample_rate == _WHISPER_SAMPLE_RATE or len(audio) == 0:
        return audio
    target_len = max(1, int(len(audio) * _WHISPER_SAMPLE_RATE / sample_rate))
    x_old = np.linspace(0, 1, num=len(audio), endpoint=False)
    x_new = np.linspace(0, 1, num=target_len, endpoint=False)
    return np.interp(x_new, x_old, audio).astype(np.float32)


class FasterWhisperSTTBackend(STTBackend):
    def __init__(self, config: STTLocalConfig) -> None:
        self._config = config
        self._model: Any | None = None
        self._warned_english_only = False

    def _ensure_model_sync(self) -> Any:
        if self._model is None:
            from faster_whisper import (
                WhisperModel,  # lazy: heavy import, not needed if remote-only
            )

            self._model = WhisperModel(
                self._config.model_size,
                device=self._config.device,
                compute_type=self._config.compute_type,
            )
        return self._model

    def _transcribe_sync(
        self, wav_path: Path, language: str | None
    ) -> Transcript:
        model = self._ensure_model_sync()
        # Whisper takes plain codes; a regional tag (es-mx) is just `es` to it.
        hint = primary_language(normalize_language(language) or "") or None
        audio, sample_rate = _read_wav_as_float32(wav_path)
        audio = _resample_to_whisper_rate(audio, sample_rate)
        if self._config.model_size.endswith(".en"):
            # An English-only model cannot transcribe anything else.
            if hint not in (None, "en") and not self._warned_english_only:
                self._warned_english_only = True
                logger.warning(
                    "STT model %r is English-only but the turn language is %r; "
                    "set [voice.stt.local] model_size to a multilingual model",
                    self._config.model_size,
                    hint,
                )
            hint = "en"
        segments, info = model.transcribe(audio, language=hint)
        text = " ".join(segment.text.strip() for segment in segments).strip()
        detected = normalize_language(getattr(info, "language", None))
        probability = getattr(info, "language_probability", None)
        return Transcript(
            text,
            detected,
            float(probability) if probability is not None else None,
        )

    async def transcribe(
        self, wav_path: Path, *, language: str | None = None
    ) -> Transcript:
        return await asyncio.to_thread(
            self._transcribe_sync, wav_path, language
        )
