"""Host-only Git commit/push broker. Never runs raw argv, force, tags, or URL remotes."""
import os
import signal
import subprocess
import time

from pathlib import Path
from command_runner import SECRET_RE
from git_actions import PROTECTED_BRANCHES, build_result
from workspace_isolation import (
    IsolationError, contained_path, denied_relative, file_digest, is_git_workspace,
)


MAX_OUTPUT = 8_000
DEFAULT_TIMEOUT = 30
GIT_ENV_ALLOWLIST = frozenset({
    'PATH', 'HOME', 'LANG', 'LC_ALL', 'LC_CTYPE', 'TMPDIR', 'TMP', 'TEMP', 'TZ',
    'USER', 'LOGNAME',
})


class _GitOutput:
    def __init__(self, returncode, stdout, stderr):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _env():
    cleaned = {}
    for key, value in os.environ.items():
        if key in GIT_ENV_ALLOWLIST:
            cleaned[key] = value
    cleaned['GIT_TERMINAL_PROMPT'] = '0'
    cleaned['GIT_OPTIONAL_LOCKS'] = '0'
    cleaned['GC_AUTO'] = '0'
    return cleaned


def _kill_group(process):
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            process.kill()
        except (ProcessLookupError, PermissionError, OSError):
            return
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        pass


def _run_git(workspace, args, *, check=True, timeout=DEFAULT_TIMEOUT, cancel=None):
    argv = ['git', '-C', str(workspace), '-c', 'core.hooksPath=/dev/null',
            '-c', 'commit.gpgsign=false', *args]
    try:
        process = subprocess.Popen(
            argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, env=_env(), start_new_session=True,
        )
    except FileNotFoundError as error:
        raise IsolationError('git is not available') from error
    deadline = time.monotonic() + timeout
    stdout_chunks = []
    stderr_chunks = []
    while process.poll() is None:
        if cancel is not None and cancel.is_set():
            _kill_group(process)
            try:
                process.communicate(timeout=1)
            except Exception:
                pass
            raise IsolationError('cancelled')
        if time.monotonic() > deadline:
            _kill_group(process)
            try:
                process.communicate(timeout=1)
            except Exception:
                pass
            raise IsolationError('git command timed out')
        time.sleep(0.05)
    stdout, stderr = process.communicate()
    stdout_chunks.append(stdout or '')
    stderr_chunks.append(stderr or '')
    output = SECRET_RE.sub('[redacted]', ''.join(stdout_chunks) + ''.join(stderr_chunks))
    if len(output) > MAX_OUTPUT:
        output = output[:MAX_OUTPUT]
    completed = _GitOutput(process.returncode, stdout or '', stderr or '')
    if check and completed.returncode != 0:
        raise IsolationError(output.strip()[:240] or 'git command failed')
    return completed, output


