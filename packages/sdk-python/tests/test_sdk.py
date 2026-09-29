import asyncio
import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from colleague_ai import (
    Colleague,
    ColleagueError,
    FinalizationError,
    StartupError,
    ValidationError,
)
from colleague_ai.client import LoopbackTransport, parse_sse_block, validate_join_request


ZOOM = 'https://zoom.us/j/123456789'
WORKSPACE = '/tmp/colleague-workspace'


def agent_session(**overrides):
    payload = {
        'provider': 'codex',
        'sessionId': 'thread-abc',
        'workspace': WORKSPACE,
        'model': 'gpt-5.6-terra',
    }
    payload.update(overrides)
    return payload


class FakeTransport:
    def __init__(self):
        self.created = 0
        self.cancels = 0
        self.retries = 0
        self.meetings = {}
        self.handoffs = {}
        self.event_log = {}
        self.lock = threading.Lock()
        self.unrecoverable = False
        self.append_failed = None
        self.close_stream = False
        self.auto_handoff = True
        self.partial = False
        self.fail_create = None
        self.ready_delay = 0.02
        self.commits = {}
        self.pushes = {}
        self.screen_share = {}
        self._paired = False
        self._pending_pair = None

    def create_meeting(self, payload):
        if self.fail_create:
            error = self.fail_create
            if error.get('code') == 'supervisor_unavailable':
                raise StartupError(error['message'], code=error['code'], status=503)
            raise StartupError(error['message'], code=error.get('code', 'startup'), status=error.get('status', 503))
        with self.lock:
            self.created += 1
            meeting_id = f'mtg-{self.created}'
            session = {
                'id': meeting_id,
                'platform': 'zoom',
                'meetingUrl': payload['meetingUrl'],
                'agentSession': payload['agentSession'],
                'context': payload['context'],
                'permissions': payload['permissions'],
                'state': 'joining',
                'startedAt': '2026-09-16T00:00:00Z',
            }
            self.meetings[meeting_id] = session
            self.event_log[meeting_id] = [{
                'version': 1,
                'id': 'evt-join',
                'meetingId': meeting_id,
                'timestamp': session['startedAt'],
                'type': 'meeting.joining',
            }]
        if self.auto_handoff:
            threading.Timer(self.ready_delay, self._finish, args=(meeting_id,)).start()
        return session

    def _finish(self, meeting_id, partial=None):
        session = self.meetings[meeting_id]
        session['state'] = 'ended'
        handoff = {
            'version': 1,
            'meetingId': meeting_id,
            'startedAt': session['startedAt'],
            'endedAt': '2026-09-16T00:01:00Z',
            'summary': 'done',
            'decisions': [],
            'requirements': [],
            'actionItems': [],
            'unresolvedQuestions': [],
            'filesDiscussed': [],
            'workPerformed': [],
            'artifacts': [],
            'transcriptPath': f'recordings/{meeting_id}/transcript.jsonl',
            'recommendedNextAction': 'review',
            'handoffId': f'hnd-{meeting_id}',
            'archivePath': f'recordings/{meeting_id}',
            'partial': self.partial if partial is None else partial,
        }
        self.event_log[meeting_id].extend([
            {'version': 1, 'id': 'evt-live', 'meetingId': meeting_id, 'timestamp': session['startedAt'], 'type': 'meeting.live'},
            {
                'version': 1, 'id': 'evt-transcript', 'meetingId': meeting_id, 'timestamp': session['startedAt'],
                'type': 'transcript.final', 'text': 'secret meeting speech must not leak',
            },
            {'version': 1, 'id': 'evt-ready', 'meetingId': meeting_id, 'timestamp': handoff['endedAt'], 'type': 'handoff.ready', 'handoff': handoff},
        ])
        self.handoffs[meeting_id] = handoff

    def get_meeting(self, meeting_id):
        return self.meetings[meeting_id]

    def update_context(self, meeting_id, context):
        self.meetings[meeting_id]['context'] = context
        return self.meetings[meeting_id]

    def cancel_meeting(self, meeting_id):
        self.cancels += 1
        self._finish(meeting_id, partial=False)
        self.meetings[meeting_id]['state'] = 'ended'
        self.handoffs[meeting_id]['endReason'] = 'cancelled'
        return self.meetings[meeting_id]

    def get_handoff(self, meeting_id):
        if self.unrecoverable:
            raise FinalizationError('unrecoverable handoff failure', archive_path=f'recordings/{meeting_id}')
        handoff = self.handoffs.get(meeting_id)
        if not handoff:
            raise ColleagueError('handoff not ready', code='not_ready', status=404)
        return handoff

    def retry_handoff(self, meeting_id):
        self.retries += 1
        if meeting_id not in self.handoffs:
            self._finish(meeting_id)
        self.handoffs[meeting_id]['summary'] = 'retried'
        return self.handoffs[meeting_id]

    def list_approvals(self, meeting_id):
        return {'approvals': list(getattr(self, 'approvals', {}).get(meeting_id, []))}

    def get_approval(self, meeting_id, approval_id):
        for item in getattr(self, 'approvals', {}).get(meeting_id, []):
            if item['id'] == approval_id:
                return item
        raise ColleagueError('approval not found', code='not_found', status=404)

    def decide_approval(self, meeting_id, approval_id, decision):
        value = decision if isinstance(decision, str) else decision['decision']
        item = self.get_approval(meeting_id, approval_id)
        item['status'] = value
        item['decision'] = value
        return item

    def list_artifacts(self, meeting_id):
        return {'artifacts': list(getattr(self, 'artifacts', {}).get(meeting_id, []))}

    def get_artifact(self, meeting_id, artifact_id):
        for item in getattr(self, 'artifacts', {}).get(meeting_id, []):
            if item['id'] == artifact_id:
                return item
        raise ColleagueError('artifact not found', code='not_found', status=404)

    def get_artifact_content(self, meeting_id, artifact_id):
        item = self.get_artifact(meeting_id, artifact_id)
        return {
            'mediaType': item.get('mediaType') or 'application/json',
            'body': json.dumps(item).encode('utf-8'),
        }

    def create_commit(self, meeting_id, payload):
        items = self.commits.setdefault(meeting_id, [])
        created = dict(payload)
        created.setdefault('id', 'cmt-1')
        created['meetingId'] = meeting_id
        created['kind'] = 'commit'
        created['status'] = 'requested'
        items.append(created)
        return created

    def list_commits(self, meeting_id):
        return {'commits': list(self.commits.get(meeting_id, []))}

    def get_commit(self, meeting_id, operation_id):
        for item in self.commits.get(meeting_id, []):
            if item['id'] == operation_id:
                return item
        raise ColleagueError('commit not found', code='not_found', status=404)

    def create_push(self, meeting_id, payload):
        items = self.pushes.setdefault(meeting_id, [])
        created = dict(payload)
        created.setdefault('id', 'psh-1')
        created['meetingId'] = meeting_id
        created['kind'] = 'push'
        created['status'] = 'requested'
        items.append(created)
        return created

    def list_pushes(self, meeting_id):
        return {'pushes': list(self.pushes.get(meeting_id, []))}

    def get_push(self, meeting_id, operation_id):
        for item in self.pushes.get(meeting_id, []):
            if item['id'] == operation_id:
                return item
        raise ColleagueError('push not found', code='not_found', status=404)

    def get_screen_share(self, meeting_id):
        return self.screen_share.setdefault(meeting_id, {
            'status': {'enabled': False, 'paused': False, 'degradedReason': 'disabled'},
            'observations': [],
        })

    def pause_screen_share(self, meeting_id):
        payload = self.get_screen_share(meeting_id)
        payload['status'] = dict(payload.get('status') or {}, paused=True, enabled=True)
        return payload

    def resume_screen_share(self, meeting_id):
        payload = self.get_screen_share(meeting_id)
        payload['status'] = dict(payload.get('status') or {}, paused=False, enabled=True)
        return payload

    def list_screen_share_observations(self, meeting_id):
        payload = self.get_screen_share(meeting_id)
        return {'observations': list(payload.get('observations') or [])}

    def list_providers(self):
        return {'providers': [
            {'id': 'codex', 'installed': True, 'usable': True, 'exactSessionResume': True,
             'contextContinuity': True, 'supportedModels': []},
            {'id': 'cursor', 'installed': False, 'usable': False, 'reasonUnavailable': 'missing_binary',
             'supportedModels': []},
            {'id': 'claude-code', 'installed': False, 'usable': False, 'reasonUnavailable': 'missing_binary',
             'supportedModels': []},
        ]}

    def runner_status(self):
        return {
            'paired': bool(self._paired),
            'mode': 'loopback',
            'protocolVersion': 1,
            'controlPlane': 'mock-remote' if self._paired else 'local',
        }

    def pair_runner(self, payload=None):
        self._pending_pair = {'pairingId': 'pair-1', 'pairingCode': 'ABCD2345'}
        return {
            'pairingId': 'pair-1',
            'pairingCode': 'ABCD2345',
            'expiresAt': '2026-09-17T12:02:00Z',
        }

    def complete_runner_pair(self, payload):
        pending = getattr(self, '_pending_pair', None)
        if pending is None or pending.get('used'):
            raise ColleagueError('pairing code was already used', code='pairing_replay', status=409)
        if payload.get('pairingId') != pending['pairingId'] or payload.get('pairingCode') != pending['pairingCode']:
            raise ColleagueError('pairing code is invalid', code='pairing_mismatch', status=401)
        pending['used'] = True
        self._paired = True
        return {
            'deviceId': 'dev-1',
            'deviceEnrollment': 'enroll-once',
            'tenantId': 'ten-local',
            'userId': 'usr-local',
        }

    def unpair_runner(self):
        self._paired = False
        self._pending_pair = None
        return {'paired': False, 'mode': 'loopback', 'controlPlane': 'local'}

    def events(self, meeting_id, *, last_event_id='', seen=None, stop=None):
        delivered = seen if seen is not None else set()
        cursor = last_event_id
        while True:
            for event in list(self.event_log.get(meeting_id, ())):
                event_id = event.get('id')
                if event_id:
                    if event_id in delivered:
                        continue
                    delivered.add(event_id)
                    cursor = event_id
                yield event
            if self.close_stream:
                return
            if stop is not None and stop.is_set():
                return
            time.sleep(0.02)


