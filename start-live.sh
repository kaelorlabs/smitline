#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
mkdir -p live/jobs live/workspace
docker compose build joinly
docker compose --profile live up -d live
echo 'Open http://127.0.0.1:8092/ and click Join live call.'
echo 'Keep this terminal running for Codex replies. Ctrl-C stops the reply worker.'
exec python3 -u live/worker.py
