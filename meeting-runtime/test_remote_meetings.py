"""Meetings on machines of their own (SMITLINE_MEETING_LAUNCHER, docs/meetings.md): the launcher
plugin, the start package, starting in the background, the archive over HTTP, the meeting's token
on every route, and adopting machines after a restart."""
import asyncio
import io
import itertools
import os
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from daemon_main import build_supervisor
from listening import AudioTap
from meeting_control import (
    ArchiveDoor, MeetingControl, add_routes, archive_grace, archive_tarball, health_route,
)
from meeting_supervisor import (
    ARCHIVE_GRACE, DaemonError, MeetingPool, ProductionMeetingSupervisor, RemoteMeetingPool,
    UrlHealthClient, load_launcher, remote_token_path, unpack_archive,
)
from runtime_daemon import RuntimeDaemon
from runtime_state import file_mode, meeting_state_path, pool_claim_path, read_json
from test_meeting_pool import owned, until
from test_meeting_supervisor import FakeDaemon, FakeHealth, FakeLauncher, FakeRecord
from test_runtime_daemon import create_payload

ENDED_AT = '2026-09-16T17:45:00Z'
ARCHIVE = {
    'archive.json': '{"version": 1, "meetingId": "%s", "startedAt": "2026-09-16T17:00:00Z"}\n',
    'usage.json': '{"endedAt": "' + ENDED_AT + '", "endReason": "finished", '
                  '"usage": {"usageSeconds": 42}}\n',
    'transcript.txt': '\n\nMeeting (Ada): Ship it on Friday.',
}


class FakeMachine:
    """A launcher plugin's machine. up() serves the meeting as bridge.py does, on a local port:
    /health, /control, /listen and /archive with the start package's token. It remembers each
    request it answered."""
    remote = True

    def __init__(self, provider, meeting_id, owner):
        self.provider, self.meeting_id, self.owner = provider, meeting_id, owner
        self.health_url = None
        self.running = False
        self.package = None
        self.stops = 0
        self.up_interrupted = False
        self.state = {'stage': 'starting'}
        self.door = ArchiveDoor()
        self.requests = []  # (path, status, Authorization header)
        self.before_stop = None  # the requests answered before stop() was first asked
        self.server = None

    async def up(self, package):
        self.package = package
        try:
            await self.provider.boot.wait()
        except asyncio.CancelledError:
            self.up_interrupted = True
            raise
        if self.provider.fail_up:
            raise RuntimeError('the cloud refused the machine')
        self.running = True
        if self.provider.serve:
            await self.serve()

    async def serve(self):
        environment = self.package['environment']
        control = MeetingControl(environment['SMITLINE_CONTROL_TOKEN'], self.state, owner='Ada')
        control.stop = self.leave

        @web.middleware
        async def remember(request, handler):
            response = await handler(request)
            self.requests.append((request.path, response.status,
                                  request.headers.get('Authorization')))
            return response
        app = web.Application(middlewares=[remember])
        app.router.add_get('/health', health_route(
            control, lambda: dict(self.state),
            token_required=environment.get('SMITLINE_HEALTH_TOKEN') == '1'))
        add_routes(app, control, AudioTap('pcm16', 24000), archive=self.door)
        self.server = TestServer(app)
        await self.server.start_server()
        self.health_url = f'http://127.0.0.1:{self.server.port}/health'

    def close_archive(self, disk, files=ARCHIVE):
        """The meeting closes its archive on this machine's own disk."""
        folder = Path(disk) / self.meeting_id
        folder.mkdir(parents=True)
        for name, text in files.items():
            (folder / name).write_text(text.replace('%s', self.meeting_id))
        self.door.close(folder)

    def leave(self):
        """What bridge.py's stop does (/control type stop): the meeting leaves, and its archive
        closes a moment later."""
        async def leaving():
            await asyncio.sleep(0.05)
            self.state['stage'] = 'finished'
            self.close_archive(self.provider.disk)
        self.leaving = asyncio.get_running_loop().create_task(leaving())

    async def vanish(self):
        """Nothing answers any more (the machine was preempted or deleted by hand)."""
        self.running = False
        if self.server is not None:
            await self.server.close()
            self.server = None

    async def stop(self):
        self.stops += 1
        if self.before_stop is None:
            self.before_stop = list(self.requests)
        await self.vanish()

    async def inspect(self):
        return {'running': self.running, 'unknown': False}

    def answered(self, path):
        return [(status, auth) for seen, status, auth in self.requests if seen == path]


class FakeProvider:
    """A launcher plugin (RemoteMeetingPool's docstring): machines, and what runs by label."""

    def __init__(self, existing=None, disk=None):
        self.machines = {}
        self.existing = dict(existing or {})  # machines still running from before a restart
        self.disk = disk  # where its machines keep their archives
        self.boot = asyncio.Event()
        self.boot.set()
        self.serve = True
        self.fail_up = False
        self.listed = {}
        self.removed = []

    def machine(self, meeting_id, owner):
        machine = self.existing.get(meeting_id) or FakeMachine(self, meeting_id, owner)
        self.machines[meeting_id] = machine
        return machine

    async def running(self):
        return dict(self.listed)

    async def remove(self, meeting_id):
        self.removed.append(meeting_id)


