# Set up Colleague AI

This file is written for the user's coding agent (Claude Code, Codex, Cursor, OpenClaw, Hermes, or similar). The user pastes this prompt into their agent:

> Set up Colleague AI for me from https://github.com/kaelorlabs/colleague-ai. Follow SETUP.md in that repository. Ask me only what you need, and never ask me to paste keys into this chat.

Agent: follow the steps below. Run the commands yourself, say in one short line what each step does, and ask the user only the questions the status command gives you.

## Rules

- **Never ask for keys in the chat.** Keys go on the local setup page, which writes them to the ignored `.env` file. If the user pastes a key into the chat anyway, do not repeat it or write it anywhere; point them to the page.
- **Let the page do the asking.** The page collects the OpenAI key, the user's name, and, for phone calls, the phone provider's details (SignalWire or Twilio) and the user's phone number. A typical setup needs at most two questions in the chat.
- **Don't ask what the status already answers.** When a step has a `suggest`, apply it with `colleague setup set KEY VALUE` and tell the user what you chose.
- **Make the first call before asking for a restart.** Restarting the agent app ends this conversation, so the test call comes first.

## 1. Prerequisites

Colleague AI runs on macOS and Linux. On Windows it runs inside WSL2 (Ubuntu).

| Needed for | What | Check |
|---|---|---|
| Everything | Node.js 22 or newer | `node --version` |
| Everything | Docker running (Docker Engine in WSL or Linux, or Docker Desktop) | `docker info` |

That is all. Colleague AI runs in Docker: the first start builds a small image with Python and the phone tunnel (`cloudflared`) inside, so nothing else needs installing. If this computer already has Python 3.10 or newer with `venv`, Colleague AI uses it instead; that is only required to hand meeting work to Codex, Cursor, or Claude Code, which run on this computer with the user's own logins.

