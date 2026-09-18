import json
import shutil
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path

from artifact_store import ArtifactStore
from command_runner import run_command
from providers.base import ProviderRequest
from test_schemas import permissions_payload
from workspace_actions import WorkspaceActionPlan, build_plan
from workspace_executor import WorkspaceExecutor, extract_plan_payload
from workspace_isolation import IsolationError, contained_path


GIT = shutil.which('git')


def init_repo(path):
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(['git', 'init'], cwd=path, check=True, capture_output=True)
    subprocess.run(['git', 'config', 'user.email', 'dev@example.com'], cwd=path, check=True, capture_output=True)
    subprocess.run(['git', 'config', 'user.name', 'Dev'], cwd=path, check=True, capture_output=True)
    (path / '.gitignore').write_text('.env\n')
    (path / 'README.md').write_text('hello\n')
    (path / 'src.py').write_text('value = 1\n')
    subprocess.run(['git', 'add', '.gitignore', 'README.md', 'src.py'], cwd=path, check=True, capture_output=True)
    subprocess.run(['git', 'commit', '-m', 'init'], cwd=path, check=True, capture_output=True)
    (path / '.env').write_text('SECRET=1\n')
    (path / 'untracked.py').write_text('scratch = True\n')
    (path / 'link.py').symlink_to(path / 'src.py')
    return path


class PlanSchemaTests(unittest.TestCase):
    def test_rejects_commits_absolute_paths_and_raw_shell(self):
        with self.assertRaises(ValueError):
            WorkspaceActionPlan.from_dict({
                'id': 'plan-1', 'meetingId': 'mtg-abc123', 'delegationId': 'item_1',
                'summary': 'Commit the change', 'categories': ['commits'], 'files': [],
            })
        with self.assertRaises(ValueError):
            build_plan(
                plan_id='plan-2', meeting_id='mtg-abc123', delegation_id='item_1',
                summary='Edit a file', categories=['edits'],
                files=[{'path': '/etc/passwd'}],
            )
        with self.assertRaises(ValueError):
            build_plan(
                plan_id='plan-3', meeting_id='mtg-abc123', delegation_id='item_1',
                summary='Run a command', categories=['commands'],
                files=[], commands=[{'argv': ['bash', '-c', 'rm -rf /']}],
            )
        with self.assertRaises(ValueError):
            build_plan(
                plan_id='plan-4', meeting_id='mtg-abc123', delegation_id='item_1',
                summary='Edit a file', categories=['edits'],
                files=[{'path': '../secret'}],
            )
        with self.assertRaises(ValueError):
            build_plan(
                plan_id='plan-5', meeting_id='mtg-abc123', delegation_id='item_1',
                summary='Commit the change', categories=['commands'],
                files=[], commands=[{'argv': ['git', 'commit', '-am', 'x']}],
            )

    def test_extracts_json_from_untrusted_model_text(self):
        payload = extract_plan_payload('Here you go:\n{"categories":["edits"],"summary":"Update the helper","files":[]}\n')
        self.assertEqual(payload['categories'], ['edits'])


class ExecutorHarness:
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.workspace = Path(self.temporary.name) / 'workspace'
        self.workspace.mkdir()
        (self.workspace / 'src.py').write_text('value = 1\n')
        self.artifacts = ArtifactStore(Path(self.temporary.name) / '.colleague' / 'artifacts')

    def _plan(self, **overrides):
        payload = dict(
            plan_id='plan-exec1', meeting_id='mtg-abc123', delegation_id='item_lock_1',
            summary='Update the helper', categories=['edits'],
            files=[{'path': 'src.py'}],
        )
        payload.update(overrides)
        return build_plan(**payload)

    def _request(self, **overrides):
        payload = dict(
            delegation_id='item_lock_1',
            request_text='Please update the helper.',
            permissions=permissions_payload(workspace='workspace-write', edits='approval-required'),
            workspace=str(self.workspace),
            meeting_id='mtg-abc123',
        )
        payload.update(overrides)
        return ProviderRequest(**payload)


