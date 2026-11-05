#!/usr/bin/env bash
# How much CPU the meeting camera takes with a presence pack, on this machine (scripts/camera_cpu.py):
#   scripts/camera-cpu.sh IMAGE PACK_DIR [SECONDS] [-- extra options for camera_cpu.py]
# IMAGE is a meeting image (ghcr.io/kaelorlabs/smitline-meeting:VERSION, or one built from a checkout
# with docker build -f Dockerfile.meeting -t smitline-meeting:local .), PACK_DIR a presence pack
# folder. Runs the browser measure (Chromium with the camera at the pack's size and frame rate,
# encoding through a local WebRTC loopback, as on a meeting VM) and then the Daily room camera
# measure, one after the other. Each prints a table and a last line RESULT {...} (JSON).
# Run it on an otherwise idle machine of the size to judge, such as a meeting VM's (e2-standard-2).
set -euo pipefail
IMAGE="${1:?usage: camera-cpu.sh IMAGE PACK_DIR [SECONDS] [-- options]}"
PACK="$(cd "${2:?the presence pack folder}" && pwd)"
SECONDS_TO_MEASURE="${3:-60}"
shift $(( $# < 3 ? $# : 3 ))
[[ "${1:-}" == "--" ]] && shift
HERE="$(cd "$(dirname "$0")" && pwd)"
for mode in browser daily; do
  echo "== $mode"
  docker run --rm --shm-size=1g --entrypoint /app/.venv/bin/python \
    -v "$PACK:/pack:ro" -v "$HERE:/bench:ro" "$IMAGE" \
    /bench/camera_cpu.py "$mode" --pack /pack --seconds "$SECONDS_TO_MEASURE" "$@"
done
