#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
if [[ ! -d node_modules/mammoth || ! -d node_modules/pdfjs-dist ]]; then
  echo 'Control-panel dependencies are missing. Run npm install first.' >&2
  exit 1
fi
exec node control-panel/server.mjs
