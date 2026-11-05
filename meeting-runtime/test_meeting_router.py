"""Meetings on the launcher's machines for some platforms and in this computer's pool for the
others (SMITLINE_MEETING_LAUNCHER_PLATFORMS, MeetingRouter), and the meeting's time limit in the
start package (maxMinutes)."""
import asyncio
import itertools
import json
import sys
import tempfile
import unittest
from pathlib import Path

from agent_sessions import MeetingSession
from daemon_main import build_supervisor
from meeting_line import meeting_payload
from meeting_supervisor import (
    DaemonError, MeetingPool, MeetingRouter, RemoteMeetingPool, launcher_platforms,
    remote_token_path,
)
from runtime_daemon import RuntimeDaemon
from runtime_state import pool_claim_path
from test_meeting_pool import ComposeRunner, until
from test_meeting_supervisor import FakeDaemon, FakeHealth, FakeLauncher, FakeRecord
from test_remote_meetings import FakeProvider, owner_environment
from test_runtime_daemon import create_payload
from test_schemas import meeting_session_payload

DAILY = 'https://smitline.daily.co/ab12cd34'
BROWSER = frozenset({'zoom', 'teams', 'meet'})


URLS = {'daily': DAILY, 'meet': 'https://meet.google.com/abc-defg-hij'}


def meeting(meeting_id, owner, platform='zoom'):
    fields = {'id': meeting_id, 'owner': owner}
    if platform in URLS:
        fields.update(platform=platform, meetingUrl=URLS[platform])
    return MeetingSession.from_dict(meeting_session_payload(**fields))


class UrlHealth(FakeHealth):
    """A pool container's /health stand-in, at the address the pool gives it."""

    def __init__(self, port):
        super().__init__()
        self.url = f'http://127.0.0.1:{port}/health'


class RouterCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.runtime = self.root / 'meeting-runtime'
        self.runtime.mkdir()
        self.disk = self.root / 'machine-disk'
        self.daemon = FakeDaemon()
        self.containers = {}
        self.routers, self.providers = [], []
        self.router = self.make_router()

    async def asyncTearDown(self):
        for router in self.routers:
            await router.shutdown()
        for provider in self.providers:
            for machine in list(provider.machines.values()) + list(provider.existing.values()):
                await machine.vanish()
        self.temporary.cleanup()

    def make_router(self, *, max_meetings=4, per_owner=1, provider=None, runner=None,
                    fail_room=False):
        def container(project, env):
            # The same container after a restart: it ran on while no daemon followed it.
            fake = self.containers.get(project) or FakeLauncher()
            fake.fail_up = fail_room
            fake.project, fake.env = project, env
            self.containers[project] = fake
            return fake
        local = MeetingPool(self.root, runtime_root=self.runtime, runner=runner or ComposeRunner(),
                            environ={}, max_meetings=max_meetings, per_owner=per_owner,
                            ports=(18100, 18119), poll_interval=0.02, start_timeout=0.4,
                            stop_timeout=0.4, launcher_factory=container, health_factory=UrlHealth,
                            port_free=lambda port: True)
        self.provider = provider or FakeProvider(disk=self.disk)
        self.providers.append(self.provider)
        remote = RemoteMeetingPool(self.root, launcher=self.provider, runtime_root=self.runtime,
                                   max_meetings=max_meetings, per_owner=per_owner,
                                   poll_interval=0.02, start_timeout=5.0, stop_timeout=1.0,
                                   archive_timeout=5.0)
        router = MeetingRouter(local, remote, remote_platforms=BROWSER,
                               max_meetings=max_meetings, per_owner=per_owner)
        router.owner_environment = owner_environment
        router.bind_daemon(self.daemon)
        self.routers.append(router)
        return router

    async def started(self, session, router=None):
        self.daemon.meetings.records[session.id] = FakeRecord(session)
        await (router or self.router).start(session)
        return session

    def handoff(self, meeting_id):
        record = self.daemon.meetings.get(meeting_id)
        return None if record is None else record.handoff


