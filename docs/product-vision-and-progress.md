# Colleague AI: product vision, decisions, and progress

**Document status:** canonical product brief and progress ledger  
**Last reviewed:** 2026-09-30  
**Current stage:** advanced local alpha / developer preview  
**Primary implementation branch at this snapshot:** `cleanup`

This document is the durable product memory for Colleague AI. It explains what we are building, why it matters, which decisions are settled, what already exists, and what remains. Contributors, human or agent, should read this document before proposing architecture or product changes and update the progress ledger when a milestone materially changes.

Related technical references:

- [Architecture](architecture.md)
- [Calls](calls.md)
- [Phone calls](phone.md)
- [Agents](agents.md)
- [Capability matrix](capabilities.md)
- [Meeting adapters](meeting-adapters.md)
- [Control panel](control-panel.md)
- [Product roadmap](product-roadmap.md)

## Product in one sentence

Colleague AI gives any AI agent phone calls and meetings: the agent sends a brief, Colleague AI talks with people in real time using GPT-Live, and a structured result comes back to the chat that sent it.

## The problem

Agents can research, write, and plan, but many tasks still end in a conversation: booking a table, confirming an appointment, asking a colleague a question, or sitting in a meeting. The agent stops and the person has to pick up the phone or join the call, then report back to the agent by hand.

Existing voice products are tied to one assistant or one app. An agent the user already works with, local or in the cloud, has no simple way to hand off a conversation and get a trustworthy account of it back.

## The product we want

A user should be able to tell the agent they are already using:

> Call Luigi's and book a table for 4 at 7. Or: join this meeting and help with the Q3 numbers.

Colleague AI should then:

1. Receive a brief from the agent: who to call or which meeting to join, on whose behalf, the goal, the context, what it may agree to, and what it must not share.
2. Ask, through the agent, for anything missing instead of guessing.
3. Place the phone call through the user's SignalWire or Twilio account, or join Zoom, Microsoft Teams, or Google Meet as one visible participant.
4. Open with an AI disclosure naming the person it acts for.
5. Keep one continuous `gpt-live-1` session for the conversation, listen continuously, and speak selectively.
6. Hand questions that need careful reasoning or precise facts to a backend model that knows the brief.
7. Show a safe, non-sensitive visual presence in meetings.
8. Persist transcripts, events, call records, and costs outside model memory.
9. Return a structured result: the outcome, a summary, details, decisions, action items, open questions, and the transcript.

The ideal experience is that the user asks their agent, and the call just happens.

## Primary user experience

### Agent-native launch

The primary product path is the user's own agent, not a separate website:

```text
User in Claude Code, Codex, Cursor, ChatGPT, Claude, or their own code
                  |
                  | "Call Luigi's" / "Join this meeting"
                  v
   Colleague AI MCP server / remote connector / CLI / SDK
                  |
                  v
          Local runtime daemon (calls API)
             /                 \
            v                   v
     Phone gateway          Meeting container
  SignalWire or Twilio      Zoom/Teams/Meet
             \                 /
              v               v
                   GPT-Live
                      |
                      v
          Structured result back to the agent
```

Setup is agent-driven too: the user pastes one prompt, and the agent follows [SETUP.md](../SETUP.md).

### Optional console

`http://127.0.0.1:8095` remains an optional local console. The **Calls** tab follows calls live, shows results and costs, and can end or take over a phone call. The **Meetings** tab starts a meeting by hand with private reference context, the camera, and Teams or Google account sign-in, and keeps transcripts and handoffs. It calls the same daemon as the SDKs, CLI, and MCP server. A user starting calls from their agent should not need to open it.

### During a call or meeting

- Colleague AI joins as one participant.
- GPT-Live handles audio understanding, conversational timing, interruptions, and spoken delivery.
- GPT-Live hands harder questions to the backend model and stays in the conversation meanwhile.
- A virtual camera may show listening, working, or speaking state in meetings without exposing task text or private content.

### After the call or meeting

- Transcript, runtime events, and cost are durable locally.
- The result goes back to the agent through the calls API, MCP, CLI, SDKs, or a signed webhook.

## Settled product and architecture decisions

