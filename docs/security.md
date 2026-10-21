# Security and privacy

Smitline runs on your computer with your own keys. There is no Smitline server: audio goes to OpenAI and your phone provider, and everything else stays on your machine.

## Security model

- **Loopback by default.** The runtime daemon binds `127.0.0.1` with a per-launch bearer token in `.smitline/daemon.auth`. Public binds are rejected unless server mode is turned on with a long-lived API token (see [calls](calls.md#access)). Only the phone gateway's routes, which check the provider's signatures, are exposed through a tunnel.
- **Host-owned secrets.** OpenAI and SignalWire or Twilio keys stay in the `smitline` data volume (the ignored `.env` in a checkout), typed into a one-time local page or the console's Account page rather than an agent chat. The console never returns a key (at most a known prefix such as `sk-proj-`), and saving or removing one requires its per-session token and a request from the console's own origin, so other websites the user visits cannot change keys. Removing a key only deletes Smitline's copy; it stays valid where it was issued. Local agents reach the MCP endpoint with a local token; browsers are refused. Browser profiles, transcripts, call records, and context stay on disk and gitignored.
- **Fail closed.** Unknown fields, unsupported meeting links, and incomplete briefs are rejected with a readable reason.
- **Owners.** One deployment can serve several owners (see [calls](calls.md#owners)). Every call, meeting, profile, contact book and do-not-call list belongs to one, and another owner's are answered as not found. Only an `internal` API token may name the owner of a request (`X-Smitline-Owner`); a token created for an owner always acts as it. Phone callbacks are checked with the call owner's credentials. With `SMITLINE_REQUIRE_OWNER=1` nothing acts as the local owner. Another owner's meetings run only in the meeting pool (several meetings at once, which `SMITLINE_REQUIRE_OWNER=1` always uses): each meeting container mounts only its own folders and its owner's browser profiles, so another owner's meeting never gets the installation's signed-in Google or Teams profiles and joins as a guest, and its environment holds only its OpenAI key and the meeting settings it reads, from a private (0600) file deleted as soon as the container is created; provider keys, SIP and webhook secrets and the rest of `.env` never reach it. One meeting container at a time runs only the local owner's meetings (`409 meeting_pool_required` for any other).
- **No secret-bearing logs.** Tokens are not placed in URLs, query strings, events, or errors.
- **Docker access is your choice.** Meetings need the Docker socket mounted, so Smitline can start its meeting container; that gives the container control of Docker on your computer. It uses it only to download, start and stop its own meeting container. Phone calls work without it. See [Get started](../README.md#get-started).
- **Call guardrails.** Smitline never dials emergency, premium-rate, or satellite numbers; calls people only between 8 AM and 9 PM their time; stops calling anyone who asks it to; and limits repeat calls. See [guardrails](phone.md#guardrails).
- **AI disclosure.** Every phone call says it's an AI calling for the person named in the brief during the call: the opening names that person and the reason, the disclosure follows, and Smitline has it said before hanging up if it was missed. The result records whether that was heard (`disclosureVerified`). In meetings the bot's name ("Smitline AI") and the caption on its camera tile ("NAME's AI assistant") show it is an AI for the whole meeting, also to people who join late; it says so when asked, and `SMITLINE_MEETING_INTRO=1` adds a spoken hello. If you turn the camera off or rename the bot, keep "AI" in the name.
- **Requests from the call are untrusted.** With a brief's `canDo`, people on a call can ask the calling agent for things through the voice. The voice passes on only what `canDo` names, each request says who asked (as heard, never verified), and agents are told to get the user's approval before anything that changes things, sends data, or spends money. A call takes at most 20 requests (a meeting 50). Without `canDo`, nobody on the call can ask the agent for anything.
- **One meeting, one token.** The daemon reaches a running meeting container (guidance, request answers, listening in) with a random token made for that meeting and held only in memory and the container's environment, so other programs on the computer cannot write into a meeting or listen to it.
- **Operator mute is authoritative.** Smitline does not unmute itself after a host or participant mute. It accepts only an explicit host request, such as Zoom's "Ask to unmute".

## Data and privacy

- **OpenAI:** call and meeting audio goes to GPT-Live. The backend model receives the brief, the context, and the questions GPT-Live hands it; with web search on, it can search the web. After a phone call, a summary model reads the transcript. These use your account's billing.
- **Phone provider:** calls go through your SignalWire or Twilio account. Recordings, if turned on, stay there; the daemon downloads one only when you ask.
- **Local storage:** see [retention and deletion](architecture.md#retention-and-deletion). Transcripts, call records, profiles, and context are gitignored.

Transcripts contain conversation content and are kept until you remove them. Generated agent text may represent speech that was muted or interrupted. Raw audio is not saved. Restarting a meeting participant starts a fresh voice session.

## Reporting a vulnerability

See [SECURITY.md](../SECURITY.md).
