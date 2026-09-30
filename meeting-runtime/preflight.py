"""Validate local meeting configuration without exposing secret values."""
import os
from pathlib import Path
import re
import sys
from urllib.parse import urlparse

from runtime_config import RuntimeConfig
from meeting_urls import platform_for_url


def read_env(path):
    values = {}
    for number, raw in enumerate(Path(path).read_text().splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith('#'):
            continue
        if '=' not in line:
            raise ValueError(f'{path}:{number} is not a KEY=VALUE setting')
        key, value = line.split('=', 1)
        key = key.strip()
        if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', key):
            raise ValueError(f'{path}:{number} has an invalid setting name')
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
            value = value[1:-1]
        values[key] = value
    return values


def validate(root):
    root = Path(root)
    general = read_env(root / '.env')
    meeting = read_env(root / '.env.meeting')
    merged = {**general, **meeting}
    errors = []

    for key in ('OPENAI_API_KEY', 'MEETING_URL'):
        value = merged.get(key, '')
        if not value or any(marker in value.lower() for marker in ('replace_with', 'your_meeting', 'your_')):
            errors.append(f'{key} is missing or still a placeholder')

    url = merged.get('MEETING_URL', '')
    try:
        platform_for_url(url)
    except ValueError as exc:
        errors.append(str(exc))

    try:
        runtime = RuntimeConfig.from_environ(merged)
    except ValueError as exc:
        errors.append(str(exc))
        runtime = None
    if errors:
        raise ValueError('\n'.join(f'- {error}' for error in errors))
    return runtime


if __name__ == '__main__':
    try:
        config = validate(os.environ.get('COLLEAGUE_ROOT') or Path(__file__).resolve().parent.parent)
    except (OSError, ValueError) as exc:
        print(f'Configuration check failed:\n{exc}', file=sys.stderr)
        raise SystemExit(2)
    print(f'Configuration OK: participant={config.participant_name}, '
          f'backend_model={config.backend_model}, web_search={str(config.web_search).lower()}')
