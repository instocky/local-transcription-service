# Changelog

Operational drift tracker for the local-transcription-service. Each entry
captures a single deviation between code/docs and the canonical HLD (or a
newly resolved open decision), with what changed and where.

This is **operational history**, not a release log. For new features and
ADR/HLD amendments, see the HLD files in `docs/hld/` and the task docs
in `docs/tasks/`.

## 2026-07-04 — Phase C: ack endpoint + retention (HLD-001 §13.1)

Implementing the resolved Open Decision O-4 from HLD-001 §17.

| Change                                                                                                                                                | Surface              | Rationale                                                                                                          |
|--------------------------------------------------------------------------------------------------------------------------------------------------------|----------------------|--------------------------------------------------------------------------------------------------------------------|
| New endpoint `POST /jobs/{job_id}/ack` (HLD-001 §13.1).                                                                                                | API                  | Extension calls after a successful download to flip the lifecycle and trigger retention.                           |
| **GET → POST** correction in HLD-001 §17 O-4. The original wording used `GET`; ack is state-mutating (DB write + FS rename) and MUST be `POST`.        | HLD / contract       | RFC 9110 §9.2.1 — mutating endpoints cannot be safe-method; idempotency is about *effect* idempotency, not verb.   |
| New `acked_at` column on the `jobs` table with idempotent `ALTER TABLE ADD COLUMN` migration in `JobStore.init()`.                                      | Storage              | Idempotency marker: a repeat ack is a no-op; first ack wins the timestamp.                                          |
| New `mark_acked()` and `update_transcript_path()` on `JobStore`.                                                                                        | Storage              | Atomic single-statement update of `acked_at`; path update follows the FS rename.                                    |
| New `queue/transcripts.py::move_to_trash` — atomic `Path.replace` from the source path into `trash/`, preserving the source basename (typically `{job_id}.md` in MVP since Stage 3 writes that name). Handles missing-source, same-location, and OSError. | FS                   | Single point of truth for the FS handoff; unit-testable in isolation.                                              |
| New `AckResponse` schema (`api/schemas.py`) and `POST /jobs/{job_id}/ack` handler (`api/jobs.py`).                                                    | API                  | Wire contract for the extension. `already_acked` + `transcript_moved` separate the two idempotency axes.           |
| Tests: `test_ack.py` (14 cases — happy path / idempotent retry / 404 / 409 / 401 / FS-failure / file-deleted-from-trash / 503 DB_UNAVAILABLE / partial-failure convergence / `acked_at` in JobStateResponse) + `test_transcripts.py` (7 cases — incl. auto-discovery unit test) + `test_store.py` extensions (10 net: 11 added, 1 combined non-DONE test replaced with 4 per-status tests). All passing in user-side pytest run. | Tests                | Pin the contract end-to-end and isolate the FS helper.                                                             |

#### Phase C verification at merge time (per user-side pytest run, 2026-07-04)

- `uv run pytest -q` — **184 passed** (was 153 after Phase B;
  Phase C net `+31` across `test_ack.py` 14 new,
  `test_transcripts.py` 7 new, `test_store.py` 10 net).
- `uv run ruff check .` — clean.

### Failure-mode contract (HLD-001 §13.1)

- DB write fails (any call in the DB-touching block — `mark_acked`,
  `update_transcript_path`, or the pre-flight `get`): the endpoint
  catches `aiosqlite.Error` / `sqlite3.Error` uniformly around the
  pre-flight + `mark_acked` + `update_transcript_path` calls and
  surfaces `503 DB_UNAVAILABLE`. Two sub-cases with different FS state:
    a. `mark_acked` (or pre-flight) raises before `move_to_trash`
       ran — file untouched; retry is trivial.
    b. `update_transcript_path` raises after a successful
       `move_to_trash` — file is already in `trash/`, DB path is
       stale; retry auto-discovers the canonical trash file and
       heals the DB (Phase C P1, 2026-07-04).
  In both sub-cases retry converges.
- File move fails because source doesn't exist (race / operator
  cleanup / manual delete from `trash/`): `200` with
  `transcript_moved=False`; logged warning; `acked_at` set if
  the DB write succeeded. The retry that follows attempts the
  move again — no-op if source already equals destination,
  auto-discovery if a canonical trash file is present.
- File move fails for any other OSError (permission, cross-volume,
  full filesystem): `200` with `transcript_moved=False`; logged
  warning. Operator can drag the file to `trash/` manually; the
  next ack call will see it already there (and present on disk)
  and report `transcript_moved=True`.

