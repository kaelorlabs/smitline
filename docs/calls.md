# Calls

A **call** is one conversation Colleague AI holds with people on behalf of an agent: a phone call or a meeting. Any agent starts a call with a **brief** and later reads a **result**. This is the contract every surface uses (REST, SDKs, CLI, MCP, remote connector).

A meeting is a call with `channel: "meeting"` and the Zoom, Teams, or Google Meet invite URL as `to` (CLI: `colleague call --meeting <url>`). Underneath it is an ordinary daemon meeting, and the call record links to it with `meetingId`. The daemon also has a small [meetings API](#meetings-api) that the local console uses.

## Lifecycle

```text
queued ─► connecting ─► ringing / waiting ─► in_progress ─► summarizing ─► completed
   │           │                │                 │
   └───────────┴────────────────┴─────────────────┴──────────► failed / canceled
```

| Status | Meaning |
| --- | --- |
| `queued` | Accepted; the line has not started yet. |
| `connecting` | Dialing a phone number or joining a meeting. |
| `ringing` | Phone only: the callee's phone is ringing. |
| `waiting` | Meeting only: waiting in a lobby or for admission. |
| `in_progress` | Connected; GPT-Live is in the conversation. |
| `summarizing` | The conversation ended; the result is being built. |
| `completed` | Terminal. `result` is present, including for unanswered calls. |
| `failed` | Terminal. A system error prevented a result. `error` explains it. |
| `canceled` | Terminal. Ended by the caller before a result was possible. |

`endReason` records how the conversation ended: `hangup`, `remote_hangup`, `no_answer`, `busy`, `voicemail`, `max_duration`, `canceled`, `meeting_ended`, `transferred`, `error`.

## Brief

```json
{
  "channel": "phone",
  "to": "+14155550142",
  "onBehalfOf": "Sam Rivera",
  "objective": "Book a table for 4 at 7pm tonight",
  "context": "Indoor is fine if the patio is full.",
  "questions": ["How long will they hold the table?"],
  "mayAgreeTo": ["times between 6:30 and 7:30pm", "indoor seating"],
  "mustNotShare": ["payment details"],
  "successCriteria": "A confirmed booking with a confirmation number",
  "language": "en",
  "voice": "marin",
  "maxMinutes": 10,
  "rehearsal": false,
  "notify": { "webhookUrl": "https://example.com/hooks/colleague" }
}
```

Required: `channel` and `objective`, plus `to` and `onBehalfOf` unless setup provides them. `onBehalfOf` defaults to `COLLEAGUE_OWNER_NAME`. A rehearsal (`"rehearsal": true`, phone only) calls `COLLEAGUE_OWNER_PHONE`, so `to` may be left out, and any other number is refused. `voice` defaults to `COLLEAGUE_VOICE`, then `marin`; `COLLEAGUE_EXTRA_VOICES` allows voice names beyond the documented ones. Phone numbers use E.164 (`+` and 8 to 15 digits). Meeting briefs use a Zoom, Teams, or Google Meet invite URL as `to`.

`POST /v1/calls/check` validates a brief without starting anything. An incomplete brief returns `422 brief_incomplete` with the missing fields and a question the agent can ask the user for each one. Agents should ask the user rather than guess.

## Context

A phone call gets three levels of context, so the voice can talk like someone who knows the story without being steered by it:

| Level | Comes from | Holds |
| --- | --- | --- |
| Profile | `GET` and `PATCH /v1/profile`, kept in `.colleague/profile.json` (owner-only) | Who the owner is, the people they know (name, relationship, phone, notes), how they like to come across, and standing boundaries |
| Session | The brief's `context` | What the agent and the owner have been working on: text (at most 6,000 characters), or an object with `summary`, `facts`, `decisions`, `openQuestions`, and long `details` (at most 24,000 characters) |
| Goal | The rest of the brief | `objective`, `questions`, `mayAgreeTo`, `mustNotShare`, `successCriteria`, `tone`, and `contact` |

The goal leads: it goes into GPT-Live's instructions, together with the profile's standing boundaries. The voice also starts with short reference notes: the owner, the person called, and the session's summary, facts, decisions, and open questions, cut to about 1,800 tokens. They are marked as background rather than an agenda, so the voice uses them when they help and does not recite them. The backend model the voice consults for harder questions gets everything, `details` included, up to about 12,000 tokens.

The person called comes from `contact`, or else from the profile entry with the same phone number. The voice greets them by name and matches the relationship unless `tone` says otherwise. The result's `details` answer each of the `questions`.

```json
{
  "channel": "phone",
  "to": "+14155550199",
  "objective": "Ask Alex whether Sam should launch the booking app now or wait",
  "questions": ["Launch now or wait, and why?", "What would make him use it?"],
  "tone": "casual; he is a close friend",
  "context": {
    "summary": "Sam built a booking app that lets small restaurants take reservations by text message.",
    "facts": ["The beta has 40 restaurants", "Payments work in the US and Canada"],
    "openQuestions": ["Pricing"],
    "details": "Longer notes, such as a changelog or a spec, that the voice can look things up in."
  }
}
```

During a call, `POST /v1/calls/{id}/instructions` with `"silent": true` adds a background note, such as something the owner just remembered. The voice uses it when it becomes relevant instead of acting on it at once.

Meetings get the session context and `questions` as their starting context, with the summary, facts, and details cut to 8,000 characters together; the profile is used on phone calls. In a meeting, the backend model GPT-Live hands harder questions to (`COLLEAGUE_MEETING_BACKEND_MODEL`) gets that context too, up to about 4,000 tokens. Never put passwords, keys, or card numbers in the profile or the context: the voice may repeat anything it knows.

## Result

```json
{
  "outcome": "achieved",
  "summary": "Booked an indoor booth for 4 at 7:00 pm tonight under Sam Rivera.",
  "details": [
    { "label": "Confirmation", "value": "LG-2291" },
    { "label": "Table held", "value": "15 minutes" }
  ],
  "decisions": [],
  "actionItems": [],
  "openQuestions": [],
  "transcript": [{ "speaker": "agent", "text": "Hi, this is Sam Rivera's AI assistant. I'm calling to book a table for four tonight." }],
  "durationSeconds": 252
}
```

`outcome` is one of `achieved`, `partial`, `not_reached`, `voicemail`, `declined`, `failed`, `canceled`. Outgoing phone results also carry `disclosureVerified`: whether the agent was heard saying it is an AI calling for `onBehalfOf` (see [phone calls](phone.md)). Recorded calls get a `recording` field on the call once Twilio finishes the file. Phone results are summarized from the transcript by a backend model (`COLLEAGUE_SUMMARY_MODEL`, default `gpt-5.6-luna`), which treats the transcript as untrusted data. Unanswered and busy calls get a result without a model call. If summarizing fails, the result still carries the transcript and says why. Meeting results come from the meeting handoff. `source` records which path produced the result.

## Cost

Every finished call carries `cost`, in US dollars:

```json
{
  "currency": "USD", "total": 0.0425, "phone": 0.017, "openai": 0.0255, "estimated": false,
  "items": [
    { "kind": "phone", "provider": "signalwire", "seconds": 53, "amount": 0.017, "source": "provider" },
    { "kind": "voice", "model": "gpt-live-1", "seconds": 30, "amount": 0.025, "source": "list_price" },
    { "kind": "summary", "model": "gpt-5.6-luna", "input": 478, "output": 316, "amount": 0.000475, "source": "list_price" }
  ],
  "pricesAsOf": "2026-09-29"
}
```

- **Phone line:** what Twilio or SignalWire charged, read from the provider's record of the call. Providers fill it in shortly after a call ends; the daemon checks after 20 seconds, 1, 5, and 30 minutes, and looks up older calls once when the call list is read. Until then the line is estimated from the minutes (`source: estimate`, `estimated: true`); `COLLEAGUE_PHONE_PRICE_PER_MINUTE` sets the estimate's rate. A handed-over call's second leg, to your phone, is not included.
- **OpenAI:** GPT-Live seconds, and the background and summary models' tokens (cached input and web searches included), priced from the table in `call_costs.py`. OpenAI does not bill per call, so these amounts are calculated, not billed. A model with no known price is listed in `unpriced` and left out of the total. A call keeps the cost it was given when it ended; update the table when prices change.

`GET /v1/calls` also returns `spend`: finished calls' totals per day, in the reader's time zone when `tzOffset` (minutes east of UTC) is given. The console's Calls page shows today, this month, all time, and the average per call, and for each call its brief, result, cost, and transcript.

## Delivery

| Method | Use |
| --- | --- |
| `GET /v1/calls/{id}` | Poll the call. |
| `GET /v1/calls/{id}/wait?timeout=60` | Long-poll until the call reaches a terminal status or the timeout (maximum 300 seconds) passes. |
| `GET /v1/calls/{id}/events` | Server-sent events with `Last-Event-ID` resume. Add `?format=json&after=N` for a JSON page of events after event `N`, as the local console does. |
| `notify.webhookUrl` | One `POST` when the call reaches a terminal status. |

Webhook bodies are signed: `X-Colleague-Signature: sha256=<hex HMAC of the raw body>` with the per-installation secret in `.colleague/daemon-data/webhook.secret`. Only `https://` URLs are accepted, plus `http://127.0.0.1` and `http://localhost` for local agents; an https URL may not name a private, loopback, or link-local IP address, and a host name that resolves to one is refused at delivery (`rejected`, with no request sent) unless `COLLEAGUE_WEBHOOK_ALLOW_PRIVATE=1`. Delivery is retried three times with backoff. The outcome is recorded as a `call.webhook` event with `delivered`, `attempts`, and a coarse `error` (`rejected`, `failed`, or `unreachable`), never the receiver's exact response.

## Reliability

- Statuses only move forward; a late provider callback cannot move a call back.
- `POST /v1/calls/check` applies the same checks as starting a call, including the destination allow-list.
- When the daemon stops, calls still in progress are closed as `failed` with the transcript so far. At startup, any call a previous daemon left unfinished is closed the same way, so a waiting agent always gets an answer.
- Long transcripts keep their first 60 and last 340 lines, so the opening (with the disclosure) survives.

## Other endpoints

| Endpoint | Purpose |
| --- | --- |
| `POST /v1/calls` | Start a call from a brief. |
| `GET /v1/calls` | List recent calls, newest first. |
| `POST /v1/calls/check` | Validate a brief and report missing configuration without starting a call. |
| `POST /v1/calls/{id}/instructions` | Add guidance mid-call. GPT-Live receives it as trusted instructions; with `"silent": true` it is a background note the voice uses when relevant. Meetings do not accept live instructions yet (`delivered: false`). |
| `POST /v1/calls/{id}/end` | End the call politely and build the result. |
| `POST /v1/calls/{id}/transfer` | Phone only: hand the connected call to the owner's phone. |
| `GET /v1/voices` | GPT-Live voices this installation accepts. |
| `GET /v1/profile` | The owner's profile, the first level of [context](#context). |
| `PATCH /v1/profile` | Update the profile. Fields present replace the saved ones; `people` are added or updated by name; `removePeople` drops names. A problem returns `422` with a readable message. |
| `GET /v1/openapi.json` | The machine-readable API description. |

### Meetings API

The local console starts meetings with these endpoints. Agents should use `/v1/calls` instead, which returns a result.

| Endpoint | Purpose |
| --- | --- |
| `POST /v1/meetings` | Start a meeting. Fields: `meetingUrl` (required), `context`, `camera`, `onBehalfOf`, `voice`. Unknown fields are rejected. |
| `GET /v1/meetings/{id}` | The meeting's state. |
| `POST /v1/meetings/{id}/context` | Replace the meeting's context. The voice session reads it when it starts. |
| `POST /v1/meetings/{id}/cancel` | Stop the meeting. |
| `GET /v1/meetings/{id}/events` | Server-sent events for the meeting. |
| `GET /v1/meetings/{id}/handoff` | The handoff built from the transcript after the meeting. |

## Hooks

The call service calls a small hooks object so a managed deployment can add accounts and billing without forking the core. The default hooks serve a single local owner.

| Hook | Default |
| --- | --- |
| `owner_for(request)` | `local` |
| `credentials(owner, provider)` | Reads `OPENAI_API_KEY` and `TWILIO_*` from the environment. |
| `precheck(owner, brief)` | Allows the call, applying the configured country allow-list for phone calls. |
| `record_usage(owner, call, usage)` | Appends a line to `usage.jsonl` in the call store. |
| `notify(owner, call)` | Sends the brief's webhook, if any. |
| `profile(owner)`, `save_profile(owner, profile)` | Read and write `.colleague/profile.json` next to `.env`. Without them, calls get no profile and `/v1/profile` returns `501`. |

Set `COLLEAGUE_CALL_HOOKS=module:factory` to load different hooks.

## Access

The daemon listens on loopback with a per-launch token by default. The token is in `.colleague/daemon.auth` (`/data/.colleague/daemon.auth` in the image) and changes each time the daemon starts.

Server mode also accepts long-lived API tokens; only SHA-256 digests are stored. It refuses to start without at least one API token and must sit behind a TLS-terminating proxy. From a checkout, create a token with `python3 meeting-runtime/api_tokens.py create --name NAME`, then start the daemon with `COLLEAGUE_SERVER_MODE=1 COLLEAGUE_DAEMON_HOST=0.0.0.0 ./start-runtime-daemon.sh`. In the image, create the token as the `app` user, so the file stays readable to the daemon: `docker exec -u app colleague python meeting-runtime/api_tokens.py --root /data create --name NAME`. The container's start script reads the same two variables, so pass `-e COLLEAGUE_SERVER_MODE=1 -e COLLEAGUE_DAEMON_HOST=0.0.0.0` to `docker run`.

Credentials come from the process environment first, then the ignored `.env` (`/data/.env` in the image), which is reread for every call, so keys added during setup work without a restart.
