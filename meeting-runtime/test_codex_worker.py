import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path

from agent_sessions import AgentSessionRef
from session_continuity import CONTEXT, EXACT, continuity_mode, validate_agent_session
from test_schemas import agent_session_payload


class BridgeLivenessTests(unittest.TestCase):
    def test_worker_exits_only_after_a_seen_bridge_stays_unavailable(self):
        from codex_worker import BridgeLiveness
        liveness = BridgeLiveness(timeout=10)
        self.assertTrue(liveness.update(False, 0))
        self.assertTrue(liveness.update(True, 5))
        self.assertTrue(liveness.update(False, 15))
        self.assertFalse(liveness.update(False, 15.01))


class SessionContinuityTests(unittest.TestCase):
    def test_portal_is_context_and_origin_is_exact(self):
        portal = AgentSessionRef.from_dict(agent_session_payload(
            sessionId='local-portal', metadata={'source': 'local-portal', 'continuity': 'context'}))
        origin = AgentSessionRef.from_dict(agent_session_payload())
        self.assertEqual(continuity_mode(portal), CONTEXT)
        self.assertEqual(validate_agent_session(portal), CONTEXT)
        self.assertEqual(continuity_mode(origin), EXACT)
        self.assertEqual(validate_agent_session(origin), EXACT)

    def test_rejects_last_and_exact_portal_ids(self):
        for session_id in ('--last', 'last', '-abc'):
            with self.assertRaises(ValueError):
                validate_agent_session({'sessionId': session_id, 'metadata': {}})
        with self.assertRaises(ValueError):
            validate_agent_session({
                'sessionId': 'local-portal',
                'metadata': {'source': 'codex-app-server', 'continuity': 'exact'},
            })
        with self.assertRaises(ValueError):
            validate_agent_session({'sessionId': '', 'metadata': {}})


class CodexWorkerInheritanceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.workspace = self.root / 'workspace'
        self.workspace.mkdir()
        self.jobs = self.root / 'jobs'
        self.jobs.mkdir()
        self.argv_log = self.root / 'argv.json'
        self.started = self.root / 'started'
        self.executable = self.root / 'fake-codex'
        self.executable.write_text(
            '#!/usr/bin/env python3\n'
            'import json, os, pathlib, sys, time\n'
            'args = sys.argv\n'
            'pathlib.Path(os.environ["FAKE_CODEX_LOG"]).write_text(json.dumps(args))\n'
            'if "--last" in args or "last" in args:\n'
            '    sys.stderr.write("unexpected last flag\\n")\n'
            '    sys.exit(2)\n'
            'if "resume" in args:\n'
            '    session = args[args.index("resume") + 1]\n'
            '    if session in ("--last", "last") or session.startswith("-"):\n'
            '        sys.stderr.write("invalid session\\n")\n'
            '        sys.exit(2)\n'
            '    if session == "missing-thread":\n'
            '        sys.stderr.write("session not found\\n")\n'
            '        sys.exit(1)\n'
            'else:\n'
            '    session = os.environ.get("FAKE_THREAD", "thread-new")\n'
            'prompt = sys.stdin.read()\n'
            'started = os.environ.get("FAKE_STARTED")\n'
            'if started:\n'
            '    pathlib.Path(started).write_text("1")\n'
            'delay = os.environ.get("FAKE_CODEX_SLEEP")\n'
            'if delay:\n'
            '    time.sleep(float(delay))\n'
            'result = pathlib.Path(args[args.index("-o") + 1])\n'
            'model = args[args.index("-m") + 1] if "-m" in args else "session-model"\n'
            'payload = {\n'
            '    "model": model,\n'
            '    "resume": "resume" in args,\n'
            '    "session": args[args.index("resume") + 1] if "resume" in args else None,\n'
            '    "cwd": os.getcwd(),\n'
            '    "sandbox": args[args.index("--sandbox") + 1] if "--sandbox" in args else None,\n'
            '    "json": "--json" in args,\n'
            '    "prompt": prompt[-240:],\n'
            '}\n'
            'result.write_text(json.dumps(payload))\n'
            'thread = os.environ.get("FAKE_THREAD") or session\n'
            'print(json.dumps({"type": "thread.started", "thread_id": thread}))\n'
            'print(json.dumps({"type": "item.completed", "text": os.environ.get("FAKE_PROGRESS", "inspecting files")}))\n'
        )
        self.executable.chmod(0o700)
        os.environ['FAKE_CODEX_LOG'] = str(self.argv_log)
        self.argv_log.unlink(missing_ok=True)
        self.started.unlink(missing_ok=True)

    def tearDown(self):
        os.environ.pop('FAKE_CODEX_LOG', None)
        os.environ.pop('FAKE_CODEX_SLEEP', None)
        os.environ.pop('FAKE_STARTED', None)
        os.environ.pop('FAKE_THREAD', None)
        os.environ.pop('FAKE_PROGRESS', None)
        self.temporary.cleanup()

    def _argv(self):
        return json.loads(self.argv_log.read_text())

    def _run(self, data, name='out.txt', **kwargs):
        from codex_worker import handle_job, run_job
        payload = {
            'model': 'gpt-5.6-luna',
            'task': data.get('task', 'Remember the cobalt heron'),
            'workspace': str(self.workspace),
            **{key: value for key, value in data.items() if key != 'task'},
        }
        output = self.root / name
        if kwargs.pop('through_handle', False):
            return handle_job(
                str(self.executable), payload, output, jobs_dir=self.jobs,
                **kwargs)
        return run_job(
            str(self.executable), payload, output, payload.get('session_id'),
            jobs_dir=self.jobs, **kwargs)

    def test_exact_resume_uses_origin_id_workspace_sandbox_and_optional_model(self):
        result = self._run({
            'continuity': 'exact',
            'session_id': 'thread-origin-1',
            'authorize_model': True,
            'model': 'gpt-5.6-sol',
            'task': 'What did we decide about the lock?',
        }, 'exact.txt')
        argv = self._argv()
        self.assertEqual(argv[1], 'exec')
        self.assertEqual(argv[2:5], ['resume', 'thread-origin-1', '-'])
        self.assertNotIn('--last', argv)
        self.assertNotIn('last', argv)
        self.assertEqual(argv[argv.index('--sandbox') + 1], 'read-only')
        self.assertEqual(Path(argv[argv.index('-C') + 1]).resolve(), self.workspace.resolve())
        self.assertIn('--json', argv)
        self.assertIn('-o', argv)
        self.assertEqual(argv[argv.index('-m') + 1], 'gpt-5.6-sol')
        body = json.loads(result['text'])
        self.assertEqual(Path(body['cwd']).resolve(), self.workspace.resolve())
        self.assertTrue(body['resume'])
        self.assertEqual(body['session'], 'thread-origin-1')
        self.assertIn('What did we decide', body['prompt'])
        self.assertEqual(result['session_id'], 'thread-origin-1')
        self.assertTrue(result['session_reused'])
        self.assertEqual(result['continuity'], 'exact')

    def test_exact_omits_model_flag_unless_authorized(self):
        self._run({
            'continuity': 'exact',
            'session_id': 'thread-origin-1',
            'authorize_model': False,
            'model': 'gpt-5.6-sol',
        })
        argv = self._argv()
        self.assertNotIn('-m', argv)
        self.assertNotIn('--model', argv)

    def test_context_mode_does_not_resume_origin_or_use_last(self):
        result = self._run({
            'continuity': 'context',
            'session_id': 'local-portal',
            'source': 'local-portal',
            'meeting_id': 'mtg-portal00000001',
        }, through_handle=True)
        argv = self._argv()
        self.assertEqual(argv[2], '-')
        self.assertNotIn('resume', argv)
        self.assertNotIn('--last', argv)
        self.assertFalse(result['session_reused'])
        self.assertEqual(result['continuity'], 'context')

    def test_rejects_invalid_and_missing_exact_ids_before_spawn(self):
        missing = self._run({'continuity': 'exact', 'task': 'x'})
        self.assertIn('session', missing['error'].lower())
        last = self._run({'continuity': 'exact', 'session_id': '--last', 'task': 'x'})
        self.assertIn('explicit originating thread id', last['error'])
        self.assertFalse(self.argv_log.exists())

    def test_session_not_found_does_not_create_a_replacement_thread(self):
        result = self._run({
            'continuity': 'exact',
            'session_id': 'missing-thread',
            'task': 'Continue the previous plan',
        })
        self.assertIn('session not found', result['error'])
        self.assertNotIn('session_id', result)

    def test_jsonl_progress_is_bounded_and_redacted(self):
        os.environ['FAKE_PROGRESS'] = 'using sk-secretsecret12 while inspecting'
        progress = self.root / 'job.progress.json'
        result = self._run({
            'continuity': 'exact',
            'session_id': 'thread-origin-1',
            'task': 'Inspect the lock',
        }, progress_path=progress)
        self.assertNotIn('error', result)
        payload = json.loads(progress.read_text())
        self.assertIn('message', payload)
        self.assertNotIn('sk-secretsecret12', payload['message'])
        self.assertIn('[redacted]', payload['message'])
        self.assertLessEqual(len(payload['message']), 200)

    def test_timeout_kills_the_child(self):
        os.environ['FAKE_CODEX_SLEEP'] = '8'
        started = time.monotonic()
        result = self._run({
            'continuity': 'exact',
            'session_id': 'thread-origin-1',
            'task': 'Slow work',
        }, timeout=0.4)
        self.assertEqual(result['error'], 'Codex agent timed out')
        self.assertLess(time.monotonic() - started, 4)

    def test_cancel_file_kills_the_child(self):
        os.environ['FAKE_CODEX_SLEEP'] = '8'
        os.environ['FAKE_STARTED'] = str(self.started)
        cancel_path = self.root / 'job.cancel'
        result = {}

        def worker():
            result['job'] = self._run({
                'continuity': 'exact',
                'session_id': 'thread-origin-1',
                'task': 'Slow work',
            }, timeout=6, cancel_path=cancel_path)

        thread = threading.Thread(target=worker)
        thread.start()
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline and not self.started.exists():
            time.sleep(0.05)
        self.assertTrue(self.started.exists())
        cancel_path.write_text('1')
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(result['job']['error'], 'cancelled')

    def test_session_lock_fails_closed_instead_of_forking(self):
        from codex_worker import exclusive_session_lock, session_lock_key
        data = {'continuity': 'exact', 'session_id': 'thread-origin-1', 'task': 'x'}
        key = session_lock_key(data, 'thread-origin-1')
        with exclusive_session_lock(self.jobs, key):
            result = self._run(data)
        self.assertIn('already in use', result['error'])
        self.assertFalse(self.argv_log.exists())

    def test_concurrent_resume_of_the_same_session_is_serialized(self):
        os.environ['FAKE_CODEX_SLEEP'] = '1.2'
        os.environ['FAKE_STARTED'] = str(self.started)
        results = []

        def worker():
            results.append(self._run({
                'continuity': 'exact',
                'session_id': 'thread-origin-1',
                'task': 'Turn',
            }, name=f'out-{threading.get_ident()}.txt', timeout=5))

        first = threading.Thread(target=worker)
        first.start()
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline and not self.started.exists():
            time.sleep(0.05)
        second = threading.Thread(target=worker)
        second.start()
        first.join(6)
        second.join(6)
        errors = [item.get('error') for item in results]
        self.assertTrue(any(item and 'already in use' in item for item in errors), errors)
        self.assertTrue(any(item.get('session_id') == 'thread-origin-1' for item in results))

    def test_append_handoff_is_idempotent_and_does_not_release(self):
        from codex_worker import append_handoff, handle_job
        data = {
            'continuity': 'exact',
            'session_id': 'thread-origin-1',
            'meeting_id': 'mtg-abc123',
            'handoff_id': 'mtg-abc123',
            'model': 'gpt-5.6-luna',
            'workspace': str(self.workspace),
            'handoff': {'summary': 'Ship Friday.', 'meetingId': 'mtg-abc123'},
        }
        first = append_handoff(
            str(self.executable), data, self.root / 'handoff1.txt', jobs_dir=self.jobs)
        second = append_handoff(
            str(self.executable), data, self.root / 'handoff2.txt', jobs_dir=self.jobs)
        self.assertTrue(first.get('appended'))
        self.assertTrue(second.get('idempotent'))
        self.assertFalse(second.get('appended'))
        argv = self._argv()
        self.assertEqual(argv[2:5], ['resume', 'thread-origin-1', '-'])
        released = handle_job(
            str(self.executable),
            {'op': 'release', 'session_id': 'thread-origin-1'},
            self.root / 'release.txt',
            jobs_dir=self.jobs,
        )
        self.assertFalse(released.get('released'))

    def test_url_hash_is_not_used_as_origin_identity(self):
        from codex_worker import handle_job
        sessions = {'a' * 24: 'thread-from-url-hash'}
        result = handle_job(
            str(self.executable),
            {
                'op': 'run',
                'task': 'Continue',
                'model': 'gpt-5.6-luna',
                'continuity': 'exact',
                'session_id': 'thread-origin-1',
                'session_key': 'a' * 24,
                'workspace': str(self.workspace),
            },
            self.root / 'hash.txt',
            jobs_dir=self.jobs,
            sessions=sessions,
        )
        argv = self._argv()
        self.assertEqual(argv[3], 'thread-origin-1')
        self.assertNotEqual(argv[3], 'thread-from-url-hash')
        self.assertEqual(result['session_id'], 'thread-origin-1')


if __name__ == '__main__':
    unittest.main()
