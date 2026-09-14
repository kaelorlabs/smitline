"""Validated runtime configuration for a Colleague AI meeting participant."""
from dataclasses import dataclass
import os

from codex_tool import CODEX_MODELS


PROFILES = ('standard', 'demo', 'fact_check')


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
    profile: str
    default_codex_model: str
    demo_data_enabled: bool

    @classmethod
    def from_environ(cls, environ=None):
        env = os.environ if environ is None else environ
        name = env.get('COLLEAGUE_PARTICIPANT_NAME', 'Colleague AI').strip()
        if not name or len(name) > 80 or any(ord(char) < 32 for char in name):
            raise ValueError('COLLEAGUE_PARTICIPANT_NAME must contain 1–80 printable characters')

        profile = env.get('COLLEAGUE_PROFILE', 'standard').strip().lower()
        if env.get('COLLEAGUE_FACT_CHECK') == '1' and 'COLLEAGUE_PROFILE' not in env:
            profile = 'fact_check'
        if profile not in PROFILES:
            raise ValueError(f'COLLEAGUE_PROFILE must be one of: {", ".join(PROFILES)}')

        model = env.get('COLLEAGUE_CODEX_MODEL', 'gpt-5.6-terra').strip()
        if model not in CODEX_MODELS:
            raise ValueError(f'COLLEAGUE_CODEX_MODEL must be one of: {", ".join(CODEX_MODELS)}')

        demo_data = _boolean(env.get('COLLEAGUE_ENABLE_DEMO_DATA'), profile in ('demo', 'fact_check'))
        if profile == 'fact_check' and not demo_data:
            raise ValueError('The fact_check profile requires COLLEAGUE_ENABLE_DEMO_DATA=1')
        return cls(name, profile, model, demo_data)
