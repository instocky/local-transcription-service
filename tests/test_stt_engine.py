"""Tests for the Stage 3 STT engines (HLD-001 §4, §12).

No network, no gateway: `httpx.AsyncClient` is driven by an injected
`httpx.MockTransport`, so every request the engine makes is captured
and every gateway response is scripted. Assertions cover the request
wire shape (URL, bearer auth, multipart fields) and the error-code
mapping for each HLD-001 §12 row that touches STT.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from local_transcription_service.pipeline.base import PipelineError
from local_transcription_service.stt import litellm_whisper
from local_transcription_service.stt.litellm_whisper import LiteLLMWhisperSTT
from local_transcription_service.stt.mock import MockSTT

BASE_URL = "http://gw.test:4000/v1"
API_KEY = "sk-litellm-master-key"
MODEL = "whisper-large-v3-turbo"

Handler = Callable[[httpx.Request], httpx.Response]


def _install_transport(monkeypatch: pytest.MonkeyPatch, handler: Handler) -> list[httpx.Request]:
    """Route the engine's `httpx.AsyncClient` through a MockTransport.

    Returns a list that is appended with every issued request (already
    read, so `.content` is available for multipart assertions).
    """
    captured: list[httpx.Request] = []
    real_client = httpx.AsyncClient

    def _handler(request: httpx.Request) -> httpx.Response:
        request.read()  # materialise the (multipart) body for assertions
        captured.append(request)
        return handler(request)

    def _factory(*args: object, **kwargs: object) -> httpx.AsyncClient:
        kwargs["transport"] = httpx.MockTransport(_handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(litellm_whisper.httpx, "AsyncClient", _factory)
    return captured


def _models_ok(model: str = MODEL) -> httpx.Response:
    return httpx.Response(200, json={"object": "list", "data": [{"id": model}]})


def _make_engine() -> LiteLLMWhisperSTT:
    return LiteLLMWhisperSTT(base_url=BASE_URL, api_key=API_KEY, model=MODEL)


@pytest.fixture
def wav(tmp_path: Path) -> Path:
    p = tmp_path / "job-abc123.wav"
    p.write_bytes(b"RIFF....WAVEfmt ")  # a few bytes are enough for the mock transport
    return p


# ---------- transcribe: success path + wire contract ----------


async def test_transcribe_returns_text_and_sends_correct_request(
    monkeypatch: pytest.MonkeyPatch, wav: Path
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return _models_ok()
        return httpx.Response(200, json={"text": "hello world"})

    captured = _install_transport(monkeypatch, handler)
    result = await _make_engine().transcribe(wav, language="en")

    assert result == "hello world"

    # Two requests: the /models preflight, then the transcription POST.
    assert [r.method for r in captured] == ["GET", "POST"]
    get_req, post_req = captured

    assert str(get_req.url) == f"{BASE_URL}/models"
    assert get_req.headers["authorization"] == f"Bearer {API_KEY}"

    assert str(post_req.url) == f"{BASE_URL}/audio/transcriptions"
    assert post_req.headers["authorization"] == f"Bearer {API_KEY}"
    assert post_req.headers["content-type"].startswith("multipart/form-data")

    body = post_req.content.decode("utf-8", errors="replace")
    assert 'name="file"; filename="job-abc123.wav"' in body
    assert 'name="model"' in body and MODEL in body
    assert 'name="response_format"' in body and "json" in body
    assert 'name="language"' in body and "en" in body


async def test_transcribe_omits_language_when_not_given(
    monkeypatch: pytest.MonkeyPatch, wav: Path
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return _models_ok()
        return httpx.Response(200, json={"text": "no lang"})

    captured = _install_transport(monkeypatch, handler)
    result = await _make_engine().transcribe(wav)

    assert result == "no lang"
    post_body = captured[1].content.decode("utf-8", errors="replace")
    assert 'name="language"' not in post_body


# ---------- transcribe: error-code mapping (HLD-001 §12) ----------


async def test_transcribe_connection_refused_is_retryable(
    monkeypatch: pytest.MonkeyPatch, wav: Path
) -> None:
    """STT gateway down (connection refused) -> STT_GATEWAY_UNAVAILABLE, retryable."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    _install_transport(monkeypatch, handler)
    with pytest.raises(PipelineError) as exc_info:
        await _make_engine().transcribe(wav)

    assert exc_info.value.code == "STT_GATEWAY_UNAVAILABLE"
    assert exc_info.value.retryable is True


async def test_transcribe_timeout_is_retryable(
    monkeypatch: pytest.MonkeyPatch, wav: Path
) -> None:
    """Gateway timeout -> STT_GATEWAY_UNAVAILABLE, retryable."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    _install_transport(monkeypatch, handler)
    with pytest.raises(PipelineError) as exc_info:
        await _make_engine().transcribe(wav)

    assert exc_info.value.code == "STT_GATEWAY_UNAVAILABLE"
    assert exc_info.value.retryable is True


async def test_transcribe_5xx_on_post_is_retryable(
    monkeypatch: pytest.MonkeyPatch, wav: Path
) -> None:
    """Preflight OK, transcription POST 5xx -> STT_GATEWAY_UNAVAILABLE, retryable."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return _models_ok()
        return httpx.Response(503, text="upstream unavailable")

    _install_transport(monkeypatch, handler)
    with pytest.raises(PipelineError) as exc_info:
        await _make_engine().transcribe(wav)

    assert exc_info.value.code == "STT_GATEWAY_UNAVAILABLE"
    assert exc_info.value.retryable is True


