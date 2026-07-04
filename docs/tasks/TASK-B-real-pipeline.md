# TASK-B — Real transcription pipeline (yt-dlp → ffmpeg → STT)

| Field       | Value                                                                    |
|-------------|--------------------------------------------------------------------------|
| Phase       | B                                                                        |
| Depends on  | Phase A (commit `53b2b89`, HLD-001 skeleton + MockPipeline)              |
| Status      | Ready for dev — B0 gate PASSED, engine locked                           |
| Engine      | whisper.cpp (Metal) on Mac Mini, fronted by LiteLLM (`:4000`)           |
| Target env  | Windows dev box; real STT via Mac Mini LiteLLM over LAN; `mock` for CI  |
| HLD         | HLD-001 §4 (amended 2026-07-03), §6, §8, §11, §12, §13, §15             |
| ADR         | ADR-012 (STT stays local — satisfied; NO ADR change needed)            |

## 1. Goal

Replace `MockPipeline` with a real three-stage pipeline that turns a
YouTube URL into a transcript, wired behind the existing
`TranscriptionPipeline` ABC. No API/wire-contract change — Phase A
already ships the endpoints, queue, worker, retry policy and auth.
Phase B fills in Stage 1–3 only.

Stage 3 talks to the **already-provisioned** whisper.cpp service behind
LiteLLM (`http://192.168.0.99:4000/v1/audio/transcriptions`, OpenAI
multipart). Phase B is developed on Windows: unit tests + CI run against
`stt_engine=mock`; the real STT integration test points at the Mac Mini
gateway over LAN. No STT daemon runs on the Windows box.

## 2. Two-layer contract (MUST NOT collapse)

There are two distinct `transcribe` contracts. Keep them separate.

- **Orchestration layer** — `TranscriptionPipeline.transcribe(video_url) -> str`
  (`pipeline/base.py`, already exists). Owns Stage 1→2→3 sequencing,
  temp-file layout under `audio-cache/`, and raising `PipelineError`
  with the right retry semantics. Writes NOTHING to the job store or
  `results/` — the worker still owns that (see base.py docstring).
- **STT engine layer (Stage 3 only)** — `STTEngine` protocol from
  HLD-001 §4:
  ```python
  class STTEngine(Protocol):
      async def transcribe(self, wav_path: Path, *, language: str | None = None) -> str: ...
      async def is_ready(self) -> bool: ...
  ```
  Implementations: `LiteLLMWhisperSTT` (default), `MockSTT` (dev/test).
  Selected by `LTS_STT_ENGINE` (`openai` | `mock`). A future
  `mlx-whisper` engine is a third value, not needed now.

`WhisperPipeline` = orchestrator that composes the three stages and
delegates Stage 3 to the configured `STTEngine`. `/ready`'s
`stt_model_loaded` check must call `STTEngine.is_ready()` (today
`api/health.py` dispatches inline on `settings.stt_engine` — refactor
it to call the engine so there is one source of truth).

## 3. Gate — B0 spike: DONE ✅ (PASSED 2026-07-03)

The B0 spike falsified the original "ollama-hosted whisper" assumption
and resolved the engine. Findings (empirical, on the Mac Mini):

