"""Several meetings at once: capacity, ports, mounts, claims, and restarts."""
import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path

from agent_sessions import MeetingSession
from daemon_main import build_supervisor
from meeting_supervisor import (
    CODE_ROOT, CommandResult, ComposeMeetingAgent, DaemonError, MeetingPool,
    ProductionMeetingSupervisor, compose_command, pool_settings,
)
from runtime_state import active_meeting_path, file_mode, pool_claim_path, read_json, write_private_json
from test_meeting_supervisor import FakeDaemon, FakeHealth, FakeLauncher, FakeRecord, session


def owned(meeting_id, owner):
    return MeetingSession.from_dict(dict(session(id=meeting_id).to_dict(), owner=owner))


async def until(predicate, timeout=3.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError('condition not met in time')
        await asyncio.sleep(0.005)


class SlowLauncher(FakeLauncher):
    """compose up and down that take their time, so starts and stops overlap. Records the
    owner env file as compose would read it, while it exists."""

    def __init__(self, up_gate=None, stop_gate=None, fail_up=False):
        super().__init__()
        self.up_gate, self.stop_gate, self.fail_up = up_gate, stop_gate, fail_up
        self.stopping = False
        self.env_file = None

    async def up(self, env):
        path = env.get('SMITLINE_MEETING_ENV_FILE')
        if path:
            self.env_file = (path, Path(path).read_text(), file_mode(Path(path)))
        if self.up_gate is not None:
            await self.up_gate.wait()
        await super().up(env)

    async def stop(self):
        self.stopping = True
        if self.stop_gate is not None:
            await self.stop_gate.wait()
        await super().stop()


def pair(launcher):
    return int(launcher.env['SMITLINE_MEETING_VNC_PORT']), int(launcher.env['SMITLINE_MEETING_HEALTH_PORT'])


class ComposeRunner:
    """Answers `docker compose ls` with the given projects and records every command."""

    def __init__(self, projects=()):
        self.projects = list(projects)
        self.calls = []

    async def run(self, args, *, env=None):
        self.calls.append(list(args))
        if args[:3] == ['docker', 'compose', 'ls']:
            return CommandResult(0, json.dumps(self.projects), '')
        return CommandResult(0, '', '')


class PoolTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.runtime = self.root / 'meeting-runtime'
        self.runtime.mkdir()
        self.launchers = {}
        self.busy = set()
        self.runner = ComposeRunner()
        self.daemon = FakeDaemon()
        self.pool = self.make_pool()

    def make_pool(self, **overrides):
        def launcher(project, env):
            fake = FakeLauncher()
            fake.project, fake.env = project, env
            self.launchers[project] = fake
            return fake
        options = dict(runtime_root=self.runtime, runner=self.runner, environ={}, max_meetings=2,
                       ports=(18100, 18109), poll_interval=0.02, start_timeout=0.4,
                       stop_timeout=0.4, launcher_factory=launcher,
                       health_factory=lambda port: FakeHealth(),
                       port_free=lambda port: port not in self.busy)
        options.update(overrides)
        return MeetingPool(self.root, **options).bind_daemon(self.daemon)

    async def asyncTearDown(self):
        await self.pool.shutdown()
        self.temporary.cleanup()

    def slow(self, up_gate=None, stop_gate=None, fail_up=False):
        def launcher(project, env):
            fake = SlowLauncher(up_gate, stop_gate, fail_up)
            fake.project, fake.env = project, env
            self.launchers[project] = fake
            return fake
        return launcher

    async def test_meetings_started_at_the_same_time_never_share_ports(self):
        gate = asyncio.Event()
        self.pool = self.make_pool(max_meetings=6, ports=(18100, 18199), launcher_factory=self.slow(gate))
        self.busy = {18102, 18105}
        meetings = [session(id=f'mtg-race{index:012x}') for index in range(6)]
        starts = [asyncio.create_task(self.pool.start(meeting)) for meeting in meetings]
        # Every meeting has its ports while every compose up is still running.
        await until(lambda: len(self.launchers) == 6)
        gate.set()
        await asyncio.gather(*starts)
        pairs = [pair(launcher) for launcher in self.launchers.values()]
        ports = [port for both in pairs for port in both]
        self.assertEqual(len(set(ports)), 12, pairs)
        self.assertFalse(set(ports) & self.busy)
        claimed = [tuple(read_json(pool_claim_path(self.root, meeting.id))['ports'].values()) for meeting in meetings]
        self.assertEqual(sorted(claimed), sorted(pairs))

    async def test_ports_and_capacity_stay_taken_until_the_container_is_gone(self):
        stop_gate = asyncio.Event()
        self.pool = self.make_pool(max_meetings=2, ports=(18100, 18103),
                                   launcher_factory=self.slow(stop_gate=stop_gate))
        first, second, third = (session(id='mtg-first00000001'), session(id='mtg-second0000001'),
                                session(id='mtg-third00000001'))
        await self.pool.start(first)
        first_launcher = self.launchers['smitline-meeting-' + first.id]
        canceling = asyncio.create_task(self.pool.cancel(first.id))
        await until(lambda: first_launcher.stopping)
        # While the first container is still being removed, its ports and its place are taken.
        await self.pool.start(second)
        self.assertNotEqual(pair(self.launchers['smitline-meeting-' + second.id]), pair(first_launcher))
        with self.assertRaises(DaemonError) as raised:
            await self.pool.start(third)
        self.assertEqual(raised.exception.code, 'capacity_exceeded')
        stop_gate.set()
        await canceling
        await self.pool.start(third)
        self.assertEqual(pair(self.launchers['smitline-meeting-' + third.id]), pair(first_launcher))

    async def test_a_meeting_that_ends_by_itself_frees_its_ports_after_its_container(self):
        stop_gate = asyncio.Event()
        health = {}

        def health_for(port):
            health[port] = FakeHealth()
            return health[port]
        self.pool = self.make_pool(max_meetings=2, ports=(18100, 18103), health_factory=health_for,
                                   launcher_factory=self.slow(stop_gate=stop_gate))
        first, second, third = (session(id='mtg-first00000001'), session(id='mtg-second0000001'),
                                session(id='mtg-third00000001'))
        self.daemon.meetings.records[first.id] = FakeRecord(first)
        await self.pool.start(first)
        first_launcher = self.launchers['smitline-meeting-' + first.id]
        health[pair(first_launcher)[1]].payload = {'stage': 'finished'}
        await until(lambda: first_launcher.stopping)
        await self.pool.start(second)
        self.assertNotEqual(pair(self.launchers['smitline-meeting-' + second.id]), pair(first_launcher))
        stop_gate.set()
        await until(lambda: self.pool.active_ids() == (second.id,))
        self.assertFalse(pool_claim_path(self.root, first.id).exists())
        await self.pool.start(third)
        self.assertEqual(pair(self.launchers['smitline-meeting-' + third.id]), pair(first_launcher))

    async def test_the_owner_cap_holds_when_an_owner_starts_two_at_once(self):
        gate = asyncio.Event()
        self.pool = self.make_pool(max_meetings=4, per_owner=1, launcher_factory=self.slow(gate))
        meetings = [owned('mtg-owner0000000a', 'acct_a'), owned('mtg-owner0000000b', 'acct_a')]
        starts = [asyncio.create_task(self.pool.start(meeting)) for meeting in meetings]
        await until(lambda: any(task.done() for task in starts))
        gate.set()
        results = await asyncio.gather(*starts, return_exceptions=True)
        refused = [result for result in results if isinstance(result, DaemonError)]
        self.assertEqual([error.code for error in refused], ['owner_capacity_exceeded'])
        self.assertEqual(len(self.launchers), 1)
        self.assertEqual(len(self.pool.active_ids()), 1)
        self.assertEqual(sum(pool_claim_path(self.root, meeting.id).exists() for meeting in meetings), 1)

    async def test_each_owner_key_reaches_only_its_own_meeting(self):
        gate = asyncio.Event()
        keys = {'acct_a': 'sk-proj-aaaa', 'acct_b': 'sk-proj-bbbb', 'local': 'sk-proj-llll'}
        self.pool = self.make_pool(max_meetings=3, launcher_factory=self.slow(gate),
                                   owner_environment=lambda owner: {
                                       'OPENAI_API_KEY': keys[owner], 'SMITLINE_OWNER_NAME': owner.upper()})
        meetings = {'acct_a': owned('mtg-owner0000000a', 'acct_a'), 'acct_b': owned('mtg-owner0000000b', 'acct_b'),
                    'local': session(id='mtg-local00000001')}
        starts = [asyncio.create_task(self.pool.start(meeting)) for meeting in meetings.values()]
        await until(lambda: len(self.launchers) == 3 and all(launcher.env_file for launcher in self.launchers.values()))
        gate.set()
        await asyncio.gather(*starts)
        # Every pool meeting, the local owner's too, gets its own file and nothing of the others'.
        for owner, meeting in meetings.items():
            path, body, mode = self.launchers['smitline-meeting-' + meeting.id].env_file
            self.assertEqual(mode, 0o600)
            self.assertIn(f'OPENAI_API_KEY="{keys[owner]}"', body)
            for other, key in keys.items():
                if other != owner:
                    self.assertNotIn(key, body)
            self.assertIn(f'SMITLINE_OWNER_NAME="{owner.upper()}"', body)
            # The file sits in the meeting's own folder and is gone once compose read it.
            self.assertEqual(Path(path).parent.name, meeting.id)
            self.assertFalse(Path(path).exists())
        self.assertEqual(list(self.root.rglob('owner.env')), [])

    async def test_the_key_file_is_removed_when_compose_fails(self):
        self.pool = self.make_pool(launcher_factory=self.slow(fail_up=True),
                                   owner_environment=lambda owner: {'OPENAI_API_KEY': 'sk-proj-secret'})
        meeting = owned('mtg-owner0000000a', 'acct_a')
        with self.assertRaises(RuntimeError):
            await self.pool.start(meeting)
        path, body, mode = self.launchers['smitline-meeting-' + meeting.id].env_file
        self.assertEqual(mode, 0o600)
        self.assertIn('sk-proj-secret', body)
        self.assertFalse(Path(path).exists())
        self.assertEqual(list(self.root.rglob('owner.env')), [])
        self.assertEqual(self.pool.active_ids(), ())
        self.assertFalse(pool_claim_path(self.root, meeting.id).exists())

    async def test_the_real_port_probe_skips_a_port_in_use(self):
        import socket
        from meeting_supervisor import port_is_free
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as held:
            held.bind(('127.0.0.1', 0))
            held.listen()
            port = held.getsockname()[1]
            self.assertFalse(port_is_free(port))
            self.pool = self.make_pool(ports=(port, port + 1), port_free=port_is_free)
            with self.assertRaises(DaemonError) as raised:
                await self.pool.start(session())
            self.assertEqual(raised.exception.code, 'no_free_ports')
        self.assertTrue(port_is_free(port))
        await self.pool.start(session())
        self.assertEqual(pair(self.launchers['smitline-meeting-' + session().id]), (port, port + 1))

    async def test_each_meeting_gets_its_own_project_ports_and_claim(self):
        first, second = session(id='mtg-first00000001'), session(id='mtg-second0000001')
        self.busy = {18100}
        await asyncio.gather(self.pool.start(first), self.pool.start(second))
        envs = {launcher.project: launcher.env for launcher in self.launchers.values()}
        self.assertEqual(set(envs), {'smitline-meeting-mtg-first00000001',
                                     'smitline-meeting-mtg-second0000001'})
        ports = sorted((int(env['SMITLINE_MEETING_VNC_PORT']), int(env['SMITLINE_MEETING_HEALTH_PORT']))
                       for env in envs.values())
        # 18100 is taken on the host, so its pair is skipped.
        self.assertEqual(ports, [(18102, 18103), (18104, 18105)])
        claim = read_json(pool_claim_path(self.root, first.id))
        self.assertEqual(claim['meetingId'], first.id)
        self.assertEqual(claim['project'], 'smitline-meeting-mtg-first00000001')
        self.assertEqual(set(claim['ports']), {'vnc', 'health'})
        self.assertFalse(active_meeting_path(self.root).exists())

        await self.pool.cancel(first.id)
        self.assertFalse(pool_claim_path(self.root, first.id).exists())
        self.assertEqual(self.pool.active_ids(), (second.id,))
        # Its ports are free again for the next meeting.
        third = session(id='mtg-third00000001')
        await self.pool.start(third)
        self.assertEqual(self.launchers['smitline-meeting-mtg-third00000001'].env['SMITLINE_MEETING_VNC_PORT'],
                         envs['smitline-meeting-' + first.id]['SMITLINE_MEETING_VNC_PORT'])

    async def test_capacity_and_the_per_owner_cap(self):
        self.pool = self.make_pool(max_meetings=3, per_owner=1)
        await self.pool.start(owned('mtg-owner0000000a', 'acct_a'))
        with self.assertRaises(DaemonError) as raised:
            await self.pool.start(owned('mtg-owner0000000b', 'acct_a'))
        self.assertEqual((raised.exception.status, raised.exception.code), (409, 'owner_capacity_exceeded'))
        # The local owner is never held to the per-owner cap, only to the pool's size.
        await self.pool.start(session(id='mtg-local00000001'))
        await self.pool.start(session(id='mtg-local00000002'))
        with self.assertRaises(DaemonError) as raised:
            await self.pool.start(owned('mtg-owner0000000c', 'acct_c'))
        self.assertEqual(raised.exception.code, 'capacity_exceeded')
        self.assertEqual(len(self.launchers), 3)
        # Starting a running meeting again is not a second meeting.
        await self.pool.start(session(id='mtg-local00000001'))
        self.assertEqual(len(self.launchers), 3)

    async def test_no_free_ports(self):
        self.busy = set(range(18100, 18110))
        with self.assertRaises(DaemonError) as raised:
            await self.pool.start(session())
        self.assertEqual(raised.exception.code, 'no_free_ports')
        self.assertEqual(self.pool.active_ids(), ())

    async def test_a_failed_start_frees_its_place(self):
        def failing(project, env):
            fake = FakeLauncher()
            fake.fail_up = True
            return fake
        self.pool = self.make_pool(launcher_factory=failing)
        with self.assertRaises(RuntimeError):
            await self.pool.start(session())
        self.assertEqual(self.pool.active_ids(), ())
        self.assertFalse(pool_claim_path(self.root, session().id).exists())

    async def test_meetings_mount_only_their_own_folders(self):
        await self.pool.start(session(id='mtg-local00000001'))
        await self.pool.start(owned('mtg-owner0000000a', 'acct_a'))
        local = self.launchers['smitline-meeting-mtg-local00000001'].env
        other = self.launchers['smitline-meeting-mtg-owner0000000a'].env
        self.assertEqual(local['SMITLINE_MEETING_RUN_DIR'],
                         str(self.runtime / 'run' / 'meetings' / 'mtg-local00000001'))
        self.assertEqual(local['SMITLINE_MEETING_RECORDINGS_DIR'],
                         str(self.runtime / 'recordings' / 'mtg-local00000001'))
        self.assertEqual(local['SMITLINE_MEETING_PROFILES_DIR'], str(self.runtime / 'profiles'))
        # Another owner never gets the installation's signed-in profiles, only its own folder.
        self.assertEqual(other['SMITLINE_MEETING_PROFILES_DIR'],
                         str(self.root / '.smitline' / 'owners' / 'acct_a' / 'meeting-profiles'))
        self.assertEqual(other['SMITLINE_MEETING_RECORDINGS_DIR'],
                         str(self.runtime / 'recordings' / 'mtg-owner0000000a'))
        for env in (local, other):
            for key in ('SMITLINE_MEETING_RUN_DIR', 'SMITLINE_MEETING_RECORDINGS_DIR',
                        'SMITLINE_MEETING_PROFILES_DIR'):
                self.assertEqual(file_mode(Path(env[key])), 0o700, key)

    def test_the_published_image_mounts_volume_subpaths(self):
        pool = self.make_pool(environ={'SMITLINE_MEETING_IMAGE': 'ghcr.io/kaelorlabs/smitline-meeting:1'})
        env = pool._mount_env('mtg-owner0000000a', 'acct_a', (18100, 18101))
        self.assertEqual(env['SMITLINE_MEETING_RUN_DIR'], 'meeting-runtime/run/meetings/mtg-owner0000000a')
        self.assertEqual(env['SMITLINE_MEETING_RECORDINGS_DIR'], 'meeting-runtime/recordings/mtg-owner0000000a')
        self.assertEqual(env['SMITLINE_MEETING_PROFILES_DIR'], '.smitline/owners/acct_a/meeting-profiles')
        with tempfile.TemporaryDirectory() as elsewhere:
            outside = self.make_pool(environ={'SMITLINE_MEETING_IMAGE': 'x'}, runtime_root=elsewhere)
            with self.assertRaises(RuntimeError):
                outside._mount_env('mtg-owner0000000a', 'acct_a', (18100, 18101))

    async def test_reconcile_follows_live_meetings_and_removes_only_its_own_other_containers(self):
        live, gone, unclaimed = (session(id='mtg-live000000001'), session(id='mtg-gone000000001'),
                                 session(id='mtg-unclaimed0001'))
        for meeting in (live, gone, unclaimed):
            self.daemon.meetings.records[meeting.id] = FakeRecord(meeting)
        write_private_json(pool_claim_path(self.root, live.id), {
            'meetingId': live.id, 'owner': 'local', 'project': 'smitline-meeting-' + live.id,
            'ports': {'vnc': 18104, 'health': 18105}})
        write_private_json(pool_claim_path(self.root, gone.id), {
            'meetingId': gone.id, 'ports': {'vnc': 18106, 'health': 18107}})
        # A claim of this installation whose meeting record is gone: still this installation's.
        write_private_json(pool_claim_path(self.root, 'mtg-orphan0000001'), {
            'meetingId': 'mtg-orphan0000001', 'ports': {'vnc': 18108, 'health': 18109}})
        self.runner.projects = [
            {'Name': 'smitline-meeting-' + live.id, 'Status': 'running(1)'},
            {'Name': 'smitline-meeting-' + gone.id, 'Status': 'exited(0)'},
            {'Name': 'smitline-meeting-' + unclaimed.id, 'Status': 'running(1)'},
            {'Name': 'smitline-meeting-mtg-orphan0000001', 'Status': 'running(1)'},
            # Another installation's meetings on the same Docker host (a test copy, a hosted
            # deployment beside a personal one) are never this daemon's to remove.
            {'Name': 'smitline-meeting-mtg-elsewhere00001', 'Status': 'running(1)'},
            {'Name': 'smitline-meeting-not-a-meeting', 'Status': 'running(1)'},
            {'Name': 'smitline', 'Status': 'running(1)'},
        ]
        self.pool = self.make_pool()
        await self.pool.reconcile(self.daemon)
        downs = [call for call in self.runner.calls if 'down' in call]
        self.assertEqual(sorted(call[3] for call in downs),
                         ['smitline-meeting-' + gone.id, 'smitline-meeting-mtg-orphan0000001',
                          'smitline-meeting-' + unclaimed.id])
        self.assertFalse(pool_claim_path(self.root, 'mtg-orphan0000001').exists())
        # The single meeting container's project is never touched.
        self.assertFalse(any('smitline' == call[3] for call in downs if len(call) > 3))
        self.assertEqual(self.pool.active_ids(), (live.id,))
        launcher = self.launchers['smitline-meeting-' + live.id]
        self.assertEqual(launcher.env['SMITLINE_MEETING_HEALTH_PORT'], '18105')
        self.assertFalse(pool_claim_path(self.root, gone.id).exists())
        self.assertTrue(pool_claim_path(self.root, live.id).exists())
        # Meetings whose container is gone get their handoff; the live one keeps going.
        finished = {handoff.meeting_id for handoff in self.daemon.handoffs}
        self.assertEqual(finished, {gone.id, unclaimed.id})
        # The adopted meeting's ports are not handed out again.
        launcher.running = True
        await self.pool.start(session(id='mtg-another000001'))
        self.assertNotEqual(self.launchers['smitline-meeting-mtg-another000001'].env['SMITLINE_MEETING_VNC_PORT'],
                            '18104')

    async def test_reconcile_waits_when_docker_does_not_answer(self):
        class Broken(ComposeRunner):
            async def run(self, args, *, env=None):
                return CommandResult(1, '', 'Cannot connect to the Docker daemon')
        meeting = session()
        self.daemon.meetings.records[meeting.id] = FakeRecord(meeting)
        self.pool = self.make_pool(runner=Broken())
        await self.pool.reconcile(self.daemon)
        self.assertEqual(self.daemon.handoffs, [])

    async def test_reconcile_removes_nothing_when_docker_answers_nonsense(self):
        class Garbled(ComposeRunner):
            async def run(self, args, *, env=None):
                self.calls.append(list(args))
                return CommandResult(0, 'WARN[0000] not json', '')
        meeting = session()
        self.daemon.meetings.records[meeting.id] = FakeRecord(meeting)
        runner = Garbled()
        self.pool = self.make_pool(runner=runner)
        await self.pool.reconcile(self.daemon)
        self.assertFalse(any('down' in call for call in runner.calls))
        self.assertEqual(self.daemon.handoffs, [])

    async def test_reconcile_removes_its_container_when_the_meeting_record_is_unreadable(self):
        class Unreadable(type(self.daemon.meetings)):
            def get(self, meeting_id):
                raise ValueError('corrupt snapshot')
        write_private_json(pool_claim_path(self.root, 'mtg-corrupt000001'), {
            'meetingId': 'mtg-corrupt000001', 'ports': {'vnc': 18100, 'health': 18101}})
        self.daemon.meetings = Unreadable()
        self.runner.projects = [{'Name': 'smitline-meeting-mtg-corrupt000001', 'Status': 'running(1)'},
                                {'Name': 'smitline-meeting-mtg-elsewhere00001', 'Status': 'running(1)'}]
        self.pool = self.make_pool()
        await self.pool.reconcile(self.daemon)
        downs = [call[3] for call in self.runner.calls if 'down' in call]
        self.assertEqual(downs, ['smitline-meeting-mtg-corrupt000001'])
        self.assertEqual(self.pool.active_ids(), ())

    async def test_context_and_cancel_of_a_meeting_without_a_container(self):
        meeting = session()
        self.daemon.meetings.records[meeting.id] = FakeRecord(meeting)
        await self.pool.add_context(meeting.id, meeting.context)
        await self.pool.cancel(meeting.id)
        self.assertEqual(self.daemon.handoffs[-1].meeting_id, meeting.id)
        self.assertEqual(self.launchers, {})


class PoolComposeTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_pool_meeting_is_its_own_compose_project(self):
        runner = ComposeRunner()
        env = {'SMITLINE_MEETING_ID': 'mtg-first00000001', 'SMITLINE_MEETING_VNC_PORT': '18100'}
        agent = ComposeMeetingAgent(runner, environ={}, project='smitline-meeting-mtg-first00000001', env=env)
        await agent.up({'MEETING_URL': 'x'})
        await agent.inspect()
        await agent.stop()
        compose = ['docker', 'compose', '-p', 'smitline-meeting-mtg-first00000001',
                   '-f', 'compose.meeting.pool.yaml']
        self.assertEqual(runner.calls, [
            [*compose, 'up', '-d', '--build', 'meeting-agent'],
            [*compose, 'ps', '-a', '--format', 'json'],
            # A finished pool meeting's container is removed, not only stopped.
            [*compose, 'down', '--remove-orphans'],
        ])
        image, _ = compose_command({'SMITLINE_MEETING_IMAGE': 'x'}, project='p')
        self.assertEqual(image, ['docker', 'compose', '-p', 'p', '-f',
                                 str(CODE_ROOT / 'compose.meeting.pool.image.yaml')])

    def test_pool_compose_files_mount_no_shared_data(self):
        for name in ('compose.meeting.pool.yaml', 'compose.meeting.pool.image.yaml'):
            if not (CODE_ROOT / name).is_file():
                self.skipTest('the compose files are not in the meeting image')
            text = (CODE_ROOT / name).read_text()
            self.assertNotIn('6082', text)
            self.assertNotIn('8094:8094', text)
            self.assertNotIn('- data:/data', text)
            self.assertNotIn('SMITLINE_AUTH_MODE', text)
            for key in ('SMITLINE_MEETING_VNC_PORT', 'SMITLINE_MEETING_HEALTH_PORT',
                        'SMITLINE_MEETING_RUN_DIR', 'SMITLINE_MEETING_RECORDINGS_DIR',
                        'SMITLINE_MEETING_PROFILES_DIR', 'SMITLINE_MEETING_ENV_FILE'):
                self.assertIn(key, text, (name, key))


    def test_pool_compose_files_read_no_installation_env(self):
        for name in ('compose.meeting.pool.yaml', 'compose.meeting.pool.image.yaml'):
            if not (CODE_ROOT / name).is_file():
                self.skipTest('the compose files are not in the meeting image')
            text = (CODE_ROOT / name).read_text()
            # Only the meeting's own env file: never .env or .env.meeting wholesale.
            self.assertNotIn('/.env', text, name)
            self.assertNotIn('path: .env', text, name)
            self.assertIn('path: ${SMITLINE_MEETING_ENV_FILE', text, name)


# Values an installation keeps in .env (and in the daemon's own environment) that no meeting
# container may ever see: provider keys, SIP and webhook secrets, the hosted service's token,
# phone numbers, and anything else that happens to be there.
INSTALLATION_SECRETS = {
    'TWILIO_ACCOUNT_SID': 'AC-test-account-sid-not-real', 'TWILIO_AUTH_TOKEN': 'tw-auth-secret',
    'TWILIO_FROM_NUMBER': '+15005550006', 'SIGNALWIRE_SPACE': 'secret-space',
    'SIGNALWIRE_PROJECT_ID': 'sw-project-secret', 'SIGNALWIRE_API_TOKEN': 'PTsw-token-secret',
    'SIGNALWIRE_FROM_NUMBER': '+15005550007', 'SIGNALWIRE_SIGNING_KEY': 'PSKsw-signing-secret',
    'SMITLINE_SIP_TRUNK_URL': 'sip:trunk-secret@sip.example.com', 'SMITLINE_SIP_USERNAME': 'sip-user-secret',
    'SMITLINE_SIP_PASSWORD': 'sip-password-secret', 'OPENAI_WEBHOOK_SECRET': 'whsec_openai-secret',
    'OPENAI_PROJECT_ID': 'proj_openai-secret', 'SMITLINE_CLOUD_API': 'http://cloud-api-secret:8080',
    'SMITLINE_CLOUD_INTERNAL_TOKEN': 'cloud-internal-secret', 'SMITLINE_CALL_HOOKS': 'secret_hooks:create',
    'SMITLINE_NOTIFY_WEBHOOK': 'https://hooks.example.com/notify-secret',
    'SMITLINE_PUBLIC_URL': 'https://public-secret.example.com', 'SMITLINE_CALLER_ID': '+15005550008',
    'SMITLINE_OWNER_PHONE': '+14155550111', 'AWS_SECRET_ACCESS_KEY': 'aws-secret-value',
    'GITHUB_TOKEN': 'ghp_github-secret', 'DATABASE_PASSWORD': 'db-password-secret',
}


class PoolEnvironmentTests(unittest.IsolatedAsyncioTestCase):
    """A pool meeting's container gets the OpenAI key and the settings the meeting reads, and
    nothing else of the installation's .env, .env.meeting, or the daemon's environment."""

    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.runtime = self.root / 'meeting-runtime'
        self.runtime.mkdir()
        dotenv = {**INSTALLATION_SECRETS, 'OPENAI_API_KEY': 'sk-proj-installation',
                  'SMITLINE_VOICE': 'cinder', 'SMITLINE_OWNER_NAME': 'Robin',
                  'SMITLINE_PARTICIPANT_NAME': "Robin's notetaker",
                  'SMITLINE_MEETING_BACKEND_MODEL': 'gpt-5.6-terra', 'SMITLINE_MEETING_INTRO': '0'}
        (self.root / '.env').write_text(''.join(f'{key}={value}\n' for key, value in dotenv.items()))
        (self.root / '.env.meeting').write_text(
            'SMITLINE_MEETING_INSTRUCTIONS=Take notes only.\nSMITLINE_VOICE=quartz\n'
            'MEETING_URL=https://zoom.us/j/111111111\nMEETING_PASSCODE=passcode-secret\n'
            'SMITLINE_AUTH_MODE=google\nSIGNALWIRE_API_TOKEN=PTsw-in-env-meeting-secret\n')
        from call_hooks import DefaultCallHooks
        self.hooks = DefaultCallHooks(
            environ={'SMITLINE_CLOUD_INTERNAL_TOKEN': 'cloud-internal-from-process-secret',
                     'SMITLINE_MEETING_CAMERA_STYLE': 'animated'},
            env_file=self.root / '.env')
        self.launchers = {}

        def launcher(project, env):
            fake = SlowLauncher()
            fake.project, fake.env = project, env
            self.launchers[project] = fake
            return fake
        from meeting_line import pool_meeting_environment
        self.pool = MeetingPool(
            self.root, runtime_root=self.runtime, runner=ComposeRunner(), environ={}, max_meetings=3,
            ports=(18100, 18109), poll_interval=0.02, start_timeout=0.4, stop_timeout=0.4,
            launcher_factory=launcher, health_factory=lambda port: FakeHealth(), port_free=lambda port: True,
            owner_environment=lambda owner: pool_meeting_environment(
                self.hooks, owner, self.root / '.env.meeting'),
        ).bind_daemon(FakeDaemon())

    async def asyncTearDown(self):
        await self.pool.shutdown()
        self.temporary.cleanup()

    async def container_env(self, meeting):
        from test_owner_isolation import parse_env_file
        await self.pool.start(meeting)
        captured = self.launchers['smitline-meeting-' + meeting.id].env_file
        self.assertIsNotNone(captured, f'{meeting.owner} meeting got no env file of its own')
        path, body, mode = captured
        self.assertEqual(mode, 0o600)
        self.assertFalse(Path(path).exists())
        for name, value in INSTALLATION_SECRETS.items():
            self.assertNotIn(value, body, name)
        for leaked in ('cloud-internal-from-process-secret', 'passcode-secret', 'PTsw-in-env-meeting-secret',
                       'MEETING_URL', 'SMITLINE_AUTH_MODE'):
            self.assertNotIn(leaked, body)
        return parse_env_file(body)

    async def test_no_installation_secret_reaches_a_pool_meeting(self):
        from meeting_line import POOL_MEETING_SETTINGS
        mine = await self.container_env(session(id='mtg-local00000001'))
        theirs = await self.container_env(owned('mtg-owner0000000a', 'acct_a'))
        allowed = set(POOL_MEETING_SETTINGS) | {'OPENAI_API_KEY'}
        self.assertLessEqual(set(mine), allowed)
        self.assertLessEqual(set(theirs), allowed)
        # The local owner's meeting reads what one container at a time would: .env, then
        # .env.meeting over it, plus the daemon's own environment for the allowed settings.
        self.assertEqual(mine, {
            'OPENAI_API_KEY': 'sk-proj-installation', 'SMITLINE_VOICE': 'quartz',
            'SMITLINE_OWNER_NAME': 'Robin', 'SMITLINE_PARTICIPANT_NAME': "Robin's notetaker",
            'SMITLINE_MEETING_BACKEND_MODEL': 'gpt-5.6-terra', 'SMITLINE_MEETING_INTRO': '0',
            'SMITLINE_MEETING_INSTRUCTIONS': 'Take notes only.', 'SMITLINE_MEETING_CAMERA_STYLE': 'animated'})
        # Another owner's per-owner settings come from its own settings, never .env.meeting.
        self.assertEqual(theirs['SMITLINE_VOICE'], 'cinder')
        self.assertEqual(theirs['SMITLINE_MEETING_INSTRUCTIONS'], 'Take notes only.')
        # The local owner's meetings still join as Smitline; another owner's under its name.
        names = {meeting_id: read_json(self.runtime / 'run' / 'meetings' / meeting_id / 'runtime.json')['participantName']
                 for meeting_id in ('mtg-local00000001', 'mtg-owner0000000a')}
        self.assertEqual(names, {'mtg-local00000001': 'Smitline', 'mtg-owner0000000a': "Robin's notetaker"})

    def test_the_meeting_reads_nothing_outside_its_allowlist(self):
        from meeting_line import POOL_MEETING_SETTINGS
        reads = meeting_environment_reads()
        allowed = set(POOL_MEETING_SETTINGS) | {'OPENAI_API_KEY'}
        known = allowed | MEETING_ENV_SET_INSIDE | MEETING_ENV_NOT_IN_THE_POOL
        unknown = {name: places for name, places in reads.items() if name not in known}
        self.assertEqual(unknown, {}, 'the meeting reads a variable the pool does not pass: add it to '
                                      'POOL_MEETING_SETTINGS (meeting_line.py) or to the lists here')
        self.assertEqual(allowed - set(reads), set(), 'POOL_MEETING_SETTINGS names a variable nothing reads')


# What the meeting container sets for itself (or compose sets), and what only a meeting
# started by hand or a sign-in reads: none of it comes from the installation's settings.
MEETING_ENV_SET_INSIDE = {'SMITLINE_RUNTIME_STATE', 'SMITLINE_CONTROL_TOKEN', 'DISPLAY', 'WAYLAND_DISPLAY', 'XDG_SESSION_TYPE',
                          'PULSE_SOURCE', 'PULSE_SERVER', 'JOINLY_BROWSER_PROFILE_DIR', 'JOINLY_NAME'}
MEETING_ENV_NOT_IN_THE_POOL = {
    'MEETING_URL', 'MEETING_PASSCODE',  # a meeting started by hand (.env.meeting); calls use the runtime state
    'SMITLINE_AUTH_MODE',               # signing the bot in, one container at a time
    'SMITLINE_ROOT',                    # preflight.py, which runs on the host
}
# Imported by the meeting for constants and types; it never runs their environment reads.
DAEMON_ONLY_MODULES = {'call_hooks.py', 'call_policy.py'}


def meeting_environment_reads():
    """{variable: [file:line]} for every environment variable read by code the meeting
    container runs: bridge.py (and the sign-in helpers) and what they import from
    meeting-runtime/ and joinly/."""
    import ast
    import importlib.util
    runtime = Path(__file__).resolve().parent
    spec = importlib.util.find_spec('joinly')
    joinly_root = Path(spec.origin).parent.parent if spec and spec.origin else runtime.parent / 'joinly'
    roots = (runtime, joinly_root)

    def resolve(name):
        for root in roots:
            for candidate in (root.joinpath(*name.split('.')).with_suffix('.py'),
                              root.joinpath(*name.split('.'), '__init__.py')):
                if candidate.is_file():
                    return candidate
        return None
    queue = [runtime / name for name in ('bridge.py', 'google_account.py', 'teams_account.py')]
    seen = set()
    while queue:
        path = queue.pop()
        if path in seen:
            continue
        seen.add(path)
        for node in ast.walk(ast.parse(path.read_text(encoding='utf-8'))):
            names = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
                names = [node.module] + [f'{node.module}.{alias.name}' for alias in node.names]
            queue += [found for found in map(resolve, names) if found is not None]
    upper = lambda node: (isinstance(node, ast.Constant) and isinstance(node.value, str)
                          and node.value.isupper() and len(node.value) > 2)
    envish = lambda node: 'env' in ast.unparse(node).lower()
    reads = {}
    for path in sorted(seen):
        if path.name in DAEMON_ONLY_MODULES:
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding='utf-8'))):
            name = None
            if isinstance(node, ast.Call) and node.args and upper(node.args[0]) and (
                    envish(node.func) or ast.unparse(node.func) in ('getenv', 'os.getenv')):
                name = node.args[0].value
            elif isinstance(node, ast.Subscript) and upper(node.slice) and envish(node.value):
                name = node.slice.value
            elif isinstance(node, ast.Compare) and upper(node.left) and \
                    any(isinstance(op, (ast.In, ast.NotIn)) for op in node.ops) and \
                    any(envish(comparator) for comparator in node.comparators):
                name = node.left.value
            if name:
                reads.setdefault(name, []).append(f'{path.name}:{node.lineno}')
    return reads


