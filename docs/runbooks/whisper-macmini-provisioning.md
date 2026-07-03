# RUNBOOK — Provision whisper.cpp (Metal) behind LiteLLM on the Mac Mini

| Field   | Value                                                              |
| ------- | ------------------------------------------------------------------ |
| Target  | Mac Mini, Apple Silicon, macOS — LAN `192.168.0.99`                |
| Serves  | whisper.cpp `whisper-server`, loopback `127.0.0.1:8779`            |
| Fronted | existing LiteLLM Proxy `:4000` (OpenAI `/v1/audio/transcriptions`) |
| Model   | benchmark `medium` vs `large-v3-turbo`, pick after RTF numbers     |
| Runs by | operator over SSH (this doc = copy-paste commands)                 |

Incorporates Tech Lead review: model benchmark, dynamic threads, warmup,
ffmpeg preflight, stdout/stderr logs, launchd from the start, liveness via
LiteLLM `/v1/models`, `verbose_json` check, monitoring.

> **Preferred execution: run the scripts, do NOT paste this Markdown line-by-line.**
> Interactive zsh mangles inline `#` comments and non-ASCII. Canonical scripts
> live in `scripts/whisper-macmini/` (ASCII, bash, idempotent):
> `provision-whisper.sh` (build + models + wrapper + launchd),
> `bench-whisper.sh` (medium vs large-v3-turbo on a scratch port),
> `smoke-whisper.sh` (direct + gateway smoke). Deliver each to the Mac via a
> single `cat > x.sh <<'EOF' ... EOF` heredoc, then `bash x.sh`.
>
> **This Mac Mini: 4 P-cores + 6 E-cores, 16 GB RAM → `THREADS=4`.** Threads use
> `sysctl -n hw.perflevel0.physicalcpu` with an explicit `if [ -z ]` fallback —
> never a silent `|| sysctl hw.ncpu` (ncpu includes E-cores).

The step-by-step below is the narrative/reference for what the scripts do.

---

## Step 0 — Preflight

> Shell = zsh (macOS default). Line 1 (`setopt interactive_comments`) makes the
> inline `#` comments in this runbook safe to paste; without it zsh treats `#`
> as a bad argument.

```bash
setopt interactive_comments
command -v brew >/dev/null || { echo "Homebrew missing - install first"; exit 1; }
which ffmpeg && ffmpeg -version | head -1     # --convert depends on ffmpeg
command -v bc || echo "bc missing (bench.sh uses python3, so this is fine)"
sysctl -n hw.perflevel0.physicalcpu           # performance cores = thread count
sysctl -n hw.ncpu                             # fallback total cores
memory_pressure | head -3                     # free memory headroom
nc -z 127.0.0.1 8779 && echo "PORT 8779 BUSY - pick another" || echo "8779 free"
```

If `ffmpeg` is missing: `brew install ffmpeg`.

## Step 1 — Build whisper.cpp + download both candidate models

```bash
brew install cmake ffmpeg          # no-op if present
if [ ! -d ~/whisper.cpp ]; then
  git clone https://github.com/ggml-org/whisper.cpp ~/whisper.cpp
else
  git -C ~/whisper.cpp pull --ff-only
fi
cd ~/whisper.cpp
git rev-parse HEAD                 # record the commit the benchmark ran on
cmake -B build                     # Metal auto-enabled on Apple Silicon
cmake --build build -j --config Release
sh ./models/download-ggml-model.sh medium
sh ./models/download-ggml-model.sh large-v3-turbo
for f in build/bin/whisper-server models/ggml-medium.bin models/ggml-large-v3-turbo.bin; do
  test -f "$f" || { echo "MISSING: $f"; exit 1; }
done
ls -lh build/bin/whisper-server models/ggml-medium.bin models/ggml-large-v3-turbo.bin
```

## Step 2 — Foreground boot + warmup (validate the engine)

Threads are computed, NOT hardcoded. Start with `large-v3-turbo`:

```bash
cd ~/whisper.cpp
THREADS=$(sysctl -n hw.perflevel0.physicalcpu 2>/dev/null || sysctl -n hw.ncpu)
./build/bin/whisper-server \
  -m models/ggml-large-v3-turbo.bin \
  --host 127.0.0.1 --port 8779 \
  --inference-path /v1/audio/transcriptions \
  --convert -t "$THREADS"
# expect log: "whisper_backend_init: using Metal backend"
```

