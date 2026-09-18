# colleague CLI

Blocking lifecycle CLI for the local Colleague AI daemon. It wraps the TypeScript SDK and does not print meeting transcript text by default.

Requires Node.js 22+. Not published to npm. Talks only to the loopback daemon.

## Join

Exact continuity requires `--thread` (the real originating coding-agent session id). Never pass `last`, `latest`, or `--last`. Zoom, Teams, and Google Meet HTTPS invites are accepted. The microphone joins muted and opens automatically only while Colleague AI delivers selected speech; an always-unmuted launch mode is intentionally unsupported.

Inside Codex, `--agent` defaults to `codex`, `--thread` defaults to the host-provided `CODEX_THREAD_ID`, and `--workspace` defaults to the current directory. The installed launcher resolves its daemon and auth files from the Colleague AI checkout, independently of the caller's current project:

```bash
~/.codex/bin/colleague join --meeting "https://us05web.zoom.us/j/YOUR_MEETING_ID"
```

Validate a context handoff before making any daemon request:

```bash
~/.codex/bin/colleague context validate --file /private/tmp/context.json
```

The context object requires string fields `objective`, `currentTask`, and `summary`; string arrays `decisions`, `constraints`, `openQuestions`, and `importantFiles`; and `recentConversation` entries shaped as `{ "role": "user" | "assistant", "text": "..." }`. Optional `git` accepts only `branch`, `commit`, and `dirty`.

Outside Codex, or when selecting another provider, pass the values explicitly:

```bash
colleague join \
  --meeting "https://us05web.zoom.us/j/YOUR_MEETING_ID" \
  --agent codex \
  --thread "$CODEX_THREAD_ID" \
  --workspace "$PWD" \
  --wait
```

`--agent` may be `codex`, `cursor`, or `claude-code` when that CLI is installed and capable. `--context-continuity` joins without an originating thread (`sessionId=local-portal`). `--no-camera` is audio-only. `--screen-share` opts in to incoming shared-content capture (off by default).

`--wait` stays in the foreground, prints concise lifecycle and delegation progress on stderr, and writes the final handoff JSON (plus archive path) to stdout. Ctrl-C requests a clean cancel and still waits for the durable handoff. Agent-native Codex launches omit `--wait` so the originating task becomes idle and can be resumed for meeting delegations.

If a participant is already running, `join` reports its meeting id and cancellation command. Pass `--replace` only when switching meetings is authorized; it cancels the active meeting and then starts the new one. `status` checks the daemon-owned active meeting record before the CLI's previous-meeting record.

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
