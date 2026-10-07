import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

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


class FakeRecord:
    def __init__(self, session, handoff=None):
        self.session = session
        self.handoff = handoff


class FakeDaemon:
    def __init__(self):
        self.transitions = []
        self.presence = []
        self.failures = []
        self.handoffs = []
        self.meetings = FakeMeetings()

    def transition(self, meeting_id, state, **_kwargs):
        self.transitions.append((meeting_id, state))

    def apply_presence(self, meeting_id, **fields):
        self.presence.append((meeting_id, fields))

    def record_finalization_failure(self, meeting_id, reason):
        self.failures.append((meeting_id, reason))
        return None

    def store_handoff(self, handoff):
        self.handoffs.append(handoff)
        record = self.meetings.records.get(handoff.meeting_id)
        if record is None:
            record = FakeRecord(session(id=handoff.meeting_id))
            self.meetings.records[handoff.meeting_id] = record
        record.handoff = handoff
        return handoff


class SupervisorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.runtime = self.root / 'meeting-runtime'
        self.runtime.mkdir()
        self.launcher = FakeLauncher()
        self.health = FakeHealth()
        self.daemon = FakeDaemon()
        self.supervisor = ProductionMeetingSupervisor(
            self.root,
            runtime_root=self.runtime,
            launcher=self.launcher,
            health=self.health,
            poll_interval=0.02,
            start_timeout=0.4,
            stop_timeout=0.4,
        ).bind_daemon(self.daemon)

    async def asyncTearDown(self):
        await self.supervisor.shutdown()
        self.temporary.cleanup()

    async def test_camera_settings_are_written_to_runtime_state_without_host_paths(self):
        meeting = session()
        await self.supervisor.start(meeting, camera_settings={
            'cameraEnabled': True,
            'cameraDefaultOn': False,
            'cameraAvatarDataUri': 'data:image/png;base64,abc',
            'cameraAvatarPath': '/Users/example/secret.png',
        })
        stored = read_json(meeting_state_path(self.runtime, meeting.id))
        self.assertTrue(stored['cameraEnabled'])
        self.assertFalse(stored['cameraDefaultOn'])
        self.assertEqual(stored['cameraAvatarDataUri'], 'data:image/png;base64,abc')
        self.assertNotIn('cameraAvatarPath', stored)
        self.assertNotIn('/Users/example/secret.png', json.dumps(stored))
        await self.supervisor.cancel(meeting.id)

    async def test_on_behalf_of_and_voice_reach_the_runtime_state(self):
        meeting = session(onBehalfOf='Jordan Lee', voice='cinder')
        await self.supervisor.start(meeting)
        stored = read_json(meeting_state_path(self.runtime, meeting.id))
        self.assertEqual(stored['onBehalfOf'], 'Jordan Lee')
        self.assertEqual(stored['voice'], 'cinder')
        config = RuntimeConfig.from_environ({
            'SMITLINE_RUNTIME_STATE': str(meeting_state_path(self.runtime, meeting.id))})
        self.assertEqual(config.owner_name, 'Jordan Lee')
        self.assertEqual(config.voice, 'cinder')
        await self.supervisor.cancel(meeting.id)

    async def test_launch_to_live_context_and_cancel(self):
        meeting = session()
        await self.supervisor.start(meeting)
        self.assertEqual(len(self.launcher.ups), 1)
        self.assertIn('/meeting-runtime/run/meetings/' + meeting.id,
                      self.launcher.ups[0]['SMITLINE_RUNTIME_STATE'])
        stored = read_json(meeting_state_path(self.runtime, meeting.id))
        self.assertEqual(stored['meetingId'], meeting.id)
        self.assertEqual(stored['meetingUrl'], ZOOM_URL)
        for removed in ('workspace', 'provider', 'sessionId', 'continuity', 'permissions',
                        'codexEnabled', 'defaultCodexModel'):
            self.assertNotIn(removed, stored)
        dumped = json.dumps(stored)
        self.assertNotIn('OPENAI', dumped)
        self.assertNotIn('apiKey', dumped)
        self.assertEqual(file_mode(meeting_state_path(self.runtime, meeting.id)), 0o600)
        self.assertEqual(file_mode(self.runtime / 'run'), 0o700)
        self.assertFalse((self.runtime / 'run' / 'meetings' / meeting.id / 'context.json').exists())
        self.health.payload = {'stage': 'waiting_for_admission'}
        await asyncio.sleep(0.08)
        self.health.payload = {'stage': 'live'}
        await asyncio.sleep(0.08)
        self.assertIn((meeting.id, 'waiting_for_admission'), self.daemon.transitions)
        self.assertIn((meeting.id, 'live'), self.daemon.transitions)
        self.assertTrue(self.daemon.presence)
        await self.supervisor.add_context(meeting.id, meeting.context)
        again = read_json(meeting_state_path(self.runtime, meeting.id))
        self.assertEqual(again['context']['objective'], 'Ship the developer platform')
        await self.supervisor.cancel(meeting.id)
        self.assertGreaterEqual(self.launcher.stops, 1)
        self.assertFalse(self.launcher.running)
        self.assertEqual(self.daemon.handoffs[-1].end_reason, 'cancelled')
        self.assertTrue(self.daemon.handoffs[-1].partial)
        await self.supervisor.cancel(meeting.id)
        self.assertFalse(self.launcher.running)

    async def test_capacity_is_exclusive_and_idempotent(self):
        first = session()
        await self.supervisor.start(first)
        await self.supervisor.start(first)
        self.assertEqual(len(self.launcher.ups), 1)
        other = session(id='mtg-other0000001')
        with self.assertRaises(DaemonError) as raised:
            await self.supervisor.start(other)
        self.assertEqual(raised.exception.code, 'capacity_exceeded')
        self.assertEqual(len(self.launcher.ups), 1)
        await self.supervisor.cancel(first.id)
        first = session()
        other = session(id='mtg-other0000002')
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

        self.launcher.fail_up = False
        self.health.payload = None
        self.supervisor.start_timeout = 0.12
        with self.assertRaises(RuntimeError):
            await self.supervisor.start(session(id='mtg-healthfail0001'))
        self.assertFalse(self.launcher.running)
        self.assertIsNone(self.supervisor._active_id)

    async def test_unexpected_exit_persists_partial_handoff(self):
        meeting = session()
        await self.supervisor.start(meeting)
        self.launcher.running = False
        await asyncio.sleep(0.08)
        self.assertEqual(self.daemon.failures, [])
        self.assertEqual(self.daemon.handoffs[0].meeting_id, meeting.id)
        self.assertTrue(self.daemon.handoffs[0].partial)
        self.assertEqual(self.daemon.handoffs[0].end_reason, 'container_exited')

    async def test_finished_without_handoff_still_builds_handoff(self):
        meeting = session()
        await self.supervisor.start(meeting)
        self.health.payload = {'stage': 'finished'}
        await asyncio.sleep(0.08)
        self.assertEqual(self.daemon.failures, [])
        self.assertEqual(self.daemon.handoffs[-1].end_reason, 'finished')
        self.assertFalse(self.daemon.handoffs[-1].partial)
        self.assertFalse(self.launcher.running)

    async def test_natural_end_releases_agent_capacity(self):
        first = session()
        await self.supervisor.start(first)
        self.health.payload = {'stage': 'meeting_ended'}
        await asyncio.sleep(0.08)
        self.assertIsNone(self.supervisor._active_id)
        self.assertFalse(self.launcher.running)
        self.assertEqual(read_json(active_meeting_path(self.root)), {})
        payload = json.loads(
            (self.runtime / 'recordings' / first.id / 'finalization.json').read_text())
        self.assertEqual(payload['status'], 'ready')

        self.health.payload = {'stage': 'starting'}
        second = session(id='mtg-nextmeeting001')
        await self.supervisor.start(second)
        self.assertEqual(self.supervisor._active_id, second.id)
        self.assertTrue(self.launcher.running)

    async def test_restart_reconciliation(self):
        meeting = session()
        self.daemon.meetings.records[meeting.id] = FakeRecord(meeting)
        await self.supervisor.reconcile(self.daemon)
        self.assertEqual(self.daemon.failures, [])
        self.assertEqual(self.daemon.handoffs[-1].meeting_id, meeting.id)
        self.assertEqual(self.daemon.handoffs[-1].end_reason, 'runtime_unavailable_on_restart')

        ended = session(id='mtg-endedalready01', state='ended')
        self.daemon.meetings.records[ended.id] = FakeRecord(ended)
        self.daemon.handoffs.clear()
        await self.supervisor.reconcile(self.daemon)
        self.assertEqual(self.daemon.handoffs, [])

        self.launcher.unknown = True
        await self.supervisor.reconcile(self.daemon)
        self.assertEqual(self.daemon.failures, [])
        self.assertEqual(self.daemon.handoffs, [])

        self.launcher.unknown = False
        self.launcher.running = True
        live = session(id='mtg-stilllive00001')
        self.daemon.meetings.records[live.id] = FakeRecord(live)
        from runtime_state import write_private_json
        write_private_json(active_meeting_path(self.root), {'meetingId': live.id})
        await self.supervisor.reconcile(self.daemon)
        self.assertEqual(self.supervisor._active_id, live.id)
        self.assertTrue(self.supervisor._watch_task)

    async def test_uncertain_running_container_is_not_taken_over(self):
        meeting = session()
        self.daemon.meetings.records[meeting.id] = FakeRecord(meeting)
        self.launcher.running = True
        await self.supervisor.reconcile(self.daemon)
        self.assertIsNone(self.supervisor._active_id)
        self.assertEqual(self.daemon.failures, [])

    async def test_token_file_permissions_and_secret_exclusion(self):
        token, path = write_auth_token(self.root, 'daemon-test-token')
        self.assertEqual(token, 'daemon-test-token')
        self.assertEqual(path, daemon_token_path(self.root))
        self.assertEqual(file_mode(path), 0o600)
        self.assertEqual(file_mode(self.root / '.smitline'), 0o700)
        self.assertEqual(path.read_text(encoding='utf-8').strip(), 'daemon-test-token')
        self.assertFalse((self.runtime / 'run' / 'daemon.auth').exists())
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

    async def test_mounted_runtime_tree_excludes_daemon_control_secrets(self):
        token, token_path = write_auth_token(self.root, 'host-only-daemon-token')
        meeting = session()
        await self.supervisor.start(meeting)
        self.assertEqual(token_path, self.root / '.smitline' / 'daemon.auth')
        self.assertTrue((self.root / '.smitline' / 'active-meeting.json').is_file())
        for path in self.runtime.rglob('*'):
            self.assertNotEqual(path.name, 'daemon.auth')
            self.assertNotEqual(path.name, 'daemon-data')
            self.assertNotEqual(path.name, 'portal-active.json')
            self.assertNotEqual(path.name, 'active-meeting.json')
            if path.is_file():
                text = path.read_text(encoding='utf-8', errors='replace')
                self.assertNotIn('host-only-daemon-token', text)
        self.assertTrue((self.runtime / 'run' / 'meetings' / meeting.id / 'runtime.json').is_file())
        self.assertEqual(file_mode(self.root / '.smitline'), 0o700)
        self.assertEqual(file_mode(token_path), 0o600)

    def test_daemon_entrypoint_keeps_control_state_host_only(self):
        from daemon_main import build_app, build_parser
        from runtime_state import daemon_data_path
        args = build_parser().parse_args([
            '--root', str(self.root), '--runtime-root', str(self.runtime),
        ])
        app, _host, _port, token_path = build_app(args)
        try:
            self.assertEqual(token_path, daemon_token_path(self.root))
            self.assertEqual(app.runtime_daemon.root, daemon_data_path(self.root).resolve())
            self.assertTrue(token_path.is_file())
            self.assertFalse((self.runtime / 'run' / 'daemon.auth').exists())
            self.assertFalse((self.runtime / 'run' / 'daemon-data').exists())
            self.assertFalse((self.runtime / 'codex-workspace').exists())
            self.assertEqual(file_mode(self.root / '.smitline'), 0o700)
            self.assertEqual(file_mode(daemon_data_path(self.root)), 0o700)
        finally:
            app.runtime_daemon.close()


