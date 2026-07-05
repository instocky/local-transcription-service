#!/usr/bin/env bash
# Launchd wrapper for local-transcription-service.
#
# Mirrors the /opt/{litellm,whisper}/run.sh pattern that LiteLLM
# and whisper LaunchDaemons use on this Mac Mini. launchd does
# NOT source $HOME/.lts-env implicitly — environments are passed
# only through the plist's <key>EnvironmentVariables</key>, and we
# deliberately keep secrets out of the plist for parity with how
# /opt/{litellm,whisper} are structured (their configs live next
# to their run.sh scripts in /opt/X/, not in their plists).
#
# Once `exec` is called, the shell process is replaced by Python
# in the OS process table — launchd tracks PID 1 of the service
# against the Python process directly. This means signals and
# log capture flow cleanly through uvicorn.

set -euo pipefail

ENV_FILE="$HOME/.lts-env"
if [ -f "$ENV_FILE" ]; then
  # Export every KEY=VALUE assignment from $HOME/.lts-env into the
  # environment of the process we're about to exec. The file holds
  # LTS_AUTH_TOKEN, LTS_STT_API_KEY, the venv-prepended PATH, and
  # any other LTS_* overrides set by the operator.
  set -a
  . "$ENV_FILE"
  set +a
fi

cd /opt/local-transcription-service
exec ./.venv/bin/python -m local_transcription_service.app
