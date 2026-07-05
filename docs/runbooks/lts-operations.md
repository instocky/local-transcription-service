# RUNBOOK — Local Transcription Service: post-install ops (Phase D, 2026-07-04; updated Phase F 2026-07-05)

| Field   | Value                                                              |
| ------- | ------------------------------------------------------------------ |
| Target  | Mac Mini, Apple Silicon, macOS — LAN `192.168.0.99`                |
| Runs by | operator over SSH (this doc = copy-paste commands)                 |

Covers the install steps for the **operational** components added in
Phase D. The main service plist
(`local.local-transcription-service.plist`, system LaunchDaemon in
`/Library/LaunchDaemons/`) is installed in `macmini-deployment.md` —
this runbook is for the additional launchd jobs, log rotation, and
the worker-count knob.

> **Preferred execution: run scripts, do NOT paste Markdown line-by-line.**
> Interactive zsh on macOS mangles inline `#` and em-dashes; heredoc
> delivery (`cat > x.sh <<'EOF' ... EOF`) is the safe path. Use bash
> to exec the resulting script. ASCII only.

---

## Step 1 — Trash cleanup launchd job (HLD-001 §13.2)

The cleanup CLI (`lts-trash-cleanup`) runs once a day at 04:00 local
and deletes acked transcripts older than `LTS_TRASH_TTL_DAYS` (default
7) or until `trash/` fits under `LTS_TRASH_MAX_BYTES` (default 512 MiB).

Substitute the placeholders before installing:

```bash
setopt interactive_comments
PLIST=scripts/launchd/com.local-transcription-service.trash-cleanup.plist
DEST=~/Library/LaunchAgents/com.local-transcription-service.trash-cleanup.plist

# __REPLACE_WITH_*__ substitutions
sed -i '' \
  -e "s|__REPLACE_WITH_USERNAME__|$(whoami)|g" \
  -e "s|__REPLACE_WITH_REPO_ROOT__|$HOME/path/to/local-transcription-service|g" \
  -e "s|__REPLACE_WITH_DATA_DIR__|$HOME/.local-transcription|g" \
  -e "s|__REPLACE_WITH_LOG_PATH__|$HOME/Library/Logs/local-transcription-service.trash-cleanup.log|g" \
  "$PLIST"

# Install (idempotent: bootout first, then bootstrap)
launchctl bootout gui/$(id -u)/com.local-transcription-service.trash-cleanup 2>/dev/null || true
cp "$PLIST" "$DEST"
launchctl bootstrap gui/$(id -u) "$DEST"
launchctl enable  gui/$(id -u)/com.local-transcription-service.trash-cleanup
launchctl kickstart -k gui/$(id -u)/com.local-transcription-service.trash-cleanup

# Verify it actually got registered
launchctl print gui/$(id -u)/com.local-transcription-service.trash-cleanup | grep -E "state|next run"

# Dry-run a manual cleanup (operator escape hatch)
LTS_DATA_DIR=$HOME/.local-transcription lts-trash-cleanup --dry-run
```

The next-run time should be `2026-XX-XX 04:00:00 ...` (the next 04:00
local). If you see a `last exit code: 78`, the env-var path resolution
failed — check the substituted plist with `plutil -lint "$DEST"` and
`plutil -p "$DEST" | head -30`.

## Step 2 — Log rotation via newsyslog (HLD-001 §16.2)

Three logs are captured by launchd (Phase F split the service log
into stdout + stderr):

- `~/Library/Logs/local-transcription-service.out.log` — service stdout (uvicorn + uvicorn access lines + `config_resolved` + `error_rate_tick`).
- `~/Library/Logs/local-transcription-service.err.log` — service stderr (operator warnings + the very rare crashbacktrace).
- `~/Library/Logs/local-transcription-service.trash-cleanup.log` —
  the daily cleanup CLI.

All three rotate at 10 MB / day, keep 5 generations, bzip2'd:

```bash
setopt interactive_comments
sudo cp scripts/launchd/local-transcription-service.conf /etc/newsyslog.d/
sudo cat /etc/newsyslog.d/local-transcription-service.conf
# Should show /Users/<you>/Library/Logs/local-transcription-service.{out,err,trash-cleanup}.log
# with count=5 size=10M when=$D0 flags=JN.
```

No `__USER__` substitution needed — Phase F removed that placeholder
(see `scripts/launchd/local-transcription-service.conf` after commit
`c623e68`).

`newsyslog` runs from `launchd`'s `com.apple.periodic` job; no restart
needed. Force a rotation test:

```bash
sudo newsyslog -v
ls -lh /Users/uri/Library/Logs/local-transcription-service.{out,err}.log*
```

