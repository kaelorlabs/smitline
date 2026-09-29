import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path

from call_brief import BriefIncomplete, CallBrief, available_voices, disclosure_line
from call_hooks import CallRefused, DefaultCallHooks, MissingCredentials, load_hooks, read_env_file
from call_notify import WebhookNotifier, load_or_create_secret, signature
from call_result import (
    RESULT_SCHEMA, ResponsesSummarizer, SummaryUnavailable, fallback_result, result_from_handoff,
    result_without_conversation, transcript_text,
)
from call_store import CallNotFound, CallStore, new_call_id
from voice_core import (
    PCMU8, LiveSession, LiveSessionError, append_event, backend_usage_from, function_call_from,
    session_config,
)


ZOOM = 'https://us05web.zoom.us/j/123456789?pwd=abc'


def phone_brief(**overrides):
    payload = {
        'channel': 'phone',
        'to': '+1 (415) 555-0142',
        'onBehalfOf': 'Robin',
        'objective': 'Book a table for 4 at 7pm',
    }
    payload.update(overrides)
    return payload


class BriefTests(unittest.TestCase):
    def test_phone_brief_normalizes_number_and_defaults(self):
        brief = CallBrief.from_dict(phone_brief(mayAgreeTo=['6:30 to 7:30pm']))
        self.assertEqual(brief.to, '+14155550142')
        self.assertEqual(brief.max_minutes, 10)
        self.assertEqual(brief.to_dict()['mayAgreeTo'], ['6:30 to 7:30pm'])
        self.assertEqual(disclosure_line(brief),
                         "Hi, I'm an AI assistant calling on behalf of Robin.")

    def test_missing_fields_come_with_questions(self):
        with self.assertRaises(BriefIncomplete) as caught:
            CallBrief.from_dict({'channel': 'phone', 'to': '+14155550142'})
        missing = caught.exception.to_dict()['missing']
        self.assertEqual([item['field'] for item in missing], ['objective', 'onBehalfOf'])
        self.assertTrue(all(item['question'].endswith('?') or item['question'].endswith('.')
                            for item in missing))

    def test_rejects_bad_values(self):
        cases = [
            phone_brief(to='555-0142'),
            phone_brief(channel='fax'),
            phone_brief(voice='alloy'),
            phone_brief(maxMinutes=0),
            phone_brief(maxMinutes=61),
            phone_brief(notify={'webhookUrl': 'http://example.com/hook'}),
            phone_brief(notify={'webhookUrl': 'https://user:pw@example.com/hook'}),
            phone_brief(agentSession={'provider': 'codex'}),
            phone_brief(unexpected=True),
            phone_brief(language='english please'),
            {'channel': 'meeting', 'to': 'https://example.com/meeting', 'onBehalfOf': 'A',
             'objective': 'x'},
            {'channel': 'meeting', 'to': ZOOM, 'onBehalfOf': 'A', 'objective': 'x',
             'rehearsal': True},
        ]
        for payload in cases:
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                CallBrief.from_dict(payload)

    def test_accepts_localhost_webhooks_and_meetings(self):
        brief = CallBrief.from_dict(phone_brief(notify={'webhookUrl': 'http://127.0.0.1:9000/x'},
                                                voice='quartz'))
        self.assertEqual(brief.webhook_url, 'http://127.0.0.1:9000/x')
        meeting = CallBrief.from_dict({'channel': 'meeting', 'to': ZOOM, 'onBehalfOf': 'Robin',
                                       'objective': 'Help with the Q3 numbers'})
        self.assertEqual(meeting.platform, 'zoom')
        self.assertEqual(meeting.max_minutes, 120)

    def test_voice_list_is_extendable(self):
        self.assertIn('marin', available_voices({}))
        self.assertIn('newvoice', available_voices({'COLLEAGUE_EXTRA_VOICES': 'newvoice, Bad Name'}))
        self.assertNotIn('Bad Name', available_voices({'COLLEAGUE_EXTRA_VOICES': 'Bad Name'}))

    def test_rejects_secret_fields(self):
        with self.assertRaises(ValueError):
            CallBrief.from_dict(phone_brief(context='x', notify={'apiKey': 'x'}))


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = CallStore(Path(self.temp.name) / 'calls')

    def tearDown(self):
        self.temp.cleanup()

    def record(self, **overrides):
        record = {'id': new_call_id(), 'owner': 'local', 'channel': 'phone', 'status': 'queued',
                  'brief': phone_brief(), 'createdAt': self.store.now()}
        record.update(overrides)
        return record

    def test_create_update_list_and_events(self):
        first = self.store.create(self.record(createdAt='2026-09-28T10:00:00Z'))
        second = self.store.create(self.record(createdAt='2026-09-28T11:00:00Z', owner='other'))
        self.store.update(first['id'], status='ringing')
        self.assertEqual(self.store.get(first['id'])['status'], 'ringing')
        self.assertEqual([r['id'] for r in self.store.list()], [second['id'], first['id']])
        self.assertEqual([r['id'] for r in self.store.list(owner='local')], [first['id']])
        one = self.store.append_event(first['id'], 'call.status', status='ringing')
        two = self.store.append_event(first['id'], 'call.transcript', speaker='agent', text='Hi')
        self.assertEqual((one['id'], two['id']), ('1', '2'))
        self.assertEqual([e['id'] for e in self.store.events(first['id'], after='1')], ['2'])
        mode = os.stat(Path(self.temp.name) / 'calls' / first['id'] / 'call.json').st_mode & 0o777
        self.assertEqual(mode, 0o600)

    def test_sequence_survives_a_new_store(self):
        record = self.store.create(self.record())
        self.store.append_event(record['id'], 'call.created')
        reopened = CallStore(Path(self.temp.name) / 'calls')
        self.assertEqual(reopened.append_event(record['id'], 'call.status')['id'], '2')

    def test_unknown_and_malformed_ids(self):
        for value in ('call-0000000000000000', '../etc', 'call-XYZ'):
            with self.assertRaises(CallNotFound):
                self.store.get(value)

    def test_refuses_secret_fields(self):
        with self.assertRaises(ValueError):
            self.store.create(self.record(authToken='x'))