class FakeRunner:
    """Records host commands and answers each with success."""

    def __init__(self):
        from meeting_supervisor import CommandResult
        self.result = CommandResult
        self.calls = []

    async def run(self, args, *, env=None):
        self.calls.append((list(args), dict(env) if env else None))
        return self.result(0, '', '')


class ComposeMeetingAgentTests(unittest.IsolatedAsyncioTestCase):
    async def test_up_builds_and_starts_the_one_meeting_image(self):
        from meeting_supervisor import ComposeMeetingAgent
        runner = FakeRunner()
        await ComposeMeetingAgent(runner, environ={}).up({'MEETING_URL': ZOOM_URL})
        commands = [args for args, _env in runner.calls]
        self.assertEqual(commands, [['docker', 'compose', '-f', 'compose.meeting.yaml',
                                     'up', '-d', '--build', 'meeting-agent']])

    async def test_the_published_image_pulls_the_meeting_image(self):
        from meeting_supervisor import CODE_ROOT, ComposeMeetingAgent
        runner = FakeRunner()
        environ = {'SMITLINE_MEETING_IMAGE': 'ghcr.io/kaelorlabs/smitline-meeting:main'}
        agent = ComposeMeetingAgent(runner, environ=environ)
        await agent.up({})
        await agent.stop()
        compose = ['docker', 'compose', '-f', str(CODE_ROOT / 'compose.meeting.image.yaml')]
        self.assertEqual([args for args, _env in runner.calls], [
            [*compose, 'up', '-d', '--pull', 'missing', 'meeting-agent'],
            [*compose, 'stop', 'meeting-agent'],
        ])

    async def test_up_runs_the_container_as_the_host_user(self):
        from meeting_supervisor import ComposeMeetingAgent
        runner = FakeRunner()
        await ComposeMeetingAgent(runner, environ={}).up({'MEETING_URL': ZOOM_URL})
        env = runner.calls[-1][1]
        self.assertEqual(env['SMITLINE_UID'], str(os.getuid()))
        self.assertEqual(env['SMITLINE_GID'], str(os.getgid()))
        self.assertEqual(env['MEETING_URL'], ZOOM_URL)
        runner = FakeRunner()
        await ComposeMeetingAgent(runner, environ={'SMITLINE_UID': '0', 'SMITLINE_GID': '0'}).up({})
        self.assertEqual(runner.calls[-1][1]['SMITLINE_UID'], '0')
        self.assertEqual(runner.calls[-1][1]['SMITLINE_GID'], '0')


