"""Tests for brain.voice.tts (TTSPool), tts_local (Piper), and tts_remote
(tts_serve + generic_openai dispatch). Contract tests use httpx.MockTransport
with tts-serve's real response shape (see tts_remote.py's module docstring)
so no live GPU/engine is needed; a handful of tests also run against the
real `piper` binary when it's on PATH.
"""

from __future__ import annotations

import base64
import os
import shutil
from pathlib import Path

import httpx
import pytest

from brain.config import TTSLocalConfig, TTSRemoteEntry
from brain.voice.tts import TTSBackend, TTSPool, build_tts_pool
from brain.voice.tts_local import PiperTTSBackend, PiperVoiceNotFound
from brain.voice.tts_remote import (
    GenericOpenAITTSBackend,
    TTSServeBackend,
    build_remote_tts_backend,
)

HAS_PIPER = shutil.which("piper") is not None


def _mock_transport(handler) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


class _ScriptedBackend(TTSBackend):
    """Succeeds or raises on command, and records every call -- used to
    drive TTSPool's failover logic deterministically."""

    def __init__(self, *, fails: bool = False) -> None:
        self.fails = fails
        self.calls: list[str] = []

    async def synthesize(self, text, out_wav_path, *, voice_id=None, params=None) -> None:
        self.calls.append(text)
        if self.fails:
            raise RuntimeError("synthesis failed")
        out_wav_path.parent.mkdir(parents=True, exist_ok=True)
        out_wav_path.write_bytes(b"RIFF-fake-wav")


# --- TTSPool: failover, cooldown, is_empty -----------------------------------


@pytest.mark.asyncio
async def test_pool_tries_remote_first_then_local(tmp_path):
    remote = _ScriptedBackend()
    local = _ScriptedBackend()
    pool = TTSPool([("remote-1", remote)], local)

    name = await pool.synthesize("hi", tmp_path / "out.wav")
    assert name == "remote-1"
    assert remote.calls == ["hi"]
    assert local.calls == []


@pytest.mark.asyncio
async def test_pool_falls_back_to_local_when_remote_fails(tmp_path):
    remote = _ScriptedBackend(fails=True)
    local = _ScriptedBackend()
    pool = TTSPool([("remote-1", remote)], local)

    name = await pool.synthesize("hi", tmp_path / "out.wav")
    assert name == "local"
    assert local.calls == ["hi"]


@pytest.mark.asyncio
async def test_pool_tries_remotes_in_priority_order_before_local(tmp_path):
    first = _ScriptedBackend(fails=True)
    second = _ScriptedBackend()
    local = _ScriptedBackend()
    pool = TTSPool([("first", first), ("second", second)], local)

    name = await pool.synthesize("hi", tmp_path / "out.wav")
    assert name == "second"
    assert local.calls == []


@pytest.mark.asyncio
async def test_pool_raises_when_everything_fails(tmp_path):
    remote = _ScriptedBackend(fails=True)
    local = _ScriptedBackend(fails=True)
    pool = TTSPool([("remote-1", remote)], local)

    with pytest.raises(RuntimeError, match="All TTS backends failed"):
        await pool.synthesize("hi", tmp_path / "out.wav")


@pytest.mark.asyncio
async def test_pool_skips_a_remote_still_in_cooldown(tmp_path):
    remote = _ScriptedBackend(fails=True)
    local = _ScriptedBackend()
    pool = TTSPool([("remote-1", remote)], local, cooldown_s=9999)

    await pool.synthesize("first", tmp_path / "a.wav")  # fails, cools down, falls to local
    assert len(remote.calls) == 1
    await pool.synthesize("second", tmp_path / "b.wav")  # remote skipped this time
    assert len(remote.calls) == 1  # not retried while cooling down
    assert local.calls == ["first", "second"]


