#!/usr/bin/env bash
# Starts the runtime daemon on this computer's Python, or in Docker when this
# computer cannot create the daemon's virtual environment (for example Ubuntu
# without python3-venv). SMITLINE_DAEMON_RUNTIME=host or docker picks one.
set -euo pipefail
# COLLEAGUE_* settings from before the rename set their SMITLINE_* names.
for old in ${!COLLEAGUE_@}; do export "SMITLINE_${old#COLLEAGUE_}=${!old}"; done
cd "$(dirname "$0")"
HOST="${SMITLINE_DAEMON_HOST:-127.0.0.1}"
PORT="${SMITLINE_DAEMON_PORT:-8765}"
ROOT="$(pwd)"
VENV="${SMITLINE_PYTHON_VENV:-$ROOT/.venv}"
REQ="$ROOT/meeting-runtime/requirements-daemon.txt"
RUNTIME="${SMITLINE_DAEMON_RUNTIME:-auto}"
IMAGE="${SMITLINE_DAEMON_IMAGE:-smitline-daemon:local}"

MODE=()
if [[ "${SMITLINE_SERVER_MODE:-}" == 1 ]]; then
  MODE=(--server)
fi

has_aiohttp() {
  "$1" -c 'import aiohttp' >/dev/null 2>&1
}

# A venv made without python3-venv installed has python but no pip.
venv_usable() {
  [[ -x "$VENV/bin/python" ]] && "$VENV/bin/python" -m pip --version >/dev/null 2>&1
}

host_python_ready() {
  [[ -n "${SMITLINE_PYTHON:-}" ]] || venv_usable \
    || python3 -c 'import sys, venv, ensurepip; sys.exit(sys.version_info < (3, 10))' >/dev/null 2>&1
}

docker_ready() {
  command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1
}

run_on_host() {
  if [[ -n "${SMITLINE_PYTHON:-}" ]]; then
    PYTHON="$SMITLINE_PYTHON"
    if ! has_aiohttp "$PYTHON"; then
      echo 'Runtime daemon Python is missing aiohttp. Point SMITLINE_PYTHON at a venv with meeting-runtime/requirements-daemon.txt installed.' >&2
      exit 1
    fi
  else
    if [[ -x "$VENV/bin/python" ]] && ! venv_usable; then
      rm -rf "$VENV"
    fi
    if [[ ! -x "$VENV/bin/python" ]]; then
      if ! python3 -m venv "$VENV"; then
        rm -rf "$VENV"
        echo 'Could not create a Python virtual environment. Start Docker to run Smitline there instead, or on Ubuntu run: sudo apt install -y python3-venv' >&2
        exit 1
      fi
    fi
    PYTHON="$VENV/bin/python"
    if ! has_aiohttp "$PYTHON"; then
      "$PYTHON" -m pip install -q -r "$REQ"
    fi
  fi
  export PYTHON
  exec "$PYTHON" -u "$ROOT/meeting-runtime/daemon_main.py" --host "$HOST" --port "$PORT" ${MODE[@]+"${MODE[@]}"}
}

run_in_docker() {
  local hash desktop sock name
  hash="$(cat Dockerfile.daemon "$REQ" | { sha256sum 2>/dev/null || shasum -a 256; } | cut -c1-12)"
  if [[ "$(docker image inspect -f '{{index .Config.Labels "smitline.hash"}}' "$IMAGE" 2>/dev/null || true)" != "$hash" ]]; then
    echo 'Building the Smitline image (first start only; about a minute)...'
    tar -cf - Dockerfile.daemon -C meeting-runtime requirements-daemon.txt \
      | docker build --quiet --label "smitline.hash=$hash" -t "$IMAGE" -f Dockerfile.daemon -
  fi
  # Host networking keeps the daemon and phone gateway on this computer's loopback,
  # exactly where they are without Docker. The checkout is mounted at the same path,
  # so the meeting containers the daemon starts see the same files.
  local args=(--rm --init --name "smitline-daemon-$PORT" --network host
    -v "$ROOT:$ROOT" -w "$ROOT" -e HOME=/tmp)
  desktop="$(docker info -f '{{.OperatingSystem}}' 2>/dev/null || true)"
  if [[ "$desktop" == *"Docker Desktop"* ]]; then
    echo 'Docker Desktop: turn on host networking (Settings > Resources > Network) so this computer can reach Smitline.'
  else
    # Files the daemon writes stay owned by you.
    args+=(--user "$(id -u):$(id -g)")
  fi
  sock=/var/run/docker.sock
  if [[ "${DOCKER_HOST:-}" == unix://* ]]; then
    sock="${DOCKER_HOST#unix://}"
  fi
  if [[ -S "$sock" ]]; then
    args+=(-v "$sock:/var/run/docker.sock")
    if [[ "$desktop" != *"Docker Desktop"* ]]; then
      args+=(--group-add "$(stat -c %g "$sock" 2>/dev/null || stat -f %g "$sock")")
    fi
  fi
  # Settings exported in this shell reach the daemon by name; values stay off the command line.
  for name in $(compgen -e); do
    case "$name" in
      OPENAI_*|TWILIO_*|SIGNALWIRE_*|SMITLINE_*) args+=(-e "$name") ;;
    esac
  done
  docker rm -f "smitline-daemon-$PORT" >/dev/null 2>&1 || true
  exec docker run "${args[@]}" "$IMAGE" \
    python -u "$ROOT/meeting-runtime/daemon_main.py" --host "$HOST" --port "$PORT" ${MODE[@]+"${MODE[@]}"}
}

case "$RUNTIME" in
  host) run_on_host ;;
  docker)
    if ! docker_ready; then
      echo 'SMITLINE_DAEMON_RUNTIME=docker, but Docker is not running.' >&2
      exit 1
    fi
    run_in_docker ;;
  auto)
    if host_python_ready; then
      run_on_host
    elif docker_ready; then
      run_in_docker
    else
      echo 'Smitline needs Docker (recommended) or Python 3.10+ with venv. Start or install Docker, or on Ubuntu run: sudo apt install -y python3-venv' >&2
      exit 1
    fi ;;
  *)
    echo "SMITLINE_DAEMON_RUNTIME must be auto, host, or docker (got $RUNTIME)" >&2
    exit 1 ;;
esac
