FROM meeting-agent-joinly:local
USER root
# compose.meeting.yaml runs this image as the host user's uid, so the bundled
# browsers must not depend on the image's own app user.
RUN apt-get update && apt-get install -y --no-install-recommends novnc websockify && rm -rf /var/lib/apt/lists/* \
    && chmod 0755 /home/app
ENV PLAYWRIGHT_BROWSERS_PATH=/home/app/.cache/ms-playwright
USER app
