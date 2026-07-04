# TASK-D — Operational hardening (trash retention, multi-worker, production polish)

| Field      | Value                                                                                                  |
| ---------- | ------------------------------------------------------------------------------------------------------ |
| Phase      | D                                                                                                      |
| Depends on | Phase C `main` (`150c43d`) — O-4 resolved; MVP functionally complete                                   |
| HLD        | HLD-001 §5 amended (multi-worker), §13.2 new (trash retention), §15/§16 amended (production hardening) |
| ADR        | ADR-012 unchanged — all three items are operational, not architectural                                 |
| Status     | DRAFT 2026-07-04 — pending Tech Lead review before dev dispatch                                        |

## 1. Goal

Close three operational gaps that the MVP shipped with, all of which
were **explicitly deferred** in earlier phases because they were not on
the critical path to a working `POST /jobs` → transcript loop:

1. **D1 — Trash retention automation.** Phase C (§13.1) made the
   `trash/` directory the resting place for acked transcripts, but
   `trash/` grows unbounded. Add a deterministic retention policy
   (TTL + size cap), ship it as an idempotent CLI command, and wire
   it into launchd as a separate periodic job.
2. **D2 — Multi-worker.** HLD-001 §5 _currently_ states "single
   FastAPI process, single async worker". The lease protocol was
   designed multi-process from day one (Phase A), but no
   configuration knob exposed it. Add `LTS_WORKER_COUNT` and run
   N claim loops in the same process as cooperative asyncio tasks.
   SQLite write-lock is the throughput ceiling — measured, not
   theoretical.
3. **D3 — Production hardening.** Three small loose ends from
   HLD-001 §15/§16: log rotation (launchd currently captures stdout
   to a single file that grows forever), healthcheck-on-start (the
   plist starts the process before dependencies like the STT
   gateway are reachable), and a structured-error rate counter
   (HLD §15 says "no metrics endpoint for MVP" — this keeps the
   promise by emitting counters to the JSON log feed).

Three sub-tasks, **one merge** — the package is a coherent
"production-ready" landing, none of the three are useful on their
own (trash cleanup without the launchd plist is manual; multi-worker
without healthcheck-on-start loses the launchd auto-restart
benefit; rate metric without log rotation is unreadable).

### 1.1 Why no new ADR

Boundary check (AGENTS.md / HLD §1):

> _What changes in this document if I scale from 1 worker to N workers,
> swap SQLite for Redis, or move from Mac Mini to Jetson?_
>
> If "nothing" — the decision belongs in an **ADR** (architecture contract).

- **D1**: retention is operational (cron-driven, no API surface).
- **D2**: `1 → N workers` in the same process against the same
  SQLite changes **nothing** about the persistence paradigm, the
  trust model, or the extension contract. Per the boundary test,
  this is HLD-only.
- **D3**: log rotation, startup probe, and an in-logs counter are
  operational hardening. No contract surface changes.

If a future phase introduces **shared-queue** (Redis/RabbitMQ) or
**multi-host** deployment, that decision will need a new ADR. Not
now. ADR-012 stays unchanged.

## 2. Decision matrix — what we are NOT building

This phase is bounded by what MVP does not need. Recording the
rejections so the next reviewer does not re-open them:

| Item                                                                 | Verdict | Reason                                                                                                                                                                                                       |
| -------------------------------------------------------------------- | ------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Redis / RabbitMQ / SQS as the queue backend                          | **NO**  | Phase A explicitly picked SQLite + lease; ADR-012 says "low operational complexity"; single-user LAN tool. Phase D does not change that.                                                                     |
| Multi-process deployment (N `local-transcription-service` processes) | **NO**  | In-process `LTS_WORKER_COUNT` covers the throughput case for MVP. Multi-process adds bind-port coordination and is a deployment-shape decision, deferred.                                                    |
| `POST /jobs/{id}/ack` moved from idempotent to ETAG/If-Match         | NO      | Idempotency-via-row-state is the simpler contract (HLD §13.1); ETags add a wire detail without buying anything.                                                                                              |
| TTL on the queue itself (DONE jobs older than N days → DELETE)       | NO      | The extension owns the lifecycle via `POST /jobs/{id}/ack`; auto-purging DONE rows breaks the extension's `transcript_path` history. The contract is: nothing in `jobs.db` expires while the job row exists. |
| Per-process log destination / log router                             | NO      | macOS launchd already gives per-process stdout capture; structured JSON to stdout is enough. Multi-process deployment (which would need log routing) is out of scope.                                        |
| Auth on the trash-cleanup CLI                                        | NO      | CLI is operator-local — invokes only on the Mac Mini, not over the network. Documented as "operator-only" in the runbook.                                                                                    |

