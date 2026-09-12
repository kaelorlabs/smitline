FROM meeting-agent-joinly:local
USER root
RUN apt-get update && apt-get install -y --no-install-recommends novnc websockify && rm -rf /var/lib/apt/lists/*
USER app
