# Set up Colleague AI

This file is written for the user's coding agent (Claude Code, Codex, Cursor, OpenClaw, Hermes, or similar). The user pastes this prompt into their agent:

> Set up Colleague AI for me from https://github.com/kaelorlabs/colleague-ai. Follow SETUP.md in that repository. Ask me only what you need, and never ask me to paste keys into this chat.

Agent: follow the steps below. Run the commands yourself, say in one short line what each step does, and ask the user only the questions the status command gives you.

## Rules

- **Never ask for keys in the chat.** Keys go on the local setup page, which writes them to the ignored `.env` file. If the user pastes a key into the chat anyway, do not repeat it or write it anywhere; point them to the page.
- **Ask, don't guess.** Every missing item comes with the exact question to ask.
- **Keep it short.** Four questions at most for a working setup: OpenAI key, the user's name, and, for phone calls, Twilio details and the user's phone number.

## 1. Get the code onto a supported system

Colleague AI runs on macOS and Linux. On Windows it runs inside WSL2.

- **Windows without WSL:** ask the user to run `wsl --install -d Ubuntu` in an administrator PowerShell and restart. Continue inside Ubuntu.
- Clone inside the Linux or macOS home directory, not under `/mnt/c` or `/mnt/d`:

```bash
git clone https://github.com/kaelorlabs/colleague-ai.git ~/colleague-ai
cd ~/colleague-ai
npm install
```

Link the `colleague` command once:

```bash
mkdir -p ~/.local/bin && ln -sf ~/colleague-ai/packages/cli/src/colleague.mjs ~/.local/bin/colleague
```

If `~/.local/bin` is not on `PATH`, write `node ~/colleague-ai/packages/cli/src/colleague.mjs` wherever the steps below say `colleague`.

## 2. Check what is missing

```bash
colleague setup status --json
```

The result has `ready`, `phoneReady`, `meetingsReady`, and a `next` list. Work through `next` in order: ask the `ask` question if there is one, then run the `fix` command. Run the status command again after each change.

## 3. Keys: the local setup page

```bash
colleague setup secrets
```

This prints a one-time address on `127.0.0.1` and tries to open it in the browser. Tell the user: "I opened a setup page on your computer. Enter your OpenAI API key there, plus your Twilio details if you want phone calls. It saves to this computer only." The command finishes when they press Save and prints which fields were saved, never the values.

GPT-Live needs an OpenAI account with billing on a paid API tier. If the status says the key cannot use `gpt-live-1`, tell the user to add billing at platform.openai.com.

## 4. The user's name

Ask: "What name should I say I'm calling on behalf of?" Then:

```bash
colleague setup set COLLEAGUE_OWNER_NAME "<name>"
```

Every phone call opens with "Hi, I'm an AI assistant calling on behalf of <name>."

## 5. Phone calls (optional)

Ask: "Do you want phone calls too?" If not, skip to step 6.

1. The user needs a Twilio account (twilio.com). Its Account SID and Auth Token go on the setup page from step 3.
2. Ask: "Should calls come from a Twilio number, or show your own mobile number?"
   - **Twilio number:** `colleague setup set TWILIO_FROM_NUMBER +1...`
   - **Own mobile:** the user verifies it once in the Twilio console as a caller ID, then `colleague setup set COLLEAGUE_CALLER_ID +1...`. `TWILIO_FROM_NUMBER` must still be set.
3. Ask: "What's your phone number? I'll call it once to show setup works." Then `colleague setup set COLLEAGUE_OWNER_PHONE +1...`.

A trial Twilio account can only call verified numbers, which includes the user's own. Twilio reaches this computer through a Cloudflare quick tunnel that starts on the first call; that needs Docker or `cloudflared`.

## 6. Meetings

Joining Zoom, Teams, or Google Meet needs Docker. If `docker` fails in the status, help the user start Docker, or install it inside WSL.

## 7. Connect the agent

```bash
colleague setup register
```

This adds the Colleague AI MCP server to Claude Code, Codex, and Cursor when they are installed, and prints the command for other MCP clients. Tell the user to restart the agent app so it loads the new tools. Cloud agents such as ChatGPT or Claude on the web need the remote connector; see [docs/agents.md](docs/agents.md).

## 8. The first call

When `phoneReady` is true:

```bash
colleague setup call-me --wait
```

Tell the user: "Your phone will ring in a few seconds. That's Colleague AI." Afterwards, ask whether they liked the voice. To change it, run `colleague setup voice --set <name>`; `colleague setup voice` lists the voices.

Without phone calls, offer: "Send me a Zoom, Teams, or Google Meet link and I'll have Colleague AI join."

## 9. Finish

Tell the user in two or three sentences what works now, then give examples:

- "Call Luigi's at +1 415 555 0142 and book a table for 4 at 7 tonight."
- "Practice the call on me first."
- "Join this meeting and help with the Q3 numbers: <link>"

Placing calls afterwards: use the `start_call` and `wait_for_call` tools, or `colleague call --to ... --objective ... --wait`. See [docs/calls.md](docs/calls.md).