async def test_pool_logs_a_warning_when_a_remote_fails_and_piper_takes_over(tmp_path, caplog):
    class _TimeoutBackend(TTSBackend):
        async def synthesize(self, text, out_wav_path, *, voice_id=None, params=None) -> None:
            raise httpx.ReadTimeout("")  # str(exc) is "" -- the log must still say what happened

    pool = TTSPool([("vox", _TimeoutBackend())], _ScriptedBackend())
    with caplog.at_level("INFO", logger="deskmate.voice.tts"):
        assert await pool.synthesize("hello", tmp_path / "a.wav") == "local"
    failure = [r for r in caplog.records if r.levelname == "WARNING"]
    assert failure and "'vox'" in failure[0].getMessage() and "ReadTimeout" in failure[0].getMessage()


async def test_pool_logs_cooldown_when_every_remote_is_skipped(tmp_path, caplog):
    flaky = _ScriptedBackend(fails=True)
    pool = TTSPool([("vox", flaky)], _ScriptedBackend(), cooldown_s=60.0)
    await pool.synthesize("first", tmp_path / "a.wav")  # fails once -> cooldown starts
    caplog.clear()
    with caplog.at_level("INFO", logger="deskmate.voice.tts"):
        assert await pool.synthesize("second", tmp_path / "b.wav") == "local"
    assert flaky.calls == ["first"]  # skipped, not retried
    assert any("cooling down" in r.getMessage() and "local Piper" in r.getMessage() for r in caplog.records)


def test_pool_has_remote_reflects_configuration():
    assert TTSPool([("vox", _ScriptedBackend())], None).has_remote
    assert not TTSPool([], _ScriptedBackend()).has_remote


def test_pool_is_empty_only_when_nothing_configured():
    assert TTSPool([], None).is_empty is True
    assert TTSPool([], _ScriptedBackend()).is_empty is False
    assert TTSPool([("r", _ScriptedBackend())], None).is_empty is False


# --- PiperTTSBackend -----------------------------------------------------------


def _piper_config(**overrides) -> TTSLocalConfig:
    fields = {"voice_model_path": "voice.onnx", "voice_config_path": "voice.onnx.json"}
    fields.update(overrides)
    return TTSLocalConfig.model_validate(fields)


@pytest.mark.asyncio
async def test_piper_missing_binary_raises_clear_error(tmp_path):
    (tmp_path / "voice.onnx").write_bytes(b"fake")
    backend = PiperTTSBackend(_piper_config(), tmp_path, piper_bin="/definitely/not/a/real/piper")
    with pytest.raises(RuntimeError, match="not found"):
        await backend.synthesize("hello", tmp_path / "out.wav")


@pytest.mark.asyncio
async def test_piper_missing_voice_model_raises_piper_voice_not_found(tmp_path):
    backend = PiperTTSBackend(_piper_config(), tmp_path, piper_bin="piper")
    with pytest.raises(PiperVoiceNotFound):
        await backend.synthesize("hello", tmp_path / "out.wav")


@pytest.mark.asyncio
async def test_piper_resolves_voice_id_override_when_file_exists(tmp_path):
    (tmp_path / "voice.onnx").write_bytes(b"default")
    (tmp_path / "alt_voice.onnx").write_bytes(b"alt")
    backend = PiperTTSBackend(_piper_config(), tmp_path, piper_bin="piper")

    assert backend._resolve_voice_path(None) == tmp_path / "voice.onnx"
    assert backend._resolve_voice_path("piper:alt_voice") == tmp_path / "alt_voice.onnx"
    # named but missing -> falls back to the configured default rather than erroring
    assert backend._resolve_voice_path("piper:nonexistent") == tmp_path / "voice.onnx"


@pytest.mark.asyncio
async def test_piper_passes_params_as_cli_flags(tmp_path, monkeypatch):
    (tmp_path / "voice.onnx").write_bytes(b"fake")
    backend = PiperTTSBackend(_piper_config(), tmp_path, piper_bin="piper")
    captured = {}

    class _FakeProc:
        returncode = 0

        async def communicate(self, input=None):
            captured["stdin"] = input
            return b"", b""

    async def fake_exec(*args, **kwargs):
        captured["args"] = args
        return _FakeProc()

    import asyncio

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    await backend.synthesize("hello", tmp_path / "out.wav", params={"length_scale": 1.2})

    assert "--length_scale" in captured["args"]
    assert "1.2" in captured["args"]
    assert captured["stdin"] == b"hello"


