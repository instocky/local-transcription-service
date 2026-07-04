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

**One FastAPI process; N concurrent claim loops inside it.**  **[DECISION — amended 2026-07-04, Phase D]**

The pipeline runs as asyncio background tasks inside the same
process as the HTTP server. The number of claim loops is set
by `LTS_WORKER_COUNT` (default `1`, range `1..64`). The reclaim
loop stays single — it is already idempotent and cheap, and
running it N times would be pure overhead.

> **Amendment note (Phase D).** The Phase A text said "single
> async worker" and treated additional workers as a future HLD
> concern. The lease protocol was designed for it from day one
> (Phase A commit `53b2b89`), but no configuration knob exposed
> it. Phase D adds `LTS_WORKER_COUNT` and runs N claim tasks
> cooperatively in the same event loop. Multi-process deployment
> (multiple `local-transcription-service` processes competing
> for the same SQLite) is still a future deployment-shape
> change, not a code change — `Worker` is already
> process-agnostic.

### 5.1 Why in-process, not multi-process

- **No bind-port coordination.** One HTTP port, one HTTP server,
  one FastAPI app. Multi-process would force port shifting or a
  worker-only process shape (no HTTP listener), and would need
  some out-of-band way to say "stop claiming".
- **No cross-process log interleaving.** Each claim task gets a
  stable `worker_id` (`f"w{i}"`) in structured log events; one
  process means one stdout stream ordered by event time.
- **SQLite's write-lock is the throughput ceiling.** One process
  with 4 tasks has the same ceiling as 4 processes. Going
  in-process saves the IPC overhead and gives us the
  throughput-oracle for free.
- **If in-process workers prove insufficient**, multi-process is
  a deployment-shape change — the code is ready.

### 5.2 Race-condition audit

All races the codebase has are already safe under `LTS_WORKER_COUNT > 1`:

| Race                                                                | Safe? | Why                                                                                                              |
|---------------------------------------------------------------------|-------|-----------------------------------------------------------------------------------------------------------------|
| Two claim tasks race for the same QUEUED job                        | YES   | `store.claim()` is one atomic UPDATE; the WHERE clause filters by `status='queued'` so only one task matches.   |
| Two reclaim tasks race for the same expired lease                   | YES   | `reclaim_expired()` is one atomic UPDATE; `lease_expires_at < ?` filters the set per call.                     |
| Two claim tasks race `store.mark_processing` for the same job      | YES   | `WHERE status='claimed' AND lease_token=:token` — only the task that holds the lease token matches.            |
| Two claim tasks race `store.mark_done` for the same job            | YES   | Same lease-token filter.                                                                                         |
| Two `mark_acked` calls from two extension clients on the same DONE | YES   | `WHERE status='done' AND acked_at IS NULL` — one UPDATE wins; the loser returns `rowcount=0` + `already_acked=true`. |
| Two `update_transcript_path` calls racing for the same job         | YES   | The column is overwritten unconditionally and is idempotent for the same value.                                  |
| Two processes start simultaneously, both call `store.init()`       | YES   | `init()` opens a connection, runs `CREATE TABLE IF NOT EXISTS`, then `PRAGMA table_info(jobs)` + idempotent `ALTER TABLE ADD COLUMN`. Each `ALTER` is a no-op once the column exists. The connection-per-op pattern means no shared in-memory state to race. |
| Two processes both run `store.ping_writable()` from `/ready`       | YES (sequential) | `BEGIN IMMEDIATE` acquires the SQLite write lock; the second caller waits, then succeeds. SQLite's busy_timeout (5 s, see §5.3) caps the wait. |

### 5.3 SQLite busy_timeout

`PRAGMA busy_timeout = 5000` is set on every connection the
store opens. This is a defensive tuning, not a correctness fix
— SQLite's default behaviour on a busy lock is to fail fast,
which is fine for the claim path (the claim loop retries on the
next tick) but wrong for the readiness probe (we want to wait,
not fail). 5 s is generous for a LAN tool and well below any
interactive-recovery budget.

### 5.4 What is unchanged from Phase A