class GitBroker:
    def __init__(self, *, artifacts=None, protected_branches=None, timeout=DEFAULT_TIMEOUT):
        self.artifacts = artifacts
        self.protected_branches = frozenset(protected_branches or PROTECTED_BRANCHES)
        self.timeout = timeout
        self._remote_pins = {}

    def _git(self, workspace, args, **kwargs):
        kwargs.setdefault('timeout', self.timeout)
        return _run_git(workspace, args, **kwargs)

    def _head(self, workspace):
        completed, _output = self._git(workspace, ['rev-parse', 'HEAD'])
        return completed.stdout.strip()

    def _config(self, workspace, key):
        completed, _output = self._git(workspace, ['config', '--get', key], check=False)
        return (completed.stdout or '').strip()

    def _ignored(self, workspace, relative):
        completed, _output = self._git(
            workspace, ['check-ignore', '-q', '--', relative], check=False)
        return completed.returncode == 0

    def _remote_urls(self, workspace):
        completed, _output = self._git(workspace, ['remote', '-v'], check=False)
        urls = {}
        for line in (completed.stdout or '').splitlines():
            parts = line.split()
            if len(parts) < 2:
                continue
            name, url = parts[0], parts[1]
            if '(push)' in line or name not in urls:
                urls[name] = url
        return urls

    def _push_url(self, workspace, remote):
        completed, _output = self._git(
            workspace, ['remote', 'get-url', '--push', remote], check=False)
        url = (completed.stdout or '').strip()
        if completed.returncode == 0 and url:
            return url
        completed, _output = self._git(
            workspace, ['remote', 'get-url', remote], check=False)
        return (completed.stdout or '').strip()

    def _pin_remotes(self, workspace):
        key = str(Path(workspace).resolve())
        pinned = dict(self._remote_pins.get(key) or {})
        pinned.update(self._remote_urls(workspace))
        self._remote_pins[key] = pinned

    def _require_pinned_remote(self, workspace, remote):
        key = str(Path(workspace).resolve())
        current = self._push_url(workspace, remote)
        if not current:
            raise IsolationError('remote is not configured')
        pinned = (self._remote_pins.get(key) or {}).get(remote)
        if pinned and pinned != current:
            raise IsolationError('remote URL changed after approval')
        self._remote_pins.setdefault(key, {})[remote] = current
        return current

    def _mode(self, workspace, relative):
        completed, _output = self._git(
            workspace, ['ls-files', '-s', '--', relative], check=False)
        line = (completed.stdout or '').strip()
        if not line:
            return None
        return line.split(' ', 1)[0]

    def commit(self, request, *, workspace, cancel=None):
        payload = request.to_dict() if hasattr(request, 'to_dict') else dict(request)
        root = workspace
        if not is_git_workspace(root):
            raise IsolationError('commits require a Git workspace')
        if cancel is not None and cancel.is_set():
            raise IsolationError('cancelled')
        expected = payload['expectedHead']
        head = self._head(root)
        if head != expected:
            raise IsolationError('working tree HEAD does not match expectedHead')
        if not self._config(root, 'user.name') or not self._config(root, 'user.email'):
            raise IsolationError('git user identity is not configured')
        cached, _output = self._git(root, ['diff', '--cached', '--name-only', '-z'])
        if cached.stdout:
            raise IsolationError('index has unrelated staged changes')
        relatives = []
        for spec in payload['files']:
            relative = spec['path']
            path = contained_path(root, relative)
            if denied_relative(relative):
                raise IsolationError('path is denied')
            if self._ignored(root, relative):
                raise IsolationError('ignored files cannot be committed')
            if self._mode(root, relative) == '160000':
                raise IsolationError('submodules cannot be committed')
            digest = file_digest(path)
            if digest != spec['sha256']:
                raise IsolationError('file hash does not match the approved manifest')
            relatives.append(relative)
        try:
            for relative in relatives:
                self._git(root, ['add', '--', relative], cancel=cancel)
            staged, _output = self._git(root, ['diff', '--cached', '--name-only'])
            staged_names = [line.strip() for line in staged.stdout.splitlines() if line.strip()]
            if set(staged_names) != set(relatives):
                raise IsolationError('staged files do not match the approved manifest')
            self._git(root, [
                'commit', '--no-verify', '--no-gpg-sign', '-m', payload['message'],
                '--', *relatives,
            ], cancel=cancel)
        except IsolationError:
            self._git(root, ['reset', '--mixed', 'HEAD'], check=False)
            raise
        sha = self._head(root)
        tree_completed, _output = self._git(root, ['rev-parse', 'HEAD^{tree}'])
        self._pin_remotes(root)
        return {
            'commitSha': sha,
            'parentSha': expected,
            'treeSha': tree_completed.stdout.strip(),
            'files': relatives,
        }

    def push(self, request, *, workspace, cancel=None):
        payload = request.to_dict() if hasattr(request, 'to_dict') else dict(request)
        root = workspace
        if not is_git_workspace(root):
            raise IsolationError('pushes require a Git workspace')
        if cancel is not None and cancel.is_set():
            raise IsolationError('cancelled')
        remote = payload['remote']
        branch = payload['branch']
        commit_sha = payload['commitSha']
        if branch in self.protected_branches or branch.split('/')[0] in self.protected_branches:
            raise IsolationError('protected branch')
        remotes_completed, _output = self._git(root, ['remote'])
        remotes = [line.strip() for line in remotes_completed.stdout.splitlines() if line.strip()]
        if remote not in remotes:
            raise IsolationError('remote is not configured')
        self._require_pinned_remote(root, remote)
        tip_completed, _output = self._git(
            root, ['rev-parse', '--verify', 'refs/heads/' + branch])
        tip = tip_completed.stdout.strip()
        if tip != commit_sha:
            raise IsolationError('commit is not the tip of the local branch')
        ancestor, _output = self._git(
            root, ['merge-base', '--is-ancestor', commit_sha, 'refs/heads/' + branch], check=False)
        if ancestor.returncode != 0:
            raise IsolationError('commit is not an ancestor of the local branch')
        refspec = 'refs/heads/' + branch + ':refs/heads/' + branch
        if refspec.startswith(':') or '+' in refspec or 'tag' in refspec.lower():
            raise IsolationError('refspec is not allowed')
        self._git(root, ['push', '--', remote, refspec], cancel=cancel)
        return {
            'commitSha': commit_sha,
            'remote': remote,
            'branch': branch,
        }


def result_from_commit(request, outcome, *, status, summary, artifact_ids=None, conflict=False):
    payload = request.to_dict() if hasattr(request, 'to_dict') else request
    return build_result(
        operation_id=payload['id'], meeting_id=payload['meetingId'], kind='commit',
        status=status, summary=summary, commit_sha=outcome.get('commitSha'),
        parent_sha=outcome.get('parentSha'), tree_sha=outcome.get('treeSha'),
        artifact_ids=artifact_ids, conflict=conflict,
    )


def result_from_push(request, outcome, *, status, summary, artifact_ids=None):
    payload = request.to_dict() if hasattr(request, 'to_dict') else request
    return build_result(
        operation_id=payload['id'], meeting_id=payload['meetingId'], kind='push',
        status=status, summary=summary, commit_sha=outcome.get('commitSha'),
        remote=outcome.get('remote'), branch=outcome.get('branch'),
        artifact_ids=artifact_ids,
    )
