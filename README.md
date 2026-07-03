# Local Transcription Service

Companion service for the **YT Transcript Copier** Chrome extension
(`../20260404_ytt`). Performs local speech-to-text inference on a
persistently available compute node (currently Mac Mini, Apple Silicon).

## System context

This service implements the architectural decision documented in:

- **ADR-012 — Local Transcription Pipeline** (system-level ADR,
  vendored from the extension repo into this repo):
  [`docs/adr/ADR-012-local-transcription-pipeline.md`](docs/adr/ADR-012-local-transcription-pipeline.md)

Operational design (worker count, queue tech, retry, lifecycle, etc.)
for this service is documented in [`docs/hld/`](docs/hld/).

## Scope of this repo

- FastAPI HTTP service exposing a job API.
- Local job queue and persistent state.
- Three-stage processing pipeline: media acquisition, audio
  conditioning, speech-to-text inference.
- Local result storage.

Out of scope: the Chrome extension itself, the system-level
architectural decision (see ADR-012), and any cloud-based STT
alternative.

## Quickstart

> Requires `uv` (https://docs.astral.sh/uv/).

```bash
uv sync
uv run local-transcription-service
```

By default the service binds to `127.0.0.1:8766`. Both are
configurable via environment variables (see `config.py`).

Smoke check:

```bash
curl http://127.0.0.1:8766/health
```

## API surface (target, MVP)

| Method | Path             | Purpose                                      |
| ------ | ---------------- | -------------------------------------------- |
| GET    | `/health`        | Liveness probe.                              |
| POST   | `/jobs`          | Submit a transcription job (YouTube URL).    |
| GET    | `/jobs/{job_id}` | Poll job state and retrieve the transcript.  |

Full contract and request/response schemas land with the implementation.

## Status

Skeletal. Only `/health` is implemented. The HLD draft
(`docs/hld/HLD-001-local-transcription-service.md`) is awaiting review
before pipeline implementation begins.

## Requirements

- Python 3.12
- `ffmpeg` available on `$PATH`
- For STT inference: a Whisper-family model (selection pending HLD review)