# Backlog

Non-blocking follow-up tasks accumulated during Phase E (Mac Mini
deploy) + Phase F (system LaunchDaemon migration) sessions,
2026-07-05. Each task has a clear acceptance criteria and is sized
small enough to pick up in a 10–15 minute slot.

## Tasks

### BLG-001 — WARP auto-connect via launchd (Phase G candidate)

| Field        | Value                                          |
|--------------|------------------------------------------------|
| Priority     | Medium                                          |
| Surface      | Mac Mini ops                                    |
| Blocked by   | —                                               |

**Problem.** `local.litellm`, `local.whisper`, and our service are
registered as system LaunchDaemons, so they auto-start after a
reboot. `Cloudflare WARP` (installed via `brew install --cask
cloudflare-warp`) is a GUI-driven tunnel — it does **not** auto-
connect when the OS comes back up. Result: the first `POST /jobs`
after every `sudo shutdown -r now` fails with
`FETCH_FAILED / nodename nor servname provided` until the operator
manually toggles WARP via the menu-bar icon.

**Repro.**
```bash
ssh uri@mac-mini-urij.local
sudo shutdown -r now
# wait ~60s for OS + daemons
curl -X POST http://127.0.0.1:8766/jobs \
  -H "X-Auth-Token: $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"video_url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"}'
# → job ends up in `failed` with DNS error
```

**Fix.** Register a system LaunchDaemon (per the
`local.local-transcription-service.plist` pattern) that calls
`warp-cli connect` on `RunAtLoad`, before our service starts
claiming jobs. Order matters — WARP must be Up before any
YouTube-bound subprocess runs, so the plist should land in
`/Library/LaunchDaemons/` (system, runs before user-side
LaunchAgents in launchd boot ordering).

Acceptance criteria:

- `warp-cli status` returns `Connected` within 30 seconds of `sudo
  shutdown -r now`, with no operator action.
- A `POST /jobs` after reboot reaches `done` without operator
  intervention.
- A second LaunchDaemon entry doesn't conflict with the existing
  `local.*` ones.

### BLG-002 — POSIX 600 on `$HOME/.lts-env` and `$HOME/.lts-token`

| Field        | Value                                          |
|--------------|------------------------------------------------|
| Priority     | Low                                             |
| Surface      | File permissions on Mac Mini                   |
| Blocked by   | —                                               |

**Problem.** The two operator-written files holding the deployed
secrets (`LTS_AUTH_TOKEN`, `LTS_STT_API_KEY`) and the venv-first
`PATH=` are currently `644`. On a single-user Mac Mini this is
de-facto private, but POSIX-correct is `600` so non-owner processes
on the box cannot read them by accident.

**Fix.** One command:

```bash
chmod 600 ~/.lts-env ~/.lts-token
ls -la ~/.lts-env ~/.lts-token
# expect: -rw-------   1 uri  staff  ...
```

Acceptance: both files show `-rw-------` and `~/`.

### BLG-003 — Rotate `LTS_AUTH_TOKEN` and `LTS_STT_API_KEY`

| Field        | Value                                          |
|--------------|------------------------------------------------|
| Priority     | High (security)                                |
| Surface      | LiteLLM config + service config + operator notes |
| Blocked by   | —                                               |

**Problem.** Both values were pasted into an mavis chat transcript
on 2026-07-05 during the Phase E debug session. The session-time
deployment used them (`LTS_AUTH_TOKEN=nTY28...`, `LITELLM_KEY=sk-c981...`).
We treat both as compromised per standard secret-in-chat policy and
the operator should rotate them at the next convenient window.

**Fix.** Two coordinated edits + one restart.

1. Generate fresh values (operator-side, not in any chat):
   ```bash
   # on Mac Mini, in a local file
   python3 -c 'import secrets; print("LTS_AUTH_TOKEN=" + secrets.token_urlsafe(24))' > /tmp/newauth
   python3 -c 'import secrets; print("sk-" + secrets.token_hex(32))' > /tmp/newkey
   ```
2. Update LiteLLM master key in `/opt/litellm/config.yaml`
   (`master_key: "..."`), restart the LiteLLM LaunchDaemon.
3. Replace the two values in `~/.lts-env`, restart our service via
   `sudo launchctl kickstart -k system/com.local-transcription-service`.
4. Update the Postman / extension `X-Auth-Token` in lockstep with
   step 3, otherwise the extension gets a 401.

Acceptance:

- `~/.lts-env` no longer contains the values from `mavis` chat history.
- `/opt/litellm/config.yaml` no longer contains the value from chat.
- `curl http://192.168.0.99:8766/health -H "X-Auth-Token: <new>"`
  returns `200 {"status":"ok","version":"0.1.0"}`.
- The post-rotation workflow (submit → done) succeeds end-to-end.
