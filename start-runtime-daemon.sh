#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
HOST="${COLLEAGUE_DAEMON_HOST:-127.0.0.1}"
PORT="${COLLEAGUE_DAEMON_PORT:-8765}"
ROOT="$(pwd)"
VENV="${COLLEAGUE_PYTHON_VENV:-$ROOT/.venv}"
REQ="$ROOT/meeting-runtime/requirements-daemon.txt"

has_aiohttp() {
  "$1" -c 'import aiohttp' >/dev/null 2>&1
}

if [[ -n "${COLLEAGUE_PYTHON:-}" ]]; then
  PYTHON="$COLLEAGUE_PYTHON"
  if ! has_aiohttp "$PYTHON"; then
    echo 'Runtime daemon Python is missing aiohttp. Point COLLEAGUE_PYTHON at a venv with meeting-runtime/requirements-daemon.txt installed.' >&2
    exit 1
  fi
else
  # A venv made without python3-venv installed has python but no pip; start over.
  if [[ -x "$VENV/bin/python" ]] && ! "$VENV/bin/python" -m pip --version >/dev/null 2>&1; then
    rm -rf "$VENV"
  fi
  if [[ ! -x "$VENV/bin/python" ]]; then
    if ! python3 -m venv "$VENV"; then
      rm -rf "$VENV"
      echo 'Could not create a Python virtual environment. On Ubuntu run: sudo apt install -y python3-venv' >&2
      exit 1
    fi
  fi
  PYTHON="$VENV/bin/python"
  if ! has_aiohttp "$PYTHON"; then
    "$PYTHON" -m pip install -q -r "$REQ"
  fi
fi

export PYTHON
MODE=()
if [[ "${COLLEAGUE_SERVER_MODE:-}" == 1 ]]; then
  MODE=(--server)
fi
exec "$PYTHON" -u "$ROOT/meeting-runtime/daemon_main.py" --host "$HOST" --port "$PORT" ${MODE[@]+"${MODE[@]}"}
