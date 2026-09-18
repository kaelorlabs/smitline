# Coding-agent providers

Colleague AI can delegate meeting work to a local coding agent. Codex remains the default. Cursor and Claude Code are optional adapters that never invent session continuity or CLI flags.

## Choosing a provider

- **SDK, CLI, and MCP host integrations** must pass the real originating session id when they want exact continuity. Never send `last`, `latest`, or `--last`.
- **Local portal users** always join with context continuity (`local-portal`). Paste a Cursor or Claude session id only through a host integration that actually has that id.
- Enable only one coding agent per meeting.

## Codex

Codex is unchanged. Exact thread resume and context continuity keep using the existing host job worker. Official login remains `codex login`.

## Cursor

Use the official **Cursor CLI** installed on this computer. Colleague AI looks for `cursor-agent` or `CURSOR_BIN`. It reads `cursor-agent --help` and `--version` only.

- Exact resume runs only when that help text documents a resume/session flag such as `--resume`.
- Noninteractive context mode runs only when help documents `--print` or `--output-format`.
- Model names are not guessed. An explicit model override requires caller authorization; exact mode will not silently change model.
- Log in with the CLI's own documented login command. Colleague AI does not collect or store Cursor credentials.

If the installed CLI cannot resume a session, start the meeting with `continuity: "context"` instead of exact mode.

## Claude Code

Use the official **Claude Code CLI** (`claude`, or `CLAUDE_BIN`). Feature detection is the same: only flags present in `claude --help` are used.

- Official local login is `claude login` as documented by Anthropic. Colleague AI does not store that login.
- Exact resume requires a documented `--resume` (or equivalent) flag. Otherwise exact mode is rejected with `exact_resume_unsupported`.
- Mutations, tests, commits, and pushes never go through `claude`; they stay on the generic approval, workspace executor, and git broker.

## Status

`GET /v1/providers`, `colleague providers`, MCP `list_coding_providers`, and the SDKs return truthful capability records: installed, usable, exactSessionResume, contextContinuity, and `reasonUnavailable` when a binary or documented flag is missing. Unknown provider ids fail closed.

See [capabilities](capabilities.md) for the Codex / Cursor / Claude Code matrix.
