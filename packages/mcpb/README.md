# Smitline MCP bundle

`smitline.mcpb` connects an MCP client that installs [MCP bundles](https://github.com/modelcontextprotocol/mcpb) (Claude Desktop, Smithery, and others) to Smitline running on your computer. Your agent can then place phone calls and join Zoom, Teams, and Google Meet meetings.

Smitline itself runs in Docker with your own keys; set it up first with [SETUP.md](../../SETUP.md). The bundle only relays MCP messages between the client and the `smitline` container's endpoint, `http://127.0.0.1:8095/mcp`. It has no dependencies and sends nothing anywhere else.

When you install it, the client asks for:

| Setting | Value |
| --- | --- |
| Smitline MCP token | `docker exec smitline smitline setup register --json` prints it. |
| Smitline MCP address | `http://127.0.0.1:8095/mcp` unless you changed the port. |

If Smitline is not running, the client still sees the tools, and a call to one says how to start Smitline.

## Building

```bash
node packages/mcpb/build.mjs
```

This refreshes `server/tools.json` from the MCP server's own tool list, and writes `packages/mcpb/dist/smitline.mcpb` with the repository's version. `npm test` fails when `tools.json` is out of date.
