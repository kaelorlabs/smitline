# Colleague AI: product vision, decisions, and progress

**Document status:** canonical product brief and progress ledger  
**Last reviewed:** 2026-09-17  
**Current stage:** advanced local alpha / developer preview  
**Primary implementation branch at this snapshot:** `agent/zoom-teams-adapters`

This document is the durable product memory for Colleague AI. It explains what we are building, why it matters, which decisions are settled, what already exists, and what remains. Coding agents should read this document before proposing architecture or product changes and update the progress ledger when a milestone materially changes.

Related technical references:

- [Architecture](architecture.md)
- [Capability matrices](capabilities.md)
- [Coding-agent providers](coding-providers.md)
- [Meeting adapters](meeting-adapters.md)
- [Control panel](control-panel.md)
- [Developer platform implementation plan](developer-platform-implementation-plan.md)
- [Product roadmap](product-roadmap.md)

## Product in one sentence

Colleague AI lets a developer bring the coding agent and project context they are already working with into a live meeting, collaborate with it by voice, allow it to perform explicitly authorized work, and continue in the same coding conversation after the meeting ends.

## The problem

Developers divide their work between coding agents and meetings. The coding agent knows the repository, current task, technical decisions, and recent debugging history, but the people in the meeting do not share that interface. The developer repeatedly restates context, copies answers between tools, takes notes, and manually translates meeting decisions back into development work.

Existing meeting assistants mainly transcribe and summarize. Existing coding agents mainly wait in a chat or editor. Neither feels like the same colleague moving with the developer between written work, live discussion, and implementation.

## The product we want

A developer should be able to tell the coding agent they are already using:

> Join this meeting. Bring the relevant context from our work, listen to the team, help when useful, carry out only the actions I authorize, and continue here when the meeting ends.

Colleague AI should then:

1. Receive the meeting invite, current coding-agent session identifier, workspace, structured context, and permissions from the host integration.
2. Temporarily lease the originating coding-agent session so the meeting and chat cannot create conflicting work.
3. Join Zoom, Microsoft Teams, or Google Meet as one visible participant named **Colleague AI**.
4. Keep one continuous `gpt-live-1` session connected during the call so it retains the original audio conversation and can participate naturally.
5. Listen continuously but speak selectively, like a quiet teammate rather than a voice assistant responding to every utterance.
6. Delegate technical work to the originating coding-agent session while remaining present in the call.
7. Show a safe, non-sensitive visual presence for listening, working, and speaking.
8. Require explicit permission or approval for commands, edits, network access, commits, and pushes.
9. Persist transcripts, events, decisions, artifacts, approvals, and technical results outside model memory.
10. Append a structured handoff to the originating coding conversation and release its lease when the meeting ends.

The ideal experience is that the developer talks to the same technical colleague before, during, and after the meeting.

## Primary user experience

### Agent-native launch

The primary product path is the coding agent, not a separate website:

```text
Developer in Codex, Cursor, or Claude Code
                  |
                  | "Join this call with our current context"
                  v
       Colleague AI SDK / CLI / MCP adapter
                  |
                  v
          Local runtime daemon
             /           \
            v             v
     Meeting adapter    Coding-agent provider
     Zoom/Teams/Meet    Same session + workspace
            \             /
             v           v
             GPT-Live collaboration
                  |
                  v
       Final handoff to original session
```

The host integration is responsible for supplying the real session ID and a bounded, structured context handoff. Colleague AI must never invent a session ID, infer `last` or `latest`, or claim exact continuity when the host cannot provide it.

### Optional operations console

`http://127.0.0.1:8095` remains an optional local operations console. It is useful for manual launches, diagnostics, approvals, account connection, transcript review, artifacts, and meeting state. It calls the same daemon as the SDK, CLI, and MCP adapter.

The console is not the intended long-term primary launcher. A developer starting Colleague AI from a supported coding-agent integration should not need to open it. Portal-launched meetings use **context continuity**, because the portal does not possess the originating coding-agent thread ID.

### During the meeting