def owner_environment(owner):
    return {'OPENAI_API_KEY': 'sk-test-key-of-' + owner, 'SMITLINE_VOICE': 'cedar'}


class RemoteCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.runtime = self.root / 'meeting-runtime'
        self.runtime.mkdir()
        self.disk = self.root / 'machine-disk'
        self.daemon = FakeDaemon()
        self.providers, self.pools = [], []
        self.provider = self.make_provider()
        self.pool = self.make_pool(self.provider)

    async def asyncTearDown(self):
        for pool in self.pools:
            await pool.shutdown()
        for provider in self.providers:
            for machine in list(provider.machines.values()) + list(provider.existing.values()):
                await machine.vanish()
        self.temporary.cleanup()

    def make_provider(self, **kwargs):
        provider = FakeProvider(disk=self.disk, **kwargs)
        self.providers.append(provider)
        return provider

    def make_pool(self, provider, **overrides):
        options = dict(runtime_root=self.runtime, max_meetings=4, poll_interval=0.02,
                       start_timeout=5.0, stop_timeout=1.0, archive_timeout=5.0,
                       owner_environment=owner_environment)
        options.update(overrides)
        pool = RemoteMeetingPool(self.root, launcher=provider, **options).bind_daemon(self.daemon)
        self.pools.append(pool)
        return pool

    async def started(self, meeting, pool=None):
        self.daemon.meetings.records[meeting.id] = FakeRecord(meeting)
        await (pool or self.pool).start(meeting)
        return self.provider.machines[meeting.id]

    def handoff(self, meeting_id):
        record = self.daemon.meetings.get(meeting_id)
        return None if record is None else record.handoff


class StartPackageTests(RemoteCase):
    async def test_the_machine_gets_everything_by_value_and_nothing_secret_is_written(self):
        meeting = owned('mtg-remote0000001', 'acct_a')
        machine = await self.started(meeting)
        package = machine.package
        token = package['controlToken']
        self.assertTrue(token)
        self.assertEqual((package['meetingId'], package['owner'], package['meetingUrl']),
                         (meeting.id, 'acct_a', meeting.meeting_url))
        # The runtime state the container reads, as the daemon keeps it.
        self.assertEqual(package['runtimeState'],
                         read_json(meeting_state_path(self.runtime, meeting.id)))
        self.assertEqual(package['environment'], {
            'OPENAI_API_KEY': 'sk-test-key-of-acct_a',
            'SMITLINE_VOICE': 'cedar',
            'MEETING_URL': meeting.meeting_url,
            'SMITLINE_RUNTIME_STATE': '/meeting-runtime/run/meetings/' + meeting.id + '/runtime.json',
            'SMITLINE_CONTROL_TOKEN': token,
            'SMITLINE_HEALTH_TOKEN': '1',
            'SMITLINE_ARCHIVE_GRACE': str(ARCHIVE_GRACE),
        })
        # No env file, and the key is nowhere on this machine's disk.
        self.assertFalse(meeting_state_path(self.runtime, meeting.id).with_name('owner.env').exists())
        for path in self.root.rglob('*'):
            if path.is_file():
                self.assertNotIn(b'sk-test-key-of-acct_a', path.read_bytes(), path)

    async def test_the_token_is_kept_privately_beside_the_claim(self):
        meeting = owned('mtg-remote0000001', 'acct_a')
        machine = await self.started(meeting)
        claim = pool_claim_path(self.root, meeting.id)
        self.assertEqual(read_json(claim), {
            'meetingId': meeting.id, 'owner': 'acct_a', 'project': 'smitline-meeting-' + meeting.id,
            'remote': True,
        })
        token = remote_token_path(self.root, meeting.id)
        self.assertEqual(token.read_text(), machine.package['controlToken'] + '\n')
        self.assertEqual((file_mode(claim), file_mode(token)), (0o600, 0o600))
        # Both go when the meeting ends.
        machine.state['stage'] = 'finished'
        machine.close_archive(self.disk)
        await until(lambda: not claim.exists())
        self.assertFalse(token.exists())

    async def test_the_daemon_reaches_the_meeting_at_its_machine_with_its_token(self):
        meeting = owned('mtg-remote0000001', 'acct_a')
        machine = await self.started(meeting)
        bearer = 'Bearer ' + machine.package['controlToken']
        base = machine.health_url[:-len('/health')]
        self.assertEqual(self.pool.control_headers(meeting.id), {'Authorization': bearer})
        self.assertEqual(self.pool.listen_url(meeting.id), base + '/listen')
        self.assertEqual(await self.pool.control(meeting.id, {'type': 'following', 'value': True}),
                         {'following': True})
        machine.state['stage'] = 'live'
        await until(lambda: (meeting.id, 'live') in self.daemon.transitions)
        # /health needs the token on a machine of its own; the daemon always sent it.
        self.assertTrue(machine.answered('/health'))
        self.assertEqual(set(machine.answered('/health')), {(200, bearer)})
        self.assertEqual(machine.answered('/control'), [(200, bearer)])

    async def test_one_machine_per_meeting_and_no_ports(self):
        first = await self.started(owned('mtg-remote0000001', 'acct_a'))
        second = await self.started(owned('mtg-remote0000002', 'acct_b'))
        self.assertIsNot(first, second)
        self.assertNotEqual(first.package['controlToken'], second.package['controlToken'])
        self.assertEqual(set(self.pool.active_ids()), {'mtg-remote0000001', 'mtg-remote0000002'})
        self.assertIsNone(self.pool.capacity_problem('acct_c'))

    async def test_a_local_pool_claim_holds_no_token(self):
        # Without a launcher nothing changes: pool claims never kept the token.
        pool = MeetingPool(self.root, runtime_root=self.runtime, environ={}, max_meetings=2,
                           ports=(18100, 18109), poll_interval=0.02, start_timeout=0.4,
                           stop_timeout=0.4, launcher_factory=lambda project, env: FakeLauncher(),
                           health_factory=lambda port: FakeHealth(),
                           port_free=lambda port: True).bind_daemon(self.daemon)
        self.pools.append(pool)
        meeting = owned('mtg-local00000001', 'acct_a')
        self.daemon.meetings.records[meeting.id] = FakeRecord(meeting)
        await pool.start(meeting)
        claim = read_json(pool_claim_path(self.root, meeting.id))
        self.assertNotIn('remote', claim)
        self.assertFalse(remote_token_path(self.root, meeting.id).exists())


