# Changelog

## Unreleased

- **Calls open with who and why.** A call now starts "Hi, I'm calling on behalf of NAME about…", and the assistant says it is NAME's AI assistant at a natural moment during the call. Openings that led with "AI assistant" sounded like scams and were mostly hung up on. If the disclosure has not been said by the time the call wraps up, the assistant is told to say it before the goodbye. Anyone who asks still gets a straight answer, recorded calls still say so in the first sentence, and `disclosureVerified` now covers the whole call (AI as a topic no longer counts).
- Setup walks agents through SignalWire step by step: signing up, the credentials, a number, verifying the numbers a free trial may call (up to 10), and adding $5 of credit to call anyone, with a warning that Auto Top-Up then stays on.
- Setup offers a phone-calls-only start without the Docker socket, says plainly what mounting the socket allows, and tells agents to let the user run the command when their safeguards block it.

## 0.1.1 — 2026-10-01

### Call guardrails

- Emergency and crisis numbers (911, 112, 999, 988, and others) are never dialed.
- Premium-rate and satellite numbers are refused unless `COLLEAGUE_ALLOW_PREMIUM_NUMBERS=1`.
- Calls ring only between 8 AM and 9 PM in the recipient's time zone (`COLLEAGUE_CALLING_HOURS`); a brief's `afterHours: true` overrides this when the user confirms.
- When someone asks not to be called again, the assistant ends the call politely, the result has `doNotCall: true`, and the number goes on a do-not-call list (`/v1/do-not-call`, `smitline do-not-call`).
- At most 5 calls to one number per 24 hours and 20 calls an hour (`COLLEAGUE_MAX_CALLS_PER_NUMBER`, `COLLEAGUE_MAX_CALLS_PER_HOUR`).

### Fixes

- `smitline` CLI output larger than 64 KB is no longer cut off when piped (it waits for output to drain before exiting).
- The console shows the Smitline mark as the participant avatar and the tab icon.

### Recording on request

- A brief with `record: true` (CLI `--record`) records that one call; `COLLEAGUE_RECORD_CALLS=1` still records every call.
- Download a recording (WAV keeps both sides on separate channels; MP3 mixes them) with `smitline calls recording --call-id <id> --out call.wav`, from the call's page in the console, or from `GET /v1/calls/{id}/recording`.

## 0.1.0 — 2026-09-30

First public release.

Smitline (previously developed as Colleague AI) gives any AI agent phone calls and meetings. The agent sends a brief; Smitline talks with people in real time using OpenAI GPT-Live (`gpt-live-1`), hands harder questions to a backend model that knows the brief, and returns a structured result: the outcome, a summary, details, decisions, action items, open questions, and the transcript.

### What it does

- **Phone calls** through your SignalWire or Twilio account. Every call opens with an AI disclosure naming the person it acts for. Rehearse a call on your own phone first, follow the live transcript, or take the call over on your phone.
- **Zoom, Microsoft Teams, and Google Meet meetings** with the same brief on the `meeting` channel. A browser participant joins in a second container, speaks through a virtual microphone after a short disclosure, and can show a presence camera.
- **One Docker image**, `ghcr.io/kaelorlabs/smitline`, started with one `docker run`. It runs the loopback daemon, the local console, the CLI (`docker exec smitline smitline ...`), and starts the meeting image, `ghcr.io/kaelorlabs/smitline-meeting`, for each meeting.
- **Any agent.** MCP over Streamable HTTP at `127.0.0.1:8095/mcp` with a local token, or over stdio (`smitline mcp`); the CLI; the REST calls API (`/v1/calls`, with an OpenAPI description); TypeScript and Python SDKs; and a remote connector with OAuth sign-in for cloud agents.
- **Agent-driven setup.** Paste one prompt into your agent. Keys are typed into a one-time local page, never into the chat, and stay in the data volume.
- **Context in three levels:** the owner's profile (who they are, the people they know, standing boundaries), the session context in the brief (a summary, facts, decisions, open questions, and long reference details), and the goal (objective, questions, tone, contact).
- **Calls dashboard** at `127.0.0.1:8095/calls`: each call's brief, live transcript, result, and phone plus OpenAI cost, with spend totals.

### Phone audio

- **Local barge-in.** Smitline listens to the other person itself: about 160 ms after they talk over it, playback pauses and the provider's buffer is cleared. A short "mhm" lets it carry on; real speech drops the rest of what it was saying.
- **Provider-clock pacing and adaptive playout.** Playback follows the phone provider's clock, recovered from the timestamps on its audio, so a computer clock that runs fast or slow does not starve or flood the line. The provider's buffer is kept near 0.3 seconds by stretching or trimming pauses; speech itself is never cut.
- **A natural opening.** The assistant says a short hello naming whose AI assistant it is, then waits for an answer before saying why it is calling. Call screeners and voicemail greetings are heard out first.

### Known limitations

See [Limitations](README.md#limitations). In short: phone audio is relayed through your computer (direct SIP is being built), Teams and Google Meet still need live acceptance testing, trial phone accounts call only verified numbers, and Twilio's free trial cannot carry call audio.