#### File-gone-from-trash invariant

`transcript_moved` reflects the **observed** filesystem state at
return time, not just the DB path. The endpoint's predicate is
`Path(transcript_path).parent.resolve() == trash_dir.resolve() and
Path(transcript_path).exists()`. An operator who deletes the file
from `trash/` after a successful ack will see the next retry return
`transcript_moved=False` rather than a stale "moved=True" — a fix
for a bug surfaced by the Phase C review (P1 finding, 2026-07-04).

#### Auto-discovery on retry (DB-stale recovery)

`move_to_trash` falls back to a **canonical-path search** when the
source is gone but `${trash_dir}/{source.basename}` exists on
disk. The endpoint uses this destination to call
`update_transcript_path`, healing a stale DB row that a previous
partial-failure left behind. Net effect: a 503 from a failed
`update_transcript_path` mid-flight converges on the next ack
instead of poisoning `GET /jobs/{id}/result` indefinitely. Two
tests pin this — `test_ack_converges_after_update_transcript_path_failure`
(end-to-end) and `test_move_to_trash_auto_discovers_when_source_missing`
(unit, on `move_to_trash` alone).

## 2026-07-04 — Phase B follow-up + drift cleanup (TASK-B6)

Closing out TASK-B6 plus the drift items surfaced by the b5 verifier and
the Tech Lead review of 2026-07-03.

| Change                                                                                                                                                | Surface              | Rationale                                                                                                          |
|--------------------------------------------------------------------------------------------------------------------------------------------------------|----------------------|--------------------------------------------------------------------------------------------------------------------|
| `scripts/launchd/com.local-transcription-service.plist` migrated from `LTS_OLLAMA_BASE_URL` / `LTS_STT_ENGINE=ollama` to `LTS_STT_BASE_URL` / `LTS_STT_API_KEY` / `LTS_STT_ENGINE=openai`. | Ops                  | b3-config moved the code; plist was missed. Drift fix.                                                              |
| `.gitignore` — added `.mavis/`.                                                                                                                        | Ops                  | The runtime dir was untracked but a future `git add .` would pull it in.                                           |
| `tests/test_worker.py::test_run_forever_processes_multiple_jobs` — replaced fixed `asyncio.sleep(0.2)` with deterministic per-job `Event` drain.            | Tests                | Phase A flake (3/5 isolated runs failed on Windows SQLite write-lock contention). Gate item 1 from b5.             |
| `src/.../worker.py` — aligned transcript file extension with HLD-001 §11/§13: `.txt` → `.md`.                                                          | Pipeline             | HLD §11 specifies `.md`; the Stage 3 writer was emitting `.txt`. Drift fix.                                         |
| `docs/hld/HLD-001-local-transcription-service.md` — reconciled JobStatus semantics, env-var contract wording, and failure-mode descriptions.              | HLD                  | Pre-merge self-consistency pass.                                                                                    |
| `README.md` + `api/health.py` docstrings — `HLD §9.2` → `§6` and other section-reference drift.                                                       | Docs                 | `§9.2` referred to an old numbering; the health endpoint was actually specced in §6. Drift fix.                     |
| `pipeline/stages.py` — split yt-dlp error patterns into permanent (SSL/cert) vs transient (network).                                                  | Pipeline             | HLD §12 distinguishes them; the code was lumping both as `FETCH_FAILED retryable=True`. Drift fix.                  |
| `stt/litellm_whisper.py` — short timeout on `is_ready()`'s `GET /v1/models`; deterministic yt-dlp output (`--ignore-config`, no sidecar files); response-shape validation. | STT / Pipeline       | Probes must not hang; yt-dlp config files were leaking between runs; `/models` response was unvalidated. Drift fix. |

### Verification at merge time

- `uv run pytest -q` — 153 passed in 10.61s (was 142; +11 from Phase B + B6 work).
- `uv run ruff check .` — clean.
- Real-gateway smoke (`192.168.0.99:4000`) — **SKIP**, same as b5: the
  Mac Mini is unreachable from the Windows runner. The Phase B opt-in
  integration test (`@pytest.mark.integration`) is the future gate for
  this; it requires a Linux/Mac host with the gateway reachable.
- `.mavis/plans/` not committed — passed.
- Secret scan (regex `sk-[a-z0-9]{20,}` over tracked files) — passed.