In a second SSH shell — warmup (first inference is always slowest), then smoke:

```bash
cd ~/whisper.cpp
# warmup (discarded)
curl -s 127.0.0.1:8779/v1/audio/transcriptions -F file="@samples/jfk.wav" -F response_format="json" >/dev/null
# smoke — json
curl -s 127.0.0.1:8779/v1/audio/transcriptions -F file="@samples/jfk.wav" -F response_format="json"
# smoke — verbose_json (timestamps / segments / language / duration for later pipeline use)
curl -s 127.0.0.1:8779/v1/audio/transcriptions -F file="@samples/jfk.wav" -F response_format="verbose_json"
```

Expect `{"text":" And so my fellow Americans..."}`. **Send both outputs.**

## Step 3 — Benchmark `medium` vs `large-v3-turbo` (RTF, cold/warm)

Save `~/whisper.cpp/bench.sh`:

```bash
cat > ~/whisper.cpp/bench.sh <<'EOF'
#!/bin/bash
# usage: ./bench.sh <port> <audio> <warm_runs>   (arithmetic via python3, no bc)
PORT=${1:-8779}; AUDIO=${2:-samples/jfk.wav}; RUNS=${3:-5}
DUR=$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$AUDIO")
url="127.0.0.1:$PORT/v1/audio/transcriptions"
now(){ python3 -c 'import time;print(time.time())'; }
echo "audio=${AUDIO} duration=${DUR}s"
a=$(now); curl -s "$url" -F file="@$AUDIO" -F response_format=json >/dev/null; b=$(now)
python3 -c "print(f'cold: {$b-$a:.3f}s')"
sum=0
for i in $(seq 1 "$RUNS"); do
  a=$(now); curl -s "$url" -F file="@$AUDIO" -F response_format=json >/dev/null; b=$(now)
  d=$(python3 -c "print(f'{$b-$a:.3f}')"); sum=$(python3 -c "print($sum+$d)")
  echo "warm[$i]: ${d}s"
done
python3 -c "avg=$sum/$RUNS; print(f'warm_avg={avg:.3f}s  RTF={avg/$DUR:.3f}  (RTF<1 = faster than realtime)')"
EOF
chmod +x ~/whisper.cpp/bench.sh
```

Run per model. Foreground-boot each model in one shell (parametrized, no file
edit), run the bench in another, Ctrl-C between models:

```bash
# shell A — large-v3-turbo
cd ~/whisper.cpp
THREADS=$(sysctl -n hw.perflevel0.physicalcpu 2>/dev/null || sysctl -n hw.ncpu)
./build/bin/whisper-server -m models/ggml-large-v3-turbo.bin \
  --host 127.0.0.1 --port 8779 --inference-path /v1/audio/transcriptions --convert -t "$THREADS"
# shell B
~/whisper.cpp/bench.sh 8779 samples/jfk.wav 5

# Ctrl-C shell A, relaunch with -m models/ggml-medium.bin, re-run bench in shell B
```

**Send both RTF blocks.** Decision rule: if `medium` RTF is materially lower
and transcript quality is acceptable on a real sample → ship `medium`;
else keep `large-v3-turbo`. Whichever wins becomes the model in Step 4/5.

## Step 4 — launchd (persistent, dynamic threads, logs)

Wrapper computes threads at launch. Model is a parameter (override with
`MODEL=... launchctl ...` or edit the default) — NO inline comment after a
backslash (that breaks line continuation):

```bash
cat > ~/whisper.cpp/run-whisper-server.sh <<'EOF'
#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"
# Swap to models/ggml-medium.bin if the benchmark chooses it.
MODEL="${MODEL:-models/ggml-large-v3-turbo.bin}"
PORT="${PORT:-8779}"
THREADS=$(sysctl -n hw.perflevel0.physicalcpu 2>/dev/null || sysctl -n hw.ncpu)
test -f "$MODEL" || { echo "model not found: $MODEL" >&2; exit 1; }
exec ./build/bin/whisper-server \
  -m "$MODEL" \
  --host 127.0.0.1 \
  --port "$PORT" \
  --inference-path /v1/audio/transcriptions \
  --convert \
  -t "$THREADS"
EOF
chmod +x ~/whisper.cpp/run-whisper-server.sh
```

