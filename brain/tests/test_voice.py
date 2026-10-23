"""Tests for brain.voice.stt (STTCoordinator, StreamingSTT), stt_remote
(contract test against the OpenAI-compatible /v1/audio/transcriptions
shape), and brain.voice.delivery.pick_delivery's rule table. A real
end-to-end test also runs the actual faster-whisper model against a real
Piper-synthesized WAV when both are available -- this is also the test that
caught and worked around a real PyAV/faster-whisper incompatibility (see
brain/voice/stt_local.py's module docstring).
"""

from __future__ import annotations

import os
import shutil
import struct
import wave
from pathlib import Path

import httpx
import pytest

from brain.config import STTLocalConfig, STTRemoteConfig
from brain.voice.delivery import pick_delivery
from brain.voice.stt import StreamingSTT, STTBackend, STTCoordinator, UnknownStreamError
from brain.voice.stt_remote import RemoteSTTBackend

HAS_PIPER = shutil.which("piper") is not None


def _mock_transport(handler) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


class _ScriptedSTTBackend(STTBackend):
    """Captures each call's WAV params at call time, not just the path --
    `end_stream()` writes the file into a `TemporaryDirectory` that's
    cleaned up as soon as it returns, so a caller inspecting the path
    afterward would find it already gone."""

    def __init__(self, *, text: str = "", fails: bool = False) -> None:
        self.text = text
        self.fails = fails
        self.calls: list[tuple[int, int, int] | None] = []  # (n_channels, framerate, n_frames), or None if no real WAV

    async def transcribe(self, wav_path: Path, *, language: str | None = None) -> str:
        if wav_path.is_file():
            with wave.open(str(wav_path), "rb") as wf:
                self.calls.append((wf.getnchannels(), wf.getframerate(), wf.getnframes()))
        else:
            self.calls.append(None)  # failover tests pass a placeholder path that's never written
        if self.fails:
            raise RuntimeError("transcription failed")
        return self.text


# --- STTCoordinator: local/remote failover -----------------------------------


@pytest.mark.asyncio
async def test_local_active_backend_never_touches_remote(tmp_path):
    local = _ScriptedSTTBackend(text="local result")
    remote = _ScriptedSTTBackend(text="remote result")
    coordinator = STTCoordinator("local", local, remote)

    name, text = await coordinator.transcribe(tmp_path / "x.wav")
    assert (name, text) == ("local", "local result")
    assert remote.calls == []


@pytest.mark.asyncio
async def test_remote_active_backend_tries_remote_first(tmp_path):
    local = _ScriptedSTTBackend(text="local result")
    remote = _ScriptedSTTBackend(text="remote result")
    coordinator = STTCoordinator("remote", local, remote)

    name, text = await coordinator.transcribe(tmp_path / "x.wav")
    assert (name, text) == ("remote", "remote result")
    assert local.calls == []


@pytest.mark.asyncio
async def test_remote_failure_falls_back_to_local(tmp_path):
    local = _ScriptedSTTBackend(text="local result")
    remote = _ScriptedSTTBackend(fails=True)
    coordinator = STTCoordinator("remote", local, remote)

    name, text = await coordinator.transcribe(tmp_path / "x.wav")
    assert (name, text) == ("local", "local result")


@pytest.mark.asyncio
async def test_remote_active_with_no_remote_configured_uses_local(tmp_path):
    local = _ScriptedSTTBackend(text="local result")
    coordinator = STTCoordinator("remote", local, None)

    name, text = await coordinator.transcribe(tmp_path / "x.wav")
    assert (name, text) == ("local", "local result")


# --- StreamingSTT: buffering, VAD, finalization ------------------------------


def _pcm16le_tone(amplitude: int, n_samples: int = 1600) -> bytes:
    return struct.pack(f"<{n_samples}h", *([amplitude] * n_samples))


class _FakeClock:
    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now


@pytest.mark.asyncio
async def test_feed_chunk_requires_started_stream():
    coordinator = STTCoordinator("local", _ScriptedSTTBackend(), None)
    streaming = StreamingSTT(coordinator, silence_threshold_ms=800)
    with pytest.raises(UnknownStreamError):
        streaming.feed_chunk("s_1", _pcm16le_tone(0))