class RoutingTests(RouterCase):
    async def test_a_room_runs_here_and_a_browser_meeting_on_a_machine_of_its_own(self):
        room = await self.started(meeting('mtg-room000000001', 'acct_a', 'daily'))
        zoom = await self.started(meeting('mtg-zoom000000001', 'acct_b'))
        # The room: a container of this computer's pool, with its ports and env file.
        container = self.containers['smitline-meeting-' + room.id]
        self.assertTrue(container.running)
        self.assertIn('SMITLINE_MEETING_HEALTH_PORT', container.env)
        self.assertNotIn(room.id, self.provider.machines)
        # The Zoom meeting: the launcher's machine, with its start package and nothing here.
        machine = self.provider.machines[zoom.id]
        self.assertEqual(machine.package['meetingId'], zoom.id)
        self.assertEqual(machine.package['environment']['OPENAI_API_KEY'], 'sk-test-key-of-acct_b')
        self.assertNotIn('smitline-meeting-' + zoom.id, self.containers)
        self.assertEqual(set(self.router.active_ids()), {room.id, zoom.id})
        claim = json.loads(pool_claim_path(self.root, zoom.id).read_text())
        self.assertTrue(claim['remote'])
        self.assertIn('ports', json.loads(pool_claim_path(self.root, room.id).read_text()))

    async def test_each_meeting_is_followed_ended_and_heard_where_it_runs(self):
        room = await self.started(meeting('mtg-room000000001', 'acct_a', 'daily'))
        zoom = await self.started(meeting('mtg-zoom000000001', 'acct_b'))
        machine = self.provider.machines[zoom.id]
        token = machine.package['controlToken']
        self.assertEqual(self.router.control_headers(zoom.id), {'Authorization': 'Bearer ' + token})
        self.assertTrue(self.router.listen_url(zoom.id).startswith(machine.health_url[:-len('/health')]))
        self.assertEqual(self.router.listen_url(room.id), 'http://127.0.0.1:18101/listen')
        self.assertEqual(await self.router.control(zoom.id, {'type': 'following', 'value': True}),
                         {'following': True})
        await self.router.cancel(zoom.id)
        self.assertGreaterEqual(machine.stops, 1)
        self.assertTrue(self.containers['smitline-meeting-' + room.id].running)
        await self.router.cancel(room.id)
        self.assertFalse(self.containers['smitline-meeting-' + room.id].running)
        self.assertEqual(self.router.active_ids(), ())

    async def test_a_meeting_ended_before_either_pool_ran_it_still_gets_its_handoff(self):
        never = meeting('mtg-zoom000000009', 'acct_a')
        self.daemon.meetings.records[never.id] = FakeRecord(never)
        await self.router.cancel(never.id)
        self.assertEqual(self.handoff(never.id).end_reason, 'cancelled')
        self.assertEqual(self.provider.machines, {})

    async def test_rooms_start_at_once_and_machines_in_the_background(self):
        room = meeting('mtg-room000000001', 'acct_a', 'daily')
        zoom = meeting('mtg-zoom000000001', 'acct_a')
        self.assertFalse(self.router.starts_in_background(room))
        self.assertTrue(self.router.starts_in_background(zoom))
        self.assertTrue(self.router.starts_in_background(meeting('mtg-meet000000001', 'acct_a', 'meet')))
        self.assertFalse(self.router.local_only)