These decisions should not be reversed casually. A proposal to change one should explain the user benefit, migration, and security effect. Retired decisions keep their numbers so older references stay readable.

1. **Runtime and SDK first.** The daemon owns calls and meetings. SDKs, CLI, console, and MCP are clients of the same runtime. MCP is a compatibility surface, not a second implementation.
2. **The console is optional.** It is an operations and fallback console, not the agent-native experience.
3. **One continuous GPT-Live session.** Use `gpt-live-1` for the whole call or meeting with `store: false`. Do not switch to transcription-only mode and lose original audio context. GPT-Live is the only voice; nothing else listens or speaks.
4. **GPT-Live owns conversational behavior.** It decides when to speak and how to handle pauses and interruptions. Do not add local silence timers, backchannel classifiers, wake phrases, or an `Allow speaking` product toggle as the normal interaction model.
5. **Selective participation.** Instructions should make Colleague AI a quiet, useful participant in meetings. It should answer direct requests and intervene when necessary for a material factual or safety correction, not reply to every sentence.
6. *Retired 2026-09-30.* Client delegation to a coding-agent provider. Meetings now use Responses delegation, as phone calls do (decision 23).
7. *Retired 2026-09-30.* Resuming the originating coding-agent session.
8. *Retired 2026-09-30.* Exclusive coding-agent session lease.
9. **Application-owned memory.** Transcripts, events, call records, and handoffs are the system of record. Model context alone is insufficient.
10. **Explicit context, never hidden reasoning.** Pass objectives, summaries, facts, decisions, constraints, and open questions. Do not request chain-of-thought or hidden traces.
11. **Platform adapters.** Zoom, Teams, and Meet DOM and policy differences remain behind a shared meeting adapter contract.
12. **Guest first, account fallback.** Teams and Meet attempt guest entry before using a locally connected account. Credentials are entered by the user on the official provider page and are never collected by Colleague AI.
13. **Local-first security boundary.** The supported runtime is a loopback daemon. Browser profiles, credentials, transcripts, and call records remain local.
14. *Retired 2026-09-30.* Typed permissions and approvals for workspace commands, edits, commits, and pushes.
15. *Retired 2026-09-30.* Isolated worktree mutations and the Git broker.
16. **Operator/platform mute is authoritative.** Colleague AI must not override an explicit host or participant mute. Generated audio is discarded when the meeting microphone is unavailable.
17. **No sensitive camera output.** The virtual camera communicates presence only. It never displays prompts, task text, credentials, or meeting content.
18. *Retired 2026-09-30.* Opt-in incoming screen observation. Colleague AI no longer reads shared screens.
19. **Truthful capabilities.** Platform features are reported only when built or tested. Unsupported features and model names are never guessed.
20. **No hardcoded demo scenario.** The runtime must support general calls and meetings with user-provided context rather than fixed data or scripted behavior.
21. **Brief in, result out.** Any agent, local or cloud, starts a phone call or meeting through the calls API with a brief and reads a structured result. A meeting is a call on the `meeting` channel; `/v1/meetings` is a small API the console uses. (Added 2026-09-28; updated 2026-09-30.)
22. **Server mode is opt-in.** Loopback with a per-launch token stays the default. A non-loopback bind requires at least one long-lived API token (stored as a digest) and a TLS-terminating proxy; it exists so cloud agents can reach a self-hosted installation. (Added 2026-09-28; narrows decision 13 rather than replacing it.)
23. **Responses delegation driven by the brief.** GPT-Live hands hard questions to a Responses backend model that knows the brief and context, because most callers are agents that cannot be reached mid-call. Phone calls use `COLLEAGUE_PHONE_BACKEND_MODEL`, meetings `COLLEAGUE_MEETING_BACKEND_MODEL`, both `gpt-5.6-terra` by default, with optional OpenAI web search. (Added 2026-09-28 for phone calls; extended to meetings 2026-09-30.)
24. **Phone calls and meetings for any agent.** Colleague AI does not integrate with coding agents beyond being a tool they can call. Claude Code, Codex, and Cursor are callers like any other MCP client. (Added 2026-09-30.)

## Current implementation snapshot

Status meanings:

- **Implemented:** code and automated coverage exist.
- **Partial:** substantial code exists, but integration, reliability, or acceptance work remains.
- **Missing:** the product capability has not yet been built.

