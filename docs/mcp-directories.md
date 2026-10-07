# MCP directories

Smitline is listed as `io.github.kaelorlabs/smitline` so agents and developers can find it. The listing describes the self-hosted Docker image: the `smitline` container serves MCP over Streamable HTTP at `http://127.0.0.1:8095/mcp` with a bearer token (`docker exec smitline smitline setup register --json` prints it). Nothing is hosted for you; your keys stay in your container.

| Directory | Listed from | Notes |
| --- | --- | --- |
| [Official MCP Registry](https://registry.modelcontextprotocol.io) | `server.json` (OCI package `ghcr.io/kaelorlabs/smitline`) | PulseMCP and GitHub's MCP registry pick entries up from here. |
| [Glama](https://glama.ai/mcp/servers) | The GitHub repository; `glama.json` names the maintainers | Glama's search also needs its build check to pass. |

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
