"""Claude Code CLI adapter. Exact resume and models are used only when the installed CLI documents them."""
from .cli_provider import CliCodingProvider


class ClaudeCodeProvider(CliCodingProvider):
    provider_id = 'claude-code'
    binary_names = ('claude',)
    env_key = 'CLAUDE_BIN'