| Area | Status | Current reality |
| --- | --- | --- |
| Platform-neutral meeting runtime | Implemented | Shared adapter contract and registry drive Zoom, Teams, and Meet. |
| Zoom web adapter | Partial | Join, admission, audio, mute, and lifecycle behavior exist and Zoom has had live use. Final regression acceptance remains necessary. |
| Microsoft Teams adapter | Partial | Guest-first join, Microsoft profile fallback, audio, mute, participant count, and termination handling exist. Tenant-policy and audio acceptance coverage remains limited. |
| Google Meet adapter | Partial | Guest-first join and Google profile fallback exist with fixture coverage. Full live acceptance remains outstanding. |
| Meeting container | Implemented | One image, `colleague-meeting:local`, from `Dockerfile.meeting` (Playwright Chromium, PulseAudio, Xvfb, x11vnc/noVNC), built on the first meeting. Uses a vendored subset of Joinly for the browser session, virtual devices, camera feed, and Teams/Meet controllers. |
| Continuous GPT-Live voice | Partial | One `gpt-live-1` session, `store: false`, audio context, and transcript events. `COLLEAGUE_VOICE` selects the voice. Meetings delegate hard questions to a Responses backend (`COLLEAGUE_MEETING_BACKEND_MODEL`), covered by bridge tests; not yet heard in a live meeting. Conversational quality and platform audio reliability still need evaluation. |
| Meeting AI disclosure | Partial | Once admitted and able to speak, a meeting session says one short AI disclosure naming the person it acts for (call brief, then `COLLEAGUE_OWNER_NAME`), then listens; `COLLEAGUE_MEETING_INTRO=0` turns it off. Covered by unit and bridge tests with a fake GPT-Live socket; not yet heard in a live meeting. |
| Selective speech and mute transport | Partial | GPT-Live-driven participation, virtual audio gating, and public drain/discard of queued playback exist, with automated tests. The platform microphone unmutes once per session and mutes at the end; between replies only the local gate closes. Zoom mute prefers a visible control and falls back to the in-meeting shortcut. When a Zoom host blocks self-unmute, the adapter accepts the host's "Ask to unmute" request (tests with fakes). Cross-platform live acceptance, including that request, is still open. |
| Local runtime daemon | Implemented | Authenticated loopback HTTP/SSE API owns calls, meetings, events, and supervision. Opt-in server mode accepts long-lived API tokens. |
| Phone calls | Partial | Outbound and opt-in inbound calls through SignalWire or Twilio, relayed media streams with G.711 passed straight to GPT-Live, Responses delegation with an `end_call` tool, AI disclosure check, voicemail detection, transfer to the owner, rehearsal, watchdogs, signed gateway, and a Cloudflare quick tunnel. Relayed SignalWire calls to real people worked on 2026-09-29. Direct SIP exists behind a setting and has no live call yet. See [phone calls](phone.md). |
| Calls API | Partial | `/v1/calls` accepts a brief, runs it on a line (phone or meeting), and publishes a result through polling, long-polling, SSE, or a signed webhook, with per-call cost. Hooks for owner, credentials, pre-call policy, usage, profile, and notification exist. Meetings through it have not yet been run live on this build. See [calls](calls.md). |
| Call context | Implemented | Owner profile, session context in the brief, and the goal, with silent notes mid-call. Covered by tests; not yet heard on a live call. |
| Durable event and meeting storage | Implemented | Versioned schemas, append-only events, transcript/archive records, validation, recovery, and local retention paths exist. |
| Meeting context | Implemented | Versioned objective, task, summary, decisions, constraints, questions, and files. The console turns pasted text and common document formats into it; a call brief turns its context into it. Enterprise connectors are not implemented. |
| Meeting results | Partial | A deterministic handoff built from the transcript becomes the call result. Richer summaries remain open. |
| Virtual camera presence | Implemented | Presence states exist without exposing task content. Live compatibility needs continued testing. |
| TypeScript SDK | Implemented locally | Call, profile, and voice methods. Package is not published. |
| Python SDK | Implemented locally | Mirrors the TypeScript SDK. Package is not published. |
| CLI | Implemented locally | `call` (phone or `--meeting`), `calls`, `profile`, `voices`, agent-driven `setup`, and `connector`. Distribution and installer UX remain. |
| MCP server | Implemented locally | Thin stdio server over the TypeScript SDK with the call tools (`start_call`, `wait_for_call`, and more). |
| Local console | Implemented | Meetings and Calls tabs at `127.0.0.1:8095`. It should remain optional. |
| Remote connector | Partial | MCP Streamable HTTP with OAuth 2.1 (dynamic registration, PKCE, owner-passphrase approval, rotating tokens stored as digests) exposes the same call tools to cloud agents through a TLS proxy. Covered by tests and a local browser run; not yet connected from ChatGPT or Claude. See [agents](agents.md). |
| Packaging and onboarding | Partial | A copied prompt lets the user's agent follow [SETUP.md](../SETUP.md): `colleague setup status --json` lists what is missing with the question to ask, keys go on a one-time local page, `setup register` connects Claude Code, Codex, and Cursor, and `setup call-me` rings the user. Node and Docker are enough. Daemon-started meetings build the meeting image when missing, run the container as the host user, and need no `.env.meeting`. There is no published SDK/CLI/MCP package, and the flow has not been run end to end by a new user. |
| Outgoing and incoming screen sharing | Missing | Colleague AI does not share a screen or read shared screens. |
| Automated QA | Strong but incomplete | Broad unit coverage exists. Automated tests cannot prove browser selectors, tenant policy, admission, audio quality, or real provider behavior. |
| Live acceptance | Incomplete | A complete acceptance run across phone calls and Zoom, Teams, and Meet on the current build is still required. |