class NotifierTests(unittest.IsolatedAsyncioTestCase):
    def call(self, url='https://example.com/hook'):
        return {'id': 'call-1', 'status': 'completed', 'brief': {'notify': {'webhookUrl': url}}}

    async def test_signs_and_delivers(self):
        sent = []

        async def post(url, body, headers):
            sent.append((url, body, headers))
            return 204
        result = await WebhookNotifier('s3cret', post=post).deliver(self.call())
        self.assertEqual(result, {'delivered': True, 'attempts': 1, 'status': 204})
        url, body, headers = sent[0]
        self.assertEqual(headers['X-Colleague-Signature'], signature('s3cret', body))
        self.assertEqual(json.loads(body)['type'], 'call.completed')

    async def test_retries_server_errors_but_not_client_errors(self):
        statuses = [500, 502, 200]
        slept = []

        async def post(url, body, headers):
            return statuses.pop(0)

        async def sleep(seconds):
            slept.append(seconds)
        notifier = WebhookNotifier('s', post=post, sleep=sleep, delays=(1, 2, 3))
        self.assertEqual((await notifier.deliver(self.call()))['attempts'], 3)
        self.assertEqual(slept, [1, 2])

        async def gone(url, body, headers):
            return 410
        self.assertEqual(await WebhookNotifier('s', post=gone).deliver(self.call()),
                         {'delivered': False, 'attempts': 1, 'status': 410})

    async def test_no_webhook_means_no_delivery(self):
        self.assertIsNone(await WebhookNotifier('s').deliver({'status': 'completed', 'brief': {}}))

    def test_secret_is_created_once_privately(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'webhook.secret'
            first = load_or_create_secret(path)
            self.assertEqual(first, load_or_create_secret(path))
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)


class HookTests(unittest.TestCase):
    def test_env_file_and_process_environment(self):
        with tempfile.TemporaryDirectory() as temp:
            env_file = Path(temp) / '.env'
            env_file.write_text('# comment\nOPENAI_API_KEY="from-file"\nexport TWILIO_ACCOUNT_SID=AC1\n')
            self.assertEqual(read_env_file(env_file)['TWILIO_ACCOUNT_SID'], 'AC1')
            hooks = DefaultCallHooks(environ={}, env_file=env_file)
            self.assertEqual(hooks.credentials('local', 'openai'), {'apiKey': 'from-file'})
            with self.assertRaises(MissingCredentials) as caught:
                hooks.credentials('local', 'twilio')
            self.assertEqual(caught.exception.missing, ('TWILIO_AUTH_TOKEN', 'TWILIO_FROM_NUMBER'))
            override = DefaultCallHooks(environ={'OPENAI_API_KEY': 'from-env'}, env_file=env_file)
            self.assertEqual(override.credentials('local', 'openai')['apiKey'], 'from-env')

    def test_calling_code_allow_list(self):
        hooks = DefaultCallHooks(environ={'COLLEAGUE_ALLOWED_CALLING_CODES': '1, +44'})
        hooks.precheck('local', CallBrief.from_dict(phone_brief()))
        hooks.precheck('local', CallBrief.from_dict(phone_brief(to='+442071838750')))
        with self.assertRaises(CallRefused):
            hooks.precheck('local', CallBrief.from_dict(phone_brief(to='+919876543210')))
        DefaultCallHooks(environ={}).precheck('local', CallBrief.from_dict(phone_brief(to='+919876543210')))

    def test_load_hooks(self):
        self.assertIsInstance(load_hooks(None, environ={}), DefaultCallHooks)
        hooks = load_hooks('call_hooks:DefaultCallHooks', environ={})
        self.assertIsInstance(hooks, DefaultCallHooks)
        with self.assertRaises(ValueError):
            load_hooks('call_hooks')


