"""Default local TTS backend: Piper (https://github.com/OHF-Voice/piper1-gpl;
free, local, GPL-3.0). Shells out to the `piper` CLI rather than importing
its Python API, so this backend doesn't care which Piper version or
packaging is installed -- just that a `piper` executable is on PATH (or
`piper_bin` points at one).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from brain.config import TTSLocalConfig
from brain.voice.tts import TTSBackend


class PiperVoiceNotFound(RuntimeError):
    pass


class PiperTTSBackend(TTSBackend):
    def __init__(self, config: TTSLocalConfig, voices_dir: Path, *, piper_bin: str = "piper") -> None:
        self._config = config
        self._voices_dir = voices_dir
        self._piper_bin = piper_bin

    def _resolve_voice_path(self, voice_id: str | None) -> Path:
        """`voice_id` is an accommodation for a future voice-catalog feature
        (`agent_state.current_voice_id` format: "piper:<stem>" or
        "remote:<id>") -- there's no catalog of alternate local voices yet,
        but if a "piper:<stem>" id names a model that actually exists in
        voices_dir, this honors it rather than ignoring it outright; any
        other value (including None) falls back to the one configured
        default voice."""
        if voice_id and voice_id.startswith("piper:"):
            candidate = self._voices_dir / f"{voice_id.removeprefix('piper:')}.onnx"
            if candidate.is_file():
                return candidate
        return self._voices_dir / self._config.voice_model_path

    async def health_check(self) -> bool:
        import shutil

        return shutil.which(self._piper_bin) is not None and self._resolve_voice_path(None).is_file()

    async def synthesize(
        self, text: str, out_wav_path: Path, *, voice_id: str | None = None, params: dict[str, Any] | None = None
    ) -> None:
        onnx_path = self._resolve_voice_path(voice_id)
        if not onnx_path.is_file():
            raise PiperVoiceNotFound(
                f"Piper voice model not found at {onnx_path}. Download it (and its .onnx.json "
                f"config) with `python -m piper.download_voices --download-dir {self._voices_dir} "
                "<voice-name>` (see https://github.com/rhasspy/piper/blob/master/VOICES.md)."
            )

        out_wav_path.parent.mkdir(parents=True, exist_ok=True)
        args = [self._piper_bin, "--model", str(onnx_path), "--output_file", str(out_wav_path)]
        # Passes through e.g. length_scale/noise_scale/speaker as --flag
        # value pairs without this backend needing to know Piper's full CLI
        # surface -- same "params dict, opaque to the client" convention
        # used by TTSRemoteEntry.params and LLMCloudConfig.extra_body.
        for key, value in (params or {}).items():
            args += [f"--{key}", str(value)]

        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise RuntimeError(f"`{self._piper_bin}` executable not found (pip install piper-tts)") from exc

        _, stderr = await proc.communicate(input=text.encode("utf-8"))
        if proc.returncode != 0:
            raise RuntimeError(f"piper failed ({proc.returncode}): {stderr.decode(errors='replace')}")
