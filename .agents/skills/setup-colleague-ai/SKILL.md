---
name: setup-colleague-ai
description: Set up Colleague AI for the user so their agent can place phone calls and join Zoom, Teams, or Google Meet. Drives `colleague setup status --json`, the local setup page, caller ID, the first test call, and agent registration. Use when the user asks to install, configure, or fix Colleague AI.
---

# Set up Colleague AI

Follow [SETUP.md](../../../SETUP.md) in the repository root. It is the canonical, step-by-step setup for agents. In short:

1. Check prerequisites: Node 22 and Docker. Colleague AI runs in Docker, so Python is not needed. Clone inside the Linux or macOS home directory (WSL2 on Windows), run `npm install`, and link `packages/cli/src/colleague.mjs` as `colleague`. An agent running on Windows runs each command with `wsl.exe -d Ubuntu --exec bash -lc '...'`.
2. Run `colleague setup status --json` and work through `next` in order: apply each `suggest` without asking, ask each `ask` question, run each `fix`.
3. Run `colleague setup secrets`. It opens a local page and returns at once. The user enters keys, their name, and phone details there, presses Done, and tells you. Never ask for keys in the chat.
4. Run `colleague setup start`, then `colleague setup call-me --wait` for the first call when `firstCallReady` is true. Offer `colleague setup voice --preview <name>` to try another voice.
5. Run `colleague setup register` last, then tell the user to restart the agent app.

## Invariants

- Keys stay in the ignored `.env`; never print them or write them elsewhere.
- Every phone call opens with the AI disclosure naming the user. Do not suggest removing it.
- Meetings: GPT-Live owns pauses, backchannels, and interruptions, and a host or participant mute is authoritative. Exact coding-agent continuity needs the real originating session id; never pass `last`, `latest`, or a URL hash.
- The optional operations console is `./start-control-panel.sh` on http://127.0.0.1:8095. It is not needed for the agent path.
- Fixture tests do not prove live call or meeting compatibility. Say so when reporting results.