class ArchiveTests(RemoteCase):
    async def test_the_archive_comes_back_before_the_handoff_and_the_machine_is_deleted(self):
        meeting = owned('mtg-remote0000001', 'acct_a')
        machine = await self.started(meeting)
        bearer = 'Bearer ' + machine.package['controlToken']
        # The meeting ends; its archive is still closing, so the daemon asks again.
        machine.state['stage'] = 'finished'
        await until(lambda: (409, bearer) in machine.answered('/archive'))
        self.assertIsNone(self.handoff(meeting.id))
        machine.close_archive(self.disk)
        await until(lambda: self.handoff(meeting.id) is not None)
        self.assertIn((200, bearer), machine.answered('/archive'))
        self.assertTrue(machine.door.fetched.is_set())
        handoff = self.handoff(meeting.id)
        self.assertEqual((handoff.partial, handoff.end_reason), (False, 'finished'))
        self.assertEqual(handoff.ended_at, ENDED_AT)  # from the machine's archive
        archive = self.runtime / 'recordings' / meeting.id
        self.assertEqual((archive / 'transcript.txt').read_text(), ARCHIVE['transcript.txt'])
        self.assertEqual(file_mode(archive / 'transcript.txt'), 0o600)
        self.assertEqual(file_mode(archive), 0o700)
        await until(lambda: machine.stops >= 1)
        self.assertFalse(pool_claim_path(self.root, meeting.id).exists())
        self.assertEqual(self.pool.active_ids(), ())

    async def test_ending_a_meeting_takes_its_archive_before_its_machine_is_deleted(self):
        # end_call or maxMinutes: the meeting is asked to stop, as SIGTERM would stop it here.
        meeting = owned('mtg-remote0000001', 'acct_a')
        machine = await self.started(meeting)
        bearer = 'Bearer ' + machine.package['controlToken']
        await asyncio.wait_for(self.pool.cancel(meeting.id), 3.0)
        handoff = self.handoff(meeting.id)
        self.assertEqual((handoff.partial, handoff.end_reason), (True, 'cancelled'))
        self.assertEqual(handoff.ended_at, ENDED_AT)  # from the machine's archive
        before = [(path, status) for path, status, _auth in machine.before_stop]
        self.assertIn(('/control', 200), before)
        self.assertIn(('/archive', 200), before)
        self.assertEqual({auth for path, _status, auth in machine.before_stop
                          if path in ('/control', '/archive')}, {bearer})
        archive = self.runtime / 'recordings' / meeting.id
        self.assertEqual((archive / 'transcript.txt').read_text(), ARCHIVE['transcript.txt'])
        self.assertFalse(pool_claim_path(self.root, meeting.id).exists())
        self.assertFalse(remote_token_path(self.root, meeting.id).exists())

    async def test_ending_a_meeting_that_does_not_answer_still_deletes_its_machine(self):
        meeting = owned('mtg-remote0000001', 'acct_a')
        machine = await self.started(meeting)
        await machine.server.close()  # its meeting is gone; the machine still runs
        machine.server = None
        await asyncio.wait_for(self.pool.cancel(meeting.id), 3.0)
        self.assertGreaterEqual(machine.stops, 1)
        handoff = self.handoff(meeting.id)
        self.assertEqual((handoff.partial, handoff.end_reason), (True, 'cancelled'))

    async def test_a_meeting_that_failed_hands_over_its_archive_before_its_machine_is_stopped(self):
        # needs_attention: the meeting closed its archive at once (bridge.py) and waits to be
        # stopped; the daemon takes the archive first.
        meeting = owned('mtg-remote0000001', 'acct_a')
        machine = await self.started(meeting)
        machine.state['stage'] = 'needs_attention'
        machine.close_archive(self.disk)
        await until(lambda: self.handoff(meeting.id) is not None)
        handoff = self.handoff(meeting.id)
        self.assertEqual((handoff.partial, handoff.end_reason), (True, 'needs_attention'))
        self.assertEqual(handoff.ended_at, ENDED_AT)
        self.assertEqual([status for status, _auth in machine.answered('/archive')], [200])
        await until(lambda: machine.stops >= 1)

    async def test_a_machine_that_vanished_gets_a_partial_handoff_without_waiting(self):
        meeting = owned('mtg-remote0000001', 'acct_a')
        self.pool = self.make_pool(self.provider, archive_timeout=30.0)
        machine = await self.started(meeting)
        await machine.vanish()
        loop = asyncio.get_running_loop()
        began = loop.time()
        await until(lambda: self.handoff(meeting.id) is not None)
        self.assertLess(loop.time() - began, 3.0)  # not the 30 s archive timeout
        handoff = self.handoff(meeting.id)
        self.assertEqual((handoff.partial, handoff.end_reason), (True, 'container_exited'))
        await until(lambda: not pool_claim_path(self.root, meeting.id).exists())


