#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
if [[ ! -f .env || ! -f .env.zoom ]]; then
  echo 'Set OPENAI_API_KEY in .env and copy zoom-live/meeting.env.example to .env.zoom with your meeting details.' >&2
  exit 1
fi
docker compose build joinly
docker compose -f compose.zoom.yaml up -d --build zoom-live
echo 'Agent browser: http://127.0.0.1:6082/vnc.html?autoconnect=true'
echo 'Status and web-search sources: http://127.0.0.1:8094/health'
