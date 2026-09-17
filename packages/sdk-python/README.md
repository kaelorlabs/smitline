# Colleague AI Python SDK

Versioned local SDK (`colleague-ai` 1.0.0) for joining Zoom, Teams, and Google Meet meetings through the Colleague AI runtime daemon. Public models mirror the daemon JSON schemas. The client interface is transport-independent; the default transport uses stdlib HTTP and SSE against the loopback daemon.

Requires Python 3.10+. This package is for local use and is not published to PyPI. There are no third-party runtime dependencies.

## Usage from a Codex, Cursor, or Claude Code host integration

```python
from colleague_ai import Colleague

colleague = Colleague()
meeting = await colleague.join_meeting({
    'url': meeting_url,
    'agentSession': {
        'provider': 'codex',
        'sessionId': current_thread_id,
        'workspace': os.getcwd(),
        'model': current_model,
    },
    'context': context_handoff,
    'permissions': {
        'workspace': 'read-only',
        'commands': 'approval-required',
        'edits': 'disabled',
        'network': 'approval-required',
        'commits': 'disabled',
        'pushes': 'disabled',
    },
})

async for event in meeting.events():
    handle(event)

handoff = await meeting.finished()
```

`join_meeting` validates inputs client-side and leaves `agentSession`, `sessionId`, `workspace`, `model`, `metadata`, and `permissions` unchanged. Exact continuity requires a real originating thread id. Do not pass `--last` or a meeting-URL hash. The local portal uses context continuity only. `runner_status` / `pair_runner` / `complete_runner_pair` / `unpair_runner` expose foundation pairing; status never includes the one-time code.

## Meeting handle

- `meeting.id`
- `await meeting.status()`
- `await meeting.add_context(context)`
- `await meeting.cancel()` — idempotent
- `await meeting.retry_finalization()`
- `meeting.on(name, handler)` and `async for event in meeting.events()`
- `await meeting.finished()` — waits for a durable `MeetingHandoff`. Never treats a closed socket as success.

Typed errors: `ValidationError`, `StartupError`, `RuntimeError`, `FinalizationError` (includes `archive_path`), `InterruptError`. Partial handoffs resolve as structured results with `partial: True`.

The loopback transport reads `.colleague/daemon.auth`, authenticates every request, reconnects SSE with `Last-Event-ID`, rotates a stale token from that file, and can start `start-runtime-daemon.sh`.

See `examples/join.py`.
