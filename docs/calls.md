# Calls

A **call** is one conversation Smitline holds with people on behalf of an agent: a phone call or a meeting. Any agent starts a call with a **brief** and later reads a **result**. This is the contract every surface uses (REST, SDKs, CLI, MCP, remote connector).

A meeting is a call with `channel: "meeting"` and the Zoom, Teams, or Google Meet invite URL as `to` (CLI: `smitline call --meeting <url>`). Underneath it is an ordinary daemon meeting, and the call record links to it with `meetingId`. The daemon also has a small [meetings API](#meetings-api) that the local console uses.

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

`endReason` records how the conversation ended: `hangup`, `remote_hangup`, `no_answer`, `busy`, `voicemail`, `max_duration`, `canceled`, `meeting_ended`, `transferred`, `error`. When the agent itself ended it, the events say why: `call.end_requested` for a phone call (the `end_call` reason), and `call.agent_left` for a meeting the agent left (`left_completed`, `left_asked_to_leave`, `left_not_productive`, `left_other`, `idle` or `alone_in_meeting`).

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
  "afterHours": false,
  "record": false,
  "notify": { "webhookUrl": "https://example.com/hooks/smitline" }
}
```

`canDo` (optional, up to 500 characters) says what the calling agent will do for the people on the call when they ask, such as "look things up in the repo and edit the docs". With it, the voice passes their requests to the agent; without it, nobody on the call can ask the agent for anything. See [requests from the call](agents.md#requests-from-the-call).

A meeting brief may also set `camera` (meetings only): `{ "enabled": true, "defaultOn": true, "avatarDataUri": "data:image/png;base64,..." }`. `enabled` shows Smitline's virtual camera tile, `defaultOn` turns it on after joining (both default to `true`), and the avatar is a PNG, JPEG, WebP, or SVG of up to 80 KB. The stored brief keeps `enabled` and `defaultOn` but not the image.

Required: `channel` and `objective`, plus `to` and `onBehalfOf` unless setup provides them. `onBehalfOf` defaults to `SMITLINE_OWNER_NAME`. A rehearsal (`"rehearsal": true`, phone only) calls `SMITLINE_OWNER_PHONE`, so `to` may be left out, and any other number is refused. `voice` defaults to `SMITLINE_VOICE`, then `marin`; `SMITLINE_EXTRA_VOICES` allows voice names beyond the documented ones. Phone numbers use E.164 (`+` and 8 to 15 digits); emergency and crisis numbers are refused. Meeting briefs use a Zoom, Teams, or Google Meet invite URL as `to`. Before a phone call rings, the [guardrails](phone.md#guardrails) check the number, the person's local time, the do-not-call list, and how often the number was called; a refusal is a `403` with a code such as `outside_calling_hours`. `afterHours: true` (phone only) skips the calling-hours check when the user confirms the person expects a call now. `record: true` (phone only) records the call in the owner's phone account; see [recording](phone.md#how-a-call-runs).

`POST /v1/calls/check` validates a brief without starting anything. An incomplete brief returns `422 brief_incomplete` with the missing fields and a question the agent can ask the user for each one. Agents should ask the user rather than guess.

## Context

A phone call gets three levels of context, so the voice can talk like someone who knows the story without being steered by it:

| Level | Comes from | Holds |
| --- | --- | --- |
| Profile | `GET` and `PATCH /v1/profile`, kept in `.smitline/profile.json` (owner-only) | Who the owner is, the people they know (name, relationship, phone, notes), how they like to come across, and standing boundaries |
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

Meetings get the session context and `questions` as their starting context, with the summary, facts, notes from earlier calls, and details cut to 8,000 characters together; the profile is used on phone calls. In a meeting, the backend model GPT-Live hands harder questions to (`SMITLINE_MEETING_BACKEND_MODEL`) gets that context too, up to about 4,000 tokens. Never put passwords, keys, or card numbers in the profile or the context: the voice may repeat anything it knows.

## Earlier calls

Calls can build on each other. A call can start with short notes from earlier calls, such as the quotes the first two roofers gave before the third is called.

- **Tasks.** A brief's optional `task` ties calls toward one goal together: `{"id": "roof-quotes-oct", "title": "Roof repair quotes"}`. The `id` is 1 to 64 lowercase letters, digits, and dashes, chosen by the agent; the first call that names a task creates it. `GET /v1/calls?task=roof-quotes-oct` lists its calls.
- **Notes.** After a call ends, the agent can save a note for later calls with `POST /v1/calls/{id}/note` and `{"text": "Apex: $14,200, 20-yr warranty, earliest start Nov 20."}` (at most 1,200 characters; MCP `save_call_note`, CLI `smitline calls note`). It is stored on the call as `carryNote`. A call without a note offers its result's summary and details instead.
- **Starting with them.** A brief's optional `carryFrom` chooses which notes an outbound call starts with: `{"task": true}` for earlier calls in its task, `{"contact": true}` for earlier calls to or from the same number (phone only), and `{"calls": ["call-..."]}` for named calls (at most 10). Notes are taken newest first, at most 10 and 4,000 characters in all, from finished calls only.

The notes are added to the voice's starting notes and the backend's background under **Earlier calls**, after the brief's own context, with an instruction not to repeat what another person or business said unless the objective needs it, and never anything in `mustNotShare`. The brief's `context` is never rewritten. The call record keeps exactly what it started with as `carried` (`callId`, `contact`, `at`, `text`, and `by`: `agent` or `result`). `POST /v1/calls/check` reports the number as `carried`.

Nothing carries over unless the agent asks or the user switched it on: each [contact](#contacts) has an **automatic context** setting, off by default, that makes calls to its number behave as if `carryFrom.contact` were `true`; a brief's `"contact": false` still overrides it for one call. **Incoming calls never start with earlier calls**, because caller ID can be faked.

Carrying context between different businesses is the point of a quotes task and a leak elsewhere, so the agent decides what is carried for each call.

## Contacts

A contact is a phone number Smitline has called or been called from. `GET /v1/contacts` lists them, latest call first, with the number of calls and the name: the one the user saved, else a profile person with that number, else the latest brief's `contact.name`. `GET /v1/contacts/{number}` (with `+` written as `%2B`) adds the call history and `nextCall`, the notes a new call to the number would start with. `PATCH /v1/contacts/{number}` sets `name` (80 characters), `notes` (600, for the user only; calls do not read them), and `autoContext`; an empty value or `false` clears one. `DELETE /v1/contacts/{number}` forgets what was saved; the number's call records and their notes stay. What the user saves is kept in `.smitline/contacts.json` (owner-only); a file that cannot be read stops contact changes rather than being replaced. Profile people stay what they are: people the user knows, added to every call to their number.

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
  "transcript": [
    { "speaker": "agent", "text": "Hi, I'm calling on behalf of Sam Rivera about a table for four tonight." },
    { "speaker": "other", "text": "Sure, go ahead." },
    { "speaker": "agent", "text": "I'm Sam's AI assistant. Do you have anything around seven?" }
  ],
  "durationSeconds": 252
}
```

