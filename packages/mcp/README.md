# Smitline MCP server

The call tools for any MCP-capable agent (`smitline-mcp` 0.1.1): phone calls and Zoom, Teams, and Google Meet meetings. Every tool calls the loopback daemon through the TypeScript SDK. It is served three ways:

- **Streamable HTTP** at `http://127.0.0.1:8095/mcp`, from the local console, with a bearer token. This is how local agents connect to the `smitline` container.
- **stdio**, with `smitline mcp` (`src/server.mjs`). Claude Desktop, which only starts stdio servers, runs `docker exec -i smitline smitline mcp`.
- **The remote connector** (`src/remote.mjs`): the same tools over HTTPS with OAuth sign-in, for cloud agents such as ChatGPT and Claude. It runs from a checkout with `start-connector.sh`; see [docs/agents.md](../../docs/agents.md).

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

## Connecting an agent

With the `smitline` container running, this prints the URL, the `Authorization` header, and what to run or paste for Claude Code, Codex, Cursor, and Claude Desktop:

```bash
docker exec smitline smitline setup register --json
```

The token is kept in `/data/.colleague/mcp.token` in the `smitline` volume and stays the same across restarts. For example, Claude Code:

```bash
claude mcp add --transport http --scope user smitline http://127.0.0.1:8095/mcp --header "Authorization: Bearer <token>"
```

The endpoint accepts only requests addressed to `127.0.0.1:8095` or `localhost:8095`, and refuses any request with an `Origin` header, so a web page cannot reach it. Restart the agent after changing its MCP configuration. [SETUP.md](../../SETUP.md) walks through all of this.

### From a checkout

Run from a clone (see [Run from a checkout](../../docs/development.md#run-from-a-checkout)), `smitline setup register` adds a stdio server to Claude Code, Codex, Cursor, and Claude Desktop when they are installed. To do it by hand:

Claude Code:

```bash
claude mcp add --scope user smitline -- node /absolute/path/to/smitline/packages/mcp/src/server.mjs
```

Codex (`~/.codex/config.toml`, see `examples/codex.mcp.toml`):

```toml
[mcp_servers.smitline]
command = "node"
args = ["/absolute/path/to/smitline/packages/mcp/src/server.mjs"]
cwd = "/absolute/path/to/smitline"
```

Cursor (`mcp.json`, see `examples/cursor.mcp.json`):

```json
{
  "mcpServers": {
    "smitline": {
      "command": "node",
      "args": ["/absolute/path/to/smitline/packages/mcp/src/server.mjs"]
    }
  }
}
```

## Security

- Loopback daemon only (`127.0.0.1`)
- `.colleague/daemon.auth` is read by the SDK and never returned
- stdio: stdout is JSON-RPC frames only; logs go to stderr and are redacted
- Calls run in the daemon, so stopping the MCP server does not end a call in progress
