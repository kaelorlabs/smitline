# MCP directories

Smitline is listed so agents and developers can find it. Every listing describes the self-hosted Docker image: the `smitline` container serves MCP over Streamable HTTP at `http://127.0.0.1:8095/mcp` with a bearer token (`docker exec smitline smitline setup register --json` prints it). Nothing is hosted for you; your keys stay in your container.

| Directory | Listing | How it gets there |
| --- | --- | --- |
| [Official MCP Registry](https://registry.modelcontextprotocol.io) | [`io.github.kaelorlabs/smitline`](https://registry.modelcontextprotocol.io/v0/servers?search=io.github.kaelorlabs/smitline) | `server.json` (OCI package `ghcr.io/kaelorlabs/smitline`), published by GitHub Actions on every release. |
| [Smithery](https://smithery.ai/servers/kaelorlabs/smitline) | [`kaelorlabs/smitline`](https://smithery.ai/servers/kaelorlabs/smitline) | The MCP bundle `smitline-smithery.mcpb` ([packages/mcpb](../packages/mcpb)). Smithery lists local servers only as bundles; the bundle relays to the `smitline` container and asks for its token. |
| PulseMCP, GitHub's MCP registry | Mirrored from the official registry | Nothing to do: they read the official registry. |
| [Glama](https://glama.ai/mcp/servers) | Submitted, awaiting review | The GitHub repository; `glama.json` names the maintainers. Glama's search also needs its build check to pass. |

## Publishing a release to the official registry (maintainers)

The registry checks that the image carries the label `io.modelcontextprotocol.server.name="io.github.kaelorlabs/smitline"` (set in the `Dockerfile`).

Publishing is automatic: after a version tag's images are published, `.github/workflows/mcp-registry.yml` signs in with the workflow's GitHub OIDC token, which the registry accepts for `io.github.kaelorlabs/*`, and publishes `server.json`. Nobody signs in and no secret is stored. The release PR sets `version` and the image tag in `server.json` to the new version; the workflow stops if they don't match the tag. To publish again by hand, run the **MCP Registry** workflow from the Actions tab.

## Publishing the bundle to Smithery (maintainers)

```bash
node packages/mcpb/build.mjs
npx -y smithery@latest auth login
npx -y smithery@latest mcp publish packages/mcpb/dist/smitline-smithery.mcpb -n kaelorlabs/smitline
```

`auth login` prints a sign-in link that works on any device. Smithery lists a bundle's tools from its manifest without running it, so it gets `smitline-smithery.mcpb`, which adds each tool's input schema (the standard MCPB manifest allows only names and descriptions). Publish again after a release so the bundle carries the new version and tool list.

For Glama's build check, the image answers MCP over stdio with `docker run -i --rm ghcr.io/kaelorlabs/smitline mcp`, and lists its tools without any keys.
