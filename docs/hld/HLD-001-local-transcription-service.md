# HLD-001 — Local Transcription Service: Operational Design

| Field      | Value                                          |
|------------|------------------------------------------------|
| Status     | Draft                                          |
| Date       | 2026-07-03                                     |
| Author     | Mavis                                          |
| Deciders   | TBD                                            |
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

**Primary:** Mac Mini, Apple Silicon (M-series), macOS.

The service binds to `127.0.0.1` by default and is intended to run
under `launchd` on the same machine as the user's Chrome browser
with the YT Transcript Copier extension installed.

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

### Engine: mlx-whisper  **[DECISION]**

Two options on the table:

| Engine          | Pros                                                          | Cons                                                  |
|-----------------|---------------------------------------------------------------|-------------------------------------------------------|
| `mlx-whisper`   | Native Metal GPU acceleration on Apple Silicon; fastest.     | Locks primary target to Apple Silicon.                |
| `faster-whisper`| Portable across macOS / Linux / Windows; CPU + CUDA paths.   | Slower than MLX on Apple Silicon.                     |

**Choice: mlx-whisper.** ADR-012 already commits to a Mac Mini as the
current compute node. Apple Silicon is the primary target; portability
is a property of the architectural contract, not the implementation.
Cross-platform portability is opt-in via a future engine swap (the
service exposes engine selection behind a single interface).

### Model size: large-v3-turbo  **[DECISION, MVP default]**

Default model for MVP: `mlx-community/whisper-large-v3-turbo`.

- Quality close to `large-v3`.
- ~6× faster than `large-v3` on Apple Silicon.
- Fits in unified memory of any current Mac Mini.

Model selection is configurable via `LTS_MODEL` env var. Switching
model does **not** require re-architecting anything — it is a config
choice, not a code change.

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

### `GET /health`

Liveness probe.

```json
{ "status": "ok", "version": "0.1.0" }
```

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
well under 10 minutes on Apple Silicon. 600s is a generous ceiling
that handles transient stalls (e.g., GC pauses) without false
reclaims.

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
hiccup) without papering over genuine problems.

Retryable errors (per ADR-012 contract):

- Media fetch transient failure (network).
- ffmpeg transient failure.
- STT inference OOM-recoverable (e.g., after releasing other memory).

Non-retryable errors:

- Invalid URL.
- Video unavailable / private / region-locked.
- No audio track.
- Missing model weights.
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

**Tool: `mlx-whisper` (Python import).**  **[DECISION]**

Loads the configured model on first use and keeps it resident in
memory for the lifetime of the process. Writes the transcript to
`${LTS_DATA_DIR}/results/{job_id}.md`.

Model memory budget: large-v3-turbo fits in <2 GB on Apple Silicon,
leaving headroom on any current Mac Mini (≥8 GB unified memory).

## 12. Failure modes and recovery

| Failure                          | Detection                        | Recovery                                                              |
|----------------------------------|----------------------------------|-----------------------------------------------------------------------|
| Service crash mid-job            | Lease expires                    | Background reclaim returns job to `queued`.                          |
| ffmpeg missing                   | Subprocess exits non-zero        | Non-retryable `error`, surfaced via API.                              |
| yt-dlp missing                   | Subprocess exits non-zero        | Non-retryable `error`, surfaced via API.                              |
| Model file missing               | Import / load failure            | Non-retryable `error`, surfaced via API.                              |
| Network drop during fetch        | yt-dlp exits with network error  | Retryable; backoff 30s.                                               |
| Disk full                        | Write raises `OSError`           | Non-retryable `error`; user frees space and resubmits.                |
| OOM during inference             | Process killed / Python raises   | Retryable; reclaim returns job to `queued`.                            |

## 13. Result storage

**Filesystem under `${LTS_DATA_DIR}/results/{job_id}.md`.**  **[DECISION]**

The API returns both the inline `transcript` text and a
`transcript_path`. The inline copy keeps the extension simple (no
extra read); the path lets other tools (or future features) reach
the canonical file directly.

Retention policy: **see Open Decision O-4 below.**

## 14. Network binding and auth

**Default bind: `127.0.0.1:8766`, no auth.**  **[DECISION]**

Threat model: same machine as the user's Chrome browser. The
extension talks to the service over loopback HTTP. Loopback-only
binding eliminates the LAN attack surface.

For LAN access, an explicit auth scheme (token / mTLS) is required
before binding to a non-loopback address. This is **out of scope
for MVP**; see Open Decision O-2.

## 15. Logging

**Structured JSON to stdout.**  **[DECISION]**

One log line per event (job submitted, job claimed, stage started,
stage finished, job done, job failed). Fields:

```json
{"ts": "...", "level": "INFO", "job_id": "...", "stage": "fetch",
 "event": "stage_finished", "duration_s": 4.2}
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
- Reads `LTS_*` env vars from `~/.local-transcription/env`.

For Linux/Jetson (future target), the equivalent would be a systemd
unit — same operational pattern, different syntax.

## 17. Open decisions (require user input before implementation)

These are decisions where reasonable defaults are documented above
but a confirmation is needed before code is written against them.

| ID  | Question                                                                                                                                       |
|-----|------------------------------------------------------------------------------------------------------------------------------------------------|
| O-1 | Confirm STT model: `large-v3-turbo` for MVP? Switch to `large-v3` if quality is insufficient, or `small`/`medium` if latency matters more?    |
| O-2 | LAN access needed for MVP? If yes, what auth scheme (token / mTLS / SSH-tunnel-only)?                                                         |
| O-3 | Audio sources beyond YouTube URLs: direct file upload? Arbitrary URL? Or strictly YouTube?                                                    |
| O-4 | Result retention policy: delete after N days, keep forever until manual cleanup, or move to a `trash/` directory after download?              |
| O-5 | Healthcheck depth: current `/health` only confirms liveness. Add a `ready` endpoint that verifies model loaded + DB writable + ffmpeg present? |
| O-6 | Concurrent job submission from the same extension (e.g., two tabs both submitting)? Rate-limit per job_id, per IP, or just trust loopback?     |

## 18. Out of scope for this HLD

The following are explicitly excluded. If pursued, each requires its
own HLD or ADR:

- Translation of transcripts.
- Speaker diarization.
- Multi-worker scaling (architecturally supported, not enabled).
- LAN / remote network access.
- Persistent transcript indexing / search.
- Cloud STT fallback.
- Web UI / dashboard.