## 3. Sub-task D1 — Trash retention automation

### 3.1 Design

**Retention policy** is a two-knob combination. Both default-on,
both tunable via env vars that the CLI reads at start (no live
reconfiguration — the CLI runs, exits, and launchd fires the next
instance):

| Knob           | Env var (CLI reads)   | Default               | Semantics                                                                                                 |
| -------------- | --------------------- | --------------------- | --------------------------------------------------------------------------------------------------------- |
| Age cap (TTL)  | `LTS_TRASH_TTL_DAYS`  | `7`                   | Files in `trash/` with `mtime < now() - TTL` are deleted (oldest first).                                  |
| Total size cap | `LTS_TRASH_MAX_BYTES` | `536870912` (512 MiB) | If the cumulative size of `trash/` exceeds the cap, delete the oldest files (by `mtime`) until under cap. |

A single invocation runs the two passes in order: TTL first, then
size cap. Each pass is independent and idempotent — running the CLI
twice in a row is a no-op the second time.

**Filesystem shape after retention:** `trash/` is allowed to be
empty; `trash/` is **not allowed to be deleted**. Operators who
want to wipe everything do it manually with `rm -rf` (and accept
that the next transcript that lands there re-creates the
directory).

**Why a CLI, not an in-process background task:** two reasons,
both about multi-worker (D2):

1. Once we run N claim loops in the same process, an in-process
   cleanup loop would run N times per `LTS_RECLAIM_INTERVAL_SECONDS`
   — each loop walking `trash/` independently. A single CLI
   scheduled by launchd runs once across the whole system.
2. The CLI is testable end-to-end with a tmpdir + a few fake
   transcript files; an in-process task would need lifecycle
   plumbing (start/stop, joined with shutdown).

### 3.2 Module layout

New module `src/local_transcription_service/retention.py`:

```python
class RetentionPolicy:
    ttl_days: int = 7
    max_bytes: int = 512 * 1024 * 1024

    def select_for_deletion(self, files: list[TrashEntry]) -> list[TrashEntry]:
        """Return the files to delete, oldest first. Pure function — no I/O."""

async def run_cleanup(*, trash_dir: Path, policy: RetentionPolicy, dry_run: bool = False, logger: logging.Logger) -> CleanupReport:
    """One retention pass. Returns counts (deleted, kept, freed_bytes).

    `dry_run=True` walks the directory, computes the deletion set, but
    does NOT unlink. CI uses this to assert the policy without side effects.
    """
```

`CleanupReport` is a `dataclass(frozen=True)` — easy to assert
on in tests, easy to log as a single JSON line at the end of the
run.

### 3.3 CLI entry point

Add a console-script in `pyproject.toml`:

```toml
[project.scripts]
lts-trash-cleanup = "local_transcription_service.retention:main"
```

`python -m local_transcription_service.retention` also works (for
launchd, which often calls `python -m` rather than the script).

CLI flags:

- `--dry-run` — log the deletion plan, exit 0, no `unlink()`.
- `--data-dir PATH` — override `${LTS_DATA_DIR}` for one-off
  runs against a non-default dir (operator escape hatch).
- All retention knobs come from env, not flags — matches the rest
  of the service's config discipline.

Exit codes:

| Code | Meaning                                                                                                                                                |
| ---- | ------------------------------------------------------------------------------------------------------------------------------------------------------ |
| 0    | Success (zero or more files deleted; cleanup converged).                                                                                               |
| 1    | Configuration error (env var parse failed, `trash_dir` missing or not a directory). Print to stderr, exit 1.                                           |
| 2    | Runtime I/O error (permission denied, fs went read-only mid-run). Cleanup is best-effort — partial deletes OK; the next launchd tick handles the rest. |

### 3.4 launchd plist

New file `scripts/launchd/com.local-transcription-service.trash-cleanup.plist`:

- Label: `com.local-transcription-service.trash-cleanup`.
- `ProgramArguments`: same Python + module invocation as the
  main plist, just `-m local_transcription_service.retention`.