class ExecutorPolicyTests(ExecutorHarness, unittest.IsolatedAsyncioTestCase):
    async def test_non_git_edits_are_unsupported(self):
        executor = WorkspaceExecutor(
            artifacts=self.artifacts,
            approval_gate=lambda payload: {'status': 'approved'},
            mutate=lambda *_args: None,
        )
        result = await executor.execute(self._request(), self._plan())
        self.assertEqual(result['status'], 'unsupported')

    async def test_disabled_edits_deny_before_mutate_and_commands_use_argv(self):
        called = []

        async def mutate(*_args):
            called.append(True)

        executor = WorkspaceExecutor(
            artifacts=self.artifacts,
            approval_gate=lambda payload: {'status': 'approved'},
            mutate=mutate,
        )
        denied = await executor.execute(
            self._request(permissions=permissions_payload(edits='disabled')),
            self._plan())
        self.assertEqual(denied['status'], 'denied')
        self.assertEqual(called, [])
        ran = await executor.execute(
            self._request(permissions=permissions_payload(
                commands='allowed', edits='disabled', network='allowed')),
            self._plan(categories=['commands'], files=[], commands=[{'argv': ['python3', '-c', 'print(1)']}]),
        )
        self.assertEqual(ran['status'], 'completed')
        self.assertEqual(ran['commands'][0]['exitCode'], 0)

    async def test_cancel_and_policy_grant_do_not_fabricate_user_approval(self):
        cancel = threading.Event()
        cancel.set()
        executor = WorkspaceExecutor(
            artifacts=self.artifacts,
            approval_gate=lambda payload: {'status': 'approved'},
            mutate=lambda *_args: None,
        )
        result = await executor.execute(self._request(), self._plan(), cancel)
        self.assertEqual(result['status'], 'cancelled')
        granted = await executor.authorize(
            permissions_payload(edits='allowed', workspace='workspace-write'),
            'edits', 'Update the helper', {}, self._request())
        self.assertEqual(granted['source'], 'policy')
        self.assertEqual(granted['status'], 'approved')

    def test_timeout_kills_the_process_group(self):
        with self.assertRaises(IsolationError):
            run_command(
                ['python3', '-c', 'import time; time.sleep(5)'],
                cwd=None, workspace=self.workspace, timeout=0.3, network_allowed=True)


@unittest.skipUnless(GIT, 'git is required for isolated workspace mutation tests')
class IsolationAndExecutorTests(ExecutorHarness, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.workspace = init_repo(Path(self.temporary.name) / 'repo')
        self.artifacts = ArtifactStore(Path(self.temporary.name) / '.colleague' / 'artifacts')

    async def test_applies_isolated_edit_without_seeding_secrets_or_following_symlinks(self):
        async def mutate(isolated, plan, request, cancel):
            (isolated / 'src.py').write_text('value = 2\n')
            self.assertFalse((isolated / '.env').exists())
            self.assertFalse((isolated / 'link.py').exists())

        events = []
        executor = WorkspaceExecutor(
            artifacts=self.artifacts,
            approval_gate=lambda payload: {'status': 'approved'},
            mutate=mutate,
            emit=lambda meeting_id, event_type, **payload: events.append((event_type, payload)),
        )
        with self.assertRaises(IsolationError):
            contained_path(self.workspace, 'link.py')
        result = await executor.execute(self._request(), self._plan())
        self.assertEqual(result['status'], 'completed')
        self.assertEqual((self.workspace / 'src.py').read_text(), 'value = 2\n')
        self.assertTrue(any(kind == 'workspace.action.completed' for kind, _payload in events))
        self.assertTrue(any(kind == 'artifact.created' for kind, _payload in events))
        listed = self.artifacts.list('mtg-abc123')
        self.assertTrue(any(item['kind'] == 'patch' for item in listed))
        dumped = json.dumps(result)
        self.assertNotIn('SECRET=1', dumped)

    def test_secret_like_untracked_files_are_not_seeded(self):
        from workspace_isolation import create_isolated_workspace, remove_isolated_workspace
        (self.workspace / 'api_keys.txt').write_text('openai=not-a-real-key\n')
        dest = Path(self.temporary.name) / 'isolated-secret'
        create_isolated_workspace(self.workspace, dest)
        self.addCleanup(lambda: remove_isolated_workspace(self.workspace, dest))
        self.assertFalse((dest / 'api_keys.txt').exists())
        self.assertTrue((dest / 'untracked.py').exists())

    async def test_conflict_preserves_user_edits_and_patch_artifact(self):
        async def mutate(isolated, plan, request, cancel):
            (isolated / 'src.py').write_text('value = 9\n')

        executor = WorkspaceExecutor(
            artifacts=self.artifacts,
            approval_gate=lambda payload: {'status': 'approved'},
            mutate=mutate,
        )
        (self.workspace / 'src.py').write_text('value = 7\n')
        result = await executor.execute(self._request(), self._plan())
        self.assertEqual(result['status'], 'conflict')
        self.assertEqual((self.workspace / 'src.py').read_text(), 'value = 7\n')
        self.assertTrue(any(item['kind'] == 'patch' for item in self.artifacts.list('mtg-abc123')))

    async def test_missing_mutate_is_unsupported(self):
        executor = WorkspaceExecutor(
            artifacts=self.artifacts,
            approval_gate=lambda payload: {'status': 'approved'},
        )
        missing = await executor.execute(self._request(), self._plan())
        self.assertEqual(missing['status'], 'unsupported')


class ArtifactStoreTests(unittest.TestCase):
    def test_rejects_path_escape_and_secret_bodies(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        store = ArtifactStore(Path(temporary.name) / 'artifacts')
        stored = store.put('mtg-abc123', kind='plan', body={'summary': 'Update the helper'})
        meta, data = store.read_body('mtg-abc123', stored['id'])
        self.assertEqual(meta['id'], stored['id'])
        self.assertIn(b'Update the helper', data)
        with self.assertRaises(ValueError):
            store.put('mtg-abc123', kind='plan', body={'apiKey': 'secret'})
        with self.assertRaises(ValueError):
            store.get('mtg-abc123', '../secret')


if __name__ == '__main__':
    unittest.main()
