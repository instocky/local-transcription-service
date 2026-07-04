# Changelog

Operational drift tracker for the local-transcription-service. Each entry
captures a single deviation between code/docs and the canonical HLD (or a
newly resolved open decision), with what changed and where.

This is **operational history**, not a release log. For new features and
ADR/HLD amendments, see the HLD files in `docs/hld/` and the task docs
in `docs/tasks/`.

## 2026-07-04 — Phase D: trash retention + multi-worker + production hardening (HLD-001 §5 / §13.2 / §15 / §16)

> **Status: STUB** — placeholder added at TASK-D draft time (2026-07-04);
> populated at merge time. The stub exists so the structure of the entry
> is locked in early and reviewers can sanity-check it against the task
> doc before any commits land on `main`. The per-change rows below list
> the surfaces the entry will cover; concrete file/line references and
> evidence (test counts, gate results) get filled in at merge.

### D1 — Trash retention automation (HLD §13.2 new)

| Change                                                                                                                              | Surface              | Rationale                                                                                                            |
|-------------------------------------------------------------------------------------------------------------------------------------|----------------------|----------------------------------------------------------------------------------------------------------------------|
| New `src/local_transcription_service/retention.py` — `RetentionPolicy` (pure) + `run_cleanup` (I/O) + `CleanupReport` (dataclass).  | Module               | Single point of truth for the retention policy; pure policy function is unit-testable without a tmpdir.              |
| New `lts-trash-cleanup` console-script + `python -m local_transcription_service.retention` CLI. Exit codes 0/1/2 per TASK-D §3.3.   | CLI                  | Operable by the launchd plist (`-m`) and by hand (`lts-trash-cleanup`).                                             |
| New env vars `LTS_TRASH_TTL_DAYS` (default 7) and `LTS_TRASH_MAX_BYTES` (default 512 MiB). Read by the CLI at start; no live re-config. | Config               | Two-knob policy: age cap + size cap. Both default-on; both tunable.                                                  |
| New `scripts/launchd/com.local-transcription-service.trash-cleanup.plist` — `StartCalendarInterval Hour=4 Minute=0`, `RunAtLoad=false`. | Ops                  | Daily tick at 04:00 local. Deleted files are already in `trash/` (post-ack) — no live-pipeline interaction.          |
| New `tests/test_retention.py` — 7 cases (TTL-only, size-only, combined, empty, dry-run, symlink, CLI subprocess).                    | Tests                | Pin the policy in isolation (pure function) and end-to-end (subprocess).                                             |
| HLD §13.2 new subsection documents the policy, CLI contract, launchd wiring, filesystem invariants.                                | HLD                  | Single source of truth for the operator-facing contract.                                                             |

### D2 — Multi-worker (HLD §5 amended)

| Change                                                                                                                              | Surface              | Rationale                                                                                                            |
|-------------------------------------------------------------------------------------------------------------------------------------|----------------------|----------------------------------------------------------------------------------------------------------------------|
| New env var `LTS_WORKER_COUNT` (default `1`, range `1..64`) on `Settings`.                                                          | Config               | Exposes the lease-based claim protocol's multi-worker capability (architecturally supported since Phase A).            |
| `Worker.__init__` accepts `worker_count`; `Worker.run_forever()` spawns N claim tasks cooperatively. Reclaim loop stays single.       | Module               | In-process multi-worker. No bind-port coordination; no cross-process log interleaving. SQLite write-lock is the ceiling. |
| `app.main()` passes `settings.worker_count` into `Worker(...)`. `config_resolved` log event surfaces `worker_count`.                | Module / Logging     | One-line observability for the deployment shape.                                                                    |
| New `PRAGMA busy_timeout = 5000` on every store connection.                                                                          | Module               | Defensive tuning so `/ready` waits on the SQLite write lock instead of failing fast.                                 |
| `scripts/launchd/com.local-transcription-service.plist` adds `LTS_WORKER_COUNT=1` (explicit default). README env table adds a row.   | Ops                  | Operators see the knob in the plist and README; default matches the existing single-worker behaviour.                 |
| `tests/test_worker.py` + `tests/test_store.py` + `tests/test_config.py` — +6 net cases (concurrent jobs, concurrent claim, lease-token respect, concurrent reclaim, range validation, default). | Tests                | Pin the race-free claim property of the SQL and the new config contract.                                            |
| HLD §5 amended to "one FastAPI process; N concurrent claim loops inside it" + new §5.1–§5.4 (why in-process, race-condition audit, busy_timeout, what is unchanged). | HLD                  | Boundary test: 1 → N workers is operational, not architectural. ADR-012 stays unchanged.                            |

### D3 — Production hardening (HLD §15 / §16 amended)

| Change                                                                                                                              | Surface              | Rationale                                                                                                            |
|-------------------------------------------------------------------------------------------------------------------------------------|----------------------|----------------------------------------------------------------------------------------------------------------------|
| New `scripts/launchd/local-transcription-service.conf` — `newsyslog` config: `count=5`, `size=10M`, `when=$D0`, `flags=JN`. Operator installs via `sudo cp ... /etc/newsyslog.d/`. | Ops                  | Built-in macOS rotation. Keeps the log file bounded without the service rotating its own stdout.                    |
| Runbook updated with the newsyslog install step.                                                                                    | Docs                 | Operator-facing install path; lives next to the existing plist install.                                              |
| New `app.main()` startup probe: `asyncio.wait_for(engine.is_ready(), timeout=5.0)` → exit `78` (`EX_CONFIG`) if not ready / raises. | Module               | launchd does not auto-restart on `EX_CONFIG`. The service stays down rather than half-broken.                      |
| New `src/local_transcription_service/metrics.py::ErrorRateCounter` — emits `error_rate_tick` every 60 s with per-code counts.       | Module               | Aggregate observability without a new endpoint; HLD §15 promise of "no metrics endpoint for MVP" is kept.            |
| `tests/test_metrics.py` + `tests/test_app.py` — +5 net cases (counter emits counts, counter resets; exit-78 not-ready, exit-78 probe-raised, happy path). | Tests                | Pin the counter shape and the startup-exit contract.                                                                |
| HLD §15 amended (worker_count in `config_resolved`, §15.1 error-rate counter, §15.2 what we are NOT doing). HLD §16 amended (§16.1 healthcheck-on-start, §16.2 log rotation, §16.3 trash cleanup plist). | HLD                  | All three items are operational; HLD is the right home.                                                             |

### Phase D net delta (target, reconfirmed at merge time)

- New files: `retention.py`, `metrics.py`, `test_retention.py`,
  `test_metrics.py`, `trash-cleanup launchd plist`,
  `newsyslog` config.
- Modified: `config.py` (one field), `worker.py` (constructor +
  run_forever), `app.py` (startup probe), `store.py` (busy_timeout),
  `scripts/launchd/com.local-transcription-service.plist`,
  `README.md` (env table + status section), runbook
  (newsyslog install step), `pyproject.toml` (one console-script).
- New tests: **+18 net** (target; actual reconfirmed at merge).
  Phase C baseline 184 → Phase D target 202.
- No new dependency; no `requirements.txt`; ADR-012 unchanged.

### Verification at merge time (filled in post-merge)

- `uv run pytest -q` — TBD (target: 202 passed).
- `uv run ruff check .` — clean.
- No cloud SDKs touched; no auth scheme change; no new endpoint.

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
