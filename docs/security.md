# Security and privacy

Smitline runs on your computer with your own keys. There is no Smitline server: audio goes to OpenAI and your phone provider, and everything else stays on your machine.

## Security model

- **Loopback by default.** The runtime daemon binds `127.0.0.1` with a per-launch bearer token in `.colleague/daemon.auth`. Public binds are rejected unless server mode is turned on with a long-lived API token (see [calls](calls.md#access)). Only the phone gateway's routes, which check the provider's signatures, are exposed through a tunnel.
- **Host-owned secrets.** OpenAI and SignalWire or Twilio keys stay in the `smitline` data volume (the ignored `.env` in a checkout), typed into a one-time local page rather than an agent chat. Local agents reach the MCP endpoint with a local token; browsers are refused. Browser profiles, transcripts, call records, and context stay on disk and gitignored.
- **Fail closed.** Unknown fields, unsupported meeting links, and incomplete briefs are rejected with a readable reason.
- **No secret-bearing logs.** Tokens are not placed in URLs, query strings, events, or errors.
- **Docker access is your choice.** Meetings need the Docker socket mounted, so Smitline can start its meeting container; that gives the container control of Docker on your computer. It uses it only to download, start and stop its own meeting container. Phone calls work without it. See [Get started](../README.md#get-started).
- **Call guardrails.** Smitline never dials emergency, premium-rate, or satellite numbers; calls people only between 8 AM and 9 PM their time; stops calling anyone who asks it to; and limits repeat calls. See [guardrails](phone.md#guardrails).
- **AI disclosure.** Every phone call says it's an AI calling for the person named in the brief by its second turn: the opening names that person and the reason, and the next turn says it's an AI, before asking for anything. The result records whether that was heard (`disclosureVerified`). Meetings open with a short disclosure too, unless the owner turns it off.
- **Operator mute is authoritative.** Smitline does not unmute itself after a host or participant mute. It accepts only an explicit host request, such as Zoom's "Ask to unmute".

## Data and privacy

- **OpenAI:** call and meeting audio goes to GPT-Live. The backend model receives the brief, the context, and the questions GPT-Live hands it; with web search on, it can search the web. After a phone call, a summary model reads the transcript. These use your account's billing.
- **Phone provider:** calls go through your SignalWire or Twilio account. Recordings, if turned on, stay there; the daemon downloads one only when you ask.
- **Local storage:** see [retention and deletion](architecture.md#retention-and-deletion). Transcripts, call records, profiles, and context are gitignored.

Transcripts contain conversation content and are kept until you remove them. Generated agent text may represent speech that was muted or interrupted. Raw audio is not saved. Restarting a meeting participant starts a fresh voice session.

## Reporting a vulnerability

See [SECURITY.md](../SECURITY.md).
