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

### Engine: whisper.cpp (Metal) behind LiteLLM  **[DECISION — amended 2026-07-03]**

> **Amendment note.** The original decision was *ollama-hosted whisper*. A B0
> provisioning spike (2026-07-03) falsified it empirically: ollama 0.31.1 on the
> Mac Mini returns **404** for `POST /api/audio/transcriptions`, no whisper model
> is runnable in ollama (`ollama list` has none), and whisper STT is an open
> upstream feature request (ollama/ollama#8202, #11798) — not a shipped API.
> Engine changed to whisper.cpp. This stays within ADR-012 (STT remains local;
> cloud STT still rejected) so **no ADR change is required** — the engine is an
> HLD/operational concern.

Options considered:

| Engine                            | Verdict                                                                                                   |
|-----------------------------------|----------------------------------------------------------------------------------------------------------|
| `ollama` (whisper-large-v3-turbo) | **Rejected.** No audio-transcription endpoint; whisper not runnable in ollama; upstream feature-request only. |
| `whisper.cpp` (Metal) via `whisper-server` | **Chosen.** Apple-Silicon first-class (Metal/Core ML), lowest latency, no LLM runtime in the STT path, native OpenAI `/v1/audio/transcriptions`. |
| `mlx-whisper` (direct)            | Deferred alt. MLX-native; kept as a drop-in `STTEngine` if whisper.cpp latency/quality proves insufficient. |

**Choice: whisper.cpp `whisper-server`, fronted by the existing LiteLLM Proxy.**
whisper-server runs loopback-only (`127.0.0.1:8779`) with `--inference-path
/v1/audio/transcriptions`, and is registered in LiteLLM (`:4000`) as an
`audio_transcription` deployment. The service therefore talks to **one** gateway
with **one** token for LLM + STT, and the STT engine is swappable behind LiteLLM
without touching Stage 3 code. Provisioning runbook + scripts:
`docs/runbooks/whisper-macmini-provisioning.md`, `scripts/whisper-macmini/`.

Deployment verified 2026-07-03 on Apple M4 (4 P + 6 E cores, 16 GB): Metal
backend active, `large-v3-turbo` (1623.92 MB) loaded, launchd-managed,
`json` + `verbose_json` smoke green.

### Model: `whisper-large-v3-turbo`  **[DECISION, MVP default]**

whisper.cpp ggml model (`ggml-large-v3-turbo.bin`, ~1.6 GB). Quality close to
`large-v3`, ~6× faster; runs on Metal within unified memory. `medium` is also
downloaded; a `medium` vs `large-v3-turbo` benchmark (RTF, cold/warm) is an
open optimization (`scripts/whisper-macmini/bench-whisper.sh`) — switching model
is a config/wrapper change, not a code change.

### Engine interface  **[DECISION]**

```python
class STTEngine(Protocol):
    async def transcribe(self, wav_path: Path, *, language: str | None = None) -> str: ...
    async def is_ready(self) -> bool: ...
```

Two implementations:

- `LiteLLMWhisperSTT` (default) — POSTs the WAV as OpenAI multipart to
  `${LTS_STT_BASE_URL}/audio/transcriptions` (LiteLLM `:4000/v1`) with
  `model=${LTS_MODEL}` and the `${LTS_STT_API_KEY}` bearer. `is_ready` calls
  `GET ${LTS_STT_BASE_URL}/models` and checks the model is listed.
- `MockSTT` — deterministic, no I/O; used when `LTS_STT_ENGINE=mock`
  (CI / dev environments with no gateway).

Engine is selected by the `LTS_STT_ENGINE` env var (`openai` for the
LiteLLM/whisper.cpp path, or `mock`). A future `mlx-whisper` engine can be added
as a third value without changing the worker or Stage 1/2.

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
  (for the LiteLLM/whisper.cpp path: model listed in `GET /v1/models`).

```json
{
  "ready": true,
  "checks": {
    "db_writable": true,
    "ffmpeg_present": true,
    "stt_engine": "openai",
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
  "status": "queued | claimed | processing | done | failed",
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

When `status == "failed"`:

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
    status        TEXT NOT NULL,        -- queued|claimed|processing|done|failed
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

A background task scans every `LTS_RECLAIM_INTERVAL_SECONDS` seconds
(default 30s) for jobs in `claimed` or `processing` status whose
`lease_expires_at` has passed. Such jobs are returned to `queued`
with `attempt` already incremented.

**Lease TTL: 600 seconds**  **[DECISION]**

`large-v3-turbo` inference on a 60-minute audio file at fp16 takes
well under 10 minutes on Apple Silicon (whisper.cpp on Metal). 600s
is a generous ceiling that handles transient stalls (e.g., GC pauses)
without false reclaims.

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

`failed` is the only terminal non-success state. Transient (retryable)
failures are deferred back to `queued` with `next_retry_at` set
(see §10); the worker reschedules them after the backoff and the
job re-enters `claimed`. A job only reaches `failed` when:

- the failure is non-retryable (e.g., invalid URL, model not
  registered), OR
- the retryable failure has exhausted `LTS_MAX_ATTEMPTS`.

The `error` payload attached to a `failed` job is a structured
`JobError { code, message, retryable }` (see `models.JobError`),
not a job status. The extension renders both `failed` jobs and
"max-attempts-deferred-then-failed" jobs the same way; the
`error.code` lets the user tell the difference.

## 10. Retry policy

**Max attempts: 2** (initial + 1 retry).  **[DECISION]**

This is a single-user interactive tool on a small compute node, not
a batch server. Two attempts are enough to absorb the most common
transient failures (network blip during media fetch, brief ffmpeg
hiccup, transient STT-gateway unavailability) without papering over
genuine problems.

### Tunables (env-var contract)

| Env var                       | Default | Meaning                                                                 |
| ----------------------------- | ------- | ----------------------------------------------------------------------- |
| `LTS_MAX_ATTEMPTS`            | `2`     | Max processing attempts per job (initial + retries).                    |
| `LTS_RETRY_BACKOFF_SECONDS`   | `30`    | Delay between retry attempts; applied as `next_retry_at = now + N s`.   |
| `LTS_LEASE_TTL_SECONDS`       | `600`   | Worker lease before reclaim (see §8).                                    |
| `LTS_RECLAIM_INTERVAL_SECONDS`| `30`    | Background scan cadence for expired leases (see §8).                    |

### Retry semantics

Retryable errors (per ADR-012 contract) — deferred back to `queued`
with `next_retry_at` until `LTS_MAX_ATTEMPTS` is reached:

- Media fetch transient failure (network).
- ffmpeg transient failure.
- STT engine transient unavailability (e.g., whisper-server / LiteLLM restart).
- STT inference OOM-recoverable (e.g., after releasing other memory).

Non-retryable errors — marked `failed` immediately on first attempt:

- Invalid URL.
- Video unavailable / private / region-locked.
- No audio track.
- Missing STT model (not registered in LiteLLM).
- Malformed request.

Backoff between attempt 1 and attempt 2: `LTS_RETRY_BACKOFF_SECONDS`
seconds (default **30 seconds**).

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

**Tool: configured `STTEngine` (default: `LiteLLMWhisperSTT` → whisper.cpp).**  **[DECISION]**

Sends the WAV from Stage 2 to the STT engine and receives a
plain-text transcript. Writes the transcript to
`${LTS_DATA_DIR}/results/{job_id}.md`.

For whisper.cpp: the model stays loaded in `whisper-server`'s memory
between jobs (cold start is once per service lifetime, not per job).
Memory budget on Apple Silicon: `whisper-large-v3-turbo` loads at
~1.6 GB (verified 1623.92 MB on the M4), leaving headroom on any
current Mac Mini (≥8 GB unified memory).

## 12. Failure modes and recovery

| Failure                          | Detection                        | Recovery                                                              |
|----------------------------------|----------------------------------|-----------------------------------------------------------------------|
| Service crash mid-job            | Lease expires                    | Background reclaim returns job to `queued` (attempt unchanged).       |
| `ffmpeg` missing                 | Subprocess `FileNotFoundError`   | Marked `failed` with `JobError(code="AUDIO_CONDITIONING_FAILED", retryable=False)`. |
| `ffmpeg` non-zero exit           | Subprocess exits non-zero        | Marked `failed` with `JobError(code="AUDIO_CONDITIONING_FAILED", retryable=False)`. |
| `yt-dlp` missing                 | Subprocess `FileNotFoundError`   | Marked `failed` with `JobError(code="FETCH_FAILED", retryable=False)`. |
| `yt-dlp` non-zero exit (config)  | Subprocess exits non-zero, no network marker | Marked `failed` with `JobError(code="FETCH_FAILED", retryable=False)` (e.g. video unavailable / private). |
| Network drop during fetch        | `yt-dlp` exits with transient network marker on stderr | Deferred to `queued` with `next_retry_at = now + LTS_RETRY_BACKOFF_SECONDS`; `JobError(code="FETCH_FAILED", retryable=True)`. |
| `yt-dlp` SSL/cert error          | `yt-dlp` exits with `ssl: certificate verify failed` on stderr | Marked `failed` with `JobError(code="FETCH_FAILED", retryable=False)` — operator must fix cert/CA bundle; retries do not help. |
| STT gateway down                 | `POST /v1/audio/transcriptions` (or preflight `GET /v1/models`) connection refused, timeout, or 5xx | Deferred to `queued` with `JobError(code="STT_GATEWAY_UNAVAILABLE", retryable=True)`; retried after `LTS_RETRY_BACKOFF_SECONDS`. |
| STT model not registered         | `GET /v1/models` does not list model | Marked `failed` with `JobError(code="MODEL_NOT_PULLED", retryable=False)`; operator registers the whisper deployment in LiteLLM and resubmits. |
| STT request rejected (4xx)       | `POST /v1/audio/transcriptions` returns a 4xx other than model-not-registered, or a malformed/non-JSON body | Marked `failed` with `JobError(code="STT_BAD_REQUEST", retryable=False)`; malformed request — fix and resubmit. |
| whisper.cpp model file missing   | `whisper-server` fails to start / 5xx | launchd surfaces via `whisper-server.err`; jobs deferred as `STT_GATEWAY_UNAVAILABLE` until the service is healthy. |
| Disk full                        | Write raises `OSError`           | Marked `failed` with `JobError(code="PIPELINE_TRANSIENT", retryable=True)`; user frees space and resubmits (worker fallback path). |
| OOM during inference             | Process killed / Python raises   | Deferred to `queued` (transient); reclaim returns job to `queued` after the lease TTL. |
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
 "stt_engine": "openai", "stt_model": "whisper-large-v3-turbo",
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
| O-1 | STT model for MVP                                                                                                              | `whisper-large-v3-turbo` via whisper.cpp (Metal), fronted by LiteLLM. Configurable via `LTS_MODEL`.                                                          |
| O-2 | LAN access needed? What auth?                                                                                                  | **LAN access needed.** Bind to Mac Mini LAN IP (`192.168.0.99:8766`). Auth = shared secret via `X-Auth-Token` header + `LTS_AUTH_TOKEN` env.              |
| O-3 | Audio sources beyond YouTube URLs                                                                                              | YouTube URLs only for MVP. Direct file URL / upload is a future HLD.                                                                                         |
| O-4 | Result retention policy                                                                                                        | Move to `${LTS_DATA_DIR}/trash/{job_id}.md` after the extension confirms download via `GET /jobs/{id}/ack`. Manual cleanup of `trash/` thereafter.        |
| O-5 | Healthcheck depth: add `/ready`?                                                                                                | **Yes.** Verifies DB writable, `ffmpeg` present, STT engine ready (whisper model listed in LiteLLM `GET /v1/models`).                                       |
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