class SdkTests(unittest.IsolatedAsyncioTestCase):
    async def test_context_git_schema_matches_runtime(self):
        context = {
            'version': 1,
            'objective': 'support meeting',
            'currentTask': 'review implementation',
            'summary': '',
            'decisions': [],
            'constraints': [],
            'openQuestions': [],
            'importantFiles': [],
            'recentConversation': [],
            'git': {'branch': 'developer-platform', 'commit': 'abc123', 'dirty': False},
        }
        validated = validate_join_request({
            'url': ZOOM,
            'agentSession': agent_session(),
            'context': context,
        })
        self.assertEqual(validated['context']['git'], context['git'])
        context['git'] = {'statusShort': ['?? private-file']}
        with self.assertRaisesRegex(ValidationError, r'context\.git\.statusShort'):
            validate_join_request({
                'url': ZOOM,
                'agentSession': agent_session(),
                'context': context,
            })

    def test_validation_rejects_secrets_and_placeholders(self):
        with self.assertRaises(ValidationError):
            validate_join_request({'url': ZOOM, 'agentSession': {**agent_session(), 'token': 'nope'}})
        with self.assertRaises(ValidationError):
            validate_join_request({'url': ZOOM, 'agentSession': agent_session(sessionId='--last')})
        with self.assertRaises(ValidationError):
            validate_join_request({'url': ZOOM, 'agentSession': agent_session(workspace='relative')})
        with self.assertRaises(ValidationError):
            validate_join_request({'url': 'http://zoom.us/j/1', 'agentSession': agent_session()})
        with self.assertRaises(ValidationError):
            validate_join_request({'url': 'https://meet.google.com/abc', 'agentSession': agent_session()})
        meet = validate_join_request({
            'url': 'https://meet.google.com/aaa-bbbb-ccc',
            'agentSession': agent_session(),
        })
        self.assertEqual(meet['meetingUrl'], 'https://meet.google.com/aaa-bbbb-ccc')
        disabled = validate_join_request({
            'url': ZOOM,
            'agentSession': agent_session(),
            'camera': {'enabled': False},
        })
        self.assertEqual(disabled['camera']['enabled'], False)
        enabled_share = validate_join_request({
            'url': ZOOM,
            'agentSession': agent_session(),
            'screenShare': {'enabled': True, 'captureIntervalMs': 5000, 'minChange': 0.05,
                            'settleTicks': 0},
        })
        self.assertTrue(enabled_share['screenShare']['enabled'])
        self.assertEqual(enabled_share['screenShare']['settleTicks'], 0)
        with self.assertRaises(ValidationError):
            validate_join_request({
                'url': ZOOM,
                'agentSession': agent_session(),
                'screenShare': {'enabled': True, 'argv': ['ffmpeg']},
            })
        for ticks in (6, -1, True, 1.5):
            with self.assertRaises(ValidationError):
                validate_join_request({
                    'url': ZOOM,
                    'agentSession': agent_session(),
                    'screenShare': {'enabled': True, 'settleTicks': ticks},
                })
        with self.assertRaises(ValidationError):
            validate_join_request({
                'url': ZOOM,
                'agentSession': agent_session(),
                'permissions': {
                    'workspace': 'none', 'commands': 'allowed', 'edits': 'disabled',
                    'network': 'disabled', 'commits': 'disabled', 'pushes': 'disabled',
                },
            })

    async def test_join_preserves_session_and_dedupes_consumers(self):
        transport = FakeTransport()
        colleague = Colleague(transport=transport)
        request = {
            'url': ZOOM,
            'agentSession': agent_session(sessionId='thread-exact', metadata={'source': 'codex'}),
            'permissions': {
                'workspace': 'read-only',
                'commands': 'approval-required',
                'edits': 'disabled',
                'network': 'approval-required',
                'commits': 'disabled',
                'pushes': 'disabled',
            },
        }
        first, second = await asyncio.gather(
            colleague.join_meeting(request),
            colleague.join_meeting(request),
        )
        third = await colleague.join_meeting(request)
        self.assertIs(first, second)
        self.assertIs(first, third)
        self.assertEqual(transport.created, 1)
        session = await first.status()
        self.assertEqual(session['agentSession']['sessionId'], 'thread-exact')
        self.assertEqual(session['agentSession']['metadata'], {'source': 'codex'})
        self.assertEqual(session['agentSession']['model'], 'gpt-5.6-terra')
        seen = []
        streamed = []

        def collect(event):
            seen.append(event['type'])

        first.on('event', collect)
        with first._lock:
            for event in list(first._emitted):
                collect(event)

        async def consume():
            async for event in first.events():
                streamed.append(event['type'])
                if event['type'] == 'handoff.ready':
                    break

        consumer = asyncio.create_task(consume())
        handoff = await first.finished()
        await asyncio.wait_for(consumer, timeout=2)
        self.assertEqual(handoff['handoffId'], f'hnd-{first.id}')
        self.assertIn('handoff.ready', seen + streamed)

    async def test_startup_error_is_prompt(self):
        transport = FakeTransport()
        transport.fail_create = {'code': 'supervisor_unavailable', 'message': 'supervisor unavailable'}
        colleague = Colleague(transport=transport)
        with self.assertRaises(StartupError):
            await colleague.join_meeting({'url': ZOOM, 'agentSession': agent_session()})

    async def test_partial_and_retry_and_cancel(self):
        transport = FakeTransport()
        transport.partial = True
        colleague = Colleague(transport=transport)
        meeting = await colleague.join_meeting({'url': ZOOM, 'agentSession': agent_session()})
        handoff = await meeting.finished()
        self.assertTrue(handoff['partial'])

        transport = FakeTransport()
        transport.auto_handoff = False
        colleague = Colleague(transport=transport)
        meeting = await colleague.join_meeting({'url': ZOOM, 'agentSession': agent_session()})
        first = await meeting.cancel()
        second = await meeting.cancel()
        self.assertEqual(first['id'], second['id'])
        self.assertEqual(transport.cancels, 1)
        cancelled = await meeting.finished()
        self.assertEqual(cancelled['endReason'], 'cancelled')

        retried = await meeting.retry_finalization()
        self.assertEqual(retried['summary'], 'retried')

    async def test_unrecoverable_finalization_includes_archive_path(self):
        transport = FakeTransport()
        transport.auto_handoff = False
        colleague = Colleague(transport=transport)
        meeting = await colleague.join_meeting({'url': ZOOM, 'agentSession': agent_session()})
        meeting._append_failed = {'retryable': False}
        transport.unrecoverable = True
        with self.assertRaises(FinalizationError) as raised:
            await meeting.finished()
        self.assertEqual(raised.exception.archive_path, f'recordings/{meeting.id}')

    async def test_blocking_finished_ignores_stream_close(self):
        transport = FakeTransport()
        transport.auto_handoff = False
        transport.close_stream = True
        colleague = Colleague(transport=transport)
        meeting = await colleague.join_meeting({'url': ZOOM, 'agentSession': agent_session()})
        pending = asyncio.create_task(meeting.finished())
        await asyncio.sleep(0.05)
        self.assertFalse(pending.done())
        transport._finish(meeting.id)
        handoff = await pending
        self.assertEqual(handoff['meetingId'], meeting.id)

    async def test_add_context(self):
        transport = FakeTransport()
        transport.auto_handoff = False
        colleague = Colleague(transport=transport)
        meeting = await colleague.join_meeting({'url': ZOOM, 'agentSession': agent_session()})
        updated = await meeting.add_context({
            'version': 1,
            'objective': 'ship sdk',
            'currentTask': 'tests',
            'summary': '',
            'decisions': [],
            'constraints': [],
            'openQuestions': [],
            'importantFiles': [],
            'recentConversation': [],
        })
        self.assertEqual(updated['context']['objective'], 'ship sdk')
        await meeting.cancel()

    async def test_approval_handle_requires_ids(self):
        transport = FakeTransport()
        transport.auto_handoff = False
        transport.approvals = {
            'mtg-1': [{
                'id': 'appr-1', 'meetingId': 'mtg-1', 'category': 'commands',
                'summary': 'Run a workspace lookup', 'status': 'pending',
            }],
        }
        colleague = Colleague(transport=transport)
        meeting = await colleague.join_meeting({'url': ZOOM, 'agentSession': agent_session()})
        listed = await meeting.list_approvals()
        self.assertEqual(listed['approvals'][0]['id'], 'appr-1')
        denied = await meeting.decide_approval('appr-1', 'denied')
        self.assertEqual(denied['status'], 'denied')
        with self.assertRaises(ValidationError):
            await meeting.decide_approval('', 'approved')
        transport.artifacts = {
            'mtg-1': [{'id': 'art-1', 'kind': 'plan', 'description': 'Workspace action plan'}],
        }
        listed_artifacts = await meeting.list_artifacts()
        self.assertEqual(listed_artifacts['artifacts'][0]['id'], 'art-1')
        with self.assertRaises(ValidationError):
            await meeting.get_artifact('')
        commit = await meeting.create_commit({
            'expectedHead': 'a' * 40,
            'message': 'Record reviewed helper changes',
            'files': [{'path': 'helper.py', 'sha256': 'b' * 64}],
        })
        self.assertEqual(commit['kind'], 'commit')
        listed_commits = await meeting.list_commits()
        self.assertEqual(listed_commits['commits'][0]['id'], commit['id'])
        with self.assertRaises(ValidationError):
            await meeting.get_commit('')
        push = await meeting.create_push({
            'commitSha': 'a' * 40,
            'remote': 'origin',
            'branch': 'colleague-work',
        })
        self.assertEqual(push['kind'], 'push')
        share = await meeting.get_screen_share()
        self.assertFalse(share['status']['enabled'])
        paused = await meeting.pause_screen_share()
        self.assertTrue(paused['status']['paused'])
        observations = await meeting.list_screen_share_observations()
        self.assertEqual(observations['observations'], [])
        providers = await colleague.list_providers()
        self.assertEqual(providers['providers'][0]['id'], 'codex')
        idle = await colleague.runner_status()
        self.assertFalse(idle['paired'])
        started = await colleague.pair_runner()
        completed = await colleague.complete_runner_pair({
            'pairingId': started['pairingId'],
            'pairingCode': started['pairingCode'],
        })
        self.assertEqual(completed['deviceEnrollment'], 'enroll-once')
        with self.assertRaises(ColleagueError):
            await colleague.complete_runner_pair({
                'pairingId': started['pairingId'],
                'pairingCode': started['pairingCode'],
            })
        paired = await colleague.runner_status()
        self.assertTrue(paired['paired'])
        self.assertNotIn('pairingCode', paired)
        self.assertNotIn('deviceEnrollment', paired)
        unpaired = await colleague.unpair_runner()
        self.assertFalse(unpaired['paired'])
        await meeting.cancel()


