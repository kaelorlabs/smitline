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
    def test_validation_rejects_secrets_and_placeholders(self):
        with self.assertRaises(ValidationError):
            validate_join_request({'url': ZOOM, 'agentSession': {**agent_session(), 'token': 'nope'}})
        with self.assertRaises(ValidationError):
            validate_join_request({'url': ZOOM, 'agentSession': agent_session(sessionId='--last')})
        with self.assertRaises(ValidationError):
            validate_join_request({'url': ZOOM, 'agentSession': agent_session(workspace='relative')})
        with self.assertRaises(ValidationError):
            validate_join_request({'url': 'http://zoom.us/j/1', 'agentSession': agent_session()})
        disabled = validate_join_request({
            'url': ZOOM,
            'agentSession': agent_session(),
            'camera': {'enabled': False},
        })
        self.assertEqual(disabled['camera']['enabled'], False)
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
