"""Allowlisted argv command runner with env scrubbing and process-group cleanup."""
import os
import re
import signal
import subprocess
import time
from pathlib import Path

from workspace_isolation import IsolationError, contained_path, denied_relative


ALLOWED_COMMANDS = frozenset({
    'python', 'python3', 'pytest', 'node', 'npm', 'npx', 'go', 'cargo', 'ruff',
    'make', 'git',
})
FORBIDDEN_GIT = frozenset({
    'commit', 'push', 'merge', 'rebase', 'cherry-pick', 'reset', 'tag', 'stash',
    'submodule', 'filter-branch', 'update-ref',
})
ENV_ALLOWLIST = frozenset({
    'PATH', 'HOME', 'LANG', 'LC_ALL', 'LC_CTYPE', 'TMPDIR', 'TMP', 'TEMP', 'TERM',
    'USER', 'LOGNAME', 'TZ',
})
NETWORK_ENV = frozenset({
    'HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'http_proxy', 'https_proxy', 'all_proxy',
    'NO_PROXY', 'no_proxy',
})
MAX_OUTPUT = 32_000
DEFAULT_TIMEOUT = 60
SECRET_RE = re.compile(
    r'(sk-[A-Za-z0-9_-]{8,}|Bearer\s+\S+|api[_-]?key\s*[:=]\s*\S+)',
    re.IGNORECASE,
)


def _scrub_env(network_allowed):
    cleaned = {}
    for key, value in os.environ.items():
        if key in NETWORK_ENV:
            continue
        if key in ENV_ALLOWLIST:
            cleaned[key] = value
    if not network_allowed:
        cleaned['NO_PROXY'] = '*'
        cleaned['no_proxy'] = '*'
    return cleaned


def validate_command(argv, *, network_allowed=False):
    if not argv or not isinstance(argv, list):
        raise IsolationError('command argv is required')
    name = Path(str(argv[0])).name
    if name != argv[0]:
        raise IsolationError('command must be a basename')
    if name not in ALLOWED_COMMANDS:
        raise IsolationError('command is not allowlisted')
    if name == 'git':
        sub = argv[1] if len(argv) > 1 else ''
        if sub in FORBIDDEN_GIT or sub in ('commit', 'push'):
            raise IsolationError('git commit and push are disabled')
    if name in ('curl', 'wget', 'nc', 'ssh') and not network_allowed:
        raise IsolationError('network commands are disabled')
    return name


def run_command(argv, *, cwd, workspace, timeout=DEFAULT_TIMEOUT, network_allowed=False,
                cancel=None):
    name = validate_command(argv, network_allowed=network_allowed)
    root = Path(workspace).resolve()
    workdir = root if not cwd else contained_path(root, cwd)
    if not workdir.is_dir():
        raise IsolationError('command cwd is not a directory')
    if denied_relative(str(workdir.relative_to(root)) if workdir != root else '.'):
        raise IsolationError('command cwd is denied')
    env = _scrub_env(network_allowed)
    start = time.monotonic()
    process = subprocess.Popen(
        [name, *argv[1:]],
        cwd=str(workdir),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=True,
        text=True,
    )
    try:
        deadline = start + float(timeout or DEFAULT_TIMEOUT)
        while True:
            if cancel is not None and getattr(cancel, 'is_set', lambda: False)():
                raise IsolationError('cancelled')
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise IsolationError('command timed out')
            try:
                output, _ignored = process.communicate(timeout=min(0.2, remaining))
                break
            except subprocess.TimeoutExpired:
                continue
    except IsolationError:
        _kill_group(process)
        output = ''
        try:
            output, _ignored = process.communicate(timeout=1)
        except Exception:
            pass
        raise
    duration = int((time.monotonic() - start) * 1000)
    text = SECRET_RE.sub('[redacted]', output or '')
    truncated = len(text) > MAX_OUTPUT
    if truncated:
        text = text[:MAX_OUTPUT]
    return {
        'name': name,
        'argv0': name,
        'exitCode': process.returncode,
        'durationMs': duration,
        'output': text,
        'truncated': truncated,
    }


def _kill_group(process):
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except OSError:
        try:
            process.kill()
        except OSError:
            pass
    try:
        process.wait(timeout=2)
    except Exception:
        try:
            process.kill()
            process.wait(timeout=1)
        except Exception:
            pass
