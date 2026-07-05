# API Contract — Local Transcription Service (Phase F, 2026-07-05)

| Field        | Value                                                                            |
| ------------ | -------------------------------------------------------------------------------- |
| Version      | tied to the service `__version__` (currently `0.1.0`)                            |
| Base URL     | `http://192.168.0.99:8766` — Mac Mini LAN address, HLD-001 §14                   |
| Transport    | HTTP/1.1, JSON request/response (`text/plain` for transcript stream)             |
| OpenAPI spec | `/openapi.json` (FastAPI-generated; re-derived via `scripts/extract-openapi.py`) |
| Swagger UI   | `/docs` (FastAPI default)                                                        |
| Audience     | YT Transcript Copier Chrome extension; operators / Postman / curl                |
| Wire format  | snake_case keys, UTC timestamps as ISO 8601 with `Z` suffix                      |

This contract is the **single source of truth** for external clients of
the service. Internal storage types are deliberately kept out — those
live in `src/local_transcription_service/models.py` and may differ from
the wire shape; the boundary mapping is enforced by `api/schemas.py`.

> The service **does not implement CORS**. The Chrome extension reaches
> the service through its background service worker, which is not
> subject to CORS restrictions; browser tabs _cannot_ call this API
> from `http://<lan>:8766` directly.

---

## 1. Authentication

Every endpoint except `/health` and `/ready` requires:

```http
X-Auth-Token: <shared secret>
```

- The shared secret is configured server-side via the `LTS_AUTH_TOKEN`
  env var (see `macmini-deployment.md` §4 for where it lives on disk).
- Comparison is **timing-safe** (`secrets.compare_digest` in
  `src/local_transcription_service/auth.py`).
- Missing or wrong header → `401 Unauthorized`, body
  `{"code": "UNAUTHORIZED", "message": "..."}`, response includes a
  `WWW-Authenticate: Token` challenge.

## 2. Endpoints

### 2.1 `GET /health` — liveness probe

Public. Always returns `200` while the process is serving HTTP.

| Status | Body                                   |
| ------ | -------------------------------------- |
| 200    | `{"status": "ok", "version": "0.1.0"}` |

### 2.2 `GET /ready` — readiness probe

Public. `200` if and only if **all** of the following are true at probe
time:

- `db_writable` — `jobs.db` is reachable and accepts a write transaction
  (`BEGIN IMMEDIATE` per `JobStore.ping_writable`).
- `ffmpeg_present` — `ffmpeg -version` exits `0` within 2 s (PATH-check).
- `stt_model_loaded` — `engine.is_ready()` is `True`.

| Status | Body                                                                                                                                       |
| ------ | ------------------------------------------------------------------------------------------------------------------------------------------ |
| 200    | `{"ready": true, "checks": {"db_writable": true, "ffmpeg_present": true, "stt_engine": "openai", "stt_model_loaded": true}}`               |
| 503    | Same body shape with `ready: false` and the failing check(s) flipped. Concrete failing field tells the operator which subsystem is broken. |

### 2.3 `POST /jobs` — submit a YouTube URL for transcription

Auth required. Body is JSON, response is `202 Accepted` per HLD §6.

**Request body:**

```json
{
  "video_url": "https://www.youtube.com/watch?v=..."
}
```

Validation:

- `video_url` is a Pydantic `HttpUrl` (syntactically valid HTTP URL).
- The host is validated against a fixed allow-list at deserialize time:
  `youtube.com`, `www.youtube.com`, `m.youtube.com`, `youtu.be`.
  Any other host → `422 Unprocessable Entity` (handled by FastAPI's
  pydantic validation, not by our handler).
- `extra` keys are **forbidden** (`extra = "forbid"`); unknown fields
  → `422`.

| Status | Body                                                                                                                         |
| ------ | ---------------------------------------------------------------------------------------------------------------------------- |
| 202    | `{"job_id": "...", "status": "queued", "poll_url": "/jobs/{job_id}"}`                                                        |
| 401    | `{"code": "UNAUTHORIZED", ...}`                                                                                              |
| 422    | FastAPI-shape error: `{ "detail": [{ "loc": [...], "msg": "...", "type": "value_error" }, ...] }` for an invalid `video_url` |