If Docker is missing, help the user install it: Docker Engine inside WSL or Linux (https://docs.docker.com/engine/install/), or Docker Desktop on a Mac. Docker Desktop needs host networking turned on (Settings > Resources > Network) so this computer can reach Colleague AI.

### When your agent runs on Windows

If you (the agent) run on Windows rather than inside WSL, run every command in this file inside Ubuntu:

```bash
wsl.exe -d Ubuntu --exec bash -lc 'cd ~/colleague-ai && colleague setup status --json'
```

Use `--exec` and single quotes around the whole Linux command, so Windows and the Linux login shell do not expand `$VARIABLES` or strip quotes before the command runs. For a value with spaces, put double quotes inside the single quotes: `'colleague setup set COLLEAGUE_OWNER_NAME "Sam Rivera"'`.

If WSL is not installed, ask the user to run `wsl --install -d Ubuntu` in an administrator PowerShell and restart, then continue.

## 2. Get the code

Clone inside the Linux or macOS home directory, not under `/mnt/c` or `/mnt/d`:

```bash
git clone https://github.com/kaelorlabs/colleague-ai.git ~/colleague-ai
cd ~/colleague-ai
npm install
mkdir -p ~/.local/bin && ln -sf ~/colleague-ai/packages/cli/src/colleague.mjs ~/.local/bin/colleague
```

If `~/.local/bin` is not on `PATH`, write `node ~/colleague-ai/packages/cli/src/colleague.mjs` wherever the steps below say `colleague`.

## 3. Check what is missing

```bash
colleague setup status --json
```

The result has:

- `ready`: keys and name are in place.
- `phoneReady`, `meetingsReady`: each channel can be used.
- `firstCallReady`: the test call to the user's phone can be made.
- `next`: steps to do now, in order. Each has a `fix` command, an `ask` question when the user must decide something, and sometimes a `suggest` to apply without asking.
- `optional`: steps that only matter if the user wants that channel.

Run it again after each change.

## 4. The setup page

```bash
colleague setup secrets
```

This starts a page on `127.0.0.1`, opens it in the browser, and returns at once with the address. Tell the user:

> I opened a setup page in your browser. Enter your OpenAI API key and your name. If you want phone calls, also add your phone number and your SignalWire details (the free trial works). Press Done when you're finished, then tell me.

If the browser did not open (`"opened": false`), give the user the address. It works only on this computer and stays available for an hour; run the command again for a new one.

When the user says they are done, run the status again. GPT-Live needs an OpenAI account with billing on a paid API tier; if the status says the key cannot use `gpt-live-1`, tell the user to add billing at platform.openai.com.

### Phone calls: SignalWire (free) or Twilio (paid)

Colleague AI streams the call's audio to GPT-Live, so the phone provider must allow live audio streaming. Offer the user one of these:

- **SignalWire, free trial (recommended to start).** Sign up at https://signalwire.com; no card is needed. In the Dashboard, the **API Credentials** page shows the **Space URL** and **Project ID**; create an **API token** there with the Voice and Numbers permissions (it starts with `SWAPI`), after which the page also shows the **Signing Key** (select Show). The agent can set the Space URL and Project ID itself with `colleague setup set`; the token and signing key go on the setup page. All four go on the setup page. Under **Phone Numbers**, get a number, or verify the user's mobile under **Verified Caller IDs** and use it as "Show my own number". A trial calls only numbers verified in SignalWire (up to 10, US and Canada), so verify the user's own number, and verify a friend's number before calling them: SignalWire rings it and the friend reads back a code. Adding $5 of credit lifts these limits.
- **Twilio, upgraded account.** Twilio's free trial blocks live audio streaming, so it cannot carry a Colleague AI call; the status says so. With funds added, enter the Account SID, Auth Token, and a Twilio number on the page.

If the account has one number, the status suggests it and you set it. If it has several, ask which one. When both providers are set up, Twilio is used unless `COLLEAGUE_PHONE_PROVIDER=signalwire`.

### Optional: direct audio

Calls work without this. If the user's OpenAI organization has outbound SIP enabled, call audio can flow straight between SignalWire and OpenAI instead of through this computer, for slightly quicker turns. Offer it only after the first call works. `colleague setup sip-trunk` sets it up: it creates a script and a SIP address in the user's SignalWire space (ask first), and the account must be out of SignalWire's trial. While OpenAI does not allow outbound SIP, calls go through this computer as before.

## 5. Start Colleague AI

```bash
colleague setup start
```

The first start builds the Colleague AI image (about a minute) and then starts it in the background; the command shows progress and prints the error if something is missing. It keeps running until the computer restarts; later calls start it again automatically.

## 6. The first call

When `firstCallReady` is true:

```bash
colleague setup call-me --wait
```

Tell the user: "Your phone will ring in a few seconds. That's Colleague AI." Every call opens with a short hello that says whose AI assistant is calling ("Hi, this is <name>'s AI assistant."), and every meeting with "Hi everyone, I'm <name>'s AI assistant. I'll mostly listen; say 'Colleague' if you need me." Afterwards, ask whether they like the voice. To try another one, `colleague setup voice --preview <name>` calls them in that voice; `colleague setup voice --set <name>` keeps it; `colleague setup voice` lists the voices. The voice can be changed the same way at any time.

Without phone calls, offer: "Send me a Zoom, Teams, or Google Meet link and I'll have Colleague AI join." Meetings need Docker running. The first meeting builds the meeting image, which can take several minutes; tell the user before it starts.

## 7. Tell Colleague AI about the user

Every phone call gets the user's profile as background: who they are, the people they call, and how they like to come across. Fill it in from what you already know about the user from this conversation and your own notes, without interviewing them:

```bash
colleague profile set --about "Sam Rivera runs a small design studio in Toronto." --style "Friendly and brief"
colleague profile person --name "Alex Chen" --relationship "business partner" --phone +14155550142
```

Tell the user in one line what you saved, and that they can say something like "Maya is my sister, +1 415 555 0199" at any time. Save only what they would expect, and never passwords, keys, or card numbers. If you know nothing about them yet, skip this step.

## 8. Connect the agent

```bash
colleague setup register
```

This adds the Colleague AI MCP server to Claude Code, Codex, and Cursor when they are installed. From WSL it also adds it to Claude Desktop, Cursor, and Claude Code on the Windows side, and it installs the call and meeting skills. It prints the command for other MCP clients. Tell the user to restart the agent app so it loads the new tools. Cloud agents such as ChatGPT or Claude on the web use the remote connector; see [docs/agents.md](docs/agents.md).

## 9. Finish

Tell the user in two or three sentences what works now, then give examples:

- "Call Luigi's at +1 415 555 0142 and book a table for 4 at 7 tonight."
- "Practice the call on me first."
- "Join this meeting and help with the Q3 numbers: <link>"

Placing calls afterwards: use the `start_call` and `wait_for_call` tools, or `colleague call --to ... --objective ... --wait`. Pass what you and the user have been working on as `context`, and add people to the profile as you learn about them. See [docs/calls.md](docs/calls.md).

## Troubleshooting

| Symptom | Fix |
|---|---|
| `Colleague AI needs Docker (recommended) or Python` | Start Docker (`sudo service docker start` in WSL, or open Docker Desktop), then `colleague setup start` |
| Want Python on this computer instead of Docker | `sudo apt install -y python3-venv`; the next start uses it. `COLLEAGUE_DAEMON_RUNTIME=docker` or `host` forces one |
| The OpenAI key "cannot use gpt-live-1" | Add billing at platform.openai.com; GPT-Live needs a paid API tier |
| `The phone provider can reach this computer` fails | Start Docker (the image includes `cloudflared`), or install `cloudflared`; on a server set `COLLEAGUE_PUBLIC_URL` |
| A call fails with "trial accounts have limited parameter access" | That is a Twilio trial; use SignalWire's free trial, or upgrade the Twilio account |
| A SignalWire trial call is refused | Verify the number you are calling in SignalWire (Phone Numbers > Verified Caller IDs) |
| Calls fail with a tunnel error | Check the network and try again; the tunnel restarts on the next call |
| The agent does not show the call tools | Run `colleague setup register` and restart the agent app |
| Anything else | `.colleague/daemon.log` in the checkout has the daemon's log |
