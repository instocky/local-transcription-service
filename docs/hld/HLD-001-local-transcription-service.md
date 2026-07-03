# HLD-001 — Local Transcription Service: Operational Design

| Field      | Value                                          |
|------------|------------------------------------------------|
| Status     | Accepted                                       |
| Date       | 2026-07-03                                     |
| Author     | Mavis                                          |
| Deciders   | Mavis, Senior Tech Lead                        |
| Supersedes | —                                              |
| Related    | ADR-012 (system-level, extension repo)         |

## 1. Scope

This HLD governs the operational design of the local transcription
service that implements ADR-012. Architecture (extension ↔ service
boundary, three-stage pipeline shape, hardware-agnostic compute node)
remains in ADR-012 and is not restated here.

The boundary test for this document:

> *What changes if I scale from 1 worker to N workers, swap SQLite
> for Redis, or move from Mac Mini to Jetson?*
>
> If "nothing" — the decision belongs in ADR-012 (or a service-local
> ADR). If "everything" — it belongs here.

## 2. Deployment target

**Primary:** Mac Mini, Apple Silicon (M-series), macOS, LAN IP
`192.168.0.99`. The service runs under `launchd` on the Mac Mini
and is reached over LAN by the user's Windows dev box (which runs
the Chrome browser with the YT Transcript Copier extension).

Hardware-agnosticism is preserved at the architectural level
(ADR-012) — a Jetson or Linux mini-PC could host this service with
the same API contract, but the STT engine choice below locks the
primary target to Apple Silicon.

## 3. Runtime stack

| Concern        | Choice                                    | Rationale                                                                |
|----------------|-------------------------------------------|--------------------------------------------------------------------------|
| Language       | Python 3.12                               | Native MLX support; faster CPython; modern typing.                       |
| Package mgr    | `uv`                                      | Fast, lockfile-driven, replaces pip + venv + pip-tools.                 |
| HTTP framework | FastAPI + uvicorn                         | Async-native; auto-OpenAPI; type-hint driven validation.                |
| Settings       | pydantic-settings                         | Env-based config with validation; consistent with stack.                 |
| Tests          | pytest + pytest-asyncio + httpx           | Async-native; httpx for API integration tests.                          |
| Linter         | ruff                                      | Single tool for lint + format; fast.                                    |

## 4. STT engine and model

### Engine: ollama-hosted whisper  **[DECISION]**

Two options on the table:

| Engine                          | Pros                                                                              | Cons                                                                              |
|---------------------------------|-----------------------------------------------------------------------------------|-----------------------------------------------------------------------------------|
| `ollama` (whisper-large-v3-turbo) | User already runs ollama; one `ollama pull` to install; model stays loaded between jobs; clean `/ready` probe via `GET /api/tags`. | Extra HTTP hop; tied to ollama daemon uptime; ~2-3 s slower than MLX on Apple Silicon. |
| `mlx-whisper` (direct)          | Native Metal GPU via MLX; fastest inference on Apple Silicon; full control over inference parameters. | Second model lifecycle alongside ollama; cold start per worker boot; user must manage downloads. |

**Choice: ollama-hosted whisper.** The user already runs ollama on
the Mac Mini. Operational simplicity wins over the 2-3 s inference
delta for a 10-minute video (the inference itself takes 1-3 minutes).
Both engines share the same `STTEngine` interface, so mlx-whisper
remains a drop-in alternative if ollama quality or latency proves
insufficient.

Setup on the Mac Mini (one-time):

```bash
ollama pull whisper-large-v3-turbo
```

### Model: `whisper-large-v3-turbo`  **[DECISION, MVP default]**

Selected via ollama's model name. Quality close to `large-v3`, ~6×
faster. Fits in unified memory of any current Mac Mini.

Model selection is configurable via `LTS_MODEL` env var. Switching
model does **not** require re-architecting anything — it is a config
choice, not a code change.

### Engine interface  **[DECISION]**

