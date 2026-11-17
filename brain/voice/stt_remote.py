"""Remote STT backend: posts to an OpenAI-compatible
`/v1/audio/transcriptions` endpoint -- multipart file upload, not JSON, per
that API's actual contract.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx

from brain.config import STTRemoteConfig
from brain.language import normalize_language, primary_language
from brain.voice.stt import STTBackend, Transcript


class RemoteSTTBackend(STTBackend):
    def __init__(self, config: STTRemoteConfig) -> None:
        self._config = config
        headers = (
            {"Authorization": f"Bearer {config.api_key}"}
            if config.api_key
            else {}
        )
        self._http = httpx.AsyncClient(
            base_url=config.base_url, timeout=config.timeout_s, headers=headers
        )

    async def transcribe(
        self, wav_path: Path, *, language: str | None = None
    ) -> Transcript:
        data = {"model": self._config.model, **self._config.params}
        if language:
            data["language"] = primary_language(
                normalize_language(language) or language
            )
        wav_bytes = await asyncio.to_thread(wav_path.read_bytes)
        files = {"file": (wav_path.name, wav_bytes, "audio/wav")}
        response = await self._http.post(
            self._config.endpoint, data=data, files=files
        )
        response.raise_for_status()
        body = response.json()
        # Plain OpenAI "json" carries only text; verbose_json (set it in
        # [voice.stt.remote] params) also reports the detected language.
        return Transcript(
            body["text"], normalize_language(body.get("language")), None
        )

    async def aclose(self) -> None:
        await self._http.aclose()