- `StartCalendarInterval`: `Hour=4, Minute=0` — once a day at
  04:00 local. Low-traffic window; if the user is mid-transcript
  at that moment, the deleted files are already in `trash/`
  (post-ack), so there is no live-pipeline interaction.
- `RunAtLoad`: `false` — no point running at boot; we want the
  daily tick.
- `StandardOutPath` / `StandardErrorPath`: same
  `~/Library/Logs/local-transcription-service.trash-cleanup.log`.
- `EnvironmentVariables`: copies only the retention knobs
  (`LTS_DATA_DIR`, `LTS_TRASH_TTL_DAYS`, `LTS_TRASH_MAX_BYTES`).
  No `LTS_AUTH_TOKEN` — the CLI doesn't need it.

### 3.5 Tests

`tests/test_retention.py` — pure-function tests + integration
with tmpdir:

- TTL-only: 10 files with `mtime = now - {1, 5, 10, 20, 30} days`,
  TTL=7 → 3 deleted, 7 kept, freed_bytes sum correct.
- Size-cap-only: 8 files totalling 1 GiB, cap=512 MiB → oldest
  4 deleted.
- Both knobs combined: TTL deletes some, then size cap catches
  the remainder.
- Empty `trash/` → no-op, exit 0.
- Dry-run: same input → same selection, no `unlink()` (mock
  `Path.unlink`, assert not called).
- CLI subprocess test: `subprocess.run([sys.executable, "-m",
...], env=...)` with a tmpdir-based `LTS_DATA_DIR`, verify
  files actually disappear and `CleanupReport` line hits stdout
  in JSON.
- Symlink in `trash/` → `Path.unlink(missing_ok=True)` does not
  follow (defensive — operator could have dropped a link there;
  we never create them, but we must not crash).

## 4. Sub-task D2 — Multi-worker (`LTS_WORKER_COUNT`)

### 4.1 Design

A single env var `LTS_WORKER_COUNT` (positive int, default `1`)
controls how many claim loops run in the same process. Each
loop is a separate `asyncio.Task` inside the existing
`Worker.run_forever()` machinery, racing for jobs via the
existing `JobStore.claim()` SQL (which is already atomic at the
SQLite statement level — `UPDATE ... WHERE status='queued'`
either matches one row or zero, never two).

Why in-process and not multi-process:

- **No bind-port coordination.** One HTTP port, one HTTP server,
  one FastAPI app. Multi-process would force port shifting
  (worker-only processes have no HTTP listener, but they would
  need a way to be told "stop claiming, the HTTP-only process is
  rebalancing").
- **No cross-process log interleaving from the same line of work.**
  Each worker task gets a stable `worker_id` (`f"w{i}"`) in
  structured log events. Easy to grep, easy to correlate.
- **SQLite's write-lock is the throughput ceiling.** One process
  with 4 tasks has the same ceiling as 4 processes. Going
  in-process saves the IPC overhead and gets us the
  throughput-oracle for free.
- **If in-process workers prove insufficient, multi-process is a
  deployment-shape change**, not a code change — `Worker` is
  already process-agnostic. The HLD amendment explicitly leaves
  that path open.

### 4.2 Race-condition audit (cross-validated against

`worker.py` and `store.py`)

Read against the Phase C code, 2026-07-04:

