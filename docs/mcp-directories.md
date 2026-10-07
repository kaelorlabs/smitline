# MCP directories

Smitline is listed as `io.github.kaelorlabs/smitline` so agents and developers can find it. The listing describes the self-hosted Docker image: the `smitline` container serves MCP over Streamable HTTP at `http://127.0.0.1:8095/mcp` with a bearer token (`docker exec smitline smitline setup register --json` prints it). Nothing is hosted for you; your keys stay in your container.

| Directory | Listed from | Notes |
| --- | --- | --- |
| [Official MCP Registry](https://registry.modelcontextprotocol.io) | `server.json` (OCI package `ghcr.io/kaelorlabs/smitline`) | PulseMCP and GitHub's MCP registry pick entries up from here. |
| [Glama](https://glama.ai/mcp/servers) | The GitHub repository; `glama.json` names the maintainers | Glama's search also needs its build check to pass. |
| [Smithery](https://smithery.ai) | The MCP bundle `smitline.mcpb` ([packages/mcpb](../packages/mcpb)) | Smithery lists local servers only as bundles. The bundle relays to the `smitline` container and asks for its token. |

## Publishing a release to the official registry (maintainers)

The registry checks that the image carries the label `io.modelcontextprotocol.server.name="io.github.kaelorlabs/smitline"` (set in the `Dockerfile`), so only images built after that label was added can be listed.

1. Push the release tag (for example `v0.2.0`) and wait for the image workflow to publish `ghcr.io/kaelorlabs/smitline:0.2.0`.
2. Set `version` and the image tag in `server.json` to that version (both are `0.2.0` now).
3. Install [`mcp-publisher`](https://github.com/modelcontextprotocol/registry/releases), then from the repository root:

   ```bash
   mcp-publisher validate
   mcp-publisher login github
   mcp-publisher publish
   ```

   `login github` opens GitHub's device sign-in. Publishing under `io.github.kaelorlabs/` needs an owner of the kaelorlabs organization. A new version is a new `publish`; earlier versions stay listed.

## Publishing the bundle to Smithery (maintainers)

```bash
node packages/mcpb/build.mjs
npx -y smithery@latest auth login
npx -y smithery@latest mcp publish packages/mcpb/dist/smitline-smithery.mcpb -n kaelorlabs/smitline
```

`auth login` prints a sign-in link that works on any device. Smithery lists a bundle's tools from its manifest without running it, so it gets `smitline-smithery.mcpb`, which adds each tool's input schema (the standard MCPB manifest allows only names and descriptions). Publish again after a release so the bundle carries the new version and tool list.

For Glama's build check, the image answers MCP over stdio with `docker run -i --rm ghcr.io/kaelorlabs/smitline mcp`, and lists its tools without any keys.