Notes:

- `poll_url` is a **server-relative path**, not a full URL — clients
  know the base via deployment config.
- The `Location` response header is not currently emitted (HLD-001 §6
  reserves it for a future change); rely on the body's `poll_url`.

### 2.4 `GET /jobs/{job_id}` — poll job state

Auth required.

| Status | Body                                                |
| ------ | --------------------------------------------------- |
| 200    | `JobStateResponse` (see §3)                         |
| 401    | auth failure                                        |
| 404    | `{"code": "NOT_FOUND", "message": "Job not found"}` |

Polling cadence: HLD-001 §8 reserves `~5 s` for LAN clients; nothing
in the protocol blocks faster or slower polling.

### 2.5 `GET /jobs/{job_id}/result` — stream the transcript file

Auth required. Streams the persisted transcript file as `text/plain`,
**not** as JSON.

| Status | Body / behaviour                                                                                                                                                                      |
| ------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 200    | Body: full transcript text, `Content-Type: text/plain; charset=utf-8`. Treated as a UTF-8 stream — the client is expected to be safe against partial reads (`Content-Length` is set). |
| 401    | auth failure                                                                                                                                                                          |
| 404    | `{"code": "NOT_FOUND", "message": "Job not found"}`                                                                                                                                   |
| 404    | `{"code": "NOT_READY", "message": "Job not done (status=<...>)"}` if job is in `queued`/`claimed`/`processing`                                                                        |
| 410    | `{"code": "JOB_FAILED", "message": "Job failed"}` — terminal-failed jobs never produce a file                                                                                         |
| 500    | `{"code": "TRANSCRIPT_MISSING", "message": "..."}` if DB says done but the file is gone from disk                                                                                     |

The streaming path is intentionally separate from `GET /jobs/{id}` —
that endpoint inlines the transcript into the JSON for a single-shot
client without dedicated download logic, while `/result` returns the
actual file with proper download semantics (Content-Length, no JSON
escaping in the body).

### 2.6 `POST /jobs/{job_id}/ack` — acknowledge a successful download

Auth required. Idempotent (HLD-001 §13.1).

Behaviour:

- First call on a `done` job → sets `acked_at = now()` and moves the
  transcript file from its current path into `{LTS_DATA_DIR}/trash/`.
- Repeated calls on an already-acked job → preserved `acked_at`
  (no bump), FS move re-attempted only if necessary. Returns 200.
- Job in `queued`/`claimed`/`processing`/`failed` → `409 Conflict`.

| Status | Body                                                                                                     |
| ------ | -------------------------------------------------------------------------------------------------------- |
| 200    | `AckResponse` (see §3.5)                                                                                 |
| 401    | auth failure                                                                                             |
| 404    | `{"code": "NOT_FOUND", "message": "Job not found"}`                                                      |
| 409    | `{"code": "NOT_DONE", "message": "Job not in DONE state (status=...); only finished jobs can be acked"}` |
| 503    | `{"code": "DB_UNAVAILABLE", "message": "Database error: ..."}` — failure-mode contract (see HLD §13.1)   |

`transcript_moved` on a 200 is the **observed filesystem state at
return time** — `true` iff the path the DB points at currently lives
inside `trash/` and the file is present. After operator cleanup of
`trash/`, future ack calls may legitimately report `transcript_moved =
false` for the same job.

---

## 3. Data types

All request and response bodies are JSON objects, all timestamps are
ISO 8601 UTC with `Z` (e.g. `"2026-07-05T08:34:58.387469Z"`).

### 3.1 `SubmitJobRequest` (request body of `POST /jobs`)

| Field       | Type             | Required | Notes                                                  |
| ----------- | ---------------- | -------- | ------------------------------------------------------ |
| `video_url` | string (HttpUrl) | yes      | YouTube URL; host must be in the allow-list (see §2.3) |