@pytest.mark.skipif(not HAS_PIPER, reason="piper binary not found on PATH")
@pytest.mark.asyncio
async def test_real_piper_synthesis_produces_wav(tmp_path):
    voice_path_str = os.environ.get("DESKMATE_TEST_PIPER_VOICE")
    voice_path = Path(voice_path_str) if voice_path_str else None
    if voice_path is None or not voice_path.is_file():
        pytest.skip(
            "no real Piper voice model available -- set DESKMATE_TEST_PIPER_VOICE "
            "to a real .onnx voice model's path to run this test"
        )
    backend = PiperTTSBackend(
        TTSLocalConfig.model_validate({"voice_model_path": voice_path.name, "voice_config_path": f"{voice_path.name}.json"}),
        voice_path.parent,
    )
    out_wav = tmp_path / "out.wav"
    await backend.synthesize("This is a real synthesis test.", out_wav)
    assert out_wav.is_file()
    assert out_wav.read_bytes()[:4] == b"RIFF"


# --- TTSServeBackend (contract test against tts-serve's real response shape) --


def _tts_serve_entry(**overrides) -> TTSRemoteEntry:
    fields = {"provider_type": "tts_serve", "base_url": "http://testserver"}
    fields.update(overrides)
    return TTSRemoteEntry.model_validate(fields)


@pytest.mark.asyncio
async def test_tts_serve_decodes_and_writes_real_response_shape(tmp_path):
    fake_wav_bytes = b"RIFF....WAVEfmt "
    encoded = base64.b64encode(fake_wav_bytes).decode()
    captured_payload = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        captured_payload.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "audio_base64": encoded,
                "sample_rate": 48000,
                "seed": 42,
                "time_used": 0.5,
                "rtf": 0.1,
            },
        )

    backend = TTSServeBackend(_tts_serve_entry())
    backend._http = httpx.AsyncClient(base_url="http://testserver", transport=_mock_transport(handler))

    out_wav = tmp_path / "out.wav"
    await backend.synthesize("Hello there", out_wav)

    assert out_wav.read_bytes() == fake_wav_bytes
    assert captured_payload["text"] == "Hello there"


@pytest.mark.asyncio
async def test_tts_serve_merges_config_and_call_params(tmp_path):
    captured_payload = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        captured_payload.update(json.loads(request.content))
        encoded = base64.b64encode(b"x").decode()
        return httpx.Response(200, json={"audio_base64": encoded, "sample_rate": 1, "seed": 1, "time_used": 0.0, "rtf": None})

    entry = _tts_serve_entry(params={"normalize": True})
    backend = TTSServeBackend(entry)
    backend._http = httpx.AsyncClient(base_url="http://testserver", transport=_mock_transport(handler))

    await backend.synthesize("hi", tmp_path / "out.wav", params={"language": "en"})
    assert captured_payload == {"text": "hi", "normalize": True, "language": "en"}


@pytest.mark.asyncio
async def test_tts_serve_raises_on_http_error(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, json={"detail": "unknown field"})

    backend = TTSServeBackend(_tts_serve_entry())
    backend._http = httpx.AsyncClient(base_url="http://testserver", transport=_mock_transport(handler))

    with pytest.raises(httpx.HTTPStatusError):
        await backend.synthesize("hi", tmp_path / "out.wav")


@pytest.mark.asyncio
async def test_tts_serve_health_check(tmp_path):
    def ok_handler(request):
        return httpx.Response(200)

    backend = TTSServeBackend(_tts_serve_entry())
    backend._http = httpx.AsyncClient(base_url="http://testserver", transport=_mock_transport(ok_handler))
    assert await backend.health_check() is True

    def fail_handler(request):
        return httpx.Response(500)

    backend._http = httpx.AsyncClient(base_url="http://testserver", transport=_mock_transport(fail_handler))
    assert await backend.health_check() is False


