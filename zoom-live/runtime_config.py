"""Validated runtime configuration for a Colleague AI meeting participant."""
from dataclasses import dataclass
import os
from pathlib import Path

from codex_tool import CODEX_MODELS


def _boolean(value, default=False):
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in ('1', 'true', 'yes', 'on'):
        return True
    if normalized in ('0', 'false', 'no', 'off'):
        return False
    raise ValueError(f'Expected a boolean value, received {value!r}')


@dataclass(frozen=True)
class RuntimeConfig:
    participant_name: str
    default_codex_model: str
    web_search_enabled: bool
    codex_enabled: bool
    charts_enabled: bool
    workspace: str
    meeting_instructions: str

    @classmethod
    def from_environ(cls, environ=None):
        env = os.environ if environ is None else environ
        name = env.get('COLLEAGUE_PARTICIPANT_NAME', 'Colleague AI').strip()
        if not name or len(name) > 80 or any(ord(char) < 32 for char in name):
            raise ValueError('COLLEAGUE_PARTICIPANT_NAME must contain 1–80 printable characters')

        model = env.get('COLLEAGUE_CODEX_MODEL', 'gpt-5.6-terra').strip()
        if model not in CODEX_MODELS:
            raise ValueError(f'COLLEAGUE_CODEX_MODEL must be one of: {", ".join(CODEX_MODELS)}')

        web_search = _boolean(env.get('COLLEAGUE_ENABLE_WEB_SEARCH'), True)
        codex = _boolean(env.get('COLLEAGUE_ENABLE_CODEX'), True)
        charts = _boolean(env.get('COLLEAGUE_ENABLE_CHARTS'), False)
        if charts and not codex:
            raise ValueError('COLLEAGUE_ENABLE_CHARTS requires COLLEAGUE_ENABLE_CODEX=1')
        workspace = env.get('COLLEAGUE_WORKSPACE', '').strip()
        if workspace and not Path(workspace).is_absolute():
            raise ValueError('COLLEAGUE_WORKSPACE must be an absolute path')
        meeting_instructions = env.get('COLLEAGUE_MEETING_INSTRUCTIONS', '').strip()
        if len(meeting_instructions) > 2000 or any(ord(char) < 32 for char in meeting_instructions):
            raise ValueError('COLLEAGUE_MEETING_INSTRUCTIONS must contain at most 2000 printable characters')
        return cls(name, model, web_search, codex, charts, workspace, meeting_instructions)