## How far we are

These percentages are directional estimates, not release metrics:

- **Local proof of concept:** approximately **90%**. The system can place phone calls, join meetings, converse, retain transcripts, and report results.
- **Local developer preview:** approximately **75–80%**. The shared daemon, calls API, phone line, adapters, SDKs, CLI, MCP server, remote connector, and agent-driven setup exist; live meeting acceptance on this build and conversational quality remain unfinished.
- **Reliable beta:** approximately **50–60%**. Distribution, lifecycle polish, cross-platform acceptance, telemetry, recovery UX, and compatibility support are still required.
- **Hosted multi-user product:** approximately **20–30%**. The call hooks and server mode exist, but accounts, organizations, encrypted secret management, a hosted control plane, billing, and production isolation are not built.

## Highest-priority product gaps

### 1. Complete live acceptance and audio reliability

Run repeatable real calls and meetings covering:

- Phone: disclosure, voicemail and call screeners, take-over, rehearsal, and direct SIP once OpenAI enables it.
- Meetings: guest join, lobby, admission, and signed-in fallback on Zoom, Teams, and Meet.
- Continuous listening, transcript continuity, and backend answers.
- Selective response behavior in a multi-person conversation.
- Complete first and subsequent spoken sentences.
- Platform unmute, virtual microphone playback, interruption, and remute.
- Host mute and policy-blocked microphone behavior.
- Empty-room and meeting-ended detection.
- Stop, restart, archive, and the result.

### 2. Make conversation natural

- Direct SIP for phone calls, so audio does not detour through this computer.
- Latency and conversational timing in calls and meetings.

### 3. Package the product

- Publish or bundle supported SDK, CLI, and MCP packages.
- Add one-command installation and update paths.
- Keep the first-run diagnostic current for Docker, audio, browser, API keys, and phone setup.
- Keep the console as an optional local dashboard.

### 4. Make results dependable

- Produce structured meeting summaries, decisions, action items, owners, and unresolved questions beyond the transcript-based handoff.
- Let agents send live instructions to a meeting in progress.

### 5. Prepare for safe company context

- Define connector contracts for company databases, documents, tickets, and knowledge systems.
- Scope every connector by identity, call, and permission.
- Start with read-only queries and auditable provenance.
- Add retrieval budgets, redaction, retention policy, and source citations.
- Never treat retrieved content as permission to perform an action.

### 6. Build hosted operation only after the local product is trustworthy

- Accounts, organizations, roles, and device ownership.
- Encrypted secret and browser-profile handling.
- Tenant isolation, audit logs, budgets, rate limits, health, and upgrades.

