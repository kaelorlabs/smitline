#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
if [[ -f .env ]]; then
  exec node --env-file=.env gpt-live/server.mjs
fi
exec node gpt-live/server.mjs