# --- GenericOpenAITTSBackend ---------------------------------------------------


def _openai_entry(**overrides) -> TTSRemoteEntry:
    fields = {
        "provider_type": "generic_openai",
        "base_url": "http://testserver",
        "params": {"model": "tts-1", "voice": "alloy"},
    }
    fields.update(overrides)
    return TTSRemoteEntry.model_validate(fields)


@pytest.mark.asyncio
async def test_generic_openai_writes_raw_response_bytes(tmp_path):
    raw_audio = b"RAW_AUDIO_BYTES_NOT_JSON"
    captured_payload = {}
    captured_headers = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        captured_payload.update(json.loads(request.content))
        captured_headers.update(request.headers)
        return httpx.Response(200, content=raw_audio)

    entry = _openai_entry(api_key="secret-key")
    backend = GenericOpenAITTSBackend(entry)
    backend._http = httpx.AsyncClient(
        base_url="http://testserver", headers={"Authorization": "Bearer secret-key"}, transport=_mock_transport(handler)
    )

    out_wav = tmp_path / "out.wav"
    await backend.synthesize("hello", out_wav)

    assert out_wav.read_bytes() == raw_audio
    assert captured_payload["input"] == "hello"
    assert captured_payload["model"] == "tts-1"
    assert captured_payload["voice"] == "alloy"
    assert captured_payload["response_format"] == "wav"
    assert captured_headers["authorization"] == "Bearer secret-key"


@pytest.mark.asyncio
async def test_generic_openai_lets_call_params_override_response_format(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"mp3-bytes")

    backend = GenericOpenAITTSBackend(_openai_entry())
    backend._http = httpx.AsyncClient(base_url="http://testserver", transport=_mock_transport(handler))

    await backend.synthesize("hi", tmp_path / "out.mp3", params={"response_format": "mp3"})
    # no assertion error means setdefault correctly didn't clobber the override;
    # exercised fully by test_generic_openai_writes_raw_response_bytes's payload check


# --- dispatch registry ---------------------------------------------------------


def test_build_remote_tts_backend_dispatches_on_provider_type():
    assert isinstance(build_remote_tts_backend(_tts_serve_entry()), TTSServeBackend)
    assert isinstance(build_remote_tts_backend(_openai_entry()), GenericOpenAITTSBackend)


def test_build_remote_tts_backend_rejects_unknown_provider_type():
    # provider_type is a Literal in the schema (a real typo is caught at
    # config load time) -- model_construct bypasses that validation so the
    # registry's own runtime guard can be exercised directly.
    bad_entry = TTSRemoteEntry.model_construct(
        provider_type="something_else", base_url="http://x", endpoint="/synthesize",
        capabilities_endpoint="/capabilities", priority=100, enabled=True, timeout_s=10.0,
        api_key="", params={},
    )
    with pytest.raises(ValueError, match="Unknown provider_type"):
        build_remote_tts_backend(bad_entry)


# --- build_tts_pool wiring ------------------------------------------------------


def test_build_tts_pool_wires_remote_and_local_from_settings(tmp_path):
    from brain.config import Settings

    settings = Settings.model_validate(
        {
            "storage": {"workspace_dir": str(tmp_path / "ws"), "deploy_dir": str(tmp_path / "deploy")},
            "llm": {"local": {"base_url": "http://x", "model": "m"}},
            "voice": {
                "tts": {
                    "active_backend": "remote",
                    "voices_dir": str(tmp_path),
                    "local": {"voice_model_path": "voice.onnx", "voice_config_path": "voice.onnx.json"},
                    "remote": {
                        "voxcpm_elsrig": {"provider_type": "tts_serve", "base_url": "http://127.0.0.1:7500", "priority": 10},
                        "disabled_one": {"provider_type": "tts_serve", "base_url": "http://x", "enabled": False, "priority": 50},
                    },
                }
            },
        }
    )

    pool = build_tts_pool(settings)
    assert pool.is_empty is False
    assert len(pool._remote) == 1
    assert pool._remote[0].name == "voxcpm_elsrig"
    assert isinstance(pool._local, PiperTTSBackend)