- Colleague AI joins as one participant; individual backend agents do not join separately.
- GPT-Live handles audio understanding, conversational timing, interruptions, and spoken delivery.
- GPT-Live remains connected while delegated work executes and may acknowledge that work is in progress.
- The coding-agent provider handles technical reasoning and repository-aware work.
- The runtime, not the model prompt, enforces permissions and approvals.
- A virtual camera may show listening, working, or speaking state without exposing task text or private content.
- Incoming shared-content observation is opt-in and off by default.

### After the meeting

- Transcript and runtime events are durable locally.
- Decisions, action items, artifacts, work results, and unresolved questions become a structured handoff.
- Exact-continuity meetings append that handoff once to the originating coding-agent conversation.
- The exclusive session lease is released only after finalization is durably complete or its failure is durably recorded.
- The developer resumes the same coding conversation and can continue implementation from the meeting outcome.

## Continuity modes

### Exact continuity

Exact continuity is the core product promise. It requires a host integration to provide a real, resumable session ID.

- The runtime leases the originating session.
- Delegated turns resume that session when the provider supports it.
- The final handoff is appended once to that session.
- Codex is the first complete target.
- Cursor and Claude Code use exact mode only when their installed CLI documents the necessary resume capability.

### Context continuity

Context continuity is a fallback for the portal and generic clients.

- The caller supplies a structured summary, recent conversation, files, and constraints.
- The runtime uses `local-portal` or another explicitly context-only identity.
- A new provider interaction may be created from that context.
- The product must label this truthfully and never present it as resuming the original conversation.

## Settled product and architecture decisions

These decisions should not be reversed casually. A proposal to change one should explain the user benefit, migration, security effect, and effect on exact continuity.

1. **Runtime and SDK first.** The daemon owns meetings. SDKs, CLI, portal, and MCP are clients of the same runtime. MCP is a compatibility surface, not a second implementation.
2. **The portal is optional.** It is an operations and fallback console, not the final agent-native experience.
3. **One continuous GPT-Live session.** Use `gpt-live-1` from admission through meeting shutdown with `store: false`. Do not switch to transcription-only mode and lose original audio context.
4. **GPT-Live owns conversational behavior.** It decides when to speak and how to handle pauses and interruptions. Do not add local silence timers, backchannel classifiers, wake phrases, or an `Allow speaking` product toggle as the normal interaction model.
5. **Selective participation.** Instructions should make Colleague AI a quiet, useful teammate. It should answer direct requests and intervene when necessary for a material factual or safety correction, not reply to every sentence.
6. **Client delegation.** GPT-Live delegates technical work to the local runtime. There is no extra general-purpose Responses model between GPT-Live and the coding-agent provider.
7. **Resume the real originating session.** Never use a meeting URL hash, `last`, `latest`, or a fabricated identifier as the source of continuity.
8. **Exclusive session lease.** The originating conversation is owned by the meeting until finalization, preventing competing turns and conflicting work.
9. **Application-owned memory.** Transcripts, events, artifacts, approvals, and handoffs are the system of record. Model context alone is insufficient.
10. **Explicit context, never hidden reasoning.** Transfer objectives, summaries, decisions, constraints, open questions, relevant recent turns, files, and Git state. Do not request chain-of-thought or hidden traces.
11. **Platform adapters.** Zoom, Teams, and Meet DOM and policy differences remain behind a shared meeting adapter contract.
12. **Guest first, account fallback.** Teams and Meet attempt guest entry before using a locally connected account. Credentials are entered by the user on the official provider page and are never collected by Colleague AI.
13. **Local-first security boundary.** The supported runtime is a loopback daemon. Coding-agent logins, browser profiles, credentials, workspaces, transcripts, and artifacts remain local.
14. **Typed permissions enforced in code.** Context is never authorization. Commands, edits, network calls, commits, and pushes use explicit modes and one-time approvals.
15. **Isolated mutations.** Approved edits and commands run in an isolated worktree. Commit and push operations use a typed Git broker and separate authorization.
16. **Operator/platform mute is authoritative.** Colleague AI must not override an explicit host or participant mute. Generated audio is discarded when the meeting microphone is unavailable.
17. **No sensitive camera output.** The virtual camera communicates presence only. It never displays prompts, code, task text, credentials, or meeting content.
18. **Screen observation is opt-in.** Incoming shared-content capture is off by default, bounded, locally retained, and cannot be enabled by voice.
19. **Truthful capabilities.** Optional provider and platform features are reported only when detected or tested. Unsupported resume flags or model names are never guessed.
20. **No hardcoded demo scenario.** The runtime must support general meetings and user-provided company/project context rather than fixed sales data or scripted hackathon behavior.

