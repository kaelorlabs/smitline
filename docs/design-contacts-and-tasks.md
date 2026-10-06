# Design: contacts, tasks, and carried-over context

Status: proposal, for review before implementation. Builds on the console redesign (branch `console-ui-redesign`).

## Problem

Every call starts from zero. An agent that phones ten roofers for quotes gives each call the same brief, so the third roofer cannot hear that the first two quoted $14,200 and $12,900. A second call to the dentist does not know the first one booked the cleaning. The agent can paste earlier results into `context` by hand, but most agents do not, and the console has no view of who was called or how calls relate.

## Goals

- Group calls by the person or business called (a **contact**) and by the job they serve (a **task**).
- Let a new call start with short notes from earlier calls, chosen by the agent or switched on per contact.
- Show all of it in the console: a Contacts page, Calls grouped by contact or task, and on each call the context it started with.

Non-goals for this change: meeting participants as contacts, syncing contacts from a phone or CRM, a hosted backend, and deleting call records.

## Terms

| Term | Meaning |
| --- | --- |
| Contact | A phone number Smitline has called or been called from, with an optional name, notes, and settings. Keyed by the E.164 number. |
| Task | A label that ties calls together toward one goal, such as "roof repair quotes". Chosen by the agent. |
| Carry note | A short text saved on a finished call for later calls to use. Written by the agent, or taken from the result when the agent writes none. |
| Carried context | The carry notes a call started with. Snapshotted on the call record. |

"Session" was the working name for tasks. It is already used for the brief's `context` (`SessionContext`, `briefing.py`) and for GPT-Live sessions (`liveSessionId`), so this doc uses **task**. The UI can say "task" too.

## How it works

### Contacts

A contact exists for every number in the call store; nothing extra is written until the user or agent adds a name, notes, or a setting. The console lists contacts by grouping call records on `brief.to` for phone calls.

A contact's display name comes from, in order: the contact's saved name, a profile person with that phone number (`contact_for`), and the latest `brief.contact.name` for that number.

Profile people stay what they are today: people the user knows, written by the user or with `update_profile`, added to every call to their number. Contacts do not replace them. Businesses an agent calls once do not belong in the profile, because profile people are described to the voice as people the user knows.

### Tasks

The brief gains an optional `task`:

```json
{ "task": { "id": "roof-quotes-oct", "title": "Roof repair quotes" } }
```

`id` is 1–64 characters of `[a-z0-9-]`, chosen by the agent. `title` is optional, up to 120 characters; the latest one wins. No endpoint creates a task: the first call that names it does. `GET /v1/calls?task=roof-quotes-oct` lists its calls.

### Carry notes

When a call finishes, the agent can save a note for later calls:

- REST: `POST /v1/calls/{callId}/note` with `{ "text": "Apex: $14,200, 20-yr warranty, earliest start Nov 20." }`, up to 1,200 characters.
- MCP: `save_call_note`. CLI: `smitline calls note <callId> "<text>"`.

The note is stored on the record as `carryNote: { text, by: "agent", at }`. A call with no saved note offers its result instead: `summary` plus `details`, clipped to the same length, marked `by: "result"`.

Notes go through `reject_secrets` like every other write.

### Starting a call with earlier context

The brief gains an optional `carryFrom`:

```json
{ "carryFrom": { "task": true, "contact": true, "calls": ["call-0123456789abcdef"] } }
```

| Key | Effect |
| --- | --- |
| `task` | Add the carry notes of earlier finished calls in the brief's `task`. Requires `task`. |
| `contact` | Add the carry notes of earlier calls to the same number. `false` overrides the contact's setting for this call. |
| `calls` | Add these calls' carry notes. Up to 10 ids, each owned by the same owner. |

When the call starts, the daemon resolves the notes, newest first, at most 10 notes and 4,000 characters in all, and:

1. Snapshots them on the record as `carried: [{ callId, contact, text, by }]`, so the console and the result show exactly what the call knew.
2. Adds them as a section of the voice notes and backend background, after the brief's own context, under the heading "Earlier calls". The brief's `context` is never rewritten.
3. Adds one instruction: earlier calls are background for this call; do not repeat what another person or business said unless the objective needs it, and never anything in `mustNotShare`.

No extra model call compresses notes. Notes are short by design and clipped; if that proves too blunt, a summarising pass can come later.

`check_call_brief` reports how many notes would be carried, so an agent can see it before calling.

### Per-contact automatic context

Each contact has a setting, **Add to new calls automatically**, off by default. When on, outbound calls to that number behave as if `carryFrom.contact` were `true` unless the brief says `false`.

Off by default follows decision 10: context should be explicit. The user turns it on for contacts where it clearly helps (the dentist, a regular restaurant), and the call record still shows what was carried.