class ResultTests(unittest.IsolatedAsyncioTestCase):
    transcript = [{'speaker': 'agent', 'text': "Hi, I'm an AI assistant calling for Robin."},
                  {'speaker': 'other', 'text': 'Sure, 7pm works. Confirmation LG-2291.'}]

    def test_results_without_a_conversation(self):
        self.assertEqual(result_without_conversation('no_answer')['outcome'], 'not_reached')
        self.assertEqual(result_without_conversation('busy')['summary'], 'The line was busy.')
        self.assertEqual(result_without_conversation('canceled')['outcome'], 'canceled')
        self.assertEqual(result_without_conversation('hangup')['outcome'], 'not_reached')
        self.assertIsNone(result_without_conversation('hangup', transcript=self.transcript))

    def test_meeting_handoff_mapping(self):
        result = result_from_handoff({
            'summary': 'Agreed on the Q3 correction.',
            'decisions': [{'text': 'Use $800,000'}],
            'actionItems': [{'text': 'Send the deck', 'owner': 'Casey'}],
            'unresolvedQuestions': ['Who presents?'],
            'partial': True,
        }, duration_seconds=600)
        self.assertEqual(result['outcome'], 'partial')
        self.assertEqual(result['decisions'], ['Use $800,000'])
        self.assertEqual(result['actionItems'], ['Casey: Send the deck'])
        self.assertEqual(result['openQuestions'], ['Who presents?'])
        self.assertEqual(result['durationSeconds'], 600)

    async def test_summarizer_request_and_parse(self):
        captured = {}

        async def post(payload):
            captured.update(payload)
            return {'output': [{'type': 'message', 'content': [{'type': 'output_text', 'text': json.dumps({
                'outcome': 'achieved', 'summary': 'Booked for 7pm.',
                'details': [{'label': 'Confirmation', 'value': 'LG-2291'}, {'label': '', 'value': 'x'}],
                'decisions': ['7pm'], 'actionItems': [], 'openQuestions': [],
            })}]}], 'usage': {'input_tokens': 900, 'output_tokens': 80}}
        summarizer = ResponsesSummarizer('sk-test', post=post)
        result = await summarizer.summarize({'objective': 'Book', 'onBehalfOf': 'Robin'},
                                            self.transcript, duration_seconds=65)
        self.assertEqual(captured['text']['format']['schema'], RESULT_SCHEMA)
        self.assertTrue(captured['text']['format']['strict'])
        self.assertFalse(captured['store'])
        self.assertIn('Other party: Sure, 7pm works.', captured['input'])
        self.assertEqual(result['details'], [{'label': 'Confirmation', 'value': 'LG-2291'}])
        self.assertEqual(result['summaryTokens'], {'input': 900, 'output': 80})
        self.assertEqual(result['transcript'][1]['speaker'], 'other')

    async def test_summarizer_failures(self):
        async def bad(payload):
            return {'output': [{'type': 'message', 'content': [{'type': 'output_text', 'text': 'nope'}]}]}
        with self.assertRaises(SummaryUnavailable):
            await ResponsesSummarizer('k', post=bad).summarize({}, self.transcript)
        with self.assertRaises(SummaryUnavailable):
            ResponsesSummarizer('')
        fallback = fallback_result('voicemail', self.transcript, 30, 'boom')
        self.assertEqual(fallback['outcome'], 'voicemail')
        self.assertEqual(len(fallback['transcript']), 2)

    def test_transcript_text_is_bounded(self):
        long = [{'speaker': 'other', 'text': 'x' * 1000}] * 100
        self.assertLessEqual(len(transcript_text(long, limit=2000)), 2100)