## Current implementation snapshot

Status meanings:

- **Implemented:** code and automated coverage exist.
- **Partial:** substantial code exists, but integration, reliability, or acceptance work remains.
- **Missing:** the product capability has not yet been built.

| Area | Status | Current reality |
| --- | --- | --- |
| Platform-neutral meeting runtime | Implemented | Shared adapter contract and registry drive Zoom, Teams, and Meet. |
| Zoom web adapter | Partial | Join, admission, audio, mute, chat, and lifecycle behavior exist and Zoom has had live use. Final regression acceptance remains necessary. |
| Microsoft Teams adapter | Partial | Guest-first join, Microsoft profile fallback, audio, mute, chat, participant count, and termination handling exist. Tenant-policy and audio acceptance coverage remains limited. |
| Google Meet adapter | Partial | Guest-first join and Google profile fallback exist with fixture coverage. Full live acceptance remains outstanding. |
| Continuous GPT-Live voice | Implemented | One `gpt-live-1` session, `store: false`, audio context, transcript events, and client delegation exist. Conversational quality and platform audio reliability still need evaluation. |
| Selective speech and mute transport | Partial | GPT-Live-driven participation and virtual audio gating exist. Cross-platform unmute/remute behavior is still an active reliability area. |
| Local runtime daemon | Implemented | Authenticated loopback HTTP/SSE API owns sessions, events, leases, approvals, artifacts, providers, and supervision. |
| Durable event and meeting storage | Implemented | Versioned schemas, append-only events, transcript/archive records, validation, recovery, and local retention paths exist. |
| Exact Codex continuity | Partial | Real thread IDs, leasing, resumed delegated turns, and final handoff append are implemented. The installed Codex skill launches a CLI child of the active task so it inherits `CODEX_THREAD_ID` directly; the full live round trip still needs acceptance testing. |
| Cursor provider | Partial | Capability-detected adapter exists. Exact resume depends on documented capabilities of the installed Cursor CLI and host-provided session identity. |
| Claude Code provider | Partial | Capability-detected adapter exists. Exact resume depends on documented capabilities of the installed Claude CLI and host-provided session identity. |
| Structured context handoff | Implemented | Versioned objective, task, summary, decision, constraint, question, file, conversation, and Git-state transfer exists. Automatic host-side construction is still integration work. |
| Document and text context | Implemented | Portal can accept bounded text and common document formats for local context search. Enterprise connectors are not implemented. |
| Tavily web search | Implemented | Optional local tool exists and uses the operator's key. |
| Delegated technical work | Implemented | Client-delegated requests can reach provider workers with meeting and workspace context. Quality and progress UX need further live evaluation. |
| Approval control plane | Implemented | Portal, daemon, SDK, CLI, and MCP share one-decision approval records and replay protection. |
| Isolated workspace execution | Implemented | Approved actions run in isolated Git worktrees with containment checks and artifacts. |
| Commit and push broker | Implemented | Typed, approval-gated local Git operations exist. Real remote operations require explicit user authorization. |
| Artifacts and charts | Partial | Local artifacts and chart generation exist. Reliable delivery of images/files into meeting chat is not complete. |
| Incoming screen understanding | Partial | Opt-in capture and local observations exist. Cross-platform live quality needs acceptance testing. |
| Outgoing screen sharing | Missing | Colleague AI cannot yet share its desktop or artifact view as a meeting screen share. |
| Virtual camera presence | Implemented | Presence states exist without exposing task content. Live compatibility needs continued testing. |
| TypeScript SDK | Implemented locally | Blocking handle, events, status, cancellation, approvals, artifacts, and durable handoff exist. Package is not published. |
| Python SDK | Implemented locally | Mirrors the local daemon contract. Package is not published. |
| CLI | Implemented locally | Blocking join/status/cancel/handoff/approval/artifact workflows exist. Distribution and installer UX remain. |
| MCP adapter | Implemented locally | Thin stdio adapter over the TypeScript SDK exists, including durable-task behavior where the client supports it. It provides meeting controls, but exact Codex launch uses the task-local CLI because persistent MCP processes cannot reliably inherit per-task identity. Other hosts must pass an explicit session id or use context continuity. |
| Local operations portal | Implemented | Manual launch and operations console at `127.0.0.1:8095`; always context continuity. It should remain optional. |
| Hosted runner/control plane | Foundation only | Pairing and isolation primitives exist. There is no production multi-tenant hosted service. |
| Packaging and onboarding | Partial | The local installer installs the task-local CLI launcher and Codex meeting skill, and registers MCP controls. There is no one-command public distribution or published SDK/CLI/MCP package. |
| Automated QA | Strong but incomplete | Broad unit and adversarial coverage exists. Automated tests cannot prove browser selectors, tenant policy, admission, audio quality, or real provider behavior. |
| Live acceptance | Incomplete | A complete current-matrix acceptance run across Zoom, Teams, Meet, exact session resume, approvals, handoff, and restart recovery is still required. |

