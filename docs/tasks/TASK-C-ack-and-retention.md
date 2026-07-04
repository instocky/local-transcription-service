# TASK-C — Ack endpoint and result retention (HLD-001 §13.1, O-4)

| Field       | Value                                                                       |
|-------------|-----------------------------------------------------------------------------|
| Phase       | C (next phase after Phase B + B6 follow-up)                                 |
| Depends on  | Phase B `main` (`d492088`) — completes the result-lifecycle gap from HLD-001 O-4 |
| HLD         | HLD-001 §13 amended (new §13.1 Lifecycle & ack); §17 O-4 corrected (GET → POST) |
| ADR         | ADR-012 unchanged                                                            |
| Status      | READY → IMPLEMENTING 2026-07-04                                              |

## 1. Goal

Close out HLD-001 Open Decision **O-4** by:

1. Adding `POST /jobs/{job_id}/ack` (idempotent) — the extension
   calls this after a successful transcript download.
2. Setting `acked_at` on the job row inside an atomic single-statement
   UPDATE so concurrent ack calls collapse to one write.
3. Moving the transcript file from its current path into
   `${LTS_DATA_DIR}/trash/`, preserving the source basename
   (`Path.replace` — atomic on the same volume, POSIX + Windows).
   In MVP the source is always the Stage-3 output at
   `${LTS_DATA_DIR}/results/{job_id}.md`, so the destination is
   `${LTS_DATA_DIR}/trash/{job_id}.md`; the basename-preserve rule
   covers the operator-renamed edge case without leaving stale
   files.
4. Updating `transcript_path` in the DB so subsequent
   `GET /jobs/{id}/result` calls stream from the trash location.
5. Surfacing DB failures as `503 DB_UNAVAILABLE` (via
   `aiosqlite.Error` / `sqlite3.Error` catch around the pre-flight
   + `mark_acked` + `update_transcript_path` block) so retries
   can converge.

No new architectural decision; this is operational implementation of an
already-resolved HLD open item. ADR-012 stays unchanged.

## 2. Why now (carryover from Phase B)

Phase A shipped `GET /jobs/{id}/result` (the read path) but no
lifecycle for "the extension has the transcript, what next?". Phase
B added `mark_done` to set `transcript_path`, but the file then
lives forever in `results/` — the "Manual cleanup of `trash/`"
sentence in O-4 was unreachable because no path ever wrote to
`trash/`.

Phase C adds the missing half of the lifecycle.

## 3. Scope

### 3.1 HLD amendment (HLD-001 §13)

Add **§13.1 Lifecycle & ack** (now merged into HLD-001) covering:

- The two-step write order (DB `acked_at` first, FS move second;
  rationale: DB is the source of truth, FS is hygiene).
- Status codes (200 / 404 / 409 / 401) and what each means.
- Idempotency contract (first call vs repeat, `already_acked` flag).
- Failure-mode contract for DB write / FS move / already-moved states.
- The deliberate `GET` → `POST` correction in §17 O-4 (state-mutating
  endpoint cannot be a safe method).

### 3.2 Schema migration

- New `acked_at TEXT` column on the `jobs` table.
- Idempotent `ALTER TABLE ADD COLUMN` in `JobStore.init()`, gated by
  `PRAGMA table_info(jobs)` (same pattern as the existing
  `next_retry_at` migration).
- New `Job.acked_at` field; `to_row()` / `from_row()` updated.

### 3.3 Storage

- `JobStore.mark_acked(job_id) -> tuple[Job, bool]` — atomic single-
  statement UPDATE filtered by `status='done' AND acked_at IS NULL`.
  The rowcount tells the caller whether *this* call did the write.
- `JobStore.update_transcript_path(job_id, path) -> bool` — separate
  method so the endpoint can update the FS-tracked path after the
  rename without coupling the two writes into one transaction.

### 3.4 FS lifecycle helper

New module `queue/transcripts.py` owning the file-move responsibility:

- `move_to_trash(*, job_id, source, trash_dir) -> MoveOutcome` —
  atomic `Path.replace` with graceful handling of:
  - `source` is `None` (DB-only ack),
  - `source` does not exist (race / operator cleanup) — `moved=False`,
    logged warning,
  - `source` already inside `trash_dir` — `moved=False`,
    `destination=source` (no rename needed),
  - `Path.replace` raises `FileNotFoundError` or `OSError` — logged
    and surfaced as `moved=False`.

The endpoint never raises on FS failures (see HLD-001 §13.1).

### 3.5 API endpoint