class CancelTests(RemoteCase):
    async def test_ending_a_meeting_while_its_machine_is_created_does_not_wait_for_it(self):
        self.provider.boot.clear()  # the cloud takes its time
        meeting = owned('mtg-remote0000001', 'acct_a')
        self.daemon.meetings.records[meeting.id] = FakeRecord(meeting)
        starting = asyncio.create_task(self.pool.start(meeting))
        await until(lambda: meeting.id in self.provider.machines
                    and self.provider.machines[meeting.id].package is not None)
        await asyncio.wait_for(self.pool.cancel(meeting.id), 1.0)  # the start may take 5 s
        with self.assertRaises(DaemonError) as caught:
            await starting
        self.assertEqual(caught.exception.code, 'canceled')
        machine = self.provider.machines[meeting.id]
        self.assertTrue(machine.up_interrupted)
        self.assertGreaterEqual(machine.stops, 1)
        handoff = self.handoff(meeting.id)
        self.assertEqual((handoff.partial, handoff.end_reason), (True, 'cancelled'))
        self.assertFalse(pool_claim_path(self.root, meeting.id).exists())
        self.assertEqual(self.pool.active_ids(), ())

    async def test_ending_a_meeting_while_its_machine_boots_does_not_wait_for_it(self):
        self.provider.serve = False  # created, but the meeting never answers /health
        meeting = owned('mtg-remote0000001', 'acct_a')
        self.daemon.meetings.records[meeting.id] = FakeRecord(meeting)
        starting = asyncio.create_task(self.pool.start(meeting))
        await until(lambda: meeting.id in self.provider.machines
                    and self.provider.machines[meeting.id].running)
        await asyncio.wait_for(self.pool.cancel(meeting.id), 1.0)
        with self.assertRaises(DaemonError) as caught:
            await starting
        self.assertEqual(caught.exception.code, 'canceled')
        self.assertGreaterEqual(self.provider.machines[meeting.id].stops, 1)

    async def test_a_start_stops_once_its_meeting_has_a_handoff(self):
        # Ended before its start began (the finisher wrote its handoff): no machine stays up.
        self.provider.serve = False
        meeting = owned('mtg-remote0000001', 'acct_a')
        self.daemon.meetings.records[meeting.id] = FakeRecord(meeting)
        starting = asyncio.create_task(self.pool.start(meeting))
        await until(lambda: meeting.id in self.provider.machines
                    and self.provider.machines[meeting.id].running)
        self.daemon.meetings.records[meeting.id].handoff = object()
        with self.assertRaises(DaemonError) as caught:
            await asyncio.wait_for(starting, 1.0)
        self.assertEqual(caught.exception.code, 'canceled')
        self.assertFalse(self.provider.machines[meeting.id].running)


def counting_inspect(machine):
    """Count the launcher's inspect() calls from now on: each is a cloud API call."""
    calls = []
    real = machine.inspect

    async def inspect():
        calls.append(1)
        return await real()
    machine.inspect = inspect
    return calls


