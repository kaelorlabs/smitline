# Agents

Any agent can ask Colleague AI to phone someone or join a meeting: it sends a [brief](calls.md#brief) and gets back a [result](calls.md#result). Agents on this computer talk to the loopback daemon directly. Cloud agents such as ChatGPT and Claude cannot reach this computer, so they use the remote connector.

## Remote connector for cloud agents

The remote connector is an MCP server that cloud agents reach over HTTPS. It uses the MCP Streamable HTTP transport (protocol versions 2025-06-18 and 2025-03-26) and OAuth 2.1 sign-in, so an agent can connect only after you approve it with your owner passphrase. It runs next to the daemon, listens on `127.0.0.1:8767`, and is published by your TLS proxy. It offers only the call tools, and it reaches the daemon through the TypeScript SDK on the same machine.

```text
ChatGPT / Claude ──HTTPS──► your proxy (TLS) ──► connector 127.0.0.1:8767 ──► daemon 127.0.0.1:8765
```

### Set up server mode

You need a machine that stays on and already places calls (see [phone calls](phone.md); meetings also need Docker), and a domain name for it, such as `colleague.example.com`.

1. Point the domain's DNS record at the machine and open ports 80 and 443.
2. Put a TLS proxy in front of the connector. Forward only `/mcp`, `/oauth/*`, and `/.well-known/*` to port 8767. With [Caddy](https://caddyserver.com/), which gets the certificate by itself:

   ```caddyfile
   colleague.example.com {
   	@connector path /mcp /oauth/* /.well-known/*
   	handle @connector {
   		reverse_proxy 127.0.0.1:8767
   	}
   	handle {
   		respond 404
   	}
   }
   ```

   If the same domain also serves the phone gateway, add `handle /twilio/* { reverse_proxy 127.0.0.1:8766 }`.
3. Set the public address. It must be the https origin only, with no path:

   ```bash
   colleague setup set COLLEAGUE_CONNECTOR_URL https://colleague.example.com
   ```

4. Choose the owner passphrase: run `colleague setup secrets` and fill in **Owner passphrase** under **Remote connector (server mode)**. It needs at least 12 characters; a few random words work well. The setup page only opens on the machine itself, so on a headless server run `colleague setup secrets --no-open` and forward the printed port over SSH (`ssh -L PORT:127.0.0.1:PORT server`). You can also write `COLLEAGUE_CONNECTOR_PASSPHRASE=...` into `.env` yourself.
5. Start the connector and keep it running under your process manager (systemd, tmux, or similar):

   ```bash
   ./start-connector.sh
   ```

   It refuses to start, and says what is missing, until the address and passphrase are set. It prints the connector URL, `https://colleague.example.com/mcp`. If the daemon is not running, the first call starts it. The connector does not need the daemon's own server mode (`COLLEAGUE_SERVER_MODE`); the daemon stays on loopback.

| Variable | Default | Purpose |
| --- | --- | --- |
| `COLLEAGUE_CONNECTOR_URL` | none | Public https origin of the connector, such as `https://colleague.example.com`. Required. |
| `COLLEAGUE_CONNECTOR_PASSPHRASE` | none | Owner passphrase for approving agents, at least 12 characters. Required. |
| `COLLEAGUE_CONNECTOR_PORT` | `8767` | Loopback port the proxy forwards to. |
| `COLLEAGUE_CONNECTOR_ALLOWED_ORIGINS` | none | Comma-separated browser origins, besides the connector's own, that may call `/mcp` and `/oauth/*`. Only needed for browser-based MCP clients, such as a local MCP Inspector. |

The connector reads these from its environment first, then from `.env`, when it starts. Restart it after a change.

### Add the connector to your agent

Add `https://colleague.example.com/mcp` as a custom connector (also called a remote MCP server) in the agent. The agent registers itself, then opens an approval page from your server in the browser. Check the app name and the return address on that page, enter the owner passphrase, and choose **Allow**.

Menus change often, and custom connectors may depend on your plan or on workspace settings, so check each product's current help pages. At the time of writing:

- **Claude** (claude.ai, the desktop app, and mobile): open the settings for connectors, choose to add a custom connector, and paste the URL. On Team and Enterprise plans an owner may need to add it for the organization first.
- **ChatGPT**: custom MCP connectors are under the settings for connectors or apps, sometimes behind a developer mode switch. Create a connector with the URL and choose OAuth sign-in.
- **Grok, Perplexity, and others**: check whether the product supports custom remote MCP connectors with OAuth sign-in. Any client that supports Streamable HTTP, OAuth dynamic client registration, and PKCE can use the same URL.

Coding agents on the same computer should use the local MCP server instead; it offers more than calls.

Then ask the agent something like "Call the restaurant at +1 415 555 0142 and book a table for four at 7 tonight." It writes a brief with `start_call`, asks you for anything missing, follows the call with `wait_for_call`, and reports the outcome.

### Security model

- **You approve every agent.** Agents can register themselves, but they get no access until you type the owner passphrase on the approval page. The page shows the app name the agent claims and the address the browser returns to; approve only a connection you just started yourself. After 5 wrong passphrases in 10 minutes, approval locks for 10 minutes for everyone.
- **Short-lived, revocable tokens.** Access tokens are random, opaque, and last 1 hour. Refresh tokens last 30 days and change on every use; if an old refresh token or authorization code is used again, that agent's access is revoked. PKCE (S256) is required, authorization codes work once within 10 minutes, and tokens are bound to `https://<domain>/mcp`. Only SHA-256 digests are stored, in `.colleague/connector/` with owner-only permissions. Tokens and the passphrase are never logged.
- **Call tools only.** Agents can check a brief, start, follow, list, instruct, end, and transfer calls, and list voices. They cannot reach meeting coding tools, workspaces, approvals, commits, pushes, or runner pairing. An approved agent can still place calls that cost money and speak for you, within `COLLEAGUE_ALLOWED_CALLING_CODES`, so approve only agents you trust.
- **The daemon stays on loopback.** The connector listens on `127.0.0.1` only, and just its routes are public through your proxy. It rejects requests from browser origins other than its own (and `COLLEAGUE_CONNECTOR_ALLOWED_ORIGINS`), and request bodies larger than 1 MB.

See and revoke access with the CLI. Revoking takes effect on a running connector.

```bash
colleague connector status                  # registered agents and active grants, never tokens
colleague connector revoke --client <id>    # one agent
colleague connector revoke --all            # every agent
```

Changing the passphrase does not revoke existing grants; run `colleague connector revoke --all` as well. Changing `COLLEAGUE_CONNECTOR_URL` invalidates all tokens, because they are bound to the old address.
