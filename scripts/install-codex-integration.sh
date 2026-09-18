#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SERVER="$ROOT/packages/mcp/src/server.mjs"
CLI_SOURCE="$ROOT/packages/cli/src/colleague.mjs"
SKILL_SOURCE="$ROOT/.agents/skills/join-colleague-ai-meeting"
SKILLS_HOME="${CODEX_SKILLS_DIR:-${CODEX_HOME:-$HOME/.codex}/skills}"
SKILL_DEST="$SKILLS_HOME/join-colleague-ai-meeting"
BIN_HOME="${CODEX_BIN_DIR:-${CODEX_HOME:-$HOME/.codex}/bin}"
CLI_DEST="$BIN_HOME/colleague"

if [[ ! -f "$SERVER" ]]; then
  echo "Colleague AI MCP server is missing: $SERVER" >&2
  exit 1
fi

if [[ ! -f "$CLI_SOURCE" ]]; then
  echo "Colleague AI CLI is missing: $CLI_SOURCE" >&2
  exit 1
fi

if [[ ! -f "$SKILL_SOURCE/SKILL.md" ]]; then
  echo "Colleague AI Codex skill is missing: $SKILL_SOURCE/SKILL.md" >&2
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

install -d -m 700 "$BIN_HOME"
chmod 755 "$CLI_SOURCE"
ln -sfn "$CLI_SOURCE" "$CLI_DEST"

install -d -m 700 "$SKILL_DEST" "$SKILL_DEST/agents"
install -m 600 "$SKILL_SOURCE/SKILL.md" "$SKILL_DEST/SKILL.md"
install -m 600 "$SKILL_SOURCE/agents/openai.yaml" "$SKILL_DEST/agents/openai.yaml"

echo
echo "Colleague AI is registered with Codex."
echo "The exact-continuity launcher is installed at $CLI_DEST."
echo "The exact-continuity meeting skill is installed at $SKILL_DEST."
echo "Restart Codex so it reloads the meeting skill and MCP controls."
echo "Meeting launch now runs inside the active task so CODEX_THREAD_ID is inherited directly."
echo "Then open a project task and ask: Join this meeting: <invite URL>"
