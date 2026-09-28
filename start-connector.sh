#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
if ! command -v node >/dev/null 2>&1; then
  echo 'Node.js 22 or newer is required to run the remote connector.' >&2
  exit 1
fi
exec node packages/mcp/src/remote.mjs
