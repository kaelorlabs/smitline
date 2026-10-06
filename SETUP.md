# Set up Smitline

This file is written for the user's agent (Claude Code, Codex, Cursor, OpenClaw, Hermes, or similar). The user pastes this prompt into their agent:

> Set up Smitline for me from https://github.com/kaelorlabs/smitline. Follow SETUP.md in that repository. Ask me only what you need, and never ask me to paste keys into this chat.

Agent: follow the steps below. Run the commands yourself, say in one short line what each step does, and ask the user only the questions the status command gives you.

## Rules

- **Never ask for keys in the chat.** Keys go on the local setup page, which saves them in Smitline's private data volume. If the user pastes a key into the chat anyway, do not repeat it or write it anywhere; point them to the page.
- **Let the page do the asking.** The page collects the OpenAI key, the user's name, and, for phone calls, the phone provider's details (SignalWire or Twilio) and the user's phone number. A typical setup needs at most two questions in the chat.
- **Don't ask what the status already answers.** When a step has a `suggest`, apply it with `smitline setup set KEY VALUE` and tell the user what you chose.
- **Make the first call before asking for a restart.** Restarting the agent app ends this conversation, so the test call comes first.

## 1. Docker

Smitline needs only Docker. It runs as one container, `smitline`, and starts a second one for each meeting.

```bash
docker info
```