class CloudCallTests(RemoteCase):
    async def test_a_meeting_that_answers_costs_no_cloud_calls(self):
        machine = await self.started(owned('mtg-remote0000001', 'acct_a'))
        calls = counting_inspect(machine)
        seen = len(machine.answered('/health'))
        await until(lambda: len(machine.answered('/health')) >= seen + 10)
        self.assertEqual(calls, [])

    async def test_a_starting_machine_is_asked_about_at_the_poll_interval(self):
        self.provider.serve = False  # it boots and boots
        pool = self.make_pool(self.provider, poll_interval=0.2)
        meeting = owned('mtg-remote0000001', 'acct_a')
        self.daemon.meetings.records[meeting.id] = FakeRecord(meeting)
        starting = asyncio.create_task(pool.start(meeting))
        await until(lambda: meeting.id in self.provider.machines)
        calls = counting_inspect(self.provider.machines[meeting.id])
        await asyncio.sleep(0.6)
        self.assertLessEqual(len(calls), 5)  # a container is asked every 0.05 s: 12 times
        await pool.cancel(meeting.id)
        with self.assertRaises(DaemonError):
            await starting


class AdoptionTests(RemoteCase):
    async def restart(self, machines, listed):
        """A new daemon on the same folders: the old one's watches stop, its machines run on."""
        await self.pool.shutdown()
        provider = self.make_provider(existing=machines)
        provider.listed = listed
        pool = self.make_pool(provider)
        await pool.reconcile(self.daemon)
        return provider, pool

    async def test_a_restarted_daemon_adopts_a_running_machine_with_its_token(self):
        meeting = owned('mtg-remote0000001', 'acct_a')
        machine = await self.started(meeting)
        token = machine.package['controlToken']
        provider, pool = await self.restart({meeting.id: machine}, {meeting.id: True})
        self.assertEqual(pool.active_ids(), (meeting.id,))
        self.assertEqual(pool.control_headers(meeting.id), {'Authorization': 'Bearer ' + token})
        self.assertEqual(await pool.control(meeting.id, {'type': 'following', 'value': True}),
                         {'following': True})
        machine.state['stage'] = 'live'
        await until(lambda: (meeting.id, 'live') in self.daemon.transitions)
        # It follows the meeting to its end, archive included.
        machine.state['stage'] = 'finished'
        machine.close_archive(self.disk)
        await until(lambda: self.handoff(meeting.id) is not None)
        self.assertEqual(self.handoff(meeting.id).ended_at, ENDED_AT)
        self.assertEqual(provider.removed, [])

    async def test_restart_removes_ended_meetings_machines_and_leaves_strangers_alone(self):
        live = owned('mtg-remote0000001', 'acct_a')
        machine = await self.started(live)
        ended = owned('mtg-remote0000002', 'acct_b')
        self.daemon.meetings.records[ended.id] = FakeRecord(
            type(ended).from_dict(dict(ended.to_dict(), state='ended')))
        gone = owned('mtg-remote0000003', 'acct_c')
        self.daemon.meetings.records[gone.id] = FakeRecord(gone)  # its machine is not running
        pool_claim_path(self.root, gone.id).write_text('{"meetingId": "%s"}' % gone.id)
        remote_token_path(self.root, gone.id).write_text('old-token\n')
        provider, pool = await self.restart(
            {live.id: machine},
            {live.id: True, ended.id: True, 'mtg-stranger00001': True, '../not-an-id': True})
        self.assertEqual(pool.active_ids(), (live.id,))
        self.assertEqual(provider.removed, [ended.id])  # a stranger's machine is not ours
        self.assertFalse(pool_claim_path(self.root, gone.id).exists())
        self.assertFalse(remote_token_path(self.root, gone.id).exists())
        self.assertTrue(remote_token_path(self.root, live.id).exists())
        handoff = self.handoff(gone.id)
        self.assertEqual((handoff.partial, handoff.end_reason),
                         (True, 'runtime_unavailable_on_restart'))

    async def test_a_cloud_that_cannot_list_machines_changes_nothing(self):
        meeting = owned('mtg-remote0000001', 'acct_a')
        await self.started(meeting)
        await self.pool.shutdown()
        provider = self.make_provider()

        async def broken():
            raise RuntimeError('the cloud API is down')
        provider.running = broken
        pool = self.make_pool(provider)
        await pool.reconcile(self.daemon)
        self.assertTrue(pool_claim_path(self.root, meeting.id).exists())
        self.assertIsNone(self.handoff(meeting.id))


