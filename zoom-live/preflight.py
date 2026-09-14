"""Validate local meeting configuration without exposing secret values."""
from pathlib import Path
import re
import sys
from urllib.parse import urlparse

from runtime_config import RuntimeConfig


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
    meeting = read_env(root / '.env.zoom')
    merged = {**general, **meeting}
    errors = []

    for key in ('OPENAI_API_KEY', 'TAVILY_API_KEY', 'ZOOM_MEETING_URL'):
        value = merged.get(key, '')
        if not value or any(marker in value.lower() for marker in ('replace_with', 'your_meeting', 'your_')):
            errors.append(f'{key} is missing or still a placeholder')

    url = merged.get('ZOOM_MEETING_URL', '')
    parsed = urlparse(url)
    if parsed.scheme != 'https' or not re.fullmatch(r'(?:[a-z0-9-]+\.)?zoom\.us', parsed.hostname or ''):
        errors.append('ZOOM_MEETING_URL must be an HTTPS zoom.us meeting URL')
    if not re.search(r'/j/\d+|/wc/(?:join/)?\d+', parsed.path):
        errors.append('ZOOM_MEETING_URL does not contain a recognized meeting ID')

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
        config = validate(Path(__file__).resolve().parent.parent)
    except (OSError, ValueError) as exc:
        print(f'Configuration check failed:\n{exc}', file=sys.stderr)
        raise SystemExit(2)
    print(f'Configuration OK: profile={config.profile}, participant={config.participant_name}, '
          f'codex_model={config.default_codex_model}, demo_data={str(config.demo_data_enabled).lower()}')
