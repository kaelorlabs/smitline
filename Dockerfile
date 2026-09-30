# Colleague AI in one image: the runtime daemon (calls API, phone gateway, meetings), the
# local console with its MCP endpoint, and the colleague CLI. Run it with host networking,
# a data volume, and the Docker socket (meetings run in a second container):
#
#   docker run -d --name colleague --restart unless-stopped --network host \
#     -v colleague:/data -v /var/run/docker.sock:/var/run/docker.sock \
#     ghcr.io/kaelorlabs/colleague
#
# Settings and records live in the volume. Everything else: docker exec colleague colleague ...
FROM node:22-bookworm-slim AS node
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --omit=dev --omit=optional --ignore-scripts --no-audit --no-fund

# Pinned so a rebuild of the same release gets the same binaries.
FROM docker:29.8.1-cli AS docker-cli
FROM cloudflare/cloudflared:2026.9.3 AS cloudflared

FROM python:3.12-slim-bookworm
# The meeting image this build goes with; the workflow sets the published tag.
ARG MEETING_IMAGE=colleague-meeting:local
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=docker-cli /usr/local/bin/docker /usr/local/bin/docker
COPY --from=docker-cli /usr/local/libexec/docker/cli-plugins/docker-compose /usr/local/libexec/docker/cli-plugins/docker-compose
COPY --from=cloudflared /usr/local/bin/cloudflared /usr/local/bin/cloudflared
COPY meeting-runtime/requirements-daemon.txt /tmp/requirements-daemon.txt
RUN apt-get update && apt-get install -y --no-install-recommends tini \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir -r /tmp/requirements-daemon.txt && rm /tmp/requirements-daemon.txt \
    && groupadd --gid 1001 app && useradd --uid 1001 --gid 1001 -m app

WORKDIR /app
COPY --from=node /app/node_modules ./node_modules
COPY package.json compose.meeting.image.yaml start-meeting-agent.sh ./
COPY .agents ./.agents
COPY docker ./docker
COPY packages ./packages
COPY control-panel ./control-panel
COPY meeting-runtime ./meeting-runtime
RUN ln -s /app/docker/colleague /usr/local/bin/colleague

# The release version (0.1.0); the workflow sets it, local builds say dev.
ARG VERSION=dev
LABEL org.opencontainers.image.version=${VERSION}
ENV PYTHONUNBUFFERED=1 \
    COLLEAGUE_ROOT=/data \
    COLLEAGUE_MEETING_DATA=/data/meetings \
    COLLEAGUE_MANAGED=1 \
    COLLEAGUE_MEETING_IMAGE=${MEETING_IMAGE} \
    COLLEAGUE_VERSION=${VERSION}
VOLUME /data
ENTRYPOINT ["/usr/bin/tini", "--", "/app/docker/entrypoint.sh"]