LaunchAgent (paths resolved from `$HOME`):

```bash
mkdir -p ~/Library/Logs
cat > ~/Library/LaunchAgents/com.local.whisper-server.plist <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.local.whisper-server</string>
  <key>ProgramArguments</key>
  <array><string>$HOME/whisper.cpp/run-whisper-server.sh</string></array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$HOME/Library/Logs/whisper-server.log</string>
  <key>StandardErrorPath</key><string>$HOME/Library/Logs/whisper-server.err</string>
</dict>
</plist>
EOF
```

Stop the foreground server from Step 2 first (Ctrl-C), then load the service.
`bootout` first makes this re-runnable (bootstrap errors if already loaded):

```bash
launchctl bootout gui/$(id -u)/com.local.whisper-server 2>/dev/null || true
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.local.whisper-server.plist
launchctl enable  gui/$(id -u)/com.local.whisper-server
launchctl kickstart -k gui/$(id -u)/com.local.whisper-server
launchctl print gui/$(id -u)/com.local.whisper-server | grep -E "state|pid"
tail -n 20 ~/Library/Logs/whisper-server.err
```

Liveness (whisper.cpp has no `/health`; index page = process alive):

```bash
curl -fsS -o /dev/null -w "%{http_code}\n" 127.0.0.1:8779/    # expect 200
```

## Step 5 — Register in LiteLLM + verify it actually registered

```bash
sudo cp /opt/litellm/config.yaml /opt/litellm/config.yaml.bak.$(date +%s)
```

Add to `model_list:` (use the model chosen in Step 3):

```yaml
- model_name: whisper-large-v3-turbo
  litellm_params:
    model: openai/whisper-large-v3-turbo # openai/-route → LiteLLM appends /audio/transcriptions
    api_base: http://127.0.0.1:8779/v1
    api_key: 'dummy' # whisper-server has no auth (loopback only)
  model_info:
    mode: audio_transcription
```

Restart LiteLLM (**use your existing restart method** — tell me if it's a
launchd label / script, I'll pin the exact command), then CONFIRM registration
before trusting the wire (past LiteLLM/version drift makes this mandatory):

```bash
MK=sk-...yourmasterkey
curl -s http://127.0.0.1:4000/v1/models -H "Authorization: Bearer $MK" \
  | python3 -c "import sys,json;print([m['id'] for m in json.load(sys.stdin)['data']])"
# must list: whisper-large-v3-turbo
```

Gateway smoke (exactly how the service will call it) — test BOTH formats so a
param dropped by LiteLLM shows up immediately:

```bash
# json (what the service uses)
curl -s http://127.0.0.1:4000/v1/audio/transcriptions \
  -H "Authorization: Bearer $MK" \
  -F file="@$HOME/whisper.cpp/samples/jfk.wav" \
  -F model="whisper-large-v3-turbo"
# verbose_json (timestamps/segments/language/duration must survive the proxy)
curl -s http://127.0.0.1:4000/v1/audio/transcriptions \
  -H "Authorization: Bearer $MK" \
  -F file="@$HOME/whisper.cpp/samples/jfk.wav" \
  -F model="whisper-large-v3-turbo" \
  -F response_format="verbose_json"
```

**Send both outputs** — final end-to-end confirmation.

## Step 6 — Monitoring during one real run

```bash
# GPU/CPU power draw (5 samples)
sudo powermetrics --samplers cpu_power,gpu_power -i 1000 -n 5
# live resource view
top -o cpu -l 3 | head -20
```

Watch: CPU, GPU (Metal), RAM, temperature. Record peak RSS for the chosen model.

---

## Report back to update HLD/task-doc

1. Step 2 outputs (json + verbose_json).
2. Step 3 RTF blocks for both models + chosen model.
3. Step 5 `/v1/models` list + gateway smoke output.
4. LiteLLM restart method (to pin Step 5 command).

On green: rewrite TASK-B (B0 → PASS, engine = whisper.cpp behind LiteLLM) and
amend HLD-001 §4 / ADR-012 (Stage 3 engine: ollama → OpenAI-compatible whisper;
config `LTS_STT_BASE_URL` + `LTS_STT_API_KEY`, `LTS_STT_ENGINE=openai`).
