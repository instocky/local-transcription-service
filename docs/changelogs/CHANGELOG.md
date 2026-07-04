# Changelog

Operational drift tracker for the local-transcription-service. Each entry
captures a single deviation between code/docs and the canonical HLD (or a
newly resolved open decision), with what changed and where.

This is **operational history**, not a release log. For new features and
ADR/HLD amendments, see the HLD files in `docs/hld/` and the task docs
in `docs/tasks/`.

## 2026-07-04 — Phase D: trash retention + multi-worker + production hardening (HLD-001 §5 / §13.2 / §15 / §16)

### D1 — Trash retention automation (HLD §13.2 new)

| Change                                                                                                                              | Surface              | Rationale                                                                                                            |
|-------------------------------------------------------------------------------------------------------------------------------------|----------------------|----------------------------------------------------------------------------------------------------------------------|
| New `src/local_transcription_service/retention.py` (498 lines) — `RetentionPolicy` (pure) + `TrashEntry` + `CleanupReport` (frozen dataclasses) + `select_for_deletion` (pure, two-pass TTL→size) + `run_cleanup` (I/O, dry-run aware) + `main` / `amain` CLI entry. | Module               | Single point of truth for the retention policy; pure policy function is unit-testable without a tmpdir.              |
| New `lts-trash-cleanup` console-script + `python -m local_transcription_service.retention` CLI. Exit codes 0/1/2 per TASK-D §3.3.   | CLI                  | Operable by the launchd plist (`-m`) and by hand (`lts-trash-cleanup`).                                             |
| New env vars `LTS_TRASH_TTL_DAYS` (default 7) and `LTS_TRASH_MAX_BYTES` (default 512 MiB). Read by the CLI at start; no live re-config. | Config               | Two-knob policy: age cap + size cap. Both default-on; both tunable.                                                  |
| New `scripts/launchd/com.local-transcription-service.trash-cleanup.plist` — `StartCalendarInterval Hour=4 Minute=0`, `RunAtLoad=false`, env vars retention-only. | Ops                  | Daily tick at 04:00 local. Deleted files are already in `trash/` (post-ack) — no live-pipeline interaction.          |
| `pyproject.toml` — registered `lts-trash-cleanup` console-script under `[project.scripts]`.                                         | Build                | Idempotent `uv sync` exposes the CLI on `$PATH`.                                                                    |
| New `tests/test_retention.py` — 27 cases: 9 pure-function (TTL / size / combined / empty / zero-TTL / zero-max / range validation / dataclass frozen invariants / counter f-string formatting) + 9 I/O tests (happy / keep-recent / dry-run / empty / missing-dir / symlink-not-follow / subdir-skip / not-a-dir / subprocess) + 9 CLI tests. | Tests                | Pin the policy in isolation (pure function) and end-to-end (subprocess).                                             |
| New `docs/runbooks/lts-operations.md` — operator runbook for the trash-cleanup install + log rotation + worker-count + probe verification + rollback. | Docs                 | Single operator-facing doc for Phase D ops; ASCII-only heredoc-friendly.                                              |
| HLD §13.2 new subsection (78 lines) documents the policy, CLI contract, launchd wiring, filesystem invariants.                       | HLD                  | Single source of truth for the operator-facing contract.                                                             |

### D2 — Multi-worker (HLD §5 amended)

