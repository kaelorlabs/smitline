#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
if [[ ! -f .env || ! -f .env.zoom ]]; then
  echo 'Set OPENAI_API_KEY in .env and copy zoom-live/meeting.env.example to .env.zoom with your meeting details.' >&2
  exit 1
fi
python3 zoom-live/preflight.py
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
mkdir -p zoom-live/jobs zoom-live/codex-workspace
mkdir -p zoom-live/recordings

# Read only the two non-secret product switches needed by the host worker. Do
# not source the meeting file as shell code.
COLLEAGUE_PROFILE_VALUE=standard
COLLEAGUE_DEMO_DATA_VALUE=
while IFS='=' read -r key value; do
  case "$key" in
    COLLEAGUE_PROFILE) COLLEAGUE_PROFILE_VALUE="$value" ;;
    COLLEAGUE_ENABLE_DEMO_DATA) COLLEAGUE_DEMO_DATA_VALUE="$value" ;;
  esac
done < .env.zoom
if [[ -z "$COLLEAGUE_DEMO_DATA_VALUE" && ( "$COLLEAGUE_PROFILE_VALUE" == demo || "$COLLEAGUE_PROFILE_VALUE" == fact_check ) ]]; then
  COLLEAGUE_DEMO_DATA_VALUE=1
fi
if [[ "$COLLEAGUE_DEMO_DATA_VALUE" == 1 ]]; then
  python3 zoom-live/company_database.py >/dev/null
fi
export COLLEAGUE_ENABLE_DEMO_DATA="${COLLEAGUE_DEMO_DATA_VALUE:-0}"
docker compose build joinly
docker compose -f compose.zoom.yaml up -d --build zoom-live
echo 'Agent browser: http://127.0.0.1:6082/vnc.html?autoconnect=true'
echo 'Status, web-search sources, and Codex runs: http://127.0.0.1:8094/health'
echo 'Keep this terminal running for Codex tool calls. Ctrl-C stops the Codex worker.'
exec python3 -u zoom-live/codex_worker.py