## Near-term milestone plan

### Milestone A — Live acceptance on the current build

- [x] Relayed SignalWire phone calls to real people (2026-09-29).
- [ ] A meeting joined through `start_call` on this build, with a spoken backend answer and a result.
- [ ] Pass the full Zoom acceptance script.
- [ ] Pass the full Teams acceptance script with guest and account-fallback cases.
- [ ] Pass the full Google Meet acceptance script with guest and account-fallback cases.
- [ ] Record a compatibility matrix with tested browser/provider versions and policy limitations.

**Exit criterion:** the documented supported calls and meetings consistently connect, listen, speak completely, end, and return a result.

### Milestone B — Natural phone calls

- [ ] A live direct-SIP call once OpenAI enables outbound SIP.
- [ ] Tell automated call screeners apart from voicemail.
- [ ] Measure and reduce reply delay.

**Exit criterion:** a phone call feels close to talking with ChatGPT voice.

### Milestone C — Distribution

- [x] Decide supported installation form: one Docker image, `ghcr.io/kaelorlabs/colleague`, started with one `docker run`.
- [x] Package the daemon, console, CLI, MCP endpoint, and meeting image; update by pulling the image again.
- [x] Document uninstall, credential removal, and data deletion (SETUP.md, architecture retention table).
- [ ] Make the GHCR packages public, and confirm a first install on a clean Mac and a clean Windows machine.
- [ ] Recruit a small external cohort and capture consented reliability metrics.

**Exit criterion:** someone outside the original machine can install Colleague AI from their agent and complete a first call and a first meeting.

### Milestone D — Useful meeting outcomes

- [ ] Generate dependable structured meeting outcomes.
- [ ] Improve speaker attribution and late-join context when platform data permits it.
- [ ] Measure response usefulness, false interventions, and result completeness.

**Exit criterion:** people send Colleague AI to recurring meetings because it reduces follow-up work.

### Milestone E — Hosted team product

- [ ] Design and threat-model the production control plane.
- [ ] Add accounts, organizations, roles, and encrypted configuration.
- [ ] Add multi-tenant storage, retention controls, audit logs, budgets, and billing.
- [ ] Add safe upgrades, health monitoring, incident controls, and support tooling.

**Exit criterion:** a team can operate Colleague AI across approved users without sharing one person's local configuration.

## Explicitly out of scope for the immediate release

- Government Teams, webinars, and town halls.
- Silent collection of credentials or scripted password entry.
- Chain-of-thought or private reasoning transfer.
- Calls without an AI disclosure.
- Bringing coding agents into meetings, or acting on a code workspace from a call.
- Screen sharing, outgoing or incoming.
- A hosted service.

## Success measures

Track product outcomes rather than only code completion:

- Time from "call" or "join this meeting" to connected and listening.
- Percentage of calls and meetings completed without console or noVNC intervention.
- Percentage of spoken responses delivered completely on the first attempt.
- Reply delay on phone calls.
- False-intervention rate in multi-person meetings.
- Result accuracy: whether the outcome and details match the transcript.
- Recovery success after daemon, container, browser, or network interruption.
- Install-to-first-call time for a new user.

## Definition of a beta

Colleague AI is ready for a beta when all of the following are true:

- A new user can install it from their agent without editing repository source.
- Any MCP-capable agent can place a phone call and join a meeting from a brief and read the result.
- The console is optional for the normal successful path.
- Phone calls and Zoom, plus at least one of Teams or Meet, pass the complete live acceptance suite repeatedly.
- The agent listens continuously, speaks selectively, and delivers full audio reliably.
- Secrets, transcripts, browser profiles, and context remain local and gitignored.
- Installation, diagnostics, cancellation, recovery, upgrade, and deletion are documented.
- Capability claims match tested behavior.

## Instructions for contributors

Before implementing a substantial change:

