"""Remote TTS backends, dispatched on `TTSRemoteEntry.provider_type` --
mirrors brain/llm_pool.py's `build_cloud_client()` registry pattern exactly.

- `"tts_serve"`: https://github.com/scorbo2/tts-serve's contract:
  `POST <endpoint>` with `{text, **params}` where `params` may include the
  optional common fields (`audio_base64`, `reference_text`, `language`,
  `seed`) plus whatever the engine advertises -> JSON response
  `{audio_base64, sample_rate, seed, time_used, rtf}`. `audio_base64` in the
  response is PCM_16 WAV data, base64-encoded -- decode and write directly,
  no WAV header construction needed. Voice precedence (engine-supported
  parts only): the entry's `voice_sample` (sent as `audio_base64`, plus
  `reference_text`), else a `seed`, else the engine default -- a sample
  drops any seed, since the engine would otherwise get both.
- `"generic_openai"`: the OpenAI `/v1/audio/speech` contract -- POST
  `{input, **params}` (`model`/`voice` are mandatory per that contract but
  engine-specific/user-configured, so they come from `params`, never
  defaulted here) -> raw audio bytes directly in the response body.
"""

from __future__ import annotations

import base64
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

from brain.config import TTSRemoteEntry
from brain.voice.tts import TTSBackend

logger = logging.getLogger("deskmate.voice.tts_remote")


def _read_voice_sample(config: TTSRemoteEntry) -> bytes | None:
    if not config.voice_sample:
        return None
    return Path(config.voice_sample).expanduser().read_bytes()


class TTSServeBackend(TTSBackend):
    def __init__(self, config: TTSRemoteEntry) -> None:
        self._config = config
        self._http = httpx.AsyncClient(base_url=config.base_url, timeout=config.timeout_s)

    async def synthesize(
        self, text: str, out_wav_path: Path, *, voice_id: str | None = None, params: dict[str, Any] | None = None
    ) -> None:
        # voice_id is an accommodation for a future voice-selection feature,
        # with no meaning for tts-serve today: each configured remote
        # *instance* already is one engine/voice -- choosing between
        # instances (via TTSPool's priority order) is the voice-selection
        # mechanism here, not a per-call field.
        payload = {"text": text, **self._config.params, **(params or {})}
        sample = _read_voice_sample(self._config)
        if sample is not None:
            # Sample beats seed: send the reference audio, never both.
            payload.pop("seed", None)
            payload["audio_base64"] = base64.b64encode(sample).decode("ascii")
            if self._config.voice_sample_text:
                payload["reference_text"] = self._config.voice_sample_text
        response = await self._http.post(self._config.endpoint, json=payload)
        response.raise_for_status()
        data = response.json()
        audio_bytes = base64.b64decode(data["audio_base64"], validate=True)
        out_wav_path.parent.mkdir(parents=True, exist_ok=True)
        out_wav_path.write_bytes(audio_bytes)

    async def health_check(self) -> bool:
        try:
            response = await self._http.get("/health")
            return response.status_code == 200
        except httpx.HTTPError:
            return False

    async def aclose(self) -> None:
        await self._http.aclose()


class GenericOpenAITTSBackend(TTSBackend):
    """No `health_check()` override, deliberately -- mirrors
    `llm_pool.GenericOpenAICloudClient`'s identical choice on the LLM side.
    There's no standardized health endpoint across arbitrary OpenAI-
    compatible hosts the way there is for a self-hosted service we control
    (`TTSServeBackend` above hits a real `GET /health`); inventing one
    (e.g. `GET /v1/models`) would add cost/auth surface on a metered API
    without actually confirming the speech endpoint works. Falls back to
    the ABC's `return True`."""

    def __init__(self, config: TTSRemoteEntry) -> None:
        self._config = config
        headers = {"Authorization": f"Bearer {config.api_key}"} if config.api_key else {}
        self._http = httpx.AsyncClient(base_url=config.base_url, timeout=config.timeout_s, headers=headers)

    async def synthesize(
        self, text: str, out_wav_path: Path, *, voice_id: str | None = None, params: dict[str, Any] | None = None
    ) -> None:
        # model/voice are mandatory per the OpenAI contract but have no
        # sane engine-agnostic default -- they must come from this entry's
        # own `params` (set by whoever configured this remote instance);
        # omitting them is a loud 422 from the server, not guessed here.
        payload = {"input": text, **self._config.params, **(params or {})}
        payload.setdefault("response_format", "wav")
        response = await self._http.post(self._config.endpoint, json=payload)
        response.raise_for_status()
        out_wav_path.parent.mkdir(parents=True, exist_ok=True)
        out_wav_path.write_bytes(response.content)

    async def aclose(self) -> None:
        await self._http.aclose()


_REMOTE_BACKEND_REGISTRY: dict[str, Callable[[TTSRemoteEntry], TTSBackend]] = {
    "tts_serve": TTSServeBackend,
    "generic_openai": GenericOpenAITTSBackend,
}


def build_remote_tts_backend(config: TTSRemoteEntry) -> TTSBackend:
    try:
        factory = _REMOTE_BACKEND_REGISTRY[config.provider_type]
    except KeyError:
        raise ValueError(
            f"Unknown provider_type {config.provider_type!r}. Known: {sorted(_REMOTE_BACKEND_REGISTRY)}"
        ) from None
    return factory(config)