| Race                                                                              | Safe? | Why                                                                                                                                                                                                                                                                                                                                    |
| --------------------------------------------------------------------------------- | ----- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Two claim tasks race for the same QUEUED job                                      | YES   | `store.claim()` is one atomic UPDATE; the WHERE clause filters by `status='queued'` so only one task matches.                                                                                                                                                                                                                          |
| Two claim tasks both get a job in the same iteration (different jobs)             | YES   | Each claim returns a distinct row; no shared state.                                                                                                                                                                                                                                                                                    |
| Two reclaim tasks race for the same expired lease                                 | YES   | `reclaim_expired()` is one atomic UPDATE; `lease_expires_at < ?` filters the set per call.                                                                                                                                                                                                                                             |
| Two claim tasks race `store.mark_processing` for the same job                     | YES   | `WHERE status='claimed' AND lease_token=:token` — only the task that holds the lease token matches.                                                                                                                                                                                                                                    |
| Two claim tasks race `store.mark_done` for the same job                           | YES   | Same lease-token filter.                                                                                                                                                                                                                                                                                                               |
| Two claim tasks race `store.mark_failed` for the same job                         | YES   | Same lease-token filter.                                                                                                                                                                                                                                                                                                               |
| Two claim tasks race `store.defer_retry` for the same job                         | YES   | Same lease-token filter.                                                                                                                                                                                                                                                                                                               |
| Two `mark_acked` calls (Phase C, from two extension clients) on the same DONE job | YES   | `WHERE status='done' AND acked_at IS NULL` — one UPDATE wins, the other returns `rowcount=0`. The `already_acked` flag is the existing second axis.                                                                                                                                                                                    |
| Two `update_transcript_path` calls racing for the same job                        | YES   | The column is overwritten unconditionally and is **idempotent** for the same value; the loser writes the same value the winner wrote. (Documented in `store.update_transcript_path`.)                                                                                                                                                  |
| Two processes start simultaneously, both call `store.init()`                      | YES   | `init()` opens a connection, executes `CREATE TABLE IF NOT EXISTS`, then `PRAGMA table_info(jobs)` + idempotent `ALTER TABLE ADD COLUMN`. Each `ALTER` is a no-op once the column exists. The connection-per-op pattern means there is no shared in-memory state to race.                                                              |
| Two processes both run `store.ping_writable()` from `/ready`                      | YES   | `BEGIN IMMEDIATE` acquires the SQLite write lock; the second caller waits, then succeeds. `/ready` returns true when both have observed write success (sequential, not concurrent — see §4.3).                                                                                                                                         |
| Two processes both run `trash_cleanup` (D1) on the same `trash/`                  | YES   | `unlink()` is atomic per file. Both passes converge on the same deletion set because each `unlink` is a one-shot operation; the second pass either skips (already gone) or finds nothing to delete. `CleanupReport.deleted` may count the file once across both runs — accepted (the runbook documents this as a property, not a bug). |

All races the codebase has are already safe. Phase D does not
introduce new ones; it widens the workload that exercises the
existing safety.

### 4.3 `/ready` write-lock contention

`store.ping_writable()` uses `BEGIN IMMEDIATE` to force SQLite
to acquire the write lock. With N worker tasks hammering
`store.claim()` every 500 ms, the write lock is held briefly per
claim (microseconds for a single-row UPDATE), but two probes
(`/ready` from monitoring + a claim in flight) will serialize.
The probe is short and unbounded waits are capped by
SQLite's busy_timeout (default — see below). If `/ready`
ever starves, the symptom is a probe returning 503 when the
service is healthy, which is acceptable for a LAN tool; we are
not promising sub-second readiness under heavy contention.

**Action:** set `PRAGMA busy_timeout = 5000` (5 s) on every
connection the store opens (in `store.init()` and on every
`aiosqlite.connect()` path via a small connection helper). This
is a defensive tuning, not a correctness fix — SQLite's default
behaviour on a busy lock is to fail fast, which is fine for the
claim path (the claim loop retries on the next tick) but wrong
for the readiness probe (we want to wait, not fail).

### 4.4 Wire changes

- `config.Settings` — new field `worker_count: int = Field(default=1, ge=1, le=64)`.
  Enforced range prevents typos (`worker_count=0` would mean
  "no workers, service runs HTTP only" — ambiguous; `ge=1`
  forces the intent).
- `worker.Worker` — `__init__` accepts `worker_count: int = 1`,
  `run_forever()` spawns N claim tasks (the reclaim loop stays
  single — it's already idempotent and cheap, and running it
  N times would be pure overhead).
- `app.main()` — passes `settings.worker_count` into `Worker(...)`.
  Logs `worker_count` in the `config_resolved` startup event
  (HLD §15).
- `scripts/launchd/com.local-transcription-service.plist` —
  adds `<key>LTS_WORKER_COUNT</key><string>1</string>` (default
  value, explicit in the plist so operators see the knob).
- README env-var table — adds `LTS_WORKER_COUNT` row pointing
  to §5.

### 4.5 Tests

`tests/test_worker.py` additions:

- `test_run_forever_with_worker_count_4_processes_concurrent_jobs`
  — submit 8 jobs, run with `worker_count=4`, expect all 8 to
  reach DONE. The deterministic-drain pattern from Phase B6
  applies — count `DONE` jobs after a fixed number of
  `process_one()` calls (or a bounded `wait_for`), not a
  wall-clock sleep.
- `test_concurrent_claim_only_one_worker_wins_per_job` — fire
  10 concurrent `store.claim()` calls against a 5-job queue,
  verify exactly 5 succeed (one per row) and 5 return None.
  Pins the atomic-claim property of the SQL.
