# Troubleshooting

| Symptom | Check |
| --- | --- |
| The console at http://127.0.0.1:8095 does not answer | On Docker Desktop, turn on host networking (Settings > Resources > Network > Enable host networking), then `docker restart smitline`. `docker logs smitline` shows whether the container started. |
| Agent is silent in a meeting | Address it directly, then inspect `floorState`, `microphoneState`, `stage`, and `/health`. Unmute in the meeting UI if a host muted it. If `microphoneState` is `blocked` in Zoom, the host disabled self-unmute: the host can click **Ask to unmute** on its tile, and Smitline accepts. |
| Agent cannot enter the meeting | Inspect the browser viewer for waiting-room, sign-in, passcode, or host-removal messages. Connect a Microsoft or Google account only when guest access is denied. |
| The first meeting takes a while to start | The meeting image (about 1.8 GB) is being downloaded. Later meetings start at once. From a checkout it is built instead. |
| Voice API rejects the session | Check account access to `gpt-live-1` and the configured backend model. GPT-Live needs billing on a paid API tier. |
| A phone call fails with "This network blocks Cloudflare tunnels" | The network (often venue, office or guest Wi-Fi) blocks outbound port 7844, which the quick tunnel needs; it fails within seconds instead of after the 90-second start timeout. Switch networks (a phone hotspot works) or set `SMITLINE_PUBLIC_URL`. Meetings still work. Before a demo, check a phone brief (`check_call_brief`, or `POST /v1/calls/check`): it tests the tunnel too. |
| A call is refused before it rings | The message says why: calling hours, the do-not-call list, too many calls to one number, or a premium-rate number. See [guardrails](phone.md#guardrails). |
| Daemon unauthorized | The token is in `.smitline/daemon.auth` (`/data/.smitline/daemon.auth` in the container) and changes each time the daemon starts; do not put it in a URL. Restart the daemon (`docker restart smitline`) to rotate it. |
| Other phone call problems | See the troubleshooting table in [SETUP.md](../SETUP.md#troubleshooting). |