```python
class STTEngine(Protocol):
    async def transcribe(self, wav_path: Path, *, language: str | None = None) -> str: ...
    async def is_ready(self) -> bool: ...
```

Two implementations:

- `OllamaWhisperSTT` (default) — POSTs WAV bytes to
  `${LTS_OLLAMA_BASE_URL}/api/audio/transcriptions` with
  `model=${LTS_MODEL}`. `is_ready` calls `GET /api/tags` and checks
  the model is in the list.
- `MLXWhisperSTT` (alt, optional dep) — Python import of
  `mlx_whisper.transcribe()`. Loaded only when `LTS_STT_ENGINE=mlx-whisper`.

Engine is selected by the `LTS_STT_ENGINE` env var (`ollama`,
`mlx-whisper`, or `mock` for dev/test environments that do not
have a real STT daemon running).

## 5. Service topology

**Single FastAPI process, single async worker.**  **[DECISION]**

The pipeline runs as an asyncio background task inside the same
process as the HTTP server. ADR-012 explicitly excludes "Multiple
concurrent processing workers" from scope, so adding workers is a
future HLD concern, not an architecture change.

The service is structured so that adding workers later is a
configuration change, not a redesign:

- The job store (Section 7) supports atomic claim with a lease.
- The HTTP layer is stateless except for the job store.
- A second process can be launched against the same SQLite file
  without code changes — both will compete for jobs via the lease
  protocol.

## 6. API contract (MVP target)

All endpoints under `/`. JSON in, JSON out. Identifiers are opaque
strings (UUIDv4 by default).

All endpoints (except `GET /health` and `GET /ready`) require the
`X-Auth-Token: ${LTS_AUTH_TOKEN}` header. Missing or mismatched
token → `401 Unauthorized`. No body, no retry hint.

### `GET /health`

Liveness probe. **No auth required.**

```json
{ "status": "ok", "version": "0.1.0" }
```

### `GET /ready`

Readiness probe. **No auth required** (so dev monitoring can poll
without managing tokens).

Returns `200` only when all of:

- Database file writable (`jobs.db`).
- `ffmpeg` binary found on `$PATH`.
- Configured STT engine reports `is_ready() == True`
  (for ollama: model in `/api/tags` response).

```json
{
  "ready": true,
  "checks": {
    "db_writable": true,
    "ffmpeg_present": true,
    "stt_engine": "ollama",
    "stt_model_loaded": true
  }
}
```

When any check fails, returns `503 Service Unavailable` with the
same payload structure and the failing check set to `false`.

### `POST /jobs`

Submit a transcription job.

Request:

```json
{ "video_url": "https://www.youtube.com/watch?v=..." }
```

Response (202 Accepted):

```json
{
  "job_id": "9f3c1b7e-...-...",
  "status": "queued",
  "poll_url": "/jobs/9f3c1b7e-...-..."
}
```

### `GET /jobs/{job_id}`

Poll job state.

Response (200 OK):

```json
{
  "job_id": "9f3c1b7e-...-...",
  "status": "queued | claimed | processing | done | error | failed",
  "attempt": 0,
  "created_at": "2026-07-03T14:30:00Z",
  "started_at": null,
  "finished_at": null,
  "error": null,
  "transcript": null,
  "transcript_path": null
}
```

When `status == "done"`:

```json
{
  ...
  "status": "done",
  "finished_at": "2026-07-03T14:34:12Z",
  "transcript": "Full transcript text...",
  "transcript_path": "/Users/me/.local-transcription/results/9f3c1b7e.md"
}
```

When `status == "error"` or `"failed"`:

```json
{
  ...
  "status": "failed",
  "finished_at": "2026-07-03T14:31:05Z",
  "error": {
    "code": "FETCH_FAILED",
    "message": "yt-dlp could not retrieve media",
    "retryable": false
  }
}
```

The extension polls this endpoint. Poll cadence is in
Section 9 (operational concern).

## 7. Job store

**SQLite via `aiosqlite`.**  **[DECISION]**

Rationale:

