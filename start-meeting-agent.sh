#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
if [[ ! -f .env ]]; then
  echo 'Set OPENAI_API_KEY in .env.' >&2
  exit 1
fi
# The container runs as this user so it can use the private runtime files.
export COLLEAGUE_UID="${COLLEAGUE_UID:-$(id -u)}"
export COLLEAGUE_GID="${COLLEAGUE_GID:-$(id -g)}"
MOUNTS=(meeting-runtime/profiles meeting-runtime/recordings)
mkdir -p "${MOUNTS[@]}"
if ! chmod 700 "${MOUNTS[@]}"; then
  echo "Those directories must belong to you: sudo chown -R \"\$(id -u):\$(id -g)\" ${MOUNTS[*]}" >&2
  exit 1
fi
if [[ "${COLLEAGUE_AUTH_MODE:-}" == teams || "${COLLEAGUE_AUTH_MODE:-}" == google ]]; then
  docker compose -f compose.meeting.yaml up -d --build meeting-agent
  exit 0
fi
if [[ ! -f .env.meeting ]]; then
  echo 'Copy meeting-runtime/meeting.env.example to .env.meeting with your meeting details.' >&2
  exit 1
fi
python3 meeting-runtime/preflight.py
docker compose -f compose.meeting.yaml up -d --build meeting-agent
echo 'Agent browser: http://127.0.0.1:6082/vnc.html?autoconnect=true'
echo 'Status and live captions: http://127.0.0.1:8094/health'
