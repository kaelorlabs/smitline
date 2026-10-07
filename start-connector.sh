#!/usr/bin/env bash
set -euo pipefail
# COLLEAGUE_* settings from before the rename set their SMITLINE_* names.
for old in ${!COLLEAGUE_@}; do export "SMITLINE_${old#COLLEAGUE_}=${!old}"; done
cd "$(dirname "$0")"
if ! command -v node >/dev/null 2>&1; then
  echo 'Node.js 22 or newer is required to run the remote connector.' >&2
  exit 1
fi
exec node packages/mcp/src/remote.mjs
