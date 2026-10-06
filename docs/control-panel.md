# Smitline local console

The local console is an optional operator interface for Smitline. Agents do not need it: they place calls and join meetings through the calls API. The console has three sections: **Meetings** and **Calls**, to follow what your agents did, and **Account**, for keys and settings. On a wide screen they sit in a sidebar with Account at the bottom; on a narrow one, in the top bar.

## Open the console

The `smitline` container serves it: open [http://127.0.0.1:8095](http://127.0.0.1:8095). Add your keys under **Account**, or with `docker exec smitline smitline setup secrets` (see [SETUP.md](../SETUP.md)).

The console never returns API keys to the browser. Keys, uploaded context, meeting transcripts, and browser profiles stay in the container's data volume.

From a checkout, run `npm install`, then `bash start-control-panel.sh`, and keep the terminal open; it starts the runtime daemon when it is not running, and the files above stay in the checkout, ignored by Git.

## Meetings

`http://127.0.0.1:8095` opens on the meetings Smitline joined through the calls API: totals for today, this month, and all time, the average length and cost per meeting, and a list showing each meeting's platform, time, length, and whether it met its goal (**Goal met** for an `achieved` result, **Goal partly met** for `partial`, **Goal not met** otherwise). Choosing one shows its objective and brief, the outcome and summary, decisions, action items, open questions, the cost, and the full transcript, read from the meeting's local archive when the result does not carry it, with **Download handoff**. While a meeting runs, **Leave meeting** asks Smitline to leave. Totals need a daemon that knows `GET /v1/calls?channel=`; with an older one the list still works and the totals are hidden.

**Start a meeting manually** (`/meetings/new`) is for a one-off meeting; usually your agent starts them. It starts the meeting through the calls API with the objective you give, so it is listed and judged like any other.

## Calls

`http://127.0.0.1:8095/calls` lists recent phone calls started through the calls API, with spend totals for today, this month, and all time, and the average per call. Choosing one shows its brief, its status, the live transcript as it happens, and the result when it ends: the outcome and summary first, then details such as confirmation numbers, open questions, follow-ups, and what was agreed, and what the call cost. While a phone call is connected, **Take over the call** rings `COLLEAGUE_OWNER_PHONE` and hands the call to you (press twice to confirm; Smitline leaves the call), and **End call** asks the assistant to wrap up and hang up. There is no listen-in; the live transcript is how you follow along. Reading calls needs no token, like the transcript views; the two actions require the console's token and a same-origin request. With no calls yet, the page suggests what to ask your agent.

## Account

`http://127.0.0.1:8095/setup` (**Account**) shows the checklist `smitline setup status` prints, with each item OK, needing a fix, or not checked, and every key and setting from the setup page. It verifies the keys online when it opens and on **Check again**.

- **Replace** (or **Add**) saves a new value, checks it again, and says what the check found, such as "key works and has GPT-Live access" or "OpenAI rejected the key". Values are validated as on the setup page.
- **Remove** deletes Smitline's saved copy from `.env`. It does not revoke the key at OpenAI or the phone provider; the page links to where to revoke it, such as platform.openai.com/api-keys.
- A saved key shows as **Saved**, with at most its known prefix (`sk-proj-`, `AC`, `PT`); keys are never returned to the browser. A value set in the container's environment overrides `.env` and can only be changed there.
- Reading the page takes the console's session token; saving and removing take the token and a request from the console's own address, so other websites cannot change keys.

`smitline setup secrets` still works and saves to the same place.

## Starting a meeting manually

### Configure a meeting

1. Paste the complete Zoom, Teams, or Google Meet invite URL. A URL containing Zoom's `pwd` parameter is supported. Google Meet codes must be the official `xxx-yyyy-zzz` form on `meet.google.com`.
2. Say what the meeting should achieve (**Objective**). The Meetings page shows whether it got there.
3. Enter a passcode when Zoom requires one separately.
4. Choose the name shown in the meeting.
5. Choose the camera. **Show in the meeting** (default on) publishes a virtual camera tile, and **Turn camera on after joining** starts it on. Add an avatar or logo if you like (PNG, JPEG, WebP, or SVG up to 80 KB). Task text is never shown.
6. Optionally add short meeting guidance: the agent's role, terminology, response style, or meeting-specific boundaries. Do not use it for credentials.
7. For a Teams or Google Meet meeting that needs an account, connect one first; see [meeting adapters](meeting-adapters.md).
8. Run the checks, resolve any reported issue, and start Smitline. The meeting starts through the calls API with your objective and guidance as its brief; the saved reference context follows once the meeting exists. **Stop Smitline** ends the call, which then writes its result.

The checks validate the meeting settings, the OpenAI key, and Docker availability. The first meeting downloads the meeting image (about 1.8 GB), which takes a few minutes; from a checkout it is built instead.

### Supply private reference context

Paste text or upload TXT, Markdown, CSV, TSV, JSON, YAML, PDF, or DOCX files under **Reference context**. The console extracts text locally and stores it in `context/index.json` under the meeting data (`/data/meetings` in the `smitline` volume, `meeting-runtime/` in a checkout). Individual files are limited to 8 MB, a batch can contain up to 10 files, and all saved extracted text is limited to 1,000,000 characters.

The saved sources go to the meeting as its starting context. GPT-Live gets them as background, and so does the backend model it hands harder questions to (`COLLEAGUE_MEETING_BACKEND_MODEL`). Longer material is cut to fit. The voice session reads the context when it starts, so add sources before starting the meeting, or restart the participant after changing them.

Use **Clear saved context** when the material should no longer be available. This deletes the local context index; it does not alter the original documents.

### Operate the meeting

The console reports the join stage, platform microphone state, listening state, and runtime log. Admit **Smitline** from the waiting room when prompted. It listens continuously but is instructed to respond only when directly addressed, explicitly assigned a task, or able to make an important factual correction. GPT-Live decides how to handle conversational pauses, backchannels, and interruptions; the virtual microphone transports its output without adding a local turn-taking delay.

The platform may display Smitline as unmuted because the runtime keeps the browser audio connection stable. No audio is transmitted while the local gate is closed. If a host or participant mutes Smitline in Zoom, Teams, or Google Meet, that mute is respected and the runtime will not override it automatically. In Zoom, a host who has disabled self-unmute can click **Ask to unmute** on Smitline's tile; it accepts that explicit request.

**Open meeting view** shows the container's browser. Stop Smitline from the console before starting another meeting or changing configuration. Starting a new voice session clears the voice model's conversation memory.

### Review local records

Each meeting creates a directory under `recordings/` in the meeting data (`/data/meetings` in the volume, `meeting-runtime/` in a checkout) containing the incremental transcript and event trace. The Meetings page shows each meeting's transcript and handoff. The **Transcripts** tab of the manual page still lists every recording there, including meetings started by hand before they went through the calls API, opens their transcripts, and downloads the handoff: the summary, decisions, action items, and open questions built from the transcript.

The transcript records recognized and generated text. It is not a raw-audio archive and may include a generated response that was muted or interrupted before participants heard it.

## Command-line operation

From a checkout, the lower-level launcher remains available for debugging:

```bash
cp meeting-runtime/meeting.env.example .env.meeting
bash start-meeting-agent.sh
```

It reads `.env.meeting`, checks the configuration with `python3`, and builds and starts the Docker participant. Runtime status is available at [http://127.0.0.1:8094/health](http://127.0.0.1:8094/health), and the browser viewer at [http://127.0.0.1:6082/vnc.html?autoconnect=true](http://127.0.0.1:6082/vnc.html?autoconnect=true).

## Troubleshooting

- If the console does not answer, check `docker ps` for the `smitline` container and `docker logs smitline`. From a checkout, run `npm install` and confirm Node.js 22 or later is active.
- If the checks report Docker unavailable, start Docker (Docker Desktop, or the Docker Engine service inside WSL) and rerun the checks.
- If the participant is silent, address it directly with a question or task and inspect `floorState`, `microphoneState`, the runtime log, and health state. If the platform microphone was externally muted, unmute it through the meeting UI before expecting audio; in Zoom the host can click **Ask to unmute** on its tile.
- If the meeting does not admit the participant, inspect the browser viewer for a waiting-room, sign-in, passcode, or host-removal message.
- If supplied context is not used, add the source before starting the meeting or restart the participant after changing sources.

See [meeting adapters](meeting-adapters.md) for Teams/Google account connection and the continuous GPT-Live listening limitation. See [architecture](architecture.md) for retention paths and [capabilities](capabilities.md) for the platform matrix.