class OneContainerTests(unittest.IsolatedAsyncioTestCase):
    """One meeting container at a time (SMITLINE_MAX_MEETINGS=1) uses the installation's
    signed-in browser profiles and sees the whole data volume, so it runs the local owner's
    meetings only; another owner's need the pool."""

    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.runtime = self.root / 'meeting-runtime'
        self.runtime.mkdir()
        self.launcher = FakeLauncher()
        self.supervisor = ProductionMeetingSupervisor(
            self.root, runtime_root=self.runtime, launcher=self.launcher, health=FakeHealth(),
            poll_interval=0.02, start_timeout=0.4, stop_timeout=0.4,
            owner_environment=lambda owner: {'OPENAI_API_KEY': 'sk-proj-owner'}).bind_daemon(FakeDaemon())

    async def asyncTearDown(self):
        await self.supervisor.shutdown()
        self.temporary.cleanup()

    async def test_the_single_container_refuses_another_owner_meeting(self):
        with self.assertRaises(DaemonError) as raised:
            await self.supervisor.start(owned('mtg-owner0000000a', 'acct_a'))
        self.assertEqual((raised.exception.status, raised.exception.code), (409, 'meeting_pool_required'))
        self.assertIn('SMITLINE_MAX_MEETINGS', raised.exception.message)
        self.assertEqual(self.launcher.ups, [])
        self.assertFalse(active_meeting_path(self.root).exists())
        self.assertEqual(list(self.runtime.rglob('*.json')) + list(self.runtime.rglob('*.env')), [])
        # The local owner's meetings run exactly as before: no env file of their own.
        await self.supervisor.start(session())
        self.assertNotIn('SMITLINE_MEETING_ENV_FILE', self.launcher.ups[0])
        await self.supervisor.cancel(session().id)

    async def test_calls_and_checks_refuse_it_before_anything_starts(self):
        from aiohttp.test_utils import TestClient, TestServer
        from api_tokens import ApiTokenStore
        from call_hooks import DefaultCallHooks
        from call_service import CallService
        from call_store import CallStore
        from meeting_line import MeetingLine
        from runtime_daemon import create_app
        from test_call_service import ZOOM, FakeSummarizer
        store = CallStore(self.root / 'calls')
        hooks = DefaultCallHooks(environ={'OPENAI_API_KEY': 'sk-test'}, store=store)
        tokens = ApiTokenStore(self.root / 'api-tokens.json')
        internal, _ = tokens.create('gateway', kind='internal')
        brief = {'channel': 'meeting', 'to': ZOOM, 'onBehalfOf': 'Ana', 'objective': 'Take notes'}

        async def ask(supervisor, owner, start=False):
            """POST /v1/calls/check (and /v1/calls when start) as the owner, through a daemon
            whose meetings go to `supervisor`."""
            app = create_app(root=self.root / f'daemon-{id(supervisor)}', auth_token='launch-token',
                             supervisor=supervisor, api_tokens=tokens, call_service_factory=lambda daemon: CallService(
                                 store, hooks=hooks, lines={'meeting': MeetingLine(daemon)},
                                 summarizer_factory=lambda name: FakeSummarizer(), environ={}))
            supervisor.bind_daemon(app.runtime_daemon)
            headers = {'Authorization': 'Bearer launch-token'} if owner == 'local' else \
                {'Authorization': f'Bearer {internal}', 'X-Smitline-Owner': owner}
            async with TestClient(TestServer(app)) as client:
                checked = await (await client.post('/v1/calls/check', json=brief, headers=headers)).json()
                if not start:
                    return checked, None
                started = await client.post('/v1/calls', json=brief, headers=headers)
                return checked, (started.status, await started.json())
        message = 'Meetings for other owners need the meeting pool: set SMITLINE_MAX_MEETINGS to 2 or more.'
        checked, (status, payload) = await ask(self.supervisor, 'acct_a', start=True)
        self.assertEqual((checked['ok'], checked['problems']), (False, [message]))
        self.assertEqual((status, payload['error']), (409, {'code': 'meeting_pool_required', 'message': message}))
        self.assertEqual(store.list(limit=None), [])
        self.assertEqual(self.launcher.ups, [])
        checked, _ = await ask(self.supervisor, 'local')
        self.assertEqual((checked['ok'], checked['problems']), (True, []))
        # With the pool (or SMITLINE_REQUIRE_OWNER=1, which always uses it) the same meeting is fine.
        pool = MeetingPool(self.root, runtime_root=self.runtime, runner=ComposeRunner(), environ={})
        checked, _ = await ask(pool, 'acct_a')
        self.assertEqual((checked['ok'], checked['problems']), (True, []))


