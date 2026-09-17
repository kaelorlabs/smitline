import hashlib
import os
import shutil
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from git_actions import CommitRequest, GitActionResult, GitOperation, PushRequest
from git_broker import GitBroker
from workspace_isolation import IsolationError


HEAD = 'a' * 40
DIGEST = 'b' * 64
HAS_GIT = shutil.which('git') is not None


def commit_payload(**overrides):
    payload = {
        'version': 1,
        'id': 'cmt-1',
        'meetingId': 'mtg-abc123',
        'expectedHead': HEAD,
        'message': 'Record reviewed helper changes',
        'files': [{'path': 'helper.py', 'sha256': DIGEST}],
    }
    payload.update(overrides)
    return payload


def push_payload(**overrides):
    payload = {
        'version': 1,
        'id': 'psh-1',
        'meetingId': 'mtg-abc123',
        'commitSha': HEAD,
        'remote': 'origin',
        'branch': 'colleague-work',
    }
    payload.update(overrides)
    return payload


class GitActionSchemaTests(unittest.TestCase):
    def test_commit_and_push_round_trip_and_refusals(self):
        parsed = CommitRequest.from_dict(commit_payload())
        self.assertEqual(parsed.to_dict()['expectedHead'], HEAD)
        self.assertEqual(CommitRequest.from_dict(parsed.to_dict()).to_dict(), parsed.to_dict())
        with self.assertRaises(ValueError):
            CommitRequest.from_dict(commit_payload(files=[]))
        with self.assertRaises(ValueError):
            CommitRequest.from_dict(commit_payload(argv=['git', 'commit']))
        with self.assertRaises(ValueError):
            CommitRequest.from_dict(commit_payload(message='git commit the helper'))
        with self.assertRaises(ValueError):
            CommitRequest.from_dict(commit_payload(files=[{'path': '../secret', 'sha256': DIGEST}]))
        with self.assertRaises(ValueError):
            CommitRequest.from_dict(commit_payload(expectedHead='HEAD'))
        parsed_push = PushRequest.from_dict(push_payload())
        self.assertEqual(parsed_push.to_dict()['remote'], 'origin')
        with self.assertRaises(ValueError):
            PushRequest.from_dict(push_payload(remote='https://example.com/repo.git'))
        with self.assertRaises(ValueError):
            PushRequest.from_dict(push_payload(remote='git@github.com:org/repo.git'))
        with self.assertRaises(ValueError):
            PushRequest.from_dict(push_payload(branch='main'))
        with self.assertRaises(ValueError):
            PushRequest.from_dict(push_payload(branch='HEAD'))
        with self.assertRaises(ValueError):
            PushRequest.from_dict(push_payload(branch='refs/heads/x'))
        with self.assertRaises(ValueError):
            PushRequest.from_dict(push_payload(force=True))
        with self.assertRaises(ValueError):
            PushRequest.from_dict(push_payload(refspec='+:refs/heads/x'))
        result = GitActionResult.from_dict({
            'version': 1, 'operationId': 'cmt-1', 'meetingId': 'mtg-abc123',
            'kind': 'commit', 'status': 'completed', 'summary': 'Recorded 1 reviewed file',
            'commitSha': 'c' * 40, 'parentSha': HEAD, 'treeSha': 'd' * 40,
        })
        self.assertNotIn('url', result.public_dict())
        dumped = str(result.public_dict())
        self.assertNotIn('https://', dumped)

    def test_operation_persists_without_urls(self):
        payload = GitOperation.from_dict({
            'id': 'cmt-1', 'kind': 'commit', 'meetingId': 'mtg-abc123',
            'status': 'requested', 'request': commit_payload(),
            'createdAt': '2026-09-16T17:00:00Z', 'updatedAt': '2026-09-16T17:00:00Z',
        }).public_dict()
        self.assertEqual(payload['approvalId'] if 'approvalId' in payload else None, None)
        self.assertNotIn('https://', str(payload))