- `test_concurrent_mark_processing_respects_lease` — two tasks
  each `claim()` distinct jobs, then both call
  `mark_processing` for the wrong job_id; verify only the
  matching lease succeeds.

`tests/test_store.py` additions:

- `test_concurrent_reclaim_is_safe` — N=20 concurrent
  `reclaim_expired()` calls against a fixture of 10 expired
  leases; verify exactly 10 jobs end up back in QUEUED (the
  rest are no-ops).

`tests/test_config.py` additions:

- `LTS_WORKER_COUNT=0` → `ValidationError` (`ge=1`).
- `LTS_WORKER_COUNT=999` → `ValidationError` (`le=64`).
- Default value is `1`.

## 5. Sub-task D3 — Production hardening

Three independent small items, grouped because they share the
deployment-surface review.

### 5.1 Log rotation

**Current state:** the launchd plist captures stdout/stderr to
`__REPLACE_WITH_LOG_PATH__` (typically `~/Library/Logs/local-transcription-service.log`).
There is no rotation. A long-running install accumulates a
single file forever — at ~200 bytes per `stage_finished` event
plus per-job lifecycle logs, that's roughly 50–100 KB/day for
a moderate user. Within a year: ~30 MB. Not catastrophic, but
untidy and a risk if the disk fills.

**Approach:** use macOS's built-in `newsyslog` (the
`/etc/newsyslog.conf`/`/etc/newsyslog.d/` system). Drop a
config file at `scripts/launchd/local-transcription-service.conf`
(versioned, operator-applies manually) and document the install
step in the existing runbook.

```text
# logfilename                        [owner:group]  mode  count  size  when  flags
/Users/__USER__/Library/Logs/local-transcription-service.log  644  5  10M  $D0  JN
/Users/__USER__/Library/Logs/local-transcription-service.trash-cleanup.log  644  5  10M  $D0  JN
```

- `count=5` — keep 5 rotated files (so 50 MB total ceiling).
- `size=10M` — rotate when the current file crosses 10 MB.
- `when=$D0` — rotate at midnight on any day it crosses the size threshold.
- `flags=JN` — bzip2 the rotated files (`J`), create with the right
  mode if missing (`N`).

**Linux/Jetson target:** equivalent is `logrotate` with a
`/etc/logrotate.d/local-transcription-service` snippet. The
shape is identical; future-HLD note (not implemented now).

### 5.2 Healthcheck-on-start

**Current state:** `launchd` starts the process on user login
(`RunAtLoad=true`). If the STT gateway (`192.168.0.99:4000`)
or `whisper-server` (`127.0.0.1:8779`) is not yet up — common
during boot, especially when launchd fires before the Mac Mini
has fully initialized networking — the first few jobs fail with
`STT_GATEWAY_UNAVAILABLE` and have to wait for the retry.

**Approach:** in `app.main()`, after constructing the engine,
call `await asyncio.wait_for(engine.is_ready(), timeout=5.0)`.
If it returns `False` (or raises), log a `startup_stt_not_ready`
event and call `sys.exit(78)` (sysexits.h `EX_CONFIG`). launchd
will not auto-restart on `EX_CONFIG` (`KeepAlive.Crashed` only
triggers on signal-style exits); the operator sees the log line
and the service stays down until they intervene.

```python
# app.main() — after build_pipeline, before uvicorn.serve():
try:
    if not await asyncio.wait_for(engine.is_ready(), timeout=5.0):
        logger.error(
            "startup aborted: STT engine not ready",
            extra={"event": "startup_stt_not_ready"},
        )
        sys.exit(78)
except (asyncio.TimeoutError, Exception) as exc:  # noqa: BLE001
    logger.error(
        "startup aborted: STT readiness probe failed: %s", exc,
        extra={"event": "startup_stt_not_ready"},
    )
    sys.exit(78)
```

The 5-second budget is the same number
`LiteLLMWhisperSTT.is_ready()` already uses internally for the
`GET /v1/models` short-timeout path (Phase B drift fix, commit
`22d7f04`). Hard exit is the correct posture — silently starting
a half-broken service is worse than no service.

### 5.3 Structured-error rate counter

**Current state:** HLD §15 logs JSON one event per line. There
is no aggregate signal — operators tailing the log see each
event individually but cannot quickly answer "how many
`FETCH_FAILED` in the last hour?".

