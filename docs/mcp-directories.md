# MCP directories

Smitline is listed so agents and developers can find it. Every listing describes the self-hosted Docker image: the `smitline` container serves MCP over Streamable HTTP at `http://127.0.0.1:8095/mcp` with a bearer token (`docker exec smitline smitline setup register --json` prints it). Nothing is hosted for you; your keys stay in your container.

| Directory | Listing | How it gets there |
| --- | --- | --- |
| [Official MCP Registry](https://registry.modelcontextprotocol.io) | [`io.github.kaelorlabs/smitline`](https://registry.modelcontextprotocol.io/v0/servers?search=io.github.kaelorlabs/smitline) | `server.json` (OCI package `ghcr.io/kaelorlabs/smitline`), published by GitHub Actions on every release. |
| [Glama](https://glama.ai/mcp/servers) | [`kaelorlabs/smitline`](https://glama.ai/mcp/servers/kaelorlabs/smitline) | The GitHub repository; `glama.json` names the maintainers. Glama's build check runs `node packages/mcp/src/server.mjs` from a clone (no install step, no keys) behind its `mcp-proxy`, and lists the tools. |
| [MCP Market](https://mcpmarket.com) | [`smitline`](https://mcpmarket.com/server/smitline) | Listed from the GitHub repository. A maintainer can claim it with GitHub to edit it. |
| [LobeHub](https://lobehub.com/mcp) | [`kaelorlabs-smitline`](https://lobehub.com/mcp/kaelorlabs-smitline) | Built from the GitHub repository. A maintainer can claim it to update it. |

Smithery listed Smitline until it became part of Arcade: its proxy and accounts shut down on 2026-10-31, and the listing page is gone. PulseMCP has paused new listings.

## Publishing a release to the official registry (maintainers)

The registry checks that the image carries the label `io.modelcontextprotocol.server.name="io.github.kaelorlabs/smitline"` (set in the `Dockerfile`).

Publishing is automatic: after a version tag's images are published, `.github/workflows/mcp-registry.yml` signs in with the workflow's GitHub OIDC token, which the registry accepts for `io.github.kaelorlabs/*`, and publishes `server.json`. Nobody signs in and no secret is stored. The release PR sets `version` and the image tag in `server.json` to the new version; the workflow stops if they don't match the tag. To publish again by hand, run the **MCP Registry** workflow from the Actions tab.

## Glama's build check

For Glama's build check, the image answers MCP over stdio with `docker run -i --rm ghcr.io/kaelorlabs/smitline mcp`, and lists its tools without any keys.