## How far we are

These percentages are directional estimates, not release metrics:

- **Local proof of concept:** approximately **90%**. The system can join calls, converse, use tools, retain transcripts, and operate through the portal.
- **Local developer preview:** approximately **75–80%**. The shared daemon, adapters, SDKs, CLI, MCP, providers, permissions, handoffs, and Codex installer exist; automatic context quality and full live reliability remain unfinished.
- **Reliable developer beta:** approximately **50–60%**. Distribution, automatic host context/session capture, lifecycle polish, cross-platform acceptance, telemetry, recovery UX, and compatibility support are still required.
- **Hosted multi-user product:** approximately **20–30%**. The security model and pairing foundation exist, but accounts, organizations, encrypted secret management, durable hosted control plane, billing, fleet operations, and production isolation are not built.

The project is beyond a hackathon demo and has a substantial product foundation. It is not yet the frictionless experience where any developer installs one integration and tells their current coding agent to join a meeting.

## Highest-priority product gaps

### 1. Prove and harden the Codex host integration

The Codex-native MCP entry point and installer now exist. Complete and prove the vertical slice so it:

- Remains simple to install, diagnose, update, and remove.
- Obtains the real active thread ID from the invoking Codex task environment and passes it explicitly across the MCP boundary.
- Builds the structured context handoff from the current task without exporting hidden reasoning.
- Supplies the current workspace, model, and explicit permissions.
- Starts the local daemon automatically.
- Blocks or leases the originating conversation for the meeting lifecycle.
- Shows meeting/delegation progress in the originating coding-agent interface.
- Receives the final handoff and makes the same conversation usable again.

This is the most important missing bridge between the implemented platform and the product promise.

### 2. Complete live acceptance and audio reliability

Run repeatable real meetings for Zoom, Teams, and Meet covering:

- Guest join, lobby, admission, and signed-in fallback.
- Continuous listening and transcript continuity.
- Selective response behavior in a multi-person conversation.
- Complete first and subsequent spoken sentences.
- Platform unmute, virtual microphone playback, interruption, and remute.
- Host mute and policy-blocked microphone behavior.
- Text chat and capability reporting.
- Empty-room and meeting-ended detection.
- Stop, restart, archive, final handoff, and lease release.

### 3. Package the developer experience

- Publish or bundle supported SDK, CLI, and MCP packages.
- Add one-command installation and update paths.
- Provide Codex, Cursor, and Claude setup guides generated from truthful capabilities.
- Add a first-run diagnostic for Docker, audio, browser, provider login, API keys, and meeting access.
- Keep the portal as an optional local dashboard launched automatically when requested.

