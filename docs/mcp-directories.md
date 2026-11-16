# MCP directories

Smitline is listed so agents and developers can find it. Where a directory shows a display name, it is **Smitline: AI Phone Calls & Meetings**. Listings offer two ways to use it:

- **Self-hosted:** the `smitline` container serves MCP over Streamable HTTP at `http://127.0.0.1:8095/mcp` with a bearer token (`docker exec smitline smitline setup register --json` prints it). Your keys stay in your container.
- **Smitline Cloud:** the hosted version by Kaelor Labs, at `https://app.smitline.com/mcp`. Clients sign in with OAuth, and there is nothing to install. It is in private beta: without an invite, sign-in leads to the [early access](https://smitline.com/early-access) page.

| Directory | Listing | How it gets there |
| --- | --- | --- |
| [Official MCP Registry](https://registry.modelcontextprotocol.io) | [`io.github.kaelorlabs/smitline`](https://registry.modelcontextprotocol.io/v0/servers?search=io.github.kaelorlabs/smitline) | `server.json`: the OCI package `ghcr.io/kaelorlabs/smitline` and, as a `remotes` entry, Smitline Cloud's address. Published by GitHub Actions on every release. |
| [Glama](https://glama.ai/mcp/servers) | [`kaelorlabs/smitline`](https://glama.ai/mcp/servers/kaelorlabs/smitline) | The GitHub repository; `glama.json` names the maintainers, who set the name, thumbnail, description and categories under Admin → Listing. Glama's build check runs `node packages/mcp/src/server.mjs` from a clone (no install step, no keys) behind its `mcp-proxy`, and lists the tools. Glama makes a separate connector page for Smitline Cloud from the registry's `remotes` entry, which a maintainer claims. |
| [MCP Market](https://mcpmarket.com) | [`smitline`](https://mcpmarket.com/server/smitline) | Listed from the GitHub repository. It has no claim button; changes go to support@mcpmarket.com. |
| [LobeHub](https://lobehub.com/mcp) | [`kaelorlabs-smitline`](https://lobehub.com/mcp/kaelorlabs-smitline) | Built from the GitHub repository. A maintainer claims it with `@lobehub/market-cli` to update it; its `cloudEndpoint` field takes Smitline Cloud's address. |
| [mcp.so](https://mcp.so) | [`smitline`](https://mcp.so/servers/smitline) | Submitted as an issue ([chatmcp/mcpso#6742](https://github.com/chatmcp/mcpso/issues/6742)). Changes go in a reply there or to support@mcp.so. |
| [Ext Ops Panel](https://extops.dev/plugins) | [`io.github.kaelorlabs/smitline`](https://extops.dev/plugins/mcp/io.github.kaelorlabs/smitline) | Listed from the official MCP Registry entry. A maintainer can verify ownership to show as its author. It offers a banner for the README, which the README shows. |

Smithery listed Smitline until it became part of Arcade: its proxy and accounts shut down on 2026-10-31, and the listing page is gone. PulseMCP has paused new listings.

## Publishing a release to the official registry (maintainers)

The registry checks that the image carries the label `io.modelcontextprotocol.server.name="io.github.kaelorlabs/smitline"` (set in the `Dockerfile`).

`server.json` lists both ways in: the image under `packages`, and Smitline Cloud under `remotes` with no headers, since clients find its OAuth sign-in when they connect. The registry has no field for the beta; the description says "Hosted or self-hosted."

Publishing is automatic: after a version tag's images are published, `.github/workflows/mcp-registry.yml` signs in with the workflow's GitHub OIDC token, which the registry accepts for `io.github.kaelorlabs/*`, and publishes `server.json`. Nobody signs in and no secret is stored. The release PR sets `version` and the image tag in `server.json` to the new version; the workflow stops if they don't match the tag. To publish again by hand, run the **MCP Registry** workflow from the Actions tab.

## Glama's build check

For Glama's build check, the image answers MCP over stdio with `docker run -i --rm ghcr.io/kaelorlabs/smitline mcp`, and lists its tools without any keys.
