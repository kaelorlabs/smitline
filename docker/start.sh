#!/bin/bash
# The two processes of the colleague container: the runtime daemon and the local console
# (web pages and the MCP endpoint). If either stops, the container stops, and Docker's
# restart policy starts it again.
set -euo pipefail
cd /app
mkdir -p "$COLLEAGUE_ROOT/.colleague" "$COLLEAGUE_MEETING_DATA"
chmod 700 "$COLLEAGUE_ROOT/.colleague" "$COLLEAGUE_MEETING_DATA"
MODE=(--host "${COLLEAGUE_DAEMON_HOST:-127.0.0.1}")
if [[ "${COLLEAGUE_SERVER_MODE:-}" == 1 ]]; then
  MODE+=(--server)
fi
python -u meeting-runtime/daemon_main.py --port "${COLLEAGUE_DAEMON_PORT:-8765}" "${MODE[@]}" &
node control-panel/server.mjs &
wait -n
exit $?
