# Meetings

Smitline joins Zoom, Microsoft Teams, and Google Meet meetings, and Daily rooms, as a participant that listens and speaks, by link or [by phone](#joining-by-phone). An agent starts a meeting the same way as a phone call, and you can also start one by hand in the console.

## Join a meeting from an agent

An agent joins a meeting through the calls API: `start_call` with `channel: "meeting"` and `to` set to the Zoom, Teams, Google Meet, or Daily link. From the CLI:

```bash
docker exec smitline smitline call --meeting "https://us05web.zoom.us/j/YOUR_MEETING_ID" \
  --objective "Help with the Q3 numbers" --wait
```

`--channel meeting --to <url>` does the same, and so does a `--to` that starts with `http://` or `https://`. The result comes back like a phone call's. See [calls](calls.md).

The first meeting downloads the meeting image, `ghcr.io/kaelorlabs/smitline-meeting` (about 1.8 GB of disk). From a checkout it is built instead.

Admit **Smitline AI** if it enters the waiting room. It joins quietly, like a person who comes in to listen: its name and the caption on its camera tile ("Arun's AI assistant") show everyone, also people who join later, that it is an AI and whose. It unmutes its meeting microphone once and listens continuously; if someone greets it or asks who it is, it says so in a few words. It reads who is in the meeting from the platform's participant list (Zoom, Teams, Google Meet) when it joins and every 20 seconds, and gets the names as quiet context, with a note on which one is probably the person it works for, so "I'm Arun" makes sense to it. Where the meeting shows who is speaking (Daily rooms, Teams and Google Meet; not Zoom yet), it is also told who has the floor once they have held it for a moment, so it can answer people by name, and the meeting's transcript names them (best effort: the words arrive a moment after they are said). It answers when someone addresses it and hands harder questions to the backend model (`SMITLINE_MEETING_BACKEND_MODEL`, default `gpt-5.6-terra`; `SMITLINE_MEETING_WEB_SEARCH=1` adds OpenAI web search). Between replies a local audio gate sends silence, so the platform shows it unmuted; it mutes the microphone when it leaves. `docker exec smitline smitline setup set SMITLINE_MEETING_INTRO 1` adds a short spoken hello at the first pause ("Hi everyone, Arun's AI assistant here."), for meetings where people may not see its tile; `SMITLINE_PARTICIPANT_NAME` changes its name. The backend settings are set the same way.

It leaves by itself, after a short goodbye, when someone asks it to ("thanks, you can drop off now"), when the meeting is clearly over, or when the meeting has moved on and nobody needs it. It also leaves when it is the only one left, and when nobody has spoken for `SMITLINE_MEETING_IDLE_LEAVE_SECONDS` (default 600; `0` turns this off). Your agent can make it leave at any time with `end_call`. Leaving closes the voice session and stops the meeting container, so nothing is billed after it leaves. The call's `call.agent_left` event says why it left. `docker exec smitline smitline setup set SMITLINE_MEETING_INTRO 0` skips the disclosure; the backend settings are set the same way.

Talked over, it stops, as on a phone call: once someone has spoken over it for half a second and the voice has stopped, the rest of what it was saying is dropped; a short "mhm" does not stop it. The hello waits for a real pause in the meeting's audio, not only in the transcript, which trails the words. Each meeting's archive says what came in from the meeting: `input_audio` (bytes, voiced milliseconds, the peak sample, and in a Daily room `dropped_ms`), the same as `input` in `process_stopped`, and `input_silent` once when the meeting showed someone speaking while nothing came in for 10 seconds.

## Daily rooms

A [Daily](https://www.daily.co) room is a meeting your agent can start itself: people join from the link in any browser, with no account and no app. Make one with your Daily account's API key:

```bash
SMITLINE_DAILY_API_KEY=... python3 meeting-runtime/daily_rooms.py create --minutes 60
```

It prints the room's link; send Smitline there like to any meeting, and send the people the same link. An agent can also ask for a room in the brief itself: `channel: "meeting"` with `to: "new"` opens one with the installation's `SMITLINE_DAILY_API_KEY`, open for the meeting's `maxMinutes` plus 15 minutes, and the call returns its link as `to` for the agent to send people. The room is public, with no knocking, and expires after the minutes you gave it, when Daily ejects everyone. The link is the only key, so share it only with the people you invite. `daily_rooms.py delete <name>` removes a room sooner. A private room's link with a meeting token (`?t=...`) works too.

In a Daily room Smitline joins without a browser: daily-python connects it, it hears everyone's microphones (never their video), and the meeting container draws its camera tile. It shows the still picture, or with `SMITLINE_MEETING_CAMERA_STYLE=mascot` the clips of the presence pack in `SMITLINE_MEETING_CAMERA_PACK` (the animated style needs a browser, so it shows the still picture here). The camera goes out as one full-size layer at the pack's frame rate (up to 1.5 Mbit/s), so nobody is sent a postage-stamp copy, and each frame is decoded as it is shown, so a change of clip never stalls the picture. It is usually in the room before the people you sent the link to, so it waits for the first of them (`waiting_for_admission`, up to 10 minutes, like a lobby) before its voice starts: nothing is billed for the wait. Notes for the chat go to the room's chat. It leaves as in any meeting, and when the room expires.

### Check that Smitline hears a room

`meeting-runtime/daily_hearing_check.py` joins a room twice: as a talker that plays five one-second bursts of a voice-like tone, and as a meeting does (the same `DailyRoom` and `DailySpeaker`). It prints one JSON line with what the bot heard: the bursts and how late each came in, the voiced milliseconds against those played, the level, and the speaker's own counts. It needs only the room's link, no API key, takes about half a minute, and exits 0 when every burst was heard, 2 when some were, 1 when nothing was, and 3 when it could not run:

```bash
docker run --rm --entrypoint /app/.venv/bin/python -e ROOM_URL=https://TEAM.daily.co/ROOM \
  ghcr.io/kaelorlabs/smitline-meeting /meeting-runtime/daily_hearing_check.py
```

From a checkout, add `-v "$PWD/meeting-runtime:/meeting-runtime:ro"` to check your own changes.

## Joining by phone

Smitline can also join a meeting by its phone number: Zoom meetings, Teams meetings whose organizer has audio conferencing, and Workspace Google Meet meetings all have one. Smitline dials the number, the phone provider keys in the meeting ID and passcode, and the meeting voice takes part by audio. It needs your phone provider (Twilio or SignalWire) set up, as for phone calls, but no meeting container, no [machine of its own](#each-meeting-on-a-machine-of-its-own) and no browser, so it joins in seconds and takes no meeting seat (`SMITLINE_MAX_MEETINGS`).

Give the dial-in in the brief's `dialIn`: the `number`, the `meetingId` (Teams: the phone conference ID, not the Teams meeting ID; Google Meet: the PIN) and, for Zoom, the numeric phone `passcode` printed with the dial-in numbers. Or paste the invitation itself as `dialIn.invite`, and Smitline reads them from it: its one-tap lines (`+14388097799,,85951289482#,,,,*123456#`), `tel:` links, the dial-in numbers, "Meeting ID", "Phone conference ID", "PIN" and the passcode. It dials the first number the installation may call (`SMITLINE_ALLOWED_CALLING_CODES`), preferring a published bridge ([below](#calling-hours-and-limits)). `to` can be the dial-in number itself, also as a one-tap number, or be left out:

```json
{
  "channel": "meeting",
  "to": "https://us06web.zoom.us/j/85951289482?pwd=...",
  "dialIn": { "invite": "Join Zoom Meeting ... One tap mobile +14388097799,,85951289482#,,,,*123456# Canada ..." },
  "objective": "Take notes on the launch plan"
}
```

When it dials in:

- **No link** (`to` is the number, or missing): always by phone.
- **A link and its dial-in**: by link, and by phone when the platform turns the browser away (the join ends `authentication_required` before it got in, as Google Meet does to most guests), or when no meeting seat is free. `SMITLINE_MEETING_DIAL_IN_FIRST=zoom,teams,meet` (or `all`) joins those platforms by phone first: an installation that runs no meeting browser, for example. Each only while the number passes the phone rules ([below](#calling-hours-and-limits)); otherwise it stays with the link. A meeting nobody let in is not joined again by phone: that was the host's choice.

How the call runs:

- **The keys.** The phone provider plays them with its own DTMF (`<Play digits>`, which Twilio and SignalWire both document) once the call is answered, before the voice comes on the line, so the voice never hears or answers the bridge's prompts. They are timed like the platform's one-tap number, where each comma is a two-second pause: Zoom `,,MEETINGID#,,,,*PASSCODE#`, Teams and Google Meet `,,ID#`. A one-tap number in the invitation is dialed exactly as printed. `dialIn.keys` overrides it for an unusual bridge. The stored brief keeps what was dialed (`number`, `meetingId`, `passcode`, `platform` and `keys`), never the invitation text.
- **In, or waiting.** What the bridge says next decides. A waiting room, a lobby or a host who has not started ("Please wait, the meeting host will let you in soon") makes the call `waiting`: that time is not meeting time, and after 10 minutes it gives up (`not_admitted`). People talking, or 8 seconds of quiet, means it is in (`in_progress`). A bridge that refuses the ID or passcode ("The meeting ID you entered is invalid") ends the call at once (`dial_in_refused`). A bridge asking for more ("press pound to skip") is neither.
- **The voice.** The same participation policy as in a browser meeting: it listens, answers when someone addresses it by name, hands harder questions to `SMITLINE_MEETING_BACKEND_MODEL`, and takes requests with `canDo`. Nobody sees a name or a camera tile, so it says it is an AI out loud, always: once it is in, at the first real pause, the hello ("Hey, this is NAME's AI assistant. I'm here to answer any questions. I'll be here taking notes, so let me know if you need anything."), never over anyone; if someone speaks to it first, it says so as it answers. The audio is the phone line's (G.711), as on phone calls.
- **Leaving.** As in a browser meeting: when someone asks it to and it has said goodbye, when the leave check says the meeting is over, when nobody has spoken for `SMITLINE_MEETING_IDLE_LEAVE_SECONDS`, at `maxMinutes` counted from when it got in, on `end_call`, or when the meeting ends and the bridge hangs up. It has no participant list, so it does not notice being alone.
- **Never recorded**, and no answering-machine check.

The call is a meeting's: `channel` `meeting`, the meeting summary, and `endReason` `meeting_ended`, `max_duration`, `canceled`, `not_admitted`, `dial_in_refused`, or `busy` or `no_answer` when the number did not answer. `line.joinedBy` is `"phone"`, with the platform when known. `usage.voiceSeconds` is the time in the meeting, as for any meeting; the phone line's `phoneSeconds` and its provider's price are in the call's [cost](calls.md#cost). Listening in, `send_call_instruction` and requests work as in any meeting.

### Calling hours and limits

A dial-in number is dialed like a phone number, with the phone [guardrails](phone.md#guardrails): the calling codes, premium and satellite numbers and the do-not-call list always, and calling hours (`SMITLINE_CALLING_HOURS`) and the call limits (`SMITLINE_MAX_CALLS_PER_NUMBER`, `SMITLINE_MAX_CALLS_PER_HOUR`) unless it is a meeting platform's published dial-in number. Anyone can put a number in a brief, and a number that is not a bridge may be someone's phone; a published bridge serves every meeting on it, so it is dialed at any hour and as often as meetings need. Meetings joined by phone count toward the limits as calls do.

- **The published numbers.** Smitline ships Zoom's own US and Canada dial-in numbers in [`meeting-runtime/dial_in_bridges.json`](../meeting-runtime/dial_in_bridges.json), copied from the "Find your local number" pages that Zoom invitations link to (<https://zoom.us/u/actuuYsofT> and <https://zoom.us/u/adtBZPAIK7>, read on 2026-11-05). Teams and Google Meet numbers keep the phone rules for now: Teams' belong to each organizer's audio conferencing, and Google Meet's come from a pool.
- **`SMITLINE_DIAL_IN_BRIDGES`** changes the list: comma-separated `published` (the shipped list, also what an unset setting means), E.164 numbers, or `none`. Numbers alone replace the published list; with `published` they extend it (`published,+15145550100`). Anything else is skipped, so a mistake only keeps a number under the phone rules.
- **When a rule stops it.** A meeting with no link is refused before anything rings, `outside_calling_hours` with when it can dial in, or `too_many_calls_to_number`; the message says that a Zoom number from the invitation has no such limits. `afterHours: true` dials outside calling hours when the user confirms the meeting is on now and the number is its own. A meeting with a link joins by its link instead, and if the platform then turns the browser away, the result says it did not dial in and why.

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

While a meeting runs, the daemon reaches it through `POST /control` on the same port, with a token made for that meeting alone (`SMITLINE_CONTROL_TOKEN`, in the container's environment and the daemon's memory, never on disk): `send_call_instruction` guidance, and answers to [requests](agents.md#requests-from-the-call) from the people in the meeting. Listening in (`/listen`) needs the same token. Requests leave the meeting through `/health`, as its captions do. A meeting the daemon adopted after a restart has no token, so it runs on without either until it ends. A meeting on a [machine of its own](#each-meeting-on-a-machine-of-its-own) is the exception: its token is also kept on the daemon's disk, so a restarted daemon still has it.

## Each meeting on a machine of its own

A hosted installation can run each meeting on a machine of its own, such as a cloud VM made for that meeting and deleted after it, instead of in a container on the daemon's Docker host. `SMITLINE_MEETING_LAUNCHER=package.module:create` names the launcher plugin that makes the machines; the daemon imports it at startup and will not start if it is missing or incomplete. Leave it unset to run meetings on this computer, exactly as before.

What changes for a meeting:

- **Starting takes as long as the machine.** A Google Cloud VM takes about 80 seconds to boot. The call is `connecting` meanwhile (the meeting is `joining`), and `end_call` ends it at once, deleting the machine, even halfway through creating it. A machine that never comes up fails the call: "The meeting could not be started. Try again in a few minutes." The daemon gives a machine 300 seconds to answer, or the plugin's own `start_timeout`.
- **The machine gets everything by value.** The plugin's `up()` receives a start package: the meeting's runtime state (the container reads it at `SMITLINE_RUNTIME_STATE`), the container's whole environment (its owner's OpenAI key and meeting settings, as a pool meeting's env file holds them, and `SMITLINE_CONTROL_TOKEN`), the meeting's token, and its time limit (`maxMinutes`, from the brief), so the plugin can give the machine a lifetime a little longer than the meeting can last. It must reach the machine over a private channel, never in instance metadata. No env file is written on the daemon's side.
- **Every route takes the meeting's token.** The machine's port 8094 is reached over a network, so the meeting checks the token on `/health` too (`SMITLINE_HEALTH_TOKEN=1`), not only on `/control` and `/listen`. Open the port to the daemon alone.
- **The archive comes back over HTTP.** The daemon cannot read the machine's disk. Once the meeting has closed its archive, `GET /archive` returns it, a gzipped tar of the files in `recordings/<meeting id>/` (`409` until then). The meeting's server waits up to `SMITLINE_ARCHIVE_GRACE` seconds (the daemon sets 120) for the daemon to take it. The daemon unpacks it into its own `recordings/<meeting id>/`, writes the handoff from it, and has the machine deleted. Ending a meeting (`end_call`, or `maxMinutes`) first asks it to stop, as SIGTERM stops a container here (`POST /control` with `{"type": "stop"}`), and takes its archive before deleting the machine. A meeting that fails (`needs_attention`) closes its archive at once, so it comes back too. A machine that vanished gets a partial handoff from what the daemon followed live.
- **A restart keeps the meetings.** The meeting's token is kept in `.smitline/active-meetings/<meeting id>.token` (0600), beside its claim. A restarted daemon asks the plugin which machines of this installation run, adopts those whose meetings are still going (with their tokens, so instructions, listening in and the archive keep working), deletes the machines of meetings that ended, and leaves machines it knows nothing about alone.
- **Capacity is what you allow.** `SMITLINE_MAX_MEETINGS` (default 1) caps how many machines run at once and `SMITLINE_MAX_MEETINGS_PER_OWNER` each owner's. There are no ports or folders to share.
- **Some platforms can stay here.** `SMITLINE_MEETING_LAUNCHER_PLATFORMS` lists the platforms whose meetings get a machine (`zoom`, `teams`, `meet`, `daily`); the others run in this computer's meeting pool as before. `zoom,teams,meet` keeps Smitline rooms (Daily) here: they need no browser, so they start in seconds and take little room. The two limits then count every meeting, wherever it runs, and a restart adopts both kinds. Unset, or all four, every meeting gets a machine.

The daemon asks the machine's `/health` every second, and asks the plugin for the machine's state (`inspect()`) only while it starts or stops, or when the meeting stops answering, so a meeting in progress costs no cloud API calls.

A launcher plugin's `create(environ=, project_root=, runtime_root=)` returns a provider with:

| Part | What it does |
| --- | --- |
| `machine(meeting_id, owner)` | The machine for one meeting, at once, with no I/O. After a restart it is asked again for a machine that still runs, and then stands for that one. |
| machine `remote = True` | Marks it as a machine of its own. |
| machine `async up(package)` | Create the machine and have it run the meeting image with the start package. It may return once the machine is asked for. A meeting ended meanwhile cancels it. |
| machine `async stop()` | Delete the machine, also one that never finished starting. A machine never serves a second meeting. |
| machine `async inspect()` | `{"running": bool, "unknown": bool}`. `running` is false once `stop()` was asked or the meeting exited, even while the cloud is still deleting the machine. Keep it cheap, from a cache, if the cloud's API has quotas. |
| machine `health_url` | `http://ADDRESS:8094/health` once the machine has an address, else `None`. A plain attribute that `up()` or `inspect()` sets. |
| `async running()` | `{meeting_id: running}` for every machine of this installation, found by label. |
| `async remove(meeting_id)` | Delete that meeting's machine. |
| `start_timeout` (optional) | Seconds a machine may take to answer `/health`. |

`RemoteMeetingPool` in [meeting_supervisor.py](../meeting-runtime/meeting_supervisor.py) describes the same, and [test_remote_meetings.py](../meeting-runtime/test_remote_meetings.py) has a fake plugin that serves a meeting on a local port.

## Appearing in the meeting

A virtual camera shows Smitline listening, working, or speaking, and never displays task text. Turn it off in the console to join audio-only. By default the camera is a still picture that changes with the state, which costs almost no CPU. `SMITLINE_MEETING_CAMERA_STYLE=animated` switches to the animated camera, which redraws at 30 frames a second and needs about a full CPU core more. Private context added in the console reaches the voice and its backend as background.

`SMITLINE_MEETING_CAMERA_STYLE=mascot` plays a presence pack instead: a character that walks in once Smitline is admitted, stands and listens, thinks while it waits on its backend, talks while it speaks, and waves and hops off when it leaves. `SMITLINE_MEETING_CAMERA_PACK` names the pack's folder, which must be readable inside the meeting container (copy it into a meeting image of your own). A pack is a `manifest.json` and WebP or PNG frames: clips that loop or play once, the clip each presence state shows, and an optional arrival clip; [presence_pack.py](../meeting-runtime/presence_pack.py) describes the format. The frames are pictures only; the camera adds the caption. A new clip blends in over a quarter of a second, so a change of state never jumps. A 640x360 pack at 12 frames a second needs about a tenth of a CPU core more than the still picture in a browser and sends about 0.8 Mbit/s; at 24 frames a second it needs somewhat more. A pack that cannot be read leaves the still picture.

A pack may also say who its character is (`persona`, a few sentences) and give it a GPT-Live `voice`: Smitline appears as that character, so its voice is given the persona with its instructions, and the meeting uses the character's voice unless the brief names one. `SMITLINE_VOICE` still sets the voice for phone calls and for meetings without such a pack.

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