class LoopbackHttpTests(unittest.TestCase):
    def test_token_rotation_and_sse_dedup(self):
        seen_tokens = []
        auth_values = ['stale-token', 'rotated-token']

        def request(method, path, body=None, token=None, last_event_id='', stream=False):
            seen_tokens.append(token)
            if token != 'rotated-token':
                raise ColleagueError('unauthorized', code='unauthorized', status=401)
            if method == 'POST' and path == '/v1/meetings':
                return {
                    'id': 'mtg-1',
                    'meetingUrl': body['meetingUrl'],
                    'agentSession': body['agentSession'],
                    'state': 'joining',
                    'startedAt': '2026-09-16T00:00:00Z',
                }
            raise AssertionError(path)

        def popping_auth():
            if auth_values:
                return auth_values.pop(0)
            return 'rotated-token'

        transport = LoopbackTransport(
            request=request,
            read_auth=popping_auth,
            is_port_open=lambda: True,
            autostart=False,
        )
        session = transport.create_meeting({
            'meetingUrl': ZOOM,
            'agentSession': agent_session(),
            'context': {'version': 1, 'objective': 'o', 'currentTask': 't', 'summary': '',
                        'decisions': [], 'constraints': [], 'openQuestions': [],
                        'importantFiles': [], 'recentConversation': []},
            'permissions': {
                'workspace': 'read-only', 'commands': 'approval-required', 'edits': 'disabled',
                'network': 'approval-required', 'commits': 'disabled', 'pushes': 'disabled',
            },
        })
        self.assertEqual(session['id'], 'mtg-1')
        self.assertEqual(seen_tokens, ['stale-token', 'rotated-token'])
        self.assertEqual(session['agentSession']['sessionId'], 'thread-abc')
        self.assertNotIn('rotated-token', json.dumps(session))

        first = parse_sse_block('id: evt-1\nevent: meeting.live\ndata: {"type":"meeting.live"}\n')
        self.assertEqual(first['id'], 'evt-1')
        from colleague_ai.client import iterate_sse_text
        events = list(iterate_sse_text(
            'id: evt-1\ndata: {"id":"evt-1"}\n\nid: evt-1\ndata: {"id":"evt-1"}\n\nid: evt-2\ndata: {"id":"evt-2"}\n\n',
            seen=set(),
        ))
        self.assertEqual([event[0]['id'] for event in events], ['evt-1', 'evt-2'])

    def test_daemon_autostart_uses_lock_and_token_file(self):
        spawns = []
        root = Path(tempfile.mkdtemp())
        (root / '.colleague').mkdir()
        (root / '.colleague' / 'daemon.auth').write_text('host-token\n')
        opened = {'value': False}

        def spawn():
            spawns.append(1)
            opened['value'] = True

        transport = LoopbackTransport(
            root=root,
            port=59999,
            spawn_daemon=spawn,
            is_port_open=lambda: opened['value'],
            startup_timeout_s=1,
        )
        transport._ensure_daemon()
        self.assertEqual(spawns, [1])
        self.assertEqual(transport._token, 'host-token')
        self.assertNotIn('host-token', json.dumps({'meetingId': 'mtg-1'}))


