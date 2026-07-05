# Local Transcription Service

Companion service for the **YT Transcript Copier** Chrome extension
(`../20260404_ytt`). Performs local speech-to-text inference on a
persistently available compute node (currently Mac Mini, Apple Silicon).

## System context

This service implements the architectural decision documented in:

- **ADR-012 - Local Transcription Pipeline** (system-level ADR,
  vendored from the extension repo into this repo):
  [`docs/adr/ADR-012-local-transcription-pipeline.md`](docs/adr/ADR-012-local-transcription-pipeline.md)

Operational design (worker count, queue tech, retry, lifecycle, etc.)
for this service is documented in [`docs/hld/`](docs/hld/).
**The HLD is the source of truth for the operator-facing contract
(env var names, response shapes, retry policy).** If something here
disagrees with HLD-001, the HLD wins and this file is wrong.

## Scope of this repo

- FastAPI HTTP service exposing a job API.
- Local job queue and persistent state (SQLite, lease-based).
- Background worker that drains the queue.
- Auth via shared `X-Auth-Token` header.

Out of scope: the Chrome extension itself, the system-level
architectural decision (see ADR-012), and any cloud-based STT
alternative.

## Quickstart

> Requires `uv` (https://docs.astral.sh/uv/) and Python 3.12.

The service binds to `192.168.0.99:8766` by default (the Mac Mini's
LAN IP per HLD-001 §14). Override `LTS_BIND_HOST` for loopback-only
or other LAN addresses.

```powershell
# Minimum required
$env:LTS_AUTH_TOKEN = "change-me-please-1234567890"

# Optional overrides (env var names per HLD-001 §4 / §14)
$env:LTS_BIND_HOST        = "127.0.0.1"        # default 192.168.0.99
$env:LTS_PORT             = "8766"             # default 8766
$env:LTS_DATA_DIR         = "$HOME\.local-transcription"
$env:LTS_STT_ENGINE       = "openai"           # or "mock" (CI / offline)
$env:LTS_STT_BASE_URL     = "http://192.168.0.99:4000/v1"  # LiteLLM gateway
$env:LTS_STT_API_KEY      = "<your-litellm-master-key>"    # required when LTS_STT_ENGINE=openai
$env:LTS_MODEL            = "whisper-large-v3-turbo"
```

```bash
uv sync
uv run local-transcription-service
```

The service starts both the HTTP server and the background worker in
the same process.

## API surface (current — HLD-001 §6, §13.1)

All routes below require the `X-Auth-Token` header (set in
`LTS_AUTH_TOKEN`), except `/health` and `/ready` which are public
probes (HLD-001 §14).

| Method | Path                       | Auth | Status          | Purpose                                  |
| ------ | -------------------------- | ---- | --------------- | ---------------------------------------- |
| GET    | `/health`                  | no   | 200             | Liveness probe.                          |
| GET    | `/ready`                   | no   | 200 / 503       | Readiness probe (db writable + ffmpeg + STT model). |
| POST   | `/jobs`                    | yes  | 202             | Submit a YouTube URL for transcription.  |
| GET    | `/jobs/{job_id}`           | yes  | 200 / 404       | Poll job state. Includes `transcript` + `transcript_path` for DONE. |
| GET    | `/jobs/{job_id}/result`    | yes  | 200 / 404 / 410 / 500 | Stream the finished transcript file. |
| POST   | `/jobs/{job_id}/ack`       | yes  | 200 / 401 / 404 / 409 | Acknowledge a successful download; moves the transcript to `trash/` (HLD §13.1). Idempotent. |

Submit a job:

```bash
curl -X POST http://192.168.0.99:8766/jobs \
  -H "X-Auth-Token: $LTS_AUTH_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"video_url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"}'
# 202 Accepted
# {"job_id":"...","status":"queued","poll_url":"/jobs/..."}
```

Poll until `status == "done"`:

```bash
curl http://192.168.0.99:8766/jobs/$JOB_ID -H "X-Auth-Token: $LTS_AUTH_TOKEN"
```

## Deploying to the Mac Mini (Phase F, 2026-07-05)

The service runs under launchd on the Mac Mini (Apple Silicon,
LAN `192.168.0.99`) as a **system** LaunchDaemon — the same shape as
the sibling `/opt/litellm` and `/opt/whisper` services. **No background
process is left over from `uv run`.** Once installed, `sudo shutdown
-r now` brings the service back automatically.

> **Full architecture + recovery runbook:**
> [`docs/runbooks/macmini-deployment.md`](docs/runbooks/macmini-deployment.md) — read this first if you need to recover from a reboot, rotate secrets, or restart the service after a config change.
>
> **Phase D ops (trash cleanup, newsyslog, multi-worker, probe):**
> [`docs/runbooks/lts-operations.md`](docs/runbooks/lts-operations.md).

### One-time install on the Mac Mini (SSH)

```bash
ssh uri@mac-mini-urij.local

cd /opt/local-transcription-service && {
  git config pull.ff only    # one-time, prevents uv.lock divergence
  git pull

  uv sync                    # picks up the runtime deps (httpx, yt-dlp)
  chmod +x scripts/launchd/run.sh

  sudo cp scripts/launchd/local.local-transcription-service.plist /Library/LaunchDaemons/
  sudo chown root:wheel /Library/LaunchDaemons/local.local-transcription-service.plist
  sudo chmod 644 /Library/LaunchDaemons/local.local-transcription-service.plist

  sudo launchctl bootstrap system /Library/LaunchDaemons/local.local-transcription-service.plist
}
```

`run.sh` reads `$HOME/.lts-env` for the runtime secret/env.
First-time setup of that file:

```bash
cat > $HOME/.lts-env <<'EOF'
LTS_AUTH_TOKEN=<shared-secret>           # 16+ chars; rotated separately, see BLG-003
LTS_STT_API_KEY=<litellm-master-key>     # rotated separately, see BLG-003
LTS_BIND_HOST=192.168.0.99
LTS_PORT=8766
LTS_DATA_DIR=/Users/uri/.local-transcription
LTS_STT_BASE_URL=http://192.168.0.99:4000/v1
LTS_STT_ENGINE=openai
LTS_MODEL=whisper-large-v3-turbo
LTS_WORKER_COUNT=1
PATH=/opt/local-transcription-service/.venv/bin:$PATH
EOF
chmod 600 $HOME/.lts-env                 # see BLG-002
```

### Day-to-day ops

```bash
# Tail the service log
tail -F /Users/uri/Library/Logs/local-transcription-service.out.log

# Restart the service after a config / code change
sudo launchctl kickstart -k system/com.local-transcription-service

# Health probe (also tells the extension "ready to submit")
curl http://192.168.0.99:8766/health
```

### Production prerequisites

- `Cloudflare WARP` toggled on in the menu bar. There is **no auto-connect
  on reboot** yet — see `BLG-001` in
  [`docs/backlog.md`](docs/backlog.md). Until BLG-001 lands, you must
  re-enable WARP after every `sudo shutdown -r now`.
- `local.litellm` and `local.whisper` LaunchDaemons already up (they are
  the same pattern as ours — installed and managed by the operator).

## Configuration

| Env var                | Default                  | HLD ref     | Notes |
| ---------------------- | ------------------------ | ----------- | ----- |
| `LTS_AUTH_TOKEN`       | *(required, ≥16 chars)*  | §14         | Shared secret, sent in `X-Auth-Token` header. |
| `LTS_BIND_HOST`        | `192.168.0.99`           | §14         | LAN IP of the Mac Mini. |
| `LTS_PORT`             | `8766`                   | §14         | TCP port. |
| `LTS_DATA_DIR`         | `~/.local-transcription` | §7, §13     | Holds `jobs.db`, `audio-cache/`, `results/`, `trash/`. |
| `LTS_STT_ENGINE`       | `openai`                 | §4 amended  | `openai` (LiteLLM/whisper.cpp, default) or `mock` (CI / offline). |
| `LTS_STT_BASE_URL`     | `http://192.168.0.99:4000/v1` | §4    | OpenAI-compatible endpoint exposed by LiteLLM. Stage 3 POSTs `${LTS_STT_BASE_URL}/audio/transcriptions`. |
| `LTS_STT_API_KEY`      | *(required when `LTS_STT_ENGINE=openai`)* | §4 | Bearer token sent on every STT call. Empty when `LTS_STT_ENGINE=mock`. |
| `LTS_MODEL`            | `whisper-large-v3-turbo` | §4          | Model name passed to the STT engine. |
| `LTS_LEASE_TTL_SECONDS`| `600`                    | §8          | Worker lease before reclaim. |
| `LTS_RECLAIM_INTERVAL_SECONDS` | `30`             | §8          | How often the reclaim loop runs. |
| `LTS_MAX_ATTEMPTS`     | `2`                      | §10         | Max processing attempts per job. |
| `LTS_RETRY_BACKOFF_SECONDS` | `30`                 | §10         | Delay between retry attempts for retryable failures. |
| `LTS_WORKER_COUNT`     | `1`                      | §5          | Number of concurrent claim loops in the same process (Phase D). Range `1..64`. SQLite write-lock is the ceiling — past the P-core count (4 on an M4 Mac Mini) yields diminishing returns. |

## Status

**Phase A complete** (HLD-001 implementation, Windows + mock pipeline):

- ✅ `/health`, `/ready` (HLD §8) — with engine dispatch (`openai` / `mock`).
- ✅ `POST /jobs` (202 + `poll_url`) and `GET /jobs/{id}` (with `transcript` + `transcript_path`).
- ✅ `GET /jobs/{id}/result` (text/plain stream).
- ✅ `X-Auth-Token` auth (timing-safe compare, `WWW-Authenticate: Token` on 401).
- ✅ SQLite queue with lease-based single-flight claim, stale-worker protection.
- ✅ Retry policy (HLD §10): `defer_retry` + `next_retry_at`, 30s backoff, `max_attempts=2`.
- ✅ Background worker (`claim` + `reclaim` loops in the same event loop as uvicorn).

**Phase B complete** (real pipeline, on top of Phase A):

- ✅ Real pipeline: Stage 1 `yt-dlp` → Stage 2 `ffmpeg` (16 kHz mono WAV) → Stage 3
  `LiteLLMWhisperSTT` (OpenAI multipart to LiteLLM). See
  `docs/tasks/TASK-B-real-pipeline.md` for the per-stage spec.
- ✅ Config migration: `LTS_OLLAMA_BASE_URL` → `LTS_STT_BASE_URL` + `LTS_STT_API_KEY`,
  `LTS_STT_ENGINE=openai` (B5a).
- ✅ Drift cleanup + B6 follow-up — flake fix on `test_run_forever_processes_multiple_jobs`,
  plist migration to `LTS_STT_*`, `.mavis/` added to `.gitignore`, transcript
  extension `.txt` → `.md` aligned with HLD §11/§13. See
  `docs/changelogs/CHANGELOG.md` (2026-07-04 entry).
- 153 tests passing (Phase B baseline), `ruff check` clean.

**Phase C complete** (HLD-001 §13.1 — closes O-4):

- ✅ `POST /jobs/{job_id}/ack` — idempotent, sets `acked_at`, moves the transcript
  from `results/` to `trash/`. FS move is re-attempted on each call when the file
  isn't already in trash; auto-discovery in the move helper heals a stale DB path
  after a partial `update_transcript_path` failure on a prior call. DB failures
  surface as `503 DB_UNAVAILABLE`.
- ✅ `GET /jobs/{id}` now exposes `acked_at` so the extension can confirm download
  acknowledgement from a poll cycle alone. Pinned by
  `test_get_job_after_ack_includes_acked_at_and_new_path`.
- 184 tests passing (Phase B 153 + Phase C net +31, per pytest run 2026-07-04).
  `ruff check` clean.
  See `docs/changelogs/CHANGELOG.md` (2026-07-04 Phase C entry) for the
  per-surface breakdown.
- See `docs/tasks/TASK-C-ack-and-retention.md` for the task spec + acceptance
  criteria; status flipped to **DONE** at HEAD `150c43d`.

**Phase D complete** (HLD-001 §5 / §13.2 / §15 / §16 — operational hardening):

- ✅ **D1 — Trash retention.** `lts-trash-cleanup` CLI (`python -m
  local_transcription_service.retention`) deletes files from `trash/`
  older than `LTS_TRASH_TTL_DAYS` (default `7`) or until the dir fits
  under `LTS_TRASH_MAX_BYTES` (default `512 MiB`). Daily tick at 04:00
  local via `scripts/launchd/com.local-transcription-service.trash-cleanup.plist`
  (`StartCalendarInterval`, `RunAtLoad=false`). `--dry-run` for operator
  preview. Pinned by 27 cases in `tests/test_retention.py`.
- ✅ **D2 — Multi-worker.** New env var `LTS_WORKER_COUNT` (default `1`,
  range `1..64`). Worker spawns N cooperative claim tasks in the same
  event loop (each tagged with `worker_id=f"w{i}"` in structured logs).
  Reclaim loop stays single — it is already idempotent. SQLite
  `PRAGMA busy_timeout=5000` set on every connection so `/ready`
  waits on the write lock instead of failing fast. Race-condition audit
  in HLD §5.2 — every existing UPDATE is already safe under
  `LTS_WORKER_COUNT > 1`.
- ✅ **D3 — Production hardening.**
  - `metrics.ErrorRateCounter` emits `error_rate_tick` every 60 s
    with per-code counts (HLD §15.1; no Prometheus endpoint, the
    log feed is the dashboard).
  - `app.main()` runs a startup STT-readiness probe (`is_ready()`
    under 5 s); failure exits `78` (`EX_CONFIG`) so launchd does
    not auto-restart on a broken dependency (HLD §16.1).
  - `scripts/launchd/local-transcription-service.conf` — `newsyslog`
    config rotates both log files at 10 MiB / day, keep 5
    generations, bzip2'd (HLD §16.2).
- 232 tests passing (Phase C 184 + Phase D net +48 retention / metrics / worker / config / app), `ruff check` clean.
- See `docs/tasks/TASK-D-trash-retention-multi-worker-hardening.md`
  for the task spec + acceptance criteria.

### Phase E + F complete (Mac Mini LAN deploy, 2026-07-05)

- ✅ **E — Deploy on the Mac Mini.** `local-transcription-service` is
  reachable from a Windows host on the LAN at
  `http://192.168.0.99:8766`. Real-gateway e2e: `POST /jobs` for
  `dQw4w9WgXcQ` reaches `done` in ~18 s, full transcript retrieved,
  `POST /jobs/{id}/ack` clears `acked_at` and moves the transcript to
  `trash/`. This closes the "manual smoke on a Mac Mini-reachable
  host" gate that was SKIP during the Phase B integration run.
- ✅ **F — System LaunchDaemon, venv-pinned subprocess.** The service
  now runs under `/Library/LaunchDaemons/local.local-transcription-service.plist`
  (mirror of `local.litellm.plist` / `local.whisper.plist`) — same
  install pattern as the sibling services. Env vars moved out of the
  plist into the wrapper's source (`run.sh` reads `$HOME/.lts-env`).
  `app.py` hardcodes the yt-dlp path to `.venv/bin/yt-dlp` so the
  bare `fetch_media(... ytdlp_bin="yt-dlp")` lookup can't accidentally
  hit the user's `~/Library/Python/3.9/bin/yt-dlp` (LibreSSL → urllib3
  modern-TLS fail). `pyproject.toml` now lists `httpx` and `yt-dlp` as
  runtime deps (no longer only in dev). See
  `docs/changelogs/CHANGELOG.md` (2026-07-05 entries) for the
  per-surface breakdown, and `docs/runbooks/macmini-deployment.md`
  for the full architecture + recovery runbook.

