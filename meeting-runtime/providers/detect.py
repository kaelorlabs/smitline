"""Feature-detect coding-agent CLIs from local --help/--version text only."""
import os
import shutil
import subprocess


RESUME_TOKENS = ('--resume', '--session-id', '--session', '--conversation-id')
PRINT_TOKENS = ('--print', '--output-format')
JSON_TOKENS = ('--output-format', '--json', 'stream-json')
MODEL_TOKENS = ('--model',)
AUTH_STATUS_TOKENS = ('login status', 'auth status', 'whoami')
SANDBOX_TOKENS = ('--sandbox', '--permission-mode')


def collect_help(command, runner=None):
    if not command:
        return ''
    run = runner or subprocess.run
    chunks = []
    for args in ([command, '--help'], [command, 'exec', '--help'], [command, '--version']):
        try:
            result = run(args, capture_output=True, text=True, timeout=4, check=False)
        except (OSError, subprocess.TimeoutExpired):
            continue
        chunks.append((result.stdout or '') + '\n' + (result.stderr or ''))
    return '\n'.join(chunks)


def _first_token(help_text, tokens):
    lowered = str(help_text or '').lower()
    for token in tokens:
        if token.lower() in lowered:
            return token
    return None


def detect_cli_flags(help_text):
    return {
        'resume': _first_token(help_text, RESUME_TOKENS),
        'print': _first_token(help_text, PRINT_TOKENS),
        'json': _first_token(help_text, JSON_TOKENS),
        'model': _first_token(help_text, MODEL_TOKENS),
        'sandbox': _first_token(help_text, SANDBOX_TOKENS),
        'authStatus': _first_token(help_text, AUTH_STATUS_TOKENS),
    }


def which_binary(names, env_key=None):
    if env_key:
        configured = os.environ.get(env_key)
        if configured:
            return configured
    for name in names:
        found = shutil.which(name)
        if found:
            return found
    return None
