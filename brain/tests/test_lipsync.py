"""Tests for brain.voice.lipsync. Includes a real end-to-end integration
test against the actual installed `rhubarb` binary (and, if available,
`piper`) -- skipped automatically if either isn't on PATH. See
brain/voice/lipsync.py's module docstring for why no `-r` flag is passed.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import wave
from pathlib import Path

import pytest

from brain.voice.lipsync import (
    LipSyncError,
    RhubarbLipSync,
    VisemeFrame,
    extract_visemes_if_needed,
)

HAS_RHUBARB = shutil.which("rhubarb") is not None
HAS_PIPER = shutil.which("piper") is not None


def _silent_wav(path: Path, duration_s: float = 0.5) -> None:
    with wave.open(str(path), "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(16000)
        f.writeframes(b"\x00\x00" * int(16000 * duration_s))


# --- pure / mocked: error handling, bypass policy ----------------------------


@pytest.mark.asyncio
async def test_missing_binary_raises_lipsync_error(tmp_path):
    lipsync = RhubarbLipSync("/definitely/not/a/real/rhubarb/binary")
    wav_path = tmp_path / "silence.wav"
    _silent_wav(wav_path)
    with pytest.raises(LipSyncError, match="not found"):
        await lipsync.extract_visemes(wav_path)


@pytest.mark.asyncio
async def test_nonzero_exit_raises_lipsync_error(tmp_path, monkeypatch):
    lipsync = RhubarbLipSync("rhubarb")

    class _FakeProc:
        returncode = 1

        async def communicate(self):
            return b"", b"could not open input file"

    async def fake_exec(*args, **kwargs):
        return _FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    with pytest.raises(LipSyncError, match="could not open input file"):
        await lipsync.extract_visemes(tmp_path / "missing.wav")


@pytest.mark.asyncio
async def test_extract_visemes_if_needed_bypassed_when_faceless(tmp_path):
    lipsync = RhubarbLipSync("/should/never/be/invoked")
    result = await extract_visemes_if_needed(lipsync, tmp_path / "x.wav", has_face=False)
    assert result == []


@pytest.mark.asyncio
async def test_extract_visemes_parses_mouth_cues_from_mocked_output(tmp_path, monkeypatch):
    """Mocks the subprocess but exercises real JSON parsing + shape_key_name
    mapping, independent of whether rhubarb is actually installed here."""
    lipsync = RhubarbLipSync("rhubarb")
    captured_args = {}

    class _FakeProc:
        returncode = 0

        async def communicate(self):
            return b"", b""

    async def fake_exec(*args, **kwargs):
        captured_args["args"] = args
        # args[-1] is the wav path; -o's value is two before it when no dialog file
        out_index = args.index("-o") + 1
        out_path = Path(args[out_index])
        out_path.write_text(json.dumps({"mouthCues": [{"start": 0.0, "end": 0.1, "value": "X"}, {"start": 0.1, "end": 0.3, "value": "D"}]}))
        return _FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    frames = await lipsync.extract_visemes(tmp_path / "speech.wav", dialog_text="hello")

    assert frames == [
        VisemeFrame(start_s=0.0, end_s=0.1, shape_key="viseme_X"),
        VisemeFrame(start_s=0.1, end_s=0.3, shape_key="viseme_D"),
    ]
    assert "--dialogFile" in captured_args["args"]


# --- real end-to-end integration ----------------------------------------------


@pytest.mark.skipif(not HAS_RHUBARB, reason="rhubarb binary not found on PATH")
@pytest.mark.asyncio
async def test_real_rhubarb_on_silence_produces_rest_viseme(tmp_path):
    """Real rhubarb, no piper needed: a silent WAV should produce a
    rest/closed-mouth viseme (X) covering it, not an error."""
    lipsync = RhubarbLipSync(shutil.which("rhubarb"))
    wav_path = tmp_path / "silence.wav"
    _silent_wav(wav_path, duration_s=1.0)

    frames = await lipsync.extract_visemes(wav_path)
    assert len(frames) >= 1
    assert all(f.shape_key.startswith("viseme_") for f in frames)
    assert frames[0].shape_key == "viseme_X"


@pytest.mark.skipif(not (HAS_RHUBARB and HAS_PIPER), reason="needs both rhubarb and piper on PATH")
@pytest.mark.asyncio
async def test_real_piper_to_rhubarb_end_to_end(tmp_path):
    """The actual pipeline this module exists for: real TTS audio in,
    real plausible viseme timing out, matching the exact format
    blender/viseme_shapekeys.py expects."""
    wav_path = tmp_path / "speech.wav"
    text = "Hello there, this is a real end to end test."
    proc = await asyncio.create_subprocess_exec(
        "piper",
        "--model",
        str(_find_piper_voice()),
        "--output_file",
        str(wav_path),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    await proc.communicate(input=text.encode("utf-8"))
    assert wav_path.is_file()

    lipsync = RhubarbLipSync(shutil.which("rhubarb"))
    frames = await extract_visemes_if_needed(lipsync, wav_path, has_face=True, dialog_text=text)

    assert len(frames) > 1
    assert all(f.shape_key.startswith("viseme_") for f in frames)
    assert frames[0].start_s == 0.0
    # frames should be in non-decreasing chronological order
    assert all(frames[i].start_s <= frames[i + 1].start_s for i in range(len(frames) - 1))


def _find_piper_voice() -> Path:
    import os

    env_override = os.environ.get("DESKMATE_TEST_PIPER_VOICE")
    if env_override and Path(env_override).is_file():
        return Path(env_override)
    pytest.skip(
        "no Piper voice model found -- set DESKMATE_TEST_PIPER_VOICE to a "
        "real .onnx voice model's path to run this test"
    )


async def test_recognizer_flag_is_passed_to_rhubarb(monkeypatch, tmp_path):
    seen = {}

    async def fake_exec(*args, **kwargs):
        seen["args"] = args
        Path(args[args.index("-o") + 1]).write_text('{"mouthCues": []}')

        class _Proc:
            returncode = 0

            async def communicate(self):
                return b"", b""

        return _Proc()

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)
    wav = tmp_path / "x.wav"
    wav.write_bytes(b"RIFF")
    await RhubarbLipSync("rhubarb").extract_visemes(wav)  # default: the fast recognizer
    assert seen["args"][seen["args"].index("-r") + 1] == "phonetic"
    await RhubarbLipSync("rhubarb", "pocketSphinx").extract_visemes(wav)
    assert seen["args"][seen["args"].index("-r") + 1] == "pocketSphinx"