Unknown fields → `422` (`extra = "forbid"`).

### 3.2 `SubmitJobResponse` (response body of `POST /jobs`)

| Field      | Type   | Notes                                               |
| ---------- | ------ | --------------------------------------------------- |
| `job_id`   | string | opaque, server-allocated (ULID-ish `MN17...` shape) |
| `status`   | string | always `"queued"` on initial submit                 |
| `poll_url` | string | server-relative path: `/jobs/{job_id}`              |

### 3.3 `JobStateResponse` (response body of `GET /jobs/{id}`)

| Field             | Type               | Notes                                                                   |
| ----------------- | ------------------ | ----------------------------------------------------------------------- |
| `job_id`          | string             | opaque                                                                  |
| `video_url`       | string             | the URL submitted (`str(...)` of the HttpUrl, not the raw JSON object)  |
| `status`          | string             | one of `queued`, `claimed`, `processing`, `done`, `failed` (see §3.6)   |
| `attempt`         | integer            | 1-based; incremented on each worker claim (HLD §10)                     |
| `created_at`      | string (ISO 8601)  | when the job was submitted                                              |
| `started_at`      | string \| null     | when the first attempt started; null while `queued`                     |
| `finished_at`     | string \| null     | when the job reached a terminal state (`done`/`failed`); null otherwise |
| `error`           | `JobError` \| null | populated when `status == "failed"`; otherwise null                     |
| `transcript`      | string \| null     | full transcript text when `status == "done"`; otherwise null            |
| `transcript_path` | string \| null     | server-side file path when `status == "done"`; null otherwise           |
| `acked_at`        | string \| null     | ISO 8601 of the first successful ack; null if never acked (or failed)   |

### 3.4 `JobError` (nested in `JobStateResponse` and `AckResponse`)

| Field       | Type    | Notes                                                                                                                                           |
| ----------- | ------- | ----------------------------------------------------------------------------------------------------------------------------------------------- |
| `code`      | string  | stable identifier — fetch the table in `src/local_transcription_service/queue/store.py` for the canonical list. Examples today: `FETCH_FAILED`. |
| `message`   | string  | human-readable description of what went wrong                                                                                                   |
| `retryable` | boolean | operator hint — whether a fresh submit with the same URL is likely to succeed                                                                   |

The set of error codes is **not** a closed enum: future phases may add
new codes (e.g. `STT_RATE_LIMITED`). Clients should treat unknown
codes as "retryable = false" and surface the `message` to the user.

### 3.5 `AckResponse` (response body of `POST /jobs/{id}/ack`)

| Field              | Type              | Notes                                                                                   |
| ------------------ | ----------------- | --------------------------------------------------------------------------------------- |
| `job_id`           | string            | echoes                                                                                  |
| `acked_at`         | string (ISO 8601) | timestamp of the **first** successful ack                                               |
| `already_acked`    | boolean           | `true` iff `acked_at` was set on a previous call                                        |
| `transcript_moved` | boolean           | observed FS state at return time — `true` iff file is on disk under `{data_dir}/trash/` |
| `transcript_path`  | string \| null    | server-side path of the transcript after this call (may be in `trash/` or `results/`)   |

### 3.6 `JobStatus` enum

`queued → claimed → processing → done | failed` (HLD-001 §9). Only the
terminal states `done` and `failed` are reachable from `processing` via
the worker; `claimed` and `processing` can return to `queued` on lease
expiry.

```json
"queued"        // accepted, not yet claimed by a worker
"claimed"       // a worker holds the lease; pre-fetch
"processing"    // a worker is mid-pipeline (yt-dlp / ffmpeg / STT)
"done"          // terminal success — transcript on disk
"failed"        // terminal failure — see `error`
```

Client polling logic should treat `processing` and `claimed` identically
(both "in flight").

### 3.7 Error envelope (all 4xx/5xx bodies)

```json
{
  "code": "...",
  "message": "...",
  "retryable": false // only present on structured error payloads
}
```

FastAPI's own validation errors (`422` for a malformed request body)
have a **different** shape — `{ "detail": [...] }` — and are not subject
to this envelope. Client parsers should handle both shapes.