- `POST /jobs/{job_id}/ack` in `api/jobs.py`; mounted under the
  existing `require_token` dependency.
- `AckResponse` schema in `api/schemas.py`:
  ```python
  {
    "job_id": str,
    "acked_at": datetime,           # ISO 8601 UTC
    "already_acked": bool,          # False on first call, True on retry
    "transcript_moved": bool,       # True if the file is in trash (now or earlier)
    "transcript_path": str | None,  # canonical path after the move
  }
  ```
- Pre-flight `get` → 404 / 409 mapping. The store's `mark_acked`
  is idempotent under concurrent calls.

### 3.6 Tests

- `tests/test_ack.py` — 14 cases (all passing in the user-side
  pytest run, 2026-07-04):
  - 200 newly acked + DB has `acked_at`
  - file actually moves to `trash/`
  - `transcript_path` in DB is updated
  - idempotent retry → `already_acked=True`, original timestamp preserved
  - 404 for unknown job
  - 409 for queued and failed jobs
  - 401 for missing or wrong token
  - `GET /jobs/{id}` after ack surfaces `acked_at` + `transcript_path`
    (test asserts `pre == None` and `post == AckResponse.acked_at`)
  - `GET /jobs/{id}/result` after ack still streams
  - FS-move failure → 200 with `transcript_moved=False`, DB still acked,
    retry then succeeds.
  - `transcript_moved=False` when file deleted from trash (P1 finding).
  - 503 `DB_UNAVAILABLE` when `mark_acked` raises (P3 finding).
  - Retry converges after `update_transcript_path` failure mid-flight
    (P1 regression — auto-discovery heals stale DB path).
  - `GET /jobs/{id}` exposes the new `acked_at` field on
    `JobStateResponse`.
- `tests/test_transcripts.py` — 7 cases for the FS helper in isolation:
  - happy path
  - missing source
  - source already in `trash_dir`
  - trash dir created if missing
  - source is `None`
  - OSError swallowed (logged)
  - Auto-discovery when source gone but canonical trash file present
    (Phase C P1 unit-level coverage).
- `tests/test_store.py` extensions — 11 cases added (1 combined
  non-DONE test replaced with 4 per-status tests), covering:
  - `mark_acked` happy / idempotent / unknown / per-status
    (QUEUED / CLAIMED / PROCESSING / FAILED)
  - `update_transcript_path` happy / unknown
  - legacy DB migration adds `acked_at`

Phase C net delta: **+31 tests across 3 files** (14 new ack + 7 new
transcripts + 10 net store). Total suite `184 passed` (153 Phase B
baseline + 31 net), per user-side pytest run 2026-07-04.

## 4. Out of scope (explicit)

- Automatic trash cleanup (cron / TTL / size cap) — O-4 says
  "Manual cleanup of `trash/` thereafter" and that remains the
  operator's job.
- `GET /jobs/{id}/ack` (some HTTP frameworks treat POST as the only
  mutating verb; an explicit GET that returns the ack state could
  be added later but adds little — the same data is in
  `GET /jobs/{id}.acked_at` after the schema lands).
- Notification (push, email, websocket) that "your transcript is
  ready" — the extension polls; no server-push needed for MVP.
- A separate daemon that walks `trash/` and audits file presence
  against the DB — could be a future runbook item.
- Multi-worker ack coordination. The endpoint is HTTP, not
  worker-bound; concurrent ack calls collapse via the atomic
  UPDATE. Future multi-worker still holds.

## 5. Acceptance criteria

- [ ] New endpoint `POST /jobs/{job_id}/ack` returns 200 on first ack.
- [ ] Same endpoint returns 200 + `already_acked=true` on repeat.
- [ ] The file ends up in `${LTS_DATA_DIR}/trash/` with the source
      basename preserved (typically `{job_id}.md` in MVP).
- [ ] `GET /jobs/{id}` after ack surfaces the trash path.
- [ ] `GET /jobs/{id}/result` after ack still streams the same content.
- [ ] `acked_at` migration is idempotent (re-running `init()` on a
       pre-C DB adds the column without error).
- [ ] `uv run ruff check .` clean.
- [ ] No new dep added; no `requirements.txt`.
- [ ] HLD-001 §13.1 reflects the implementation; §17 O-4 says `POST`
       (no longer `GET`).

## 6. Report-back

Not needed — single-shot implementation. The verifier role picks this
up via the standard pytest + ruff gate; if needed, a b7-style six-check
integration gate can be run after merge to confirm no drift was
introduced.

If the user wants a formal gate before merge, see the
b5-integration-gate procedure documented in TASK-B §9.