| Change                                                                                                                              | Surface              | Rationale                                                                                                            |
|-------------------------------------------------------------------------------------------------------------------------------------|----------------------|----------------------------------------------------------------------------------------------------------------------|
| New env var `LTS_WORKER_COUNT` (default `1`, range `1..64`) on `Settings` (`Field(ge=1, le=64)`).                                  | Config               | Exposes the lease-based claim protocol's multi-worker capability (architecturally supported since Phase A).            |
| `Worker.__init__` accepts `worker_count` (default 1) and `error_rate_counter` (default None); `Worker.run_forever()` spawns N claim tasks cooperatively in the same event loop. Reclaim loop stays single. Each claim task tagged `worker_id=f"w{i}"` in structured logs. | Module               | In-process multi-worker. No bind-port coordination; no cross-process log interleaving. SQLite write-lock is the ceiling. |
| `app.main()` passes `settings.worker_count` into `Worker(...)` and constructs an `ErrorRateCounter`. `_log_config_resolved()` surfaces `worker_count` in the startup event. | Module / Logging     | One-line observability for the deployment shape.                                                                    |
| `JobStore._connect()` async-context-manager helper — every `aiosqlite.connect(self._db_path)` in the module now sets `PRAGMA busy_timeout = 5000` immediately after open. All 14 connection sites refactored. | Module               | Defensive tuning so `/ready` waits on the SQLite write lock instead of failing fast.                                 |
| `scripts/launchd/com.local-transcription-service.plist` adds `LTS_WORKER_COUNT=1` (explicit default). README env table adds a row pointing to §5. | Ops                  | Operators see the knob in the plist and README; default matches the existing single-worker behaviour.                 |
| `tests/test_worker.py` — +3 cases (`test_run_forever_with_worker_count_4_processes_concurrent_jobs`, `test_concurrent_claim_only_one_worker_wins_per_job`, `test_concurrent_mark_processing_respects_lease`). | Tests                | Pin the race-free claim property of the SQL with deterministic drain.                                               |
| `tests/test_store.py` — +2 cases (`test_concurrent_reclaim_is_safe`, `test_busy_timeout_set_on_every_connection`).               | Tests                | Pin the atomic reclaim and the per-connection pragma.                                                                |
| `tests/test_config.py` — +5 cases (default, env-var pick-up, zero-rejected, too-large-rejected, non-integer-rejected).            | Tests                | Pin the `Field(ge=1, le=64)` contract.                                                                                |
| HLD §5 amended to "one FastAPI process; N concurrent claim loops inside it" + new §5.1–§5.4 (why in-process, race-condition audit table, busy_timeout, what is unchanged). | HLD                  | Boundary test: 1 → N workers is operational, not architectural. ADR-012 stays unchanged.                            |

### D3 — Production hardening (HLD §15 / §16 amended)

| Change                                                                                                                              | Surface              | Rationale                                                                                                            |
|-------------------------------------------------------------------------------------------------------------------------------------|----------------------|----------------------------------------------------------------------------------------------------------------------|
| New `scripts/launchd/local-transcription-service.conf` — `newsyslog` config: `count=5`, `size=10M`, `when=$D0`, `flags=JN` for both `local-transcription-service.log` and `local-transcription-service.trash-cleanup.log`. Operator installs via `sudo cp ... /etc/newsyslog.d/`. | Ops                  | Built-in macOS rotation. Keeps the log file bounded without the service rotating its own stdout.                    |
| `docs/runbooks/lts-operations.md` updated with the newsyslog install step (`sudo cp ... ; sudo newsyslog -v`) and rollback.        | Docs                 | Operator-facing install path; lives next to the existing plist install.                                              |
| New `app.main()` startup probe: `app._startup_probe(engine)` calls `asyncio.wait_for(engine.is_ready(), timeout=5.0)`; failure or False → `_run` exits `78` (`EX_CONFIG`) so launchd does not auto-restart. | Module               | launchd does not auto-restart on `EX_CONFIG`. The service stays down rather than half-broken.                      |
| New `src/local_transcription_service/metrics.py` — `ErrorRateCounter` (dict + asyncio.Lock) + `run_error_rate_loop` background task that emits `error_rate_tick` every 60 s with per-code counts. `Worker._handle_pipeline_failure` increments on the terminal-FAIL path only (NOT on retry defer). | Module               | Aggregate observability without a new endpoint; HLD §15 promise of "no metrics endpoint for MVP" is kept.            |
| `tests/test_metrics.py` — 7 cases (per-code counts, tick returns + resets, reset clears, fresh-dict semantics, empty-after-init, log-line emission, stop-event short-circuit). | Tests                | Pin the counter shape and the tick loop's lifecycle.                                                                 |
| `tests/test_app.py` — +3 cases (`test_startup_probe_returns_true_when_ready`, `test_startup_probe_returns_false_when_not_ready`, `test_startup_probe_returns_false_when_probe_raises`). | Tests                | Pin the probe contract: ready → True, not-ready → False + log, raises → False + log.                                |
| HLD §15 amended (worker_count in `config_resolved`, §15.1 error-rate counter, §15.2 what we are NOT doing). HLD §16 amended (§16.1 healthcheck-on-start, §16.2 log rotation, §16.3 trash cleanup plist). | HLD                  | All three items are operational; HLD is the right home.                                                             |