- ollama 0.31.1 → `POST /api/audio/transcriptions` returns **404**; no
  whisper model runnable (`ollama list` has none); whisper STT is an open
  upstream feature-request (ollama/ollama#8202, #11798). → ollama path REJECTED.
- **Resolution (Tech Lead approved):** whisper.cpp `whisper-server` (Metal)
  on `127.0.0.1:8779` with `--inference-path /v1/audio/transcriptions`,
  registered in the existing LiteLLM Proxy (`:4000`) as an
  `audio_transcription` deployment. HLD-001 §4 amended; ADR-012 unchanged
  (STT stays local).
- **Deployment verified:** Apple M4, Metal backend, `large-v3-turbo`
  (1623.92 MB), launchd-managed, `json` + `verbose_json` smoke green
  direct on `:8779`. Runbook: `docs/runbooks/whisper-macmini-provisioning.md`;
  scripts: `scripts/whisper-macmini/`.
- **Confirmed end-to-end (2026-07-03):** LiteLLM `/v1/models` lists
  `whisper-large-v3-turbo`; gateway smoke `POST :4000/v1/audio/transcriptions`
  (jfk.wav) returns correct text **with** word timestamps / language / duration
  surviving the proxy. Path `client → LiteLLM → whisper.cpp` is green.
- **Open (optional, not blocking):** `medium` vs `large-v3-turbo` benchmark
  (`bench-whisper.sh`) — model swap is a wrapper/config change.

## 4. Tasks

### B1 — Stage 1: media acquisition (yt-dlp)
- Invoke `yt-dlp` via `asyncio.create_subprocess_exec` (no shell).
- Output to `${LTS_DATA_DIR}/audio-cache/{job_id}.{ext}` (HLD §11).
  Pipeline needs the `job_id` — thread it through (extend the ABC
  signature or pass a per-job context; decide in B1, keep base.py
  docstring honest).
- Error mapping (HLD §12): binary missing / non-zero exit → non-retryable
  `PipelineError(code="FETCH_FAILED", retryable=False)`; network error →
  retryable `PipelineError(code="FETCH_FAILED", retryable=True)`.

### B2 — Stage 2: audio conditioning (ffmpeg)
- `ffmpeg` subprocess → 16 kHz mono PCM WAV (HLD §11) — the exact format
  whisper.cpp expects (so the gateway `--convert` never has to re-encode).
- Delete the Stage 1 raw file after the WAV is produced (HLD §11).
- ffmpeg missing / non-zero → non-retryable `PipelineError`.

### B3 — Stage 3: STT engine (`LiteLLMWhisperSTT`)
- `STTEngine` protocol + `LiteLLMWhisperSTT` + `MockSTT`.
- `LiteLLMWhisperSTT.transcribe` POSTs the WAV as OpenAI multipart via
  `httpx.AsyncClient` to `${LTS_STT_BASE_URL}/audio/transcriptions`
  (`http://192.168.0.99:4000/v1`) with `model=${LTS_MODEL}`,
  `Authorization: Bearer ${LTS_STT_API_KEY}`, `response_format=json`;
  read `.text` from the response. Connection refused / 5xx → retryable;
  model not in `GET ${LTS_STT_BASE_URL}/models` → non-retryable
  `PipelineError(code="MODEL_NOT_PULLED", retryable=False)`.
- `is_ready()` = model listed in `GET /models`.

### B4 — WhisperPipeline orchestrator + wiring
- `WhisperPipeline.transcribe(video_url)` runs B1→B2→B3, cleans up temp
  files on both success and failure paths.
- `app.py` / `create_app`: select pipeline + engine from
  `settings.stt_engine` (`mock` still fully supported for tests/CI).
- Refactor `api/health.py` `/ready` to delegate `stt_model_loaded` to
  `STTEngine.is_ready()` instead of the inline dispatch.
- Structured stage logging (HLD §15): `stage_started` / `stage_finished`
  with `job_id`, `stage`, `duration_s`.

## 5. Contract reconciliation (fix drift in this phase)

**5a. Config (`config.py`) — align to the amended HLD §4:**
- `stt_engine: Literal["ollama","mlx-whisper","mock"] = "ollama"` →
  `Literal["openai","mock"] = "openai"`.
- Drop `ollama_base_url`; add `stt_base_url: str = "http://192.168.0.99:4000/v1"`
  (`LTS_STT_BASE_URL`) and `stt_api_key: str` (`LTS_STT_API_KEY`, the LiteLLM
  master key — required when `stt_engine=openai`).
- Update `test_config.py` env-contract tests accordingly.

**5b. Error codes — pick HLD as canonical:**
- HLD §6/§12 use `FETCH_FAILED`, `MODEL_NOT_PULLED`.
- `pipeline/base.py` docstring uses `PIPELINE_TRANSIENT`, `INVALID_URL`,
  `MODEL_MISSING`.
- **Action:** adopt the HLD codes, update the `PipelineError` docstring,
  pin with tests. New code needed → add to HLD §12 first (HLD is source of truth).

## 6. Test plan
- **Unit, no network/binaries:** subprocess and httpx calls mocked.
  Assert argv passed to yt-dlp/ffmpeg, WAV format flags, error-code
  mapping (retryable vs not) for every row in HLD §12.
- **MockSTT path stays green:** the existing 74 tests must not regress;
  `stt_engine=mock` remains the CI default (no gateway in CI).
- **One opt-in integration test** (`@pytest.mark.integration`, skipped by
  default) doing a real short-video end-to-end against the Mac Mini LiteLLM
  gateway (`192.168.0.99:4000`, needs `LTS_STT_API_KEY`).
- `ruff check .` clean; `uv run pytest` green.

## 7. Out of scope (explicit)
- Mac Mini launchd wiring is DONE (see runbook); production hardening
  (logrotate, healthcheck-on-start) → Phase C polish.
- Real `mlx-whisper` engine (drop-in alt) → future, only if whisper.cpp
  latency/quality proves insufficient.
- Result trash/retention policy + `GET /jobs/{id}/ack` (HLD O-4) → separate task.
- Concurrent workers, diarization, translation, cloud STT → ADR-012 out of scope.

## 8. Acceptance criteria
- [x] B0 spike done, engine locked (whisper.cpp behind LiteLLM), HLD §4 amended.
- [ ] `TranscriptionPipeline` and `STTEngine` remain two separate layers.
- [ ] yt-dlp/ffmpeg invoked without a shell; temp files cleaned on all paths.
- [ ] Every HLD §12 failure row maps to the correct retryable/non-retryable code.
- [ ] `config.py` migrated to `LTS_STT_BASE_URL` + `LTS_STT_API_KEY` + `LTS_STT_ENGINE=openai`.
- [ ] `/ready` `stt_model_loaded` delegates to `STTEngine.is_ready()` (`GET /v1/models`).
- [ ] Error codes reconciled to the HLD set; base.py docstring updated.
- [ ] `stt_engine=mock` still passes all Phase A tests in CI.
- [ ] No new dependency added except via `uv add` (yt-dlp, httpx if needed); no `requirements.txt`.

---

## 9. Phase B integration report (2026-07-03, `b5-integration-gate`)

Verifier session `mvs_539aa4b221a141ffa0dd54908211a5c5` ran the
six-check integration gate against
`feature/phase-b-real-pipeline @ 87c470a`. Verdict is below each
check; full evidence is in the verifier deliverable
`plan_e233ba8d/outputs/b5-integration-gate/deliverable.md`.

| # | Check                                              | Result          |
|---|----------------------------------------------------|-----------------|
| 1 | `uv run pytest -q`                                 | **FAIL** (flake)|
| 2 | `uv run ruff check .`                              | PASS            |
| 3 | Smoke through real gateway (192.168.0.99:4000)     | **SKIP** (env)  |
| 4 | Secret-scan tracked files (`sk-[a-z0-9]{20,}`)     | PASS            |
| 5 | Branch scope: 3–5 commits covering b1–b4           | PASS            |
| 6 | `.mavis/plans/` not committed                      | PASS            |

### 9.1 Pytest (FAIL)

First run reported **142 passed**, but that result was non-
deterministic. Re-running the full suite flipped to
**1 failed, 141 passed** (same shell, same code, same venv).
The culprit is one and only one test:
`tests/test_worker.py::test_run_forever_processes_multiple_jobs`
(line 235). Isolating the file and running it five times in a
row reproduced the non-determinism:

| Run | Result | Detail                    |
|-----|--------|---------------------------|
| 1   | PASS   | done in 0.52s             |
| 2   | FAIL   | `assert 1 == 3`           |
| 3   | FAIL   | `assert 2 == 3`           |
| 4   | PASS   | done in 0.34s             |
| 5   | FAIL   | `assert 2 == 3`           |

3 of 5 runs failed (60% flake rate). Producer's `142/142
PASS` claim was a coincidence of timing, not a verified
property.

**Root cause (read-only inspection only — no fix
applied):** the test schedules
`worker.stop()` after `asyncio.sleep(0.2)` and asserts three
SQLite round-trips (claim → mark_processing →
pipeline.transcribe → mark_done) finish inside that window.
`MockPipeline.transcribe` is instantaneous, so the bottleneck
is the SQLite write-lock contention on Windows. The 200 ms
budget is not a real-time guarantee and the assertion is
wrong.

**Provenance:** the test was introduced in Phase A
(`53b2b89 feat: complete Phase A of local-transcription-service
per HLD-001`) — `git log -S test_run_forever_processes_multiple_jobs`
returns only that commit. Diffing `git log main..HEAD --
tests/test_worker.py` shows Phase B touched only lines 30–31,
52, 76, 101, 125–127, 158 (signature + `stt_engine="mock"` +
error-code reconciliation to HLD set). The flaky test
definition at line 235 was **not** modified by B1–B4. So
this is a **pre-existing flake**, not a Phase B regression
in the strict sense — but the gate requires `uv run pytest
-q` to be reliable, and it is not.

**Required to unblock (out of scope for this gate; sent back
to the producer/test-writing role):**
- Replace the 0.2 s sleep with a deterministic drain: assert
  `store.count_by_status(JobStatus.DONE) >= 1` after a fixed
  number of `worker.process_one()` calls, or use
  `asyncio.sleep_for_termination` polling on the done count.
- Pin Windows-aware timing tolerance, or add `@pytest.mark.flaky`
  with explicit reruns if the design genuinely relies on
  real-time guarantees.
- Whatever the shape — the test as written is unshippable.

### 9.2 Ruff (PASS)

```
$ uv run ruff check .
All checks passed!
```

Exit 0, zero violations. (pyproject.toml selects E, F, I, B,
UP, ASYNC; line-length 100.)

### 9.3 Real-gateway smoke (SKIP)

The Windows verifier runner cannot reach
`192.168.0.99:4000`:
- TCP `Test-NetConnection -ComputerName 192.168.0.99 -Port 4000`
  hung past 15 s with no response (multiple independent
  attempts; each required explicit outbound-network
  permission from the runner's permission layer).
- `$env:LTS_STT_API_KEY` is empty in this session; a `jfk.wav`
  is not present locally either.

Per the gate instructions ("If the mac is unreachable, mark
this as SKIP (with reason) and do not FAIL the whole gate on
connectivity alone"), this is recorded as **SKIP** rather than
FAIL. The Phase B opt-in integration test for this gateway is
described in §6 (skipped by default in CI), so the contract
is honoured; the Tech-Lead should run the manual smoke on a
machine that can reach the Mac Mini before sign-off.

### 9.4 Secrets scan (PASS)

```
$ git grep -nE "sk-[a-z0-9]{20,}"
(no output, exit 1 — grep convention)

$ git ls-files | grep -F 'sk-'   # sanity check, non-regex
docs/tasks/TASK-B-real-pipeline.md  # substring only — content is comment text
```

Confirmed no API key in tracked files. (The single hit in the
sanity grep is a non-secret mention in this very task doc;
the regex grep returns nothing.)

### 9.5 Branch scope (PASS)

```
$ git log main..HEAD --oneline
e7c8e5f feat(stt): add STTEngine protocol, LiteLLMWhisperSTT + MockSTT (B3)
b196678 feat(pipeline): implement Stage 1 (yt-dlp) + Stage 2 (ffmpeg) + RealPipeline orchestrator (B1+B2)
d7ccd3f feat(config): migrate to LTS_STT_BASE_URL / LTS_STT_API_KEY (B5a)
8c3671a fix(health): align /ready STT probe with the openai / mock contract
87c470a ﻿feat(pipeline): wire WhisperPipeline + DI + /ready engine.is_ready() dispatch (B4)
```

5 commits on top of `main`. Parent linkage is a clean
linear chain (`git log main..HEAD --format="%H %p %s"`
shows each commit's parent == prior commit). b1+b2 are
combined into `b196678`; b3, b5a (config), health-fix,
and b4 round out the branch. No reverts, no merges, no
force-push evidence in reflog (the `reset: moving to HEAD`
entries are local no-ops that preserve history).

`git ls-remote origin feature/phase-b-real-pipeline`
returns nothing — the branch has not been pushed yet. Local
reflog shows clean commits only; no history rewrite. (Once
pushed, `git log origin/main..HEAD` is the authoritative
check.)

### 9.6 `.mavis/plans/` not committed (PASS)

```
$ git ls-files .mavis
(no output)

$ git ls-files .mavis/plans
(no output)

$ git ls-files | grep -F '.mavis/plans'
(no output)
```

`.mavis/` is untracked in the working tree and not part of
any commit. `.gitignore` does not list it explicitly, but
the verifier confirms it is excluded by virtue of being
untracked. It would be cleaner to add `.mavis/` to
`.gitignore` so a future `git add .` doesn't accidentally
pull it in, but that's hygiene not a gate failure.

### 9.7 Verdict

**VERDICT: FAIL** on gate item 1 (pytest). Gate items 2, 4,
5, 6 are clean. Gate item 3 is SKIP per scope. The flake
predates Phase B (Phase A, commit 53b2b89), so this is not
a Phase B regression in the strict sense — but
`uv run pytest -q` is the literal gate command and it does
not run reliably on the verifier's Windows runner, so the
gate cannot be declared PASS until either the flake is fixed
or its flakiness is acknowledged and contained (e.g.
flaky-mark + reruns, or a deterministic rewrite of the
test body).

**Action requested:** hand the flake back to the producer
(or a dedicated test-writing producer) for fix; do not
merge until a clean `uv run pytest -q` run is reproducible.