class FakeMessage:
    def __init__(self, data):
        from aiohttp import WSMsgType
        self.type = WSMsgType.TEXT
        self.data = json.dumps(data)


class FakeWs:
    def __init__(self, incoming):
        self.sent = []
        self.incoming = incoming
        self.closed = False

    async def send_json(self, payload):
        self.sent.append(payload)

    async def close(self):
        self.closed = True

    def __aiter__(self):
        async def generator():
            for item in self.incoming:
                yield FakeMessage(item)
        return generator()


class VoiceCoreTests(unittest.IsolatedAsyncioTestCase):
    def test_config_and_appends(self):
        config = session_config(instructions='Be brief.', audio_format=PCMU8, voice='quartz',
                                delegation={'type': 'responses', 'responses': {'model': 'm'}})
        self.assertEqual(config['audio'], {'format': {'type': 'audio/pcmu', 'rate': 8000},
                                           'output': {'voice': 'quartz'}})
        self.assertFalse(config['store'])
        event = append_event('session.commentary.append', 'Say hello', None)
        self.assertIsNone(event['delegation_id'])
        self.assertIsNone(append_event('session.thinking.append', '   '))
        with self.assertRaises(ValueError):
            append_event('session.update', 'x')

    async def test_session_lifecycle(self):
        ws = FakeWs([
            {'type': 'session.started', 'session': {'id': 'sess_1'}},
            {'type': 'session.output_audio.delta', 'delta': 'AAAA'},
            {'type': 'session.usage.updated', 'usage': {'seconds': 12}},
            {'type': 'session.closed', 'reason': 'close_requested', 'usage': {'seconds': 30}},
            {'type': 'session.output_audio.delta', 'delta': 'never'},
        ])

        async def connect(url, key):
            self.assertEqual(key, 'sk-test')
            return ws, None
        seen = []
        async with LiveSession('sk-test', {'model': 'gpt-live-1'}, connect=connect) as live:
            self.assertEqual(ws.sent[0]['type'], 'session.start')
            self.assertFalse(await live.send_audio(b'\x00\x01'))
            async for event in live.events():
                seen.append(event['type'])
                if event['type'] == 'session.output_audio.delta':
                    self.assertTrue(await live.send_audio('AAAA'))
        self.assertEqual(seen, ['session.started', 'session.output_audio.delta',
                                'session.usage.updated', 'session.closed'])
        self.assertEqual((live.session_id, live.usage_seconds, live.close_reason),
                         ('sess_1', 30, 'close_requested'))
        self.assertEqual(ws.sent[1], {'type': 'session.input_audio.append', 'audio': 'AAAA'})
        self.assertTrue(ws.closed)

    async def test_startup_error_raises(self):
        ws = FakeWs([{'type': 'error', 'error': {'code': 'model_not_found'}}])

        async def connect(url, key):
            return ws, None
        with self.assertRaises(LiveSessionError) as caught:
            async with LiveSession('k', {}, connect=connect) as live:
                async for _event in live.events():
                    pass
        self.assertEqual(caught.exception.code, 'model_not_found')

    async def test_function_calls_and_usage(self):
        event = {'type': 'response.event', 'delegation_id': 'd1', 'event': {
            'type': 'response.output_item.done',
            'item': {'type': 'function_call', 'call_id': 'c1', 'name': 'end_call',
                     'arguments': '{"reason": "done"}'}}}
        self.assertEqual(function_call_from(event), {
            'delegation_id': 'd1', 'call_id': 'c1', 'name': 'end_call',
            'arguments': {'reason': 'done'}})
        self.assertIsNone(function_call_from({'type': 'session.started'}))
        usage = backend_usage_from({'type': 'response.event', 'event': {
            'type': 'response.completed', 'response': {'usage': {'input_tokens': 5, 'output_tokens': 2}}}})
        self.assertEqual(usage, {'input': 5, 'output': 2})

        ws = FakeWs([])

        async def connect(url, key):
            return ws, None
        async with LiveSession('k', {}, connect=connect) as live:
            await live.submit_function_output('c1', {'ok': True}, delegation_id='d1')
        self.assertEqual(ws.sent[1]['item'], {'type': 'function_call_output', 'call_id': 'c1',
                                              'output': '{"ok": true}'})
        self.assertEqual(ws.sent[2]['type'], 'response.create')


if __name__ == '__main__':
    unittest.main()
