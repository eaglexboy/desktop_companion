"""Tests for brain.voice.stereo (mono -> stereo WAV)."""

from __future__ import annotations

import struct
import wave
from pathlib import Path

import pytest

from brain.voice.stereo import ensure_stereo_wav


def _write(path: Path, channels: int, width: int, rate: int, frames: bytes) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(width)
        w.setframerate(rate)
        w.writeframes(frames)


def _read(path: Path) -> tuple[int, int, int, bytes]:
    with wave.open(str(path), "rb") as w:
        return w.getnchannels(), w.getsampwidth(), w.getframerate(), w.readframes(w.getnframes())


def test_mono_16bit_becomes_two_identical_channels(tmp_path):
    samples = [0, 1000, -1000, 32767, -32768, 5]
    wav = tmp_path / "a.wav"
    _write(wav, 1, 2, 24000, struct.pack(f"<{len(samples)}h", *samples))

    assert ensure_stereo_wav(wav) is True

    channels, width, rate, data = _read(wav)
    assert (channels, width, rate) == (2, 2, 24000)
    interleaved = struct.unpack(f"<{len(samples) * 2}h", data)
    assert list(interleaved[0::2]) == samples  # left
    assert list(interleaved[1::2]) == samples  # right


@pytest.mark.parametrize("width", [1, 3, 4])
def test_other_sample_widths_duplicate_whole_samples(tmp_path, width):
    frames = bytes(range(1, 1 + width * 4))  # 4 distinct samples
    wav = tmp_path / "a.wav"
    _write(wav, 1, width, 16000, frames)

    ensure_stereo_wav(wav)

    channels, got_width, _, data = _read(wav)
    assert (channels, got_width) == (2, width)
    expected = b"".join(frames[i : i + width] * 2 for i in range(0, len(frames), width))
    assert data == expected


def test_stereo_file_is_left_untouched(tmp_path):
    wav = tmp_path / "a.wav"
    _write(wav, 2, 2, 24000, struct.pack("<4h", 1, 2, 3, 4))
    before = wav.read_bytes()

    assert ensure_stereo_wav(wav) is False
    assert wav.read_bytes() == before


def test_empty_mono_file_stays_valid(tmp_path):
    wav = tmp_path / "a.wav"
    _write(wav, 1, 2, 24000, b"")

    ensure_stereo_wav(wav)

    assert _read(wav) == (2, 2, 24000, b"")


def test_no_temp_file_left_behind(tmp_path):
    wav = tmp_path / "a.wav"
    _write(wav, 1, 2, 24000, struct.pack("<3h", 1, 2, 3))

    ensure_stereo_wav(wav)

    assert [p.name for p in tmp_path.iterdir()] == ["a.wav"]


def test_unreadable_file_raises_and_is_unchanged(tmp_path):
    wav = tmp_path / "a.wav"
    wav.write_bytes(b"this is not a wav file")

    with pytest.raises(wave.Error):
        ensure_stereo_wav(wav)

    assert wav.read_bytes() == b"this is not a wav file"
    assert [p.name for p in tmp_path.iterdir()] == ["a.wav"]