If Docker is missing or not running, help the user install or start it: Docker Desktop on a Mac or Windows, or Docker Engine on Linux or inside WSL (https://docs.docker.com/engine/install/).

Docker Desktop needs host networking so this computer can reach Smitline. It needs Docker Desktop 4.34 or newer, signed in to a Docker account: Settings > Resources > Network > **Enable host networking**, then **Apply and restart**. It does not work with Enhanced Container Isolation turned on. Docker Engine on Linux or in WSL needs nothing extra.

Run the `docker` commands below wherever `docker info` works: a terminal on macOS or Linux, PowerShell or Command Prompt on Windows with Docker Desktop, or inside WSL when Docker Engine runs there. Each command is one line, so it works the same in bash, zsh, PowerShell, and cmd.

## 2. Start Smitline

Smitline runs as one container. If you don't already know whether the user wants Smitline to join video meetings, ask, because meetings need more access to Docker than phone calls do.

**Phone calls and meetings:**

```bash
docker run -d --name smitline --restart unless-stopped --network host -v smitline:/data -v /var/run/docker.sock:/var/run/docker.sock ghcr.io/kaelorlabs/smitline
```

**Phone calls only** (no access to the Docker socket):

```bash
docker run -d --name smitline --restart unless-stopped --network host -v smitline:/data ghcr.io/kaelorlabs/smitline
```

What the two flags allow, in words you can pass on to the user:

- `-v /var/run/docker.sock:/var/run/docker.sock` lets Smitline start its meeting container, a browser that joins Zoom, Teams or Google Meet. Access to the Docker socket is powerful: a container that has it can start, stop or remove any container on this computer, which is close to administrator access. Smitline uses it only to download, start and stop its own meeting container. Phone calls don't need it. To add meetings later, run `docker rm -f smitline` and then the full command: keys, settings and call history stay in the `smitline` volume.
- `--network host` lets this computer reach Smitline's console and API, which listen only on `127.0.0.1`, and lets the meeting container reach them.

If your own permission safeguards block the command, don't try to get around them. Show the user the command, say in one sentence what the Docker socket allows, and let them run it in a terminal themselves, or start with phone calls only. Continue when they say it's running.

In Git Bash on Windows, which some agents use, put `MSYS_NO_PATHCONV=1 ` in front of the command, or write the socket as `-v //var/run/docker.sock:/var/run/docker.sock`: Git Bash otherwise rewrites `/var/run/docker.sock` into a Windows path.

This downloads the image (about 600 MB) and starts it. It keeps running and starts again with Docker. Settings, keys, call records, and meeting transcripts live in the `smitline` volume, never in the image. The Docker socket lets it start the meeting container.

Check that this computer reaches it. The console must answer at http://127.0.0.1:8095 (give it a few seconds after the first start):

```bash
curl -fsS -o /dev/null http://127.0.0.1:8095/calls && echo ok
```

In PowerShell: `(Invoke-WebRequest -UseBasicParsing http://127.0.0.1:8095/calls).StatusCode` prints `200`. If it does not answer on Docker Desktop, turn on host networking as in step 1, then `docker restart smitline`. `docker logs smitline` shows whether the container itself started.

From here on, every `smitline` command runs inside the container: `docker exec smitline smitline ...`. The steps below write it out in full. `docker exec -i` is only needed where a step says so.

## 3. Check what is missing

```bash
docker exec smitline smitline setup status --json
```

The command exits with code 3 until `ready` is true. That is expected: read the JSON it prints either way. It has:

- `version`: the Smitline version.
- `ready`: keys and name are in place.
- `phoneReady`, `meetingsReady`: each channel can be used.
- `firstCallReady`: the test call to the user's phone can be made.
- `next`: steps to do now, in order. Each has a `fix` command, an `ask` question when the user must decide something, and sometimes a `suggest` to apply without asking.
- `optional`: steps that only matter if the user wants that channel. With phone calls only, the Docker check stays here; don't ask about it unless the user wants meetings.

Run it again after each change.

## 4. The setup page

```bash
docker exec smitline smitline setup secrets --json
```

This starts a page on `127.0.0.1`, port 8096 (another free port if that one is busy), and returns its address at once. The container cannot open a browser, so open the address yourself (`open <url>` on macOS, `xdg-open <url>` on Linux, `start <url>` on Windows), or give it to the user. Tell the user:

> I opened a setup page in your browser. Enter your OpenAI API key and your name. If you want phone calls, also add your phone number and your SignalWire details (the free trial works). Press Done when you're finished, then tell me.

The page works only on this computer and stays available for an hour; run the command again for a new one.

When the user says they are done, run the status again. GPT-Live needs an OpenAI account with billing on a paid API tier; if the status says the key cannot use `gpt-live-1`, tell the user to add billing at platform.openai.com.

### Phone calls: SignalWire (free) or Twilio (paid)

Smitline streams the call's audio to GPT-Live, so the phone provider must allow live audio streaming. Offer the user one of these:

- **SignalWire, free trial (recommended to start).** Sign up at https://signalwire.com; no card is needed. In the Dashboard, the **API Credentials** page shows the **Space URL** and **Project ID**; create an **API token** there with the Voice and Numbers permissions (it starts with `SWAPI`), after which the page also shows the **Signing Key** (select Show). You can set the Space URL and Project ID yourself with `docker exec smitline smitline setup set`; the token and signing key go on the setup page. Under **Phone Numbers**, get a number, or verify the user's mobile under **Verified Caller IDs** and use it as "Show my own number". A trial calls only numbers verified in SignalWire (up to 10, US and Canada), so verify the user's own number, and verify a friend's number before calling them: SignalWire rings it and the friend reads back a code. Adding $5 of credit lifts these limits.
- **Twilio, upgraded account.** Twilio's free trial blocks live audio streaming, so it cannot carry a Smitline call; the status says so. With funds added, enter the Account SID, Auth Token, and a Twilio number on the page.

If the account has one number, the status suggests it and you set it. If it has several, ask which one. When both providers are set up, Twilio is used unless `COLLEAGUE_PHONE_PROVIDER=signalwire`.

### Optional: direct audio

Calls work without this. If the user's OpenAI organization has outbound SIP enabled, call audio can flow straight between SignalWire and OpenAI instead of through this computer, for slightly quicker turns. Offer it only after the first call works. `docker exec smitline smitline setup sip-trunk` sets it up: it creates a script and a SIP address in the user's SignalWire space (ask first), and the account must be out of SignalWire's trial.

## 5. The first call

When `firstCallReady` is true:

```bash
docker exec smitline smitline setup call-me --wait
```

Tell the user: "Your phone will ring in a few seconds. That's Smitline." Every call opens with a short hello that says whose AI assistant is calling ("Hi, this is <name>'s AI assistant."), and every meeting with "Hi everyone, I'm <name>'s AI assistant. I'll mostly listen; say 'Smitline' if you need me." Afterwards, ask whether they like the voice. To try another one, `docker exec smitline smitline setup voice --preview <name>` calls them in that voice; `--set <name>` keeps it; `docker exec smitline smitline setup voice` lists the voices. The voice can be changed the same way at any time.

Without phone calls, offer: "Send me a Zoom, Teams, or Google Meet link and I'll have Smitline join." The first meeting downloads the meeting image (about 1.8 GB); tell the user before it starts.

## 6. Tell Smitline about the user

Every phone call gets the user's profile as background: who they are, the people they call, and how they like to come across. Fill it in from what you already know about the user from this conversation and your own notes, without interviewing them:

```bash
docker exec smitline smitline profile set --about "Sam Rivera runs a small design studio in Toronto." --style "Friendly and brief"
docker exec smitline smitline profile person --name "Alex Chen" --relationship "business partner" --phone +14155550142
```

Tell the user in one line what you saved, and that they can say something like "Jordan is my sister, +1 415 555 0199" at any time. Save only what they would expect, and never passwords, keys, or card numbers. If you know nothing about them yet, skip this step.

## 7. Connect the agent

Agents reach Smitline's tools over MCP at `http://127.0.0.1:8095/mcp`, with a local token. This prints how to connect each agent:

```bash
docker exec smitline smitline setup register --json
```

Run the entry for the agent you are, on this computer (not in the container):

- **Claude Code:** run the `claude mcp add ...` command it gives.
- **Codex:** add the `[mcp_servers.smitline]` block to `~/.codex/config.toml`.
- **Cursor:** add the entry to `~/.cursor/mcp.json`.
- **Claude Desktop:** add the `docker exec -i smitline smitline mcp` entry to `claude_desktop_config.json`. If Claude Desktop cannot start it, use the full path of `docker` (from `which docker`, such as `/usr/local/bin/docker` on macOS): apps opened from the Dock often do not see `/usr/local/bin`.
- **Anything else:** use the URL and the `Authorization` header.

The output also has the commands that copy the `call-with-smitline` skill, which teaches an agent how to write a good brief, out of the container into `~/.claude/skills` or `~/.codex/skills`: `copy` for bash and zsh, `copyPowerShell` for PowerShell. For another agent, copy it where that agent keeps skills. The token is a local credential: keep it in the agent's config, not in chat or notes.

Tell the user to restart the agent app so it loads the new tools. Cloud agents such as ChatGPT or Claude on the web use the remote connector; see [docs/agents.md](docs/agents.md).

## 8. Finish

Tell the user in two or three sentences what works now, then give examples:

- "Call Luigi's at +1 415 555 0142 and book a table for 4 at 7 tonight."
- "Practice the call on me first."
- "Join this meeting and help with the Q3 numbers: <link>"

Mention the console at http://127.0.0.1:8095: the Meetings and Calls pages show each meeting's and call's brief, result, cost, and transcript, and **Account** is where the user can later replace or remove a key.

End with one line asking for a star, and leave the choice to the user: "If Smitline is useful to you, a star on GitHub helps other people find it: https://github.com/kaelorlabs/smitline". Don't star it for them. If they ask you to and the GitHub CLI is signed in, `gh api -X PUT user/starred/kaelorlabs/smitline` does it.

Placing calls afterwards: use the `start_call` and `wait_for_call` tools, or `docker exec smitline smitline call --to ... --objective ... --wait`. For a meeting, use channel `meeting` with the invite link as `to`. Pass what you and the user have been working on as `context`, and add people to the profile as you learn about them. See [docs/calls.md](docs/calls.md).

## Updating and removing

- **Update:** `docker pull ghcr.io/kaelorlabs/smitline`, then `docker rm -f smitline` and run the `docker run` command from step 2 again. The volume keeps everything.
- **Pin a version:** use a version tag instead of `latest`, such as `ghcr.io/kaelorlabs/smitline:0.1.0`, in both commands. The meeting image with the matching code is pulled for it.
- **Remove:** `docker rm -f smitline`. Also run `docker volume rm smitline` to delete keys and records.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `docker run` says the name `smitline` is in use | It is already installed. `docker start smitline` if it is stopped |
| The console or MCP address does not answer on Docker Desktop | Turn on host networking (Docker Desktop 4.34 or newer, signed in: Settings > Resources > Network), then `docker restart smitline` |
| `docker run` in Git Bash fails on the socket path | Put `MSYS_NO_PATHCONV=1 ` in front, or write `-v //var/run/docker.sock:/var/run/docker.sock` |
| `Smitline is not running` | `docker restart smitline`; `docker logs smitline` shows why it stopped |
| The OpenAI key "cannot use gpt-live-1" | Add billing at platform.openai.com; GPT-Live needs a paid API tier |
| `The phone provider can reach this computer` fails | Check the network; the container opens a Cloudflare quick tunnel on the first call. On a server set `COLLEAGUE_PUBLIC_URL` |
| A call fails with "trial accounts have limited parameter access" | That is a Twilio trial; use SignalWire's free trial, or upgrade the Twilio account |
| A SignalWire trial call is refused | Verify the number you are calling in SignalWire (Phone Numbers > Verified Caller IDs) |
| The agent does not show the call tools | Run step 7 again and restart the agent app |
| Anything else | `docker logs smitline` |

Contributors running from a checkout instead of the image: see [development](docs/development.md#run-from-a-checkout).