class CallTests(unittest.IsolatedAsyncioTestCase):
    async def test_call_methods_use_the_calls_api(self):
        seen = []

        def request(method, path, body=None, token=None, last_event_id=''):
            seen.append((method, path, body))
            if path == '/v1/calls':
                return {'id': 'call-0123456789abcdef', 'status': 'queued'}
            if path.startswith('/v1/calls?'):
                return {'calls': [{'id': 'call-0123456789abcdef'}]}
            return {'id': 'call-0123456789abcdef', 'status': 'completed'}

        transport = LoopbackTransport(request=request, read_auth=lambda: 't',
                                      is_port_open=lambda: True, autostart=False)
        client = Colleague(transport=transport)
        brief = {'channel': 'phone', 'to': '+14155550142', 'onBehalfOf': 'Robin',
                 'objective': 'Book a table'}
        self.assertEqual((await client.start_call(brief))['status'], 'queued')
        self.assertEqual((await client.wait_for_call('call-0123456789abcdef', 999))['status'],
                         'completed')
        self.assertEqual(len(await client.list_calls(500)), 1)
        await client.instruct_call('call-0123456789abcdef', 'Ask about parking')
        self.assertEqual(seen[0], ('POST', '/v1/calls', brief))
        self.assertEqual(seen[1][1], '/v1/calls/call-0123456789abcdef/wait?timeout=300')
        self.assertEqual(seen[2][1], '/v1/calls?limit=100')
        self.assertEqual(seen[3], ('POST', '/v1/calls/call-0123456789abcdef/instructions',
                                   {'text': 'Ask about parking'}))
        await client.instruct_call('call-0123456789abcdef', 'He tried it yesterday', silent=True)
        await client.get_profile()
        await client.update_profile({'about': 'Robin builds Colleague AI.'})
        self.assertEqual(seen[4][2], {'text': 'He tried it yesterday', 'silent': True})
        self.assertEqual(seen[5][:2], ('GET', '/v1/profile'))
        self.assertEqual(seen[6], ('PATCH', '/v1/profile', {'about': 'Robin builds Colleague AI.'}))

    def test_error_details_are_kept(self):
        from colleague_ai.client import _map_http_error
        error = _map_http_error(422, {'error': {
            'code': 'brief_incomplete', 'message': 'brief is missing objective',
            'missing': [{'field': 'objective', 'question': 'What should the call achieve?'}]}}, 'x')
        self.assertIsInstance(error, ValidationError)
        self.assertEqual(error.details['missing'][0]['field'], 'objective')


class ExampleTests(unittest.TestCase):
    def test_example_imports(self):
        example = ROOT / 'examples' / 'join.py'
        self.assertTrue(example.is_file())
        source = example.read_text(encoding='utf-8')
        self.assertIn('join_meeting', source)
        self.assertNotRegex(source, r'sk-[A-Za-z0-9]')
        self.assertNotIn('OPENAI_API_KEY', source)


if __name__ == '__main__':
    unittest.main()
