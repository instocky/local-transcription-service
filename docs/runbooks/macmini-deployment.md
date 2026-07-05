# RUNBOOK — Mac Mini deployment architecture (Phase F, 2026-07-05)

| Field   | Value                                                              |
| ------- | ------------------------------------------------------------------ |
| Target  | Mac Mini (Apple Silicon, macOS), LAN `192.168.0.99`               |
| Runs by | mavis/macOS SSH (`uri@mac-mini-urij.local`)                        |
| Phase   | F — system LaunchDaemon, in-process venv subprocess deps           |

This runbook answers the question **"how is the local transcription stack laid out and managed on the Mac Mini?"**. It is intentionally **separate** from `lts-operations.md`, which keeps being the focused Phase D ops document (trash cleanup, newsyslog, worker count, startup probe). Detailed Phase D procedures stay there; cross-service architecture, ordering, and recovery live here.

The companion file `docs/backlog.md` records open follow-ups that surfaced during the Phase E + F debug session (WARP auto-connect, `chmod 600` on secrets, secret rotation).

---

## 1. Inventory — what runs where on this Mac Mini

| Surface                | Path                                              | Owner        | Lives in                                |
| ---------------------- | ------------------------------------------------- | ------------ | --------------------------------------- |
| LiteLLM gateway        | `/opt/litellm/`                                   | `root:wheel` | `/Library/LaunchDaemons/local.litellm.plist` |
| whisper-server (Metal) | `/opt/whisper/`                                   | `uri`        | `/Library/LaunchDaemons/local.whisper.plist` |
| Local transcription svc| `/opt/local-transcription-service/`               | `uri:staff`  | `/Library/LaunchDaemons/local.local-transcription-service.plist` |
| Service secrets        | `$HOME/.lts-env` + `$HOME/.lts-token`             | `uri`        | home dir (chmod 600 — BLG-002)          |
| Service runtime data   | `$HOME/.local-transcription/` (SQLite WAL, audio cache, results/, trash/) | `uri` | home dir |
| Service logs           | `$HOME/Library/Logs/local-transcription-service.{out,err}.log`, plus rotated `.bz2` generations (`count=5 size=10M`). Newsyslog config at `/etc/newsyslog.d/local-transcription-service.conf`. | `uri` | `~/Library/Logs/`, `/etc/newsyslog.d/` |

All three services share the `local.<name>` plist namespace — installed in `/Library/LaunchDaemons/` (root-owned), running as `UserName=uri`. They are **system** daemons, not user LaunchAgents.

The three plists have the same shape: `Label`, `UserName`, `ProgramArguments` (one shell wrapper), `RunAtLoad`, `KeepAlive`, `WorkingDirectory`, `StandardOutPath`, `StandardErrorPath`. There is no `<key>ThrottleInterval</key>`, no `<key>EnvironmentVariables</key>` dict — env is sourced in the shell wrapper. (See Phase F in `CHANGELOG.md` for the rationale.)

## 2. Boot / shutdown ordering

`launchd` starts `/Library/LaunchDaemons/` plists in **dependency order** (the order registered via `load`/`bootstrap`, configurable via `<key>Requires</key>` / `<key>After</key>` / etc — none of which our three plists currently use).

We rely on the **runtime** dependency instead:

- `local-transcription-service` does a 5 s **STT-readiness probe** at startup (`app._startup_probe`); failure exits `78` and `KeepAlive` does not auto-restart on `EX_CONFIG` (HLD §16.1). Net effect: service stays down if LiteLLM / whisper-server aren't reachable yet.
- `local.litellm` itself depends on **whisper-server being up** (LiteLLM registers `audio_transcription` against the `127.0.0.1:8779` upstream). If whisper-server is not yet bound, LiteLLM may start but `/v1/audio/transcriptions` calls will fail; LiteLLM does not probe-block the way our service does. Practical impact: occasional bootstrap race where the first 1–2 jobs after reboot get `FETCH_FAILED / upstream connection refused` until the worker retries.
- Both `local.litellm` and `local.whisper` use `RunAtLoad=true` and `KeepAlive=true`, so once they're up, they stay up.

**Network dependency** (independent of launchd): `local-transcription-service` calls `yt-dlp` → YouTube. The Mac Mini is on a network where YouTube DNS is blocked at the resolver level (see `note_20260703-install-whisper.cpp.md` and Phase E debugging). The workaround in production is **Cloudflare WARP** enabled via menu-bar GUI — there is **no auto-connect** after reboot yet (BLG-001). Until BLG-001 lands, the operator toggles WARP once per reboot, before any `POST /jobs` lands.

