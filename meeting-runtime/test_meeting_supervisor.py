import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from agent_sessions import MeetingSession
from meeting_supervisor import DaemonError, ProductionMeetingSupervisor
from runtime_state import (
    active_meeting_path, daemon_token_path, file_mode, meeting_state_path, read_json,
)
from test_schemas import ZOOM_URL, context_payload, meeting_session_payload

from daemon_main import write_auth_token
from runtime_config import RuntimeConfig, resolve_meeting_url


def session(**overrides):
    payload = meeting_session_payload(**overrides)
    return MeetingSession.from_dict(payload)


class FakeLauncher:
    def __init__(self):
        self.running = False
        self.unknown = False
        self.ups = []
        self.stops = 0
        self.fail_up = False
        self.stay_stopped = False

    async def up(self, env):
        self.ups.append(dict(env))
        if self.fail_up:
            raise RuntimeError('compose up failed')
        if not self.stay_stopped:
            self.running = True

    async def stop(self):
        self.stops += 1
        self.running = False

    async def inspect(self):
        return {'running': self.running, 'unknown': self.unknown}


class FakeHostWorkerHandle:
    def __init__(self, exit_code=None):
        self.exit_code = exit_code
        self.terminated = False
        self.waited = False

    def poll(self):
        return self.exit_code

    def terminate(self):
        self.terminated = True
        if self.exit_code is None:
            self.exit_code = 0

    async def wait(self):
        self.waited = True
        if self.exit_code is None:
            self.exit_code = 0
        return self.exit_code


class FakeHostWorker:
    def __init__(self):
        self.codex_bin = '/usr/local/bin/codex'
        self.login_ok = True
        self.fail_start = False
        self.early_exit = False
        self.starts = []
        self.handles = []

    def discover_codex(self):
        return self.codex_bin

    def check_login(self, _codex_bin):
        if not self.login_ok:
            raise RuntimeError('Codex CLI is not logged in. Run codex login first.')

    async def start(self, *, env, cwd):
        if self.fail_start:
            raise RuntimeError('worker start failed')
        handle = FakeHostWorkerHandle(exit_code=1 if self.early_exit else None)
        self.starts.append({'env': dict(env), 'cwd': cwd})
        self.handles.append(handle)
        return handle


class FakeHealth:
    def __init__(self, payload=None):
        self.payload = payload if payload is not None else {'stage': 'starting'}
        self.calls = 0

    async def fetch(self):
        self.calls += 1
        return self.payload


class FakeMeetings:
    def __init__(self):
        self.records = {}

    def get(self, meeting_id):
        return self.records.get(meeting_id)

    def list_ids(self):
        return tuple(sorted(self.records))


class FakeLeases:
    def __init__(self):
        self.records = {}

    def get(self, provider, session_id):
        return self.records.get((provider, session_id), True)


class FakeRecord:
    def __init__(self, session, lease_token='lease-secret', handoff=None):
        self.session = session
        self.lease_token = lease_token
        self.handoff = handoff


class FakeDaemon:
    def __init__(self):
        self.transitions = []
        self.heartbeats = []
        self.failures = []
        self.meetings = FakeMeetings()
        self.leases = FakeLeases()

    def transition(self, meeting_id, state, **_kwargs):
        self.transitions.append((meeting_id, state))

    def heartbeat_lease(self, provider, session_id):
        self.heartbeats.append((provider, session_id))
        return {'provider': provider, 'sessionId': session_id}

    def record_finalization_failure(self, meeting_id, reason):
        self.failures.append((meeting_id, reason))
        return None


class SupervisorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.runtime = self.root / 'meeting-runtime'
        self.runtime.mkdir()
        self.launcher = FakeLauncher()
        self.health = FakeHealth()
        self.host_worker = FakeHostWorker()
        self.daemon = FakeDaemon()
        self.supervisor = ProductionMeetingSupervisor(
            self.root,
            runtime_root=self.runtime,
            launcher=self.launcher,
            health=self.health,
            host_worker=self.host_worker,
            poll_interval=0.02,
            start_timeout=0.4,
            stop_timeout=0.4,
        ).bind_daemon(self.daemon)

    async def asyncTearDown(self):
        await self.supervisor.shutdown()
        self.temporary.cleanup()

    async def test_launch_to_live_context_cancel_and_heartbeat(self):
        meeting = session()
        await self.supervisor.start(meeting)
        self.assertEqual(len(self.launcher.ups), 1)
        self.assertEqual(len(self.host_worker.starts), 1)
        worker_env = self.host_worker.starts[0]['env']
        self.assertEqual(worker_env['COLLEAGUE_WORKSPACE'], '/Users/Taylor/project')
        self.assertEqual(worker_env['CODEX_BIN'], '/usr/local/bin/codex')
        self.assertEqual(self.host_worker.starts[0]['cwd'], str(self.runtime))
        self.assertNotIn('OPENAI_API_KEY', worker_env)
        self.assertNotIn('TAVILY_API_KEY', worker_env)
        self.assertNotIn('MEETING_PASSCODE', worker_env)
        self.assertIn('/meeting-runtime/run/meetings/' + meeting.id, self.launcher.ups[0]['COLLEAGUE_RUNTIME_STATE'])
        stored = read_json(meeting_state_path(self.runtime, meeting.id))
        self.assertEqual(stored['meetingId'], meeting.id)
        self.assertEqual(stored['meetingUrl'], ZOOM_URL)
        dumped = json.dumps(stored)
        self.assertNotIn('leaseId', dumped)
        self.assertNotIn('OPENAI', dumped)
        self.assertNotIn('apiKey', dumped)
        self.assertEqual(file_mode(meeting_state_path(self.runtime, meeting.id)), 0o600)
        self.assertEqual(file_mode(self.runtime / 'run'), 0o700)
        self.health.payload = {'stage': 'waiting_for_admission'}
        await asyncio.sleep(0.08)
        self.health.payload = {'stage': 'live'}
        await asyncio.sleep(0.08)
        self.assertIn((meeting.id, 'waiting_for_admission'), self.daemon.transitions)
        self.assertIn((meeting.id, 'live'), self.daemon.transitions)
        self.assertTrue(self.daemon.heartbeats)
        updated = meeting.context
        await self.supervisor.add_context(meeting.id, updated)
        again = read_json(meeting_state_path(self.runtime, meeting.id))
        self.assertEqual(again['context']['objective'], 'Ship the developer platform')
        await self.supervisor.cancel(meeting.id)
        self.assertGreaterEqual(self.launcher.stops, 1)
        self.assertFalse(self.launcher.running)
        handle = self.host_worker.handles[0]
        self.assertTrue(handle.terminated)
        self.assertTrue(handle.waited)
        self.assertIsNone(self.supervisor._worker)
        await self.supervisor.cancel(meeting.id)
        self.assertFalse(self.launcher.running)
        self.assertEqual(len(self.host_worker.handles), 1)

    async def test_capacity_is_exclusive_and_idempotent(self):
        first = session()
        await self.supervisor.start(first)
        await self.supervisor.start(first)
        self.assertEqual(len(self.launcher.ups), 1)
        self.assertEqual(len(self.host_worker.starts), 1)
        other = session(id='mtg-other0000001', agentSession={
            **meeting_session_payload()['agentSession'], 'sessionId': 'thread-other-1',
        })
        with self.assertRaises(DaemonError) as raised:
            await self.supervisor.start(other)
        self.assertEqual(raised.exception.code, 'capacity_exceeded')
        self.assertEqual(len(self.launcher.ups), 1)
        await self.supervisor.cancel(first.id)
        first = session()
        other = session(id='mtg-other0000002', agentSession={
            **meeting_session_payload()['agentSession'], 'sessionId': 'thread-other-2',
        })
        first_result, second_result = await asyncio.gather(
            self.supervisor.start(first),
            self.supervisor.start(other),
            return_exceptions=True,
        )
        errors = [item for item in (first_result, second_result) if isinstance(item, Exception)]
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0].code, 'capacity_exceeded')
        self.assertEqual(len(self.launcher.ups), 2)

    async def test_startup_and_health_failure_stops_container(self):
        self.launcher.fail_up = True
        with self.assertRaises(RuntimeError):
            await self.supervisor.start(session())
        self.assertGreaterEqual(self.launcher.stops, 1)
        self.assertIsNone(self.supervisor._active_id)
        self.assertEqual(self.host_worker.starts, [])
        self.assertIsNone(self.supervisor._worker)

        self.launcher.fail_up = False
        self.health.payload = None
        self.supervisor.start_timeout = 0.12
        with self.assertRaises(RuntimeError):
            await self.supervisor.start(session(id='mtg-healthfail0001'))
        self.assertFalse(self.launcher.running)
        self.assertIsNone(self.supervisor._active_id)
        self.assertEqual(self.host_worker.starts, [])

    async def test_unexpected_exit_records_finalization_failure(self):
        meeting = session()
        await self.supervisor.start(meeting)
        self.launcher.running = False
        await asyncio.sleep(0.08)
        self.assertEqual(self.daemon.failures[0][0], meeting.id)
        self.assertEqual(self.daemon.failures[0][1], 'container_exited')
        handle = self.host_worker.handles[0]
        self.assertTrue(handle.terminated)
        self.assertTrue(handle.waited)
        self.assertIsNone(self.supervisor._worker)

    async def test_finished_without_handoff_is_finalization_failure(self):
        meeting = session()
        await self.supervisor.start(meeting)
        self.health.payload = {'stage': 'finished'}
        await asyncio.sleep(0.08)
        self.assertEqual(self.daemon.failures[-1][1], 'runtime_ended_before_handoff')
        handle = self.host_worker.handles[0]
        self.assertTrue(handle.terminated)
        self.assertTrue(handle.waited)
        self.assertIsNone(self.supervisor._worker)

    async def test_restart_reconciliation(self):
        meeting = session()
        self.daemon.meetings.records[meeting.id] = FakeRecord(meeting)
        await self.supervisor.reconcile(self.daemon)
        self.assertEqual(self.daemon.failures[-1][1], 'runtime_unavailable_on_restart')

        self.daemon.failures.clear()
        self.launcher.unknown = True
        await self.supervisor.reconcile(self.daemon)
        self.assertEqual(self.daemon.failures, [])

        self.launcher.unknown = False
        self.launcher.running = True
        write_path = active_meeting_path(self.runtime)
        from runtime_state import write_private_json
        write_private_json(write_path, {'meetingId': meeting.id})
        await self.supervisor.reconcile(self.daemon)
        self.assertEqual(self.supervisor._active_id, meeting.id)
        self.assertTrue(self.supervisor._watch_task)
        self.assertEqual(len(self.host_worker.starts), 1)
        await self.supervisor.reconcile(self.daemon)
        self.assertEqual(len(self.host_worker.starts), 1)

    async def test_uncertain_running_container_is_not_taken_over(self):
        meeting = session()
        self.daemon.meetings.records[meeting.id] = FakeRecord(meeting)
        self.launcher.running = True
        await self.supervisor.reconcile(self.daemon)
        self.assertIsNone(self.supervisor._active_id)
        self.assertEqual(self.daemon.failures, [])

    async def test_token_file_permissions_and_secret_exclusion(self):
        token, path = write_auth_token(self.runtime, 'daemon-test-token')
        self.assertEqual(token, 'daemon-test-token')
        self.assertEqual(path, daemon_token_path(self.runtime))
        self.assertEqual(file_mode(path), 0o600)
        self.assertEqual(file_mode(self.runtime / 'run'), 0o700)
        self.assertEqual(path.read_text(encoding='utf-8').strip(), 'daemon-test-token')
        from runtime_state import write_private_json
        with self.assertRaises(ValueError):
            write_private_json(meeting_state_path(self.runtime, 'mtg-secret0000001'), {
                'version': 1,
                'meetingId': 'mtg-secret0000001',
                'apiKey': 'sk-live-secret',
            })
        with self.assertRaises(ValueError):
            write_private_json(meeting_state_path(self.runtime, 'mtg-secret0000002'), {
                'version': 1,
                'password': 'meeting-passcode',
            })

    async def test_codex_disabled_does_not_start_worker(self):
        self.supervisor.wants_codex = lambda _session: False
        meeting = session()
        await self.supervisor.start(meeting)
        self.assertEqual(self.host_worker.starts, [])
        self.assertIsNone(self.supervisor._worker)
        await self.supervisor.cancel(meeting.id)
        self.assertEqual(self.host_worker.handles, [])

    async def test_missing_codex_and_login_failure_are_truthful(self):
        self.host_worker.codex_bin = None
        with self.assertRaises(RuntimeError) as raised:
            await self.supervisor.start(session())
        self.assertIn('Codex CLI not found', str(raised.exception))
        self.assertFalse(self.launcher.running)
        self.assertEqual(self.launcher.ups, [])
        self.assertIsNone(self.supervisor._worker)
        self.assertEqual(self.host_worker.starts, [])

        self.host_worker.codex_bin = '/usr/local/bin/codex'
        self.host_worker.login_ok = False
        with self.assertRaises(RuntimeError) as raised:
            await self.supervisor.start(session(id='mtg-loginfail00001'))
        self.assertIn('not logged in', str(raised.exception))
        self.assertFalse(self.launcher.running)
        self.assertEqual(self.host_worker.starts, [])

    async def test_early_worker_exit_stops_container(self):
        self.host_worker.early_exit = True
        with self.assertRaises(RuntimeError) as raised:
            await self.supervisor.start(session())
        self.assertIn('exited during startup', str(raised.exception))
        self.assertFalse(self.launcher.running)
        self.assertIsNone(self.supervisor._worker)
        handle = self.host_worker.handles[0]
        self.assertTrue(handle.waited)

    async def test_worker_death_records_finalization_and_stops(self):
        meeting = session()
        await self.supervisor.start(meeting)
        handle = self.host_worker.handles[0]
        handle.exit_code = 1
        await asyncio.sleep(0.08)
        self.assertEqual(self.daemon.failures[-1][1], 'codex_worker_exited')
        self.assertTrue(handle.waited)
        self.assertIsNone(self.supervisor._worker)
        self.assertFalse(self.launcher.running)

    async def test_shutdown_and_reconcile_await_worker(self):
        meeting = session()
        await self.supervisor.start(meeting)
        handle = self.host_worker.handles[0]
        await self.supervisor.shutdown()
        self.assertTrue(handle.terminated)
        self.assertTrue(handle.waited)
        self.assertIsNone(self.supervisor._worker)
        self.assertTrue(self.launcher.running)

        self.host_worker.starts.clear()
        self.daemon.meetings.records[meeting.id] = FakeRecord(meeting)
        from runtime_state import write_private_json
        write_private_json(active_meeting_path(self.runtime), {'meetingId': meeting.id})
        await self.supervisor.reconcile(self.daemon)
        self.assertEqual(len(self.host_worker.starts), 1)
        restarted = self.host_worker.handles[-1]
        self.launcher.running = False
        await self.supervisor.reconcile(self.daemon)
        self.assertTrue(restarted.terminated)
        self.assertTrue(restarted.waited)
        self.assertIsNone(self.supervisor._worker)
        await self.supervisor.reconcile(self.daemon)
        self.assertIsNone(self.supervisor._worker)


