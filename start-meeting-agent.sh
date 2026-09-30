#!/usr/bin/env bash
# Starts the meeting container by hand: to sign the bot in to Teams or Google
# (COLLEAGUE_AUTH_MODE=teams or google), or to join the meeting in .env.meeting.
# The runtime daemon starts it the same way for meetings placed through the calls API.
set -euo pipefail
CODE="$(cd "$(dirname "$0")" && pwd)"
# Settings live in COLLEAGUE_ROOT (the image's data volume) or in the checkout.
DATA="${COLLEAGUE_ROOT:-$CODE}"
MEETING_DATA="${COLLEAGUE_MEETING_DATA:-$CODE/meeting-runtime}"
if [[ -n "${COLLEAGUE_MEETING_IMAGE:-}" ]]; then
  COMPOSE=(docker compose -f "$CODE/compose.meeting.image.yaml")
  GET_IMAGE=(--pull missing)
else
  COMPOSE=(docker compose -f "$CODE/compose.meeting.yaml" --project-directory "$CODE")
  GET_IMAGE=(--build)
fi
if [[ ! -f "$DATA/.env" ]]; then
  echo 'Set OPENAI_API_KEY first: colleague setup secrets' >&2
  exit 1
fi
# The container runs as this user so it can use the private runtime files.
export COLLEAGUE_UID="${COLLEAGUE_UID:-$(id -u)}"
export COLLEAGUE_GID="${COLLEAGUE_GID:-$(id -g)}"
MOUNTS=("$MEETING_DATA/profiles" "$MEETING_DATA/recordings")
mkdir -p "${MOUNTS[@]}"
if ! chmod 700 "${MOUNTS[@]}"; then
  echo "Those directories must belong to you: sudo chown -R \"\$(id -u):\$(id -g)\" ${MOUNTS[*]}" >&2
  exit 1
fi
if [[ "${COLLEAGUE_AUTH_MODE:-}" == teams || "${COLLEAGUE_AUTH_MODE:-}" == google ]]; then
  "${COMPOSE[@]}" up -d "${GET_IMAGE[@]}" meeting-agent
  exit 0
fi
if [[ ! -f "$DATA/.env.meeting" ]]; then
  echo "Copy meeting-runtime/meeting.env.example to $DATA/.env.meeting with your meeting details." >&2
  exit 1
fi
COLLEAGUE_ROOT="$DATA" python3 "$CODE/meeting-runtime/preflight.py"
"${COMPOSE[@]}" up -d "${GET_IMAGE[@]}" meeting-agent
echo 'Agent browser: http://127.0.0.1:6082/vnc.html?autoconnect=true'
echo 'Status and live captions: http://127.0.0.1:8094/health'