`outcome` is one of `achieved`, `partial`, `not_reached`, `voicemail`, `declined`, `failed`, `canceled`. Requests from the call that were not finished are in `openRequests`, for the agent to follow up; each call keeps all its requests, with their threads, in `requests`. Outgoing phone results also carry `disclosureVerified`: whether the agent was heard saying it is an AI calling for `onBehalfOf` during the call (see [phone calls](phone.md)). Recorded calls get a `recording` field on the call once the provider finishes the file; `GET /v1/calls/{id}/recording` downloads it. Phone results are summarized from the transcript by a backend model (`SMITLINE_SUMMARY_MODEL`, default `gpt-5.6-luna`), which treats the transcript as untrusted data. Unanswered and busy calls get a result without a model call. If summarizing fails, the result still carries the transcript and says why. When the other person asked not to be called again, the result has `doNotCall: true` and their number goes on the do-not-call list. Meeting results are summarized the same way from the meeting's transcript, which the call gets from the meeting archive when the meeting ends, with rules for meetings: whether the objective was met is judged only from what was said, and open questions are only those still unresolved at the end. A meeting where nothing was said falls back to its handoff, which never counts as `achieved` (`partial` when it records decisions or action items, `not_reached` otherwise). `source` records which path produced the result.

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
  "pricesAsOf": "2026-10-06"
}
```

- **Phone line:** what Twilio or SignalWire charged, read from the provider's record of the call. Providers fill it in shortly after a call ends; the daemon checks after 20 seconds, 1, 5, and 30 minutes, and looks up older calls once when the call list is read. Until then the line is estimated from the minutes (`source: estimate`, `estimated: true`) at US rates: Twilio $0.014 a started minute; SignalWire $0.011 a started minute ($0.008 outbound plus $0.003 for the audio stream) and $0.006 a call (`ratePerMinute`, `ratePerCall`). `SMITLINE_PHONE_PRICE_PER_MINUTE` replaces the estimate with one rate a started minute. A handed-over call's second leg, to your phone, is not included.
- **OpenAI:** GPT-Live seconds, and the background and summary models' tokens (cached input and web searches included), priced from the table in `call_costs.py`. OpenAI does not bill per call, so these amounts are calculated, not billed. A model with no known price is listed in `unpriced` and left out of the total. A call keeps the cost it was given when it ended; update the table when prices change.

`GET /v1/calls` also returns `spend`: finished calls' totals and connected `seconds` per day, in the reader's time zone when `tzOffset` (minutes east of UTC) is given. `channel=phone` or `channel=meeting` limits both the calls and the spend to one kind. The console's Calls and Meetings pages show today, this month, all time, and the average per call or meeting, and for each one its brief, result, cost, and transcript.

## Delivery

| Method | Use |
| --- | --- |
| `GET /v1/calls/{id}` | Poll the call. |
| `GET /v1/calls/{id}/wait?timeout=60` | Long-poll until the call reaches a terminal status or the timeout (maximum 300 seconds) passes. Add `since` (empty at first, then the last answer's `live.cursor`) to follow the call live: the answer also comes about 2 seconds after something new is said, with `live.transcript` holding the lines said after the cursor, and at once for a request from the call, in `live.requests`. `only=requests` wakes for requests alone and leaves `live.transcript` out. While an agent waits live, the call counts it as following, so the voice passes requests on (see [requests from the call](agents.md#requests-from-the-call)). |
| `GET /v1/calls/{id}/listen` | Listen in: both sides of a call in progress as NDJSON, a `format` line (`mulaw` at 8000 Hz for phone calls, `pcm16` at 24000 Hz for meetings), then `{"side", "audio"}` chunks in base64 until the call ends. Off unless the owner sets `SMITLINE_LISTEN=1`: `403 listening_off`. `409 listen_unavailable` when the call has not connected or its audio does not pass through Smitline (direct SIP), `409 call_ended` after it ends. Each listen adds a `call.listening` event. A listener that falls behind loses audio; the call never waits for it. |
| `GET /v1/calls/{id}/events` | Server-sent events with `Last-Event-ID` resume; `call.transcript` events carry each line as it is said, for phone calls and meetings. Add `?format=json&after=N` for a JSON page of events after event `N`, as the local console does. |
| `notify.webhookUrl` | One `POST` when the call reaches a terminal status. |

Webhook bodies are signed: `X-Smitline-Signature: sha256=<hex HMAC of the raw body>` with the per-installation secret in `.smitline/daemon-data/webhook.secret` (an owner other than the local one has its own, in `.smitline/owners/<owner>/webhook.secret`; see [owners](#owners)). Only `https://` URLs are accepted, plus `http://127.0.0.1` and `http://localhost` for local agents; an https URL may not name a private, loopback, or link-local IP address, and a host name that resolves to one is refused at delivery (`rejected`, with no request sent) unless `SMITLINE_WEBHOOK_ALLOW_PRIVATE=1`. Delivery is retried three times with backoff. The outcome is recorded as a `call.webhook` event with `delivered`, `attempts`, and a coarse `error` (`rejected`, `failed`, or `unreachable`), never the receiver's exact response.

