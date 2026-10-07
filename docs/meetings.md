# Meetings

Smitline joins Zoom, Microsoft Teams, and Google Meet meetings as a participant that listens and speaks. An agent starts a meeting the same way as a phone call, and you can also start one by hand in the console.

## Join a meeting from an agent

An agent joins a meeting through the calls API: `start_call` with `channel: "meeting"` and `to` set to the Zoom, Teams, or Google Meet link. From the CLI:

```bash
docker exec smitline smitline call --meeting "https://us05web.zoom.us/j/YOUR_MEETING_ID" \
  --objective "Help with the Q3 numbers" --wait
```

`--channel meeting --to <url>` does the same, and so does a `--to` that starts with `http://` or `https://`. The result comes back like a phone call's. See [calls](calls.md).

The first meeting downloads the meeting image, `ghcr.io/kaelorlabs/smitline-meeting` (about 1.8 GB of disk). From a checkout it is built instead.

Admit **Smitline** if it enters the waiting room. It unmutes its meeting microphone once, says a short AI disclosure naming who it acts for, then listens continuously. It answers when someone addresses it and hands harder questions to the backend model (`COLLEAGUE_MEETING_BACKEND_MODEL`, default `gpt-5.6-terra`; `COLLEAGUE_MEETING_WEB_SEARCH=1` adds OpenAI web search). Between replies a local audio gate sends silence, so the platform shows it unmuted; it mutes the microphone when it leaves. `docker exec smitline smitline setup set COLLEAGUE_MEETING_INTRO 0` skips the disclosure; the backend settings are set the same way.

## The meetings console

Open [http://127.0.0.1:8095](http://127.0.0.1:8095); the `smitline` container serves it. **Meetings** lists the meetings Smitline joined, with whether each met its goal, its summary, decisions, action items, and full transcript. **Start a meeting manually** starts one by hand: paste the meeting link and say what it should achieve, add private reference context from text or files (TXT, Markdown, CSV, JSON, YAML, PDF, DOCX), choose the camera, connect a Microsoft or Google account when a Teams or Meet meeting needs one, then start and stop Smitline and follow its live status. **Calls** lists phone calls, and **Account** holds your keys and the setup checklist. See the [control panel guide](control-panel.md) and [meeting adapters](meeting-adapters.md).

The console talks to the **loopback daemon** in the same container. API keys are never returned to the browser.

| Local interface | Address |
| --- | --- |
| Meetings console | http://127.0.0.1:8095 (start one by hand: /meetings/new) |
| MCP for local agents (bearer token from `smitline setup register`) | http://127.0.0.1:8095/mcp |
| Calls (live transcript, results, costs, take over) | http://127.0.0.1:8095/calls |
| Account (keys and setup checklist) | http://127.0.0.1:8095/setup |
| Meeting browser viewer | http://127.0.0.1:6082/vnc.html?autoconnect=true |
| Meeting status and transcript | http://127.0.0.1:8094/health |
| Runtime daemon (loopback) | http://127.0.0.1:8765 |

A `live` health status means the bridge reached the meeting audio loop. Verify a spoken exchange to confirm the complete audio path.

## Appearing in the meeting

A virtual camera shows Smitline listening, working, or speaking, and never displays task text. Turn it off in the console to join audio-only. By default the camera is a still picture that changes with the state, which costs almost no CPU. `COLLEAGUE_MEETING_CAMERA_STYLE=animated` switches to the animated camera, which redraws at 30 frames a second and needs about a full CPU core more. Private context added in the console reaches the voice and its backend as background.

## Debugging from a checkout

To launch a meeting participant without the daemon:

```bash
cp meeting-runtime/meeting.env.example .env.meeting
chmod 600 .env.meeting
bash start-meeting-agent.sh
```

It reads `.env.meeting` (`MEETING_URL`, `MEETING_PASSCODE`, the participant name, and the backend settings), checks it with `python3`, and starts the container. Stop it with:

```bash
docker compose -f compose.meeting.yaml stop meeting-agent
```
