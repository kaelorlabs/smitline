import asyncio
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from agent_sessions import MeetingSession
from meeting_repository import MeetingCorruptionError, MeetingRepository
from test_schemas import (
    MEET_URL, TEAMS_URL, TIMESTAMP, ZOOM_URL, context_payload, handoff_payload,
)

try:
    from aiohttp.test_utils import TestClient, TestServer
    from runtime_daemon import DaemonError, create_app, require_loopback_bind
    HAS_AIOHTTP = True
except ImportError:
    HAS_AIOHTTP = False
    TestClient = None
    TestServer = None


class FakeClock:
    def __init__(self, moment=None):
        self.now = moment or datetime(2026, 9, 16, 17, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now = self.now + timedelta(seconds=seconds)


class FakeSupervisor:
    def __init__(self):
        self.started = []
        self.sessions = []
        self.cameras = []
        self.contexts = []
        self.cancelled = []
        self.fail_start = False
        self.fail_context = False
        self.fail_cancel = False
        self.start_error = RuntimeError('join failed')
        self.context_error = RuntimeError('context failed')
        self.cancel_error = RuntimeError('cancel failed')

    async def start(self, meeting, camera_settings=None):
        if self.fail_start:
            raise self.start_error
        self.started.append(meeting.id)
        self.sessions.append(meeting)
        self.cameras.append(camera_settings)

    async def add_context(self, meeting_id, context):
        if self.fail_context:
            raise self.context_error
        self.contexts.append((meeting_id, context.objective))

    async def cancel(self, meeting_id):
        if self.fail_cancel:
            raise self.cancel_error
        self.cancelled.append(meeting_id)


def create_payload(**overrides):
    payload = {
        'meetingUrl': ZOOM_URL,
        'context': context_payload(),
    }
    payload.update(overrides)
    return payload


async def read_sse(response, *, min_events=1, min_comments=0, timeout=2.0):
    buffer = b''
    events = []
    comments = []
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if len(events) >= min_events and len(comments) >= min_comments:
            break
        remaining = deadline - asyncio.get_event_loop().time()
        if remaining <= 0:
            break
        try:
            chunk = await asyncio.wait_for(response.content.read(1024), timeout=remaining)
        except asyncio.TimeoutError:
            break
        if not chunk:
            break
        buffer += chunk
        while b'\n\n' in buffer:
            block, buffer = buffer.split(b'\n\n', 1)
            text = block.decode('utf-8')
            if text.startswith(':'):
                comments.append(text)
                continue
            event = {}
            data_lines = []
            for line in text.split('\n'):
                if line.startswith('id: '):
                    event['id'] = line[4:]
                elif line.startswith('event: '):
                    event['event'] = line[7:]
                elif line.startswith('data: '):
                    data_lines.append(line[6:])
            if data_lines:
                event['data'] = json.loads('\n'.join(data_lines))
                events.append(event)
    return events, comments


@unittest.skipUnless(HAS_AIOHTTP, 'aiohttp is required')
class RuntimeDaemonTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.clock = FakeClock()
        self.supervisor = FakeSupervisor()
        self.auth = 'test-daemon-token'
        self.auth_publications = []
        self.app = create_app(
            root=self.temporary.name,
            auth_token=self.auth,
            on_auth_token=self.auth_publications.append,
            supervisor=self.supervisor,
            clock=self.clock,
            sse_poll_interval=0.02,
            sse_heartbeat_interval=0.05,
            max_body_bytes=4096,
        )
        self.daemon = self.app.runtime_daemon
        self.client = TestClient(TestServer(self.app))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        self.temporary.cleanup()

    def headers(self, **extra):
        return {'Authorization': 'Bearer ' + self.auth, **extra}

    async def create(self, **overrides):
        response = await self.client.post(
            '/v1/meetings', json=create_payload(**overrides), headers=self.headers())
        return response

    async def test_auth_missing_wrong_query_and_correct(self):
        self.assertEqual(self.auth_publications, [self.auth])
        created = await self.create()
        self.assertEqual(created.status, 201)
        meeting_id = (await created.json())['id']
        missing = await self.client.get('/v1/meetings/' + meeting_id)
        self.assertEqual(missing.status, 401)
        self.assertEqual(self.auth_publications, [self.auth, self.auth])
        wrong = await self.client.get(
            '/v1/meetings/' + meeting_id, headers={'Authorization': 'Bearer other-token'})
        self.assertEqual(wrong.status, 401)
        query = await self.client.get('/v1/meetings/' + meeting_id + '?token=' + self.auth)
        self.assertEqual(query.status, 401)
        ok = await self.client.get('/v1/meetings/' + meeting_id, headers=self.headers())
        self.assertEqual(ok.status, 200)
        body = await ok.json()
        self.assertNotIn(self.auth, json.dumps(body))

    async def test_coding_agent_routes_are_gone(self):
        created = await self.create()
        meeting_id = (await created.json())['id']
        for method, path in (
                ('get', '/v1/providers'),
                ('get', '/v1/runner'),
                ('post', '/v1/runner/pair'),
                ('post', '/v1/runner/unpair'),
                ('post', '/v1/meetings/' + meeting_id + '/handoff/retry'),
                ('get', '/v1/meetings/' + meeting_id + '/approvals'),
                ('get', '/v1/meetings/' + meeting_id + '/commits'),
                ('get', '/v1/meetings/' + meeting_id + '/pushes'),
                ('get', '/v1/meetings/' + meeting_id + '/artifacts'),
                ('get', '/v1/meetings/' + meeting_id + '/screen-share'),
                ('get', '/v1/agent-sessions/codex/thread-1/status')):
            with self.subTest(path=path):
                response = await getattr(self.client, method)(path, headers=self.headers())
                self.assertIn(response.status, (404, 405))

    async def test_non_loopback_bind_is_rejected(self):
        with self.assertRaises(ValueError):
            require_loopback_bind('8.8.8.8')
        with self.assertRaises(ValueError):
            create_app(root=self.temporary.name, bind_host='192.168.1.9', auth_token='x')
        self.assertEqual(require_loopback_bind('127.0.0.1'), '127.0.0.1')
        self.assertEqual(require_loopback_bind('::1'), '::1')

    async def test_body_type_size_and_schema_errors(self):
        text = await self.client.post(
            '/v1/meetings', data=b'{"meetingUrl":"x"}',
            headers=self.headers(**{'Content-Type': 'text/plain'}))
        self.assertEqual(text.status, 415)
        huge = await self.client.post(
            '/v1/meetings', data=b'{' + b'x' * 5000 + b'}',
            headers=self.headers(**{'Content-Type': 'application/json'}))
        self.assertIn(huge.status, (413, 400))
        invalid = await self.create(meetingUrl='https://example.com/not-a-meeting')
        self.assertEqual(invalid.status, 422)
        extra = await self.create(unknown='nope')
        self.assertEqual(extra.status, 422)
        missing = await self.client.post(
            '/v1/meetings', json={'meetingUrl': ZOOM_URL}, headers=self.headers())
        self.assertEqual(missing.status, 422)
        self.assertEqual(self.supervisor.started, [])

    async def test_create_rejects_coding_agent_fields(self):
        for field, value in (
                ('agentSession', {'provider': 'codex', 'sessionId': 'thread-1',
                                  'workspace': '/tmp/w'}),
                ('permissions', {'workspace': 'none'}),
                ('screenShare', {'enabled': True})):
            with self.subTest(field=field):
                response = await self.create(**{field: value})
                self.assertEqual(response.status, 422)
                body = await response.json()
                self.assertEqual(body['error']['code'], 'invalid_request')
                self.assertIn(field, body['error']['message'])
        self.assertEqual(self.supervisor.started, [])

    async def test_create_accepts_on_behalf_of_and_voice(self):
        created = await self.create(onBehalfOf='Maya Shah', voice='cinder')
        self.assertEqual(created.status, 201)
        body = await created.json()
        self.assertEqual(body['onBehalfOf'], 'Maya Shah')
        self.assertEqual(body['voice'], 'cinder')
        self.assertEqual(self.supervisor.sessions[-1].on_behalf_of, 'Maya Shah')
        self.assertEqual(self.supervisor.sessions[-1].voice, 'cinder')
        stored = await (await self.client.get(
            '/v1/meetings/' + body['id'], headers=self.headers())).json()
        self.assertEqual(stored['onBehalfOf'], 'Maya Shah')
        plain = await (await self.create()).json()
        self.assertNotIn('onBehalfOf', plain)
        self.assertNotIn('voice', plain)
        for field, value in (('voice', 'not-a-voice'), ('voice', 7),
                             ('onBehalfOf', 'x' * 121), ('onBehalfOf', ['Maya'])):
            with self.subTest(field=field, value=value):
                response = await self.create(**{field: value})
                self.assertEqual(response.status, 422)

    async def test_direct_create_errors_are_daemon_errors(self):
        with self.assertRaises(DaemonError) as raised:
            await self.daemon.create_meeting(create_payload(agentSession={}))
        self.assertEqual(raised.exception.status, 422)
        session = await self.daemon.create_meeting(create_payload(onBehalfOf='Sam'))
        self.assertEqual(session.on_behalf_of, 'Sam')

    async def test_url_platform_detection_and_create_contract(self):
        zoom = await self.create()
        self.assertEqual(zoom.status, 201)
        body = await zoom.json()
        self.assertEqual(body['platform'], 'zoom')
        self.assertEqual(body['state'], 'joining')
        self.assertEqual(body['startedAt'], TIMESTAMP)
        self.assertTrue(body['id'].startswith('mtg-'))
        for removed in ('agentSession', 'permissions', 'providerCapabilities'):
            self.assertNotIn(removed, body)
        self.assertEqual(len(self.supervisor.started), 1)
        teams = await self.create(meetingUrl=TEAMS_URL)
        self.assertEqual(teams.status, 201)
        self.assertEqual((await teams.json())['platform'], 'teams')
        meet = await self.create(meetingUrl=MEET_URL)
        self.assertEqual(meet.status, 201)
        self.assertEqual((await meet.json())['platform'], 'meet')

    async def test_camera_settings_are_optional_and_presence_is_public(self):
        created = await self.create(camera={'enabled': False, 'defaultOn': False})
        self.assertEqual(created.status, 201)
        body = await created.json()
        self.assertEqual(body['cameraEnabled'], False)
        self.assertEqual(body['cameraState'], 'off')
        self.assertEqual(body['visualState'], 'joining')
        self.assertNotIn('cameraAvatarDataUri', body)
        self.assertEqual(self.supervisor.cameras[-1]['cameraEnabled'], False)
        types = [event.type for event in self.daemon.events.replay(body['id'])]
        self.assertIn('presence.updated', types)
        extra = await self.create(camera={'unknown': True})
        self.assertEqual(extra.status, 422)
        updated = self.daemon.apply_presence(
            body['id'], cameraEnabled=False, cameraState='off', visualState='ended')
        self.assertEqual(updated.visual_state, 'ended')
        dumped = json.dumps(updated.to_dict())
        self.assertNotIn('transcript', dumped.lower())

    async def test_unconfigured_supervisor_and_startup_failure_rollback(self):
        root = Path(self.temporary.name) / 'unconfigured'
        app = create_app(root=root, auth_token=self.auth, clock=self.clock)
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            response = await client.post(
                '/v1/meetings', json=create_payload(), headers=self.headers())
            self.assertEqual(response.status, 503)
            body = await response.json()
            self.assertEqual(body['error']['code'], 'supervisor_unavailable')
            self.assertIn('not configured', body['error']['message'])
            self.assertNotIn(self.auth, json.dumps(body))
        finally:
            await client.close()
        self.supervisor.fail_start = True
        failed = await self.create()
        self.assertEqual(failed.status, 503)
        self.assertEqual((await failed.json())['error']['code'], 'supervisor_unavailable')

    async def test_startup_failure_persists_ended_meeting(self):
        captured = []

        class CaptureSupervisor(FakeSupervisor):
            async def start(inner, meeting, camera_settings=None):
                captured.append(meeting.id)
                raise RuntimeError('admission denied')

        self.supervisor = CaptureSupervisor()
        await self.client.close()
        self.app = create_app(
            root=self.temporary.name + '-ended',
            auth_token=self.auth,
            supervisor=self.supervisor,
            clock=self.clock,
        )
        self.daemon = self.app.runtime_daemon
        self.client = TestClient(TestServer(self.app))
        await self.client.start_server()
        response = await self.create()
        self.assertEqual(response.status, 503)
        meeting_id = captured[0]
        stored = await self.client.get('/v1/meetings/' + meeting_id, headers=self.headers())
        self.assertEqual(stored.status, 200)
        self.assertEqual((await stored.json())['state'], 'ended')
        events = [event.type for event in self.daemon.events.replay(meeting_id)]
        self.assertIn('meeting.ended', events)
        self.assertEqual(self.supervisor.cancelled, [meeting_id])

    async def test_durable_restart_and_permissions(self):
        created = await self.create(onBehalfOf='Sam')
        meeting = await created.json()
        meeting_id = meeting['id']
        snapshot = Path(self.daemon.meetings.root) / meeting_id / 'snapshot.json'
        self.assertEqual(snapshot.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.daemon.meetings.root.stat().st_mode & 0o777, 0o700)
        self.assertFalse((snapshot.parent / 'lease.json').exists())
        os.chmod(self.daemon.meetings.root, 0o777)
        await self.client.get('/v1/meetings/' + meeting_id, headers=self.headers())
        self.assertEqual(self.daemon.meetings.root.stat().st_mode & 0o777, 0o700)
        app = create_app(
            root=self.temporary.name, auth_token=self.auth, supervisor=self.supervisor,
            clock=self.clock)
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            restored = await client.get('/v1/meetings/' + meeting_id, headers=self.headers())
            self.assertEqual(restored.status, 200)
            body = await restored.json()
            self.assertEqual(body['id'], meeting_id)
            self.assertEqual(body['platform'], 'zoom')
            self.assertEqual(body['onBehalfOf'], 'Sam')
        finally:
            await client.close()

    async def test_snapshot_from_a_coding_agent_session_still_loads(self):
        created = await self.create()
        meeting_id = (await created.json())['id']
        path = Path(self.daemon.meetings.root) / meeting_id / 'snapshot.json'
        legacy = json.loads(path.read_text())
        legacy['agentSession'] = {
            'provider': 'codex', 'sessionId': 'thread-1', 'workspace': '/tmp/w',
            'metadata': {'onBehalfOf': 'Grace Hopper'}}
        legacy['permissions'] = {'workspace': 'none', 'commands': 'disabled'}
        path.write_text(json.dumps(legacy))
        (path.parent / 'lease.json').write_text('{"leaseId": "old"}')
        response = await self.client.get('/v1/meetings/' + meeting_id, headers=self.headers())
        self.assertEqual(response.status, 200)
        body = await response.json()
        self.assertEqual(body['onBehalfOf'], 'Grace Hopper')
        self.assertNotIn('agentSession', body)

    async def test_context_rules_and_cancel_idempotence(self):
        created = await self.create()
        meeting_id = (await created.json())['id']
        updated_context = context_payload(objective='Updated objective')
        updated = await self.client.post(
            '/v1/meetings/' + meeting_id + '/context', json=updated_context,
            headers=self.headers())
        self.assertEqual(updated.status, 200)
        self.assertEqual((await updated.json())['context']['objective'], 'Updated objective')
        self.assertEqual(self.supervisor.contexts[-1], (meeting_id, 'Updated objective'))
        first = await self.client.post(
            '/v1/meetings/' + meeting_id + '/cancel', headers=self.headers())
        self.assertEqual(first.status, 200)
        self.assertEqual((await first.json())['state'], 'ended')
        self.assertEqual(self.supervisor.cancelled, [meeting_id])
        second = await self.client.post(
            '/v1/meetings/' + meeting_id + '/cancel', headers=self.headers())
        self.assertEqual(second.status, 200)
        self.assertEqual(self.supervisor.cancelled, [meeting_id])
        late = await self.client.post(
            '/v1/meetings/' + meeting_id + '/context', json=updated_context,
            headers=self.headers())
        self.assertEqual(late.status, 409)

    async def test_handoff_matching_and_event_order(self):
        created = await self.create()
        meeting = await created.json()
        meeting_id = meeting['id']
        missing = await self.client.get(
            '/v1/meetings/' + meeting_id + '/handoff', headers=self.headers())
        self.assertEqual(missing.status, 404)
        mismatch = handoff_payload(meetingId=meeting_id, startedAt='2020-01-01T00:00:00Z')
        with self.assertRaises(DaemonError):
            self.daemon.store_handoff(mismatch)
        await self.client.post('/v1/meetings/' + meeting_id + '/cancel', headers=self.headers())
        ready = handoff_payload(meetingId=meeting_id, startedAt=meeting['startedAt'])
        stored = self.daemon.store_handoff(ready)
        self.assertEqual(stored.meeting_id, meeting_id)
        handoff = await self.client.get(
            '/v1/meetings/' + meeting_id + '/handoff', headers=self.headers())
        self.assertEqual(handoff.status, 200)
        types = [event.type for event in self.daemon.events.replay(meeting_id)]
        self.assertLess(types.index('meeting.ended'), types.index('handoff.ready'))
        body = await handoff.json()
        self.assertNotIn(self.auth, json.dumps(body))

    async def test_sse_order_replay_last_event_id_heartbeat_and_disconnect(self):
        created = await self.create()
        meeting_id = (await created.json())['id']
        response = await self.client.get(
            '/v1/meetings/' + meeting_id + '/events', headers=self.headers())
        self.assertEqual(response.status, 200)
        self.assertIn('text/event-stream', response.headers['Content-Type'])
        events, comments = await read_sse(response, min_events=2, min_comments=1)
        types = [event['event'] for event in events]
        self.assertEqual(types[:2], ['meeting.joining', 'presence.updated'])
        self.assertTrue(any('heartbeat' in comment for comment in comments))
        last_id = events[0]['id']
        await response.release()
        resumed = await self.client.get(
            '/v1/meetings/' + meeting_id + '/events',
            headers=self.headers(**{'Last-Event-ID': last_id}))
        replayed, _comments = await read_sse(resumed, min_events=1)
        self.assertTrue(replayed)
        self.assertNotEqual(replayed[0]['id'], last_id)
        self.assertEqual(replayed[0]['event'], 'presence.updated')
        await resumed.release()

    async def test_corrupt_snapshot_is_fail_closed(self):
        created = await self.create()
        meeting_id = (await created.json())['id']
        path = Path(self.daemon.meetings.root) / meeting_id / 'snapshot.json'
        before = path.read_bytes()
        path.write_bytes(b'')
        response = await self.client.get('/v1/meetings/' + meeting_id, headers=self.headers())
        self.assertEqual(response.status, 409)
        self.assertEqual((await response.json())['error']['code'], 'corrupt_snapshot')
        self.assertEqual(path.read_bytes(), b'')
        path.write_bytes(before)

    async def test_symlink_and_path_resistance(self):
        created = await self.create()
        meeting_id = (await created.json())['id']
        path = Path(self.daemon.meetings.root) / meeting_id / 'snapshot.json'
        victim = Path(self.temporary.name) / 'outside.json'
        victim.write_text('keep-me', encoding='utf-8')
        path.unlink()
        path.symlink_to(victim)
        response = await self.client.get('/v1/meetings/' + meeting_id, headers=self.headers())
        self.assertIn(response.status, (409, 422))
        self.assertEqual(victim.read_text(encoding='utf-8'), 'keep-me')
        traversal = await self.client.get('/v1/meetings/../etc/passwd', headers=self.headers())
        self.assertIn(traversal.status, (400, 404, 422))

    async def test_secret_absence(self):
        created = await self.create()
        body = await created.json()
        dumped = json.dumps(body)
        self.assertNotIn(self.auth, dumped)
        self.assertNotIn('Bearer', dumped)
        self.assertEqual(self.supervisor.started, [body['id']])

    async def test_finalization_failure_ends_the_meeting(self):
        created = await self.create()
        meeting_id = (await created.json())['id']
        self.daemon.record_finalization_failure(meeting_id, 'supervisor_crash')
        stored = await self.client.get('/v1/meetings/' + meeting_id, headers=self.headers())
        self.assertEqual((await stored.json())['state'], 'ended')
        self.daemon.record_finalization_failure(meeting_id, 'again')
        types = [event.type for event in self.daemon.events.replay(meeting_id)]
        self.assertEqual(types.count('meeting.ended'), 1)

    async def test_closed_repository_guard(self):
        created = await self.create()
        meeting_id = (await created.json())['id']
        store = MeetingRepository(self.daemon.meetings.root)
        store.close()
        with self.assertRaises(RuntimeError):
            store.get(meeting_id)

    async def test_forward_lifecycle_and_same_state_idempotence(self):
        created = await self.create()
        meeting_id = (await created.json())['id']
        self.daemon.transition(meeting_id, 'live')
        with self.assertRaises(DaemonError):
            self.daemon.transition(meeting_id, 'joining')
        current = await (await self.client.get(
            '/v1/meetings/' + meeting_id, headers=self.headers())).json()
        self.assertEqual(current['state'], 'live')
        types = [event.type for event in self.daemon.events.replay(meeting_id)]
        self.assertEqual(types.count('meeting.live'), 1)
        self.assertEqual(types.count('meeting.joining'), 1)
        again = self.daemon.transition(meeting_id, 'live')
        self.assertEqual(again.state, 'live')
        types = [event.type for event in self.daemon.events.replay(meeting_id)]
        self.assertEqual(types.count('meeting.live'), 1)
        with self.assertRaises(DaemonError):
            self.daemon.transition(meeting_id, 'waiting_for_admission')
        ended = self.daemon.transition(meeting_id, 'ended', reason='host_ended')
        self.assertEqual(ended.state, 'ended')
        same = self.daemon.transition(meeting_id, 'ended')
        self.assertEqual(same.state, 'ended')
        types = [event.type for event in self.daemon.events.replay(meeting_id)]
        self.assertEqual(types.count('meeting.ended'), 1)
        with self.assertRaises(DaemonError):
            self.daemon.transition(meeting_id, 'live')

    async def test_context_and_cancel_supervisor_failures_are_json(self):
        created = await self.create()
        meeting_id = (await created.json())['id']
        self.supervisor.fail_context = True
        updated = context_payload(objective='Updated objective after persist')
        response = await self.client.post(
            '/v1/meetings/' + meeting_id + '/context', headers=self.headers(), json=updated)
        self.assertEqual(response.content_type, 'application/json')
        self.assertEqual(response.status, 503)
        body = await response.json()
        self.assertEqual(body['error']['code'], 'supervisor_context_failed')
        stored = await (await self.client.get(
            '/v1/meetings/' + meeting_id, headers=self.headers())).json()
        self.assertEqual(stored['context']['objective'], 'Updated objective after persist')
        self.supervisor.fail_context = False
        self.supervisor.fail_cancel = True
        cancelled = await self.client.post(
            '/v1/meetings/' + meeting_id + '/cancel', headers=self.headers())
        self.assertEqual(cancelled.content_type, 'application/json')
        self.assertEqual(cancelled.status, 503)
        self.assertEqual((await cancelled.json())['error']['code'], 'supervisor_cancel_failed')
        still = await (await self.client.get(
            '/v1/meetings/' + meeting_id, headers=self.headers())).json()
        self.assertEqual(still['state'], 'joining')
        types = [event.type for event in self.daemon.events.replay(meeting_id)]
        self.assertNotIn('meeting.ended', types)

    async def test_unexpected_exception_is_json_internal_error(self):
        created = await self.create()
        meeting_id = (await created.json())['id']

        def boom(*_args, **_kwargs):
            raise RuntimeError('secret-token leaked stack')

        self.daemon.get_meeting = boom
        response = await self.client.get('/v1/meetings/' + meeting_id, headers=self.headers())
        self.assertEqual(response.status, 500)
        self.assertEqual(response.content_type, 'application/json')
        body = await response.json()
        self.assertEqual(body['error']['code'], 'internal_error')
        self.assertNotIn('secret-token', json.dumps(body))
        self.assertNotIn(self.auth, json.dumps(body))

    async def test_concurrent_cancel_invokes_supervisor_once(self):
        created = await self.create()
        meeting_id = (await created.json())['id']
        original = self.supervisor.cancel

        async def slow_cancel(meeting):
            await asyncio.sleep(0.05)
            await original(meeting)

        self.supervisor.cancel = slow_cancel
        first, second = await asyncio.gather(
            self.client.post('/v1/meetings/' + meeting_id + '/cancel', headers=self.headers()),
            self.client.post('/v1/meetings/' + meeting_id + '/cancel', headers=self.headers()),
        )
        self.assertEqual(sorted([first.status, second.status]), [200, 200])
        self.assertEqual(self.supervisor.cancelled, [meeting_id])
        types = [event.type for event in self.daemon.events.replay(meeting_id)]
        self.assertEqual(types.count('meeting.ended'), 1)

    async def test_duplicate_and_conflicting_handoff(self):
        created = await self.create()
        meeting = await created.json()
        meeting_id = meeting['id']
        ready = handoff_payload(meetingId=meeting_id, startedAt=meeting['startedAt'])
        self.daemon.store_handoff(ready)
        again = self.daemon.store_handoff(ready)
        self.assertEqual(again.meeting_id, meeting_id)
        types = [event.type for event in self.daemon.events.replay(meeting_id)]
        self.assertEqual(types.count('handoff.ready'), 1)
        other = handoff_payload(
            meetingId=meeting_id, startedAt=meeting['startedAt'], summary='Different summary')
        with self.assertRaises(DaemonError):
            self.daemon.store_handoff(other)

    async def test_retry_restores_meeting_ended_before_handoff_ready(self):
        created = await self.create()
        meeting = await created.json()
        meeting_id = meeting['id']
        ready = handoff_payload(meetingId=meeting_id, startedAt=meeting['startedAt'])
        original = self.daemon.events.append
        self.daemon.events.append = _once_fail(
            original, _event_type_predicate('meeting.ended'),
            RuntimeError('ended append failed'))
        with self.assertRaises(Exception):
            self.daemon.store_handoff(ready)
        types = [event.type for event in self.daemon.events.replay(meeting_id)]
        self.assertNotIn('meeting.ended', types)
        self.assertNotIn('handoff.ready', types)
        self.daemon.events.append = original
        stored = self.daemon.store_handoff(ready)
        self.assertEqual(stored.meeting_id, meeting_id)
        types = [event.type for event in self.daemon.events.replay(meeting_id)]
        self.assertEqual(types.count('meeting.ended'), 1)
        self.assertEqual(types.count('handoff.ready'), 1)
        self.assertLess(types.index('meeting.ended'), types.index('handoff.ready'))

    async def test_retry_restores_handoff_ready(self):
        created = await self.create()
        meeting = await created.json()
        meeting_id = meeting['id']
        ready = handoff_payload(meetingId=meeting_id, startedAt=meeting['startedAt'])
        original = self.daemon.events.append
        self.daemon.events.append = _once_fail(
            original, _event_type_predicate('handoff.ready'),
            RuntimeError('handoff.ready append failed'))
        with self.assertRaises(Exception):
            self.daemon.store_handoff(ready)
        record = self.daemon.meetings.get(meeting_id)
        self.assertIsNotNone(record.handoff)
        types = [event.type for event in self.daemon.events.replay(meeting_id)]
        self.assertIn('meeting.ended', types)
        self.assertNotIn('handoff.ready', types)
        self.daemon.events.append = original
        self.daemon.store_handoff(ready)
        types = [event.type for event in self.daemon.events.replay(meeting_id)]
        self.assertEqual(types.count('handoff.ready'), 1)
        self.assertEqual(types.count('meeting.ended'), 1)
        self.assertLess(types.index('meeting.ended'), types.index('handoff.ready'))

    def _track_created_ids(self):
        ids = []
        original = self.daemon.meetings.put

        def wrapped(session, *args, **kwargs):
            if session.id not in ids:
                ids.append(session.id)
            return original(session, *args, **kwargs)

        self.daemon.meetings.put = wrapped
        return ids

    async def _create_should_fail(self):
        response = await self.create()
        self.assertGreaterEqual(response.status, 500)
        return response

    async def test_create_put_failure_ends_the_meeting(self):
        ids = self._track_created_ids()
        tracked = self.daemon.meetings.put
        self.daemon.meetings.put = _fail_after_once(tracked, RuntimeError('put after write'))
        response = await self._create_should_fail()
        self.assertEqual((await response.json())['error']['code'], 'create_failed')
        meeting_id = ids[0]
        self.assertEqual((await (await self.client.get(
            '/v1/meetings/' + meeting_id, headers=self.headers())).json())['state'], 'ended')
        types = [event.type for event in self.daemon.events.replay(meeting_id)]
        self.assertIn('meeting.ended', types)
        self.assertEqual(self.supervisor.started, [])
        self.assertEqual(self.supervisor.cancelled, [])

    async def test_create_joining_event_failure_ends_the_meeting(self):
        ids = self._track_created_ids()
        original = self.daemon.events.append
        self.daemon.events.append = _once_fail(
            original, _event_type_predicate('meeting.joining'),
            RuntimeError('joining before write'))
        await self._create_should_fail()
        meeting_id = ids[0]
        types = [event.type for event in self.daemon.events.replay(meeting_id)]
        self.assertNotIn('meeting.joining', types)
        self.assertIn('meeting.ended', types)
        self.assertEqual(self.supervisor.started, [])

    async def test_create_start_before_and_after_write(self):
        ids = self._track_created_ids()
        original = self.supervisor.start

        async def fail_before(_meeting, camera_settings=None):
            raise RuntimeError('start before write')

        self.supervisor.start = fail_before
        response = await self._create_should_fail()
        self.assertEqual((await response.json())['error']['code'], 'supervisor_unavailable')
        meeting_id = ids[0]
        self.assertEqual(self.supervisor.started, [])
        self.assertEqual(self.supervisor.cancelled, [meeting_id])

        self.supervisor.started = []
        self.supervisor.cancelled = []

        async def fail_after(meeting, camera_settings=None):
            await original(meeting, camera_settings=camera_settings)
            raise RuntimeError('start after write')

        self.supervisor.start = fail_after
        response = await self._create_should_fail()
        self.assertEqual((await response.json())['error']['code'], 'supervisor_unavailable')
        meeting_id = ids[-1]
        self.assertEqual(self.supervisor.started, [meeting_id])
        self.assertEqual(self.supervisor.cancelled, [meeting_id])
        types = [event.type for event in self.daemon.events.replay(meeting_id)]
        self.assertIn('meeting.ended', types)

    async def test_concurrent_handoff_cancel_and_failure_converge(self):
        created = await self.create()
        meeting = await created.json()
        meeting_id = meeting['id']
        ready = handoff_payload(meetingId=meeting_id, startedAt=meeting['startedAt'])
        original_cancel = self.supervisor.cancel

        async def slow_cancel(meeting_id_arg):
            await asyncio.sleep(0.05)
            await original_cancel(meeting_id_arg)

        self.supervisor.cancel = slow_cancel

        async def delayed_handoff():
            await asyncio.sleep(0)
            return self.daemon.store_handoff(ready)

        async def delayed_failure():
            await asyncio.sleep(0.01)
            return self.daemon.record_finalization_failure(meeting_id, 'race')

        cancel_response, _handoff, _failure = await asyncio.gather(
            self.client.post('/v1/meetings/' + meeting_id + '/cancel', headers=self.headers()),
            delayed_handoff(),
            delayed_failure(),
            return_exceptions=True,
        )
        stored = await (await self.client.get(
            '/v1/meetings/' + meeting_id, headers=self.headers())).json()
        self.assertEqual(stored['state'], 'ended')
        types = [event.type for event in self.daemon.events.replay(meeting_id)]
        self.assertEqual(types.count('meeting.ended'), 1)
        if 'handoff.ready' in types:
            self.assertLess(types.index('meeting.ended'), types.index('handoff.ready'))
        self.assertNotIsInstance(cancel_response, Exception)
        self.assertEqual(cancel_response.status, 200)

    async def test_independent_meetings_do_not_block_each_other(self):
        gate = asyncio.Event()
        original_start = self.supervisor.start

        async def blocked_start(meeting, camera_settings=None):
            if meeting.on_behalf_of == 'Slow':
                await gate.wait()
            await original_start(meeting, camera_settings=camera_settings)

        self.supervisor.start = blocked_start
        slow = asyncio.create_task(self.create(onBehalfOf='Slow'))
        await asyncio.sleep(0.05)
        fast = await self.create(onBehalfOf='Fast')
        self.assertEqual(fast.status, 201, await fast.text())
        self.assertFalse(slow.done())
        gate.set()
        slow_response = await slow
        self.assertEqual(slow_response.status, 201)

    async def test_duplicate_concurrent_handoff_is_exactly_once(self):
        created = await self.create()
        meeting = await created.json()
        meeting_id = meeting['id']
        ready = handoff_payload(meetingId=meeting_id, startedAt=meeting['startedAt'])

        def run_handoff():
            return self.daemon.store_handoff(ready)

        first, second = await asyncio.gather(
            asyncio.to_thread(run_handoff),
            asyncio.to_thread(run_handoff),
        )
        self.assertEqual(first.meeting_id, meeting_id)
        self.assertEqual(second.meeting_id, meeting_id)
        types = [event.type for event in self.daemon.events.replay(meeting_id)]
        self.assertEqual(types.count('handoff.ready'), 1)
        self.assertEqual(types.count('meeting.ended'), 1)


def _once_fail(original, predicate, error):
    state = {'fired': False}

    def wrapper(*args, **kwargs):
        if not state['fired'] and predicate(*args, **kwargs):
            state['fired'] = True
            raise error
        return original(*args, **kwargs)

    return wrapper


def _fail_after_once(original, error, predicate=None):
    state = {'fired': False}

    def wrapper(*args, **kwargs):
        if predicate is not None and not predicate(*args, **kwargs):
            return original(*args, **kwargs)
        result = original(*args, **kwargs)
        if not state['fired']:
            state['fired'] = True
            raise error
        return result

    return wrapper


def _event_type_predicate(wanted):
    def predicate(event, *args, **kwargs):
        if isinstance(event, dict):
            return event.get('type') == wanted
        return getattr(event, 'type', None) == wanted
    return predicate


class MeetingRepositoryUnitTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.store = MeetingRepository(self.temporary.name)

    def tearDown(self):
        self.store.close()
        self.temporary.cleanup()

    def test_empty_snapshot_is_corruption_not_absence(self):
        session = MeetingSession.from_dict({
            'id': 'mtg-repo1',
            'platform': 'zoom',
            'meetingUrl': ZOOM_URL,
            'context': context_payload(),
            'state': 'joining',
            'startedAt': TIMESTAMP,
        })
        self.store.put(session)
        path = self.store.root / 'mtg-repo1' / 'snapshot.json'
        path.write_bytes(b'  \n')
        with self.assertRaises(MeetingCorruptionError):
            self.store.get('mtg-repo1')
        self.assertEqual(path.read_bytes(), b'  \n')
        self.assertIsNone(self.store.get('mtg-missing'))


if __name__ == '__main__':
    unittest.main()
