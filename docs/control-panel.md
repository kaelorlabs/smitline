# Colleague AI local console

The local console is an optional operator interface for Colleague AI. Agents do not need it: they place calls and join meetings through the calls API. The console has two tabs: **Meetings**, to start a meeting by hand and follow it, and **Calls**, to follow calls that agents started.

## Before the first run

Install the local requirements from the repository root:

```bash
cp .env.example .env
chmod 600 .env
npm install
```

Add `OPENAI_API_KEY` to `.env`, or run `colleague setup secrets` and use the setup page. Install Docker: Docker Desktop, or Docker Engine inside WSL on Windows.

The console never returns API keys to the browser. `.env`, `.env.meeting`, uploaded context, meeting transcripts, and browser profiles are ignored by Git.

## Start the console

```bash
bash start-control-panel.sh
```

Open [http://127.0.0.1:8095](http://127.0.0.1:8095). Keep the terminal open while you use it. The console starts the runtime daemon when it is not running.

## Calls

`http://127.0.0.1:8095/calls` (**Calls** in the console's top bar) lists recent phone calls and meetings started through the calls API, with spend totals for today, this month, and all time, and the average per call. Choosing one shows its brief, its status, the live transcript as it happens, and the result when it ends: the outcome and summary first, then details such as confirmation numbers, open questions, follow-ups, and what was agreed, and what the call cost. While a phone call is connected, **Take over the call** rings `COLLEAGUE_OWNER_PHONE` and hands the call to you (press twice to confirm; Colleague AI leaves the call), and **End call** asks the assistant to wrap up and hang up. There is no listen-in; the live transcript is how you follow along. Reading calls needs no token, like the transcript views; the two actions require the console's token and a same-origin request. With no calls yet, the page suggests what to ask your agent.

## Meetings

### Configure a meeting

1. Paste the complete Zoom, Teams, or Google Meet invite URL. A URL containing Zoom's `pwd` parameter is supported. Google Meet codes must be the official `xxx-yyyy-zzz` form on `meet.google.com`.
2. Enter a passcode when Zoom requires one separately.
3. Choose the name shown in the meeting.
4. Choose the camera. **Show in the meeting** (default on) publishes a virtual camera tile, and **Turn camera on after joining** starts it on. Add an avatar or logo if you like (PNG, JPEG, WebP, or SVG up to 80 KB). Task text is never shown.
5. Optionally add short meeting guidance: the agent's role, terminology, response style, or meeting-specific boundaries. Do not use it for credentials.
6. For a Teams or Google Meet meeting that needs an account, connect one first; see [meeting adapters](meeting-adapters.md).
7. Run the checks, resolve any reported issue, and start the colleague.

The checks validate the meeting settings, the OpenAI key, and Docker availability. The first meeting builds the meeting image, which takes a couple of minutes.

### Supply private reference context

Paste text or upload TXT, Markdown, CSV, TSV, JSON, YAML, PDF, or DOCX files under **Reference context**. The console extracts text locally and stores it in `meeting-runtime/context/index.json`. Individual files are limited to 8 MB, a batch can contain up to 10 files, and all saved extracted text is limited to 1,000,000 characters.

The saved sources go to the meeting as its starting context. GPT-Live gets them as background, and so does the backend model it hands harder questions to (`COLLEAGUE_MEETING_BACKEND_MODEL`). Longer material is cut to fit. The voice session reads the context when it starts, so add sources before starting the meeting, or restart the participant after changing them.

Use **Clear saved context** when the material should no longer be available. This deletes the local context index; it does not alter the original documents.

### Operate the meeting

The console reports the join stage, platform microphone state, listening state, and runtime log. Admit **Colleague AI** from the waiting room when prompted. It listens continuously but is instructed to respond only when directly addressed, explicitly assigned a task, or able to make an important factual correction. GPT-Live decides how to handle conversational pauses, backchannels, and interruptions; the virtual microphone transports its output without adding a local turn-taking delay.

The platform may display Colleague AI as unmuted because the runtime keeps the browser audio connection stable. No audio is transmitted while the local gate is closed. If a host or participant mutes Colleague AI in Zoom, Teams, or Google Meet, that mute is respected and the runtime will not override it automatically. In Zoom, a host who has disabled self-unmute can click **Ask to unmute** on Colleague AI's tile; it accepts that explicit request.

**Open meeting view** shows the container's browser. Stop the colleague from the console before starting another meeting or changing configuration. Starting a new voice session clears the voice model's conversation memory.

### Review local records

Each meeting creates a directory under `meeting-runtime/recordings/` containing the incremental transcript and event trace. **Transcripts** lists recent meetings, opens their transcripts, and downloads the handoff: the summary, decisions, action items, and open questions built from the transcript.

The transcript records recognized and generated text. It is not a raw-audio archive and may include a generated response that was muted or interrupted before participants heard it.

## Command-line operation

The lower-level launcher remains available for debugging:

```bash
cp meeting-runtime/meeting.env.example .env.meeting
bash start-meeting-agent.sh
```

It reads `.env.meeting`, checks the configuration with `python3`, and builds and starts the Docker participant. Runtime status is available at [http://127.0.0.1:8094/health](http://127.0.0.1:8094/health), and the browser viewer at [http://127.0.0.1:6082/vnc.html?autoconnect=true](http://127.0.0.1:6082/vnc.html?autoconnect=true).

## Troubleshooting

- If the console cannot start, run `npm install` and confirm Node.js 22 or later is active.
- If the checks report Docker unavailable, start Docker (Docker Desktop, or the Docker Engine service inside WSL) and rerun the checks.
- If the participant is silent, address it directly with a question or task and inspect `floorState`, `microphoneState`, the runtime log, and health state. If the platform microphone was externally muted, unmute it through the meeting UI before expecting audio; in Zoom the host can click **Ask to unmute** on its tile.
- If the meeting does not admit the participant, inspect the browser viewer for a waiting-room, sign-in, passcode, or host-removal message.
- If supplied context is not used, add the source before starting the meeting or restart the participant after changing sources.

See [meeting adapters](meeting-adapters.md) for Teams/Google account connection and the continuous GPT-Live listening limitation. See [architecture](architecture.md) for retention paths and [capabilities](capabilities.md) for the platform matrix.