- ADR-012 commits to "low operational complexity" and "no distributed
  state". SQLite is the simplest single-node persistent store that
  still survives restarts.
- No new daemon, no auth, no memory pressure.
- Atomic claim with lease is implementable in a few SQL statements
  (see Section 8).

Database path: `${LTS_DATA_DIR}/jobs.db`, default
`~/.local-transcription/jobs.db`.

### Schema (sketch)

```sql
CREATE TABLE jobs (
    job_id        TEXT PRIMARY KEY,
    video_url     TEXT NOT NULL,
    status        TEXT NOT NULL,        -- queued|claimed|processing|done|error|failed
    attempt       INTEGER NOT NULL DEFAULT 0,
    lease_token   TEXT,                 -- NULL when not claimed
    lease_expires_at TEXT,              -- ISO 8601 UTC
    created_at    TEXT NOT NULL,
    started_at    TEXT,
    finished_at   TEXT,
    transcript_path TEXT,
    error_code    TEXT,
    error_message TEXT,
    error_retryable INTEGER
);

CREATE INDEX idx_jobs_status_lease ON jobs(status, lease_expires_at);
```

## 8. Lease and reclaim

**Lease-based single-flight claim.**  **[DECISION]**

The worker claims a job atomically by:

```sql
UPDATE jobs
SET status = 'claimed',
    lease_token = :token,
    lease_expires_at = :expires_at,
    attempt = attempt + 1
WHERE job_id = :job_id
  AND status = 'queued';
```

A background task scans every `LTS_RECLAIM_INTERVAL` seconds (default
30s) for jobs in `claimed` or `processing` status whose
`lease_expires_at` has passed. Such jobs are returned to `queued`
with `attempt` already incremented.

**Lease TTL: 600 seconds**  **[DECISION]**

`large-v3-turbo` inference on a 60-minute audio file at fp16 takes
well under 10 minutes on Apple Silicon (or ~10 min via ollama
network overhead). 600s is a generous ceiling that handles transient
stalls (e.g., GC pauses) without false reclaims.

## 9. State machine

```
        ┌─────────┐
        │ queued  │
        └────┬────┘
             │ claim (atomic)
             ▼
        ┌─────────┐  lease expired ┌─────────┐
        │ claimed │ ───────────────►│ queued  │  (attempt++)
        └────┬────┘                 └─────────┘
             │ pipeline start
             ▼
        ┌────────────┐  lease expired ┌─────────┐
        │ processing │ ──────────────►│ queued  │  (attempt++)
        └────┬───────┘                └─────────┘
             │
   ┌─────────┼─────────┐
   ▼                   ▼
┌──────┐            ┌──────┐
│ done │            │failed│ (attempt exhausted)
└──────┘            └──────┘
```

`error` is reserved for transient failures during processing where a
retry is sensible (e.g., transient network blip during media
download). `failed` is the terminal state after retry exhaustion.
The extension treats both as terminal non-success but logs them
differently.

## 10. Retry policy

**Max attempts: 2** (initial + 1 retry).  **[DECISION]**

This is a single-user interactive tool on a small compute node, not
a batch server. Two attempts are enough to absorb the most common
transient failures (network blip during media fetch, brief ffmpeg
hiccup, transient ollama unavailability) without papering over
genuine problems.

Retryable errors (per ADR-012 contract):

- Media fetch transient failure (network).
- ffmpeg transient failure.
- STT engine transient unavailability (e.g., ollama daemon restart).
- STT inference OOM-recoverable (e.g., after releasing other memory).

Non-retryable errors:

- Invalid URL.
- Video unavailable / private / region-locked.
- No audio track.
- Missing STT model (not pulled into ollama).
- Missing model weights (mlx-whisper path).
- Malformed request.

Backoff between attempt 1 and attempt 2: **30 seconds.**

## 11. Pipeline stages (operational)

ADR-012 commits to three sequential stages. This section pins down
the concrete tool choices per stage.

### Stage 1 — Media acquisition