class CapacityTests(RouterCase):
    async def test_the_limit_counts_rooms_and_machines_together(self):
        self.router = self.make_router(max_meetings=2, per_owner=None)
        await self.started(meeting('mtg-room000000001', 'acct_a', 'daily'))
        await self.started(meeting('mtg-zoom000000001', 'acct_b'))
        for third in (meeting('mtg-room000000002', 'acct_c', 'daily'), meeting('mtg-zoom000000002', 'acct_d')):
            with self.subTest(platform=third.platform):
                self.assertEqual(self.router.capacity_problem('acct_c')[:2], (409, 'capacity_exceeded'))
                with self.assertRaises(DaemonError) as caught:
                    await self.started(third)
                self.assertEqual(caught.exception.code, 'capacity_exceeded')
        self.assertNotIn('mtg-zoom000000002', self.provider.machines)

    async def test_one_meeting_per_account_whatever_its_platform(self):
        await self.started(meeting('mtg-room000000001', 'acct_a', 'daily'))
        self.assertEqual(self.router.capacity_problem('acct_a')[:2], (409, 'owner_capacity_exceeded'))
        self.assertIsNone(self.router.capacity_problem('acct_b'))
        with self.assertRaises(DaemonError) as caught:
            await self.started(meeting('mtg-zoom000000001', 'acct_a'))
        self.assertEqual(caught.exception.code, 'owner_capacity_exceeded')
        self.assertEqual(self.provider.machines, {})
        await self.started(meeting('mtg-zoom000000002', 'acct_b'))  # another account still may

    async def test_meetings_that_start_together_never_pass_the_limit(self):
        self.router = self.make_router(max_meetings=2, per_owner=None)
        self.provider.boot.clear()  # every machine is still booting while the others ask
        sessions = [meeting(f'mtg-zoom00000000{index}', f'acct_{index}0000000000') for index in range(4)]
        sessions.append(meeting('mtg-room000000001', 'acct_r0000000000', 'daily'))
        for session in sessions:
            self.daemon.meetings.records[session.id] = FakeRecord(session)
        starts = [asyncio.create_task(self.router.start(session)) for session in sessions]
        await until(lambda: sum(1 for task in starts if task.done()) >= 3)
        refused = [task for task in starts if task.done() and isinstance(task.exception(), DaemonError)]
        self.assertEqual(len(refused), 3)
        self.assertEqual(len(self.router.active_ids()), 2)
        self.provider.boot.set()
        await asyncio.gather(*starts, return_exceptions=True)

    async def test_a_meeting_that_ended_frees_its_place(self):
        self.router = self.make_router(max_meetings=1)
        zoom = await self.started(meeting('mtg-zoom000000001', 'acct_a'))
        self.assertEqual(self.router.capacity_problem('acct_b')[1], 'capacity_exceeded')
        await self.router.cancel(zoom.id)
        self.assertIsNone(self.router.capacity_problem('acct_a'))
        await self.started(meeting('mtg-room000000001', 'acct_a', 'daily'))


class RestartTests(RouterCase):
    async def test_a_restart_adopts_both_kinds_and_finishes_neither(self):
        room = await self.started(meeting('mtg-room000000001', 'acct_a', 'daily'))
        zoom = await self.started(meeting('mtg-zoom000000001', 'acct_b'))
        gone = meeting('mtg-zoom000000002', 'acct_c')  # its machine is no longer running
        self.daemon.meetings.records[gone.id] = FakeRecord(gone)
        pool_claim_path(self.root, gone.id).write_text('{"meetingId": "%s"}' % gone.id)
        machine = self.provider.machines[zoom.id]
        await self.router.shutdown()
        # A new daemon on the same folders: the room's container and the machine run on.
        provider = FakeProvider(disk=self.disk, existing={zoom.id: machine})
        provider.listed = {zoom.id: True}
        runner = ComposeRunner([{'Name': 'smitline-meeting-' + room.id, 'Status': 'running(1)'}])
        router = self.make_router(provider=provider, runner=runner)
        await router.reconcile(self.daemon)
        self.assertEqual(set(router.active_ids()), {room.id, zoom.id})
        # Neither pool finished or forgot the other's meeting.
        self.assertIsNone(self.handoff(room.id))
        self.assertIsNone(self.handoff(zoom.id))
        self.assertTrue(pool_claim_path(self.root, room.id).exists())
        self.assertTrue(pool_claim_path(self.root, zoom.id).exists())
        self.assertTrue(remote_token_path(self.root, zoom.id).exists())
        self.assertEqual(router.control_headers(zoom.id),
                         {'Authorization': 'Bearer ' + machine.package['controlToken']})
        # The meeting whose machine is gone is finished, and its claim goes.
        self.assertEqual(self.handoff(gone.id).end_reason, 'runtime_unavailable_on_restart')
        self.assertFalse(pool_claim_path(self.root, gone.id).exists())
        # The adopted meetings count: the same accounts can't start another.
        self.assertEqual(router.capacity_problem('acct_a')[1], 'owner_capacity_exceeded')
        self.assertEqual(router.capacity_problem('acct_b')[1], 'owner_capacity_exceeded')

    async def test_when_the_machines_cannot_be_listed_nothing_is_finished(self):
        room = await self.started(meeting('mtg-room000000001', 'acct_a', 'daily'))
        zoom = await self.started(meeting('mtg-zoom000000001', 'acct_b'))
        await self.router.shutdown()
        provider = FakeProvider(disk=self.disk)

        async def broken():
            raise RuntimeError('the cloud API is down')
        provider.running = broken
        runner = ComposeRunner([{'Name': 'smitline-meeting-' + room.id, 'Status': 'running(1)'}])
        router = self.make_router(provider=provider, runner=runner)
        await router.reconcile(self.daemon)
        self.assertIsNone(self.handoff(zoom.id))
        self.assertTrue(pool_claim_path(self.root, zoom.id).exists())
        self.assertTrue(remote_token_path(self.root, zoom.id).exists())
        self.assertEqual(router.active_ids(), (room.id,))  # what is known to run is followed


