#!/usr/bin/env bash
# Starts the meeting container by hand: to sign the bot in to Teams or Google
# (SMITLINE_AUTH_MODE=teams or google), or to join the meeting in .env.meeting.
# The runtime daemon starts it the same way for meetings placed through the calls API.
set -euo pipefail
# COLLEAGUE_* settings from before the rename set their SMITLINE_* names.
for old in ${!COLLEAGUE_@}; do export "SMITLINE_${old#COLLEAGUE_}=${!old}"; done
CODE="$(cd "$(dirname "$0")" && pwd)"
# Settings live in SMITLINE_ROOT (the image's data volume) or in the checkout.
DATA="${SMITLINE_ROOT:-$CODE}"
MEETING_DATA="${SMITLINE_MEETING_DATA:-$CODE/meeting-runtime}"
if [[ -n "${SMITLINE_MEETING_IMAGE:-}" ]]; then
  COMPOSE=(docker compose -f "$CODE/compose.meeting.image.yaml")
  GET_IMAGE=(--pull missing)
else
  COMPOSE=(docker compose -f "$CODE/compose.meeting.yaml" --project-directory "$CODE")
  GET_IMAGE=(--build)
fi
if [[ ! -f "$DATA/.env" ]]; then
  echo 'Set OPENAI_API_KEY first: smitline setup secrets' >&2
  exit 1
fi
# The container runs as this user so it can use the private runtime files.
export SMITLINE_UID="${SMITLINE_UID:-$(id -u)}"
export SMITLINE_GID="${SMITLINE_GID:-$(id -g)}"
# The folders it mounts; the published image mounts them as subpaths of the volume, which must exist.
MOUNTS=("$MEETING_DATA/profiles" "$MEETING_DATA/recordings" "$MEETING_DATA/run")
mkdir -p "${MOUNTS[@]}"
if ! chmod 700 "${MOUNTS[@]}"; then
  echo "Those directories must belong to you: sudo chown -R \"\$(id -u):\$(id -g)\" ${MOUNTS[*]}" >&2
  exit 1
fi
# The meeting view opens only with this start's token (meeting_view.py), kept in a private file
# where the console's Open meeting view finds it.
SMITLINE_VIEWER_TOKEN="$(od -An -N32 -tx1 /dev/urandom | tr -d ' \n')"
export SMITLINE_VIEWER_TOKEN
mkdir -p "$DATA/.smitline"
chmod 700 "$DATA/.smitline"
rm -f "$DATA/.smitline/meeting-view-token"
(umask 077 && printf '%s\n' "$SMITLINE_VIEWER_TOKEN" > "$DATA/.smitline/meeting-view-token")
view() {
  # In a terminal only: the console finds the token itself.
  if [[ -t 1 ]]; then
    echo "Meeting view: http://127.0.0.1:6082/vnc.html#autoconnect=true&path=websockify%3Ftoken%3D$SMITLINE_VIEWER_TOKEN"
  fi
}
if [[ "${SMITLINE_AUTH_MODE:-}" == teams || "${SMITLINE_AUTH_MODE:-}" == google ]]; then
  # Signing in needs no settings and no keys.
  SMITLINE_MEETING_ENV_FILE='' "${COMPOSE[@]}" up -d "${GET_IMAGE[@]}" meeting-agent
  view
  exit 0
fi
if [[ ! -f "$DATA/.env.meeting" ]]; then
  echo "Copy meeting-runtime/meeting.env.example to $DATA/.env.meeting with your meeting details." >&2
  exit 1
fi
SMITLINE_ROOT="$DATA" python3 "$CODE/meeting-runtime/preflight.py"
# Only what the meeting reads, in a private file that compose copies into the container; it is
# deleted once the container exists.
PRIVATE="$(mktemp -d)"
trap 'rm -rf "$PRIVATE"' EXIT
python3 "$CODE/meeting-runtime/meeting_env.py" "$DATA" "$PRIVATE/meeting.env"
SMITLINE_MEETING_ENV_FILE="$PRIVATE/meeting.env" "${COMPOSE[@]}" up -d "${GET_IMAGE[@]}" meeting-agent
view
echo 'Status and live captions: http://127.0.0.1:8094/health'
