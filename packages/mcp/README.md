# Colleague AI MCP adapter

Local stdio MCP server (`@colleague-ai/mcp` 1.0.0) over the TypeScript SDK. It does not own a second meeting runtime. All tools call the loopback daemon through `@colleague-ai/sdk`.

Do not publish this package and do not install it globally.

## Tools

| Tool | Behavior |
| --- | --- |
| `join_current_meeting` | Codex-native exact-continuity join using the invoking task's `CODEX_THREAD_ID` |
| `start_meeting` | Creates a meeting and returns a durable `{ meetingId }` immediately |
| `get_meeting_status` | Current daemon session |
| `add_meeting_context` | Versioned `ContextHandoff` |
| `cancel_meeting` | SDK cancel; includes a partial handoff when one is ready |
| `get_meeting_handoff` | Poll for the durable handoff |
| `retry_meeting_handoff` | Retry exact-session append |
| `list_meeting_approvals` / `get_meeting_approval` / `decide_meeting_approval` | One decision per approval |
| `list_meeting_artifacts` / `get_meeting_artifact` | Metadata; content is local |
| `list_coding_providers` | Truthful capability records |
| `get_runner_status` / `pair_runner` / `complete_runner_pair` / `unpair_runner` | Foundation pairing; loopback remains supported |

`start_meeting` requires `url`, `provider`, `workspace`, `context`, and `permissions`. Exact continuity also requires the **real originating `sessionId`**. This server never reads the host conversation id, never invents a thread, and rejects `last` / `latest` / `--last`. `cameraEnabled` defaults true. `screenShareEnabled` defaults false and cannot be turned on by voice.

Set `continuity: "context"` for generic MCP clients. That uses `sessionId: "local-portal"` and must not be treated as exact Codex/Cursor resume.

## MCP Tasks

If the client includes `io.modelcontextprotocol/tasks` on the `start_meeting` request and passes `waitUntilHandoff: true`, the server returns a `CreateTaskResult` (`resultType: "task"`) and waits for the durable handoff. Poll `tasks/get`. Progress notifications describe lifecycle and delegation only — never transcript text.

Clients without Tasks should poll `get_meeting_handoff`.

## Codex

Codex supplies `CODEX_THREAD_ID` to task command subprocesses, but does not forward that per-task value to a persistent MCP server. Before calling `join_current_meeting`, read the variable from the current task command environment and pass its exact value as `sessionId`. Never invent, infer, or reuse a different ID. The tool also requires the current workspace and a structured context handoff.

Example `~/.codex/config.toml` mcp_servers fragment:

```toml
[mcp_servers.colleague-ai]
command = "node"
args = ["/absolute/path/to/colleague-ai-private/packages/mcp/src/server.mjs"]
cwd = "/absolute/path/to/colleague-ai-private"
```

See `examples/codex.mcp.toml`. Restart Codex after changing MCP configuration. Use `start_meeting` with an explicit session id only for non-Codex hosts or integration testing.

## Cursor

Cursor MCP config (`mcp.json`):

```json
{
  "mcpServers": {
    "colleague-ai": {
      "command": "node",
      "args": ["/absolute/path/to/colleague-ai-private/packages/mcp/src/server.mjs"]
    }
  }
}
```

See `examples/cursor.mcp.json`. Cursor must inject the current composer/chat id; without it, use `continuity: "context"`.

## Claude Code

Claude Code may start this MCP server through Anthropic's documented MCP client setup. Exact continuity still requires the real originating session id from that host. If the installed `claude --help` does not document a resume flag, use `continuity: "context"`. Colleague AI does not invent Claude MCP config keys or resume flags.

## Security

- Loopback daemon only (`127.0.0.1`)
- `.colleague/daemon.auth` is read by the SDK and never returned
- stdout is JSON-RPC frames only; logs go to stderr and are redacted
- SIGINT/SIGTERM cancel live meetings unless `COLLEAGUE_MCP_LEAVE_RUNNING=1`
