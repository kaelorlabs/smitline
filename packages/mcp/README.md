# Colleague AI MCP adapter

Local stdio MCP server (`@colleague-ai/mcp` 1.0.0) over the TypeScript SDK. It gives any MCP-capable agent phone calls and meetings: every tool calls the loopback daemon through `@colleague-ai/sdk`.

`src/remote.mjs` is the remote connector: the same call tools over MCP Streamable HTTP with OAuth sign-in, for cloud agents such as ChatGPT and Claude. Start it with `start-connector.sh`; see [docs/agents.md](../../docs/agents.md).

Do not publish this package and do not install it globally.

## Tools

| Tool | Behavior |
| --- | --- |
| `start_call` | Place a phone call or join a meeting from a brief; returns the queued call at once |
| `check_call_brief` | Validate a brief without placing the call; missing fields come with a question to ask |
| `wait_for_call` / `get_call` / `list_calls` | Follow a call to its structured result |
| `send_call_instruction` / `end_call` / `transfer_call_to_me` | Steer, end, or take over a call in progress |
| `list_voices` | GPT-Live voices |
| `get_profile` / `update_profile` | The user's profile that every call gets as background |

See [calls](../../docs/calls.md) for the brief and the result.

## Phone calls and meetings

Both go through `start_call`:

- Phone: `{ "channel": "phone", "to": "+14155550142", "objective": "..." }`
- Meeting: `{ "channel": "meeting", "to": "https://zoom.us/j/...", "objective": "..." }` joins that Zoom, Teams, or Google Meet invite.

Then call `wait_for_call` until the status is `completed`, `failed`, or `canceled`, and tell the user the outcome.

## Registering the server

`colleague setup register` adds the server to Claude Code, Codex, Cursor, and Claude Desktop when they are installed. To do it by hand:

Claude Code:

```bash
claude mcp add --scope user colleague-ai -- node /absolute/path/to/colleague-ai/packages/mcp/src/server.mjs
```

Codex (`~/.codex/config.toml`, see `examples/codex.mcp.toml`):

```toml
[mcp_servers.colleague-ai]
command = "node"
args = ["/absolute/path/to/colleague-ai/packages/mcp/src/server.mjs"]
cwd = "/absolute/path/to/colleague-ai"
```

Cursor (`mcp.json`, see `examples/cursor.mcp.json`):

```json
{
  "mcpServers": {
    "colleague-ai": {
      "command": "node",
      "args": ["/absolute/path/to/colleague-ai/packages/mcp/src/server.mjs"]
    }
  }
}
```

Restart the agent after changing its MCP configuration.

## Security

- Loopback daemon only (`127.0.0.1`)
- `.colleague/daemon.auth` is read by the SDK and never returned
- stdout is JSON-RPC frames only; logs go to stderr and are redacted
- Calls run in the daemon, so stopping the MCP server does not end a call in progress
