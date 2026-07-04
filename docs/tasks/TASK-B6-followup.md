# TASK-B6 — Follow-up: flake fix + plist/.gitignore polish

| Field       | Value                                                                       |
|-------------|-----------------------------------------------------------------------------|
| Phase       | B (post-merge follow-up)                                                    |
| Depends on  | TASK-B-real-pipeline (b1–b5 all PASS; b5 verdict = accept with FAIL on item 1) |
| Branch      | `feature/phase-b-real-pipeline` → merged to `main`                          |
| Status      | DONE 2026-07-04 — merged into `main` at HEAD `d492088`; Phase C started         |
| HLD         | HLD-001 §4 (amended), §11, §12                                              |
| ADR         | ADR-012 unchanged (STT stays local)                                         |

## 1. Goal

Two surgical commits on top of `feature/phase-b-real-pipeline @ 87c470a`,
no contract drift:

1. Fix the pytest flake that b5 flagged on item 1.
2. Polish out-of-scope loose ends that the b5 gate surfaced
   (plist env-var contract drift + missing `.mavis/` ignore).

Both items are pre-approved by the Tech Lead (recs `a` and
the .gitignore/plist polish). No new design decisions in this task.

## 2. Why now (carried from b5 verdict)

`b5-integration-gate` was `accept` overall: items 2/4/5/6 PASS, item 3
SKIP per scope. Item 1 — `uv run pytest -q` — flipped between
`142 passed` and `1 failed` (3 of 5 isolated runs of `test_worker.py`
failed, root cause from Phase A `53b2b89`, not a Phase B regression).

The out-of-scope polish items were identified by both the verifier
and the Tech Lead as small loose ends to clean up before the
branch leaves dev:

- `scripts/launchd/com.local-transcription-service.plist` still has
  `LTS_STT_ENGINE=ollama` and `LTS_OLLAMA_BASE_URL=http://127.0.0.1:11434`
  (Phase A contract). b3-config already moved the code to
  `LTS_STT_{BASE_URL,API_KEY,MODEL}` — the plist just didn't get
  touched.
- `.gitignore` is missing `.mavis/` (currently untracked but a future
  `git add .` would pull it in).

## 3. Scope

### 3.1 Commit A — flake fix (`fix(test): deterministic drain for test_run_forever_processes_multiple_jobs`)

**File:** `tests/test_worker.py::test_run_forever_processes_multiple_jobs`
(currently lines 235–250).

**Root cause:** the test schedules `worker.stop()` after
`asyncio.sleep(0.2)` and asserts three SQLite round-trips
(claim → mark_processing → pipeline.transcribe → mark_done) finish
inside that window. `MockPipeline.transcribe` is instantaneous,
so the bottleneck is SQLite write-lock contention on Windows.
200 ms is not a real-time guarantee.

**Fix (rec `a` from coder; Tech Lead-approved):** deterministic drain
via per-job `Event`/`Future`.

