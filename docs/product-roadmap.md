# Colleague AI product roadmap

This document describes how Colleague AI becomes a product that a team can operate repeatedly and trust. Work is organized around user outcomes.

## Product principle

Colleague AI should behave like a quiet, prepared teammate: easy to invite, explicit about what it can access, useful when addressed, careful with private context, and observable when a tool is working or fails.

## Milestone 1 — Reliable local product

Goal: one developer can configure, start, observe, stop, and restart a meeting agent without editing source code.

- Keep the meeting policy general and accept bounded operator guidance instead of coded scenarios.
- Validate participant name, meeting guidance, model, keys, and meeting URL before startup.
- Represent joining, waiting, muted, listening, tool-running, leaving, and failure states explicitly.
- Ensure meeting audio is forwarded exactly once and stale output is discarded after muting.
- Preserve transcripts and tool traces with stable identifiers and retention controls.
- Add deterministic tests for configuration, meeting guidance, mute transitions, tool failures, and meeting lifecycle.
- Replace the shell-and-JSON status experience with a small local control surface.

Exit criteria: a user can paste a meeting invite into a local UI, start one agent, understand every state, complete a spoken tool call, end the session cleanly, and reopen its transcript.

## Milestone 2 — Workspaces and useful tools

Goal: the agent can safely help with a real project through an explicitly selected workspace.

- Let users select an explicit Codex workspace and show its access mode.
- Add tool permissions per meeting: web search, Codex analysis, database connectors, and artifacts.
- Let operators paste notes and upload common documents, then retrieve bounded passages through a local context-search tool.
- Add managed company data connectors with scoped credentials, schema discovery, access policies, and auditable read-only queries.
- Carry relevant meeting context into Codex while keeping resumable sessions scoped to a workspace and meeting.
- Show tool progress and source links to the organizer.
- Deliver generated charts through a reliable local artifact page before adding platform-specific chat delivery.

Exit criteria: a user can choose a real workspace, see exactly what the agent may access, run a read-only task, and inspect the answer and sources after the meeting.

## Milestone 3 — Multi-user product

Goal: a small team can use Colleague AI without sharing one developer’s machine configuration.

- Add authenticated accounts, teams, encrypted secret storage, and role-based permissions.
- Introduce a session service and durable metadata store while keeping raw meeting data retention configurable.
- Add deployment packaging, migrations, structured telemetry, rate limits, budgets, and audit logs.
- Separate meeting adapters from the reasoning and tool runtime.
- Test supported Zoom configurations and publish a compatibility matrix.

Exit criteria: invited team members can launch agents using approved integrations, with clear ownership, isolation, billing controls, and auditable actions.

## Milestone 4 — Meeting-native collaboration

Goal: Colleague AI contributes naturally throughout a real work cycle.

- Improve turn-taking, interruptions, speaker attribution, and latency.
- Add meeting summaries, decisions, assigned actions, and follow-up workflows.
- Support confirmed delivery of links and artifacts into meeting chat.
- Add more meeting providers only behind the common adapter contract and provider-specific tests.
- Evaluate quality using recorded consented scenarios, including false-positive interventions and tool-grounding accuracy.

Exit criteria: teams choose to bring the agent into recurring meetings because it reduces follow-up work and improves access to trustworthy information.

## Current boundary

The repository is a **local** product: Zoom, Teams, and Google Meet through a loopback daemon on this computer. OpenAI and Tavily calls use the operator’s accounts. The meeting web client is automated through a local browser. Hosted runtime pairing is a foundation, not production hosting. There is no multi-tenant isolation, encrypted secret service, or support guarantee yet. Chart files are generated locally; Zoom attachment delivery remains experimental. Cursor and Claude Code are optional capability-detected adapters. Exact continuity requires a host-supplied session id.