## 3. The shell-wrapper pattern (consistent across all three services)

All three plists invoke a single shell wrapper, not the binary directly. Reasons:

- Plist stays minimal (no `<key>EnvironmentVariables</key>` dict to maintain).
- Wrapper can `set -euo pipefail` and verify preconditions (`which python3`, `mkdir -p` data dirs, etc).
- Wrapper can `exec ...` so launchd sees the final process directly (no extra wrapper PID).

Example — our service's `scripts/launchd/run.sh` (mirrors `/opt/litellm/run.sh` and `/opt/whisper/run.sh`):

```bash
#!/usr/bin/env bash
set -euo pipefail

ENV_FILE="$HOME/.lts-env"
if [ -f "$ENV_FILE" ]; then
  set -a
  . "$ENV_FILE"
  set +a
fi

cd /opt/local-transcription-service
exec ./.venv/bin/python -m local_transcription_service.app
```

`exec` replaces the shell in the process table, so launchd's parent-of-record is the Python process — the standard pattern for plumbing signals and log capture through uvicorn.

## 4. Environment: `$HOME/.lts-env`

The service picks up its environment from `$HOME/.lts-env` (the wrapper's `set -a; . "$ENV_FILE"; set +a`). The file is `key=value` form, **no Python type annotations** (python-dotenv silently drops lines containing `:` per the 2026-06-09 pitfall note).

Current keys (typical content — operator overwrites on secret rotation):

```bash
LTS_AUTH_TOKEN=<shared-secret>
LTS_STT_API_KEY=<litellm-master-key>
LTS_BIND_HOST=192.168.0.99
LTS_PORT=8766
LTS_DATA_DIR=/Users/uri/.local-transcription
LTS_STT_BASE_URL=http://192.168.0.99:4000/v1
LTS_STT_ENGINE=openai
LTS_MODEL=whisper-large-v3-turbo
LTS_WORKER_COUNT=1
PATH=/opt/local-transcription-service/.venv/bin:/Users/uri/Library/Python/3.9/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin
```

Two operational notes:

- `PATH=` is **explicit** (not relying on launchd's sparse system PATH). The first entry is the project venv — required so `_resolve_venv_binary("yt-dlp")` (in `app.py`) finds `Py 3.12 yt-dlp` instead of the user's `Py 3.9 / LibreSSL` yt-dlp on `~/Library/Python/3.9/bin`. See `app.py` docstring + the Phase F / Phase E entries in `CHANGELOG.md`.
- `LTS_AUTH_TOKEN` and `LTS_STT_API_KEY` are **shared secrets** — they bypassed through `mavis` chat during the Phase E session. Treat them as compromised and rotate at the next opportunity (BLG-003).

## 5. Deploy workflow

Edition model: **edit on Windows → push to GitHub → pull on Mac Mini → restart the relevant daemon.** Code never lives directly on the Mac Mini; the only state there that is *not* in the repo is `/opt/litellm/config.yaml`, `$HOME/.lts-env`, `$HOME/.lts-token`, and the SQLite WAL.

```bash
# Windows dev box
cd C:\Projects\_Others\0703_local-transcription-service
# make edits, then:
git add -A
git commit -m "..."
git push origin main

# Mac Mini over SSH
ssh uri@mac-mini-urij.local
cd /opt/local-transcription-service
git config pull.ff only                   # one-time, prevents uv.lock divergence
git pull
uv sync                                    # picks up pyproject.toml changes
chmod +x scripts/launchd/run.sh           # git sometimes strips +x on pull
sudo launchctl kickstart -k system/com.local-transcription-service
```

Optionally: `./uv run ruff check . && uv run pytest` before the commit on Windows (AGENTS.md convention).

## 6. After `sudo shutdown -r now` — what to expect

Acceptance test run during Phase F (2026-07-05):

| Time after reboot      | What should be true                                               |
| ---------------------- | ----------------------------------------------------------------- |
| 0–15 s                 | macOS comes up; no daemons yet                                    |
| ~30 s                  | `local.litellm` running; `local.whisper` running                |
| ~45 s                  | `local.local-transcription-service` running                      |
| ~60 s                  | `curl http://192.168.0.99:8766/health` returns `200 {"status":"ok","version":"0.1.0"}` |
| Operator action        | Toggle **WARP on** in the menu bar (BLG-001 will automate this)  |
| After WARP on          | `POST /jobs` reaches `done` in ~20 s (real YouTube → transcript) |

The `state = running` line will read `last exit code = (never exited)` only on the very first successful boot after plist install; subsequent restarts show the most recent non-zero exit code if there was one.

## 7. Operator diagnostics

```bash
# Are all three services up?
sudo launchctl print system/com.local-transcription-service | grep -E 'state|last exit code|path ='
sudo launchctl print system/local.litellm | grep -E 'state|last exit code|path ='
sudo launchctl print system/local.whisper  | grep -E 'state|last exit code|path ='

# Live tail of our service stdout / stderr
tail -F /Users/uri/Library/Logs/local-transcription-service.out.log
tail -F /Users/uri/Library/Logs/local-transcription-service.err.log

# Force a restart if the service hung (kickstart -k sends SIGTERM)
sudo launchctl kickstart -k system/com.local-transcription-service

# Inspect jobs.db (force a SQL-level read; the file is SQLite WAL)
sqlite3 ~/.local-transcription/jobs.db \
  "SELECT job_id, status, attempt, datetime(created_at), error
   FROM jobs ORDER BY created_at DESC LIMIT 10;"

# YouTube reachability without VPN
curl -sS -o /dev/null -w 'HTTP=%{http_code} time=%{time_total}s\n' \
  --max-time 8 https://www.youtube.com/watch?v=dQw4w9WgXcQ
# HTTP=000 = WARP off; HTTP=200 = WARP on

# Quick end-to-end smoke after a config change
curl -X POST http://192.168.0.99:8766/jobs \
  -H "X-Auth-Token: $LTS_AUTH_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"video_url":"https://www.youtube.com/watch?v=dQw4w9WgXcQ"}'
# poll /jobs/<id> until status==done
```

If `state = running` but `POST /jobs` returns DNS errors — first thing to check is WARP. Second is `ls -la scripts/launchd/run.sh ~/.lts-env` — both must be readable by `uri` and non-empty.

## 8. Troubleshooting matrix

| Symptom                                                            | First check                                                | Likely fix                                                                          |
| ------------------------------------------------------------------ | ---------------------------------------------------------- | ----------------------------------------------------------------------------------- |
| `POST /jobs` → `401`                                              | `~/.lts-env` `LTS_AUTH_TOKEN` matches Postman's header     | Sync values (BLG-003 if rotation pending)                                            |
| `POST /jobs` → `failed / FETCH_FAILED / nodename nor servname ...` | WARP on? `curl --max-time 8 https://www.youtube.com/...`   | Toggle WARP on; long-term: BLG-001                                                    |
| `POST /jobs` → `failed / yt-dlp permanent error ...`              | Check `~/.lts-env` PATH first entry = service `.venv/bin`  | If missing, prepend `/opt/local-transcription-service/.venv/bin:` to `PATH=` in `.lts-env` |
| `state = running` but `lsof -iTCP:8766` empty                       | `~/.lts-env` `LTS_BIND_HOST` is `192.168.0.99` (not `127.0.0.1`) | Set to `192.168.0.99`; restart via `launchctl kickstart -k`                          |
| `last exit code = 78` (no auto-restart)                            | LiteLLM / whisper not up                                    | `sudo launchctl print system/local.litellm` / `local.whisper` — if both up, `kickstart -k` our service to retry probe |
| `bind: address already in use`                                     | Another process holding :8766 (usually a stale `nohup python` from pre-Phase-F deploy) | `pkill -f local_transcription_service.app` then `launchctl kickstart -k`              |
| `Mac Mini is up but `lsof` shows no `192.168.0.99:8766` listener  | Plist stuck in `state = error`                              | `sudo launchctl bootout system/com.local-transcription-service` then `bootstrap` again |

## 9. References

- HLD-001 — `docs/hld/HLD-001-local-transcription-service.md` — what the service is supposed to look like operationally (worker count, startup probe, log rotation).
- ADR-012 — `docs/adr/ADR-012-local-transcription-pipeline.md` — system-level decision (vendored from the extension repo); why we have local STT at all.
- `docs/runbooks/lts-operations.md` — Phase D ops: trash-cleanup launchd job, newsyslog install, multi-worker knob, startup probe verification.
- `docs/changelogs/CHANGELOG.md` — Phase E (deploy) and Phase F (system launchd + venv deps) entries.
- `docs/backlog.md` — Phase G candidates: BLG-001 WARP, BLG-002 chmod 600, BLG-003 secret rotation.
- `AGENTS.md` — root agent contract (uv, ruff, pytest conventions) — applies to both Windows edit and Mac Mini ops sides.
