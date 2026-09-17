# Colleague AI developer platform implementation plan

Status: historical implementation design; the platform foundation is implemented

Repository: `colleague-ai-private`

Current implementation branch: `developer-platform`
Prerequisite: retain the Zoom/Teams adapter work in commits `fa4fb33`, `3eaec75`, and `0aa7121` (PR #3) when choosing the implementation base.

> This file preserves the detailed design that guided implementation. Some “current baseline” statements describe the repository before the platform work landed. For current product truth, progress, and open milestones, read [product vision, decisions, and progress](product-vision-and-progress.md). Code and capability tests take precedence over historical implementation instructions. Current Codex hosts expose `CODEX_THREAD_ID` to task command subprocesses but do not automatically forward it to persistent MCP servers, so the invoking agent must pass it explicitly.

## Purpose

Colleague AI should let a developer move an active coding-agent conversation into a live meeting and return to the same conversation afterward.

The intended interaction is:

> Join this meeting, bring our current project context, help the team when needed, perform approved development work, and continue here when the meeting ends.

Colleague AI must feel like another interface to the developer's existing coding agent, not an unrelated meeting bot. The first complete provider integration will target Codex. The architecture must leave explicit extension points for Cursor, Claude Code, other coding agents, and Google Meet.

## Product contract

1. A developer works with a coding agent in Conversation A.
2. Conversation A invokes Colleague AI with a meeting URL, its real resumable session identifier, the workspace, permissions, and a structured context handoff.
3. Colleague AI acquires an exclusive lease on Conversation A. The session cannot process unrelated turns while the meeting owns it.
4. GPT-Live joins through the Zoom or Teams adapter and receives the relevant prior coding context at startup.
5. GPT-Live handles live audio, turn-taking, interruptions, and selective spoken participation.
6. When technical work is needed, GPT-Live delegates to Colleague AI. Colleague AI resumes Conversation A directly through the coding-agent provider adapter.
7. GPT-Live stays connected and continues listening while delegated work runs.
8. Verified results return to GPT-Live for natural spoken delivery.
9. When the meeting ends, Colleague AI adds the meeting transcript, decisions, action items, artifacts, and work results to Conversation A.
10. Colleague AI releases the session lease and the developer continues in the same coding-agent conversation.

The first release should prove this claim end to end with Codex, Zoom, and Teams.

## Non-negotiable decisions

- Build a runtime and SDK first. MCP is an optional compatibility adapter, not the core runtime.
- Preserve one continuous `gpt-live-1` session during the meeting so the voice model retains the original audio conversation.
- Use GPT-Live client delegation for coding-agent work. Do not keep an extra Responses model between GPT-Live and the coding-agent CLI.
- Resume the exact coding-agent session that launched the meeting. Do not create a new Codex session keyed only by the meeting URL.
- Hold an exclusive lease on the originating coding-agent session until the meeting is finalized or cancelled.
- Keep complete transcripts, task state, tool output, and artifacts in the Colleague AI runtime. Do not rely on the model context as the system of record.
- Pass useful project state explicitly. Do not request or attempt to export hidden reasoning or chain-of-thought.
- Keep platform-specific DOM behavior inside meeting adapters.
- Enforce permissions in application code, independently of model instructions.
- Store credentials, browser profiles, transcripts, recordings, context, and artifacts outside Git.

## Current repository baseline

The repository already contains:

- `meeting-runtime/adapters_base.py`: platform adapter contract.
- `meeting-runtime/adapters.py`: Zoom and Teams adapter registry.
- `meeting-runtime/meeting_urls.py`: strict platform URL detection.
- `meeting-runtime/bridge.py`: GPT-Live WebSocket, session configuration, audio flow, tool dispatch, status, and transcript events.
- `meeting-runtime/codex_tool.py`: bounded job client and current `run_codex` function schema.
- `meeting-runtime/codex_worker.py`: authenticated host Codex CLI worker with read-only execution and resumable thread IDs.
- `meeting-runtime/call_record.py`: local meeting archive.
- `meeting-runtime/context_tool.py`: organizer-provided document retrieval.
- `meeting-runtime/search_tool.py`: Tavily-backed public search.
- `meeting-runtime/participation.py`: virtual audio transport and platform mute handling.
- `meeting-runtime/meeting_lifecycle.py`: meeting termination behavior.
- `control-panel/`: local meeting console and lifecycle APIs.
- `compose.meeting.yaml` and `start-meeting-agent.sh`: Docker runtime startup.

Current important behavior:

- `bridge.py` starts `gpt-live-1` with `store: false` and Responses delegation.
- Startup configuration does not yet populate GPT-Live `session.input` with prior coding-agent history.
- The current Codex client derives a meeting session key from the meeting URL.
- The first meeting Codex task creates a new Codex thread; later meeting tasks resume that new thread.
- Codex runs read-only and already captures the thread ID returned by `codex exec`.
- Zoom and Teams already share a platform-neutral meeting runtime.
- The physical meeting microphone stays connected while a virtual gate controls generated audio transport.
- GPT-Live owns conversational turn-taking. Do not reintroduce local silence, backchannel, or semantic interruption classifiers.

The implementation must preserve current Zoom/Teams admission, authentication, audio, mute, transcript, chat, context, web-search, chart, Docker, PulseAudio, and noVNC behavior unless a milestone explicitly replaces it.

## Target architecture

```text
Codex conversation --------> Codex provider adapter -----+
Cursor session ------------> Cursor provider adapter ----|
Claude Code session -------> Claude provider adapter ----+--> Colleague AI runtime
Generic MCP client --------> MCP compatibility adapter --|          |
                                                               +----+----+
                                                               |         |
                                                           GPT-Live   Meeting adapter
                                                               |       Zoom / Teams
                                                               |
                                                    Transcript, events, artifacts
```

The runtime is the single owner of active meetings, session leases, GPT-Live connections, transcripts, delegation state, approvals, and final handoffs. SDKs, the CLI, the portal, and MCP must all call the same runtime interfaces.

## Core domain models

Use versioned schemas and validate all data at process boundaries.

```ts
type AgentProvider = "codex" | "cursor" | "claude-code" | "generic";

interface AgentSessionRef {
  provider: AgentProvider;
  sessionId: string;
  workspace: string;
  model?: string;
  metadata?: Record<string, string>;
}

interface ContextHandoff {
  version: 1;
  objective: string;
  currentTask: string;
  summary: string;
  decisions: string[];
  constraints: string[];
  openQuestions: string[];
  importantFiles: string[];
  recentConversation: Array<{
    role: "user" | "assistant";
    text: string;
  }>;
  git?: {
    branch?: string;
    commit?: string;
    dirty?: boolean;
  };
}

interface MeetingPermissions {
  workspace: "none" | "read-only" | "workspace-write";
  commands: "disabled" | "approval-required" | "allowed";
  edits: "disabled" | "approval-required" | "allowed";
  network: "disabled" | "approval-required" | "allowed";
  commits: "disabled" | "approval-required";
  pushes: "disabled" | "approval-required";
}

interface MeetingSession {
  id: string;
  platform: "zoom" | "teams";
  meetingUrl: string;
  agentSession: AgentSessionRef;
  context: ContextHandoff;
  permissions: MeetingPermissions;
  state: MeetingState;
  startedAt: string;
}
```

Do not treat free-form context as authorization. Permissions require separate typed fields.

## Exclusive coding-agent session lease

Conversation A must become unavailable for competing execution after it launches a meeting.

State machine:

```text
available -> acquiring -> in_meeting -> finalizing -> available
                      \-> failed --------------------/
```

The lease record must contain:

- Provider and session ID
- Meeting ID
- Acquisition time
- Last heartbeat
- Owner process identity
- Current delegated turn, if any
- Finalization state

Required behavior:

- Acquisition is atomic.
- Only one meeting can own an agent session.
- Only one delegated agent turn can execute against the session at a time.
- Competing turns are rejected with `agent_session_in_meeting` or queued when the provider explicitly supports queuing.
- Cancellation stops active delegated work where possible, leaves the meeting, writes a partial handoff, and releases the lease.
- A crashed owner cannot lock a session forever. Use a heartbeat plus a conservative recovery procedure.
- Never release the lease until transcript and handoff persistence has completed or the finalization failure has been durably recorded.

A library call may remain awaited for the duration of the meeting, but the lock must be enforced by the runtime rather than depending on a UI textbox being disabled.

## Runtime API

Create a local daemon that owns all meetings. Prefer a versioned HTTP API plus an event stream so TypeScript, Python, CLI, portal, and MCP clients share one implementation.

Initial endpoints:

```text
POST   /v1/meetings
GET    /v1/meetings/:meetingId
POST   /v1/meetings/:meetingId/context
POST   /v1/meetings/:meetingId/cancel
GET    /v1/meetings/:meetingId/events
GET    /v1/meetings/:meetingId/handoff

POST   /v1/agent-sessions/:provider/:sessionId/lease
DELETE /v1/agent-sessions/:provider/:sessionId/lease
GET    /v1/agent-sessions/:provider/:sessionId/status
```

Use a random per-launch bearer token for the local daemon. Bind to localhost. Never return API keys or authentication material to browser clients.

The event stream should use Server-Sent Events initially unless existing runtime constraints make a WebSocket materially simpler.

## Runtime event model

Use one event vocabulary across the daemon, SDKs, CLI, portal, logs, and visual presence:

```ts
type ColleagueEvent =
  | { type: "meeting.joining" }
  | { type: "meeting.waiting_for_admission" }
  | { type: "meeting.live" }
  | { type: "meeting.ended"; reason: string }
  | { type: "agent_session.locked"; sessionId: string }
  | { type: "agent_session.released"; sessionId: string }
  | { type: "transcript.delta"; entry: TranscriptEntry }
  | { type: "delegation.started"; delegationId: string }
  | { type: "delegation.progress"; delegationId: string; message: string }
  | { type: "delegation.completed"; delegationId: string }
  | { type: "delegation.cancelled"; delegationId: string; reason: string }
  | { type: "approval.required"; request: ApprovalRequest }
  | { type: "artifact.created"; artifact: Artifact }
  | { type: "handoff.ready"; handoff: MeetingHandoff };
```

Persist events as append-only JSONL for the first implementation. Wrap storage behind an interface so a later hosted version can use a database without changing the SDK contract.

## SDK contract

### TypeScript

```ts
const meeting = await colleague.joinMeeting({
  url: meetingUrl,
  agentSession: {
    provider: "codex",
    sessionId: currentThreadId,
    workspace: process.cwd(),
    model: currentModel,
  },
  context: contextHandoff,
  permissions: {
    workspace: "read-only",
    commands: "approval-required",
    edits: "disabled",
    network: "approval-required",
    commits: "disabled",
    pushes: "disabled",
  },
});

meeting.on("state", renderState);
meeting.on("transcript", persistTranscript);
meeting.on("delegation", renderDelegation);
meeting.on("approval_required", requestApproval);

const handoff = await meeting.finished;
```

### Python

```python
meeting = await colleague.join_meeting(...)

async for event in meeting.events():
    handle(event)

handoff = await meeting.finished()
```

The initial SDK may wrap the local daemon rather than embedding the complete runtime in-process. Keep the public API independent of that decision.

## CLI contract

```bash
colleague join \
  --meeting "https://..." \
  --agent codex \
  --thread "$CODEX_THREAD_ID" \
  --workspace "$PWD" \
  --wait
```

Requirements:

- Remain active until the meeting completes.
- Display concise lifecycle and delegation progress.
- Forward Ctrl-C into clean cancellation and meeting leave.
- Do not print private transcript text by default.
- Output a final structured handoff or a path to it.
- Return nonzero for startup failure and a distinct status for partial finalization.

## Optional MCP adapter

Expose:

- `start_meeting`
- `get_meeting_status`
- `add_meeting_context`
- `cancel_meeting`
- `get_meeting_handoff`

The adapter calls the same daemon/SDK. It does not implement a second meeting runtime.

Use the MCP Tasks extension when the client advertises support. Use progress notifications for compatible long-running requests. For clients without durable tasks, return a meeting handle immediately and let them poll or call `get_meeting_handoff` later.

Do not depend on a generic MCP server automatically receiving the host application's current conversation ID. Provider integrations must inject the real session ID. If that is unavailable, accept a context handoff and label the meeting as context continuity rather than exact session continuity.

## Codex provider adapter

Implement Codex first.

```python
class CodingAgentProvider(Protocol):
    async def validate_session(self, ref: AgentSessionRef) -> ProviderSessionStatus: ...
    async def acquire(self, ref: AgentSessionRef, meeting_id: str) -> SessionLease: ...
    async def run(self, lease: SessionLease, task: AgentTask) -> AgentResult: ...
    async def cancel(self, lease: SessionLease, task_id: str) -> None: ...
    async def append_handoff(self, lease: SessionLease, handoff: MeetingHandoff) -> AgentResult: ...
    async def release(self, lease: SessionLease) -> None: ...
```

Codex requirements:

- Obtain the actual originating thread ID from a Codex host integration or App Server. Never ask the model to invent its ID.
- Resume that thread for every meeting task.
- Reuse the current `codex exec ... resume <thread-id> -` capability initially if App Server integration is not yet ready.
- Serialize turns per thread.
- Preserve the originating workspace and model unless the caller explicitly authorizes an override.
- Start with `read-only` sandboxing.
- Capture structured progress and the final response.
- Append the final meeting handoff to the same thread before releasing it.

The current worker's `sessions.json` mapping may remain temporarily for backward compatibility, but an explicit originating thread ID must take precedence. Remove meeting-URL hashing as the primary session identity.

## GPT-Live migration

### Client delegation

Change the startup configuration in `meeting-runtime/bridge.py` from Responses delegation to:

```json
{
  "model": "gpt-live-1",
  "store": false,
  "delegation": {
    "type": "client"
  }
}
```

Keep `store: false` for the first implementation and retain application-owned recovery through transcripts and handoffs. Stored Live sessions and forks may be evaluated separately later.

### Startup context

Populate `session.input` when the Live session starts. Relevant API facts from the official documentation:

- Startup history accepts up to 128 messages and 8,192 combined tokens.
- Supported roles are `developer`, `user`, and `assistant`.
- Startup `input` cannot be replaced through `session.update`.
- Runtime context can be added through `session.instructions.append`, `session.thinking.append`, and `session.commentary.append`.
- Each runtime append carries a plain string of up to 500 tokens.

Source: <https://developers.openai.com/api/docs/guides/live-conversations>

Recommended startup ordering:

1. Trusted developer message with objective, project summary, permissions, and important constraints.
2. Selected recent user/assistant exchanges.
3. Existing meeting transcript or summary if the agent joins late.
4. Current task and open questions.

Keep behavioral policy in `instructions`. Keep project facts and dialogue in `input`. Do not place untrusted documents or meeting speech into developer instructions.

### Transcript ownership

Collect and persist:

- `session.input_transcript.delta`
- `session.output_transcript.delta`
- start/end offsets
- platform speaker information when available
- delegation IDs and offsets
- tool/task results
- approvals
- artifacts
- lifecycle events

Transcript deltas are not guaranteed to be complete turns and may contain recognition errors. Build a small transcript assembler that retains raw deltas and emits normalized entries without destroying the source record.

### Delegation workflow

When `session.delegation.created` arrives:

1. Save the opaque delegation ID and `offset_ms`.
2. Locate the relevant spoken request from the transcript timeline. The delegation event does not contain task text.
3. Combine the request with the initial context handoff, relevant meeting transcript, workspace, and permissions.
4. Create a self-contained `AgentTask`.
5. Queue it against the originating coding-agent session.
6. Emit `delegation.started` and update the visual presence to `working`.
7. Resume the coding-agent session.
8. Send quiet progress to GPT-Live with `session.thinking.append` where useful.
9. Validate/redact the result and record provenance.
10. Send the concise result through `session.commentary.append` using the original delegation ID.
11. Emit `delegation.completed` and restore the visual state.

GPT-Live remains connected and continues listening while the coding agent works. It may briefly acknowledge that it is checking, handle simple unrelated conversation, accept corrections, or cancel obsolete work. Prompt it never to invent the pending technical result.

Source: <https://developers.openai.com/api/docs/guides/live-delegation>

## Context handoff construction

The provider integration should build the handoff from information already available to the coding agent. Do not require vendor conversation exports.

Rank content in this order:

1. Current objective and task
2. Decisions and constraints
3. Open questions
4. Files currently under discussion
5. Recent exchanges needed for continuity
6. Git and workspace state
7. Older background summary

Enforce configurable size limits before data reaches GPT-Live. Keep the complete context handoff in the local meeting archive even when only a subset enters the model context.

## Late-join context

Conversation that occurred before Colleague AI joined may come from:

- Platform captions or transcripts where accessible
- A transcript supplied in the launch request
- Organizer notes or documents
- A Colleague AI recorder already in the room

Colleague AI cannot recover audio that was never captured or made available. State this clearly in user-facing status.

Add a context-selection pipeline that deduplicates segments, extracts decisions and active topics, identifies unresolved questions, and selects the portion appropriate for GPT-Live startup history.

## Post-meeting handoff

```ts
interface MeetingHandoff {
  version: 1;
  meetingId: string;
  startedAt: string;
  endedAt: string;
  summary: string;
  decisions: Array<{ text: string; evidence?: TranscriptReference[] }>;
  requirements: string[];
  actionItems: Array<{ text: string; owner?: string; due?: string }>;
  unresolvedQuestions: string[];
  filesDiscussed: string[];
  workPerformed: AgentWorkRecord[];
  artifacts: ArtifactReference[];
  transcriptPath: string;
  recommendedNextAction: string;
}
```

Finalization order:

1. Stop accepting new delegated work.
2. Cancel or finish work according to the cancellation policy.
3. Leave the meeting.
4. Finalize raw transcript and event files.
5. Generate the structured handoff.
6. Resume Conversation A and append the handoff.
7. Record whether the append succeeded.
8. Release the session lease.

If appending the handoff fails, preserve it locally and expose a retry operation. Do not lose the meeting record or silently release without recording the failure.

## Visual identity and progress inside meetings

Build a virtual camera source that Zoom and Teams see as a normal camera. This supplies a consistent identity for guest and authenticated joins.

States:

| State | Participant tile |
| --- | --- |
| Joining | Colleague AI logo with connection animation |
| Listening | Static avatar with a subtle pulse |
| Working | Animated dots and `Checking project` |
| Waiting for approval | Amber indicator |
| Speaking | Audio waveform |
| Error | Small red indicator with concise status |
| Finalizing | `Preparing handoff` |

Requirements:

- Never display repository names, code, prompts, credentials, or private task text.
- Avoid rapid flashing and distracting animation.
- Let users disable the virtual camera.
- Keep audio behavior independent from the visual state.
- Support a configured avatar path and a bundled default asset.

Authenticated Zoom and Microsoft accounts may provide platform profile pictures, but guest joins cannot depend on them. Treat platform profile images as an enhancement and the virtual camera as the cross-platform path.

## Screen-share understanding (later milestone)

GPT-Live does not directly support image or video input. Do not stream meeting video into it.

Later implementation:

1. Detect the meeting's shared-content surface.
2. Capture frames at a low configurable rate.
3. Detect meaningful visual changes before invoking a vision backend.
4. Send selected frames to a vision-capable coding-agent/backend workflow.
5. Store timestamped observations and screenshots as artifacts.
6. Add concise relevant observations to GPT-Live through `session.thinking.append`.
7. Include observations and artifacts in the final handoff.

Never capture the entire desktop by default.

## Development work during meetings

Start read-only. Initial supported actions:

- Inspect code and project structure
- Search files
- Analyze logs and structured data
- Run safe read-only calculations or queries
- Explain failures
- Produce implementation plans

Later add workspace mutations behind explicit permissions:

- Edit files
- Run tests
- Start previews
- Create commits
- Prepare pull requests
- Share screenshots or preview links

The coding-agent provider must apply the same or stricter permissions than the originating session. A spoken request does not expand permissions.

## Control-panel changes

Add:

- Originating provider, model, workspace, and session status
- Exclusive-session-lock indicator
- Delegation activity timeline
- Approval requests
- Virtual-camera preview and enable/disable control
- Meeting permission profile
- Context-handoff inspection
- Downloadable final handoff
- Separate views for meeting transcript and coding-agent activity

Do not make the local portal the only supported control path. It should consume the same daemon API as the SDK and CLI.

## Proposed modules

Add or extract:

```text
meeting-runtime/
  agent_sessions.py
  session_leases.py
  delegation_router.py
  transcript_assembler.py
  context_handoff.py
  meeting_handoff.py
  event_store.py
  visual_presence.py
  providers/
    __init__.py
    base.py
    codex.py

packages/
  sdk-typescript/
  sdk-python/
  cli/
  mcp-server/
```

Names may be adjusted to fit the repository's language boundaries, but keep provider adapters, platform adapters, runtime orchestration, and presentation concerns separate.

## Delivery milestones

### Milestone 1: daemon and session leases

- Define versioned schemas and event records.
- Add the local daemon.
- Add durable meeting IDs.
- Implement exclusive agent-session leases and heartbeats.
- Move the portal onto daemon APIs.
- Preserve existing Zoom and Teams behavior.

Acceptance: the portal starts and stops existing calls through the daemon with no voice or platform regression.

### Milestone 2: GPT-Live client delegation

- Switch from Responses delegation to client delegation.
- Add startup `session.input` context.
- Persist transcript deltas.
- Route delegated work directly to a provider adapter.
- Support progress, cancellation, and result delivery.
- Remove the intermediary Responses backend from this path.

Acceptance: a spoken repository question reaches Codex directly, GPT-Live remains connected, and the verified result is spoken.

### Milestone 3: original Codex session inheritance

- Integrate with Codex App Server or another host interface that provides the real thread ID.
- Bind the meeting to Conversation A.
- Resume that exact thread for every delegated task.
- Serialize all turns against that thread.
- Append the final meeting handoff to it.

Acceptance: facts established before the meeting are available during it, and meeting decisions are present in the same Codex conversation afterward.

### Milestone 4: SDK and blocking lifecycle

- Publish local TypeScript and Python SDK packages.
- Build the blocking CLI workflow.
- Stream events and progress.
- Implement clean cancellation.
- Add crash-safe lease recovery.

Acceptance: one awaited SDK call spans the meeting and returns a structured handoff.

### Milestone 5: MCP compatibility

- Build a thin MCP adapter over the SDK.
- Add progress notifications.
- Support durable MCP Tasks when available.
- Add context-only fallback when exact session identity is unavailable.

Acceptance: a generic MCP client can launch, inspect, cancel, and retrieve a meeting.

### Milestone 6: visual presence

- Add a default avatar.
- Implement virtual camera output.
- Render listening, working, approval, speaking, error, and finalization states.
- Validate Zoom and Teams display behavior.

Acceptance: meeting participants can see when Colleague AI is working without repeated spoken status messages.

### Milestone 7: controlled workspace actions

- Add workspace-write permission mode.
- Add explicit action approvals.
- Track diffs, commands, tests, and artifacts.
- Include all work in the final handoff.

Acceptance: a participant can request a small authorized code change and receive a verified result during the call.

### Milestone 8: screen-share context

- Capture shared-content frames.
- Add change detection and vision analysis.
- Store observations and screenshots.
- Feed relevant observations into the meeting context.

Acceptance: Colleague AI can answer a question about the content being shared.

### Milestone 9: additional providers and platforms

- Cursor provider adapter
- Claude Code provider adapter
- Google Meet adapter
- Hosted/remote runtime mode

## Test plan

### Unit tests

- Context-handoff validation, normalization, prioritization, and truncation
- Session lease acquisition, exclusivity, heartbeat, expiry, and recovery
- Per-thread turn ordering
- Permission evaluation
- Transcript delta reconstruction
- Delegation-to-task construction around `offset_ms`
- Cancellation and corrected spoken requests
- Handoff generation and retry
- Event persistence and replay
- Virtual-camera state rendering
- Provider and platform detection

### Integration tests

- GPT-Live startup history generation
- Client delegation event handling
- Direct Codex thread resume
- Multiple delegations in one meeting
- Cancellation during Codex execution
- GPT-Live listening while work is pending
- Result delivery through `session.commentary.append`
- Runtime restart and lease recovery
- Meeting-end finalization ordering
- SDK, CLI, portal, and MCP parity

### Existing regression tests

Run and preserve:

```bash
npm test
node --test control-panel/*.test.mjs
```

Run the existing Python unittest suite inside the meeting runtime image, matching the repository's established Docker test workflow. Maintain all Zoom/Teams adapter fixtures and live acceptance guidance in `docs/meeting-adapters.md`.

### Live acceptance test

For both Zoom and Teams:

1. Begin with an existing Codex thread containing known project context.
2. Launch Colleague AI from that thread.
3. Confirm the thread is exclusively leased.
4. Confirm Colleague AI joins and listens.
5. Ask about a fact established before the meeting.
6. Ask a question requiring repository inspection.
7. Confirm the visual state becomes `working`.
8. Confirm GPT-Live continues listening while Codex runs.
9. Confirm the original Codex thread is resumed.
10. Confirm the result is spoken completely.
11. End the meeting.
12. Confirm automatic leave and transcript finalization.
13. Confirm the structured handoff is appended to the original thread.
14. Confirm the session lease is released.
15. Continue working in the same conversation using a meeting decision.

## Security and privacy requirements

- Never serialize or log API keys, access tokens, cookies, or platform credentials.
- Never request hidden reasoning or chain-of-thought from a coding agent.
- Treat meeting speech, documents, repositories, web results, and tool output as untrusted data.
- Keep transcripts, context, browser profiles, recordings, job files, and artifacts gitignored.
- Use restrictive permissions for local state files.
- Redact sensitive values from status and logs.
- Require explicit authorization for external side effects.
- Show the provider session and workspace before joining.
- Provide retention and deletion controls.
- Never display private task details through the virtual camera.
- Prevent unrelated local clients from taking over an active lease.
- Preserve platform mute authority and existing audio-safety behavior.

## Failure and recovery requirements

- If GPT-Live disconnects, keep the local transcript and meeting state, attempt only bounded recovery, and report the gap.
- If Codex fails, tell GPT-Live the task failed without fabricating a result.
- If the meeting ends while Codex is working, follow the configured finish-or-cancel policy, then finalize.
- If handoff injection into Conversation A fails, keep the handoff locally and expose retry.
- If the daemon crashes, recover meeting archives and stale leases on restart.
- If a platform profile or admission policy fails, surface `authentication_required` or `needs_attention` without starting GPT-Live prematurely.
- If a virtual camera fails, continue audio participation and report degraded visual presence.

## Initial implementation scope

Implement first:

- Local daemon
- Versioned models and event stream
- Exclusive session leases
- Codex provider adapter
- Real originating Codex thread binding
- GPT-Live client delegation
- Startup context handoff
- Continuous transcript storage
- Blocking TypeScript SDK workflow
- CLI wrapper
- Zoom and Teams preservation
- Structured final handoff
- Basic virtual camera with listening, working, and speaking states
- Read-only Codex permissions

Defer:

- Cursor and Claude Code providers
- Google Meet
- Repository writes, commits, and pushes
- Screen-share vision
- Hosted multi-user service
- Dynamic management of platform account profile pictures

## Suggested implementation order and commit boundaries

1. **Define runtime schemas and event storage**
2. **Add exclusive coding-agent session leases**
3. **Extract Codex into a provider adapter with explicit thread IDs**
4. **Switch GPT-Live to client delegation and persist transcripts**
5. **Add startup context handoff and final meeting handoff**
6. **Introduce the daemon API and migrate the portal**
7. **Add the TypeScript SDK and blocking CLI**
8. **Add the MCP compatibility adapter**
9. **Add virtual-camera identity and progress states**
10. **Complete Zoom and Teams live acceptance testing**

Keep commits independently reviewable and include migration notes whenever a runtime configuration or persisted format changes.

## Definition of done for the first product milestone

The milestone is complete only when:

- An existing Codex conversation launches a Zoom or Teams meeting.
- Colleague AI receives a relevant context handoff.
- The original Codex thread is locked for the meeting.
- GPT-Live continuously listens and participates selectively.
- Technical work resumes the original Codex thread directly.
- GPT-Live remains responsive while delegated work runs.
- Participants see a safe visual working indicator.
- The agent leaves automatically when the meeting ends.
- The complete local transcript and event archive are preserved.
- A structured handoff is appended to the original Codex conversation.
- The session unlocks and the developer continues from the meeting's decisions.
- All existing automated tests pass and both Zoom and Teams complete the live acceptance sequence.
