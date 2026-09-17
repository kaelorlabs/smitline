# colleague CLI

Blocking lifecycle CLI for the local Colleague AI daemon. It wraps the TypeScript SDK and does not print meeting transcript text by default.

Requires Node.js 22+. Not published to npm.

## Join

Exact continuity requires `--thread`:

```bash
colleague join \
  --meeting "https://zoom.us/j/..." \
  --agent codex \
  --thread "$CODEX_THREAD_ID" \
  --workspace "$PWD" \
  --wait
```

`--context-continuity` allows a join without an originating thread (`sessionId=local-portal`).

`--wait` stays in the foreground, prints concise lifecycle and delegation progress on stderr, and writes the final handoff JSON (plus archive path) to stdout. Ctrl-C requests a clean cancel and still waits for the durable handoff.

## Other commands

```bash
colleague status
colleague cancel
colleague context add --file context.json
colleague handoff get
colleague handoff retry
```

These use the meeting id saved under `.colleague/cli-meeting.json` after `join`, or `--meeting-id`.

## Exit codes

| Code | Meaning |
| --- | --- |
| 0 | Durable handoff ready |
| 2 | Validation / usage |
| 3 | Startup (daemon or supervisor) |
| 4 | Runtime failure |
| 5 | Partial finalization |
| 6 | Unrecoverable finalization |
| 130 | Interrupt after cancel+handoff wait |

Tokens and other secrets are never written to argv diagnostics, logs, or progress lines.
