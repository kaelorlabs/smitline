#!/bin/sh
# Runs as root only long enough to hand the data volume and the Docker socket to the app
# user, then starts Smitline as that user. The meeting container runs as the same user,
# so both can read the private files they share.
set -eu
DATA="${COLLEAGUE_ROOT:-/data}"
if [ "$(id -u)" = 0 ]; then
  mkdir -p "$DATA"
  if [ "$(stat -c %u "$DATA")" != "$(id -u app)" ]; then
    chown -R app:app "$DATA"
  fi
  chmod 700 "$DATA"
  SOCK=/var/run/docker.sock
  if [ -S "$SOCK" ]; then
    GID="$(stat -c %g "$SOCK")"
    GROUP="$(getent group "$GID" | cut -d: -f1)"
    if [ -z "$GROUP" ]; then
      GROUP=docker-host
      groupadd --gid "$GID" "$GROUP"
    fi
    usermod -aG "$GROUP" app
  fi
  exec setpriv --reuid=app --regid=app --init-groups env HOME=/home/app /app/docker/start.sh "$@"
fi
exec /app/docker/start.sh "$@"