**Tool: `yt-dlp` (CLI invocation via subprocess).**  **[DECISION]**

Outputs a local audio file in the original container, kept under
`${LTS_DATA_DIR}/audio-cache/{job_id}.{ext}`.

### Stage 2 — Audio conditioning

**Tool: `ffmpeg` (CLI invocation via subprocess).**  **[DECISION]**

Normalises to 16 kHz mono PCM WAV — the format Whisper expects.
Cleans up the raw media file from Stage 1 after the WAV is produced.

### Stage 3 — Speech-to-text inference

**Tool: configured `STTEngine` (default: `OllamaWhisperSTT`).**  **[DECISION]**

Sends the WAV from Stage 2 to the STT engine and receives a
plain-text transcript. Writes the transcript to
`${LTS_DATA_DIR}/results/{job_id}.md`.

For ollama: the model stays loaded in ollama's process memory
between jobs (cold start is once per `ollama serve` lifetime, not
per job). Memory budget on Apple Silicon: `whisper-large-v3-turbo`
fits in <2 GB unified memory, leaving headroom on any current Mac
Mini (≥8 GB unified memory).

## 12. Failure modes and recovery

| Failure                          | Detection                        | Recovery                                                              |
|----------------------------------|----------------------------------|-----------------------------------------------------------------------|
| Service crash mid-job            | Lease expires                    | Background reclaim returns job to `queued`.                          |
| `ffmpeg` missing                 | Subprocess exits non-zero        | Non-retryable `error`, surfaced via API.                              |
| `yt-dlp` missing                 | Subprocess exits non-zero        | Non-retryable `error`, surfaced via API.                              |
| Ollama daemon down               | `POST /api/audio/transcriptions` connection refused or 5xx | Retryable; reclaim returns job to `queued` after 30s backoff. |
| Ollama model not pulled          | `/api/tags` does not list model  | Non-retryable `error` with `code=MODEL_NOT_PULLED`; user runs `ollama pull` and resubmits. |
| mlx-whisper model file missing   | Import / load failure            | Non-retryable `error`, surfaced via API.                              |
| Network drop during fetch        | yt-dlp exits with network error  | Retryable; backoff 30s.                                               |
| Disk full                        | Write raises `OSError`           | Non-retryable `error`; user frees space and resubmits.                |
| OOM during inference             | Process killed / Python raises   | Retryable; reclaim returns job to `queued`.                            |
| Wrong / missing X-Auth-Token     | API request missing header       | `401 Unauthorized`. No job state change.                              |

## 13. Result storage

**Filesystem under `${LTS_DATA_DIR}/results/{job_id}.md`.**  **[DECISION]**

The API returns both the inline `transcript` text and a
`transcript_path`. The inline copy keeps the extension simple (no
extra read); the path lets other tools (or future features) reach
the canonical file directly.

Retention policy: **see Open Decision O-4 below.**

## 14. Network binding and auth

