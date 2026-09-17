---
name: setup-colleague-ai
description: Set up Colleague AI's local Zoom, Teams, or Google Meet participant, continuous GPT-Live voice session, loopback daemon, optional Codex/Cursor/Claude Code tools, and control panel.
---

# Set up Colleague AI

Use the current private checkout. Read `README.md`, `docs/architecture.md`, and `docs/meeting-adapters.md` before setup. Reuse the vendored Joinly source; do not replace it with an upstream clone.

1. Verify Docker Desktop, host Python 3.10+, Node.js 22+, and the coding-agent CLI you enabled (Codex, Cursor `cursor-agent`, or Claude Code `claude`). See [docs/coding-providers.md](docs/coding-providers.md) and [docs/capabilities.md](docs/capabilities.md).
2. Install Node dependencies with `npm install` if missing.
3. Keep API credentials in ignored `.env`. Copy `meeting-runtime/meeting.env.example` to `.env.meeting` only if the latter does not exist. Use placeholders only. Never overwrite existing secrets or print them.
4. Run `./start-control-panel.sh`, open http://127.0.0.1:8095, provide a Zoom, Teams, or Google Meet invite, choose **one** coding agent, camera, optional incoming shared-content capture, then run checks and start. The portal uses context continuity (`local-portal`), not exact thread resume.
5. Connect Microsoft or Google through the console's browser when guest entry is blocked. The account profile stays local and ignored. Stop account setup before starting a meeting.
6. Admission starts a continuous GPT-Live session. The agent defaults to silence and responds selectively. The platform audio connection stays open while a local virtual microphone gate transmits silence.
7. GPT-Live owns pause, backchannel, and interruption behavior. Do not add a local speech classifier or silence delay. An external platform mute is authoritative and must not be reopened automatically.
8. Exact continuity is only for host SDK/CLI/MCP integrations that inject the real originating session id. Never pass `last`, `latest`, `--last`, or a URL hash.
9. Verify admission, continuous listening, a directly requested answer, interruption handling, and the saved transcript in an actual meeting. Fixture tests do not prove real meeting compatibility.

Direct runtime launch uses `./start-meeting-agent.sh`. The loopback daemon is `./start-runtime-daemon.sh` on `127.0.0.1:8765`. Supported names are generic: `meeting-runtime/`, `.env.meeting`, `MEETING_URL`, `compose.meeting.yaml`, service `meeting-agent`. Old Zoom-specific paths are not read; move existing jobs/workspace/context/recordings with the runtime directory.

Historical local Whisper/Kokoro demos (`live/`, `start-live.sh`) are not the product setup path.
