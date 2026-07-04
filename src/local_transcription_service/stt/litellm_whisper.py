"""`LiteLLMWhisperSTT` — Stage 3 against whisper.cpp behind LiteLLM.

HLD-001 §4 (amended 2026-07-03): the STT engine is whisper.cpp
(`whisper-server`, Metal) registered in the LiteLLM Proxy as an
`audio_transcription` deployment. This client speaks the OpenAI
`/v1/audio/transcriptions` multipart contract to the gateway.

Error mapping (HLD-001 §12) — every failure becomes a `PipelineError`
with the retry semantics the worker expects:

    connection refused / timeout / 5xx  -> STT_GATEWAY_UNAVAILABLE (retryable)
    model absent from GET /models       -> MODEL_NOT_PULLED        (non-retryable)
    other 4xx / malformed response      -> STT_BAD_REQUEST         (non-retryable)

`transcribe` preflights `GET /models` before uploading the WAV: a
cheap check avoids shipping megabytes of audio to a gateway that
cannot serve the model, and lets us distinguish a missing model
(non-retryable) from a transient outage (retryable).
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

import httpx

from local_transcription_service.pipeline.base import PipelineError

logger = logging.getLogger(__name__)

# Env-var names kept here only for the dev/test fallback path
# inside LiteLLMWhisperSTT.__init__; production reads them via
# Settings (config.py). Mirrors config.Settings field names so the
# two stay aligned.
_ENV_BASE_URL = "LTS_STT_BASE_URL"
_ENV_API_KEY = "LTS_STT_API_KEY"
_ENV_MODEL = "LTS_MODEL"

_DEFAULT_BASE_URL = "http://192.168.0.99:4000/v1"
_DEFAULT_MODEL = "whisper-large-v3-turbo"
# STT of a long audio file can legitimately take minutes on the gateway.
_DEFAULT_TIMEOUT_S = 300.0
# GET /models is a small JSON response and is also called by the
# readiness probe (`is_ready`). A blackhole / slow-LAN gateway must
# not make the probe hang for the full transcription timeout — that
# would turn /ready from a fast 503 into a multi-minute timeout and
# trip any LB / monitor that polls the probe. 5 s is generous for a
# LAN gateway and short enough that the probe stays useful.
_DEFAULT_READINESS_TIMEOUT_S = 5.0


class LiteLLMWhisperSTT:
    """OpenAI-multipart STT client for the LiteLLM/whisper.cpp gateway.

    Constructor accepts explicit ``base_url`` / ``api_key`` / ``model``
    arguments; production wiring goes through
    :func:`local_transcription_service.app.build_stt_engine`, which
    pulls them from :class:`local_transcription_service.config.Settings`.

    The ``None``-fallback to ``os.environ.get(...)`` is kept only for
    dev/test invocations that construct the client without a Settings
    object (e.g., one-off scripts in ``scripts/whisper-macmini/``).
    It is NOT a production path: ``Settings._check_openai_requires_api_key``
    already validates the env vars at startup.
    """

    def __init__(
        self,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
        timeout_s: float = _DEFAULT_TIMEOUT_S,
        readiness_timeout_s: float = _DEFAULT_READINESS_TIMEOUT_S,
    ) -> None:
        self._base_url = (base_url or os.environ.get(_ENV_BASE_URL, _DEFAULT_BASE_URL)).rstrip("/")
        self._api_key = api_key if api_key is not None else os.environ.get(_ENV_API_KEY, "")
        self._model = model or os.environ.get(_ENV_MODEL, _DEFAULT_MODEL)
        self._timeout_s = timeout_s
        self._readiness_timeout_s = readiness_timeout_s

    @property
    def model(self) -> str:
        return self._model

    @property
    def _auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._api_key}"}

    # ---------- public API (STTEngine protocol) ----------

    async def transcribe(self, wav_path: Path, *, language: str | None = None) -> str:
        # Preflight: don't upload the WAV if the model isn't registered.
        models = await self._list_models()
        if self._model not in models:
            raise PipelineError(
                f"model {self._model!r} not registered in LiteLLM gateway",
                code="MODEL_NOT_PULLED",
                retryable=False,
            )

        audio = await asyncio.to_thread(wav_path.read_bytes)
        data = {"model": self._model, "response_format": "json"}
        if language is not None:
            data["language"] = language
        files = {"file": (wav_path.name, audio, "audio/wav")}
        url = f"{self._base_url}/audio/transcriptions"

        try:
            async with httpx.AsyncClient(timeout=self._timeout_s) as client:
                response = await client.post(
                    url, headers=self._auth_headers, data=data, files=files
                )
        except httpx.RequestError as exc:
            raise self._gateway_unavailable("POST /audio/transcriptions", exc) from exc

        self._raise_for_status(response, "POST /audio/transcriptions")

        try:
            payload = response.json()
        except ValueError as exc:
            raise PipelineError(
                "STT gateway returned a non-JSON transcription response",
                code="STT_BAD_REQUEST",
                retryable=False,
            ) from exc

        text = payload.get("text")
        if not isinstance(text, str):
            raise PipelineError(
                "STT gateway response is missing the 'text' field",
                code="STT_BAD_REQUEST",
                retryable=False,
            )
        return text

    async def is_ready(self) -> bool:
        """`True` iff the configured model is listed by `GET /models`.

        Never raises: a gateway outage or a rejected request reports
        not-ready, per the `STTEngine` contract (HLD-001 §8).

        Uses ``readiness_timeout_s`` (default 5 s) instead of the
        long ``timeout_s`` reserved for actual transcription, so a
        blackholed / slow gateway does not turn the /ready probe
        into a multi-minute hang for any LB / monitor that polls it.
        """
        try:
            models = await self._list_models(timeout_s=self._readiness_timeout_s)
        except Exception:  # noqa: BLE001 — STTEngine contract: must not raise
            logger.warning("is_ready: probe failed, reporting not ready", exc_info=True)
            return False
        return self._model in models

    # ---------- internals ----------

    async def _list_models(self, *, timeout_s: float | None = None) -> list[str]:
        """Return the model ids from `GET {base}/models` (OpenAI shape).

        ``timeout_s`` defaults to ``self._timeout_s`` (the long
        transcription timeout). Callers that need a tighter deadline
        — currently ``is_ready`` — pass an explicit override; the
        /ready probe must not block on a slow gateway for the full
        transcription window.
        """
        if timeout_s is None:
            timeout_s = self._timeout_s
        url = f"{self._base_url}/models"
        try:
            async with httpx.AsyncClient(timeout=timeout_s) as client:
                response = await client.get(url, headers=self._auth_headers)
        except httpx.RequestError as exc:
            raise self._gateway_unavailable("GET /models", exc) from exc

        self._raise_for_status(response, "GET /models")

        try:
            payload = response.json()
        except ValueError as exc:
            raise PipelineError(
                "STT gateway returned a non-JSON /models response",
                code="STT_BAD_REQUEST",
                retryable=False,
            ) from exc

        # The OpenAI /models contract is:
        #   {"object": "list", "data": [{"id": "<model-id>", ...}, ...]}
        # The previous code did `[m.get("id", "") for m in
        # payload.get("data", [])]`, which silently misbehaves on
        # any deviation:
        #   - payload not a dict (e.g. JSON array) -> AttributeError
        #     on .get()
        #   - payload.data not a list (e.g. string, dict, None) ->
        #     iteration yields chars/keys, list of "" or dict keys
        #   - entry not a dict (e.g. {"data": ["foo"]}) ->
        #     AttributeError on m.get()
        # Any of these would let an AttributeError/TypeError escape
        # _list_models: transcribe() falls into the
        # PIPELINE_TRANSIENT (retryable) branch instead of the
        # intended STT_BAD_REQUEST (non-retryable), and is_ready()
        # returns False for the wrong reason. Strict validation
        # surfaces gateway contract violations as STT_BAD_REQUEST.
        if not isinstance(payload, dict):
            raise PipelineError(
                "STT gateway /models response is not a JSON object",
                code="STT_BAD_REQUEST",
                retryable=False,
            )
        data = payload.get("data")
        if not isinstance(data, list):
            raise PipelineError(
                "STT gateway /models response.data is not a list",
                code="STT_BAD_REQUEST",
                retryable=False,
            )
        models: list[str] = []
        for entry in data:
            if not isinstance(entry, dict):
                raise PipelineError(
                    "STT gateway /models response contains a non-object entry",
                    code="STT_BAD_REQUEST",
                    retryable=False,
                )
            mid = entry.get("id")
            if not isinstance(mid, str):
                raise PipelineError(
                    "STT gateway /models response entry is missing a string 'id'",
                    code="STT_BAD_REQUEST",
                    retryable=False,
                )
            models.append(mid)
        return models

    @staticmethod
    def _gateway_unavailable(op: str, exc: Exception) -> PipelineError:
        """Map a transport error (refused / timeout) to a retryable failure."""
        logger.warning("STT gateway unreachable on %s: %s", op, exc)
        return PipelineError(
            f"STT gateway unreachable on {op}: {exc}",
            code="STT_GATEWAY_UNAVAILABLE",
            retryable=True,
        )

    @staticmethod
    def _raise_for_status(response: httpx.Response, op: str) -> None:
        """Map HTTP status codes to the HLD-001 §12 STT failure codes."""
        if response.status_code >= 500:
            raise PipelineError(
                f"STT gateway error on {op}: HTTP {response.status_code}",
                code="STT_GATEWAY_UNAVAILABLE",
                retryable=True,
            )
        if response.status_code >= 400:
            raise PipelineError(
                f"STT gateway rejected {op}: HTTP {response.status_code}",
                code="STT_BAD_REQUEST",
                retryable=False,
            )