- The job store (Section 7) supports atomic claim with a lease.
- The HTTP layer is stateless except for the job store.
- A second process *can* still be launched against the same
  SQLite file without code changes — both will compete for jobs
  via the lease protocol. The HLD does not enable that mode by
  default, but the code is ready for it if a future deployment
  needs it.

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
  "transcript_path": null,
  "acked_at": null
}
```

`acked_at` (added Phase C, 2026-07-04) carries the ISO 8601 UTC
timestamp of the first successful `POST /jobs/{job_id}/ack`, or
`null` if the job has never been acked. Same value as the
`AckResponse.acked_at` returned by the ack endpoint, preserved
idempotently across retries. Lets the extension confirm "the
download was acknowledged" from a poll cycle alone — no separate
ack probe required.

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

### 13.1 Lifecycle & ack (O-4 operational details, Phase C, 2026-07-04)

After the extension confirms a successful download, it MUST call
`POST /jobs/{job_id}/ack`. The service then:

1. Sets `jobs.acked_at` (UTC, ISO 8601) — idempotency marker.
2. Atomically moves the transcript file from its current path
   (typically `${LTS_DATA_DIR}/results/{job_id}.md`) into
   `${LTS_DATA_DIR}/trash/`, preserving the basename (same
   filesystem = atomic on POSIX; on Windows `os.replace` is atomic
   on the same volume). The destination is named after the source
   file so an operator who relocated the file earlier keeps that
   basename.
3. Updates `transcript_path` in the DB to reflect the new location
   so subsequent reads (e.g. an extension retry after a network
   glitch) follow the file.

The endpoint is **idempotent at the contract layer**:

- `acked_at` is set on the first successful call and preserved on
  retries — never overwritten with "now" again.
- The FS move is **re-attempted on every call** when the file is
  not already in the trash (i.e. a prior move failed or the
  operator fixed a transient issue between calls). This is what
  makes a retry-after-transient-FS-issue converge instead of
  leaving the file stranded.
- `transcript_moved` reflects the *current* filesystem state
  (file in trash after this call), independent of whether *this*
  call did the rename — so the extension can reconcile on retry
  without a separate status probe.
- `GET /jobs/{id}` and `GET /jobs/{id}/result` continue to work
  after ack — `result` reads from the trash path the DB now points
  at. (Operators handle dump-the-trash manually.)

Status codes:

| Code | When                                                                                  |
|------|---------------------------------------------------------------------------------------|
| 503  | Database write failed. Two sub-cases — surfaced uniformly as `503 DB_UNAVAILABLE` (the endpoint catches `aiosqlite.Error` / `sqlite3.Error` around the entire DB-touching block). For both, retrying the ack converges: (a) `mark_acked` fails before any FS work — trivial retry; (b) `update_transcript_path` fails after a successful `move_to_trash` — the file is already in `trash/`, the retry auto-discovers the canonical trash path and heals the DB (Phase C P1, 2026-07-04). |

**Why POST (not GET):** the original §17 O-4 wording said `GET`,
but ack has a state-mutating side effect (file rename + DB
update), so it MUST be `POST` per RFC 9110 §9.2.1. Phase C
introduces this correction. Tracked in `docs/changelogs/`.

**Failure-mode contract:**

- DB write fails (any call in the DB-touching block — `mark_acked`,
  `update_transcript_path`, or even the pre-flight `get`):
  `503` with code `DB_UNAVAILABLE`. The endpoint catches
  `aiosqlite.Error` / `sqlite3.Error` uniformly around the entire
  block. The two sub-cases behave differently in the FS:
    a. `mark_acked` (or pre-flight) fails before `move_to_trash`
       ran → file is still at its source path. Retry is trivial
       once the DB is recoverable.
    b. `update_transcript_path` fails after a successful
       `move_to_trash` → **partial state**: file is in `trash/`,
       DB path is stale at the source. Retry **auto-discovers** the
       canonical trash file and heals the DB (Phase C P1,
       2026-07-04). `GET /jobs/{id}/result` recovers on the same
       retry without operator intervention.
  Response body carries an error message that names the underlying
  exception for operator triage.
- File move fails because source doesn't exist (operator cleanup
  between retries, race against `mark_done`, manual delete from
  `trash/`, the partial-state window from sub-case (b) above): `200`
  with `transcript_moved=False`; logged warning; `acked_at` is set
  if the DB write succeeded. The retry that follows
  **auto-discovers** the canonical file in `trash/<basename>` and
  uses it to heal the stale DB path — so the endpoint converges
  after a partial failure (Phase C P1, 2026-07-04).
- File move fails for any other reason (permission, cross-volume,
  full filesystem): `200` with `transcript_moved=False`; logged
  warning. Operator can drag the file to `trash/` manually; the
  next ack call will see it already there and report
  `transcript_moved=True`.

**Why this ordering:** DB is the source of truth for "acked?". The
FS move is a hygiene optimisation. Splitting them lets us keep
the contract idempotent even when the FS move is flaky — the
DB never goes back to "not acked" after a successful ack, and the
FS move eventually converges across retries.

Retention policy: **see §13.2 (Phase D, 2026-07-04) — replaces the
manual cleanup wording in O-4.**

### 13.2 Trash retention automation (Phase D, 2026-07-04)

Phase C (§13.1) made `trash/` the resting place for acked
transcripts but left its growth unbounded. Phase D adds a
deterministic retention policy shipped as a standalone CLI
(`lts-trash-cleanup`) and a separate launchd plist that fires
it daily.

#### Policy knobs

| Knob           | Env var                  | Default          | Semantics                                                                |
|----------------|--------------------------|------------------|--------------------------------------------------------------------------|
| Age cap (TTL)  | `LTS_TRASH_TTL_DAYS`     | `7`              | Files in `trash/` with `mtime < now() - TTL` are deleted (oldest first). |
| Size cap       | `LTS_TRASH_MAX_BYTES`    | `536870912` (512 MiB) | If the cumulative size of `trash/` exceeds the cap, delete the oldest files (by `mtime`) until under cap. |

A single invocation runs the two passes **in order: TTL first,
then size cap**. Each pass is independent and idempotent —
running the CLI twice in a row is a no-op the second time.

#### CLI contract

Console-script: `lts-trash-cleanup`. Also invokable as
`python -m local_transcription_service.retention` (the form
launchd uses).

Flags:

- `--dry-run` — log the deletion plan, exit 0, no `unlink()`.
- `--data-dir PATH` — override `${LTS_DATA_DIR}` for one-off
  runs.

Exit codes:

| Code | Meaning                                                                                                              |
|------|----------------------------------------------------------------------------------------------------------------------|
| 0    | Success (zero or more files deleted; cleanup converged).                                                            |
| 1    | Configuration error (env var parse failed, `trash_dir` missing or not a directory).                                  |
| 2    | Runtime I/O error (permission denied, fs went read-only mid-run). Best-effort cleanup; the next launchd tick handles the rest. |

#### launchd wiring

New plist at
`scripts/launchd/com.local-transcription-service.trash-cleanup.plist`:

- Label: `com.local-transcription-service.trash-cleanup`.
- `StartCalendarInterval`: `Hour=4, Minute=0` — once a day at
  04:00 local. Low-traffic window; deleted files are already in
  `trash/` (post-ack), so there is no live-pipeline interaction.
- `RunAtLoad`: `false` — no point running at boot; we want the
  daily tick.
- `StandardOutPath` / `StandardErrorPath`:
  `~/Library/Logs/local-transcription-service.trash-cleanup.log`.
- `EnvironmentVariables`: copies only the retention knobs
  (`LTS_DATA_DIR`, `LTS_TRASH_TTL_DAYS`, `LTS_TRASH_MAX_BYTES`).
  No `LTS_AUTH_TOKEN` — the CLI doesn't need it.

#### Why a CLI, not an in-process background task

1. Once we run N claim loops (Phase D §5), an in-process
   cleanup loop would run N times per interval — each loop
   walking `trash/` independently. A single CLI scheduled by
   launchd runs once across the whole system.
2. The CLI is testable end-to-end with a tmpdir + a few fake
   transcript files; an in-process task would need lifecycle
   plumbing (start/stop, joined with shutdown).
3. The launchd-driven approach gives the operator a free
   override: temporarily running `lts-trash-cleanup --dry-run`
   to inspect the deletion plan is a single command, no service
   restart.

#### Filesystem invariants

- `trash/` is allowed to be empty after a cleanup pass.
- `trash/` is **not** deleted by the CLI. Operators who want to
  wipe everything do it manually with `rm -rf` (and accept that
  the next transcript that lands there re-creates the directory).
- Symlinks in `trash/` are never followed. The CLI uses
  `Path.unlink(missing_ok=True)`; an operator who drops a link
  there gets the same crash-resistant behaviour as for a real
  file.

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
 "lease_ttl_s": 600, "max_attempts": 2,
 "worker_count": 4}
```