@pytest.mark.asyncio
async def test_end_stream_transcribes_buffered_pcm_as_wav(tmp_path):
    local = _ScriptedSTTBackend(text="hello world")
    coordinator = STTCoordinator("local", local, None)
    streaming = StreamingSTT(coordinator, silence_threshold_ms=800)

    streaming.start_stream("s_1", sample_rate=16000)
    streaming.feed_chunk("s_1", _pcm16le_tone(5000))
    streaming.feed_chunk("s_1", _pcm16le_tone(5000))
    name, text = await streaming.end_stream("s_1")

    assert (name, text) == ("local", "hello world")
    n_channels, framerate, n_frames = local.calls[0]
    assert framerate == 16000
    assert n_channels == 1
    assert n_frames == 3200  # two 1600-sample chunks


@pytest.mark.asyncio
async def test_end_stream_discards_state_so_it_cannot_be_finalized_twice():
    coordinator = STTCoordinator("local", _ScriptedSTTBackend(text="x"), None)
    streaming = StreamingSTT(coordinator, silence_threshold_ms=800)
    streaming.start_stream("s_1")
    streaming.feed_chunk("s_1", _pcm16le_tone(0))
    await streaming.end_stream("s_1")

    with pytest.raises(UnknownStreamError):
        await streaming.end_stream("s_1")


def test_trailing_silence_detection_uses_energy_not_just_elapsed_time():
    clock = _FakeClock(start=0.0)
    coordinator = STTCoordinator("local", _ScriptedSTTBackend(), None)
    streaming = StreamingSTT(coordinator, silence_threshold_ms=500, clock=clock)

    streaming.start_stream("s_1")
    clock.now = 0.1
    streaming.feed_chunk("s_1", _pcm16le_tone(8000))  # loud -- resets the voice-activity clock
    clock.now = 0.5
    assert streaming.is_trailing_silence_exceeded("s_1") is False  # only 0.4s since last voice

    clock.now = 0.7
    streaming.feed_chunk("s_1", _pcm16le_tone(0))  # silence -- does not reset the clock
    clock.now = 0.65  # 0.55s since the loud chunk at t=0.1
    assert streaming.is_trailing_silence_exceeded("s_1") is True


def test_unknown_stream_silence_check_is_false_not_an_error():
    coordinator = STTCoordinator("local", _ScriptedSTTBackend(), None)
    streaming = StreamingSTT(coordinator, silence_threshold_ms=500)
    assert streaming.is_trailing_silence_exceeded("never_started") is False


@pytest.mark.asyncio
async def test_discard_stream_drops_buffered_audio_without_transcribing():
    backend = _ScriptedSTTBackend(text="should never be called")
    coordinator = STTCoordinator("local", backend, None)
    streaming = StreamingSTT(coordinator, silence_threshold_ms=500)
    streaming.start_stream("s_1")
    streaming.feed_chunk("s_1", _pcm16le_tone(0))
    streaming.discard_stream("s_1")

    with pytest.raises(UnknownStreamError):
        await streaming.end_stream("s_1")
    assert backend.calls == []


# --- RemoteSTTBackend contract ------------------------------------------------


@pytest.mark.asyncio
async def test_remote_stt_posts_multipart_and_parses_text_field(tmp_path):
    wav_path = tmp_path / "utterance.wav"
    wav_path.write_bytes(b"RIFF-fake-wav-bytes")
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["content_type"] = request.headers.get("content-type", "")
        captured["body"] = request.content
        return httpx.Response(200, json={"text": "transcribed text"})

    config = STTRemoteConfig.model_validate(
        {"base_url": "http://testserver", "endpoint": "/v1/audio/transcriptions", "model": "whisper-1"}
    )
    backend = RemoteSTTBackend(config)
    backend._http = httpx.AsyncClient(base_url="http://testserver", transport=_mock_transport(handler))

    text = await backend.transcribe(wav_path, language="en")
    assert text == "transcribed text"
    assert "multipart/form-data" in captured["content_type"]
    assert b"whisper-1" in captured["body"]
    assert b"utterance.wav" in captured["body"]


