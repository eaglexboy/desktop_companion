"""Wraps the Rhubarb Lip Sync CLI (MIT-licensed, free) to turn a WAV (+
optional known dialog text, which improves accuracy) into viseme timing
frames using the shared viseme codes from blender/viseme_shapekeys.py.

The recognizer is passed explicitly (`-r`) and is configurable
(`[voice] lipsync_recognizer`): `pocketSphinx` is Rhubarb's English-tuned
recognizer but ran at roughly real time on this hardware, while `phonetic`
(language-independent) took about a fifth of that for a similar spread of
mouth shapes. Because extraction runs once per spoken sentence, before that
sentence can play, its speed directly sets the delay before speech and the
gaps between sentences -- so the fast one is the default. Either produces
well-formed `mouthCues` in exactly this module's expected `{start, end, value}`
shape.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from dataclasses import dataclass
from pathlib import Path

from blender.viseme_shapekeys import shape_key_name


@dataclass(frozen=True)
class VisemeFrame:
    start_s: float
    end_s: float
    shape_key: str


class LipSyncError(RuntimeError):
    pass


class RhubarbLipSync:
    def __init__(self, rhubarb_bin: str, recognizer: str = "phonetic") -> None:
        self.rhubarb_bin = rhubarb_bin
        self.recognizer = recognizer

    async def extract_visemes(self, wav_path: Path, dialog_text: str | None = None) -> list[VisemeFrame]:
        """Runs rhubarb against `wav_path`, optionally passing `dialog_text`
        (the known text that was synthesized -- always available here,
        since we generated the audio ourselves, which is exactly the case
        Rhubarb's own docs say `--dialogFile` most improves)."""
        with tempfile.TemporaryDirectory() as tmp:
            out_json = Path(tmp) / "visemes.json"
            args = [self.rhubarb_bin, "-r", self.recognizer, "-f", "json", "-o", str(out_json)]
            if dialog_text:
                dialog_file = Path(tmp) / "dialog.txt"
                dialog_file.write_text(dialog_text)
                args += ["--dialogFile", str(dialog_file)]
            args.append(str(wav_path))

            try:
                proc = await asyncio.create_subprocess_exec(
                    *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
                )
            except FileNotFoundError as exc:
                raise LipSyncError(
                    f"rhubarb binary not found at {self.rhubarb_bin!r} -- download a release "
                    "from https://github.com/DanielSWolf/rhubarb-lip-sync/releases and set "
                    "voice.rhubarb_bin to its path."
                ) from exc

            _, stderr = await proc.communicate()
            if proc.returncode != 0:
                raise LipSyncError(f"rhubarb failed ({proc.returncode}): {stderr.decode(errors='replace')}")

            data = json.loads(out_json.read_text())

        return [
            VisemeFrame(start_s=cue["start"], end_s=cue["end"], shape_key=shape_key_name(cue["value"]))
            for cue in data.get("mouthCues", [])
        ]


async def extract_visemes_if_needed(
    lipsync: RhubarbLipSync, wav_path: Path, *, has_face: bool, dialog_text: str | None = None
) -> list[VisemeFrame]:
    """Extraction is bypassed completely if has_face == false -- a faceless
    avatar has no viseme shape keys to drive at all (see
    blender/generator.py's Mode B / talk_pulse), so there's no reason to
    even shell out to rhubarb."""
    if not has_face:
        return []
    return await lipsync.extract_visemes(wav_path, dialog_text)
