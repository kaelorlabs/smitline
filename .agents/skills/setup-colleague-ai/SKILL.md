---
name: setup-colleague-ai
description: Set up Colleague AI's local Zoom or Teams participant, continuous GPT-Live voice session, optional Codex tools, and control panel.
---

# Set up Colleague AI

Use the current private checkout. Read `README.md` and `docs/meeting-adapters.md` before setup. Reuse the vendored Joinly source; do not replace it with an upstream clone.

1. Verify Docker Desktop, host Python, Node, and the coding-agent CLI you enabled (Codex, Cursor `cursor-agent`, or Claude Code `claude`). See [docs/coding-providers.md](docs/coding-providers.md).
2. Install Node dependencies with `npm install` if missing.
3. Keep API credentials in ignored `.env`. Copy `meeting-runtime/meeting.env.example` to `.env.meeting` only if the latter does not exist. Never overwrite existing secrets or print them.
4. Run `./start-control-panel.sh`, open http://127.0.0.1:8095, provide a Zoom/Teams invite, choose tools and context, then run checks and start.
5. Connect Microsoft through the console's browser when needed. The account profile stays local and ignored. Stop account setup before starting a meeting.
6. Admission starts a continuous GPT-Live session. The agent defaults to silence and responds selectively to direct questions, explicit tasks, requested tool results, and established material corrections. It keeps the platform audio connection open while a local virtual microphone gate transmits silence.
7. GPT-Live owns pause, backchannel, and interruption behavior. Do not add a local speech classifier or silence delay. The virtual gate handles model-audio transport only. An external platform mute is authoritative and must not be reopened automatically.
8. Verify admission, continuous listening, a directly requested answer, interruption handling, and the saved transcript in an actual meeting. Fixture tests do not prove real meeting compatibility.

Direct runtime launch uses `./start-meeting-agent.sh`. The supported configuration and service names are generic. Historical local Whisper/Kokoro demos are not the product setup path.
