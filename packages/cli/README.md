# colleague CLI

Blocking lifecycle CLI for the local Colleague AI daemon. It wraps the TypeScript SDK and does not print meeting transcript text by default.

Requires Node.js 22+. Not published to npm. Talks only to the loopback daemon.

## Join

Exact continuity requires `--thread` (the real originating coding-agent session id). Never pass `last`, `latest`, or `--last`. Zoom, Teams, and Google Meet HTTPS invites are accepted.

```bash
colleague join \
  --meeting "https://us05web.zoom.us/j/YOUR_MEETING_ID" \
  --agent codex \
  --thread "$CODEX_THREAD_ID" \
  --workspace "$PWD" \
  --wait
```

`--agent` may be `codex`, `cursor`, or `claude-code` when that CLI is installed and capable. `--context-continuity` joins without an originating thread (`sessionId=local-portal`). `--no-camera` is audio-only. `--screen-share` opts in to incoming shared-content capture (off by default).

`--wait` stays in the foreground, prints concise lifecycle and delegation progress on stderr, and writes the final handoff JSON (plus archive path) to stdout. Ctrl-C requests a clean cancel and still waits for the durable handoff.

## Other commands

```bash
colleague status
colleague cancel
colleague context add --file context.json
colleague handoff get
colleague handoff retry
colleague approvals list --meeting-id <id>
colleague artifacts list --meeting-id <id>
colleague commits list --meeting-id <id>
colleague screen-share status --meeting-id <id>
colleague providers
colleague runner status
colleague runner pair
colleague runner complete --pairing-id <id> --pairing-code <code>
colleague runner unpair
```

Meeting-scoped commands use the id saved under `.colleague/cli-meeting.json` after `join`, or `--meeting-id`. Runner pairing reveals the code once; status never includes it. Local loopback remains the supported path.

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
