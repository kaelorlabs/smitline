#!/bin/bash
# The two processes of the smitline container: the runtime daemon and the local console
# (web pages and the MCP endpoint). If either stops, the container stops, and Docker's
# restart policy starts it again.
set -euo pipefail
cd /app
# COLLEAGUE_* settings from before the rename set their SMITLINE_* names.
for old in ${!COLLEAGUE_@}; do export "SMITLINE_${old#COLLEAGUE_}=${!old}"; done
# Rename old .env keys and the .colleague folder in the data volume first.
python meeting-runtime/old_names.py "$SMITLINE_ROOT"
mkdir -p "$SMITLINE_ROOT/.smitline" "$SMITLINE_MEETING_DATA"
chmod 700 "$SMITLINE_ROOT/.smitline" "$SMITLINE_MEETING_DATA"
# With a command, run that smitline command instead of the services. With `mcp`, MCP clients
# that start their own container talk stdio (docker run -i --rm ... ghcr.io/kaelorlabs/smitline mcp);
# with --network host and the smitline volume, calls go to the running smitline container.
if [[ $# -gt 0 ]]; then
  exec /usr/local/bin/smitline "$@"
fi
MODE=(--host "${SMITLINE_DAEMON_HOST:-127.0.0.1}")
if [[ "${SMITLINE_SERVER_MODE:-}" == 1 ]]; then
  MODE+=(--server)
fi
python -u meeting-runtime/daemon_main.py --port "${SMITLINE_DAEMON_PORT:-8765}" "${MODE[@]}" &
node control-panel/server.mjs &
wait -n
exit $?