### 4. Make meeting output dependable

- Produce structured summaries, decisions, action items, owners, and unresolved questions.
- Deliver links and artifacts reliably through supported meeting chat paths.
- Provide a local artifact/results view when a platform cannot accept files.
- Add post-meeting export integrations only with explicit user authorization.

### 5. Prepare for safe company context

- Define connector contracts for company databases, documents, tickets, and knowledge systems.
- Scope every connector by identity, meeting, workspace, and permission.
- Start with read-only queries and auditable provenance.
- Add retrieval budgets, redaction, retention policy, and source citations.
- Never treat retrieved content as permission to perform an action.

### 6. Build hosted operation only after the local product is trustworthy

- Accounts, organizations, roles, and device ownership.
- Encrypted secret and browser-profile handling.
- Durable control plane and local runner fleet management.
- Tenant isolation, audit logs, budgets, rate limits, health, and upgrades.
- Clear separation between hosted coordination and local workspace execution.

## Near-term milestone plan

### Milestone A — Codex-native vertical slice

- [x] Install Colleague AI as a local Codex MCP integration.
- [ ] Start a meeting from an existing Codex conversation without opening the portal and prove it in a live call.
- [ ] Verify the installed skill captures the real Codex thread ID and structured context in a live call.
- [ ] Confirm the session is exclusively leased while the meeting is active.
- [ ] Complete one read-only delegated task in that exact session.
- [ ] Append one final handoff and resume the original conversation.
- [ ] Show lifecycle and delegation progress without exposing transcript text unnecessarily.

**Exit criterion:** a developer can move one real Codex task into a Zoom call and back without manually copying identifiers or context.

### Milestone B — Cross-platform reliability

- [ ] Complete the current participation/microphone fix and automated tests.
- [ ] Pass the full Zoom acceptance script.
- [ ] Pass the full Teams acceptance script with guest and account-fallback cases.
- [ ] Pass the full Google Meet acceptance script with guest and account-fallback cases.
- [ ] Record a compatibility matrix with tested browser/provider versions and policy limitations.
- [ ] Add reproducible diagnostics for failure states found during acceptance.

**Exit criterion:** the documented supported meeting configurations consistently join, listen, speak completely, remute, end, and archive.

### Milestone C — Developer preview distribution

- [ ] Decide supported installation form: packaged CLI, desktop helper, plugin, or combined installer.
- [ ] Package the daemon, Docker assets, SDK/MCP configuration, and update mechanism.
- [ ] Add first-run setup and health diagnostics.
- [ ] Make the optional console open from the coding-agent integration.
- [ ] Document uninstall, credential removal, data deletion, and version migration.
- [ ] Recruit a small external developer cohort and capture consented reliability metrics.

**Exit criterion:** a developer outside the original machine can install Colleague AI, connect a supported coding agent, and complete the Codex-native vertical slice.

### Milestone D — Useful meeting outcomes

- [ ] Generate dependable structured meeting outcomes.
- [ ] Add confirmed link/artifact delivery for each supported platform or report unavailability clearly.
- [ ] Add a post-meeting artifact view and export contract.
- [ ] Improve speaker attribution and late-join context when platform data permits it.
- [ ] Measure response usefulness, false interventions, delegation accuracy, and handoff completeness.

**Exit criterion:** teams keep Colleague AI in recurring development meetings because it reduces restatement and follow-up work.

### Milestone E — Hosted team product

- [ ] Design and threat-model the production control plane.
- [ ] Add accounts, organizations, roles, runner ownership, and encrypted configuration.
- [ ] Add multi-tenant storage, retention controls, audit logs, budgets, and billing.
- [ ] Operate local runners without uploading workspace bytes or long-lived provider credentials.
- [ ] Add safe upgrades, health monitoring, incident controls, and support tooling.

**Exit criterion:** a team can operate Colleague AI across approved users and projects without sharing one developer's local configuration.

## Explicitly out of scope for the immediate release

