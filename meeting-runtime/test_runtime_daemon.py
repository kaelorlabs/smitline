import asyncio
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from agent_sessions import MeetingSession
from meeting_repository import MeetingCorruptionError, MeetingRepository
from session_leases import LeaseStateError, SessionLeaseStore
from test_schemas import (
    MEET_URL, TEAMS_URL, TIMESTAMP, ZOOM_URL, agent_session_payload, context_payload,
    handoff_payload, permissions_payload,
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
        self.contexts = []
        self.cancelled = []
        self.fail_start = False
        self.fail_context = False
        self.fail_cancel = False
        self.start_error = RuntimeError('join failed')
        self.context_error = RuntimeError('context failed')
        self.cancel_error = RuntimeError('cancel failed')

    async def start(self, meeting, camera_settings=None, screen_share_settings=None):
        if self.fail_start:
            raise self.start_error
        self.started.append(meeting.id)
        self.cameras = getattr(self, 'cameras', [])
        self.cameras.append(camera_settings)
        self.screen_shares = getattr(self, 'screen_shares', [])
        self.screen_shares.append(screen_share_settings)

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
        'agentSession': agent_session_payload(),
        'context': context_payload(),
        'permissions': permissions_payload(),
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
        self.assertNotIn('leaseId', json.dumps(body))
        self.assertNotIn(self.auth, json.dumps(body))

    async def test_providers_status_and_cursor_exact_resume_capability(self):
        listed = await self.client.get('/v1/providers', headers=self.headers())
        self.assertEqual(listed.status, 200)
        body = await listed.json()
        self.assertEqual([item['id'] for item in body['providers']], ['codex', 'cursor', 'claude-code'])
        dumped = json.dumps(body)
        self.assertNotIn('token', dumped.lower())
        created = await self.create()
        meeting = await created.json()
        self.assertEqual(meeting['agentSession']['provider'], 'codex')
        self.assertEqual(meeting['providerCapabilities']['id'], 'codex')
        from providers.cursor import CursorProvider
        from providers.registry import ProviderRegistry
        self.daemon.provider_registry = ProviderRegistry(providers={
            'cursor': CursorProvider(command='/bin/echo', help_text='''
Usage: fake-agent
  --print <prompt>
'''),
        })
        rejected = await self.create(agentSession=agent_session_payload(
            provider='cursor', sessionId='thread-cursor-1',
            metadata={'continuity': 'exact', 'source': 'host'}))
        self.assertEqual(rejected.status, 422)
        detail = await rejected.json()
        self.assertIn('exact_resume_unsupported', json.dumps(detail))
        allowed = await self.create(agentSession=agent_session_payload(
            provider='cursor', sessionId='local-portal',
            metadata={'continuity': 'context', 'source': 'local-portal'}))
        self.assertEqual(allowed.status, 201)
        cursor_meeting = await allowed.json()
        self.assertEqual(cursor_meeting['agentSession']['provider'], 'cursor')
        self.assertFalse(cursor_meeting['providerCapabilities']['exactSessionResume'])
        self.assertTrue(cursor_meeting['providerCapabilities']['contextContinuity'])

    async def test_runner_pairing_is_single_use_and_omits_enrollment_from_status(self):
        idle = await self.client.get('/v1/runner', headers=self.headers())
        self.assertEqual(idle.status, 200)
        body = await idle.json()
        self.assertFalse(body['paired'])
        self.assertEqual(body['mode'], 'loopback')
        started = await self.client.post('/v1/runner/pair', json={}, headers=self.headers())
        self.assertEqual(started.status, 201)
        pairing = await started.json()
        self.assertIn('pairingCode', pairing)
        completed = await self.client.post(
            '/v1/runner/pair/complete',
            json={'pairingId': pairing['pairingId'], 'pairingCode': pairing['pairingCode']},
            headers=self.headers())
        self.assertEqual(completed.status, 201)
        enrollment = (await completed.json())['deviceEnrollment']
        replay = await self.client.post(
            '/v1/runner/pair/complete',
            json={'pairingId': pairing['pairingId'], 'pairingCode': pairing['pairingCode']},
            headers=self.headers())
        self.assertEqual(replay.status, 409)
        status = await (await self.client.get('/v1/runner', headers=self.headers())).json()
        dumped = json.dumps(status)
        self.assertTrue(status['paired'])
        self.assertNotIn(pairing['pairingCode'], dumped)
        self.assertNotIn(enrollment, dumped)
        self.assertNotIn('deviceEnrollment', dumped)
        unpaired = await self.client.post('/v1/runner/unpair', json={}, headers=self.headers())
        self.assertEqual(unpaired.status, 200)
        self.assertFalse((await unpaired.json())['paired'])

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

    async def test_url_platform_detection_and_create_contract(self):
        zoom = await self.create()
        self.assertEqual(zoom.status, 201)
        body = await zoom.json()
        self.assertEqual(body['platform'], 'zoom')
        self.assertEqual(body['state'], 'joining')
        self.assertEqual(body['startedAt'], TIMESTAMP)
        self.assertTrue(body['id'].startswith('mtg-'))
        self.assertEqual(len(self.supervisor.started), 1)
        teams = await self.create(
            meetingUrl=TEAMS_URL,
            agentSession=agent_session_payload(sessionId='thread-teams-1'),
        )
        self.assertEqual(teams.status, 201)
        self.assertEqual((await teams.json())['platform'], 'teams')
        meet = await self.create(
            meetingUrl=MEET_URL,
            agentSession=agent_session_payload(sessionId='thread-meet-1'),
        )
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
        types = [event.type for event in self.daemon.events.replay(body['id'])]
        self.assertIn('presence.updated', types)
        extra = await self.create(camera={'unknown': True}, agentSession=agent_session_payload(sessionId='thread-cam-2'))
        self.assertEqual(extra.status, 422)
        updated = self.daemon.apply_presence(
            body['id'], cameraEnabled=False, cameraState='off', visualState='ended')
        self.assertEqual(updated.visual_state, 'ended')
        dumped = json.dumps(updated.to_dict())
        self.assertNotIn('transcript', dumped.lower())

    async def test_exact_continuity_rejects_last_before_join(self):
        response = await self.create(agentSession=agent_session_payload(sessionId='--last'))
        self.assertEqual(response.status, 422)
        self.assertEqual(self.supervisor.started, [])
        self.assertIsNone(self.daemon.leases.get('codex', '--last'))
        claimed = await self.create(agentSession=agent_session_payload(
            sessionId='local-portal',
            metadata={'source': 'codex-app-server', 'continuity': 'exact'},
        ))
        self.assertEqual(claimed.status, 422)
        self.assertEqual(self.supervisor.started, [])
        portal = await self.create(agentSession=agent_session_payload(
            sessionId='local-portal',
            metadata={'source': 'local-portal', 'continuity': 'context'},
        ))
        self.assertEqual(portal.status, 201)
        self.assertEqual(len(self.supervisor.started), 1)

    async def test_create_acquire_conflict_is_atomic(self):
        first, second = await asyncio.gather(self.create(), self.create())
        statuses = sorted([first.status, second.status])
        self.assertEqual(statuses, [201, 409])
        winner = first if first.status == 201 else second
        meeting = await winner.json()
        self.assertEqual(len(self.supervisor.started), 1)
        leased = await self.client.get(
            '/v1/agent-sessions/codex/thread-origin-1/status', headers=self.headers())
        self.assertEqual(leased.status, 200)
        status = await leased.json()
        self.assertEqual(status['meetingId'], meeting['id'])
        self.assertEqual(status['state'], 'in_meeting')
        self.assertNotIn('leaseId', status)

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
        try:
            failed = await self.create(agentSession=agent_session_payload(sessionId='thread-fail-1'))
            self.assertEqual(failed.status, 503)
            meeting = await failed.json()
            self.assertEqual(meeting.get('error', {}).get('code'), 'supervisor_unavailable')
            status = await self.client.get(
                '/v1/agent-sessions/codex/thread-fail-1/status', headers=self.headers())
            self.assertEqual(status.status, 404)
        finally:
            self.supervisor.fail_start = False

    async def test_startup_failure_persists_ended_meeting(self):
        self.supervisor.fail_start = True
        captured = []

        class CaptureSupervisor(FakeSupervisor):
            async def start(inner, meeting, camera_settings=None, screen_share_settings=None):
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
        body = await stored.json()
        self.assertEqual(body['state'], 'ended')
        events = [event.type for event in self.daemon.events.replay(meeting_id)]
        self.assertIn('meeting.ended', events)
        self.assertIn('agent_session.released', events)
        self.assertIsNone(self.daemon.leases.get('codex', 'thread-origin-1'))

    async def test_durable_restart_and_permissions(self):
        created = await self.create()
        meeting = await created.json()
        meeting_id = meeting['id']
        snapshot = Path(self.daemon.meetings.root) / meeting_id / 'snapshot.json'
        self.assertEqual(snapshot.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.daemon.meetings.root.stat().st_mode & 0o777, 0o700)
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
            status = await client.get(
                '/v1/agent-sessions/codex/thread-origin-1/status', headers=self.headers())
            self.assertEqual(status.status, 200)
        finally:
            await client.close()

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
        status = await self.client.get(
            '/v1/agent-sessions/codex/thread-origin-1/status', headers=self.headers())
        self.assertEqual(status.status, 200)
        premature = await self.client.delete(
            '/v1/agent-sessions/codex/thread-origin-1/lease', headers=self.headers())
        self.assertEqual(premature.status, 409)

    async def test_handoff_matching_finalization_and_release_order(self):
        created = await self.create()
        meeting = await created.json()
        meeting_id = meeting['id']
        missing = await self.client.get(
            '/v1/meetings/' + meeting_id + '/handoff', headers=self.headers())
        self.assertEqual(missing.status, 404)
        mismatch = handoff_payload(meetingId=meeting_id, startedAt='2020-01-01T00:00:00Z')
        with self.assertRaises(DaemonError):
            self.daemon.store_handoff(mismatch)
        status = await self.client.get(
            '/v1/agent-sessions/codex/thread-origin-1/status', headers=self.headers())
        self.assertEqual(status.status, 200)
        await self.client.post('/v1/meetings/' + meeting_id + '/cancel', headers=self.headers())
        ready = handoff_payload(meetingId=meeting_id, startedAt=meeting['startedAt'])
        stored = self.daemon.store_handoff(ready)
        self.assertEqual(stored.meeting_id, meeting_id)
        handoff = await self.client.get(
            '/v1/meetings/' + meeting_id + '/handoff', headers=self.headers())
        self.assertEqual(handoff.status, 200)
        types = [event.type for event in self.daemon.events.replay(meeting_id)]
        self.assertLess(types.index('handoff.ready'), types.index('agent_session.released'))
        released = await self.client.get(
            '/v1/agent-sessions/codex/thread-origin-1/status', headers=self.headers())
        self.assertEqual(released.status, 404)
        body = await handoff.json()
        self.assertNotIn('leaseId', json.dumps(body))
        self.assertNotIn(self.auth, json.dumps(body))

    async def test_lease_endpoints_heartbeat_and_no_token_leakage(self):
        created = await self.create()
        meeting_id = (await created.json())['id']
        status = await self.client.get(
            '/v1/agent-sessions/codex/thread-origin-1/status', headers=self.headers())
        before = await status.json()
        beat = await self.client.post(
            '/v1/agent-sessions/codex/thread-origin-1/lease', headers=self.headers())
        self.assertEqual(beat.status, 200)
        after = await beat.json()
        self.assertEqual(after['meetingId'], meeting_id)
        self.assertNotIn('leaseId', after)
        self.assertNotIn('leaseId', before)
        self.assertGreaterEqual(after['lastHeartbeat'], before['lastHeartbeat'])
        gone = await self.client.get(
            '/v1/agent-sessions/codex/missing-thread/status', headers=self.headers())
        self.assertEqual(gone.status, 404)

    async def test_sse_order_replay_last_event_id_heartbeat_and_disconnect(self):
        created = await self.create()
        meeting_id = (await created.json())['id']
        response = await self.client.get(
            '/v1/meetings/' + meeting_id + '/events', headers=self.headers())
        self.assertEqual(response.status, 200)
        self.assertIn('text/event-stream', response.headers['Content-Type'])
        events, comments = await read_sse(response, min_events=2, min_comments=1)
        types = [event['event'] for event in events]
        self.assertEqual(types[:2], ['agent_session.locked', 'meeting.joining'])
        self.assertTrue(any('heartbeat' in comment for comment in comments))
        for event in events:
            self.assertNotIn('leaseId', json.dumps(event['data']))
        last_id = events[0]['id']
        await response.release()
        resumed = await self.client.get(
            '/v1/meetings/' + meeting_id + '/events',
            headers=self.headers(**{'Last-Event-ID': last_id}))
        replayed, _comments = await read_sse(resumed, min_events=1)
        self.assertTrue(replayed)
        self.assertNotEqual(replayed[0]['id'], last_id)
        self.assertEqual(replayed[0]['event'], 'meeting.joining')
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
        conflict = await self.create()
        self.assertEqual(conflict.status, 409)
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

    async def test_secret_absence_and_default_supervisor_does_not_fake_start(self):
        created = await self.create()
        body = await created.json()
        dumped = json.dumps(body)
        self.assertNotIn('leaseId', dumped)
        self.assertNotIn(self.auth, dumped)
        self.assertNotIn('Bearer', dumped)
        lease_path = Path(self.daemon.meetings.root) / body['id'] / 'lease.json'
        self.assertTrue(lease_path.exists())
        self.assertEqual(self.supervisor.started, [body['id']])

    async def test_finalization_failure_releases_lease(self):
        created = await self.create()
        meeting_id = (await created.json())['id']
        self.daemon.record_finalization_failure(meeting_id, 'handoff_append_failed')
        status = await self.client.get(
            '/v1/agent-sessions/codex/thread-origin-1/status', headers=self.headers())
        self.assertEqual(status.status, 404)
        stored = await self.client.get('/v1/meetings/' + meeting_id, headers=self.headers())
        self.assertEqual((await stored.json())['state'], 'ended')

    async def test_closed_repository_guard(self):
        created = await self.create()
        meeting_id = (await created.json())['id']
        store = MeetingRepository(self.daemon.meetings.root)
        store.close()
        with self.assertRaises(RuntimeError):
            store.get(meeting_id)
        leases = SessionLeaseStore(self.daemon.leases.root)
        leases.close()
        with self.assertRaises(RuntimeError):
            leases.get('codex', 'thread-origin-1')

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

    async def test_historical_and_second_meeting_use_active_lease(self):
        historical = MeetingSession.from_dict({
            'id': 'mtg-0000000000000000',
            'platform': 'zoom',
            'meetingUrl': ZOOM_URL,
            'agentSession': agent_session_payload(),
            'context': context_payload(),
            'permissions': permissions_payload(),
            'state': 'ended',
            'startedAt': '2026-09-16T16:00:00Z',
        })
        self.daemon.meetings.put(historical, None)
        created = await self.create()
        self.assertEqual(created.status, 201)
        meeting_id = (await created.json())['id']
        status = await self.client.get(
            '/v1/agent-sessions/codex/thread-origin-1/status', headers=self.headers())
        self.assertEqual(status.status, 200)
        self.assertEqual((await status.json())['meetingId'], meeting_id)
        beat = await self.client.post(
            '/v1/agent-sessions/codex/thread-origin-1/lease',
            headers=self.headers(), json={})
        self.assertEqual(beat.status, 200)
        ready = handoff_payload(meetingId=meeting_id, startedAt=TIMESTAMP)
        self.daemon.store_handoff(ready)
        second = await self.create()
        self.assertEqual(second.status, 201)
        second_id = (await second.json())['id']
        beat = await self.client.post(
            '/v1/agent-sessions/codex/thread-origin-1/lease',
            headers=self.headers(), json={})
        self.assertEqual(beat.status, 200)
        self.assertEqual((await beat.json())['meetingId'], second_id)

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

    async def test_false_release_is_prevented_when_release_fails(self):
        created = await self.create()
        meeting_id = (await created.json())['id']

        def boom(*_args, **_kwargs):
            raise LeaseStateError('cannot release')

        self.daemon.leases.release = boom
        with self.assertRaises(DaemonError):
            self.daemon.record_finalization_failure(meeting_id, 'supervisor crash')
        events = [event.type for event in self.daemon.events.replay(meeting_id)]
        self.assertNotIn('agent_session.released', events)
        self.assertIsNotNone(self.daemon.leases.get('codex', 'thread-origin-1'))
        lease_path = Path(self.daemon.meetings.root) / meeting_id / 'lease.json'
        self.assertTrue(lease_path.exists())

    async def test_active_turn_handoff_is_immutable(self):
        created = await self.create()
        session = await created.json()
        meeting_id = session['id']
        record = self.daemon.meetings.get(meeting_id)
        snapshot = (Path(self.daemon.meetings.root) / meeting_id / 'snapshot.json').read_bytes()
        self.daemon.leases.start_turn('codex', 'thread-origin-1', record.lease_token, 'turn-1')
        handoff = handoff_payload(meetingId=meeting_id, startedAt=session['startedAt'])
        with self.assertRaises(DaemonError):
            self.daemon.store_handoff(handoff)
        self.assertEqual(
            (Path(self.daemon.meetings.root) / meeting_id / 'snapshot.json').read_bytes(),
            snapshot)
        self.assertIsNone(self.daemon.meetings.get(meeting_id).handoff)
        types = [event.type for event in self.daemon.events.replay(meeting_id)]
        self.assertNotIn('handoff.ready', types)
        self.assertNotIn('meeting.ended', types)
        self.assertEqual(self.daemon.meetings.get(meeting_id).session.state, 'joining')
        status = await self.client.get(
            '/v1/agent-sessions/codex/thread-origin-1/status', headers=self.headers())
        self.assertEqual(status.status, 200)

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
        self.assertEqual(types.count('agent_session.released'), 1)
        other = handoff_payload(
            meetingId=meeting_id, startedAt=meeting['startedAt'], summary='Different summary')
        with self.assertRaises(DaemonError):
            self.daemon.store_handoff(other)

    async def test_partial_handoff_retries(self):
        created = await self.create()
        meeting = await created.json()
        meeting_id = meeting['id']
        ready = handoff_payload(meetingId=meeting_id, startedAt=meeting['startedAt'])
        original_complete = self.daemon.leases.complete_finalization

        def boom_complete(*args, **kwargs):
            raise LeaseStateError('complete failed')

        self.daemon.leases.complete_finalization = boom_complete
        with self.assertRaises(DaemonError):
            self.daemon.store_handoff(ready)
        self.assertIsNotNone(self.daemon.meetings.get(meeting_id).handoff)
        self.assertIsNotNone(self.daemon.leases.get('codex', 'thread-origin-1'))
        self.daemon.leases.complete_finalization = original_complete
        original_release = self.daemon.leases.release

        def boom_release(*args, **kwargs):
            raise LeaseStateError('cannot release')

        self.daemon.leases.release = boom_release
        with self.assertRaises(DaemonError):
            self.daemon.store_handoff(ready)
        self.daemon.leases.release = original_release
        original_clear = self.daemon.meetings.clear_lease_token

        def boom_clear(*args, **kwargs):
            raise RuntimeError('clear failed')

        self.daemon.meetings.clear_lease_token = boom_clear
        with self.assertRaises(Exception):
            self.daemon.store_handoff(ready)
        self.daemon.meetings.clear_lease_token = original_clear
        stored = self.daemon.store_handoff(ready)
        self.assertEqual(stored.meeting_id, meeting_id)
        self.assertIsNone(self.daemon.leases.get('codex', 'thread-origin-1'))
        types = [event.type for event in self.daemon.events.replay(meeting_id)]
        self.assertEqual(types.count('handoff.ready'), 1)
        self.assertEqual(types.count('agent_session.released'), 1)
        self.assertEqual(types.count('meeting.ended'), 1)
        self.assertLess(types.index('meeting.ended'), types.index('handoff.ready'))
        self.assertLess(types.index('handoff.ready'), types.index('agent_session.released'))

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
        self.assertIsNotNone(self.daemon.leases.get('codex', 'thread-origin-1'))
        self.daemon.events.append = original
        stored = self.daemon.store_handoff(ready)
        self.assertEqual(stored.meeting_id, meeting_id)
        types = [event.type for event in self.daemon.events.replay(meeting_id)]
        self.assertEqual(types.count('meeting.ended'), 1)
        self.assertEqual(types.count('handoff.ready'), 1)
        self.assertEqual(types.count('agent_session.released'), 1)
        self.assertLess(types.index('meeting.ended'), types.index('handoff.ready'))
        self.assertLess(types.index('handoff.ready'), types.index('agent_session.released'))
        self.assertIsNone(self.daemon.leases.get('codex', 'thread-origin-1'))

    async def test_retry_restores_handoff_ready_before_release(self):
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
        self.assertIsNotNone(self.daemon.leases.get('codex', 'thread-origin-1'))
        self.daemon.events.append = original
        self.daemon.store_handoff(ready)
        types = [event.type for event in self.daemon.events.replay(meeting_id)]
        self.assertEqual(types.count('handoff.ready'), 1)
        self.assertEqual(types.count('meeting.ended'), 1)
        self.assertEqual(types.count('agent_session.released'), 1)
        self.assertLess(types.index('meeting.ended'), types.index('handoff.ready'))
        self.assertLess(types.index('handoff.ready'), types.index('agent_session.released'))
        self.assertIsNone(self.daemon.leases.get('codex', 'thread-origin-1'))

    def _track_acquired_ids(self):
        ids = []
        original = self.daemon.leases.acquire

        def wrapped(agent, meeting_id):
            ids.append(meeting_id)
            return original(agent, meeting_id)

        self.daemon.leases.acquire = wrapped
        return ids

    async def _create_should_fail(self, session_id='thread-origin-1'):
        response = await self.create(agentSession=agent_session_payload(sessionId=session_id))
        self.assertGreaterEqual(response.status, 500)
        return response

    async def test_create_put_before_write_releases_lease(self):
        ids = self._track_acquired_ids()
        original = self.daemon.meetings.put
        self.daemon.meetings.put = _once_fail(
            original, lambda *a, **k: True, RuntimeError('put before write'))
        response = await self._create_should_fail()
        self.assertEqual((await response.json())['error']['code'], 'create_failed')
        meeting_id = ids[0]
        stored = await self.client.get('/v1/meetings/' + meeting_id, headers=self.headers())
        self.assertEqual(stored.status, 200)
        self.assertEqual((await stored.json())['state'], 'ended')
        self.assertIsNone(self.daemon.leases.get('codex', 'thread-origin-1'))
        types = [event.type for event in self.daemon.events.replay(meeting_id)]
        self.assertIn('meeting.ended', types)
        self.assertIn('agent_session.released', types)
        self.assertEqual(self.supervisor.started, [])
        self.assertEqual(self.supervisor.cancelled, [])

    async def test_create_put_after_write_ends_and_releases(self):
        ids = self._track_acquired_ids()
        original = self.daemon.meetings.put
        self.daemon.meetings.put = _fail_after_once(original, RuntimeError('put after write'))
        response = await self._create_should_fail()
        self.assertEqual((await response.json())['error']['code'], 'create_failed')
        meeting_id = ids[0]
        self.assertEqual((await (await self.client.get(
            '/v1/meetings/' + meeting_id, headers=self.headers())).json())['state'], 'ended')
        self.assertIsNone(self.daemon.leases.get('codex', 'thread-origin-1'))
        types = [event.type for event in self.daemon.events.replay(meeting_id)]
        self.assertIn('meeting.ended', types)
        self.assertIn('agent_session.released', types)
        self.assertEqual(self.supervisor.started, [])

    async def test_create_locked_event_before_and_after_write(self):
        ids = self._track_acquired_ids()
        original = self.daemon.events.append
        self.daemon.events.append = _once_fail(
            original, _event_type_predicate('agent_session.locked'),
            RuntimeError('locked before write'))
        await self._create_should_fail('thread-locked-before')
        meeting_id = ids[0]
        types = [event.type for event in self.daemon.events.replay(meeting_id)]
        self.assertNotIn('agent_session.locked', types)
        self.assertIn('meeting.ended', types)
        self.assertIn('agent_session.released', types)
        self.assertIsNone(self.daemon.leases.get('codex', 'thread-locked-before'))

        ids = self._track_acquired_ids()
        self.daemon.events.append = _fail_after_once(
            original, RuntimeError('locked after write'),
            predicate=_event_type_predicate('agent_session.locked'))
        await self._create_should_fail('thread-locked-after')
        meeting_id = ids[-1]
        types = [event.type for event in self.daemon.events.replay(meeting_id)]
        self.assertIn('agent_session.locked', types)
        self.assertNotIn('meeting.joining', types)
        self.assertIn('meeting.ended', types)
        self.assertIsNone(self.daemon.leases.get('codex', 'thread-locked-after'))

    async def test_create_joining_event_before_and_after_write(self):
        ids = self._track_acquired_ids()
        original = self.daemon.events.append
        self.daemon.events.append = _once_fail(
            original, _event_type_predicate('meeting.joining'),
            RuntimeError('joining before write'))
        await self._create_should_fail('thread-joining-before')
        meeting_id = ids[0]
        types = [event.type for event in self.daemon.events.replay(meeting_id)]
        self.assertIn('agent_session.locked', types)
        self.assertNotIn('meeting.joining', types)
        self.assertIn('meeting.ended', types)
        self.assertIsNone(self.daemon.leases.get('codex', 'thread-joining-before'))

        ids = self._track_acquired_ids()
        self.daemon.events.append = _fail_after_once(
            original, RuntimeError('joining after write'),
            predicate=_event_type_predicate('meeting.joining'))
        await self._create_should_fail('thread-joining-after')
        meeting_id = ids[-1]
        types = [event.type for event in self.daemon.events.replay(meeting_id)]
        self.assertIn('meeting.joining', types)
        self.assertEqual(self.supervisor.started, [])
        self.assertIn('meeting.ended', types)
        self.assertIsNone(self.daemon.leases.get('codex', 'thread-joining-after'))

    async def test_enter_meeting_after_start_cancels_supervisor(self):
        ids = self._track_acquired_ids()
        original = self.daemon.leases.enter_meeting
        self.daemon.leases.enter_meeting = _once_fail(
            original, lambda *a, **k: True, RuntimeError('enter after start'))
        response = await self._create_should_fail('thread-enter-1')
        self.assertEqual((await response.json())['error']['code'], 'create_failed')
        meeting_id = ids[0]
        self.assertEqual(self.supervisor.started, [meeting_id])
        self.assertEqual(self.supervisor.cancelled, [meeting_id])
        stored = await (await self.client.get(
            '/v1/meetings/' + meeting_id, headers=self.headers())).json()
        self.assertEqual(stored['state'], 'ended')
        self.assertIsNone(self.daemon.leases.get('codex', 'thread-enter-1'))
        types = [event.type for event in self.daemon.events.replay(meeting_id)]
        self.assertIn('meeting.ended', types)
        self.assertIn('agent_session.released', types)
        self.assertEqual(types.count('agent_session.released'), 1)

    async def test_create_start_before_and_after_write(self):
        ids = self._track_acquired_ids()
        original = self.supervisor.start

        async def fail_before(_meeting, camera_settings=None, screen_share_settings=None):
            raise RuntimeError('start before write')

        self.supervisor.start = fail_before
        response = await self._create_should_fail('thread-start-before')
        self.assertEqual((await response.json())['error']['code'], 'supervisor_unavailable')
        meeting_id = ids[0]
        self.assertEqual(self.supervisor.started, [])
        self.assertEqual(self.supervisor.cancelled, [meeting_id])
        self.assertIsNone(self.daemon.leases.get('codex', 'thread-start-before'))

        ids = self._track_acquired_ids()
        self.supervisor.started = []
        self.supervisor.cancelled = []

        async def fail_after(meeting, camera_settings=None, screen_share_settings=None):
            await original(meeting, camera_settings=camera_settings,
                           screen_share_settings=screen_share_settings)
            raise RuntimeError('start after write')

        self.supervisor.start = fail_after
        response = await self._create_should_fail('thread-start-after')
        self.assertEqual((await response.json())['error']['code'], 'supervisor_unavailable')
        meeting_id = ids[-1]
        self.assertEqual(self.supervisor.started, [meeting_id])
        self.assertEqual(self.supervisor.cancelled, [meeting_id])
        self.assertIsNone(self.daemon.leases.get('codex', 'thread-start-after'))
        types = [event.type for event in self.daemon.events.replay(meeting_id)]
        self.assertIn('meeting.ended', types)
        self.assertIn('agent_session.released', types)

    async def test_create_cleanup_failure_preserves_lease(self):
        ids = self._track_acquired_ids()
        original_put = self.daemon.meetings.put
        self.daemon.meetings.put = _once_fail(
            original_put, lambda *a, **k: True, RuntimeError('put before write'))
        original_release = self.daemon.leases.release

        def boom(*_a, **_k):
            raise LeaseStateError('cannot release')

        self.daemon.leases.release = boom
        response = await self._create_should_fail('thread-cleanup-1')
        self.assertEqual(response.status, 503)
        self.assertEqual((await response.json())['error']['code'], 'retry_required')
        meeting_id = ids[0]
        types = [event.type for event in self.daemon.events.replay(meeting_id)]
        self.assertNotIn('agent_session.released', types)
        self.assertIsNotNone(self.daemon.leases.get('codex', 'thread-cleanup-1'))
        lease_path = Path(self.daemon.meetings.root) / meeting_id / 'lease.json'
        self.assertTrue(lease_path.exists())

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
            try:
                return self.daemon.record_finalization_failure(meeting_id, 'race')
            except DaemonError as error:
                return error

        cancel_response, handoff_result, failure_result = await asyncio.gather(
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
            if 'agent_session.released' in types:
                self.assertLess(types.index('handoff.ready'), types.index('agent_session.released'))
        if 'agent_session.released' in types:
            self.assertIsNone(self.daemon.leases.get('codex', 'thread-origin-1'))
        else:
            self.assertIsNotNone(self.daemon.leases.get('codex', 'thread-origin-1'))
        self.assertEqual(types.count('agent_session.released'), 0 if (
            self.daemon.leases.get('codex', 'thread-origin-1') is not None) else 1)
        self.assertNotIsInstance(cancel_response, Exception)
        self.assertEqual(cancel_response.status, 200)

    async def test_independent_meetings_do_not_block_each_other(self):
        gate = asyncio.Event()
        original_start = self.supervisor.start

        async def blocked_start(meeting, camera_settings=None, screen_share_settings=None):
            if meeting.agent_session.session_id == 'thread-origin-1':
                await gate.wait()
            await original_start(meeting, camera_settings=camera_settings,
                                 screen_share_settings=screen_share_settings)

        self.supervisor.start = blocked_start
        slow = asyncio.create_task(self.create())
        await asyncio.sleep(0.05)
        fast = await self.create(agentSession=agent_session_payload(sessionId='thread-fast-1'))
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
        self.assertEqual(types.count('agent_session.released'), 1)
        self.assertIsNone(self.daemon.leases.get('codex', 'thread-origin-1'))

    async def test_exact_append_failure_releases_lease_and_retry_still_succeeds(self):
        created = await self.create()
        meeting = await created.json()
        meeting_id = meeting['id']
        handoff_id = 'hnd-' + meeting_id
        self.daemon.prepare_finalization(meeting_id)
        self.daemon.note_append_failure(meeting_id, handoff_id, 'codex unavailable')
        lease = await self.client.get(
            '/v1/agent-sessions/codex/thread-origin-1/status', headers=self.headers())
        self.assertEqual(lease.status, 404)
        missing = await self.client.get(
            '/v1/meetings/' + meeting_id + '/handoff', headers=self.headers())
        self.assertEqual(missing.status, 404)
        types = [event.type for event in self.daemon.events.replay(meeting_id)]
        self.assertIn('handoff.append_failed', types)
        self.assertNotIn('handoff.ready', types)
        self.assertIn('agent_session.released', types)

        async def retry(_meeting_id):
            ready = handoff_payload(meetingId=meeting_id, startedAt=meeting['startedAt'])
            self.daemon.store_handoff(ready)
            return {'status': 'ready'}

        self.supervisor.retry_handoff = retry
        duplicated = await asyncio.gather(
            self.client.post(
                '/v1/meetings/' + meeting_id + '/handoff/retry',
                json={}, headers=self.headers()),
            self.client.post(
                '/v1/meetings/' + meeting_id + '/handoff/retry',
                json={}, headers=self.headers()),
        )
        self.assertEqual(sorted(item.status for item in duplicated), [200, 200])
        types = [event.type for event in self.daemon.events.replay(meeting_id)]
        self.assertEqual(types.count('handoff.ready'), 1)
        self.assertEqual(types.count('agent_session.released'), 1)
        released = await self.client.get(
            '/v1/agent-sessions/codex/thread-origin-1/status', headers=self.headers())
        self.assertEqual(released.status, 404)
        self.daemon.note_append_failure(meeting_id, handoff_id, 'again')
        self.assertEqual(
            [event.type for event in self.daemon.events.replay(meeting_id)].count(
                'handoff.append_failed'),
            1,
        )

    async def test_prepare_finalization_settles_active_turn(self):
        created = await self.create()
        session = await created.json()
        meeting_id = session['id']
        record = self.daemon.meetings.get(meeting_id)
        self.daemon.leases.start_turn('codex', 'thread-origin-1', record.lease_token, 'turn-1')
        self.daemon.prepare_finalization(meeting_id)
        lease = self.daemon.leases.get('codex', 'thread-origin-1')
        self.assertIsNone(lease.active_delegated_turn)
        self.assertEqual(lease.state, 'finalizing')
        stored = self.daemon.store_handoff(
            handoff_payload(meetingId=meeting_id, startedAt=session['startedAt']))
        self.assertEqual(stored.meeting_id, meeting_id)
        self.assertIsNone(self.daemon.leases.get('codex', 'thread-origin-1'))


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
        from agent_sessions import MeetingSession
        session = MeetingSession.from_dict({
            'id': 'mtg-repo1',
            'platform': 'zoom',
            'meetingUrl': ZOOM_URL,
            'agentSession': agent_session_payload(),
            'context': context_payload(),
            'permissions': permissions_payload(),
            'state': 'joining',
            'startedAt': TIMESTAMP,
        })
        self.store.put(session, 'lease-secret-value')
        path = self.store.root / 'mtg-repo1' / 'snapshot.json'
        path.write_bytes(b'  \n')
        with self.assertRaises(MeetingCorruptionError):
            self.store.get('mtg-repo1')
        self.assertEqual(path.read_bytes(), b'  \n')
        self.assertIsNone(self.store.get('mtg-missing'))


@unittest.skipUnless(HAS_AIOHTTP, 'aiohttp is required')
class ApprovalDaemonTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.clock = FakeClock()
        self.supervisor = FakeSupervisor()
        self.auth = 'test-daemon-token'
        self.app = create_app(
            root=self.temporary.name,
            auth_token=self.auth,
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
        return await self.client.post(
            '/v1/meetings', json=create_payload(**overrides), headers=self.headers())

    async def test_permission_escalation_is_rejected_at_create(self):
        portal = await self.create(
            agentSession=agent_session_payload(
                sessionId='local-portal',
                metadata={'source': 'local-portal', 'continuity': 'context'},
            ),
            permissions=permissions_payload(workspace='workspace-write'),
        )
        self.assertEqual(portal.status, 422)
        exact = await self.create(permissions=permissions_payload(commands='allowed'))
        self.assertEqual(exact.status, 422)

    async def test_approvals_are_atomic_idempotent_and_expire_after_restart(self):
        created = await self.create()
        meeting = await created.json()
        meeting_id = meeting['id']
        missing = await self.client.get(
            '/v1/meetings/' + meeting_id + '/approvals', headers={'Authorization': 'Bearer other'})
        self.assertEqual(missing.status, 401)
        created_approval = await self.client.post(
            '/v1/meetings/' + meeting_id + '/approvals',
            json={'category': 'commands', 'summary': 'Run a workspace lookup',
                  'scope': {'host': 'workspace'}, 'ttlSeconds': 1},
            headers=self.headers())
        self.assertEqual(created_approval.status, 201)
        approval = await created_approval.json()
        self.assertEqual(approval['status'], 'pending')
        self.assertNotIn('consumed', approval)
        self.assertNotIn('password', str(approval).lower())
        listed = await self.client.get(
            '/v1/meetings/' + meeting_id + '/approvals', headers=self.headers())
        self.assertEqual((await listed.json())['approvals'][0]['id'], approval['id'])
        first = await self.client.post(
            '/v1/meetings/' + meeting_id + '/approvals/' + approval['id'] + '/decision',
            json={'decision': 'approved'}, headers=self.headers())
        self.assertEqual(first.status, 200)
        replay = await self.client.post(
            '/v1/meetings/' + meeting_id + '/approvals/' + approval['id'] + '/decision',
            json={'decision': 'approved'}, headers=self.headers())
        self.assertEqual(replay.status, 200)
        stale = await self.client.post(
            '/v1/meetings/' + meeting_id + '/approvals/' + approval['id'] + '/decision',
            json={'decision': 'denied'}, headers=self.headers())
        self.assertEqual(stale.status, 409)
        second = await self.client.post(
            '/v1/meetings/' + meeting_id + '/approvals',
            json={'category': 'network', 'summary': 'Allow a web search', 'ttlSeconds': 1},
            headers=self.headers())
        pending = await second.json()
        self.clock.advance(2)
        app = create_app(
            root=self.temporary.name, auth_token=self.auth, supervisor=self.supervisor,
            clock=self.clock)
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            listed = await client.get(
                '/v1/meetings/' + meeting_id + '/approvals', headers=self.headers())
            statuses = {item['id']: item['status'] for item in (await listed.json())['approvals']}
            self.assertEqual(statuses[pending['id']], 'expired')
            expired_decision = await client.post(
                '/v1/meetings/' + meeting_id + '/approvals/' + pending['id'] + '/decision',
                json={'decision': 'approved'}, headers=self.headers())
            self.assertEqual(expired_decision.status, 409)
        finally:
            await client.close()

    async def test_cancel_meeting_cancels_pending_approvals_and_handoff_includes_policy(self):
        created = await self.create()
        meeting = await created.json()
        meeting_id = meeting['id']
        self.daemon.transition(meeting_id, 'live')
        pending = await self.client.post(
            '/v1/meetings/' + meeting_id + '/approvals',
            json={'category': 'commands', 'summary': 'Run a workspace lookup'},
            headers=self.headers())
        approval = await pending.json()
        stored = await self.client.get(
            '/v1/meetings/' + meeting_id, headers=self.headers())
        self.assertEqual((await stored.json())['visualState'], 'needs_attention')
        cancelled = await self.client.post(
            '/v1/meetings/' + meeting_id + '/cancel', headers=self.headers())
        self.assertEqual(cancelled.status, 200)
        got = await self.client.get(
            '/v1/meetings/' + meeting_id + '/approvals/' + approval['id'],
            headers=self.headers())
        body = await got.json()
        self.assertEqual(body['status'], 'cancelled')
        self.daemon.store_handoff(handoff_payload(
            meetingId=meeting_id, startedAt=meeting['startedAt']))
        handoff = await self.client.get(
            '/v1/meetings/' + meeting_id + '/handoff', headers=self.headers())
        payload = await handoff.json()
        self.assertEqual(payload['permissions']['workspace'], 'read-only')
        self.assertEqual(payload['approvals'][0]['id'], approval['id'])
        self.assertEqual(payload['approvals'][0]['status'], 'cancelled')

    async def test_wait_for_decision_is_single_use_and_does_not_block_other_meetings(self):
        first = await (await self.create()).json()
        second = await (await self.create(
            agentSession=agent_session_payload(sessionId='thread-origin-2'))).json()
        created = self.daemon.create_approval(first['id'], {
            'category': 'commands', 'summary': 'Run a workspace lookup',
        })
        waiter = asyncio.create_task(
            self.daemon.wait_for_decision(first['id'], created['id'], timeout=2))
        await asyncio.sleep(0.02)
        other = await self.client.get(
            '/v1/meetings/' + second['id'], headers=self.headers())
        self.assertEqual(other.status, 200)
        self.daemon.decide_approval(first['id'], created['id'], {'decision': 'approved'})
        granted = await waiter
        self.assertEqual(granted['status'], 'approved')
        with self.assertRaises(DaemonError):
            self.daemon.consume_approval(first['id'], created['id'])

    async def test_artifact_routes_require_auth_and_stay_inside_the_meeting(self):
        created = await self.create()
        meeting = await created.json()
        meeting_id = meeting['id']
        stored = self.daemon.artifacts.put(
            meeting_id, kind='plan', body={'summary': 'Update the helper'},
            description='Workspace action plan')
        missing = await self.client.get(
            '/v1/meetings/' + meeting_id + '/artifacts',
            headers={'Authorization': 'Bearer other'})
        self.assertEqual(missing.status, 401)
        listed = await self.client.get(
            '/v1/meetings/' + meeting_id + '/artifacts', headers=self.headers())
        self.assertEqual(listed.status, 200)
        self.assertEqual((await listed.json())['artifacts'][0]['id'], stored['id'])
        escaped = await self.client.get(
            '/v1/meetings/' + meeting_id + '/artifacts/../secret', headers=self.headers())
        self.assertIn(escaped.status, (404, 422))
        content = await self.client.get(
            '/v1/meetings/' + meeting_id + '/artifacts/' + stored['id'] + '/content',
            headers=self.headers())
        self.assertEqual(content.status, 200)
        self.assertEqual(content.headers.get('X-Content-Type-Options'), 'nosniff')
        body = await content.read()
        self.assertIn(b'Update the helper', body)
        self.assertNotIn(b'sk-', body)


def git_permissions():
    return permissions_payload(
        workspace='workspace-write',
        edits='approval-required',
        commits='approval-required',
        pushes='approval-required',
    )


class FakeGitBroker:
    def __init__(self):
        self.commits = []
        self.pushes = []
        self.fail = None
        self.commit_calls = 0
        self.push_calls = 0

    def commit(self, request, *, workspace, cancel=None):
        from workspace_isolation import IsolationError
        self.commit_calls += 1
        payload = request.to_dict() if hasattr(request, 'to_dict') else dict(request)
        self.commits.append(payload)
        if self.fail:
            raise IsolationError(self.fail)
        return {
            'commitSha': 'c' * 40,
            'parentSha': payload['expectedHead'],
            'treeSha': 'd' * 40,
            'files': [item['path'] for item in payload['files']],
        }

    def push(self, request, *, workspace, cancel=None):
        from workspace_isolation import IsolationError
        self.push_calls += 1
        payload = request.to_dict() if hasattr(request, 'to_dict') else dict(request)
        self.pushes.append(payload)
        if self.fail:
            raise IsolationError(self.fail)
        return {
            'commitSha': payload['commitSha'],
            'remote': payload['remote'],
            'branch': payload['branch'],
        }


@unittest.skipUnless(HAS_AIOHTTP, 'aiohttp is required')
class GitDaemonTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.clock = FakeClock()
        self.supervisor = FakeSupervisor()
        self.auth = 'test-daemon-token'
        self.app = create_app(
            root=self.temporary.name,
            auth_token=self.auth,
            supervisor=self.supervisor,
            clock=self.clock,
            sse_poll_interval=0.02,
            sse_heartbeat_interval=0.05,
            max_body_bytes=4096,
        )
        self.daemon = self.app.runtime_daemon
        self.broker = FakeGitBroker()
        self.daemon.git_broker = self.broker
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

    def commit_body(self, **overrides):
        payload = {
            'id': 'cmt-1',
            'expectedHead': 'a' * 40,
            'message': 'Record reviewed helper changes',
            'files': [{'path': 'helper.py', 'sha256': 'b' * 64}],
        }
        payload.update(overrides)
        return payload

    def push_body(self, **overrides):
        payload = {
            'id': 'psh-1',
            'commitSha': 'c' * 40,
            'remote': 'origin',
            'branch': 'colleague-work',
        }
        payload.update(overrides)
        return payload

    async def wait_status(self, meeting_id, kind, operation_id, wanted):
        for _ in range(50):
            path = '/v1/meetings/' + meeting_id + '/' + kind + '/' + operation_id
            response = await self.client.get(path, headers=self.headers())
            body = await response.json()
            if body.get('status') == wanted:
                return body
            await asyncio.sleep(0.05)
        self.fail('operation did not reach ' + wanted)

    async def test_commits_require_own_approval_and_are_idempotent(self):
        created = await self.create(permissions=git_permissions())
        meeting = await created.json()
        meeting_id = meeting['id']
        denied = await self.client.post(
            '/v1/meetings/' + meeting_id + '/commits',
            json=self.commit_body(argv=['commit', '-m', 'x']),
            headers=self.headers())
        self.assertEqual(denied.status, 422)
        disabled = await self.create(
            agentSession=agent_session_payload(sessionId='thread-origin-disabled'))
        self.assertEqual(disabled.status, 201)
        disabled_id = (await disabled.json())['id']
        blocked = await self.client.post(
            '/v1/meetings/' + disabled_id + '/commits',
            json=self.commit_body(),
            headers=self.headers())
        self.assertEqual(blocked.status, 422)
        self.assertEqual(self.broker.commit_calls, 0)
        edits = self.daemon.create_approval(meeting_id, {
            'category': 'edits', 'summary': 'Update the helper',
        })
        requested = await self.client.post(
            '/v1/meetings/' + meeting_id + '/commits',
            json=self.commit_body(),
            headers=self.headers())
        self.assertEqual(requested.status, 201)
        commit = await requested.json()
        self.assertEqual(commit['status'], 'requested')
        self.assertNotEqual(commit['approvalId'], edits['id'])
        self.daemon.decide_approval(meeting_id, edits['id'], {'decision': 'approved'})
        await asyncio.sleep(0.1)
        still = await self.client.get(
            '/v1/meetings/' + meeting_id + '/commits/cmt-1', headers=self.headers())
        self.assertEqual((await still.json())['status'], 'requested')
        self.assertEqual(self.broker.commit_calls, 0)
        self.daemon.decide_approval(meeting_id, commit['approvalId'], {'decision': 'approved'})
        finished = await self.wait_status(meeting_id, 'commits', 'cmt-1', 'completed')
        self.assertEqual(self.broker.commit_calls, 1)
        self.assertEqual(finished['result']['commitSha'], 'c' * 40)
        replay = await self.client.post(
            '/v1/meetings/' + meeting_id + '/commits',
            json=self.commit_body(),
            headers=self.headers())
        self.assertEqual(replay.status, 201)
        self.assertEqual((await replay.json())['status'], 'completed')
        self.assertEqual(self.broker.commit_calls, 1)
        types = [event.type for event in self.daemon.events.replay(meeting_id)]
        self.assertIn('git.action.requested', types)
        self.assertIn('git.action.completed', types)
        self.assertIn('artifact.created', types)
        self.assertIn('approval.required', types)
        listed = await self.client.get(
            '/v1/meetings/' + meeting_id + '/commits', headers=self.headers())
        self.assertEqual((await listed.json())['commits'][0]['id'], 'cmt-1')
        artifacts = await self.client.get(
            '/v1/meetings/' + meeting_id + '/artifacts', headers=self.headers())
        self.assertEqual((await artifacts.json())['artifacts'][0]['kind'], 'git-commit')

    async def test_push_uses_separate_approval_and_never_returns_urls(self):
        created = await self.create(permissions=git_permissions())
        meeting_id = (await created.json())['id']
        rejected = await self.client.post(
            '/v1/meetings/' + meeting_id + '/pushes',
            json=self.push_body(remote='https://example.com/repo.git'),
            headers=self.headers())
        self.assertEqual(rejected.status, 422)
        requested = await self.client.post(
            '/v1/meetings/' + meeting_id + '/pushes',
            json=self.push_body(),
            headers=self.headers())
        self.assertEqual(requested.status, 201)
        push = await requested.json()
        self.daemon.decide_approval(meeting_id, push['approvalId'], {'decision': 'approved'})
        finished = await self.wait_status(meeting_id, 'pushes', 'psh-1', 'completed')
        dumped = json.dumps(finished)
        self.assertNotIn('https://', dumped)
        self.assertNotIn('git@', dumped)
        self.assertEqual(finished['result']['remote'], 'origin')
        self.assertEqual(self.broker.push_calls, 1)
        replay = await self.client.post(
            '/v1/meetings/' + meeting_id + '/pushes',
            json=self.push_body(),
            headers=self.headers())
        self.assertEqual((await replay.json())['status'], 'completed')
        self.assertEqual(self.broker.push_calls, 1)

    async def test_conflict_and_auth_and_handoff(self):
        created = await self.create(permissions=git_permissions())
        meeting = await created.json()
        meeting_id = meeting['id']
        self.broker.fail = 'working tree HEAD does not match expectedHead'
        requested = await self.client.post(
            '/v1/meetings/' + meeting_id + '/commits',
            json=self.commit_body(id='cmt-2'),
            headers=self.headers())
        commit = await requested.json()
        self.daemon.decide_approval(meeting_id, commit['approvalId'], {'decision': 'approved'})
        failed = await self.wait_status(meeting_id, 'commits', 'cmt-2', 'conflict')
        self.assertTrue(failed['result']['conflict'])
        missing = await self.client.get(
            '/v1/meetings/' + meeting_id + '/commits',
            headers={'Authorization': 'Bearer other'})
        self.assertEqual(missing.status, 401)
        self.daemon.store_handoff(handoff_payload(
            meetingId=meeting_id, startedAt=meeting['startedAt']))
        handoff = await self.client.get(
            '/v1/meetings/' + meeting_id + '/handoff', headers=self.headers())
        payload = await handoff.json()
        self.assertTrue(any(item['taskId'] == 'cmt-2' for item in payload['workPerformed']))


@unittest.skipUnless(HAS_AIOHTTP, 'aiohttp is required')
class ScreenShareDaemonTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.clock = FakeClock()
        self.supervisor = FakeSupervisor()
        self.auth = 'test-daemon-token'
        from visual_analysis import StaticVisualAnalysisProvider
        from visual_hash import solid_png
        self.png = solid_png(32, 32, 12, 34, 56)
        self.app = create_app(
            root=self.temporary.name,
            auth_token=self.auth,
            supervisor=self.supervisor,
            clock=self.clock,
            jobs_dir=Path(self.temporary.name) / 'jobs',
            visual_analyzer=StaticVisualAnalysisProvider(
                {'summary': 'A shared slide', 'confidence': 0.81}),
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

    async def test_disabled_by_default_and_locked_after_start(self):
        created = await self.client.post(
            '/v1/meetings', json=create_payload(), headers=self.headers())
        meeting = await created.json()
        meeting_id = meeting['id']
        status = await self.client.get(
            '/v1/meetings/' + meeting_id + '/screen-share', headers=self.headers())
        payload = await status.json()
        self.assertFalse(payload['status']['enabled'])
        self.assertEqual(payload['status']['degradedReason'], 'disabled')
        paused = await self.client.post(
            '/v1/meetings/' + meeting_id + '/screen-share/pause',
            json={}, headers=self.headers())
        self.assertEqual(paused.status, 409)
        enabled = await self.client.post(
            '/v1/meetings', json=create_payload(
                screenShare={'enabled': True, 'captureIntervalMs': 3000},
                agentSession=agent_session_payload(sessionId='thread-share-1')),
            headers=self.headers())
        live = await enabled.json()
        live_id = live['id']
        live_status = await (await self.client.get(
            '/v1/meetings/' + live_id + '/screen-share', headers=self.headers())).json()
        self.assertTrue(live_status['status']['enabled'])
        self.assertEqual(live_status['settings']['captureIntervalMs'], 3000)
        resume = await self.client.post(
            '/v1/meetings/' + live_id + '/screen-share/pause',
            json={}, headers=self.headers())
        self.assertEqual(resume.status, 200)
        paused_status = await resume.json()
        self.assertTrue(paused_status['status']['paused'])
        unauth = await self.client.get(
            '/v1/meetings/' + live_id + '/screen-share')
        self.assertEqual(unauth.status, 401)

    async def test_ingest_observation_artifact_and_handoff(self):
        created = await self.client.post(
            '/v1/meetings', json=create_payload(
                screenShare={'enabled': True},
                agentSession=agent_session_payload(sessionId='thread-share-2')),
            headers=self.headers())
        meeting = await created.json()
        meeting_id = meeting['id']
        from screen_share_pipeline import ScreenShareBus
        bus = ScreenShareBus(self.daemon.jobs_dir, meeting_id)
        bus.write_inbox(self.png, {'id': 'frm-test1', 'capturedAt': TIMESTAMP})
        results = await self.daemon.ingest_screen_share_inbox(meeting_id)
        self.assertEqual(results[0]['observation']['summary'], 'A shared slide')
        listed = await (await self.client.get(
            '/v1/meetings/' + meeting_id + '/screen-share/observations',
            headers=self.headers())).json()
        self.assertEqual(listed['observations'][0]['summary'], 'A shared slide')
        artifacts = await (await self.client.get(
            '/v1/meetings/' + meeting_id + '/artifacts', headers=self.headers())).json()
        shot = next(item for item in artifacts['artifacts'] if item['kind'] == 'screenshot')
        content = await self.client.get(
            '/v1/meetings/' + meeting_id + '/artifacts/' + shot['id'] + '/content',
            headers=self.headers())
        self.assertEqual(content.status, 200)
        self.assertEqual(content.headers['X-Content-Type-Options'], 'nosniff')
        self.assertEqual(content.headers['Cache-Control'], 'no-store')
        self.assertIn('image/png', content.headers['Content-Type'])
        body = await content.read()
        self.assertEqual(body[:8], b'\x89PNG\r\n\x1a\n')
        missing = await self.client.get(
            '/v1/meetings/' + meeting_id + '/artifacts/' + shot['id'] + '/content',
            headers={'Authorization': 'Bearer other'})
        self.assertEqual(missing.status, 401)
        traversal = await self.client.get(
            '/v1/meetings/' + meeting_id + '/artifacts/../secret/content',
            headers=self.headers())
        self.assertIn(traversal.status, (404, 422))
        self.daemon.store_handoff(handoff_payload(
            meetingId=meeting_id, startedAt=meeting['startedAt']))
        handoff = await (await self.client.get(
            '/v1/meetings/' + meeting_id + '/handoff', headers=self.headers())).json()
        self.assertTrue(any(item['taskId'].startswith('obs-') for item in handoff['workPerformed']))
        dumped = json.dumps(handoff)
        self.assertNotIn('\\x89PNG', dumped)
        events = [event.type for event in self.daemon.events.replay(meeting_id)]
        self.assertIn('screen_share.started', events)
        self.assertIn('screen_share.observation', events)

    async def test_container_health_does_not_override_host_analyzer(self):
        created = await self.client.post(
            '/v1/meetings', json=create_payload(
                screenShare={'enabled': True},
                agentSession=agent_session_payload(sessionId='thread-share-4')),
            headers=self.headers())
        meeting_id = (await created.json())['id']
        path = '/v1/meetings/' + meeting_id + '/screen-share'
        before = await (await self.client.get(path, headers=self.headers())).json()
        self.assertTrue(before['status']['analyzerAvailable'])
        self.daemon.apply_screen_share_health(meeting_id, {
            'available': True, 'active': True, 'capturing': True, 'analyzerAvailable': False})
        after = await (await self.client.get(path, headers=self.headers())).json()
        self.assertTrue(after['status']['analyzerAvailable'])
        self.assertTrue(after['status']['capturing'])

    async def test_revisited_screen_reuses_observation(self):
        created = await self.client.post(
            '/v1/meetings', json=create_payload(
                screenShare={'enabled': True},
                agentSession=agent_session_payload(sessionId='thread-share-3')),
            headers=self.headers())
        meeting_id = (await created.json())['id']
        from screen_share_pipeline import ScreenShareBus
        from visual_hash import solid_png
        bus = ScreenShareBus(self.daemon.jobs_dir, meeting_id)
        other = solid_png(32, 32, 200, 40, 40)
        for index, png in enumerate((self.png, other, self.png)):
            bus.write_inbox(png, {
                'id': 'frm-reuse%d' % index, 'capturedAt': TIMESTAMP, 'maskedTiles': [1, 2]})
        results = await self.daemon.ingest_screen_share_inbox(meeting_id)
        self.assertEqual([result.get('reused') for result in results], [None, None, True])
        self.assertEqual(len(self.daemon.screen_share_host.analyzer.calls), 2)
        listed = await (await self.client.get(
            '/v1/meetings/' + meeting_id + '/screen-share/observations',
            headers=self.headers())).json()
        self.assertEqual(len(listed['observations']), 3)
        self.assertTrue(listed['observations'][-1]['reused'])
        self.assertEqual(listed['observations'][-1]['frameArtifactId'],
                         listed['observations'][0]['frameArtifactId'])
        shots = [event for event in self.daemon.events.replay(meeting_id)
                 if event.type == 'screen_share.frame_selected']
        self.assertEqual(len(shots), 2)
        observed = [event.payload['observation'] for event in self.daemon.events.replay(meeting_id)
                    if event.type == 'screen_share.observation']
        self.assertEqual([item.reused for item in observed], [False, False, True])
        outbox = bus.take_outbox()
        self.assertEqual(sorted(item.reused for item in outbox), [False, False, True])


if __name__ == '__main__':
    unittest.main()