def _run(workspace, args):
    import subprocess
    completed = subprocess.run(
        ['git', '-C', str(workspace), *args], check=True, capture_output=True, text=True)
    return completed.stdout.strip()


def _init_repo(directory):
    import subprocess
    subprocess.run(['git', 'init', str(directory)], check=True, capture_output=True, text=True)
    _run(directory, ['config', 'user.name', 'Tester'])
    _run(directory, ['config', 'user.email', 'tester@example.com'])
    Path(directory, 'README.md').write_text('start\n', encoding='utf-8')
    _run(directory, ['add', '--', 'README.md'])
    _run(directory, ['commit', '-m', 'init'])
    _run(directory, ['branch', '-M', 'colleague-work'])
    return _run(directory, ['rev-parse', 'HEAD'])


@unittest.skipUnless(HAS_GIT, 'git is required')
class GitBrokerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / 'repo'
        self.root.mkdir()
        self.head = _init_repo(self.root)
        self.broker = GitBroker()

    def tearDown(self):
        self.temporary.cleanup()

    def _digest(self, relative):
        return hashlib.sha256((self.root / relative).read_bytes()).hexdigest()

    def _commit_request(self, relative='helper.py', **overrides):
        (self.root / relative).write_text('print("ok")\n', encoding='utf-8')
        payload = commit_payload(
            expectedHead=self.head,
            files=[{'path': relative, 'sha256': self._digest(relative)}],
            **overrides,
        )
        return CommitRequest.from_dict(payload)

    def test_commit_exact_paths_skips_hooks_and_signing(self):
        hook = self.root / '.git' / 'hooks' / 'pre-commit'
        hook.write_text('#!/bin/sh\nexit 1\n', encoding='utf-8')
        os.chmod(hook, 0o755)
        _run(self.root, ['config', 'commit.gpgsign', 'true'])
        (self.root / 'other.py').write_text('skip\n', encoding='utf-8')
        request = self._commit_request()
        outcome = self.broker.commit(request, workspace=self.root)
        self.assertEqual(len(outcome['commitSha']), 40)
        self.assertEqual(outcome['parentSha'], self.head)
        names = _run(self.root, ['diff-tree', '--no-commit-id', '--name-only', '-r', 'HEAD'])
        self.assertEqual(names.splitlines(), ['helper.py'])
        self.assertTrue((self.root / 'other.py').exists())
        self.assertNotEqual(_run(self.root, ['rev-parse', 'HEAD']), self.head)

    def test_expected_head_staged_secret_and_hash_conflicts(self):
        request = self._commit_request()
        with self.assertRaises(IsolationError):
            self.broker.commit(
                CommitRequest.from_dict(commit_payload(
                    expectedHead='e' * 40,
                    files=request.to_dict()['files'])),
                workspace=self.root)
        self.assertEqual(_run(self.root, ['rev-parse', 'HEAD']), self.head)
        extra = self.root / 'staged.py'
        extra.write_text('nope\n', encoding='utf-8')
        _run(self.root, ['add', '--', 'staged.py'])
        with self.assertRaises(IsolationError):
            self.broker.commit(request, workspace=self.root)
        _run(self.root, ['reset', '--mixed', 'HEAD'])
        ignored = self.root / '.env'
        ignored.write_text('SECRET=1\n', encoding='utf-8')
        Path(self.root / '.gitignore').write_text('.env\n', encoding='utf-8')
        with self.assertRaises(IsolationError):
            self.broker.commit(
                CommitRequest.from_dict(commit_payload(
                    expectedHead=self.head,
                    files=[{'path': '.env', 'sha256': hashlib.sha256(ignored.read_bytes()).hexdigest()}])),
                workspace=self.root)
        helper = self.root / 'helper.py'
        helper.write_text('print("ok")\n', encoding='utf-8')
        with self.assertRaises(IsolationError):
            self.broker.commit(
                CommitRequest.from_dict(commit_payload(
                    expectedHead=self.head,
                    files=[{'path': 'helper.py', 'sha256': 'f' * 64}])),
                workspace=self.root)
        self.assertEqual(_run(self.root, ['rev-parse', 'HEAD']), self.head)

    def test_push_to_local_bare_remote_and_refusals(self):
        request = self._commit_request()
        outcome = self.broker.commit(request, workspace=self.root)
        bare = Path(self.temporary.name) / 'remote.git'
        import subprocess
        subprocess.run(['git', 'init', '--bare', str(bare)],
                       check=True, capture_output=True, text=True)
        _run(self.root, ['remote', 'add', 'origin', str(bare)])
        pushed = self.broker.push(
            PushRequest.from_dict(push_payload(commitSha=outcome['commitSha'])),
            workspace=self.root)
        self.assertEqual(pushed['remote'], 'origin')
        self.assertNotIn('http', str(pushed))
        self.assertNotIn(str(bare), str(pushed))
        remote_tip = subprocess.run(
            ['git', '--git-dir', str(bare), 'rev-parse', 'refs/heads/colleague-work'],
            check=True, capture_output=True, text=True).stdout.strip()
        self.assertEqual(remote_tip, outcome['commitSha'])
        protected = GitBroker(protected_branches={'colleague-work'})
        with self.assertRaises(IsolationError):
            protected.push(
                PushRequest.from_dict(push_payload(commitSha=outcome['commitSha'])),
                workspace=self.root)
        with self.assertRaises(IsolationError):
            self.broker.push(
                PushRequest.from_dict(push_payload(
                    commitSha=outcome['commitSha'], remote='missing')),
                workspace=self.root)
        self.assertEqual(_run(self.root, ['rev-parse', 'HEAD']), outcome['commitSha'])

    def test_push_failure_does_not_roll_back_commit(self):
        request = self._commit_request()
        outcome = self.broker.commit(request, workspace=self.root)
        with self.assertRaises(IsolationError):
            self.broker.push(
                PushRequest.from_dict(push_payload(commitSha=outcome['commitSha'])),
                workspace=self.root)
        self.assertEqual(_run(self.root, ['rev-parse', 'HEAD']), outcome['commitSha'])

    def test_timeout_cancel_and_redaction(self):
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(IsolationError):
            self.broker.commit(self._commit_request(), workspace=self.root, cancel=cancel)
        wrapper_dir = Path(self.temporary.name) / 'bin'
        wrapper_dir.mkdir()
        real = shutil.which('git')
        wrapper = wrapper_dir / 'git'
        wrapper.write_text(
            '#!/bin/sh\n'
            'for arg in "$@"; do\n'
            '  if [ "$arg" = "commit" ]; then\n'
            '    echo Bearer sk-testtoken123 >&2\n'
            '    sleep 8\n'
            '    break\n'
            '  fi\n'
            'done\n'
            'exec %s "$@"\n' % real,
            encoding='utf-8',
        )
        os.chmod(wrapper, 0o755)
        env_path = str(wrapper_dir) + os.pathsep + os.environ.get('PATH', '')
        with mock.patch.dict(os.environ, {'PATH': env_path}):
            timed = GitBroker(timeout=0.3)
            with self.assertRaises(IsolationError) as raised:
                timed.commit(self._commit_request(), workspace=self.root)
        self.assertIn('timed out', str(raised.exception))
        failing = GitBroker()
        wrapper.write_text(
            '#!/bin/sh\n'
            'for arg in "$@"; do\n'
            '  if [ "$arg" = "commit" ]; then\n'
            '    echo Bearer sk-testtoken123 >&2\n'
            '    exit 1\n'
            '  fi\n'
            'done\n'
            'exec %s "$@"\n' % real,
            encoding='utf-8',
        )
        with mock.patch.dict(os.environ, {'PATH': env_path}):
            with self.assertRaises(IsolationError) as redacted:
                failing.commit(self._commit_request(), workspace=self.root)
        self.assertIn('[redacted]', str(redacted.exception))
        self.assertNotIn('sk-testtoken123', str(redacted.exception))


if __name__ == '__main__':
    unittest.main()
