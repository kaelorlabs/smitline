#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
if [[ ! -f .env ]]; then
  echo 'Set OPENAI_API_KEY in .env.' >&2
  exit 1
fi
# The container runs as this user so it can use the private runtime files.
export COLLEAGUE_UID="${COLLEAGUE_UID:-$(id -u)}"
export COLLEAGUE_GID="${COLLEAGUE_GID:-$(id -g)}"
MOUNTS=(meeting-runtime/profiles meeting-runtime/jobs meeting-runtime/recordings)
mkdir -p "${MOUNTS[@]}"
if ! chmod 700 "${MOUNTS[@]}"; then
  echo "Those directories must belong to you: sudo chown -R \"\$(id -u):\$(id -g)\" ${MOUNTS[*]}" >&2
  exit 1
fi
if [[ "${COLLEAGUE_AUTH_MODE:-}" == teams || "${COLLEAGUE_AUTH_MODE:-}" == google ]]; then
  docker compose build joinly
  docker compose -f compose.meeting.yaml up -d --build meeting-agent
  exit 0
fi
if [[ ! -f .env.meeting ]]; then
  echo 'Copy meeting-runtime/meeting.env.example to .env.meeting with your meeting details.' >&2
  exit 1
fi
python3 meeting-runtime/preflight.py
mkdir -p meeting-runtime/codex-workspace

# Read only the non-secret product switches needed by the host worker. Do
# not source the meeting file as shell code.
COLLEAGUE_WORKSPACE_VALUE=
COLLEAGUE_CODEX_VALUE=1
COLLEAGUE_CHARTS_VALUE=0
while IFS='=' read -r key value; do
  case "$key" in
    COLLEAGUE_WORKSPACE) COLLEAGUE_WORKSPACE_VALUE="$value" ;;
    COLLEAGUE_ENABLE_CODEX) COLLEAGUE_CODEX_VALUE="$value" ;;
    COLLEAGUE_ENABLE_CHARTS) COLLEAGUE_CHARTS_VALUE="$value" ;;
  esac
done < .env.meeting
if [[ -n "$COLLEAGUE_WORKSPACE_VALUE" ]]; then
  export COLLEAGUE_WORKSPACE="$COLLEAGUE_WORKSPACE_VALUE"
fi
export COLLEAGUE_ENABLE_CHARTS="$COLLEAGUE_CHARTS_VALUE"
if [[ "$COLLEAGUE_CODEX_VALUE" == 1 ]]; then
  CODEX_BIN="${CODEX_BIN:-$(command -v codex || true)}"
  if [[ -z "$CODEX_BIN" && -x /Applications/ChatGPT.app/Contents/Resources/codex ]]; then
    CODEX_BIN=/Applications/ChatGPT.app/Contents/Resources/codex
  fi
  if [[ -z "$CODEX_BIN" ]]; then
    echo 'Codex CLI not found. Install it or set CODEX_BIN, then run codex login.' >&2
    exit 1
  fi
  if ! "$CODEX_BIN" login status >/dev/null 2>&1; then
    echo 'Codex CLI is not logged in. Run codex login first.' >&2
    exit 1
  fi
  export CODEX_BIN
fi
docker compose build joinly
docker compose -f compose.meeting.yaml up -d --build meeting-agent
echo 'Agent browser: http://127.0.0.1:6082/vnc.html?autoconnect=true'
echo 'Status, web-search sources, and coding-agent runs: http://127.0.0.1:8094/health'
if [[ "$COLLEAGUE_CODEX_VALUE" == 1 ]]; then
  echo 'Keep this terminal running for Codex tool calls. Ctrl-C stops the Codex worker.'
  exec python3 -u meeting-runtime/codex_worker.py
fi
echo 'Codex tool disabled. The meeting participant continues in Docker.'
