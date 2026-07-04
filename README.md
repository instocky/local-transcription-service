# Local Transcription Service

Companion service for the **YT Transcript Copier** Chrome extension
(`../20260404_ytt`). Performs local speech-to-text inference on a
persistently available compute node (currently Mac Mini, Apple Silicon).

## System context

This service implements the architectural decision documented in:

- **ADR-012 - Local Transcription Pipeline** (system-level ADR,
  vendored from the extension repo into this repo):
  [`docs/adr/ADR-012-local-transcription-pipeline.md`](docs/adr/ADR-012-local-transcription-pipeline.md)

Operational design (worker count, queue tech, retry, lifecycle, etc.)
for this service is documented in [`docs/hld/`](docs/hld/).
**The HLD is the source of truth for the operator-facing contract
(env var names, response shapes, retry policy).** If something here
disagrees with HLD-001, the HLD wins and this file is wrong.

## Scope of this repo

- FastAPI HTTP service exposing a job API.
- Local job queue and persistent state (SQLite, lease-based).
- Background worker that drains the queue.
- Auth via shared `X-Auth-Token` header.

Out of scope: the Chrome extension itself, the system-level
architectural decision (see ADR-012), and any cloud-based STT
alternative.

## Quickstart

> Requires `uv` (https://docs.astral.sh/uv/) and Python 3.12.

The service binds to `192.168.0.99:8766` by default (the Mac Mini's
LAN IP per HLD-001 §14). Override `LTS_BIND_HOST` for loopback-only
or other LAN addresses.

```powershell
# Minimum required
$env:LTS_AUTH_TOKEN = "change-me-please-1234567890"

# Optional overrides (env var names per HLD-001 §4 / §14)
$env:LTS_BIND_HOST        = "127.0.0.1"        # default 192.168.0.99
$env:LTS_PORT             = "8766"             # default 8766
$env:LTS_DATA_DIR         = "$HOME\.local-transcription"
$env:LTS_STT_ENGINE       = "openai"           # or "mock" (CI / offline)
$env:LTS_STT_BASE_URL     = "http://192.168.0.99:4000/v1"  # LiteLLM gateway
$env:LTS_STT_API_KEY      = "<your-litellm-master-key>"    # required when LTS_STT_ENGINE=openai
$env:LTS_MODEL            = "whisper-large-v3-turbo"
```

```bash
uv sync
uv run local-transcription-service
```

The service starts both the HTTP server and the background worker in
the same process.

## API surface (current — HLD-001 §6)

All routes below require the `X-Auth-Token` header (set in
`LTS_AUTH_TOKEN`), except `/health` and `/ready` which are public
probes (HLD-001 §14).

| Method | Path                       | Auth | Status          | Purpose                                  |
| ------ | -------------------------- | ---- | --------------- | ---------------------------------------- |
| GET    | `/health`                  | no   | 200             | Liveness probe.                          |
| GET    | `/ready`                   | no   | 200 / 503       | Readiness probe (db writable + ffmpeg + STT model). |
| POST   | `/jobs`                    | yes  | 202             | Submit a YouTube URL for transcription.  |
| GET    | `/jobs/{job_id}`           | yes  | 200 / 404       | Poll job state. Includes `transcript` + `transcript_path` for DONE. |
| GET    | `/jobs/{job_id}/result`    | yes  | 200 / 404 / 410 / 500 | Stream the finished transcript file. |

Submit a job:

```bash
curl -X POST http://192.168.0.99:8766/jobs \
  -H "X-Auth-Token: $LTS_AUTH_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"video_url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"}'
# 202 Accepted
# {"job_id":"...","status":"queued","poll_url":"/jobs/..."}
```

Poll until `status == "done"`:

```bash
curl http://192.168.0.99:8766/jobs/$JOB_ID -H "X-Auth-Token: $LTS_AUTH_TOKEN"
```

## Configuration

| Env var                | Default                  | HLD ref     | Notes |
| ---------------------- | ------------------------ | ----------- | ----- |
| `LTS_AUTH_TOKEN`       | *(required, ≥16 chars)*  | §14         | Shared secret, sent in `X-Auth-Token` header. |
| `LTS_BIND_HOST`        | `192.168.0.99`           | §14         | LAN IP of the Mac Mini. |
| `LTS_PORT`             | `8766`                   | §14         | TCP port. |
| `LTS_DATA_DIR`         | `~/.local-transcription` | §7, §13     | Holds `jobs.db`, `audio-cache/`, `results/`, `trash/`. |
| `LTS_STT_ENGINE`       | `openai`                 | §4 amended  | `openai` (LiteLLM/whisper.cpp, default) or `mock` (CI / offline). |
| `LTS_STT_BASE_URL`     | `http://192.168.0.99:4000/v1` | §4    | OpenAI-compatible endpoint exposed by LiteLLM. Stage 3 POSTs `${LTS_STT_BASE_URL}/audio/transcriptions`. |
| `LTS_STT_API_KEY`      | *(required when `LTS_STT_ENGINE=openai`)* | §4 | Bearer token sent on every STT call. Empty when `LTS_STT_ENGINE=mock`. |
| `LTS_MODEL`            | `whisper-large-v3-turbo` | §4          | Model name passed to the STT engine. |
| `LTS_LEASE_TTL_SECONDS`| `600`                    | §8          | Worker lease before reclaim. |
| `LTS_RECLAIM_INTERVAL_SECONDS` | `30`             | §8          | How often the reclaim loop runs. |
| `LTS_MAX_ATTEMPTS`     | `2`                      | §10         | Max processing attempts per job. |
| `LTS_RETRY_BACKOFF_SECONDS` | `30`                 | §10         | Delay between retry attempts for retryable failures. |

## Status

**Phase A complete** (HLD-001 implementation, Windows + mock pipeline):

- ✅ `/health`, `/ready` (HLD §8) — with engine dispatch (`openai` / `mock`).
- ✅ `POST /jobs` (202 + `poll_url`) and `GET /jobs/{id}` (with `transcript` + `transcript_path`).
- ✅ `GET /jobs/{id}/result` (text/plain stream).
- ✅ `X-Auth-Token` auth (timing-safe compare, `WWW-Authenticate: Token` on 401).
- ✅ SQLite queue with lease-based single-flight claim, stale-worker protection.
- ✅ Retry policy (HLD §10): `defer_retry` + `next_retry_at`, 30s backoff, `max_attempts=2`.
- ✅ Background worker (`claim` + `reclaim` loops in the same event loop as uvicorn).
- ✅ 118 tests passing (Phase A baseline + B1/B2/B3 work in progress), `ruff check` clean.

**STT engine decided (2026-07-03, HLD §4 amended):** whisper.cpp (Metal) on the
Mac Mini, fronted by the existing LiteLLM Proxy (`:4000`) via OpenAI
`/v1/audio/transcriptions`. The ollama path was rejected (no whisper STT
endpoint). whisper-server is provisioned and live on `127.0.0.1:8779`
(launchd, Apple M4, `large-v3-turbo`). See
`docs/runbooks/whisper-macmini-provisioning.md` and `scripts/whisper-macmini/`.

**Pending (Phase B):**

- ⏳ Real pipeline: Stage 1 yt-dlp → Stage 2 ffmpeg (16 kHz mono WAV) → Stage 3
  `LiteLLMWhisperSTT` (OpenAI multipart to LiteLLM). See `docs/tasks/TASK-B-real-pipeline.md`.
- ✅ Config migration: `LTS_OLLAMA_BASE_URL` → `LTS_STT_BASE_URL` + `LTS_STT_API_KEY`,
  `LTS_STT_ENGINE=openai` (B5a, this commit).
- ⏳ `medium` vs `large-v3-turbo` benchmark (`scripts/whisper-macmini/bench-whisper.sh`).
- ⏳ Result trash policy (HLD O-4): move to `trash/` after extension ack (separate task).

## Requirements

- Python 3.12
- `ffmpeg` on `$PATH` (Stage 2 audio conditioning, plus `/ready` probe)
- For STT: reachable LiteLLM gateway (`http://192.168.0.99:4000`) with the
  whisper.cpp `audio_transcription` deployment registered; `LTS_STT_API_KEY`
  set to the LiteLLM master key. CI uses `stt_engine=mock` (no gateway needed).