### Phase D net delta (reconfirmed)

- **New files (7):** `src/local_transcription_service/retention.py`,
  `src/local_transcription_service/metrics.py`,
  `tests/test_retention.py`, `tests/test_metrics.py`,
  `scripts/launchd/com.local-transcription-service.trash-cleanup.plist`,
  `scripts/launchd/local-transcription-service.conf`,
  `docs/runbooks/lts-operations.md`.
- **Modified (5):** `src/local_transcription_service/config.py` (1 field),
  `src/local_transcription_service/queue/store.py` (1 helper + 14 call-site refactors),
  `src/local_transcription_service/worker.py` (constructor + run_forever + worker_id threading + counter),
  `src/local_transcription_service/app.py` (startup probe + counter wiring),
  `scripts/launchd/com.local-transcription-service.plist` (LTS_WORKER_COUNT=1),
  `pyproject.toml` (1 console-script),
  `README.md` (env table + Status section),
  `docs/hld/HLD-001-local-transcription-service.md` (§5, §13.2, §15, §16 amendments).
- **New tests: +46 net** — Phase C baseline 184 → **Phase D 230**. Breakdown:
  `test_retention.py` +27, `test_metrics.py` +7, `test_worker.py` +3,
  `test_store.py` +2, `test_config.py` +5, `test_app.py` +3 — exceeds
  the TASK-D §8 target of +18 net; every additional case is meaningful.
- **No new dependency; no `requirements.txt`; ADR-012 unchanged.**

### Verification at merge time

- `uv run pytest -q` — **230 passed, 1 skipped** in 15.28s (Windows fs symlink skip).
  Phase C baseline 184 → Phase D 230, +46 net.
- `uv run ruff check .` — clean (all checks passed).
- **Post-merge review bump (2026-07-04):** the Tech Lead P1 review added
  2 race-accounting tests for `retention.run_cleanup` —
  `test_run_cleanup_accounts_for_already_gone_files` and
  `test_run_cleanup_accounts_already_gone_alongside_real_deletions`.
  Current HEAD reports **232 passed, 1 skipped** (Phase D net +48);
  the +2 delta is the P1 review fix on `main`, not a new phase.
  The numbers above (230 / +46) are the **at-merge snapshot**; the
  README Status section carries the current-HEAD number (232 / +48).
- **Drift check vs HLD-001 amendments (manual grep):**
  - `git grep -nE "single async worker"` over `src/` returns nothing. ✅
  - `git grep -nE "Manual cleanup of .trash"` over `docs/` returns nothing. ✅
  - `git grep -n "sys.exit(78)"` over `src/` returns 1 hit in `app.py`. ✅
  - `git grep -n "lts-trash-cleanup"` over `pyproject.toml` returns 1 hit. ✅
- **Secret scan:** `git grep -nE "sk-[a-z0-9]{20,}"` over tracked files — no hits. ✅
- **`.mavis/plans/` not committed** (`git ls-files .mavis` returns empty). ✅
- **Race-condition sanity:** `test_run_forever_with_worker_count_4_processes_concurrent_jobs`
  runs 8 jobs through 4 cooperative claim tasks; all 8 reach DONE
  with deterministic drain via the Phase B6 `done_event` pattern.
  This is the in-test proxy for the d4 race-condition audit (TASK-D
  §4.2): the audit table claims every existing UPDATE is already
  safe under `LTS_WORKER_COUNT > 1`, and this test verifies it in
  practice for the claim → mark_processing → mark_done path.
- **Real-gateway smoke (192.168.0.99:4000)** — **SKIP**, same as Phase B/C:
  the Windows runner cannot reach the Mac Mini. The Phase B opt-in
  integration test (`@pytest.mark.integration`) is the future gate.
- No cloud SDKs touched; no auth scheme change; no new endpoint;
  no new dependency.

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