You should see `local-transcription-service.log.0.bz2` (the rotated
+ bzip2'd predecessor) alongside the live `.log`.

## Step 3 — Multi-worker (HLD-001 §5)

The default `LTS_WORKER_COUNT=1` matches single-worker behaviour and
needs no action. To increase, edit `$HOME/.lts-env` (NOT the plist —
Phase F moved env vars out of the plist into the wrapper's source):

```bash
setopt interactive_comments
ENV_FILE=$HOME/.lts-env
# Read current value (or 1 if not set)
CURRENT=$(grep -E '^LTS_WORKER_COUNT=' "$ENV_FILE" | cut -d= -f2)
echo "current LTS_WORKER_COUNT=${CURRENT:-unset}"

# Set to 4 (M4 Mac Mini: 4 P + 6 E cores → 4 claim tasks is sane)
if grep -q '^LTS_WORKER_COUNT=' "$ENV_FILE"; then
  sed -i '' 's|^LTS_WORKER_COUNT=.*|LTS_WORKER_COUNT=4|' "$ENV_FILE"
else
  printf '\nLTS_WORKER_COUNT=4\n' >> "$ENV_FILE"
fi

# Restart the service so it picks the new value up
sudo launchctl kickstart -k system/com.local-transcription-service

# Verify in the startup log line (logs are .out.log / .err.log since Phase F)
grep config_resolved /Users/uri/Library/Logs/local-transcription-service.out.log | tail -1
# expect: ... "worker_count": 4 ...
```

Constraint: `1 ≤ LTS_WORKER_COUNT ≤ 64` (HLD §5 / pydantic field
range). The service exits `78` (EX_CONFIG) at startup if the value
is out of range — `tail -f` the log to confirm.

## Step 4 — Healthcheck-on-start verification (HLD-001 §16.1)

The service now exits `78` if the STT engine is not reachable within
5 seconds of startup. To verify the probe path works:

```bash
setopt interactive_comments
# Stop whisper-server first to simulate a cold-boot race
sudo launchctl bootout system/local.whisper 2>/dev/null || true

# Restart the service — should fail-fast with exit 78
sudo launchctl kickstart -k system/com.local-transcription-service
sleep 8
tail -n 5 /Users/uri/Library/Logs/local-transcription-service.err.log | grep startup_stt_not_ready
# expect: {"event": "startup_stt_not_ready", ...}

# Bring the STT engine back up; the service stays down until launchd
# is told to re-kickstart (KeepAlive on system context requires manual kickstart).
sudo launchctl bootstrap system /Library/LaunchDaemons/local.whisper.plist
sudo launchctl kickstart -k system/com.local-transcription-service
```

If the service comes back without manual intervention after the STT
restart, the probe isn't wired correctly — open an issue against the
service.

## Step 5 — Error-rate counter (HLD-001 §15.1)

Verify the counter is emitting:

```bash
tail -F /Users/uri/Library/Logs/local-transcription-service.out.log | grep error_rate_tick
# expect a JSON line every 60s: {"event": "error_rate_tick", "interval_s": 60, "counts": {...}}
```

To build an ad-hoc dashboard without a metrics endpoint (HLD §15 promise
kept), pipe the log through `jq` and a rolling-window aggregator:

```bash
tail -F /Users/uri/Library/Logs/local-transcription-service.out.log \
  | jq -c 'select(.event == "error_rate_tick") | .counts' \
  | python3 -c '
import sys, json
from collections import Counter
total = Counter()
for line in sys.stdin:
    total.update(json.loads(line))
print(dict(total))
'
```

---

## Rollback

If a Phase D component misbehaves:

```bash
setopt interactive_comments
# Disable the trash cleanup tick (keeps the plist, stops the schedule)
launchctl disable gui/$(id -u)/com.local-transcription-service.trash-cleanup

# Revert LTS_WORKER_COUNT to 1 in $HOME/.lts-env (Phase F moved it out of the plist)
ENV_FILE=$HOME/.lts-env
if grep -q '^LTS_WORKER_COUNT=' "$ENV_FILE"; then
  sed -i '' 's|^LTS_WORKER_COUNT=.*|LTS_WORKER_COUNT=1|' "$ENV_FILE"
fi
sudo launchctl kickstart -k system/com.local-transcription-service

# Remove the newsyslog config
sudo rm /etc/newsyslog.d/local-transcription-service.conf
sudo newsyslog -v
```

The trash-cleanup plist and newsyslog config are additive (Phase D).
The main service plist moved to a system LaunchDaemon in Phase F —
its rollback is the same `bootout`+`bootstrap` cycle but in the
`system/` namespace; see `macmini-deployment.md` §5 for the canonical
deploy workflow.