**Inbound calls never get carried context.** Caller ID can be spoofed; carrying notes into an inbound call would read earlier conversations to whoever fakes the number.

### Where the data lives

| Data | Where |
| --- | --- |
| `task`, `carryFrom` | The stored brief (`record.brief`), like other brief fields |
| `carryNote`, `carried` | The call record (`call.json`), outside the brief |
| Contact names, notes, auto-add setting | `.colleague/contacts.json`, mode 0600 |

`contacts.json`:

```json
{
  "version": 1,
  "contacts": [
    { "number": "+14155550142", "name": "City Dental Office", "notes": "Front desk is Maria.", "autoContext": true, "updatedAt": "2026-10-06T21:00:00Z" }
  ]
}
```

Up to 1,000 entries; names 80 characters, notes 600, as profile people. The file is managed like `do-not-call.json` (`call_policy.py`): atomic writes, and a read error stops the feature rather than losing settings.

This is the one new file. Arun's decision on 2026-10-06: keep it a local JSON file for now and move to SQLite if the data grows. Listing calls already scans every `call.json` (`CallStore.list`), and the new `?contact=` and `?task=` filters scan the same way. That is fine at today's volumes. A SQLite index (roadmap: "durable metadata store") is the fix when it is not.

### Deleting

Deleting a contact removes its entry from `contacts.json`: name, notes, and setting. It does not delete call records, which stay the system of record (decision 9), and their carry notes stay with them. The console says so. Deleting call records is a separate feature; there is no delete path today.

## API changes

| Surface | Change |
| --- | --- |
| Brief | `task`, `carryFrom` added to `BRIEF_FIELDS`, both validated with their own allowlists; unknown keys rejected |
| Call record | `carryNote`, `carried` |
| `GET /v1/calls` | `?contact=<E.164>`, `?task=<id>`, alongside `?channel=` from the console redesign |
| `POST /v1/calls/{id}/note` | Save a carry note on a finished call |
| `GET /v1/contacts` | Contacts with call counts, last call, name, notes, setting |
| `GET`, `PATCH`, `DELETE /v1/contacts/{number}` | Read, edit, or forget one contact |
| OpenAPI | `Brief`, `Call`, new `Contact`, `ContactList` |
| MCP | `save_call_note`, `list_contacts`; `list_calls` gains `channel`, `contact`, `task`; `start_call` and the brief schema gain `task`, `carryFrom` |
| CLI | `--task`, `--carry-task`, `--carry-contact`, `--carry-call`; `calls note`; `calls list --contact/--task`; `contacts list|show|set|forget` |
| SDKs | TypeScript and Python types and methods for all of the above |
| Docs | `docs/calls.md` (brief, context levels, endpoints), `docs/agents.md`, `docs/architecture.md` retention table (add `contacts.json`, and the missing `do-not-call.json`), the call-with-smitline skill |

The console writes contacts only through endpoints that already require the console token and a same-origin request (`authorized()` in `control-panel/server.mjs`).

## Console

- **Contacts** (sidebar): list with search; detail with name, number (partly hidden), notes, the auto-add switch, "What the next call will know" (the notes that would be carried now), and history.
- **Calls**: group by Newest, By contact, or By task. A call shows a contact chip, a task chip, **Context Smitline started with** (from `carried`), and **Saved for later calls** (from `carryNote`).
- The prototype: https://claude.ai/artifact/AnDwSzB8pwBHKvRMMKAcVW

## Privacy and safety

- Carrying context between different businesses is the point of a quotes task and a leak elsewhere. The agent chooses what is carried; nothing crosses contacts unless the agent names the task or calls. The voice is told not to repeat other parties' words unless the objective needs it.
- `mustNotShare` applies to carried notes as to everything else.
- Inbound calls never get carried context.
- Notes are checked for secrets on write.
- All of it stays local (decision 13).

## Settled decision to add

25. **Explicit call memory.** A call starts with earlier calls' notes only when the agent asks (`carryFrom`) or the user switched it on for that contact. What a call carried is stored on its record. Inbound calls never carry context.

## Plan

1. Daemon: brief fields, carry notes, carried snapshot, contacts store, list filters, OpenAPI, tests.
2. Clients: MCP tools, CLI, SDKs, docs, skill.
3. Console: Contacts page and Calls grouping.

All in one PR after `console-ui-redesign` merges, since both change `call_brief.py`, `openapi.json`, and `docs/calls.md`.

## Open questions

1. Is "task" the right name in the API and UI? Alternatives: "job", "project".
2. Should a contact's auto-add setting default to on for numbers that are also profile people?
3. Should Smitline write a carry note from the result automatically, or only offer the result as a fallback when asked, as proposed?
