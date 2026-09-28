---
name: setup-colleague-ai
description: Set up Colleague AI for the user so their agent can place phone calls and join Zoom, Teams, or Google Meet. Drives `colleague setup status --json`, the local key page, caller ID, agent registration, and the first test call. Use when the user asks to install, configure, or fix Colleague AI.
---

# Set up Colleague AI

Follow [SETUP.md](../../../SETUP.md) in the repository root. It is the canonical, step-by-step setup for agents. In short:

1. Clone inside the Linux or macOS home directory (WSL2 on Windows), run `npm install`, and link `packages/cli/src/colleague.mjs` as `colleague`.
2. Run `colleague setup status --json` and work through `next` in order: ask each `ask` question, run each `fix`.
3. Keys go only on the page from `colleague setup secrets`. Never ask for keys in the chat.
4. Set the user's name with `colleague setup set COLLEAGUE_OWNER_NAME "<name>"`. For phone calls, set the caller ID and the user's phone.
5. Run `colleague setup register` so the agent gets the call tools, then `colleague setup call-me --wait` for the first call.

## Invariants

- Keys stay in the ignored `.env`; never print them or write them elsewhere.
- Every phone call opens with the AI disclosure naming the user. Do not suggest removing it.
- Meetings: GPT-Live owns pauses, backchannels, and interruptions, and a host or participant mute is authoritative. Exact coding-agent continuity needs the real originating session id; never pass `last`, `latest`, or a URL hash.
- The optional operations console is `./start-control-panel.sh` on http://127.0.0.1:8095. It is not needed for the agent path.
- Fixture tests do not prove live call or meeting compatibility. Say so when reporting results.