class HostWorkerEnvTests(unittest.TestCase):
    def test_sanitized_environ_drops_provider_secrets(self):
        from meeting_supervisor import SubprocessHostWorker
        worker = SubprocessHostWorker('/tmp/runtime')
        env = worker.sanitized_environ({
            'CODEX_BIN': '/usr/local/bin/codex',
            'COLLEAGUE_WORKSPACE': '/Users/Taylor/project',
            'OPENAI_API_KEY': 'sk-secret',
            'TAVILY_API_KEY': 'tvly-secret',
            'MEETING_PASSCODE': '1234',
            'MEETING_URL': 'https://zoom.us/j/1',
        })
        self.assertEqual(env['CODEX_BIN'], '/usr/local/bin/codex')
        self.assertEqual(env['COLLEAGUE_WORKSPACE'], '/Users/Taylor/project')
        self.assertNotIn('OPENAI_API_KEY', env)
        self.assertNotIn('TAVILY_API_KEY', env)
        self.assertNotIn('MEETING_PASSCODE', env)
        self.assertNotIn('MEETING_URL', env)


class RuntimeStateConfigTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / 'runtime.json'

    def tearDown(self):
        self.temporary.cleanup()

    def test_state_overlay_and_meeting_url(self):
        from runtime_state import write_private_json
        write_private_json(self.path, {
            'version': 1,
            'meetingId': 'mtg-config0000001',
            'meetingUrl': ZOOM_URL,
            'participantName': 'Runtime Colleague',
            'workspace': '/tmp/workspace',
            'defaultCodexModel': 'gpt-5.6-terra',
            'webSearchEnabled': False,
            'codexEnabled': True,
            'chartsEnabled': False,
            'meetingInstructions': 'Stay brief.',
        })
        env = {'COLLEAGUE_RUNTIME_STATE': str(self.path), 'MEETING_URL': 'https://example.com/old'}
        config = RuntimeConfig.from_environ(env)
        self.assertEqual(config.participant_name, 'Runtime Colleague')
        self.assertFalse(config.web_search_enabled)
        self.assertEqual(config.meeting_instructions, 'Stay brief.')
        self.assertEqual(resolve_meeting_url(env), ZOOM_URL)

    def test_missing_state_keeps_environ(self):
        config = RuntimeConfig.from_environ({})
        self.assertEqual(config.participant_name, 'Colleague AI')
        with self.assertRaises(KeyError):
            resolve_meeting_url({})


if __name__ == '__main__':
    unittest.main()
