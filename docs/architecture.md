# Architecture

Colleague AI is a **local-first** runtime that gives any agent phone calls and meetings. The loopback daemon on `127.0.0.1` is the supported path. OpenAI GPT-Live (`gpt-live-1`) is the only voice: it does all listening and speaking. Browser profiles, transcripts, call records, and credentials stay on this computer.

See [capabilities](capabilities.md) for the meeting platform matrix, [calls](calls.md) for the brief and result, and [phone calls](phone.md) for how a phone call runs.

## Runtime pieces

```text
Your agent (Claude Code, Codex, Cursor, ChatGPT, your own code, ...)
        │
        ▼
MCP server / remote connector / CLI / SDKs / REST
        │  brief in, result out
        ▼
Loopback daemon 127.0.0.1:8765
  ├─ calls API        /v1/calls, /v1/voices, /v1/profile
  ├─ phone gateway    127.0.0.1:8766 ──► SignalWire or Twilio ◄──► phone
  │                                       (audio relayed to GPT-Live, or direct over SIP)
  └─ meetings         /v1/meetings ──► meeting container (Docker)
                                          browser + PulseAudio + Xvfb + virtual camera
                                          ◄──► Zoom / Teams / Meet web client
                                          ◄──► GPT-Live (one gpt-live-1 session)
        ▲
Local console 127.0.0.1:8095 (Meetings and Calls tabs)
```

A phone call and a meeting are both **calls**: an agent sends a brief to `/v1/calls` with channel `phone` or `meeting`, and reads the same kind of result. A meeting call starts an ordinary daemon meeting underneath. The console uses the small `/v1/meetings` API directly when you start a meeting by hand.

In both, GPT-Live hands questions that need careful reasoning or precise facts to a backend model through Responses delegation. The backend knows the brief and context. Phone calls use `COLLEAGUE_PHONE_BACKEND_MODEL` and `COLLEAGUE_PHONE_WEB_SEARCH`; meetings use `COLLEAGUE_MEETING_BACKEND_MODEL` and `COLLEAGUE_MEETING_WEB_SEARCH`. Both models default to `gpt-5.6-terra`, and web search is off unless set to `1`.

### The meeting container

One image, `colleague-meeting:local`, built from `Dockerfile.meeting`: `python:3.12-slim-bookworm` with Playwright Chromium, PulseAudio, Xvfb, and x11vnc with noVNC for account sign-in. The daemon starts it with `docker compose -f compose.meeting.yaml up -d --build meeting-agent`, so the first meeting builds it (a couple of minutes, about 1.8 GB on disk). `meeting-runtime/` and the vendored `joinly/` subset are mounted read-only at run time, so code changes need no rebuild. No speech models run in the container.

### Where the daemon runs

`start-runtime-daemon.sh` runs the daemon on this computer's Python when it can create the daemon's venv (Python 3.10+ with `venv`), and otherwise in Docker (`Dockerfile.daemon`). `COLLEAGUE_DAEMON_RUNTIME=host` or `docker` picks one. Docker is enough; nothing needs host Python.

In Docker the container uses host networking, so the daemon and phone gateway listen on this computer's loopback exactly as they do without Docker. The checkout is mounted at the same path and the container runs as the host user, so `.colleague/`, `.env`, and call records stay the user's own files, and the meeting containers the daemon starts through the host's Docker socket see the same paths. The image holds only Python, aiohttp, the Docker CLI, and `cloudflared` for the phone tunnel; the code is mounted, so code changes need no rebuild, and the image is rebuilt only when `Dockerfile.daemon` or `requirements-daemon.txt` change. Settings exported in the shell reach the container by name, never by value on the command line. Docker Desktop needs host networking turned on for this computer to reach the daemon.

## Joining a meeting

```mermaid
sequenceDiagram
    participant Agent as Agent (MCP/CLI/SDK)
    participant Daemon as Loopback daemon
    participant Super as Meeting supervisor
    participant Bot as Meeting container
    participant Meet as Zoom/Teams/Meet
    participant Live as GPT-Live
    Agent->>Daemon: POST /v1/calls (channel meeting, to = invite URL)
    Daemon->>Super: start meeting (context from the brief)
    Super->>Bot: docker compose up --build meeting-agent
    Bot->>Meet: platform adapter opens the invitation and joins (guest first)
    alt waiting room
        Bot-->>Daemon: waiting_for_admission
        Meet->>Bot: host admits
    end
    Bot->>Meet: connect computer audio
    Bot->>Live: start one gpt-live-1 session (Responses delegation)
    Bot-->>Daemon: live
    Note over Daemon: meeting ends or the agent calls end_call
    Daemon-->>Agent: result from the meeting handoff (summary, decisions, action items, transcript)
```

The brief's context, questions, and limits become the meeting's starting context. The voice session and its backend read it when the session starts. The meeting result is built from the local transcript into a handoff, without a model call.

## Selective speech (platform microphone and virtual gate)