**Approach:** add an in-process counter that emits a
`error_rate_tick` event every 60 seconds with the per-code
counts since the last tick:

```json
{
  "event": "error_rate_tick",
  "interval_s": 60,
  "counts": {
    "FETCH_FAILED": 3,
    "STT_GATEWAY_UNAVAILABLE": 1,
    "MAX_ATTEMPTS": 0
  }
}
```

Implementation:

- New tiny module `src/local_transcription_service/metrics.py`
  with `class ErrorRateCounter` — thread-safe (asyncio is
  single-threaded but the worker spawns tasks; use a plain
  `dict` + `asyncio.Lock` for cleanliness).
- Hook into `Worker._error_from_exception` (or
  `_handle_pipeline_failure`) — increment on every non-retryable
  outcome; the per-code granularity lets future extensions
  build dashboards from the log feed without code changes.
- Background task spawned in `Worker.run_forever()` alongside
  the claim/reclaim loops; cancelled on `stop()`.

**Why not Prometheus / OpenMetrics:** HLD §15 explicitly says
"no metrics endpoint for MVP". A log-emitted counter keeps
that promise and is enough for a single-user tool — the
operator's existing log-tailing workflow gets a richer feed
without a new endpoint to monitor. If a future phase needs
Prometheus, this counter is the right place to add it (the
metric names line up with the log event names).

### 5.4 Tests

`tests/test_metrics.py`:

- `test_error_rate_tick_emits_per_code_counts` — feed 3
  `FETCH_FAILED` + 1 `STT_GATEWAY_UNAVAILABLE` into the
  counter, advance the tick, assert the JSON event has the
  right shape.
- `test_error_rate_counter_resets_after_tick` — feed, tick,
  assert the next tick starts from zero.

`tests/test_app.py` additions:

- `test_main_exits_78_when_stt_engine_not_ready` — engine
  with `is_ready()=False`, call `main()`, assert
  `SystemExit(78)`.
- `test_main_exits_78_when_stt_probe_raises` — engine with
  `is_ready()` raising, same assertion.
- `test_main_proceeds_when_stt_engine_ready` — happy path.

## 6. Out of scope (explicit)

Same list as Phase C §4, plus:

- **Per-job retention overrides** ("delete this transcript but keep
  that one"). The extension owns the lifecycle via `ack`; selective
  retention is its problem, not the service's.
- **Operator web UI for `trash/`.** Out of scope per HLD §18.
- **TLS** (HLD §18). The trash-cleanup CLI is local-only by
  definition (operator on the Mac Mini); the service endpoints
  stay on LAN.
- **Cross-host deployment** (`local-transcription-service` on
  multiple Macs sharing one queue). HLD §5 single-host stays;
  multi-host needs a shared queue and is an ADR.
- **Anything that would change the extension contract.** The
  service's wire contract (`POST /jobs`, `GET /jobs/{id}`,
  `GET /jobs/{id}/result`, `POST /jobs/{id}/ack`) is frozen at
  the Phase C level. Phase D adds zero new endpoints.

## 7. Acceptance criteria

### D1 — Trash retention

- [ ] `lts-trash-cleanup` console-script + `python -m
    local_transcription_service.retention` both work.
- [ ] TTL knob (`LTS_TRASH_TTL_DAYS`, default 7) deletes files
      older than N days.
- [ ] Size cap knob (`LTS_TRASH_MAX_BYTES`, default 512 MiB)
      trims the dir to the cap, oldest first.
- [ ] `--dry-run` selects the deletion set without `unlink()`.
- [ ] Exit codes 0 / 1 / 2 follow the table in §3.3.
- [ ] `tests/test_retention.py` — at least 6 cases
      (TTL-only, size-only, combined, empty, dry-run, symlink).
- [ ] `scripts/launchd/com.local-transcription-service.trash-cleanup.plist`
      added and documented in the runbook.

### D2 — Multi-worker

- [ ] `LTS_WORKER_COUNT` env var (default `1`, range `1..64`).
- [ ] `Worker.run_forever()` spawns N claim tasks; reclaim
      loop stays single.
- [ ] `config_resolved` log event surfaces `worker_count`.
- [ ] launchd plist + README env-var table mention the knob.
- [ ] `tests/test_worker.py` — at least 3 new cases (concurrent
      jobs, concurrent claim, lease-token respect).
- [ ] `tests/test_store.py` — at least 1 new case
      (concurrent reclaim).
