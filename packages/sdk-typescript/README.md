# Colleague AI TypeScript SDK

Versioned local SDK (`@colleague-ai/sdk` 1.0.0) for joining Zoom and Teams meetings through the Colleague AI runtime daemon. The public types mirror the daemon schemas. The client interface is transport-independent; the default transport talks to the loopback daemon.

Requires Node.js 22+. This package is for local use and is not published to npm.

## Install (workspace)

From the Colleague AI repository:

```bash
node --experimental-vm-modules packages/sdk-typescript/examples/join.mjs
```

Import the SDK from a Codex or Cursor host integration:

```ts
import { Colleague } from '@colleague-ai/sdk';

const colleague = new Colleague();
const meeting = await colleague.joinMeeting({
  url: meetingUrl,
  agentSession: {
    provider: 'codex',
    sessionId: currentThreadId,
    workspace: process.cwd(),
    model: currentModel,
  },
  context: contextHandoff,
  permissions: {
    workspace: 'read-only',
    commands: 'approval-required',
    edits: 'disabled',
    network: 'approval-required',
    commits: 'disabled',
    pushes: 'disabled',
  },
});

meeting.on('state', renderState);
meeting.on('delegation', renderDelegation);
const handoff = await meeting.finished;
```

`agentSession.sessionId` must be the originating thread id. Do not pass `--last`, a URL hash, or a model-invented id. Exact continuity requires that real thread. Context-only joins use `sessionId: "local-portal"` from the CLI `--context-continuity` path.

## Meeting handle

- `meeting.id`
- `await meeting.status()`
- `await meeting.addContext(context)`
- `await meeting.cancel()` — idempotent
- `await meeting.retryFinalization()`
- `meeting.on(event, handler)` and `for await (const event of meeting.events())`
- `await meeting.finished` — resolves only with a durable `MeetingHandoff` after `handoff.ready`. Socket or process closure is not completion.

## Errors

| Class | When |
| --- | --- |
| `ValidationError` | Caller input failed client-side checks |
| `StartupError` | Daemon or supervisor could not start the meeting |
| `RuntimeError` | Meeting failed while live |
| `FinalizationError` | Unrecoverable handoff; `archivePath` points at `recordings/{meetingId}` |
| `InterruptError` | Caller cancelled through SIGINT |

Partial handoffs resolve successfully with `handoff.partial === true`.

## Loopback transport

The default transport:

1. Connects to `127.0.0.1:8765` (or `COLLEAGUE_DAEMON_PORT`)
2. Reads the host-only token from `.colleague/daemon.auth` and never returns it
3. Sends `Authorization: Bearer …` on every request
4. Reconnects SSE with `Last-Event-ID` and drops duplicate event ids
5. Rereads the token file after 401
6. Starts `start-runtime-daemon.sh` if the port is closed

See `examples/join.mjs` for a Codex-style blocking workflow.