class BackgroundStartTests(unittest.IsolatedAsyncioTestCase):
    """With a real daemon: creating a meeting returns while its machine starts."""

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
        await self.pool.shutdown()
        for machine in self.provider.machines.values():
            await machine.vanish()
        self.daemon.close()
        self.temporary.cleanup()

    def record(self, meeting_id):
        return self.daemon.meetings.get(meeting_id)

    async def test_the_meeting_is_joining_while_its_machine_starts_and_ends_at_once(self):
        self.provider.boot.clear()
        meeting = await asyncio.wait_for(self.daemon.create_meeting(create_payload()), 1.0)
        self.assertEqual(meeting.state, 'joining')
        await until(lambda: meeting.id in self.provider.machines
                    and self.provider.machines[meeting.id].package is not None)
        ended = await asyncio.wait_for(self.daemon.cancel_meeting(meeting.id), 1.0)
        self.assertEqual(ended.state, 'ended')
        machine = self.provider.machines[meeting.id]
        self.assertTrue(machine.up_interrupted)
        self.assertGreaterEqual(machine.stops, 1)
        handoff = self.record(meeting.id).handoff
        self.assertEqual((handoff.partial, handoff.end_reason), (True, 'cancelled'))
        await until(lambda: not self.daemon._start_tasks)

    async def test_a_machine_that_never_comes_up_ends_the_meeting(self):
        self.provider.fail_up = True
        meeting = await self.daemon.create_meeting(create_payload())
        await until(lambda: self.record(meeting.id).handoff is not None)
        await until(lambda: not self.daemon._start_tasks)
        record = self.record(meeting.id)
        self.assertEqual(record.session.state, 'ended')
        self.assertEqual((record.handoff.partial, record.handoff.end_reason),
                         (True, 'supervisor_start_failed'))
        self.assertGreaterEqual(self.provider.machines[meeting.id].stops, 1)
        self.assertFalse(pool_claim_path(self.root, meeting.id).exists())

    async def test_a_meeting_ended_before_its_start_began_gets_no_machine(self):
        meeting = await self.daemon.create_meeting(create_payload())
        await self.daemon.cancel_meeting(meeting.id)  # before the start in the background ran
        await until(lambda: not self.daemon._start_tasks)
        await asyncio.sleep(0.1)
        self.assertEqual([machine for machine in self.provider.machines.values()
                          if machine.running], [])
        self.assertEqual(self.record(meeting.id).handoff.end_reason, 'cancelled')

    async def test_a_meeting_on_its_machine_goes_live(self):
        meeting = await self.daemon.create_meeting(create_payload())
        await until(lambda: meeting.id in self.provider.machines
                    and self.provider.machines[meeting.id].health_url is not None)
        self.provider.machines[meeting.id].state['stage'] = 'live'
        await until(lambda: self.record(meeting.id).session.state == 'live')


class PluginLoadingTests(unittest.TestCase):
    counter = itertools.count()

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        sys.path.insert(0, str(self.root))
        self.modules = []

    def tearDown(self):
        sys.path.remove(str(self.root))
        for name in self.modules:
            sys.modules.pop(name, None)
        self.temporary.cleanup()

    def plugin(self, body):
        name = f'smitline_test_launcher_{next(self.counter)}'
        (self.root / (name + '.py')).write_text(body)
        self.modules.append(name)
        return name

    PLUGIN = (
        'class Provider:\n'
        '    start_timeout = 120\n'
        '    def __init__(self, **kwargs):\n'
        '        self.kwargs = kwargs\n'
        '    def machine(self, meeting_id, owner):\n'
        '        raise NotImplementedError\n'
        '    async def running(self):\n'
        '        return {}\n'
        '    async def remove(self, meeting_id):\n'
        '        pass\n'
        'def create(**kwargs):\n'
        '    return Provider(**kwargs)\n'
    )

    def test_a_plugin_is_built_with_the_settings_and_folders(self):
        name = self.plugin(self.PLUGIN)
        provider = load_launcher(f'{name}:create', environ={'A': '1'}, project_root=self.root,
                                 runtime_root=self.root / 'meeting-runtime')
        self.assertEqual(provider.kwargs, {'environ': {'A': '1'}, 'project_root': self.root,
                                           'runtime_root': self.root / 'meeting-runtime'})

    def test_the_daemon_runs_meetings_on_machines_when_a_launcher_is_set(self):
        name = self.plugin(self.PLUGIN)
        environ = {'SMITLINE_MEETING_LAUNCHER': f'{name}:create', 'SMITLINE_MAX_MEETINGS': '50',
                   'SMITLINE_MAX_MEETINGS_PER_OWNER': '1'}
        pool = build_supervisor(self.root, self.root / 'meeting-runtime', environ)
        self.assertIsInstance(pool, RemoteMeetingPool)
        self.assertTrue(pool.starts_in_background)
        self.assertFalse(pool.local_only)
        self.assertEqual((pool.max_meetings, pool.per_owner, pool.start_timeout), (50, 1, 120.0))
        self.assertEqual(pool.provider.kwargs['environ'], environ)

    def test_without_a_launcher_meetings_run_here_as_before(self):
        for environ in ({}, {'SMITLINE_MEETING_LAUNCHER': '  '}):
            supervisor = build_supervisor(self.root, self.root / 'meeting-runtime', environ)
            self.assertIs(type(supervisor), ProductionMeetingSupervisor)
            self.assertFalse(getattr(supervisor, 'starts_in_background', False))
            self.assertFalse(supervisor.remote)
        pool = build_supervisor(self.root, self.root / 'meeting-runtime',
                                {'SMITLINE_MAX_MEETINGS': '3'})
        self.assertIs(type(pool), MeetingPool)

    def test_a_bad_launcher_stops_the_daemon_from_starting(self):
        missing = self.plugin(self.PLUGIN.replace('    async def remove', '    async def _remove'))
        cases = [('no-colon', ValueError), (f'{missing}:create', ValueError),
                 ('smitline_no_such_module:create', ImportError)]
        for spec, error in cases:
            with self.subTest(spec=spec), self.assertRaises(error):
                build_supervisor(self.root, self.root / 'meeting-runtime',
                                 {'SMITLINE_MEETING_LAUNCHER': spec})