## Reliability

- Statuses only move forward; a late provider callback cannot move a call back.
- `POST /v1/calls/check` applies the same checks as starting a call, including the destination allow-list.
- When the daemon stops, each call still in progress is asked to end (a phone call hangs up) and gets `SMITLINE_SHUTDOWN_GRACE` seconds (default 5; `0` cuts calls off at once) to finish and report what it used; a call still running after that is closed as `failed` with the transcript so far. At startup, any call a previous daemon left unfinished is closed the same way, so a waiting agent always gets an answer.
- A phone call's `usage.phoneSeconds` is its connected time: the provider's, from its status callback, or when none arrives (incoming calls never get one), measured from when the call's audio connected to when it ended. A meeting's `usage.voiceSeconds` runs from when Smitline was let in to when the meeting ended.
- A meeting call that cannot get a meeting container is refused when it is placed, by `POST /v1/calls` and `/v1/calls/check`, rather than accepted and failed a moment later: `409 capacity_exceeded` (the one container is busy, or the pool is full), `409 owner_capacity_exceeded`, `409 meeting_pool_required` or `503 no_free_ports` (see [several meetings at once](#several-meetings-at-once)). The supervisor checks again when the meeting starts, so two calls placed at the same moment for the last place still get one answer each.
- Long transcripts keep their first 60 and last 340 lines, so the opening (with the disclosure) survives.

## Other endpoints

| Endpoint | Purpose |
| --- | --- |
| `POST /v1/calls` | Start a call from a brief. |
| `GET /v1/calls` | List recent calls, newest first. `channel`, `contact` (an E.164 number), and `task` narrow the list. |
| `POST /v1/calls/{id}/note` | Save a note on a finished call for [later calls](#earlier-calls). |
| `GET /v1/contacts`, `GET`, `PATCH`, `DELETE /v1/contacts/{number}` | [Contacts](#contacts): list, read, edit, or forget what was saved. |
| `POST /v1/calls/check` | Validate a brief and report missing configuration without starting a call. |
| `POST /v1/calls/{id}/instructions` | Add guidance mid-call, to a phone call or a meeting. GPT-Live receives it as trusted instructions; with `"silent": true` it is a background note the voice uses when relevant. `delivered: false` when the voice could not take it (a meeting adopted after a daemon restart has no channel in). |
| `POST /v1/calls/{id}/requests/{requestId}/reply` | Answer a [request from the call](agents.md#requests-from-the-call): `{"type": "status", "text", "announce"}` any number of times, then `{"type": "done", "text", "ok", "details"}` once. Returns the request and whether the voice got it. `409 request_closed` for a request already done or canceled, `409 call_ended` for a status after the call, `429 too_many_updates` after 20 statuses. |
| `POST /v1/calls/{id}/end` | End the call politely and build the result. |
| `POST /v1/calls/{id}/transfer` | Phone only: hand the connected call to the owner's phone. |
| `GET /v1/calls/{id}/recording?format=wav\|mp3` | Download a recorded call from the phone provider. WAV keeps both sides on separate channels; MP3 mixes them into one. `404 no_recording` when there is none, `409 not_ready` while the call runs. |
| `GET /v1/voices` | GPT-Live voices this installation accepts. |
| `GET /v1/profile` | The owner's profile, the first level of [context](#context). |
| `PATCH /v1/profile` | Update the profile. Fields present replace the saved ones; `people` are added or updated by name; `removePeople` drops names. A problem returns `422` with a readable message. |
| `GET /v1/do-not-call` | Numbers Smitline refuses to call because the person asked not to be called again, kept in `.smitline/do-not-call.json`. |
| `PATCH /v1/do-not-call` | `{"add": ["+1...", {"number": "+1...", "reason": "..."}], "remove": ["+1..."]}`. CLI: `smitline do-not-call add|remove <number>`. |
| `GET /v1/openapi.json` | The machine-readable API description. |

### Meetings API

The local console starts meetings with these endpoints. Agents should use `/v1/calls` instead, which returns a result. They act for the local owner only: a request for another [owner](#owners) gets `403 local_owner_only`, and so does every request when `SMITLINE_REQUIRE_OWNER=1`.

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
| `owner_for(request)` | The [owner](#owners) of the request: `local` unless an API token says otherwise. |
| `settings(owner)` | What one owner's calls read: the environment overlaid with `.env`, the same for every owner. |
| `storage_root(owner)` | `.smitline` next to `.env` for `local`; `.smitline/owners/<owner>` for any other owner. |
| `credentials(owner, provider)` | Reads `OPENAI_API_KEY` and `TWILIO_*` (or `SIGNALWIRE_*`) from the environment; the number calls show is `SMITLINE_CALLER_ID` from `settings(owner)`, else the provider number. |
| `brief_defaults(owner, payload)` | Fills `onBehalfOf` (and a rehearsal's `to`) from `SMITLINE_OWNER_NAME` and `SMITLINE_OWNER_PHONE` in `settings(owner)`. |
| `precheck(owner, brief)` | For phone calls, applies the country allow-list and the [guardrails](phone.md#guardrails) from `settings(owner)`; repeat limits count only this owner's calls. |
| `call_started(owner, record)` | Nothing. Runs once the call is recorded, before its line starts. Raising `CallRefused` ends the call as `failed` (`endReason: refused`) and answers with the refusal; any other error ends it too, with `503 call_start_failed`. |
| `contact_book(owner)` | The owner's saved contacts, `contacts.json` under `storage_root(owner)`. |
| `do_not_call(owner)`, `update_do_not_call(owner, update)` | Read and change `do-not-call.json` under `storage_root(owner)` (`.smitline/do-not-call.json` for `local`). Without them, `/v1/do-not-call` returns `501` and a request not to be called again is only reported in the result. |
| `record_usage(owner, call, usage)` | Appends a line to `usage.jsonl` in the call store. Runs once for every call that started, with the call's owner: also for a call the daemon closed because it stopped or restarted, with the usage known by then (none if the line had not reported it yet). Never for a call refused before it started. |
| `notify(owner, call)` | Sends the brief's webhook, if any, signed with `webhook_secret(owner)`. |
| `webhook_secret(owner)` | The installation's secret for `local`; for any other owner a secret of its own, made on first use in `webhook.secret` under `storage_root(owner)`. |
| `owner_for_number(number)` | Who an incoming call to `number` (E.164, or `None` when the provider sent none) is for: `local` when `SMITLINE_ACCEPT_INBOUND=1`, else `None`, which turns the call away. |
| `profile(owner)`, `save_profile(owner, profile)` | Read and write `profile.json` under `storage_root(owner)` (`.smitline/profile.json` next to `.env` for `local`). Without them, calls get no profile and `/v1/profile` returns `501`. |

Set `SMITLINE_CALL_HOOKS=module:factory` to load different hooks. The factory receives the default hooks' keyword arguments (`environ`, `env_file`, `store`, `notifier`, `clock`); subclassing `DefaultCallHooks` and overriding only what changes keeps everything else. Only `owner_for`, `credentials`, `precheck`, `record_usage` and `notify` are required; the call service falls back to the environment without `settings`, and treats a `contact_book` attribute (one book) as the local owner's.

A hook refuses with `CallRefused(code, message, status=403, **details)`. The API answers `status` with `{"error": {"code", "message", ...details}}`, so a hosted service can answer `402 insufficient_credit` with `balance` and `topUpUrl`, or `429 too_many_active_calls`. `POST /v1/calls/check` lists the message among its `problems`.

### Owners

Every call record, meeting, profile, contact book and do-not-call list belongs to one owner, an id of lowercase letters, digits, `_` and `-` (at most 64 characters, starting with a letter or digit). A self-hosted installation has one owner, `local`, and keeps its files where they always were. Reading or changing another owner's call or meeting answers `404`, as if it did not exist.

`owner_for(request)` decides the owner from the credential the daemon verified, which it keeps as `request['smitline_auth']`: `{"kind": "launch"}` for the per-launch token, `{"kind": "api", "token": {"id", "name", "kind", "owner"}}` for an API token.

- An API token created with `--owner OWNER` always acts as that owner; even an internal one gets `403 owner_header_forbidden` for a header naming another owner.
- An API token created with `--kind internal` (a hosted service's gateway) names the owner of each request with `X-Smitline-Owner: OWNER`. From any other credential the header is refused with `403 owner_header_forbidden`; an id that is not an owner id is `400 invalid_owner`.
- Everything else is `local`.

`SMITLINE_REQUIRE_OWNER=1`, for a deployment that serves accounts, refuses a request that would act as `local` with `401 owner_required`, closes the meetings API, and turns away incoming calls unless `owner_for_number` names an owner.

These settings are read per owner, through `settings(owner)`: `SMITLINE_OWNER_NAME`, `SMITLINE_OWNER_PHONE`, `SMITLINE_CALLER_ID`, `SMITLINE_VOICE`, `SMITLINE_EXTRA_VOICES`, `SMITLINE_ALLOWED_CALLING_CODES`, `SMITLINE_CALLING_HOURS`, `SMITLINE_MAX_CALLS_PER_NUMBER`, `SMITLINE_MAX_CALLS_PER_HOUR`, `SMITLINE_ALLOW_PREMIUM_NUMBERS`, `SMITLINE_RECORD_CALLS`, `SMITLINE_LISTEN`, `SMITLINE_NOTIFY_WEBHOOK` (incoming calls) and `SMITLINE_PARTICIPANT_NAME`. Provider keys, prices, models and the audio mode stay installation-wide.

Phone callbacks are checked with the credentials of the call's owner, and an incoming call with those of the owner `owner_for_number` gives for the number called, whose settings build its brief. A meeting for an owner other than `local` runs in the [meeting pool](#several-meetings-at-once) with that owner's `OPENAI_API_KEY` (from `credentials(owner, 'openai')`) and its `SMITLINE_VOICE`, `SMITLINE_EXTRA_VOICES`, `SMITLINE_PARTICIPANT_NAME` and `SMITLINE_OWNER_NAME` from `settings(owner)`, never the installation's. Its `SMITLINE_PARTICIPANT_NAME` is the name the meeting joins under.

### Several meetings at once

`SMITLINE_MAX_MEETINGS` (default 1) is how many meetings run at the same time. With 1, there is one meeting container, as always: the console shows it at `127.0.0.1:6082` (browser) and `127.0.0.1:8094` (status), and its claim is `.smitline/active-meeting.json`. That container uses the installation's signed-in browser profiles and sees the whole data volume, so it runs the local owner's meetings only: a meeting for any other owner is refused by `POST /v1/calls` and `POST /v1/calls/check` with `409 meeting_pool_required`. Above 1, or whenever `SMITLINE_REQUIRE_OWNER=1`:

- Each meeting runs in its own compose project, `smitline-meeting-<meeting id>` (`compose.meeting.pool.yaml`, or `compose.meeting.pool.image.yaml` in the published image), and is removed when the meeting ends.
- It is published on two free ports from `SMITLINE_MEETING_PORTS` (default `18100-18399`) on 127.0.0.1: noVNC, then `/health`, which the daemon follows. The console's live view follows only the single container.
- It mounts only its own `run/meetings/<id>` and `recordings/<id>` folders and its owner's browser profiles: the installation's signed-in profiles for `local`, and for any other owner `.smitline/owners/<owner>/meeting-profiles`, empty unless that owner signs in, so it joins as a guest. It never sees other meetings, other owners, or `.smitline/`. The published image mounts these as subpaths of the data volume, which needs Docker Engine 26 or later.
- Its environment holds only what the meeting reads: `OPENAI_API_KEY` from `credentials(owner, 'openai')`, and `SMITLINE_VOICE`, `SMITLINE_EXTRA_VOICES`, `SMITLINE_PARTICIPANT_NAME`, `SMITLINE_OWNER_NAME`, `SMITLINE_MEETING_BACKEND_MODEL`, `SMITLINE_MEETING_WEB_SEARCH`, `SMITLINE_MEETING_INSTRUCTIONS`, `SMITLINE_MEETING_INTRO`, `SMITLINE_MEETING_CAMERA_STYLE` and `SMITLINE_MEETING_IDLE_LEAVE_SECONDS` from `settings(owner)` with `.env.meeting` over them (for another owner, `.env.meeting` sets only the `SMITLINE_MEETING_*` ones). The daemon writes them to a private (0600) file named by `SMITLINE_MEETING_ENV_FILE`, the only env file the pool's compose files read, and deletes it once the container is created. Provider keys, SIP and webhook secrets and everything else in `.env` never reach a pool meeting.
- Each running meeting's claim (owner, project, ports) is `.smitline/active-meetings/<meeting id>.json`.
- A full pool answers `409 capacity_exceeded`. `SMITLINE_MAX_MEETINGS_PER_OWNER` caps the meetings of each owner other than `local` (`409 owner_capacity_exceeded`); unset, owners share the pool.
- At startup the daemon follows every pool meeting whose container is still running and whose meeting has not ended, removes its other `smitline-meeting-*` projects, and writes the handoff of meetings no container is running any more. A `smitline-meeting-*` project whose meeting it does not know belongs to another installation on the same Docker host and is left alone.

## Access

The daemon listens on loopback with a per-launch token by default. The token is in `.smitline/daemon.auth` (`/data/.smitline/daemon.auth` in the image) and changes each time the daemon starts.

Server mode also accepts long-lived API tokens; only SHA-256 digests are stored. It refuses to start without at least one API token and must sit behind a TLS-terminating proxy. From a checkout, create a token with `python3 meeting-runtime/api_tokens.py create --name NAME` (add `--owner OWNER` for a token that acts as another [owner](#owners), or `--kind internal` for a gateway that names the owner per request), then start the daemon with `SMITLINE_SERVER_MODE=1 SMITLINE_DAEMON_HOST=0.0.0.0 ./start-runtime-daemon.sh`. In the image, create the token as the `app` user, so the file stays readable to the daemon: `docker exec -u app smitline python meeting-runtime/api_tokens.py --root /data create --name NAME`. The container's start script reads the same two variables, so pass `-e SMITLINE_SERVER_MODE=1 -e SMITLINE_DAEMON_HOST=0.0.0.0` to `docker run`.

Credentials come from the process environment first, then the ignored `.env` (`/data/.env` in the image), which is reread for every call, so keys added during setup work without a restart.