### Open follow-ups (not blocking MVP)

- `medium` vs `large-v3-turbo` benchmark (`scripts/whisper-macmini/bench-whisper.sh`)
  — optional, kept open from Phase B (HLD §4 — `medium` is also downloaded; swap
  is a config/wrapper change).
- Carry-overs from the Phase E + F session — see
  [`docs/backlog.md`](docs/backlog.md): **BLG-001** WARP auto-connect
  on reboot, **BLG-002** `chmod 600` on `$HOME/.lts-{env,token}`,
  **BLG-003** rotate the `LTS_AUTH_TOKEN` and `LTS_STT_API_KEY` values
  that were pasted into the mavis chat transcript during Phase E.

**STT engine** (HLD §4 amended 2026-07-03): whisper.cpp (Metal) on the Mac
Mini, fronted by the existing LiteLLM Proxy (`:4000`) via OpenAI
`/v1/audio/transcriptions`. The ollama path was rejected (no whisper STT
endpoint). whisper-server is provisioned and live on `127.0.0.1:8779`
(launchd, Apple M4, `large-v3-turbo`). See
`docs/runbooks/whisper-macmini-provisioning.md` and `scripts/whisper-macmini/`.

**Phase B integration gate result (b5, 2026-07-03):** items 2/4/5/6 PASS,
item 1 (`pytest`) FAIL on a pre-existing Phase A flake (now fixed in B6),
item 3 (real-gateway smoke) **SKIP** — the Windows runner cannot reach
`192.168.0.99:4000`. Manual smoke on a Mac Mini-reachable host is the
last outstanding verification gate (extension-side, not blocking merge).

## Requirements

- Python 3.12
- `ffmpeg` on `$PATH` (Stage 2 audio conditioning, plus `/ready` probe)
- For STT: reachable LiteLLM gateway (`http://192.168.0.99:4000`) with the
  whisper.cpp `audio_transcription` deployment registered; `LTS_STT_API_KEY`
  set to the LiteLLM master key. CI uses `stt_engine=mock` (no gateway needed).
