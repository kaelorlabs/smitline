import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from aiohttp.test_utils import TestClient, TestServer

from api_tokens import ApiTokenStore
from call_brief import CallBrief
from call_hooks import CallRefused, DefaultCallHooks
from call_service import CallError, CallService
from call_store import CallStore
from meeting_line import MeetingLine, meeting_payload
from runtime_daemon import create_app, require_server_bind


ZOOM = 'https://us05web.zoom.us/j/123456789?pwd=abc'


def brief(**overrides):
    payload = {'channel': 'phone', 'to': '+14155550142', 'onBehalfOf': 'Robin',
               'objective': 'Book a table for 4 at 7pm'}
    payload.update(overrides)
    return payload


class FakeLine:
    channel = 'phone'

    def __init__(self):
        self.contexts = {}
        self.instructions = []
        self.proceed = asyncio.Event()
        self.script = [('ringing', None), ('in_progress', None)]
        self.transcript = [('agent', "Hi, I'm an AI assistant calling on behalf of Robin."),
                           ('other', 'Sure, 7pm is booked.')]
        self.end_reason = 'hangup'
        self.fail = None

    def ready(self, hooks, owner, brief):
        hooks.credentials(owner, 'openai')

    async def start(self, ctx):
        self.contexts[ctx.call_id] = ctx
        if self.fail:
            raise self.fail
        ctx.set_status('connecting')
        ctx.link(providerCallSid='CA123')
        for status, _ in self.script:
            ctx.set_status(status)
        for speaker, text in self.transcript:
            ctx.add_transcript(speaker, text)
        await self.proceed.wait()
        await ctx.finish(self.end_reason, usage={'voiceSeconds': 42, 'phoneSeconds': 45})

    async def instruct(self, call_id, text):
        self.instructions.append((call_id, text))
        return True

    async def end(self, call_id):
        self.proceed.set()
        return True


class FakeSummarizer:
    def __init__(self):
        self.calls = 0

    async def summarize(self, brief, transcript, *, duration_seconds=0):
        self.calls += 1
        return {'outcome': 'achieved', 'summary': 'Booked.', 'details': [], 'decisions': [],
                'actionItems': [], 'openQuestions': [], 'transcript': transcript,
                'durationSeconds': duration_seconds, 'source': 'summary_model',
                'summaryTokens': {'input': 10, 'output': 5}}


class RecordingNotifier:
    def __init__(self):
        self.calls = []

    async def deliver(self, call):
        self.calls.append(call)
        return {'delivered': True, 'attempts': 1, 'status': 200}


class ServiceHarness:
    def __init__(self, temp, environ=None, lines=None):
        self.store = CallStore(Path(temp) / 'calls')
        self.notifier = RecordingNotifier()
        self.hooks = DefaultCallHooks(environ=environ if environ is not None else
                                      {'OPENAI_API_KEY': 'sk-test'},
                                      store=self.store, notifier=self.notifier)
        self.line = FakeLine()
        self.summarizer = FakeSummarizer()
        self.service = CallService(self.store, hooks=self.hooks,
                                   lines=lines if lines is not None else {'phone': self.line},
                                   summarizer_factory=lambda owner: self.summarizer, environ={})


class CallServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.h = ServiceHarness(self.temp.name)

    async def asyncTearDown(self):
        await self.h.service.shutdown()
        self.temp.cleanup()

    async def test_full_lifecycle(self):
        record = await self.h.service.create(brief(notify={'webhookUrl': 'https://example.com/h'}))
        self.assertEqual(record['status'], 'queued')
        await asyncio.sleep(0)
        waiting = await self.h.service.wait(record['id'], timeout=0.05)
        self.assertEqual(waiting['status'], 'in_progress')
        self.assertIsNotNone(waiting['answeredAt'])
        self.assertEqual(waiting['line'], {'providerCallSid': 'CA123'})
        self.h.line.proceed.set()
        done = await self.h.service.wait(record['id'], timeout=5)
        self.assertEqual(done['status'], 'completed')
        self.assertEqual(done['endReason'], 'hangup')
        self.assertEqual(done['result']['outcome'], 'achieved')
        self.assertEqual(done['usage'], {'voiceSeconds': 42, 'phoneSeconds': 45,
                                         'summaryTokens': {'input': 10, 'output': 5}})
        types = [event['type'] for event in self.h.service.events(record['id'])]
        self.assertEqual(types[0], 'call.created')
        self.assertIn('call.transcript', types)
        self.assertIn('call.result', types)
        self.assertEqual(types[-1], 'call.webhook')
        self.assertEqual(self.h.notifier.calls[0]['status'], 'completed')
        usage = (Path(self.temp.name) / 'calls' / 'usage.jsonl').read_text()
        self.assertIn(record['id'], usage)

    async def test_no_conversation_skips_summary(self):
        self.h.line.transcript = []
        self.h.line.end_reason = 'no_answer'
        self.h.line.proceed.set()
        record = await self.h.service.create(brief())
        done = await self.h.service.wait(record['id'], timeout=5)
        self.assertEqual(done['result']['outcome'], 'not_reached')
        self.assertEqual(self.h.summarizer.calls, 0)

    async def test_line_failure_marks_failed(self):
        self.h.line.fail = RuntimeError('dial failed')
        record = await self.h.service.create(brief())
        done = await self.h.service.wait(record['id'], timeout=5)
        self.assertEqual(done['status'], 'failed')
        self.assertIn('dial failed', done['error'])

    async def test_instruct_and_end(self):
        record = await self.h.service.create(brief())
        await asyncio.sleep(0)
        self.assertEqual(await self.h.service.instruct(record['id'], 'Ask about parking.'),
                         {'delivered': True})
        await self.h.service.end(record['id'])
        done = await self.h.service.wait(record['id'], timeout=5)
        self.assertEqual(done['status'], 'completed')
        with self.assertRaises(CallError):
            await self.h.service.instruct(record['id'], 'too late')

    async def test_refusals(self):
        with self.assertRaises(CallError) as caught:
            await self.h.service.create(brief(channel='meeting', to=ZOOM))
        self.assertEqual(caught.exception.code, 'channel_unavailable')
        missing = ServiceHarness(self.temp.name + '-2', environ={})
        with self.assertRaises(CallError) as caught:
            await missing.service.create(brief())
        self.assertEqual((caught.exception.status, caught.exception.code), (503, 'not_configured'))
        self.assertEqual(caught.exception.details['missing'], ['OPENAI_API_KEY'])

        class Refusing(DefaultCallHooks):
            def precheck(self, owner, brief):
                raise CallRefused('insufficient_credit', 'Add credits to call.')
        self.h.service.hooks = Refusing(environ={'OPENAI_API_KEY': 'k'})
        with self.assertRaises(CallError) as caught:
            await self.h.service.create(brief())
        self.assertEqual((caught.exception.status, caught.exception.code), (403, 'insufficient_credit'))

    async def test_check_reports_problems(self):
        self.assertEqual(self.h.service.check(brief())['ok'], True)
        report = self.h.service.check(brief(channel='meeting', to=ZOOM))
        self.assertFalse(report['ok'])
        self.assertIn('not available', report['problems'][0])


class FakeSession(SimpleNamespace):
    pass


class FakeDaemon:
    def __init__(self):
        self.states = ['joining', 'waiting_for_admission', 'live', 'live', 'ended']
        self.created = []
        self.canceled = []
        self.handoff_tries = 0

    async def create_meeting(self, payload):
        self.created.append(payload)
        return FakeSession(id='mtg-1', platform='zoom', state='joining')

    async def get_meeting(self, meeting_id):
        state = self.states.pop(0) if len(self.states) > 1 else self.states[0]
        return FakeSession(id=meeting_id, state=state)

    async def get_handoff(self, meeting_id):
        self.handoff_tries += 1
        if self.handoff_tries < 2:
            raise RuntimeError('not ready')
        return {'summary': 'Agreed on the correction.', 'decisions': [{'text': 'Use $800,000'}],
                'actionItems': [], 'unresolvedQuestions': []}

    async def cancel_meeting(self, meeting_id):
        self.canceled.append(meeting_id)
        self.states = ['ended']


class MeetingLineTests(unittest.IsolatedAsyncioTestCase):
    async def test_meeting_call_follows_meeting_to_handoff(self):
        with tempfile.TemporaryDirectory() as temp:
            daemon = FakeDaemon()
            ticks = iter(range(0, 10000, 5))

            async def no_sleep(_seconds):
                await asyncio.sleep(0)
            line = MeetingLine(daemon, workspace='/tmp/ws', sleep=no_sleep,
                               monotonic=lambda: next(ticks))
            h = ServiceHarness(temp, lines={'meeting': line})
            record = await h.service.create(brief(channel='meeting', to=ZOOM,
                                                  context='Q3 sales review'))
            done = await h.service.wait(record['id'], timeout=5)
            self.assertEqual(done['status'], 'completed')
            self.assertEqual(done['line']['meetingId'], 'mtg-1')
            self.assertEqual(done['result']['source'], 'meeting_handoff')
            self.assertEqual(done['result']['decisions'], ['Use $800,000'])
            statuses = [e['data']['status'] for e in h.service.events(record['id'])
                        if e['type'] == 'call.status']
            self.assertEqual(statuses[:4], ['connecting', 'waiting', 'in_progress', 'summarizing'])
            payload = daemon.created[0]
            self.assertEqual(payload['agentSession']['provider'], 'generic')
            self.assertEqual(payload['agentSession']['metadata']['continuity'], 'context')
            self.assertEqual(payload['context']['summary'], 'Q3 sales review')
            self.assertEqual(h.summarizer.calls, 0)
            await h.service.shutdown()

    def test_payload_keeps_host_agent_session(self):
        session = {'provider': 'codex', 'sessionId': 'thread-1', 'workspace': '/w'}
        parsed = CallBrief.from_dict(brief(channel='meeting', to=ZOOM, agentSession=session))
        self.assertEqual(meeting_payload(parsed, 'call-x', '/ws')['agentSession'], session)


class ApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.h = ServiceHarness(self.temp.name)
        self.tokens = ApiTokenStore(Path(self.temp.name) / 'api-tokens.json')
        self.api_token, _ = self.tokens.create('ci')
        app = create_app(root=Path(self.temp.name) / 'daemon', auth_token='launch-token',
                         call_service=self.h.service, api_tokens=self.tokens)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()
        self.auth = {'Authorization': 'Bearer launch-token'}

    async def asyncTearDown(self):
        await self.client.close()
        self.temp.cleanup()

    async def test_create_get_wait_list(self):
        response = await self.client.post('/v1/calls', json=brief(), headers=self.auth)
        self.assertEqual(response.status, 201)
        call = await response.json()
        self.h.line.proceed.set()
        response = await self.client.get(f'/v1/calls/{call["id"]}/wait?timeout=5', headers=self.auth)
        done = await response.json()
        self.assertEqual(done['status'], 'completed')
        response = await self.client.get('/v1/calls', headers=self.auth)
        self.assertEqual((await response.json())['calls'][0]['id'], call['id'])
        response = await self.client.get(f'/v1/calls/{call["id"]}/events', headers=self.auth)
        text = await response.text()
        self.assertIn('event: call.created', text)
        self.assertIn('event: call.result', text)
        resumed = await self.client.get(f'/v1/calls/{call["id"]}/events',
                                        headers={**self.auth, 'Last-Event-ID': '2'})
        self.assertNotIn('event: call.created', await resumed.text())
        page = await self.client.get(f'/v1/calls/{call["id"]}/events?format=json&after=1',
                                     headers=self.auth)
        events = (await page.json())['events']
        self.assertEqual(events[0]['id'], '2')
        self.assertIn('call.transcript', [event['type'] for event in events])

    async def test_brief_problems_and_errors(self):
        response = await self.client.post('/v1/calls', json={'channel': 'phone'}, headers=self.auth)
        body = await response.json()
        self.assertEqual(response.status, 422)
        self.assertEqual(body['error']['code'], 'brief_incomplete')
        self.assertEqual([m['field'] for m in body['error']['missing']],
                         ['to', 'objective', 'onBehalfOf'])
        response = await self.client.post('/v1/calls', json=brief(to='12'), headers=self.auth)
        self.assertEqual((await response.json())['error']['code'], 'invalid_request')
        response = await self.client.get('/v1/calls/call-0123456789abcdef', headers=self.auth)
        self.assertEqual(response.status, 404)
        response = await self.client.post('/v1/calls/check', json=brief(), headers=self.auth)
        self.assertTrue((await response.json())['ok'])

    async def test_auth_voices_and_openapi(self):
        self.assertEqual((await self.client.get('/v1/calls')).status, 401)
        bad = {'Authorization': 'Bearer cai_wrong'}
        self.assertEqual((await self.client.get('/v1/calls', headers=bad)).status, 401)
        api = {'Authorization': f'Bearer {self.api_token}'}
        self.assertEqual((await self.client.get('/v1/calls', headers=api)).status, 200)
        voices = await (await self.client.get('/v1/voices', headers=api)).json()
        self.assertEqual(voices['default'], 'marin')
        spec = await (await self.client.get('/v1/openapi.json', headers=api)).json()
        self.assertIn('/v1/calls', spec['paths'])


class ServerModeTests(unittest.TestCase):
    def test_server_bind_requires_a_token(self):
        with tempfile.TemporaryDirectory() as temp:
            tokens = ApiTokenStore(Path(temp) / 'api-tokens.json')
            with self.assertRaises(ValueError):
                require_server_bind('0.0.0.0', tokens)
            with self.assertRaises(ValueError):
                create_app(root=Path(temp) / 'd', bind_host='0.0.0.0')
            token, entry = tokens.create('server')
            self.assertEqual(require_server_bind('0.0.0.0', tokens), '0.0.0.0')
            self.assertEqual(tokens.verify(token)['id'], entry['id'])
            self.assertNotIn(token, (Path(temp) / 'api-tokens.json').read_text())
            self.assertTrue(tokens.revoke(entry['id']))
            self.assertIsNone(tokens.verify(token))


if __name__ == '__main__':
    unittest.main()
