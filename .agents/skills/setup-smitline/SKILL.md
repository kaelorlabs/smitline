---
name: setup-smitline
description: Set up Smitline for the user so their agent can place phone calls and join Zoom, Teams, or Google Meet. Starts the Smitline Docker container, then drives `smitline setup status --json`, the local setup page, caller ID, the first test call, and agent registration. Use when the user asks to install, configure, or fix Smitline.
---

# Set up Smitline

Follow [SETUP.md](../../../SETUP.md) in the repository root (https://github.com/kaelorlabs/smitline/blob/main/SETUP.md). It is the canonical, step-by-step setup for agents, and it needs only Docker. In short:

1. Check Docker with `docker info`, then start the container with the one-line `docker run` command from SETUP.md step 2: with the Docker socket for phone calls and meetings, without it for phone calls only (ask if you don't know which the user wants). If your permission safeguards block it, show the user the command, say what the socket allows, and let them run it. Confirm the console answers at http://127.0.0.1:8095; on Docker Desktop that needs host networking turned on.
2. Every `smitline` command runs in the container: `docker exec smitline smitline ...`.
3. Run `docker exec smitline smitline setup status --json` (it exits with code 3 until `ready` is true; read the JSON anyway) and work through `next` in order: apply each `suggest` without asking, ask each `ask` question, run each `fix`.
4. Run `docker exec smitline smitline setup secrets --json`. It returns the address of a local page at once; open it for the user. The user enters keys, their name, and phone details there, presses Done, and tells you.
5. When `firstCallReady` is true, run `docker exec smitline smitline setup call-me --wait`. Offer `setup voice --preview <name>` to try another voice.
6. Run `docker exec smitline smitline setup register --json` last, apply the entry for your agent on this computer, then tell the user to restart the agent app.

## Rules

- Never ask for keys in the chat. Keys go on the local setup page, which saves them in the container's private data volume. If the user pastes a key anyway, do not repeat it or write it anywhere; point them to the page.
- Every phone call says during the call that it is the user's AI assistant; the opening names the user and the reason. Do not suggest removing it.
- Meetings: GPT-Live owns pauses, backchannels, and interruptions, and a host or participant mute is authoritative. An agent joins a meeting with `start_call` on the `meeting` channel, the invite URL as `to`.
- The console at http://127.0.0.1:8095 is optional; the agent path does not need it.
- Fixture tests do not prove live call or meeting compatibility. Say so when reporting results.
