# Smitline product roadmap

This document describes how Smitline becomes a product that people can operate repeatedly and trust: phone calls and meetings for any AI agent. Work is organized around user outcomes.

## Product principle

Smitline should behave like a prepared assistant that an agent can send on a call: easy to brief, honest that it is an AI, useful when addressed, careful with private context, and clear about what happened when the call ends.

## Milestone 1 — Reliable local product

Goal: one person can configure, start, observe, stop, and restart calls and meetings without editing source code.

- [x] Keep the meeting policy general and accept bounded operator guidance instead of coded scenarios.
- [x] Validate participant name, meeting guidance, keys, and meeting URL before startup.
- [x] Represent joining, waiting, muted, listening, leaving, and failure states explicitly.
- [x] Ensure meeting audio is forwarded exactly once and stale output is discarded after muting.
- [x] Preserve transcripts and traces with stable identifiers and retention controls.
- [x] Add deterministic tests for configuration, meeting guidance, mute transitions, and meeting lifecycle.
- [x] Replace the shell-and-JSON status experience with a small local console.
- [x] Let any agent start a phone call or meeting from a brief and read a structured result.

Exit criteria: not met until a live meeting on the current build proves a spoken exchange end to end. The console, explicit states, transcript retention, and mute tests already exist.

## Milestone 2 — Phone calls

Goal: an agent can place a phone call for its user and get a trustworthy result.

- [x] Outbound calls through SignalWire or Twilio with an AI disclosure check, voicemail detection, rehearsal, and take-over.
- [x] Owner profile, session context, and goal as three levels of call context.
- [x] Per-call cost and spend totals.
- [ ] A live call with audio directly between the provider and OpenAI over SIP.
- [ ] Tell automated call screeners apart from voicemail.
- [ ] Make phone conversation feel as natural as ChatGPT voice.

Exit criteria: a user's agent places real calls to businesses and people, and the results match what was said.

## Milestone 3 — Useful context

Goal: Smitline knows what it needs for the call and nothing more.

- [x] Let operators paste notes and upload common documents as private meeting context.
- [x] Hand hard questions to a backend model that knows the brief, with optional web search.
- [ ] Add managed company data connectors with scoped credentials, access policies, and auditable read-only queries.
- [ ] Show backend progress and sources to the owner.

Exit criteria: a user can see exactly what context a call had, and the answers can be traced to it.

## Milestone 4 — Multi-user product

Goal: a small team can use Smitline without sharing one person's machine configuration.

- [ ] Add authenticated accounts, teams, encrypted secret storage, and role-based permissions.
- [ ] Introduce a session service and durable metadata store while keeping raw meeting data retention configurable.
- [ ] Add deployment packaging, migrations, structured telemetry, rate limits, budgets, and audit logs.
- [x] Separate meeting adapters from the voice runtime.
- [ ] Test supported Zoom configurations and publish a compatibility matrix.

Exit criteria: invited team members can launch calls using approved integrations, with clear ownership, isolation, billing controls, and auditable actions.

## Milestone 5 — Meeting-native collaboration

Goal: Smitline contributes naturally throughout a real meeting.

- [ ] Improve turn-taking, interruptions, speaker attribution, and latency.
- [ ] Add meeting summaries, decisions, assigned actions, and follow-up workflows beyond the transcript-based handoff.
- [ ] Take live instructions from the agent during a meeting.
- [x] Add more meeting providers only behind the common adapter contract and provider-specific tests. Zoom, Teams, and Meet share that contract.
- [ ] Evaluate quality using recorded consented scenarios, including false-positive interventions and answer accuracy.

Exit criteria: people choose to send Smitline to recurring meetings because it reduces follow-up work.

## Current boundary

The repository is a **local** product: phone calls through SignalWire or Twilio and Zoom, Teams, and Google Meet meetings through a loopback daemon on this computer. OpenAI and phone provider calls use the operator's accounts. The meeting web client is automated through a browser in a local Docker container. There is no hosted service, multi-tenant isolation, encrypted secret service, or support guarantee yet.
