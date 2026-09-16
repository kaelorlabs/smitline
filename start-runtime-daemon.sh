#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
HOST="${COLLEAGUE_DAEMON_HOST:-127.0.0.1}"
PORT="${COLLEAGUE_DAEMON_PORT:-8765}"
exec python3 -u meeting-runtime/daemon_main.py --host "$HOST" --port "$PORT"