class PoolCapacityTests(unittest.IsolatedAsyncioTestCase):
    """A meeting the pool cannot run now is refused when the call is placed, with its code and
    nothing recorded; the pool decides again at start, for calls that race."""

    async def asyncSetUp(self):
        from api_tokens import ApiTokenStore
        from call_hooks import DefaultCallHooks
        from call_store import CallStore
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.runtime = self.root / 'meeting-runtime'
        self.runtime.mkdir()
        self.store = CallStore(self.root / 'calls')
        self.hooks = DefaultCallHooks(environ={'OPENAI_API_KEY': 'sk-test'}, store=self.store)
        self.tokens = ApiTokenStore(self.root / 'api-tokens.json')
        self.internal, _ = self.tokens.create('gateway', kind='internal')
        self.launches = []
        self.pools = []

    async def asyncTearDown(self):
        for pool in self.pools:
            await pool.shutdown()
        self.temporary.cleanup()

    def pool(self, **options):
        def launcher(project, env):
            self.launches.append(project)
            return FakeLauncher()
        settings = dict(runtime_root=self.runtime, runner=ComposeRunner(), environ={}, max_meetings=2,
                        ports=(18100, 18109), poll_interval=0.02, start_timeout=0.4, stop_timeout=0.4,
                        launcher_factory=launcher, health_factory=lambda port: FakeHealth(),
                        port_free=lambda port: True)
        settings.update(options)
        pool = MeetingPool(self.root, **settings)
        self.pools.append(pool)
        return pool

    async def running(self, pool, meeting_id, owner):
        """A meeting already running in the pool."""
        pool.bind_daemon(FakeDaemon())
        await pool.start(owned(meeting_id, owner))

    async def place(self, pool, owner):
        """POST /v1/calls/check and /v1/calls for a meeting as `owner`, through a daemon whose
        meetings go to `pool`."""
        from aiohttp.test_utils import TestClient, TestServer
        from call_service import CallService
        from meeting_line import MeetingLine
        from runtime_daemon import create_app
        from test_call_service import ZOOM, FakeSummarizer
        app = create_app(root=self.root / f'daemon-{id(pool)}', auth_token='launch-token', supervisor=pool,
                         api_tokens=self.tokens, call_service_factory=lambda daemon: CallService(
                             self.store, hooks=self.hooks, lines={'meeting': MeetingLine(daemon)},
                             summarizer_factory=lambda name: FakeSummarizer(), environ={}, shutdown_grace=0))
        pool.bind_daemon(app.runtime_daemon)
        brief = {'channel': 'meeting', 'to': ZOOM, 'onBehalfOf': 'Ana', 'objective': 'Take notes'}
        headers = {'Authorization': f'Bearer {self.internal}', 'X-Smitline-Owner': owner}
        async with TestClient(TestServer(app)) as client:
            checked = await (await client.post('/v1/calls/check', json=brief, headers=headers)).json()
            if checked['ok']:
                return checked, None, None
            response = await client.post('/v1/calls', json=brief, headers=headers)
            return checked, response.status, await response.json()

    async def assert_refused(self, pool, owner, status, code, message):
        launched = list(self.launches)
        checked, got, payload = await self.place(pool, owner)
        self.assertEqual((checked['ok'], checked['problems']), (False, [message]))
        self.assertEqual((got, payload['error']['code'], payload['error']['message']), (status, code, message))
        self.assertEqual(self.store.list(limit=None), [])
        self.assertEqual(self.launches, launched)

    async def test_a_full_pool_refuses_the_call_up_front(self):
        pool = self.pool(max_meetings=1)
        await self.running(pool, 'mtg-running000001', 'acct_b')
        await self.assert_refused(pool, 'acct_a', 409, 'capacity_exceeded', 'all 1 meeting agents are busy')

    async def test_an_owner_at_its_cap_is_refused_up_front_and_others_are_not(self):
        pool = self.pool(max_meetings=3, per_owner=1)
        await self.running(pool, 'mtg-running000001', 'acct_a')
        await self.assert_refused(pool, 'acct_a', 409, 'owner_capacity_exceeded',
                                  'this owner already has 1 meetings running')
        checked, _status, _payload = await self.place(pool, 'acct_b')
        self.assertEqual((checked['ok'], checked['problems']), (True, []))

    async def test_no_free_ports_is_refused_up_front(self):
        pool = self.pool(port_free=lambda port: False)
        await self.assert_refused(pool, 'acct_a', 503, 'no_free_ports',
                                  'no two free ports for a meeting in SMITLINE_MEETING_PORTS 18100-18109')