1. Read this document and the linked architecture and calls documents.
2. Identify which milestone and user outcome the change advances.
3. Check the working tree and do not overwrite unrelated changes.
4. Preserve the settled decisions unless the task explicitly changes one.
5. Prefer the one calls API over adding another execution path.
6. Keep platform DOM details inside adapters and phone provider details inside the phone line and gateway.
7. Add or update boundary tests for schemas, authorization, lifecycle, and recovery.
8. Do not claim a browser, audio, or phone path works from fixtures alone; label live acceptance separately.
9. Update the table, milestone checklist, and review date when the implementation status changes materially.
10. Include evidence in the commit or PR: tests run, live acceptance performed, limitations, and any follow-up work.

When updating this file, use these rules:

- Mark **Implemented** only when the code path and meaningful automated tests exist.
- Mark **Partial** when implementation exists but integration, live acceptance, distribution, or reliability work remains.
- Mark a live acceptance checkbox complete only after a real call or meeting on the current implementation.
- Never remove an unresolved limitation solely because it is inconvenient for presentation.

## Progress log

Entries before 2026-09-30 describe the product as it was then, including the coding-agent features removed on that date.

### 2026-09-30

- One service: users need only Docker. `ghcr.io/kaelorlabs/colleague` (about 600 MB) runs the daemon and the console, carries the CLI (`docker exec colleague colleague ...`), and serves MCP to local agents over Streamable HTTP at `127.0.0.1:8095/mcp` with a local token (Claude Desktop uses `docker exec -i colleague colleague mcp`). Settings and records live in the `colleague` volume. The meeting image carries its code and shares that volume. `.github/workflows/images.yml` tests pull requests and publishes both images for amd64 and arm64. SETUP.md is now Docker-only. Checked end to end on a fresh volume: setup status, the setup page, MCP over HTTP and stdio, and starting the meeting container from inside the colleague container.
- Product direction narrowed to phone calls and meetings for any AI agent. The coding-agent direction was dropped: bringing Codex, Cursor, or Claude Code into meetings, exact-session continuity, session leases, and handoff append. Recorded decision 24 and retired decisions 6, 7, 8, 14, 15, and 18.
- Removed with it: coding-agent providers and workers, runner pairing and the hosted-runtime mock control plane, approvals, workspace actions, the Git broker, charts, Tavily web search, screen-share capture and analysis, the unused `gpt-live/` voice demo, the separate meeting MCP tools and SDK meeting handles, the CLI meeting commands, the `join-colleague-ai-meeting` skill, and the Codex installer.
- Agents join meetings through the calls API: `start_call` with channel `meeting` and the invite URL as `to` (CLI: `colleague call --meeting <url>`). `/v1/meetings` keeps only what the console uses, and the call brief no longer has `agentSession`.
- Meetings now delegate hard questions to a Responses backend, as phone calls do: `COLLEAGUE_MEETING_BACKEND_MODEL` (default `gpt-5.6-terra`) and `COLLEAGUE_MEETING_WEB_SEARCH=1` for OpenAI web search.
- One meeting image, `colleague-meeting:local`, from `Dockerfile.meeting`, about 1.8 GB instead of about 4 GB for the two previous images. `compose.yaml` and `Dockerfile.login` are gone. `joinly/` is now a subset: browser session, virtual devices, camera feed, and Teams/Meet controllers; its speech models and services were removed.
- The console has a Calls tab and a Meetings tab. Host Python is no longer needed for anything; Docker is enough.
- The runtime suite passes in `colleague-meeting:local` with `joinly/` mounted. A live meeting on this build has not been run yet.

### 2026-09-28