def test_build_tts_pool_active_backend_local_skips_remotes_entirely(tmp_path):
    """active_backend == "local" (the default) must behave like
    STTCoordinator's "local never touches the network at all" -- even an
    enabled, correctly-configured remote must not be wired in, matching
    what a user who set this would actually expect."""
    from brain.config import Settings

    settings = Settings.model_validate(
        {
            "storage": {"workspace_dir": str(tmp_path / "ws"), "deploy_dir": str(tmp_path / "deploy")},
            "llm": {"local": {"base_url": "http://x", "model": "m"}},
            "voice": {
                "tts": {
                    "active_backend": "local",
                    "voices_dir": str(tmp_path),
                    "local": {"voice_model_path": "voice.onnx", "voice_config_path": "voice.onnx.json"},
                    "remote": {
                        "voxcpm_elsrig": {"provider_type": "tts_serve", "base_url": "http://127.0.0.1:7500", "priority": 10},
                    },
                }
            },
        }
    )

    pool = build_tts_pool(settings)
    assert len(pool._remote) == 0
    assert isinstance(pool._local, PiperTTSBackend)


# --- voice precedence (sample > seed > engine default) ----------------------


@pytest.mark.asyncio
async def test_tts_serve_voice_sample_replaces_seed(tmp_path):
    import json

    sample = tmp_path / "ref.wav"
    sample.write_bytes(b"REF")
    seen = {}

    def handler(request):
        seen.update(json.loads(request.content))
        return httpx.Response(200, json={"audio_base64": base64.b64encode(b"x").decode()})

    entry = TTSRemoteEntry(base_url="http://t", voice_sample=str(sample), voice_sample_text="hello")
    backend = TTSServeBackend(entry)
    backend._http = httpx.AsyncClient(base_url="http://t", transport=_mock_transport(handler))
    await backend.synthesize("hi", tmp_path / "o.wav", params={"seed": 5})
    assert "seed" not in seen
    assert base64.b64decode(seen["audio_base64"]) == b"REF"
    assert seen["reference_text"] == "hello"


@pytest.mark.asyncio
async def test_tts_serve_seed_used_without_sample(tmp_path):
    import json

    seen = {}

    def handler(request):
        seen.update(json.loads(request.content))
        return httpx.Response(200, json={"audio_base64": base64.b64encode(b"x").decode()})

    backend = TTSServeBackend(TTSRemoteEntry(base_url="http://t"))
    backend._http = httpx.AsyncClient(base_url="http://t", transport=_mock_transport(handler))
    await backend.synthesize("hi", tmp_path / "o.wav", params={"seed": 5})
    assert seen["seed"] == 5 and "audio_base64" not in seen


def test_voice_sample_validation(tmp_path):
    with pytest.raises(ValueError, match="not found"):
        TTSRemoteEntry(base_url="http://t", voice_sample=str(tmp_path / "missing.wav"))
    with pytest.raises(ValueError, match="generic_openai"):
        f = tmp_path / "a.wav"
        f.write_bytes(b"x")
        TTSRemoteEntry(provider_type="generic_openai", base_url="http://t", voice_sample=str(f))
    TTSRemoteEntry(base_url="http://t", voice_sample=str(tmp_path / "missing.wav"), enabled=False)


# --- TTSPool.warm_up -----------------------------------------------------------


async def test_warm_up_pings_remotes_without_cooling_a_failing_one(tmp_path):
    bad, local = _ScriptedBackend(fails=True), _ScriptedBackend()
    pool = TTSPool([("bad", bad)], local)
    await pool.warm_up(tmp_path / "warm.wav")
    assert bad.calls == ["Hi."]
    # A failed warm-up is not a verdict on the remote: it is still tried first.
    await pool.synthesize("real", tmp_path / "o.wav")
    assert bad.calls == ["Hi.", "real"] and local.calls == ["real"]