## 4. Common conventions

| Concern             | Convention                                                                                                          |
| ------------------- | ------------------------------------------------------------------------------------------------------------------- |
| Timestamps          | UTC, ISO 8601, `Z` suffix; sub-second precision (six digits)                                                        |
| HTTP status code    | RFC 9110 / FastAPI default; failure-mode contract per HLD §13.1                                                     |
| Body size limits    | Tiny JSON; no explicit limit. Transcript files streamed directly, not inlined.                                      |
| Idempotency         | `POST /jobs/{id}/ack` is idempotent. `POST /jobs` is NOT — repeat submits create new jobs with new `job_id` values. |
| Rate limiting       | None today; the service is single-tenant on a LAN.                                                                  |
| Pagination          | Not applicable.                                                                                                     |
| Filtering / sorting | Not applicable. Clients pass a single `job_id` directly.                                                            |
| Long-poll           | Not implemented — explicit polling recommended (see HLD §8, ~5 s cadence).                                          |

## 5. End-to-end example (curl + jq)

```bash
# Replace with your actual token from $HOME/.lts-env (cat ~/.lts-env | grep ^LTS_AUTH_TOKEN)
TOKEN='<paste-here>'
BASE='http://192.168.0.99:8766'

# 1) Submit. Expect 202.
curl -sS -X POST "$BASE/jobs" \
  -H "X-Auth-Token: $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"video_url":"https://www.youtube.com/watch?v=dQw4w9WgXcQ"}'

# → {"job_id":"...","status":"queued","poll_url":"/jobs/..."}

# 2) Poll until done / failed.
JOB_ID=...
for i in 1 2 3 4 5 6 7 8 9 10 11 12; do
  STATE=$(curl -sS "$BASE/jobs/$JOB_ID" -H "X-Auth-Token: $TOKEN")
  STATUS=$(printf '%s' "$STATE" | python3 -c 'import sys,json; print(json.load(sys.stdin)["status"])')
  echo "[t+${i}5s] $STATUS"
  case "$STATUS" in done|failed) break ;; esac
  sleep 5
done

# 3) Save the transcript.
curl -sS -X GET "$BASE/jobs/$JOB_ID/result" \
  -H "X-Auth-Token: $TOKEN" > transcript.md

# 4) Ack.
curl -sS -X POST "$BASE/jobs/$JOB_ID/ack" \
  -H "X-Auth-Token: $TOKEN"
# → {"job_id":"...","acked_at":"...","already_acked":false,"transcript_moved":true,...}
```

## 6. Compatibility & versioning

- The contract version tracks the service `__version__`. The
  `/health` body includes `"version"` so a client can confirm it is
  talking to the build it expects.
- We do **not** version the URL prefix today (`/jobs`, not `/v1/jobs`).
  Future breaking changes (renaming a field, splitting an endpoint,
  changing an HTTP status code) will require a version bump on
  `__version__`'s major component **and** either a URL prefix change
  or a parallel "deprecated" path. Document that policy before
  shipping the first breaking change — it is intentionally not in
  place yet because we have no public clients other than the in-repo
  Postman tests and the Chrome extension that ships with this repo.
- Anything added to a response payload in a backward-compatible way
  (new optional field, new optional query param) is not a breaking
  change and does not require a bump.

## 7. References

- `src/local_transcription_service/api/jobs.py` — route handlers and
  the per-endpoint status-code tables.
- `src/local_transcription_service/api/health.py` — liveness and
  readiness probes.
- `src/local_transcription_service/api/schemas.py` — the pydantic
  models that back this contract; changes here are the canonical
  source.
- `src/local_transcription_service/auth.py` — token validation logic.
- `docs/openapi.json` — auto-generated from the same FastAPI app
  (regenerate via `scripts/extract-openapi.py`).
- HLD-001 §6 (submit/poll semantics), §8 (probes and lease), §9
  (state machine), §10 (retry policy), §13.1 (ack-and-move),
  §14 (auth).
