# Colleague AI local control panel

The local control panel is the main operator interface for Colleague AI. It keeps meeting configuration, tool selection, supplied reference material, runtime status, and transcripts in one place.

## Before the first run

Install the local requirements from the repository root:

```bash
cp .env.example .env
cp meeting-runtime/meeting.env.example .env.meeting
chmod 600 .env .env.meeting
npm install
```

Add `OPENAI_API_KEY` to `.env`. Add `TAVILY_API_KEY` when web search will be enabled. Install Docker Desktop. Sign in to the coding-agent CLI you will enable (`codex login`, Cursor `cursor-agent`, or `claude login`) and confirm that login with the CLI's documented status command.

The control panel never returns API keys to the browser. `.env`, `.env.meeting`, uploaded context, meeting transcripts, job files, artifacts, profiles, and pairing hashes are ignored by Git.

## Start the console

```bash
bash start-control-panel.sh
```

## Follow calls

`http://127.0.0.1:8095/calls` (**Calls** in the console's top bar) lists recent phone calls and meetings started through the call API. Choosing one shows its status, the live transcript as it happens, and the result when it ends: the outcome and summary first, then details such as confirmation numbers, open questions, follow-ups, and what was agreed. While a phone call is connected, **Take over the call** rings `COLLEAGUE_OWNER_PHONE` and hands the call to you (press twice to confirm; Colleague AI leaves the call), and **End call** asks the assistant to wrap up and hang up. There is no listen-in; the live transcript is how you follow along. Reading calls needs no token, like the transcript views; the two actions require the console's token and a same-origin request. With no calls yet, the page suggests what to ask your agent.

Open [http://127.0.0.1:8095](http://127.0.0.1:8095). Keep the terminal open while operating the meeting agent.

## Configure a meeting

1. Paste the complete Zoom, Teams, or Google Meet invite URL. A URL containing Zoom's `pwd` parameter is supported. Google Meet codes must be the official `xxx-yyyy-zzz` form on `meet.google.com`.
2. Enter a passcode when Zoom requires one separately.
3. Choose the participant name and the default Codex model.
4. Enable only the tools needed for the meeting:
   - **Web search** uses the local `search_web` function backed by Tavily.
   - **Codex**, **Cursor**, or **Claude Code** — enable only one. The local portal always joins with context continuity (`local-portal`). Exact thread resume is only available through a host SDK/CLI/MCP integration that already has the real session id.
   - **Charts** allows Codex results to be rendered locally and requires Codex.
   - **Show in the meeting** (default on) publishes a virtual camera tile. Task text is never shown.
   - **Understand shared content** is off by default. It captures the meeting share surface at a low rate. Voice cannot enable it.
5. Optionally select an absolute workspace path. The selected coding-agent worker can inspect only this workspace. When it is blank, the worker uses the empty, ignored `meeting-runtime/codex-workspace` directory.
6. Optionally add short meeting instructions. Use these for the agent's role, terminology, response style, or meeting-specific boundaries. Do not use them for credentials.
7. Pair a hosted runner only if you are exercising the foundation control plane. Local loopback remains the supported path. The pairing code is shown once.
8. Run the checks, resolve any reported issue, and start the colleague.

The checks validate the meeting settings, required credentials, Docker availability, the selected workspace, and the enabled coding-agent CLI login.

## Supply private reference context

Paste text or upload TXT, Markdown, CSV, TSV, JSON, YAML, PDF, or DOCX files from the context section. The console extracts text locally and stores it in `meeting-runtime/context/index.json`. Individual files are limited to 8 MB, a batch can contain up to 10 files, and all saved extracted text is limited to 1,000,000 characters.

The meeting agent receives a `search_context` tool only when saved sources exist at voice-session startup. It retrieves matching passages and names the source document in its answer. Restart the meeting participant after adding or clearing context so the voice session receives the correct tool list.

Reference context is suited to supplied facts, policies, plans, product notes, customer information, tables, and company terminology. Use the Codex workspace for deeper analysis of structured data, code, or a larger project. Uploaded documents remain local unless their relevant contents are included in a model request while answering a meeting question.

Use **Clear saved context** when the material should no longer be available. This deletes the local context index; it does not alter the original documents.

## Operate the call

The console reports the join stage, platform microphone state, listening state, selected tools, runtime log, and recorded sessions. Admit **Colleague AI** from the waiting room when prompted. It listens continuously but is instructed to respond only when directly addressed, explicitly assigned a task, asked for a tool result, or able to establish an important factual correction. GPT-Live decides how to handle conversational pauses, backchannels, and interruptions; the virtual microphone transports its output without adding a local turn-taking delay.

The platform may display Colleague AI as unmuted because the runtime keeps the browser audio connection stable. No audio is transmitted while the local gate is closed. If a host or participant mutes Colleague AI in Zoom, Teams, or Google Meet, that mute is respected and the runtime will not override it automatically.

Stop the colleague from the console before starting another meeting or changing configuration. Starting a new voice session clears the voice model's conversation memory. Exact coding-agent resume happens only when a host integration supplied a real session id at join; the portal does not derive a thread from the meeting URL.

Pending approvals appear on the live console. Approve or deny each request individually. Workspace artifacts and git operations are listed there when present.

## Review local records

Each call creates a directory under `meeting-runtime/recordings/` containing the incremental transcript and event trace. The console lists recent sessions and can open their transcripts. Generated charts and supporting data are stored with the call record.

The transcript records recognized and generated text. It is not a raw-audio archive and may include a generated response that was muted or interrupted before participants heard it.

## Command-line operation

The lower-level launcher remains available for automation and debugging:

```bash
bash start-meeting-agent.sh
```

It reads `.env.meeting`, validates the configuration, builds and starts the Docker participant, and runs the host coding-agent worker when enabled. Runtime status is available at [http://127.0.0.1:8094/health](http://127.0.0.1:8094/health), and the browser viewer is available at [http://127.0.0.1:6082/vnc.html?autoconnect=true](http://127.0.0.1:6082/vnc.html?autoconnect=true).

## Troubleshooting

- If the console cannot start, run `npm install` and confirm Node.js 22 or later is active.
- If preflight reports Docker unavailable, start Docker Desktop and rerun the checks.
- If Codex is unavailable, run `codex login` or set `CODEX_BIN`. For Cursor, install `cursor-agent` and complete its official login. For Claude Code, run `claude login`.
- If the participant is silent, address it directly with a question or task and inspect `floorState`, `microphoneState`, the runtime log, and health state. If the platform microphone was externally muted, unmute it through the meeting UI before expecting audio.
- If the meeting does not admit the participant, inspect the browser viewer for a waiting-room, sign-in, passcode, or host-removal message.
- If supplied context is not available, add the source before starting the meeting or restart the participant after changing sources.
- If chart delivery fails, inspect the saved artifact in the call record. Direct Zoom chat attachment depends on host settings and Zoom web-client support.

See [adapter operation and usage](meeting-adapters.md) for Teams/Google account connection and the continuous GPT-Live listening limitation. See [architecture](architecture.md) for retention paths and [capabilities](capabilities.md) for platform and provider matrices.
