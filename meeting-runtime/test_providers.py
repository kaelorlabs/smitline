"""Cursor and Claude Code provider capability detection and fail-closed execution."""
import json
import os
import tempfile
import unittest
from unittest import mock
from pathlib import Path

from providers.base import ProviderRequest
from providers.capabilities import ProviderCapabilities
from providers.claude_code import ClaudeCodeProvider
from providers.cursor import CursorProvider
from providers.detect import collect_help, detect_cli_flags
from providers.registry import ProviderRegistry
from session_continuity import EXACT, CONTEXT
from test_schemas import context_payload, permissions_payload


FULL_HELP = """
Usage: fake-agent [options]
  --print <prompt>          Noninteractive prompt
  --resume <session-id>     Resume an existing session
  --output-format json      Structured JSON output
  --model <name>            Model id
"""
PRINT_ONLY_HELP = """
Usage: fake-agent [options]
  --print <prompt>
  --model <name>
"""


class Result:
    def __init__(self, stdout='', stderr='', returncode=0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def agent_ref(provider='cursor', session_id='thread-origin-1', continuity=EXACT):
    return {
        'provider': provider,
        'sessionId': session_id,
        'workspace': '/tmp/colleague-workspace',
        'metadata': {'continuity': continuity, 'source': 'host'},
    }


class ProviderCapabilityTests(unittest.IsolatedAsyncioTestCase):
    def test_collect_help_includes_nested_resume_command(self):
        calls = []

        def runner(args, **_kwargs):
            calls.append(args)
            if args[1:] == ['exec', 'resume', '--help']:
                return Result('Usage: codex exec resume [SESSION_ID] [PROMPT]')
            return Result('Usage: codex')

        help_text = collect_help('/usr/bin/codex', runner)
        self.assertIn(['/usr/bin/codex', 'exec', 'resume', '--help'], calls)
        self.assertEqual(detect_cli_flags(help_text)['resume'], '--resume')

    def test_help_feature_detection_does_not_invent_flags(self):
        flags = detect_cli_flags(FULL_HELP)
        self.assertEqual(flags['resume'], '--resume')
        self.assertEqual(flags['print'], '--print')
        self.assertIsNone(detect_cli_flags('Usage: tool')['resume'])
        self.assertIsNone(detect_cli_flags(
            'Usage: tool --session NAME for logs\n--print\n')['resume'])

    async def test_exact_resume_requires_documented_flag(self):
        cursor = CursorProvider(command='/bin/echo', help_text=PRINT_ONLY_HELP)
        caps = cursor.capabilities()
        self.assertTrue(caps.contextContinuity)
        self.assertFalse(caps.exactSessionResume)
        self.assertEqual(caps.supportedModels, ())
        rejected = await cursor.validate_session(agent_ref())
        self.assertFalse(rejected['ok'])
        self.assertEqual(rejected['code'], 'exact_resume_unsupported')
        context_ok = await cursor.validate_session(agent_ref(continuity=CONTEXT, session_id='local-portal'))
        self.assertTrue(context_ok['ok'])

    async def test_forbidden_session_ids_are_rejected(self):
        cursor = CursorProvider(command='/bin/echo', help_text=FULL_HELP)
        for session_id in ('last', 'latest', '--last', ''):
            result = await cursor.validate_session(agent_ref(session_id=session_id))
            self.assertFalse(result['ok'], session_id)

    async def test_missing_binary_is_unavailable(self):
        cursor = CursorProvider(command=None, help_text=FULL_HELP)
        with mock.patch('providers.cli_provider.which_binary', return_value=None):
            cursor = CursorProvider(command=None, help_text=None)
            caps = cursor.capabilities()
        self.assertFalse(caps.installed)
        self.assertEqual(caps.reasonUnavailable, 'missing_binary')

    async def test_context_run_and_exact_run_use_detected_flags_only(self):
        captured = []

        def runner(args, **kwargs):
            if '--help' in args:
                return Result(FULL_HELP)
            captured.append(list(args))
            return Result(json.dumps({'text': 'The lock is exclusive.'}))

        cursor = CursorProvider(command='/tmp/fake-cursor', runner=runner, help_text=FULL_HELP)
        request = ProviderRequest(
            delegation_id='item-1',
            request_text='What does the lock do?',
            handoff=context_payload(),
            permissions=permissions_payload(network='disabled'),
            workspace='/tmp',
            provider='cursor',
            session_id='thread-origin-1',
            continuity=EXACT,
        )
        result = await cursor.run(request, None)
        self.assertEqual(result['text'], 'The lock is exclusive.')
        self.assertIn('--resume', captured[0])
        self.assertIn('thread-origin-1', captured[0])
        self.assertIn('--print', captured[0])
        self.assertNotIn('--force', captured[0])
        self.assertEqual(result.get('continuity'), EXACT)

        captured.clear()
        claude = ClaudeCodeProvider(command='/tmp/fake-claude', runner=runner, help_text=FULL_HELP)
        context_request = ProviderRequest(
            delegation_id='item-2',
            request_text='Summarize the helper.',
            permissions=permissions_payload(),
            workspace='/tmp',
            provider='claude-code',
            session_id='local-portal',
            continuity=CONTEXT,
            model='invented-model',
            authorize_model=False,
        )
        result = await claude.run(context_request, None)
        self.assertEqual(result['text'], 'The lock is exclusive.')
        argv = captured[0]
        self.assertIn('--print', argv)
        self.assertNotIn('--resume', argv)
        self.assertNotIn('invented-model', argv)

    async def test_exact_mode_rejects_unauthorized_model_override(self):
        cursor = CursorProvider(command='/tmp/fake-cursor', help_text=FULL_HELP, runner=lambda *a, **k: Result('{}'))
        result = await cursor.run(ProviderRequest(
            delegation_id='item-3',
            request_text='Hello',
            provider='cursor',
            session_id='thread-origin-1',
            continuity=EXACT,
            model='mystery',
            authorize_model=False,
            permissions=permissions_payload(),
        ), None)
        self.assertEqual(result['error'], 'model_unauthorized')

    async def test_typed_errors_from_cli_output(self):
        def runner(args, **kwargs):
            return Result(stderr='Please login to continue', returncode=1)

        cursor = CursorProvider(command='/tmp/fake-cursor', runner=runner, help_text=FULL_HELP)
        result = await cursor.run(ProviderRequest(
            delegation_id='item-4', request_text='Hi', provider='cursor',
            session_id='thread-origin-1', continuity=CONTEXT,
            permissions=permissions_payload(),
        ), None)
        self.assertEqual(result['error'], 'authentication_required')

        def missing_session(args, **kwargs):
            return Result(stderr='session not found', returncode=1)

        cursor = CursorProvider(command='/tmp/fake-cursor', runner=missing_session, help_text=FULL_HELP)
        result = await cursor.run(ProviderRequest(
            delegation_id='item-5', request_text='Hi', provider='cursor',
            session_id='thread-origin-1', continuity=EXACT,
            permissions=permissions_payload(),
        ), None)
        self.assertEqual(result['error'], 'session_not_found')

    async def test_mutations_use_generic_executor_not_the_cli(self):
        class Executor:
            async def execute(self, request, plan, cancel):
                self.plan = plan
                return {'status': 'completed', 'summary': 'Changed helper.py after approval.'}

        def runner(args, **kwargs):
            return Result(json.dumps({
                'text': json.dumps({
                    'categories': ['edits'], 'summary': 'edit helper',
                    'files': [{'path': 'helper.py'}], 'commands': [],
                })
            }))

        cursor = CursorProvider(
            command='/tmp/fake-cursor', runner=runner, help_text=FULL_HELP, executor=Executor())
        result = await cursor.run(ProviderRequest(
            delegation_id='item-6',
            request_text='Please change the helper',
            meeting_id='mtg-abc123',
            permissions=permissions_payload(edits='approval-required', workspace='workspace-write'),
            provider='cursor',
            session_id='thread-origin-1',
            continuity=CONTEXT,
        ), None)
        self.assertIn('helper.py', result['text'])

    async def test_jobs_client_fails_closed_without_a_worker(self):
        from provider_jobs import ProviderJobClient
        with tempfile.TemporaryDirectory() as directory:
            cursor = CursorProvider(client=ProviderJobClient('cursor', jobs=directory))
            result = await cursor.run(ProviderRequest(
                delegation_id='item-jobs',
                request_text='Hi',
                provider='cursor',
                session_id='local-portal',
                continuity=CONTEXT,
                permissions=permissions_payload(),
            ), None)
            self.assertEqual(result['error'], 'missing_binary')

    def test_registry_unknown_provider_fail_closed(self):
        registry = ProviderRegistry(providers={
            'codex': CursorProvider(command='/bin/echo', help_text=PRINT_ONLY_HELP),
            'cursor': CursorProvider(command='/bin/echo', help_text=FULL_HELP),
            'claude-code': ClaudeCodeProvider(command='/bin/echo', help_text=PRINT_ONLY_HELP),
        })
        self.assertIsNone(registry.get('windsurf'))
        unknown = registry.capabilities('windsurf')
        self.assertEqual(unknown['reasonUnavailable'], 'unknown_provider')
        self.assertFalse(unknown['usable'])
        dumped = json.dumps(registry.capabilities())
        self.assertNotIn('token', dumped.lower())
        self.assertIn('cursor', dumped)
        self.assertIn('claude-code', dumped)

    def test_capability_record_omits_undetectable_auth(self):
        payload = ProviderCapabilities(id='cursor', installed=True, usable=True).to_dict()
        self.assertNotIn('authenticated', payload)
        self.assertEqual(payload['supportedModels'], [])


class ProviderScriptTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancel_kills_the_child_process_group(self):
        import asyncio
        script = '''#!/usr/bin/env python3
import sys, time
if "--help" in sys.argv or "--version" in sys.argv or "exec" in sys.argv:
    print("--print\\n--resume")
    raise SystemExit(0)
time.sleep(30)
'''
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'slow.py'
            path.write_text(script)
            os.chmod(path, 0o700)
            cursor = CursorProvider(command=str(path), timeout=8)
            cancel = __import__('threading').Event()
            task = asyncio.create_task(cursor.run(ProviderRequest(
                delegation_id='item-slow',
                request_text='Wait',
                provider='cursor',
                session_id='thread-origin-1',
                continuity=CONTEXT,
                permissions=permissions_payload(),
            ), cancel))
            await asyncio.sleep(0.2)
            cancel.set()
            result = await asyncio.wait_for(task, 5)
            self.assertEqual(result['error'], 'cancelled')


class ProviderJobIsolationTests(unittest.TestCase):
    def test_jobs_directories_are_isolated_and_unknown_providers_fail_closed(self):
        from provider_jobs import provider_jobs_dir, rewrite_private, write_heartbeat
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cursor = provider_jobs_dir('cursor', root)
            claude = provider_jobs_dir('claude-code', root)
            self.assertNotEqual(cursor, claude)
            self.assertTrue(str(cursor).endswith('/cursor'))
            write_heartbeat(cursor)
            self.assertFalse((claude / 'heartbeat').exists())
            self.assertEqual(cursor.stat().st_mode & 0o777, 0o700)
            rewrite_private(cursor / 'sample.json', '{}')
            self.assertEqual((cursor / 'sample.json').stat().st_mode & 0o777, 0o600)
            with self.assertRaises(ValueError):
                provider_jobs_dir('windsurf', root)

    def test_duplicate_worker_lock_is_exclusive(self):
        import fcntl
        from provider_jobs import worker_lock_path
        with tempfile.TemporaryDirectory() as directory:
            lock_path = worker_lock_path(directory)
            first = lock_path.open('w')
            fcntl.flock(first, fcntl.LOCK_EX | fcntl.LOCK_NB)
            second = lock_path.open('w')
            with self.assertRaises(BlockingIOError):
                fcntl.flock(second, fcntl.LOCK_EX | fcntl.LOCK_NB)
            first.close()
            second.close()

    def test_cross_provider_job_is_rejected(self):
        from provider_worker import handle_job
        from providers.cursor import CursorProvider
        cursor = CursorProvider(command='/bin/echo', help_text=FULL_HELP)
        result = handle_job(cursor, {'provider': 'claude-code', 'op': 'run', 'task': 'hi'}, 'job-x')
        self.assertEqual(result['error'], 'unknown_provider')


class CodexCapabilityTests(unittest.TestCase):
    def test_echo_binary_does_not_advertise_exact_resume(self):
        from providers.codex import CodexProvider
        original = os.environ.get('CODEX_BIN')
        os.environ['CODEX_BIN'] = '/bin/echo'
        try:
            caps = CodexProvider().capabilities()
            self.assertTrue(caps.installed)
            self.assertFalse(caps.exactSessionResume)
        finally:
            if original is None:
                os.environ.pop('CODEX_BIN', None)
            else:
                os.environ['CODEX_BIN'] = original


if __name__ == '__main__':
    unittest.main()