- Government Teams, webinars, and town halls.
- Silent collection of credentials or scripted password entry.
- Hidden access to complete coding-agent conversation histories.
- Chain-of-thought or private reasoning transfer.
- Voice-enabled permission escalation.
- Autonomous commits or pushes without the configured approval.
- Unbounded shell access.
- Automatic outgoing desktop screen sharing.
- Claiming reliable meeting-chat file delivery where the platform does not support it.
- Treating the hosted pairing foundation as a production cloud service.

## Success measures

Track product outcomes rather than only code completion:

- Time from “join this meeting” to admitted and listening.
- Percentage of meetings completed without portal or noVNC intervention.
- Percentage of spoken responses delivered completely on the first attempt.
- False-intervention rate in multi-person conversations.
- Delegated task success and cancellation rate.
- Exact-session resume and final-handoff success rate.
- Approval clarity and accidental-action rate.
- Transcript and action-item completeness.
- Recovery success after daemon, container, browser, or network interruption.
- Install-to-first-success time for a new developer.
- Percentage of meetings where users continue work from the generated handoff.

## Definition of a developer beta

Colleague AI is ready for a developer beta when all of the following are true:

- A new developer can install it without editing repository source.
- Codex can start it from the active conversation with a real thread ID and structured context.
- The portal is optional for the normal successful path.
- Zoom and at least one of Teams or Meet pass the complete live acceptance suite repeatedly.
- The agent listens continuously, speaks selectively, and delivers full audio reliably.
- One exact-session delegated task and one approval-gated isolated edit work during a real call.
- Meeting termination produces a durable handoff and releases the session lease every time.
- Secrets, transcripts, browser profiles, context, and workspace artifacts remain local and gitignored.
- Installation, diagnostics, cancellation, recovery, upgrade, and deletion are documented.
- Capability claims match tested behavior.

## Instructions for coding agents

Before implementing a substantial change:

1. Read this document and the linked architecture/capability documents.
2. Identify which milestone and user outcome the change advances.
3. Check the working tree and do not overwrite unrelated changes.
4. Preserve the settled decisions unless the task explicitly changes one.
5. Prefer one shared daemon contract over adding another execution path.
6. Keep platform DOM details inside adapters and provider CLI details inside providers.
7. Add or update boundary tests for schemas, authorization, containment, lifecycle, and recovery.
8. Do not claim a browser or audio path works from fixtures alone; label live acceptance separately.
9. Update the table, milestone checklist, and review date when the implementation status changes materially.
10. Include evidence in the commit or PR: tests run, live acceptance performed, limitations, and any follow-up work.

When updating this file, use these rules:

- Mark **Implemented** only when the code path and meaningful automated tests exist.
- Mark **Partial** when implementation exists but host integration, live acceptance, distribution, or reliability work remains.
- Mark a live acceptance checkbox complete only after a real meeting test on the current implementation.
- Never mark the portal as exact continuity.
- Never mark hosted operation production-ready based only on local pairing tests.
- Never remove an unresolved limitation solely because it is inconvenient for presentation.

## Progress log

### 2026-09-17

- Consolidated the product vision, settled decisions, implementation inventory, gaps, and milestone checklist.
- Confirmed the repository contains the shared daemon, Zoom/Teams/Meet adapters, GPT-Live client delegation, Codex/Cursor/Claude provider adapters, session leases, SDKs, CLI, MCP adapter, approvals, isolated workspace execution, Git broker, artifacts, virtual camera, and optional screen observation.
- Previously identified the main product gap as a turnkey host integration that supplies the active coding-agent session and context; the current Codex path launches a CLI child of the active task so the task ID does not cross an unreliable persistent-MCP environment boundary.
- Confirmed the portal remains an optional context-continuity operations console rather than the desired primary user experience.
- Recorded live cross-platform acceptance, distribution, meeting output delivery, enterprise context connectors, and hosted multi-user operation as unfinished work.
- Retained the Codex-native `join_current_meeting` MCP entry point for compatibility and controls, while moving identity-critical launch to the task-local CLI.
- Added an idempotent Codex integration installer, safe CLI defaults for the current Codex thread/workspace and installation root, and a local diagnostic command.