class DaemonStartTests(unittest.IsolatedAsyncioTestCase):
    """With a real daemon: a room starts before create_meeting returns, a machine after."""

    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.containers = []
        self.fail_room = False

        def container(project, env):
            fake = FakeLauncher()
            fake.fail_up = self.fail_room
            self.containers.append(fake)
            return fake
        runtime = self.root / 'meeting-runtime'
        local = MeetingPool(self.root, runtime_root=runtime, runner=ComposeRunner(), environ={},
                            max_meetings=4, ports=(18100, 18119), poll_interval=0.02,
                            start_timeout=0.4, stop_timeout=0.4, launcher_factory=container,
                            health_factory=lambda port: FakeHealth(), port_free=lambda port: True)
        self.provider = FakeProvider()
        remote = RemoteMeetingPool(self.root, launcher=self.provider, runtime_root=runtime,
                                   max_meetings=4, poll_interval=0.02, start_timeout=30.0,
                                   stop_timeout=1.0, archive_timeout=1.0)
        self.router = MeetingRouter(local, remote, remote_platforms=BROWSER, max_meetings=4)
        self.daemon = RuntimeDaemon(self.root / 'daemon', supervisor=self.router)
        self.router.bind_daemon(self.daemon)

    async def asyncTearDown(self):
        await self.router.shutdown()
        for machine in self.provider.machines.values():
            await machine.vanish()
        self.daemon.close()
        self.temporary.cleanup()

    async def test_a_room_is_running_when_its_create_returns(self):
        room = await self.daemon.create_meeting(create_payload(meetingUrl=DAILY))
        self.assertEqual(room.platform, 'daily')
        self.assertEqual(len(self.containers), 1)
        self.assertTrue(self.containers[0].running)
        self.assertEqual(self.daemon._start_tasks, set())

    async def test_a_room_that_cannot_start_fails_its_create_at_once(self):
        self.fail_room = True
        with self.assertRaises(DaemonError) as caught:
            await self.daemon.create_meeting(create_payload(meetingUrl=DAILY))
        self.assertEqual((caught.exception.status, caught.exception.code), (503, 'supervisor_unavailable'))

    async def test_a_browser_meeting_returns_while_its_machine_boots(self):
        self.provider.boot.clear()
        zoom = await asyncio.wait_for(self.daemon.create_meeting(create_payload()), 1.0)
        self.assertEqual(zoom.state, 'joining')
        await until(lambda: zoom.id in self.provider.machines
                    and self.provider.machines[zoom.id].package is not None)
        self.assertEqual(self.containers, [])
        self.provider.boot.set()
        await until(lambda: not self.daemon._start_tasks)


class TimeLimitTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.provider = FakeProvider()
        self.pool = RemoteMeetingPool(self.root, launcher=self.provider,
                                      runtime_root=self.root / 'meeting-runtime', max_meetings=4,
                                      poll_interval=0.02, start_timeout=30.0, stop_timeout=1.0,
                                      archive_timeout=1.0)
        self.daemon = RuntimeDaemon(self.root / 'daemon', supervisor=self.pool)
        self.pool.bind_daemon(self.daemon)

    async def asyncTearDown(self):
        # End what the tests started, so no start is still running when the daemon closes.
        for meeting_id in self.daemon.meetings.list_ids():
            if self.daemon.meetings.get(meeting_id).session.state != 'ended':
                await self.daemon.cancel_meeting(meeting_id)
        await until(lambda: not self.daemon._start_tasks)
        await self.pool.shutdown()
        for machine in self.provider.machines.values():
            await machine.vanish()
        self.daemon.close()
        self.temporary.cleanup()

    async def test_the_machine_learns_the_meetings_time_limit(self):
        created = await self.daemon.create_meeting(create_payload(maxMinutes=45))
        self.assertEqual(created.max_minutes, 45)
        self.assertEqual(self.daemon.meetings.get(created.id).session.max_minutes, 45)
        await until(lambda: created.id in self.provider.machines
                    and self.provider.machines[created.id].package is not None)
        self.assertEqual(self.provider.machines[created.id].package['maxMinutes'], 45)

    async def test_a_meeting_without_a_limit_sends_none(self):
        created = await self.daemon.create_meeting(create_payload())
        await until(lambda: created.id in self.provider.machines
                    and self.provider.machines[created.id].package is not None)
        self.assertNotIn('maxMinutes', self.provider.machines[created.id].package)

    async def test_a_limit_outside_a_meetings_range_is_refused(self):
        for value in (0, 241, '45', True, 4.5):
            with self.subTest(value=value), self.assertRaises(DaemonError) as caught:
                await self.daemon.create_meeting(create_payload(maxMinutes=value))
            self.assertEqual(caught.exception.status, 422)

    def test_records_without_a_limit_read_as_before(self):
        payload = meeting_session_payload()
        self.assertIsNone(MeetingSession.from_dict(payload).max_minutes)
        self.assertNotIn('maxMinutes', MeetingSession.from_dict(payload).to_dict())
        stored = MeetingSession.from_dict(dict(payload, maxMinutes=30))
        self.assertEqual(MeetingSession.from_dict(stored.to_dict()).max_minutes, 30)

    def test_the_call_hands_its_brief_s_limit_to_the_meeting(self):
        from call_brief import CallBrief
        brief = CallBrief.from_dict({'channel': 'meeting', 'to': 'https://us05web.zoom.us/j/123456789',
                                     'onBehalfOf': 'Ada', 'objective': 'Take notes', 'maxMinutes': 30})
        self.assertEqual(meeting_payload(brief)['maxMinutes'], 30)