`worker_count` (added Phase D) appears in `config_resolved` so
operators can confirm the multi-worker deployment shape from a
single log line.

`launchd` captures stdout/stderr to a file under
`~/Library/Logs/local-transcription-service.log`. **Rotation** is
handled by macOS `newsyslog` (see §16.4); the service does not
rotate its own log.

### 15.1 Error-rate counter (Phase D, 2026-07-04)

A small in-process counter emits an `error_rate_tick` event every
60 seconds with per-code counts since the last tick:

```json
{"ts": "...", "level": "INFO", "event": "error_rate_tick",
 "interval_s": 60,
 "counts": {"FETCH_FAILED": 3, "STT_GATEWAY_UNAVAILABLE": 1, "MAX_ATTEMPTS": 0}}
```

Why not Prometheus / OpenMetrics: HLD-001 says "no metrics
endpoint for MVP". A log-emitted counter keeps that promise and is
enough for a single-user tool — the operator's existing
log-tailing workflow gets a richer feed without a new endpoint to
monitor. If a future phase adds Prometheus, this counter is the
right place to extend (the metric names line up with the log
event names).

### 15.2 What we are NOT doing

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

### 16.1 Healthcheck-on-start (Phase D, 2026-07-04)

Before starting uvicorn, `app.main()` calls
`asyncio.wait_for(engine.is_ready(), timeout=5.0)`. If the
engine is not ready (returns `False` or raises), the service:

1. Logs a `startup_stt_not_ready` event with the underlying error
   in the JSON log feed.
2. Calls `sys.exit(78)` (sysexits.h `EX_CONFIG`).

`launchd` does not auto-restart on `EX_CONFIG` (`KeepAlive.Crashed`
only triggers on signal-style exits). The operator sees the log
line and the service stays down until they intervene — exactly
what we want, because silently starting a half-broken service is
worse than no service.

The 5-second budget matches the short-timeout path inside
`LiteLLMWhisperSTT.is_ready()` (Phase B drift fix, commit
`22d7f04`).

### 16.2 Log rotation (Phase D, 2026-07-04)

macOS `newsyslog` rotates the launchd-captured stdout/stderr
files. The config lives at
`scripts/launchd/local-transcription-service.conf`:

```text
# logfilename                                              mode  count  size  when  flags
/Users/__USER__/Library/Logs/local-transcription-service.log              644  5     10M   $D0   JN
/Users/__USER__/Library/Logs/local-transcription-service.trash-cleanup.log  644  5     10M   $D0   JN
```

- `count=5` — keep 5 rotated files (so 50 MB total ceiling).
- `size=10M` — rotate when the current file crosses 10 MB.
- `when=$D0` — rotate at midnight on any day it crosses the size threshold.
- `flags=JN` — bzip2 the rotated files (`J`), create with the right
  mode if missing (`N`).

Install step (operator runs once):

```bash
sudo cp scripts/launchd/local-transcription-service.conf /etc/newsyslog.d/
```

Documented in the existing runbook
(`docs/runbooks/whisper-macmini-provisioning.md`).

For Linux/Jetson (future target), the equivalent is a
`/etc/logrotate.d/local-transcription-service` snippet with the
same shape; not implemented now.

### 16.3 Trash cleanup plist (Phase D, 2026-07-04)

A second plist at
`scripts/launchd/com.local-transcription-service.trash-cleanup.plist`
fires the `lts-trash-cleanup` CLI daily at 04:00 local. See §13.2
for the CLI contract and §16 above for the general plist shape;
this one differs by:

- `StartCalendarInterval` (`Hour=4, Minute=0`) instead of
  `RunAtLoad`.
- `RunAtLoad=false`.
- `EnvironmentVariables` carries only retention knobs — no
  `LTS_AUTH_TOKEN`, no `LTS_BIND_HOST`, no `LTS_STT_*`.

Install alongside the main plist:

```bash
cp scripts/launchd/com.local-transcription-service.trash-cleanup.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.local-transcription-service.trash-cleanup.plist
```

## 17. Resolved decisions

The following decisions were resolved during planning. Recorded
here so future readers can trace the rationale.

| ID  | Question                                                                                                                       | Resolution                                                                                                                                                  |
|-----|--------------------------------------------------------------------------------------------------------------------------------|-------------------------------------------------------------------------------------------------------------------------------------------------------------|
| O-1 | STT model for MVP                                                                                                              | `whisper-large-v3-turbo` via whisper.cpp (Metal), fronted by LiteLLM. Configurable via `LTS_MODEL`.                                                          |
| O-2 | LAN access needed? What auth?                                                                                                  | **LAN access needed.** Bind to Mac Mini LAN IP (`192.168.0.99:8766`). Auth = shared secret via `X-Auth-Token` header + `LTS_AUTH_TOKEN` env.              |
| O-3 | Audio sources beyond YouTube URLs                                                                                              | YouTube URLs only for MVP. Direct file URL / upload is a future HLD.                                                                                         |
| O-4 | Result retention policy                                                                                                        | Move the transcript file into `${LTS_DATA_DIR}/trash/` (preserving the source basename — typically `{job_id}.md` in MVP since that is what Stage 3 writes) after the extension confirms download via `POST /jobs/{id}/ack`. Manual cleanup of `trash/` thereafter.            |
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