- [ ] `tests/test_config.py` — at least 2 new cases (range
      validation, default).

### D3 — Production hardening

- [ ] `newsyslog` config at
      `scripts/launchd/local-transcription-service.conf` with
      both log paths.
- [ ] Runbook updated with `sudo cp ...` install step.
- [ ] `app.main()` exits 78 if `engine.is_ready()` is False
      or raises within the 5 s budget.
- [ ] `ErrorRateCounter` emits `error_rate_tick` every 60 s
      with per-code counts.
- [ ] `tests/test_metrics.py` — at least 2 new cases.
- [ ] `tests/test_app.py` — at least 3 new cases (exit-78 paths + happy path).

### Cross-cutting

- [ ] `uv run pytest -q` green from the new HEAD; full suite
      reconfirmed post-merge.
- [ ] `uv run ruff check .` clean.
- [ ] No new dependency added via `pip`; `uv add` only if a new
      lib is genuinely required (none of D1/D2/D3 needs one).
- [ ] HLD-001 §5 / §13.2 / §15 / §16 reflect the implementation.
- [ ] CHANGELOG entry added at merge time.
- [ ] No new architectural decision; ADR-012 unchanged.

## 8. Test plan summary

| Surface         | New tests                                                                 | Total (cumulative)   |
| --------------- | ------------------------------------------------------------------------- | -------------------- |
| `retention.py`  | 6 cases (TTL, size, combined, empty, dry-run, symlink) + 1 CLI subprocess | +7 net               |
| `worker.py`     | 3 cases (concurrent jobs, concurrent claim, lease-token respect)          | +3 net               |
| `store.py`      | 1 case (concurrent reclaim)                                               | +1 net               |
| `config.py`     | 2 cases (range validation, default)                                       | +2 net               |
| `metrics.py`    | 2 cases (tick emits counts, counter resets)                               | +2 net               |
| `app.py`        | 3 cases (exit-78 not-ready, exit-78 probe-raised, happy path)             | +3 net               |
| **Phase D net** |                                                                           | **+18 net** (target) |

Phase C baseline: **184 passed**. Phase D target: **202 passed**.
(The number is a target, not a gate — if the actual delta is
196 or 208, that is fine as long as every test is meaningful.
The gate is `pytest -q` green, not a count match.)

## 9. Report-back

This task is single-shot but **wider** than Phase C. The
recommended commit structure (one merge to `main`):

1. **D1a**: `feat(retention): RetentionPolicy + run_cleanup + tests`
   — pure module + tests, no env-var wiring.
2. **D1b**: `chore(ops): lts-trash-cleanup console-script +
trash-cleanup launchd plist` — pyproject.toml + plist +
   runbook note.
3. **D2a**: `feat(worker): LTS_WORKER_COUNT + N concurrent claim tasks`
   — config + worker + tests.
4. **D2b**: `chore(ops): LTS_WORKER_COUNT in plist + README` —
   plist + README env-var row.
5. **D3a**: `feat(metrics): ErrorRateCounter + 60s tick event`.
6. **D3b**: `chore(ops): newsyslog config + install step in runbook`.
7. **D3c**: `feat(app): startup STT-readiness probe → exit 78`.
8. **docs**: `HLD §5 / §13.2 / §15 / §16` amendments +
   `CHANGELOG 2026-07-04` Phase D entry.

If the gate at merge time is unhappy (rare race, count
mismatch, etc.), follow the Phase B6 / Phase C pattern —
slice the offender off into a follow-up commit, do not
amend the merge. The Tech Lead is the final reviewer.

## 10. Open follow-ups after Phase D

These are **not** part of Phase D. Recording so they do not
get lost:

- **Manual smoke on a Mac Mini-reachable host** (Phase B open).
  Real gateway `192.168.0.99:4000`. Extension-side verification
  only; the Phase B `@pytest.mark.integration` test is the
  future gate.
- **`medium` vs `large-v3-turbo` benchmark**
  (`scripts/whisper-macmini/bench-whisper.sh`, HLD §4 open).
  Optional. Swap is a config/wrapper change.
- **Multi-process deployment** (HLD §5 deferred). Phase D's
  in-process `LTS_WORKER_COUNT` covers the throughput case
  for MVP. Multi-process is a future deployment-shape change,
  not a code change.
- **Operator web UI for `trash/`** (HLD §18 out of scope).
- **TLS** (HLD §18 out of scope).