class SettingTests(unittest.TestCase):
    counter = itertools.count()

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        sys.path.insert(0, str(self.root))
        self.module = f'smitline_router_test_launcher_{next(self.counter)}'
        (self.root / (self.module + '.py')).write_text(
            'class Provider:\n'
            '    def machine(self, meeting_id, owner):\n'
            '        raise NotImplementedError\n'
            '    async def running(self):\n'
            '        return {}\n'
            '    async def remove(self, meeting_id):\n'
            '        pass\n'
            'def create(**kwargs):\n'
            '    return Provider()\n')

    def tearDown(self):
        sys.path.remove(str(self.root))
        sys.modules.pop(self.module, None)
        self.temporary.cleanup()

    def build(self, **settings):
        environ = {'SMITLINE_MEETING_LAUNCHER': self.module + ':create', **settings}
        return build_supervisor(self.root, self.root / 'meeting-runtime', environ)

    def test_launcher_platforms(self):
        self.assertIsNone(launcher_platforms({}))
        self.assertIsNone(launcher_platforms({'SMITLINE_MEETING_LAUNCHER_PLATFORMS': ' '}))
        self.assertEqual(launcher_platforms({'SMITLINE_MEETING_LAUNCHER_PLATFORMS': 'zoom, Teams meet'}),
                         BROWSER)
        self.assertIsNone(launcher_platforms({'SMITLINE_MEETING_LAUNCHER_PLATFORMS': 'zoom,teams,meet,daily'}))
        with self.assertRaises(ValueError):
            launcher_platforms({'SMITLINE_MEETING_LAUNCHER_PLATFORMS': 'zoom,webex'})

    def test_the_daemon_routes_by_platform_when_the_setting_names_some(self):
        router = self.build(SMITLINE_MEETING_LAUNCHER_PLATFORMS='zoom,teams,meet',
                            SMITLINE_MAX_MEETINGS='10', SMITLINE_MAX_MEETINGS_PER_OWNER='1')
        self.assertIsInstance(router, MeetingRouter)
        self.assertIs(type(router.local), MeetingPool)
        self.assertIsInstance(router.remote, RemoteMeetingPool)
        self.assertEqual(router.remote_platforms, BROWSER)
        self.assertEqual((router.max_meetings, router.per_owner), (10, 1))
        self.assertFalse(router.local_only)

    def test_without_the_setting_every_meeting_runs_on_a_machine(self):
        for settings in ({}, {'SMITLINE_MEETING_LAUNCHER_PLATFORMS': 'zoom,teams,meet,daily'}):
            with self.subTest(settings=settings):
                self.assertIs(type(self.build(**settings)), RemoteMeetingPool)

    def test_an_unknown_platform_stops_the_daemon_from_starting(self):
        with self.assertRaises(ValueError):
            self.build(SMITLINE_MEETING_LAUNCHER_PLATFORMS='zoom,skype')


if __name__ == '__main__':
    unittest.main()
