"""Cursor CLI adapter. Exact resume and models are used only when the installed CLI documents them."""
from .cli_provider import CliCodingProvider


class CursorProvider(CliCodingProvider):
    provider_id = 'cursor'
    binary_names = ('cursor-agent',)
    env_key = 'CURSOR_BIN'