Implementation sketch (final shape is coder's call):

- Have the worker (or test fixture) publish a `done_count` event
  each time a job transitions to `DONE`. The test then asserts
  `count == 3` after a bounded timeout, instead of asserting it
  inside a 200 ms `asyncio.sleep` window.
- Concrete shape options to choose between:
  - (a1) inject a `JobStore` callback hook (`on_done`) into the
    worker for tests only; production worker keeps the same shape
    without the callback.
  - (a2) wrap the worker to call `store.count_by_status(DONE)`
    on each `_claim_loop` iteration and signal an `Event` once
    the count reaches the expected value.
  - (a3) use `asyncio.Event` set inside the worker after each
    `mark_done`, gated by a `settings.test_signal_done` flag
    (no-op in prod).

Pick whichever keeps the production code path unchanged. The
test must be deterministic on Windows: 10 consecutive runs of
`uv run pytest tests/test_worker.py::test_run_forever_processes_multiple_jobs -q`
must all PASS with no flake.

**Bounded wait:** use `asyncio.wait_for(..., timeout=5.0)` (already
there) so a regression fails fast rather than hanging.

**Rejected alternatives (Tech Lead decision, do not reopen):**
- `b` — `@pytest.mark.flaky` with reruns. Hides the signal,
  fails CI on flake budget exhaustion, no real fix.
- `c` — bump `asyncio.sleep(0.2)` to `0.5` or `1.0`. Doesn't
  address the root cause; flake rate may shift but won't
  reach zero on a contended SQLite path.

### 3.2 Commit B — polish (`chore(ops): migrate launchd plist to LTS_STT_* + add .mavis/ to .gitignore`)

Two files, one commit.

**`scripts/launchd/com.local-transcription-service.plist`** — replace:

```xml
<key>LTS_STT_ENGINE</key>
<string>ollama</string>
<key>LTS_MODEL</key>
<string>whisper-large-v3-turbo</string>
<key>LTS_OLLAMA_BASE_URL</key>
<string>http://127.0.0.1:11434</string>
```

with:

```xml
<key>LTS_STT_ENGINE</key>
<string>openai</string>
<key>LTS_STT_BASE_URL</key>
<string>http://192.168.0.99:4000/v1</string>
<key>LTS_STT_API_KEY</key>
<string>__REPLACE_WITH_API_KEY__</string>
<key>LTS_MODEL</key>
<string>whisper-large-v3-turbo</string>
```

Keep the `__REPLACE_WITH_*` placeholder convention for the API
key (the same convention already used for `__REPLACE_WITH_USERNAME__`,
`__REPLACE_WITH_REPO_ROOT__`, `__REPLACE_WITH_AUTH_TOKEN__`).

**`.gitignore`** — append (after the existing `models/` block,
keeps the file grouped):

```gitignore
# Tooling-only (Mavis runtime, not part of shipped repo)
.mavis/
```

Note: `audio-cache/` and `__pycache__/` are already in `.gitignore`
(lines 2 and 34) — do not duplicate.

## 4. Out of scope (explicit)

- Adding / changing other tests beyond the one flake fix.
- Refactoring `Worker._claim_loop` shape (just add the test-only
  signal hook if option a1 is picked).
- Real-gateway smoke (192.168.0.99:4000) — still requires Mac
  Mini host; the manual smoke gate stays with Tech Lead.
- Anything beyond `.gitignore` and the plist in commit B.
- No push. No merge to main. Branch stays local until Tech Lead
  reviews and signs off both commits.

## 5. Acceptance criteria

- [ ] Two commits on `feature/phase-b-real-pipeline` on top of
      `87c470a`. Linear history. No merge commits. No force-push.
- [ ] `uv run pytest -q` from the new HEAD PASS in 10 consecutive
      runs (run the full suite each time, not just the one test).
- [ ] The flake test file change is the ONLY diff in
      `tests/test_worker.py`; no other tests touched.
- [ ] `git grep -nE "ollama_base_url|LTS_OLLAMA_BASE_URL"` over
      tracked files returns no hits (plist migration complete).
- [ ] `git check-ignore -v .mavis` exits 0 with the new rule
      matching.
- [ ] `uv run ruff check .` clean.
- [ ] No new dep added; no `requirements.txt`.
- [ ] Production worker code path unchanged — same observable
      behavior for non-test callers.

## 6. Report-back

When done, send a single message to Mavis root
(`mvs_e361560318ef452b8c170996685668bc`) with:

- New HEAD SHA on `feature/phase-b-real-pipeline`.
- Two `git log -1 --format="%s"` lines (commit A and B subjects).
- `uv run pytest -q` PASS count + run-to-run stability note
  (10/10 PASS, or whatever you observed).
- Any deviation from the spec above with the reason.

If blocked, send the blocker — don't self-start fixes outside
this scope and don't touch b4 (`87c470a`) surface.