class BridgeRouteTests(unittest.IsolatedAsyncioTestCase):
    """What bridge.py serves (meeting_control.py), on a machine of its own and here."""

    async def client(self, *, token_required, door=None):
        state = {'stage': 'live', 'captions': [{'seq': 0, 'text': 'hi'}]}
        control = self.control = MeetingControl('meeting-token', state, owner='Ada')
        app = web.Application()
        app.router.add_get('/health', health_route(control, lambda: dict(state),
                                                   token_required=token_required))
        add_routes(app, control, AudioTap('pcm16', 24000), archive=door)
        client = TestClient(TestServer(app))
        await client.start_server()
        self.addAsyncCleanup(client.close)
        return client

    async def test_health_on_a_network_needs_the_meetings_token(self):
        client = await self.client(token_required=True)
        for headers in ({}, {'Authorization': 'Bearer wrong'}, {'Authorization': 'meeting-token'}):
            response = await client.get('/health', headers=headers)
            self.assertEqual(response.status, 403, headers)
        response = await client.get('/health', headers={'Authorization': 'Bearer meeting-token'})
        self.assertEqual(response.status, 200)
        self.assertEqual((await response.json())['stage'], 'live')
        self.assertEqual(response.headers['Cache-Control'], 'no-store')

    async def test_health_here_answers_as_before(self):
        client = await self.client(token_required=False)
        response = await client.get('/health')
        self.assertEqual(response.status, 200)
        self.assertEqual((await response.json())['stage'], 'live')
        # Without a door there is no /archive at all.
        self.assertEqual((await client.get(
            '/archive', headers={'Authorization': 'Bearer meeting-token'})).status, 404)

    async def test_the_archive_is_for_the_daemon_once_it_is_closed(self):
        door = ArchiveDoor()
        client = await self.client(token_required=True, door=door)
        bearer = {'Authorization': 'Bearer meeting-token'}
        self.assertEqual((await client.get('/archive')).status, 403)
        self.assertEqual((await client.get('/archive', headers=bearer)).status, 409)
        self.assertFalse(door.fetched.is_set())
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp) / 'mtg-remote0000001'
            folder.mkdir()
            (folder / 'transcript.txt').write_text('Meeting: hello')
            (folder / 'usage.json').write_text('{}')
            door.close(folder)
            response = await client.get('/archive', headers=bearer)
            self.assertEqual(response.status, 200)
            self.assertEqual(response.headers['Content-Type'], 'application/gzip')
            with tarfile.open(fileobj=io.BytesIO(await response.read()), mode='r:gz') as tar:
                self.assertEqual(sorted(tar.getnames()), ['transcript.txt', 'usage.json'])
        self.assertTrue(door.fetched.is_set())

    async def test_the_daemon_can_stop_the_meeting_as_sigterm_would(self):
        client = await self.client(token_required=True, door=ArchiveDoor())
        bearer = {'Authorization': 'Bearer meeting-token'}
        stop = {'type': 'stop'}
        self.assertEqual((await client.post('/control', json=stop)).status, 403)
        # Before the bridge set it up (it does at once), there is nothing to stop.
        self.assertEqual((await client.post('/control', json=stop, headers=bearer)).status, 409)
        stopped = []
        self.control.stop = lambda: stopped.append(True)
        response = await client.post('/control', json=stop, headers=bearer)
        self.assertEqual((response.status, await response.json()), (200, {'stopping': True}))
        self.assertEqual(stopped, [True])
        refused = await client.post('/control', json={'type': 'reboot'}, headers=bearer)
        self.assertEqual(refused.status, 422)

    async def test_a_meeting_without_an_archive_hands_over_an_empty_one(self):
        door = ArchiveDoor()
        client = await self.client(token_required=True, door=door)
        door.close(None)
        response = await client.get('/archive', headers={'Authorization': 'Bearer meeting-token'})
        self.assertEqual(response.status, 200)
        with tarfile.open(fileobj=io.BytesIO(await response.read()), mode='r:gz') as tar:
            self.assertEqual(tar.getnames(), [])

    async def test_the_server_waits_for_the_daemon_at_most_its_grace(self):
        door = ArchiveDoor()
        self.assertFalse(await door.wait_fetched(0))  # here: no wait at all
        self.assertFalse(await door.wait_fetched(0.05))
        door.fetched.set()
        self.assertTrue(await door.wait_fetched(5))

    def test_archive_grace(self):
        cases = {None: 0, '': 0, '120': 120, ' 30 ': 30, '9999': 600, '-5': 0, 'soon': 0}
        for text, seconds in cases.items():
            environ = {} if text is None else {'SMITLINE_ARCHIVE_GRACE': text}
            self.assertEqual(archive_grace(environ), seconds, text)

    def test_the_tarball_holds_top_level_files_only(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            (folder / 'events.jsonl').write_text('{}\n')
            (folder / 'nested').mkdir()
            (folder / 'nested' / 'inner.txt').write_text('no')
            (folder / 'link').symlink_to(folder / 'events.jsonl')
            with tarfile.open(fileobj=io.BytesIO(archive_tarball(folder)), mode='r:gz') as tar:
                self.assertEqual(tar.getnames(), ['events.jsonl'])


class BridgeCloseTests(unittest.TestCase):
    """bridge.close_record opens the archive door, once."""

    def test_closing_the_record_opens_the_archive_once(self):
        import bridge
        record = mock.Mock()
        record.directory = Path('/meeting-runtime/recordings/mtg-remote0000001')
        door = ArchiveDoor()
        state = {'stage': 'finished', 'archive': {'closed': False}}
        with mock.patch.object(bridge, 'record', record), \
                mock.patch.object(bridge, 'archive_door', door), \
                mock.patch.object(bridge, 'state', state):
            bridge.close_record(SimpleNamespace(backend_model='gpt-live'))
            bridge.close_record(SimpleNamespace(backend_model='gpt-live'))
        record.close.assert_called_once()
        self.assertEqual((door.closed, door.directory), (True, record.directory))
        self.assertEqual(state['archive'], {'closed': True, 'handoffStatus': 'local'})

    def test_a_meeting_without_a_record_hands_over_an_empty_archive(self):
        import bridge
        door = ArchiveDoor()
        with mock.patch.object(bridge, 'record', None), \
                mock.patch.object(bridge, 'archive_door', door):
            bridge.close_record(SimpleNamespace(backend_model='gpt-live'))
        self.assertEqual((door.closed, door.directory), (True, None))


def tarball(members):
    """A gzipped tar from (TarInfo, bytes or None) pairs."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode='w:gz') as tar:
        for info, data in members:
            if data is not None:
                info.size = len(data)
            tar.addfile(info, io.BytesIO(data) if data is not None else None)
    return buffer.getvalue()


def member(name, kind=tarfile.REGTYPE, **fields):
    info = tarfile.TarInfo(name)
    info.type = kind
    for key, value in fields.items():
        setattr(info, key, value)
    return info


class UnpackTests(unittest.TestCase):
    def test_only_plain_top_level_files_are_written_privately(self):
        data = tarball([
            (member('transcript.txt'), b'Meeting: hello'),
            (member('../escape.txt'), b'no'),
            (member('/etc/passwd'), b'no'),
            (member('nested/inner.txt'), b'no'),
            (member('.hidden'), b'no'),
            (member('link', tarfile.SYMTYPE, linkname='/etc/passwd'), None),
            (member('hard', tarfile.LNKTYPE, linkname='transcript.txt'), None),
            (member('folder', tarfile.DIRTYPE), None),
        ])
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / 'recordings' / 'mtg-remote0000001'
            written = unpack_archive(data, target)
            self.assertEqual(written, ['transcript.txt'])
            self.assertEqual(sorted(os.listdir(target)), ['transcript.txt'])
            self.assertEqual((target / 'transcript.txt').read_bytes(), b'Meeting: hello')
            self.assertEqual(file_mode(target / 'transcript.txt'), 0o600)
            self.assertEqual(file_mode(target), 0o700)
            self.assertFalse((Path(temp) / 'recordings' / 'escape.txt').exists())

    def test_an_archive_past_the_size_cap_stops_there(self):
        data = tarball([(member('a.txt'), b'x' * 8), (member('b.txt'), b'y' * 8)])
        with tempfile.TemporaryDirectory() as temp, \
                mock.patch('meeting_supervisor.MAX_ARCHIVE_BYTES', 10):
            self.assertEqual(unpack_archive(data, Path(temp) / 'out'), ['a.txt'])


class HealthClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_no_address_yet_means_no_answer_and_no_request(self):
        address = {'url': None}
        client = UrlHealthClient(lambda: address['url'], headers=lambda: {'Authorization': 'x'})
        with mock.patch('urllib.request.urlopen') as urlopen:
            self.assertIsNone(await client.fetch())
        urlopen.assert_not_called()
        address['url'] = 'http://127.0.0.1:9/health'
        self.assertEqual(client.url, 'http://127.0.0.1:9/health')
        self.assertEqual(client.headers(), {'Authorization': 'x'})

    def test_a_fixed_address_works_as_before(self):
        client = UrlHealthClient('http://127.0.0.1:8094/health')
        self.assertEqual((client.url, client.headers()), ('http://127.0.0.1:8094/health', {}))
        client.url = 'http://127.0.0.1:18101/health'
        self.assertEqual(client.url, 'http://127.0.0.1:18101/health')


if __name__ == '__main__':
    unittest.main()