class SettingsTests(unittest.TestCase):
    def test_one_meeting_at_a_time_is_the_default(self):
        self.assertEqual(pool_settings({}), {'max_meetings': 1, 'per_owner': None, 'ports': (18100, 18399)})
        self.assertEqual(pool_settings({'SMITLINE_MAX_MEETINGS': '4', 'SMITLINE_MAX_MEETINGS_PER_OWNER': '2',
                                        'SMITLINE_MEETING_PORTS': '20000-20099'}),
                         {'max_meetings': 4, 'per_owner': 2, 'ports': (20000, 20099)})
        for bad in ({'SMITLINE_MAX_MEETINGS': '0'}, {'SMITLINE_MAX_MEETINGS': 'many'},
                    {'SMITLINE_MAX_MEETINGS_PER_OWNER': '-1'}, {'SMITLINE_MEETING_PORTS': '80-90'},
                    {'SMITLINE_MEETING_PORTS': '20000'}, {'SMITLINE_MEETING_PORTS': '20099-20000'}):
            with self.subTest(env=bad), self.assertRaises(ValueError):
                pool_settings(bad)

    def test_the_daemon_picks_the_single_supervisor_or_the_pool(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.assertIsInstance(build_supervisor(root, root / 'meeting-runtime', {}),
                                  ProductionMeetingSupervisor)
            pool = build_supervisor(root, root / 'meeting-runtime', {'SMITLINE_MAX_MEETINGS': '3'})
            self.assertIsInstance(pool, MeetingPool)
            self.assertEqual(pool.max_meetings, 3)
            # A deployment that serves accounts keeps meetings apart even one at a time.
            hosted = build_supervisor(root, root / 'meeting-runtime', {'SMITLINE_REQUIRE_OWNER': '1'})
            self.assertIsInstance(hosted, MeetingPool)
            self.assertEqual(hosted.max_meetings, 1)


if __name__ == '__main__':
    unittest.main()