class MountPreparationTests(unittest.IsolatedAsyncioTestCase):
    async def test_start_creates_private_mount_sources_before_compose(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = root / 'meeting-runtime'
            runtime.mkdir()
            (runtime / 'recordings').mkdir(mode=0o755)
            seen = {}

            class Launcher(FakeLauncher):
                async def up(self, env):
                    seen.update({name: file_mode(runtime / name)
                                 for name in ('recordings', 'profiles')})
                    await super().up(env)

            supervisor = ProductionMeetingSupervisor(
                root, runtime_root=runtime, launcher=Launcher(), health=FakeHealth(),
                poll_interval=0.02, start_timeout=0.4, stop_timeout=0.4)
            meeting = session()
            await supervisor.start(meeting)
            self.assertEqual(seen, {'recordings': 0o700, 'profiles': 0o700})
            await supervisor.shutdown()

    def test_foreign_owned_mount_source_explains_the_fix(self):
        from meeting_supervisor import ProductionMeetingSupervisor as Supervisor
        with tempfile.TemporaryDirectory() as directory:
            supervisor = Supervisor(directory, runtime_root=directory, launcher=FakeLauncher(),
                                    health=FakeHealth())
            with mock.patch('meeting_supervisor.ensure_private_dir',
                            side_effect=PermissionError('Operation not permitted')):
                with self.assertRaises(RuntimeError) as raised:
                    supervisor._prepare_mounts()
            self.assertIn('sudo chown -R', str(raised.exception))


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
            'meetingInstructions': 'Stay brief.',
        })
        env = {'SMITLINE_RUNTIME_STATE': str(self.path), 'MEETING_URL': 'https://example.com/old'}
        config = RuntimeConfig.from_environ(env)
        self.assertEqual(config.participant_name, 'Runtime Colleague')
        self.assertEqual(config.meeting_instructions, 'Stay brief.')
        self.assertEqual(resolve_meeting_url(env), ZOOM_URL)

    def test_missing_state_keeps_environ(self):
        config = RuntimeConfig.from_environ({})
        self.assertEqual(config.participant_name, 'Smitline')
        with self.assertRaises(KeyError):
            resolve_meeting_url({})


if __name__ == '__main__':
    unittest.main()