```mermaid
sequenceDiagram
    participant Live as GPT-Live
    participant Gate as Virtual microphone gate
    participant Adapter as Platform adapter
    participant Meet as Meeting toolbar
    Adapter->>Meet: unmute once when the voice session starts
    Live->>Gate: AI disclosure, then only selected replies
    Gate->>Meet: speech (gate open)
    Gate->>Meet: silence after each reply (gate closed, no toolbar click)
    Note over Meet: Host/participant mute is authoritative and is not auto-reopened
    Meet-->>Adapter: Zoom host clicks "Ask to unmute"
    Adapter->>Meet: accept that explicit request, then arm the gate again
    Adapter->>Meet: mute when the session ends
```

The platform microphone is unmuted once when the voice session starts and muted when it ends. Between replies only the local virtual gate closes, so participants hear silence while the platform shows Colleague AI as unmuted. If a host or participant mutes it, generated audio is discarded and the runtime never reopens the microphone on its own. If the host disables "Allow participants to unmute themselves" in Zoom, the start-of-session unmute fails and Colleague AI cannot speak until the host asks it to unmute; the Zoom adapter accepts the host's "The host would like you to unmute" dialog, because that is the host's explicit request.

Once the microphone can carry speech, the session speaks one short AI disclosure naming the person it acts for ("Hi everyone, I'm NAME's AI assistant. I'll mostly listen; say 'Colleague' if you need me."), then returns to listening. The name comes from the runtime state (`onBehalfOf`, or the call task "Take part in this meeting on behalf of NAME."), then `COLLEAGUE_OWNER_NAME`, then "the person who invited me". `COLLEAGUE_MEETING_INTRO=0` turns it off.

GPT-Live owns pauses, backchannels, and interruptions. The local runtime does not classify meeting speech or add a silence delay.

## Guest-then-signed-in fallback

```mermaid
sequenceDiagram
    participant Op as Operator
    participant Portal as Local console
    participant Adapter as Teams/Meet adapter
    participant IdP as Official Google/Microsoft page
    Op->>Portal: Connect Google or Microsoft account
    Portal->>Adapter: Open meeting view (noVNC)
    Op->>IdP: sign in yourself (no scripted passwords)
    Adapter->>Portal: write local *-connected marker
    Op->>Portal: stop account browser, start meeting
    Portal->>Adapter: join as isolated guest
    alt guest admitted
        Adapter-->>Portal: live
    else guest explicitly denied
        Adapter->>Adapter: retry once with signed-in profile
        alt admitted
            Adapter-->>Portal: live
        else still denied
            Adapter-->>Portal: authentication_required
        end
    end
```

Disconnect removes the local profile directory. It does not revoke sessions on other devices. Zoom has no signed-in profile fallback.

## Retention and deletion

All of these paths are gitignored. Stop the meeting or daemon before deleting files in use.

| Data | Location | How to delete |
| --- | --- | --- |
| Meeting transcripts and traces | `meeting-runtime/recordings/` | Remove the meeting's folder |
| Console reference context | `meeting-runtime/context/index.json` | Console **Clear saved context**, or delete the file |
| Browser profiles (Teams/Google) | `meeting-runtime/profiles/` | Console **Disconnect**, which removes the local profile; or delete the directory |
| Per-meeting runtime state | `meeting-runtime/run/` | Delete after the meeting has stopped |
| Daemon auth token | `.colleague/daemon.auth` | Stop the daemon; deleting the file forces a new token on next start |
| Daemon meetings, events, call records, webhook secret, API token digests | `.colleague/daemon-data/` | Stop the daemon, then delete the directory |
| Owner profile | `.colleague/profile.json` | `colleague profile`, or delete the file |
| Remote connector grants (digests) | `.colleague/connector/` | `colleague connector revoke --all`, or delete the directory |
| Console active meeting | `.colleague/portal-active.json` | Stop the colleague from the console |
| API keys / meeting invite | `.env`, `.env.meeting` | Edit or delete; never commit |

Checkouts from before the cleanup may still have `meeting-runtime/jobs/` and `meeting-runtime/codex-workspace/`. Nothing uses them now; delete them.

### Container user and file permissions

The daemon writes each meeting's state under `meeting-runtime/run/` and `meeting-runtime/context/` as private files (directories 0700, files 0600). The meeting container reads them, writes `recordings/` and `profiles/`, and the host reads those back. So `compose.meeting.yaml` runs the container as the host user, `user: "${COLLEAGUE_UID:-1000}:${COLLEAGUE_GID:-1000}"`, and nothing on the host is made group- or world-readable. The daemon (`meeting_supervisor.host_user_env`) and `start-meeting-agent.sh` set both values to your `id -u` and `id -g`. Inside the container `HOME` is `/tmp` and the image points Playwright at its bundled browsers, so the uid needs no account in the image.

Before starting the container, the daemon creates `recordings/` and `profiles/` as 0700 directories, because Docker would create missing ones as root. If an earlier run left them owned by root or by uid 1001 (the image's `app` user), run `sudo chown -R "$(id -u):$(id -g)" meeting-runtime/recordings meeting-runtime/profiles`.

With rootless Docker or Podman, container root is your user: export `COLLEAGUE_UID=0 COLLEAGUE_GID=0` before starting the daemon or `start-meeting-agent.sh`; values already in the environment are kept.

Restarting the meeting participant starts a **new** GPT-Live voice session.