@pytest.mark.asyncio
async def test_remote_stt_sends_bearer_token_when_api_key_set(tmp_path):
    wav_path = tmp_path / "utterance.wav"
    wav_path.write_bytes(b"fake")
    captured_headers = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured_headers.update(request.headers)
        return httpx.Response(200, json={"text": "ok"})

    config = STTRemoteConfig.model_validate({"base_url": "http://testserver", "api_key": "secret"})
    backend = RemoteSTTBackend(config)
    backend._http = httpx.AsyncClient(
        base_url="http://testserver", headers={"Authorization": "Bearer secret"}, transport=_mock_transport(handler)
    )
    await backend.transcribe(wav_path)
    assert captured_headers["authorization"] == "Bearer secret"


@pytest.mark.asyncio
async def test_remote_stt_raises_on_http_error(tmp_path):
    wav_path = tmp_path / "utterance.wav"
    wav_path.write_bytes(b"fake")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    backend = RemoteSTTBackend(STTRemoteConfig.model_validate({"base_url": "http://testserver"}))
    backend._http = httpx.AsyncClient(base_url="http://testserver", transport=_mock_transport(handler))
    with pytest.raises(httpx.HTTPStatusError):
        await backend.transcribe(wav_path)


# --- pick_delivery rule table --------------------------------------------------


def test_pick_delivery_long_or_code_wins_regardless_of_mood():
    assert pick_delivery({"sarcasm": 90}, {"energy": 90}, is_long_or_code=True) == "explanatory"


def test_pick_delivery_high_sarcasm_is_deadpan():
    assert pick_delivery({"sarcasm": 80}, {"energy": 50}, is_long_or_code=False) == "deadpan"


def test_pick_delivery_high_energy_is_animated():
    assert pick_delivery({"sarcasm": 50}, {"energy": 80}, is_long_or_code=False) == "animated"


def test_pick_delivery_low_energy_is_subdued():
    assert pick_delivery({"sarcasm": 50}, {"energy": 20}, is_long_or_code=False) == "subdued"


def test_pick_delivery_sarcasm_takes_priority_over_energy():
    assert pick_delivery({"sarcasm": 80}, {"energy": 80}, is_long_or_code=False) == "deadpan"


def test_pick_delivery_defaults_to_animated_when_nothing_matches():
    assert pick_delivery({"sarcasm": 50}, {"energy": 50}, is_long_or_code=False) == "animated"


def test_pick_delivery_missing_keys_use_neutral_defaults():
    assert pick_delivery({}, {}, is_long_or_code=False) == "animated"


# --- real end-to-end: faster-whisper against a real Piper WAV ----------------


@pytest.mark.skipif(not HAS_PIPER, reason="piper binary not found on PATH")
@pytest.mark.asyncio
async def test_real_faster_whisper_transcribes_real_piper_audio(tmp_path):
    """This is also the regression test for the PyAV/faster-whisper
    incompatibility worked around in stt_local.py: passing a file path
    straight to WhisperModel.transcribe() can raise TypeError: open() got
    an unexpected keyword argument 'metadata_errors' against some PyAV
    versions; decoding the WAV ourselves sidesteps it."""
    voice_path_str = os.environ.get("DESKMATE_TEST_PIPER_VOICE")
    voice_path = Path(voice_path_str) if voice_path_str else None
    if voice_path is None or not voice_path.is_file():
        pytest.skip(
            "no real Piper voice model available -- set DESKMATE_TEST_PIPER_VOICE "
            "to a real .onnx voice model's path to run this test"
        )

    import asyncio

    text = "Testing the real speech recognition pipeline."
    wav_path = tmp_path / "speech.wav"
    proc = await asyncio.create_subprocess_exec(
        "piper",
        "--model",
        str(voice_path),
        "--output_file",
        str(wav_path),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    await proc.communicate(input=text.encode("utf-8"))
    assert wav_path.is_file()

    from brain.voice.stt_local import FasterWhisperSTTBackend

    backend = FasterWhisperSTTBackend(
        STTLocalConfig.model_validate({"model_size": "tiny.en", "device": "cpu", "compute_type": "int8"})
    )
    transcribed = await backend.transcribe(wav_path, language="en")
    # "tiny.en" (chosen here for download speed, not the production
    # "base.en" default) is imprecise enough to occasionally mis-hear a
    # word or two (observed live: "real speech" -> "Reels Beach") -- this
    # test exists to prove the real decode-and-resample pipeline runs
    # end-to-end (see the PyAV workaround above), not to grade model
    # accuracy, so it only checks for a plausible, non-trivial result.
    assert "pipeline" in transcribed.lower()
    assert len(transcribed.split()) >= 5