- Product direction widened: Colleague AI becomes a phone and meeting tool that any agent can use through a brief, self-hosted under Apache-2.0 first and managed later. Recorded decisions 21 to 23.
- Licensed the project under Apache-2.0 with a NOTICE for vendored Joinly (MIT). Added LF line-ending rules so Windows checkouts keep working scripts, documented Windows through WSL2, and taught the doctor to check both.
- Shared-content change detection now compares 64x36 tile signatures (luminance and edge strength), selects a frame only after it settles (`settleTicks`, default 1), masks regions that keep animating, and reuses the observation for any of the last 32 analyzed screens instead of calling the analyzer again (`reused: true`). Live acceptance on real Zoom/Teams/Meet shares stays open.
- Added the calls core: brief validation with questions for missing fields, durable call records and events, results from a strict-schema summary or the meeting handoff, signed webhooks with retries, extension hooks, a transport-neutral GPT-Live session module, the meeting line, `/v1/calls` routes with an OpenAPI description, and opt-in server mode with API tokens. Covered by unit and HTTP tests; no live acceptance yet.
- The meeting bridge still drives GPT-Live directly. Moving it onto the shared voice module waits for a live meeting to verify the change.
- Added phone calls through Twilio: the phone line, Twilio client with signature checks, a separate loopback gateway for Twilio routes, a Cloudflare quick tunnel for laptops, inbound message-taking behind a setting, and docs. A real call has not been placed yet.
- Known issue from the hackathon video (corrected): when a Zoom host disables "Allow participants to unmute themselves", the one platform unmute at session start fails, so Colleague AI can never speak in that meeting. `zoom_controls.accept_host_unmute` accepted the host's "Ask to unmute" dialog and had tests, but nothing called it. The runtime does not re-mute the platform after each reply (only its local gate closes), so accepting the request once is enough; wired in below.
- Meeting runtime fixes from acceptance review. The meeting container now runs as the host user (`COLLEAGUE_UID`/`COLLEAGUE_GID`, set by the daemon and `start-meeting-agent.sh`), so it can read the private runtime state and the host can read its recordings. Verified by starting the real image through `ProductionMeetingSupervisor.start()` on WSL2 with Docker Engine: before, the bridge failed with `PermissionError` on `runtime.json` and Docker created the mount directories as root; after, it read the runtime state, loaded the Zoom page, wrote recordings as the host user (0700/0600), and the host built the handoff from them. The daemon builds the Joinly base image when it is missing, creates the bind-mount directories itself, and `.env.meeting` is optional.
- Meetings open with one short AI disclosure naming the person Colleague AI acts for, cued as GPT-Live commentary once the meeting microphone can carry speech. `COLLEAGUE_MEETING_INTRO=0` turns it off, and `COLLEAGUE_VOICE` now sets the meeting voice. The Zoom adapter accepts the host's "Ask to unmute" dialog while the microphone is blocked or muted, then arms the microphone. Both are covered by tests with fakes; neither has been heard in a live meeting yet.
- Screen understanding: Codex image analysis runs as an async subprocess and parses only stdout, so it no longer stalls the daemon's HTTP, SSE, approvals, and phone gateway; a capture or file error degrades the status instead of ending the meeting; observations that arrive while Colleague AI speaks are deferred (newest only); and the daemon's `analyzerAvailable` is no longer overwritten by the container.
- Doctor: example placeholder keys count as missing, WSL1 is reported separately from WSL2, a failing Compose check shows its first error line, Codex checks warn unless Codex exact continuity is installed (or `--codex` is passed), and python3 with venv support is checked. Removed the unused `live/` experiment with its broken `start-live.sh`, made the remaining launchers executable, and added Apache-2.0 license metadata to the root and SDK packages.
- Added agent access and agent-driven setup: call methods in both SDKs, MCP call tools, `colleague call`/`calls`/`voices`, `colleague setup` (status with questions, local key page, settings, registration, voice, test call), SETUP.md with the copy-paste prompt, a call-briefing skill, a calls view in the console with live transcript, end, and hand-over, and the remote connector for cloud agents.
- Final acceptance review against the product goals (self-hosted, any agent, brief in and result out, wow first run, voice any time, keys never in chat), with fixes:
  - Phone: the spoken AI disclosure is checked in common languages and must name the owner, and outgoing results carry `disclosureVerified`; audio goes to Twilio in 20 ms frames; take-over says a fallback line when the owner does not answer; recordings are attached through a signed callback; incoming calls are capped and always ring the bought Twilio number; the quick tunnel is probed before Twilio gets the address; result webhooks refuse hosts that resolve to private addresses.
  - Briefs: `onBehalfOf` defaults to the owner name, rehearsals ring only the owner phone, and a verified caller ID alone is enough for outgoing calls. The remote connector hides and refuses `agentSession`.
  - Setup: status checks Python venv support, suggests the only Twilio number instead of asking, reports `firstCallReady`, and keeps phone steps optional; the setup page runs in the background, accepts several saves and a Done, and collects the caller ID and Tavily key; `setup start` shows progress and readable errors; `setup voice --preview` calls the owner in another voice; registration from WSL covers Windows apps and installs the skills. SETUP.md was rewritten around this flow.
  - UI: the calls view, setup page, connector approval page, and CLI output were reworked to match the console in light and dark, with plain-language states and results.
  - The runtime daemon runs in Docker when this computer cannot make a Python venv (`Dockerfile.daemon`: Python, aiohttp, the Docker CLI for meeting containers, and cloudflared for the phone tunnel). A new user needs only Node and Docker. Host Python stays the choice when available, because coding agents run on the host with the user's logins.
  - Phone calls also work through SignalWire, whose free trial allows live audio streaming (Twilio's 2026 trial strips `<Stream>`, found in the first new-user walkthrough). Same REST, webhook, and media-stream code; setup status flags a Twilio trial as unusable.
  - Direct SIP for phone calls (`COLLEAGUE_PHONE_AUDIO=sip` or `sip-webhook`): call audio flows between the provider and OpenAI, and Colleague AI steers over GPT-Live's text sideband. OpenAI dialing out needs outbound SIP enabled for the organization; until then calls fall back to the relay. `colleague setup sip-trunk` creates the SignalWire trunk. Covered by fixture tests; no live SIP call yet.
  - The relay now follows interruptions (drops speech GPT-Live abandoned), lets a hang-up give way to someone still talking, treats the carrier's machine verdict as a hint, and records reply delays and queued speech per call.
  - Three levels of call context: the owner's profile (`/v1/profile`, `colleague profile`, and the MCP `get_profile` and `update_profile` tools), the session context in the brief (text, or a summary, facts, decisions, open questions, and long details), and the goal (objective, questions, tone, contact). The goal and the profile's standing boundaries go into the voice instructions; a short reference version of the rest starts the GPT-Live session as a developer message marked as background, not an agenda; the backend model gets everything. Agents can add silent notes mid-call. Covered by tests; not yet heard on a live call.
  - Still open: a live direct-SIP call (waiting for OpenAI to enable outbound SIP) and a real meeting on this build. Relayed SignalWire calls to real people worked on 2026-09-29.

### 2026-09-18

- Marked the participation and microphone fix complete: queued playback now drains and discards through the public microphone API, with automated coverage. Live cross-platform acceptance stays open.
- Recorded the Zoom mute path that prefers a visible enabled control, verifies state, and falls back to Alt+A when a click does not change mute state.
- Recorded Codex `exec` options placed before `resume` and the prompt, and meeting-capacity release when transcript handoff fails, including the host jobs directory for provider append.
- Disabled screen-share settings now keep the published retention shape. Contributor entry is `AGENTS.md`. The unused CopilotKit starter kit is no longer in the tree.

### 2026-09-17

- Consolidated the product vision, settled decisions, implementation inventory, gaps, and milestone checklist.
- Confirmed the repository contains the shared daemon, Zoom/Teams/Meet adapters, GPT-Live client delegation, Codex/Cursor/Claude provider adapters, session leases, SDKs, CLI, MCP adapter, approvals, isolated workspace execution, Git broker, artifacts, virtual camera, and optional screen observation.
- Previously identified the main product gap as a turnkey host integration that supplies the active coding-agent session and context; the current Codex path launches a CLI child of the active task so the task ID does not cross an unreliable persistent-MCP environment boundary.
- Confirmed the portal remains an optional context-continuity operations console rather than the desired primary user experience.
- Recorded live cross-platform acceptance, distribution, meeting output delivery, enterprise context connectors, and hosted multi-user operation as unfinished work.
- Retained the Codex-native `join_current_meeting` MCP entry point for compatibility and controls, while moving identity-critical launch to the task-local CLI.
- Added an idempotent Codex integration installer, safe CLI defaults for the current Codex thread/workspace and installation root, and a local diagnostic command.
- Hardened the agent-native join workflow after a cross-task usability test: added offline handoff validation, an exact schema example and privacy-minimized skill instructions, active-meeting discovery, actionable conflict errors, `join --replace`, aligned TypeScript/Python/runtime `git` validation, and truthful loopback-permission errors. The microphone policy remains selective automatic speech rather than an always-unmuted mode.