async def test_transcribe_model_not_pulled_is_non_retryable(
    monkeypatch: pytest.MonkeyPatch, wav: Path
) -> None:
    """Model absent from /models -> MODEL_NOT_PULLED, non-retryable, no upload."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return _models_ok(model="some-other-model")
        raise AssertionError("POST must not happen when the model is missing")

    captured = _install_transport(monkeypatch, handler)
    with pytest.raises(PipelineError) as exc_info:
        await _make_engine().transcribe(wav)

    assert exc_info.value.code == "MODEL_NOT_PULLED"
    assert exc_info.value.retryable is False
    assert [r.method for r in captured] == ["GET"]  # preflight only, WAV never sent


async def test_transcribe_4xx_on_post_is_bad_request(
    monkeypatch: pytest.MonkeyPatch, wav: Path
) -> None:
    """Preflight OK, transcription POST 4xx -> STT_BAD_REQUEST, non-retryable."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return _models_ok()
        return httpx.Response(400, text="bad request")

    _install_transport(monkeypatch, handler)
    with pytest.raises(PipelineError) as exc_info:
        await _make_engine().transcribe(wav)

    assert exc_info.value.code == "STT_BAD_REQUEST"
    assert exc_info.value.retryable is False


async def test_transcribe_malformed_response_is_bad_request(
    monkeypatch: pytest.MonkeyPatch, wav: Path
) -> None:
    """200 without a 'text' field -> STT_BAD_REQUEST, non-retryable."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return _models_ok()
        return httpx.Response(200, json={"unexpected": "shape"})

    _install_transport(monkeypatch, handler)
    with pytest.raises(PipelineError) as exc_info:
        await _make_engine().transcribe(wav)

    assert exc_info.value.code == "STT_BAD_REQUEST"
    assert exc_info.value.retryable is False


async def test_transcribe_preflight_gateway_down_is_retryable(
    monkeypatch: pytest.MonkeyPatch, wav: Path
) -> None:
    """Preflight GET /models 5xx -> STT_GATEWAY_UNAVAILABLE, retryable."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, text="bad gateway")

    _install_transport(monkeypatch, handler)
    with pytest.raises(PipelineError) as exc_info:
        await _make_engine().transcribe(wav)

    assert exc_info.value.code == "STT_GATEWAY_UNAVAILABLE"
    assert exc_info.value.retryable is True


# ---------- is_ready (HLD-001 §8) ----------


async def test_is_ready_true_when_model_listed(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_transport(monkeypatch, lambda request: _models_ok())
    assert await _make_engine().is_ready() is True


async def test_is_ready_false_when_model_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_transport(monkeypatch, lambda request: _models_ok(model="other"))
    assert await _make_engine().is_ready() is False


async def test_is_ready_false_when_gateway_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    _install_transport(monkeypatch, handler)
    assert await _make_engine().is_ready() is False


async def test_is_ready_sends_bearer_to_models(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _install_transport(monkeypatch, lambda request: _models_ok())
    await _make_engine().is_ready()

    assert len(captured) == 1
    assert str(captured[0].url) == f"{BASE_URL}/models"
    assert captured[0].headers["authorization"] == f"Bearer {API_KEY}"


# ---------- env-var fallback (b3-config TODO) ----------


async def test_engine_reads_config_from_env(
    monkeypatch: pytest.MonkeyPatch, wav: Path
) -> None:
    monkeypatch.setenv("LTS_STT_BASE_URL", "http://env-gw:4000/v1")
    monkeypatch.setenv("LTS_STT_API_KEY", "env-key")
    monkeypatch.setenv("LTS_MODEL", "env-model")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return _models_ok(model="env-model")
        return httpx.Response(200, json={"text": "from env"})

    captured = _install_transport(monkeypatch, handler)
    engine = LiteLLMWhisperSTT()  # no explicit args -> read env
    result = await engine.transcribe(wav)

    assert result == "from env"
    assert str(captured[0].url) == "http://env-gw:4000/v1/models"
    assert captured[0].headers["authorization"] == "Bearer env-key"


# ---------- MockSTT ----------


async def test_mock_stt_is_deterministic(tmp_path: Path) -> None:
    wav = tmp_path / "job-xyz.wav"
    wav.write_bytes(b"")
    engine = MockSTT()
    assert await engine.transcribe(wav) == "mock transcript for job-xyz\n"
    assert await engine.transcribe(wav) == "mock transcript for job-xyz\n"


async def test_mock_stt_is_always_ready() -> None:
    assert await MockSTT().is_ready() is True