**Default bind: `192.168.0.99:8766` (the Mac Mini's LAN IP).**  **[DECISION]**

The service binds to the Mac Mini's LAN address, not `0.0.0.0` and
not `127.0.0.1`. The Windows dev box reaches the service at
`http://192.168.0.99:8766`.

Bind address is overridable via `LTS_BIND_HOST` env var (e.g., for
future loopback-only testing: `LTS_BIND_HOST=127.0.0.1`). Port is
`LTS_PORT`, default `8766`.

**Auth: shared secret token via `X-Auth-Token` header.**  **[DECISION]**

- Token configured via `LTS_AUTH_TOKEN` env var on both the Mac Mini
  (in the launchd plist) and the Windows dev box (in the extension
  settings or `background.js` config).
- All endpoints except `GET /health` and `GET /ready` require the
  header. Mismatch → `401 Unauthorized`.
- Token rotation: change the env var on both sides and restart the
  service. No version field; one active token at a time.
- For MVP, no expiry. If the token is suspected leaked, rotate.

Threat model: home LAN, single user. The token prevents accidental
discovery and access from other devices on the same network; it is
not a substitute for transport encryption (which is out of scope
for MVP — see Section 18).

## 15. Logging

**Structured JSON to stdout.**  **[DECISION]**

One log line per event (job submitted, job claimed, stage started,
stage finished, job done, job failed, STT engine choice at startup).
Fields:

```json
{"ts": "...", "level": "INFO", "job_id": "...", "stage": "fetch",
 "event": "stage_finished", "duration_s": 4.2}
```

At service startup, log the resolved configuration (token value is
**never** logged):

```json
{"ts": "...", "level": "INFO", "event": "config_resolved",
 "stt_engine": "ollama", "stt_model": "whisper-large-v3-turbo",
 "bind_host": "192.168.0.99", "bind_port": 8766,
 "data_dir": "/Users/me/.local-transcription",
 "lease_ttl_s": 600, "max_attempts": 2}
```

`launchd` captures stdout/stderr to a file under
`~/Library/Logs/local-transcription-service.log`.

No distributed tracing, no metrics endpoint for MVP — keeps with
ADR-012's "low operational complexity" driver. If a future HLD adds
observability, OpenTelemetry is the obvious extension point.

## 16. Service lifecycle (macOS)

A `launchd` plist template lives at `scripts/launchd/com.local-transcription-service.plist`.
Installation:

```bash
cp scripts/launchd/com.local-transcription-service.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.local-transcription-service.plist
launchctl start com.local-transcription-service
```

The plist:

- Runs on user login (`RunAtLoad`).
- Restarts on crash (`KeepAlive`).
- Sets working directory to the repo root.
- Inlines all `LTS_*` env vars directly under `EnvironmentVariables`.
  **launchd does NOT source external env files** — the plist must
  carry each variable explicitly. Operators who prefer the
  `~/.local-transcription/env` pattern can substitute a wrapper
  script that sources the file and execs `local-transcription-service`;
  the default plist does not.

For Linux/Jetson (future target), the equivalent would be a systemd
unit — same operational pattern, different syntax.

## 17. Resolved decisions

The following decisions were resolved during planning. Recorded
here so future readers can trace the rationale.

| ID  | Question                                                                                                                       | Resolution                                                                                                                                                  |
|-----|--------------------------------------------------------------------------------------------------------------------------------|-------------------------------------------------------------------------------------------------------------------------------------------------------------|
| O-1 | STT model for MVP                                                                                                              | `whisper-large-v3-turbo` via ollama. Configurable via `LTS_MODEL`.                                                                                          |
| O-2 | LAN access needed? What auth?                                                                                                  | **LAN access needed.** Bind to Mac Mini LAN IP (`192.168.0.99:8766`). Auth = shared secret via `X-Auth-Token` header + `LTS_AUTH_TOKEN` env.              |
| O-3 | Audio sources beyond YouTube URLs                                                                                              | YouTube URLs only for MVP. Direct file URL / upload is a future HLD.                                                                                         |
| O-4 | Result retention policy                                                                                                        | Move to `${LTS_DATA_DIR}/trash/{job_id}.md` after the extension confirms download via `GET /jobs/{id}/ack`. Manual cleanup of `trash/` thereafter.        |
| O-5 | Healthcheck depth: add `/ready`?                                                                                                | **Yes.** Verifies DB writable, `ffmpeg` present, STT engine ready (ollama model in `/api/tags`, or mlx-whisper loaded).                                    |
| O-6 | Rate-limit on submission                                                                                                       | Trust the `X-Auth-Token` — token holder is the legitimate single user. No IP-based limit for MVP.                                                            |

## 18. Out of scope for this HLD

The following are explicitly excluded. If pursued, each requires its
own HLD or ADR:

- Translation of transcripts.
- Speaker diarization.
- Multi-worker scaling (architecturally supported, not enabled).
- TLS / transport encryption (LAN only; out of scope for MVP).
- Persistent transcript indexing / search.
- Cloud STT fallback.
- Web UI / dashboard.