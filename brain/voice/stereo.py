"""Make a synthesized WAV stereo by copying its mono track to both channels.

Every TTS backend here returns mono audio. The overlay's Qt audio stack opens
the output device with the sink's own channel layout (5.1 on some desktops)
and decides for itself where a mono source lands -- on 5.1 that can be the
front-center channel, which may be muted or absent on a stereo pair of
speakers. Handing it a plain two-channel file removes the guesswork: the same
signal comes out of the left and right speakers.

Runs after lip-sync (rhubarb reads the original mono file) and just before the
chunk is announced to the overlay.
"""

from __future__ import annotations

import logging
import os
import wave
from pathlib import Path

logger = logging.getLogger("deskmate.voice.stereo")


def ensure_stereo_wav(path: Path) -> bool:
    """Rewrite `path` in place as 2-channel PCM if it is mono; returns whether
    it was changed. Already-stereo (or multi-channel) files are left alone.
    Works for any PCM sample width. Raises on an unreadable/non-PCM file --
    callers decide whether that is fatal."""
    with wave.open(str(path), "rb") as src:
        channels = src.getnchannels()
        if channels != 1:
            return False
        width = src.getsampwidth()
        rate = src.getframerate()
        frames = src.readframes(src.getnframes())

    # Interleave each sample with itself, byte-position by byte-position so the
    # same code serves 8/16/24/32-bit audio.
    out = bytearray(len(frames) * 2)
    for k in range(width):
        column = frames[k::width]
        out[k :: 2 * width] = column
        out[width + k :: 2 * width] = column

    tmp = path.with_name(path.name + ".stereo.tmp")
    try:
        with wave.open(str(tmp), "wb") as dst:
            dst.setnchannels(2)
            dst.setsampwidth(width)
            dst.setframerate(rate)
            dst.writeframes(bytes(out))
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
    return True
