#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SERVER="$ROOT/packages/mcp/src/server.mjs"

if [[ ! -f "$SERVER" ]]; then
  echo "Colleague AI MCP server is missing: $SERVER" >&2
  exit 1
fi

if ! command -v node >/dev/null 2>&1; then
  echo "Node.js 22 or newer is required." >&2
  exit 1
fi

NODE_MAJOR="$(node -p 'Number(process.versions.node.split(".")[0])')"
if [[ "$NODE_MAJOR" -lt 22 ]]; then
  echo "Node.js 22 or newer is required; found $(node --version)." >&2
  exit 1
fi

if [[ -n "${CODEX_BIN:-}" ]]; then
  CODEX="$CODEX_BIN"
elif command -v codex >/dev/null 2>&1; then
  CODEX="$(command -v codex)"
elif [[ -x /Applications/ChatGPT.app/Contents/Resources/codex ]]; then
  CODEX=/Applications/ChatGPT.app/Contents/Resources/codex
else
  echo "Codex CLI was not found. Install Codex or set CODEX_BIN." >&2
  exit 1
fi

if "$CODEX" mcp get colleague-ai >/dev/null 2>&1; then
  "$CODEX" mcp remove colleague-ai >/dev/null
fi

"$CODEX" mcp add colleague-ai -- node "$SERVER"

echo
echo "Colleague AI is registered with Codex."
echo "Restart Codex so it loads the join_current_meeting tool."
echo "Then open a project task and ask: Join this meeting: <invite URL>"
