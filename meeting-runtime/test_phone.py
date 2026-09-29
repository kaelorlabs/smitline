import asyncio
import base64
import hashlib
import hmac
import json
import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qsl

from aiohttp import WSMsgType
from aiohttp.test_utils import TestClient, TestServer

from call_brief import CallBrief
from call_hooks import DefaultCallHooks
from call_service import CallError, CallService
from call_store import CallStore
from phone_gateway import create_gateway_app
from phone_line import OutputPacer, PhoneLine, UtteranceJoiner, inbound_brief
from phone_prompts import (
    backend_instructions, delegation_config, discloses, mentions_ai, voice_instructions,
)
from tunnel import PublicUrl, TunnelError, configured_url
from twilio_client import (
    TwilioClient, compute_signature, dial_twiml, stream_twiml, valid_signature,
)


PUBLIC = 'https://abc-123.trycloudflare.com'
ENV = {'OPENAI_API_KEY': 'sk-test', 'TWILIO_ACCOUNT_SID': 'AC1', 'TWILIO_AUTH_TOKEN': 'tw-secret',
       'TWILIO_FROM_NUMBER': '+15005550006'}


def brief(**overrides):
    payload = {'channel': 'phone', 'to': '+14155550142', 'onBehalfOf': 'Robin',
               'objective': 'Book a table for 4 at 7pm'}
    payload.update(overrides)
    return payload


class FakeTwilio:
    def __init__(self):
        self.created = []
        self.updates = []
        self.remote_status = 'ringing'
        self.refuse_twiml = False

    async def create_call(self, **kwargs):
        self.created.append(kwargs)
        return {'sid': 'CA123'}

    async def get_call(self, call_sid):
        return {'sid': call_sid, 'status': self.remote_status}

    async def update_call(self, call_sid, **kwargs):
        if self.refuse_twiml and 'twiml' in kwargs:
            from twilio_client import TwilioError
            raise TwilioError(400, 21220, 'Call is not in-progress. Cannot redirect.')
        self.updates.append((call_sid, kwargs))
        return {}


class FakeLive:
    instances = []

    def __init__(self, key, config):
        self.key = key
        self.config = config
        self.started = asyncio.Event()
        self.closed = asyncio.Event()
        self.queue = asyncio.Queue()
        self.audio = []
        self.appends = []
        self.outputs = []
        self.close_requested = False
        self.usage_seconds = 0
        FakeLive.instances.append(self)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        self.closed.set()

    def push(self, event):
        self.queue.put_nowait(event)

    async def send_audio(self, audio):
        self.audio.append(audio)
        return True

    async def append(self, kind, content, delegation_id=None):
        self.appends.append((kind, content))
        return True

    async def submit_function_output(self, call_id, output, *, delegation_id=None):
        self.outputs.append((call_id, output, delegation_id))

    async def close(self):
        self.close_requested = True
        self.push({'type': 'session.closed', 'reason': 'close_requested', 'usage': {'seconds': 33}})

    async def events(self):
        while True:
            event = await self.queue.get()
            if event['type'] == 'drop':  # the socket closes without session.closed
                return
            if event['type'] == 'session.started':
                self.started.set()
            if event['type'] == 'session.closed':
                self.usage_seconds = event['usage']['seconds']
                yield event
                self.closed.set()
                return
            yield event


class Message:
    def __init__(self, data):
        self.type = WSMsgType.TEXT
        self.data = json.dumps(data)


class FakeTwilioSocket:
    def __init__(self):
        self.queue = asyncio.Queue()
        self.sent = []
        self.closed = False

    def push(self, data):
        self.queue.put_nowait(Message(data))

    async def send_str(self, text):
        self.sent.append(json.loads(text))

    async def close(self, **kwargs):
        if not self.closed:
            self.closed = True
            self.queue.put_nowait(None)

    def __aiter__(self):
        return self

    async def __anext__(self):
        item = await self.queue.get()
        if item is None:
            raise StopAsyncIteration
        return item


class FakeSummarizer:
    calls = 0

    async def summarize(self, brief, transcript, *, duration_seconds=0):
        FakeSummarizer.calls += 1
        return {'outcome': 'achieved', 'summary': 'Booked.', 'details': [], 'decisions': [],
                'actionItems': [], 'openQuestions': [], 'transcript': transcript,
                'durationSeconds': duration_seconds, 'source': 'summary_model'}


async def until(predicate, timeout=3.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError('condition not met in time')
        await asyncio.sleep(0.01)


class PhoneHarness:
    def __init__(self, temp, env=None):
        self.env = dict(ENV if env is None else env)
        self.twilio = FakeTwilio()
        self.store = CallStore(Path(temp) / 'calls')
        self.hooks = DefaultCallHooks(environ=self.env, store=self.store)

        self.url_ready = asyncio.Event()
        self.url_ready.set()

        async def public_url():
            await self.url_ready.wait()
            return PUBLIC
        self.line = PhoneLine(public_url=public_url, environ=lambda: self.env,
                              twilio_factory=lambda creds: self.twilio,
                              live_factory=FakeLive, status_grace=0.2)
        self.service = CallService(self.store, hooks=self.hooks, lines={'phone': self.line},
                                   summarizer_factory=lambda owner: FakeSummarizer(), environ={})

    async def dial(self, **overrides):
        record = await self.service.create(brief(**overrides))
        await until(lambda: self.twilio.created and self.line.session(record['id']))
        return record, self.line.session(record['id'])

    async def connect(self, record, session):
        ws = FakeTwilioSocket()
        claimed = self.line.claim(record['id'], session.token)
        assert claimed is session
        task = asyncio.create_task(session.run(ws, {'streamSid': 'MZ1', 'callSid': 'CA123'}))
        await until(lambda: FakeLive.instances and FakeLive.instances[-1] is session.live)
        return ws, session.live, task


class PhoneLineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        FakeLive.instances = []
        FakeSummarizer.calls = 0
        self.temp = tempfile.TemporaryDirectory()
        self.h = PhoneHarness(self.temp.name)

    async def asyncTearDown(self):
        await self.h.service.shutdown()
        self.temp.cleanup()

    async def test_outbound_call_end_to_end(self):
        record, session = await self.h.dial(voice='quartz')
        created = self.h.twilio.created[0]
        self.assertEqual((created['to'], created['from_']), ('+14155550142', '+15005550006'))
        self.assertIn('wss://abc-123.trycloudflare.com/twilio/media', created['twiml'])
        self.assertIn(f'value="{record["id"]}"', created['twiml'])
        self.assertIn(session.token, created['twiml'])
        self.assertEqual(created['status_callback'], f'{PUBLIC}/twilio/status/{record["id"]}')
        self.assertEqual(created['amd_callback'], f'{PUBLIC}/twilio/amd/{record["id"]}')
        self.assertIsNone(self.h.line.claim(record['id'], 'wrong-token'))

        self.h.line.on_status(record['id'], {'CallStatus': 'ringing'})
        self.assertEqual(self.h.store.get(record['id'])['status'], 'ringing')

        ws, live, task = await self.h.connect(record, session)
        config = live.config
        self.assertEqual(config['audio']['format'], {'type': 'audio/pcmu', 'rate': 8000})
        self.assertEqual(config['audio']['output'], {'voice': 'quartz'})
        self.assertEqual(config['delegation']['type'], 'responses')
        self.assertEqual(config['delegation']['responses']['tools'][0]['name'], 'end_call')
        self.assertIn("calling on behalf of Robin", config['instructions'])
        self.assertEqual(self.h.store.get(record['id'])['status'], 'in_progress')

        live.push({'type': 'session.started', 'session': {'id': 'sess_1'}})
        ws.push({'event': 'media', 'media': {'track': 'inbound', 'payload': 'f39/'}})
        live.push({'type': 'session.input_transcript.delta', 'delta': 'Hello?', 'start_ms': 0, 'end_ms': 400})
        await until(lambda: any(kind == 'session.commentary.append' for kind, _ in live.appends))
        self.assertEqual(live.audio, ['f39/'])
        live.push({'type': 'session.output_transcript.delta',
                   'delta': "Hi, I'm an AI assistant calling on behalf of Robin.", 'start_ms': 600, 'end_ms': 2000})
        audio = base64.b64encode(b'\xff' * 800).decode()
        live.push({'type': 'session.output_audio.delta', 'delta': audio})
        await until(lambda: any(m['event'] == 'mark' for m in ws.sent))
        media = [m for m in ws.sent if m['event'] == 'media']
        self.assertEqual({m['streamSid'] for m in media}, {'MZ1'})
        # Sent as 20 ms frames: 800 bytes of mu-law is five 160-byte frames.
        self.assertEqual(len(media), 5)
        self.assertEqual(b''.join(base64.b64decode(m['media']['payload']) for m in media), b'\xff' * 800)
        mark = next(m for m in ws.sent if m['event'] == 'mark')
        ws.push({'event': 'mark', 'mark': mark['mark']})
        live.push({'type': 'session.input_transcript.delta', 'delta': 'Sure, booked.', 'start_ms': 2500, 'end_ms': 3000})
        live.push({'type': 'response.event', 'delegation_id': 'd1', 'event': {
            'type': 'response.completed', 'response': {'usage': {'input_tokens': 300, 'output_tokens': 20}}}})
        live.push({'type': 'response.event', 'delegation_id': 'd1', 'event': {
            'type': 'response.output_item.done', 'item': {
                'type': 'function_call', 'call_id': 'fc1', 'name': 'end_call',
                'arguments': '{"reason": "completed"}'}}})
        await until(lambda: ws.closed)
        # Hanging up needs no tool result, so no further backend turn is requested.
        self.assertEqual(live.outputs, [])
        await asyncio.wait_for(task, 3)
        self.h.line.on_status(record['id'], {'CallStatus': 'completed', 'CallDuration': '40'})
        done = await self.h.service.wait(record['id'], timeout=5)
        self.assertEqual(done['status'], 'completed')
        self.assertEqual(done['endReason'], 'hangup')
        self.assertEqual(done['usage']['voiceSeconds'], 33)
        self.assertEqual(done['usage']['phoneSeconds'], 40)
        self.assertEqual(done['usage']['backendTokens'], {'input': 300, 'output': 20})
        self.assertEqual([line['speaker'] for line in done['result']['transcript']],
                         ['other', 'agent', 'other'])
        self.assertIn(('CA123', {'status': 'completed'}), self.h.twilio.updates)
        events = self.h.service.events(record['id'])
        disclosure = [e for e in events if e['type'] == 'call.disclosure']
        self.assertEqual(disclosure[0]['data'], {'verified': True, 'attempt': 1})
        self.assertIs(done['result']['disclosureVerified'], True)

    async def test_unanswered_call_needs_no_summary(self):
        record, _session = await self.h.dial()
        self.h.line.on_status(record['id'], {'CallStatus': 'no-answer', 'CallDuration': '0'})
        done = await self.h.service.wait(record['id'], timeout=5)
        self.assertEqual((done['endReason'], done['result']['outcome']), ('no_answer', 'not_reached'))
        self.assertEqual(FakeSummarizer.calls, 0)

    def test_disclosure_check(self):
        said = [
            ("Hi, I'm an AI assistant calling on behalf of Robin Rao.", 'Robin Rao', True),
            ('Hola, soy un asistente de IA y llamo de parte de Robin.', 'Robin', True),
            ('Hallo, hier ist ein KI-Assistent im Auftrag von Robin.', 'Robin', True),
            ('Bonjour, je suis une intelligence artificielle qui appelle pour Robin.', 'Robin', True),
            ("Hi, I'm an A.I. calling for Dr. Lee's office.", 'Dr. Lee', True),
            ('Hi, this is an assistant calling for Robin.', 'Robin', False),
            ("Hi, I'm an AI calling about a table.", 'Robin', False),
            ("Hi, I'm an AI calling about the same thing.", 'Sam', False),
        ]
        for text, name, expected in said:
            with self.subTest(text=text):
                self.assertIs(discloses(text, name), expected)
        self.assertFalse(mentions_ai('I said it again'))
        rehearsal = CallBrief.from_dict(brief(language='es', rehearsal=True))
        backend = backend_instructions(rehearsal)
        self.assertIn('language with tag es', backend)
        self.assertIn('rehearsal', backend)
        self.assertNotIn('rehearsal', backend_instructions(CallBrief.from_dict(brief())))

    async def test_missing_disclosure_is_corrected(self):
        record, session = await self.h.dial()
        ws, live, task = await self.h.connect(record, session)
        live.push({'type': 'session.started', 'session': {}})
        live.push({'type': 'session.output_transcript.delta', 'delta': 'Hello there, I would like a table.',
                   'start_ms': 0, 'end_ms': 900})
        live.push({'type': 'session.input_transcript.delta', 'delta': 'Who is this?', 'start_ms': 1000,
                   'end_ms': 1400})
        await until(lambda: any(kind == 'session.instructions.append' for kind, _ in live.appends))
        # The next utterance is checked again; a second miss is final.
        live.push({'type': 'session.output_transcript.delta', 'delta': 'Sorry, just a table for four.',
                   'start_ms': 3000, 'end_ms': 3900})
        live.push({'type': 'session.input_transcript.delta', 'delta': 'Okay.', 'start_ms': 5000,
                   'end_ms': 5400})
        await until(lambda: sum(e['type'] == 'call.disclosure'
                                for e in self.h.service.events(record['id'])) == 2)
        ws.push({'event': 'stop'})
        await asyncio.wait_for(task, 3)
        done = await self.h.service.wait(record['id'], timeout=5)
        self.assertEqual(done['endReason'], 'remote_hangup')
        self.assertTrue(live.close_requested)
        self.assertIs(done['result']['disclosureVerified'], False)

    async def test_voicemail_and_transfer(self):
        record, session = await self.h.dial()
        ws, live, task = await self.h.connect(record, session)
        live.push({'type': 'session.started', 'session': {}})
        self.h.line.on_amd(record['id'], 'machine_end_beep')
        await until(lambda: any('voicemail' in text for _kind, text in live.appends))
        with self.assertRaises(CallError) as caught:
            await self.h.service.transfer(record['id'])
        self.assertEqual(caught.exception.code, 'not_ready')
        self.h.env['COLLEAGUE_OWNER_PHONE'] = '+14155550199'
        session.owner_phone = '+14155550199'
        result = await self.h.service.transfer(record['id'])
        self.assertEqual(result, {'transferred': True})
        twiml = self.h.twilio.updates[-1][1]['twiml']
        self.assertIn('+14155550199', twiml)
        self.assertIn('callerId="+15005550006"', twiml)
        ws.push({'event': 'stop'})
        await asyncio.wait_for(task, 3)
        done = await self.h.service.wait(record['id'], timeout=5)
        self.assertEqual(done['endReason'], 'transferred')

    async def test_end_while_ringing_cancels(self):
        record, _session = await self.h.dial()
        await self.h.service.end(record['id'])
        done = await self.h.service.wait(record['id'], timeout=5)
        self.assertEqual(done['status'], 'canceled')
        self.assertEqual(self.h.twilio.updates[0], ('CA123', {'status': 'canceled'}))

    async def test_readiness(self):
        with tempfile.TemporaryDirectory() as temp:
            missing = PhoneHarness(temp, env={'OPENAI_API_KEY': 'k'})
            with self.assertRaises(CallError) as caught:
                await missing.service.create(brief())
            self.assertEqual(caught.exception.details['missing'],
                             ['TWILIO_ACCOUNT_SID', 'TWILIO_AUTH_TOKEN',
                              'TWILIO_FROM_NUMBER or COLLEAGUE_CALLER_ID'])
        self.h.line.gateway_ready = False
        with self.assertRaises(CallError) as caught:
            await self.h.service.create(brief())
        self.assertEqual(caught.exception.code, 'gateway_unavailable')

    async def test_end_while_the_tunnel_starts_never_dials(self):
        self.h.url_ready.clear()
        record = await self.h.service.create(brief())
        await until(lambda: self.h.line.session(record['id']) is not None)
        await self.h.service.end(record['id'])
        self.h.url_ready.set()
        done = await self.h.service.wait(record['id'], timeout=5)
        self.assertEqual(done['status'], 'canceled')
        self.assertEqual(self.h.twilio.created, [])

    async def test_a_call_that_never_connects_is_ended_and_explained(self):
        self.h.line.connect_timeout = 0.1
        self.h.twilio.remote_status = 'in-progress'
        record, _session = await self.h.dial()
        done = await self.h.service.wait(record['id'], timeout=5)
        self.assertEqual(done['status'], 'failed')
        self.assertIn('could not reach the phone gateway', done['error'])
        self.assertIn(('CA123', {'status': 'completed'}), self.h.twilio.updates)
        # Twilio says nobody answered and the callback was lost: an ordinary no-answer.
        self.h.twilio.remote_status = 'no-answer'
        record, _session = await self.h.dial()
        done = await self.h.service.wait(record['id'], timeout=5)
        self.assertEqual((done['status'], done['endReason']), ('completed', 'no_answer'))

    async def test_voice_session_failures_are_errors(self):
        class Broken(FakeLive):
            async def __aenter__(self):
                raise RuntimeError('401 invalid_api_key')
        self.h.line.live_factory = Broken
        record, session = await self.h.dial()
        ws = FakeTwilioSocket()
        await session.run(ws, {'streamSid': 'MZ1', 'callSid': 'CA123'})
        done = await self.h.service.wait(record['id'], timeout=5)
        self.assertEqual(done['status'], 'failed')
        self.assertIn('voice session could not start', done['error'])
        self.assertTrue(ws.closed)

        self.h.line.live_factory = FakeLive
        record, session = await self.h.dial()
        ws, live, task = await self.h.connect(record, session)
        live.push({'type': 'session.started', 'session': {}})
        live.push({'type': 'session.input_transcript.delta', 'delta': 'Hello?', 'start_ms': 0, 'end_ms': 300})
        live.push({'type': 'drop'})
        await asyncio.wait_for(task, 3)
        done = await self.h.service.wait(record['id'], timeout=5)
        self.assertEqual(done['endReason'], 'error')
        self.assertIn('voice connection was lost', done['error'])

    async def test_transfer_cancels_the_pending_hangup_and_failures_are_reported(self):
        self.h.env['COLLEAGUE_OWNER_PHONE'] = '+14155550199'
        record, session = await self.h.dial()
        session.owner_phone = '+14155550199'
        ws, live, task = await self.h.connect(record, session)
        live.push({'type': 'session.started', 'session': {}})
        await self.h.service.end(record['id'])
        pending = session._pending_hangup
        self.assertIsNotNone(pending)
        self.h.twilio.refuse_twiml = True
        with self.assertRaises(CallError) as caught:
            await self.h.service.transfer(record['id'])
        self.assertEqual((caught.exception.status, caught.exception.code), (502, 'provider_error'))
        self.assertIsNone(session.end_reason)
        self.assertTrue(pending.cancelled() or pending.done())
        self.h.twilio.refuse_twiml = False
        await self.h.service.transfer(record['id'])
        self.assertEqual(session.end_reason, 'transferred')
        ws.push({'event': 'stop'})
        await asyncio.wait_for(task, 3)
        await asyncio.sleep(0.05)
        self.assertNotIn(('CA123', {'status': 'completed'}), self.h.twilio.updates)

    def test_inbound_brief_tolerates_withheld_numbers_and_bad_webhooks(self):
        parsed = inbound_brief({'TWILIO_FROM_NUMBER': '+15005550006',
                                'COLLEAGUE_NOTIFY_WEBHOOK': 'http://example.com/x'}, 'anonymous')
        self.assertEqual(parsed.to, '+15005550006')
        self.assertIn('withheld', parsed.context)
        self.assertIsNone(parsed.webhook_url)


class PromptTests(unittest.TestCase):
    def test_instructions_cover_disclosure_and_boundaries(self):
        parsed = CallBrief.from_dict(brief(mayAgreeTo=['6:30 to 7:30pm'], mustNotShare=['card number'],
                                           language='es'))
        text = voice_instructions(parsed)
        self.assertIn("Hi, I'm an AI assistant calling on behalf of Robin.", text)
        self.assertIn('- 6:30 to 7:30pm', text)
        self.assertIn('- card number', text)
        self.assertIn('language with tag es', text)
        self.assertIn('rehearsal', voice_instructions(CallBrief.from_dict(brief(rehearsal=True))))
        self.assertIn("reached Robin's AI assistant", voice_instructions(parsed, inbound=True))
        config = delegation_config(parsed, model='gpt-5.6-luna', web_search=True)
        self.assertEqual([tool.get('name', tool['type']) for tool in config['responses']['tools']],
                         ['end_call', 'web_search'])
        self.assertEqual(config['responses']['model'], 'gpt-5.6-luna')

    def test_mentions_ai(self):
        self.assertTrue(mentions_ai("Hi, I'm an AI assistant calling"))
        self.assertTrue(mentions_ai('This is an automated call'))
        self.assertFalse(mentions_ai('Hello, I would like a table'))

    def test_inbound_brief(self):
        parsed = inbound_brief({'COLLEAGUE_OWNER_NAME': 'Robin',
                                'COLLEAGUE_NOTIFY_WEBHOOK': 'https://example.com/h'}, '+14155550100')
        self.assertEqual((parsed.on_behalf_of, parsed.webhook_url), ('Robin', 'https://example.com/h'))


class PacingTests(unittest.IsolatedAsyncioTestCase):
    async def test_output_stays_near_realtime(self):
        now = [0.0]
        slept = []
        sent = []

        async def sleep(seconds):
            slept.append(round(seconds, 3))
            now[0] += seconds

        queued = [0.0]  # seconds of audio handed to Twilio so far
        ahead = []

        async def send(message):
            sent.append(message)
            if message['event'] == 'media':
                ahead.append(queued[0] - now[0])
                queued[0] += len(base64.b64decode(message['media']['payload'])) / 8000
        pacer = OutputPacer(send, clock=lambda: now[0], sleep=sleep)
        chunk = base64.b64encode(b'\xff' * 4000).decode()  # 0.5 s of mu-law
        for _ in range(3):
            pacer.offer(chunk)
        runner = asyncio.create_task(pacer.run())
        # 1.5 s of audio goes out as 75 frames of 20 ms, plus one mark per delta.
        await until(lambda: len(sent) == 78)
        runner.cancel()
        self.assertEqual([m['mark']['name'] for m in sent if m['event'] == 'mark'],
                         ['out-1', 'out-2', 'out-3'])
        self.assertLessEqual(max(ahead), pacer.lead + 0.02 + 1e-9)
        self.assertAlmostEqual(sum(slept), 1.5 - pacer.lead - 0.02, delta=0.021)
        self.assertFalse(await pacer.drained(0.01))
        pacer.mark_played('out-3')
        self.assertTrue(await pacer.drained(0.01))

    def test_joiner(self):
        joiner = UtteranceJoiner(gap_ms=1000)
        self.assertEqual(joiner.add('other', 'Hel', 0, 200), [])
        self.assertEqual(joiner.add('other', 'lo?', 200, 400), [])
        self.assertEqual(joiner.add('agent', 'Hi ', 500, 700), [('other', 'Hello?')])
        self.assertEqual(joiner.add('agent', 'there', 3000, 3200), [('agent', 'Hi')])
        self.assertEqual(joiner.flush(), [('agent', 'there')])


class TwilioTests(unittest.IsolatedAsyncioTestCase):
    def test_signature(self):
        params = {'CallSid': 'CA1', 'From': '+1415', 'CallStatus': 'ringing'}
        url = 'https://abc.example.com/twilio/status/call-1'
        payload = url + 'CallSidCA1CallStatusringingFrom+1415'
        expected = base64.b64encode(hmac.new(b'tok', payload.encode(), hashlib.sha1).digest()).decode()
        self.assertEqual(compute_signature('tok', url, params), expected)
        self.assertTrue(valid_signature('tok', url, params, expected))
        self.assertFalse(valid_signature('tok', url, {**params, 'From': '+1'}, expected))
        self.assertFalse(valid_signature(None, url, params, expected))

    def test_twiml_escapes(self):
        import xml.etree.ElementTree as ET
        twiml = stream_twiml('wss://x/twilio/media', {'callId': 'c"1', 'token': '<t>'}, say='A & B')
        root = ET.fromstring(twiml)
        self.assertEqual(root.find('Say').text, 'A & B')
        values = {p.get('name'): p.get('value') for p in root.iter('Parameter')}
        self.assertEqual(values, {'callId': 'c"1', 'token': '<t>'})

    def test_dial_twiml_speaks_a_fallback_when_nobody_answers(self):
        import xml.etree.ElementTree as ET
        root = ET.fromstring(dial_twiml('+14155550100', '+15005550006', fallback='Sorry & bye'))
        self.assertEqual(root[0].tag, 'Dial')
        self.assertEqual(root[0].get('callerId'), '+15005550006')
        self.assertEqual((root[1].tag, root[1].text), ('Say', 'Sorry & bye'))
        self.assertEqual(len(ET.fromstring(dial_twiml('+1', '+2'))), 1)

    async def test_create_call_form(self):
        captured = {}

        async def request(method, url, form):
            captured.update(method=method, url=url, form=form)
            return 201, {'sid': 'CA9'}
        client = TwilioClient('AC1', 'tok', request=request)
        result = await client.create_call(to='+1', from_='+2', twiml='<Response/>',
                                          status_callback='https://x/s', amd_callback='https://x/a',
                                          record=True, time_limit=690)
        self.assertEqual(result, {'sid': 'CA9'})
        self.assertTrue(captured['url'].endswith('/Accounts/AC1/Calls.json'))
        form = parse_qsl(captured['form'])
        self.assertEqual([v for k, v in form if k == 'StatusCallbackEvent'],
                         ['initiated', 'ringing', 'answered', 'completed'])
        self.assertIn(('AsyncAmd', 'true'), form)
        self.assertIn(('Record', 'true'), form)
        self.assertIn(('TimeLimit', '690'), form)
        self.assertNotIn('RecordingStatusCallback', dict(form))
        await client.create_call(to='+1', from_='+2', twiml='<Response/>', record=True,
                                 recording_callback='https://x/twilio/recording/call-1')
        form = dict(parse_qsl(captured['form']))
        self.assertEqual(form['RecordingStatusCallback'], 'https://x/twilio/recording/call-1')
        self.assertEqual(form['RecordingStatusCallbackEvent'], 'completed')

        async def failing(method, url, form):
            return 400, {'code': 21211, 'message': "The 'To' number is not a valid phone number."}
        from twilio_client import TwilioError
        with self.assertRaises(TwilioError) as caught:
            await TwilioClient('AC1', 'tok', request=failing).update_call('CA1', status='completed')
        self.assertEqual(caught.exception.code, 21211)

    async def test_inbound_number_follows_the_gateway_address(self):
        from daemon_main import configure_inbound
        requests = []

        async def request(method, url, form):
            requests.append((method, url, form))
            if method == 'GET':
                return 200, {'incoming_phone_numbers': [{'sid': 'PN1', 'phone_number': '+15005550006'}]}
            return 200, {'sid': 'PN1'}

        class Service:
            hooks = DefaultCallHooks(environ=dict(ENV))

            class public_url:
                @staticmethod
                async def get():
                    return PUBLIC + '/'
        logs = []
        configured = await configure_inbound(
            Service, twilio_factory=lambda creds: TwilioClient(creds['accountSid'], creds['authToken'],
                                                               request=request),
            log=lambda *args, **kwargs: logs.append(args[0]))
        self.assertTrue(configured)
        self.assertIn('PhoneNumber=%2B15005550006', requests[0][1])
        requests.clear()
        Service.hooks = DefaultCallHooks(environ={**ENV, 'COLLEAGUE_CALLER_ID': '+14155550199'})
        self.assertEqual(Service.hooks.credentials('local', 'twilio')['fromNumber'], '+14155550199')
        await configure_inbound(
            Service, twilio_factory=lambda creds: TwilioClient(creds['accountSid'], creds['authToken'],
                                                               request=request),
            log=lambda *args, **kwargs: logs.append(args[0]))
        self.assertIn('PhoneNumber=%2B15005550006', requests[0][1])
        # A caller ID alone can place calls but cannot receive them.
        only_caller_id = {k: v for k, v in ENV.items() if k != 'TWILIO_FROM_NUMBER'}
        Service.hooks = DefaultCallHooks(environ={**only_caller_id, 'COLLEAGUE_CALLER_ID': '+14155550199'})
        self.assertFalse(await configure_inbound(
            Service, twilio_factory=lambda creds: TwilioClient(creds['accountSid'], creds['authToken'],
                                                               request=request),
            log=lambda *args, **kwargs: logs.append(args[0])))
        self.assertIn('cannot receive calls', logs[-1])
        Service.hooks = DefaultCallHooks(environ=dict(ENV))
        requests.clear()
        await configure_inbound(
            Service, twilio_factory=lambda creds: TwilioClient(creds['accountSid'], creds['authToken'],
                                                               request=request),
            log=lambda *args, **kwargs: logs.append(args[0]))
        self.assertTrue(requests[1][1].endswith('/IncomingPhoneNumbers/PN1.json'))
        self.assertEqual(dict(parse_qsl(requests[1][2])),
                         {'VoiceUrl': f'{PUBLIC}/twilio/inbound', 'VoiceMethod': 'POST'})

        class Unconfigured(Service):
            hooks = DefaultCallHooks(environ={'OPENAI_API_KEY': 'k'})
        self.assertFalse(await configure_inbound(Unconfigured, log=lambda *a, **k: logs.append(a[0])))
        self.assertIn('not configured', logs[-1])


class TunnelTests(unittest.IsolatedAsyncioTestCase):
    def test_configured_url(self):
        self.assertEqual(configured_url({'COLLEAGUE_PUBLIC_URL': 'https://calls.example.com/'}),
                         'https://calls.example.com')
        self.assertIsNone(configured_url({}))
        for bad in ('http://calls.example.com', 'https://calls.example.com/path'):
            with self.assertRaises(TunnelError):
                configured_url({'COLLEAGUE_PUBLIC_URL': bad})

    async def test_quick_tunnel_from_binary_or_docker(self):
        class Stream:
            def __init__(self, lines):
                self.lines = list(lines)

            async def readline(self):
                return self.lines.pop(0) if self.lines else b''

        output = [b'INF starting\n',
                  b'INF |  https://brave-fox-12.trycloudflare.com  |\n',
                  b'INF Registered tunnel connection connIndex=0 location=sjc01\n']

        class Process:
            returncode = None

            def __init__(self):
                self.stderr = Stream(output)

            def terminate(self):
                self.returncode = 0

            async def wait(self):
                return 0
        spawned = []

        async def spawn(*command, **kwargs):
            spawned.append(command)
            return Process()
        cloudflared = lambda name: '/usr/bin/cloudflared' if name == 'cloudflared' else None
        probed = []

        async def probe(url, timeout):
            probed.append(url)
            return True
        tunnel = PublicUrl(lambda: {}, 8766, spawn=spawn, which=cloudflared, probe=probe)
        self.assertIsNone(tunnel.current())
        self.assertEqual(await tunnel.get(), 'https://brave-fox-12.trycloudflare.com')
        self.assertEqual(await tunnel.get(), 'https://brave-fox-12.trycloudflare.com')
        self.assertEqual(tunnel.current(), 'https://brave-fox-12.trycloudflare.com')
        self.assertEqual(len(spawned), 1)
        self.assertEqual(spawned[0][-1], 'http://127.0.0.1:8766')
        self.assertEqual(probed, ['https://brave-fox-12.trycloudflare.com/healthz'])
        await tunnel.close()
        self.assertIsNone(tunnel.current())

        # cloudflared names api.trycloudflare.com in its own errors; that is never the tunnel.
        output[:] = [b'ERR failed to request quick Tunnel: Post "https://api.trycloudflare.com/tunnel": '
                     b'dial tcp: lookup api.trycloudflare.com: no such host\n']
        with self.assertRaises(TunnelError):
            await PublicUrl(lambda: {}, 8766, spawn=spawn, which=cloudflared).get()
        # An address without a registered connection is not ready.
        output[:] = [b'INF |  https://brave-fox-12.trycloudflare.com  |\n']
        with self.assertRaises(TunnelError):
            await PublicUrl(lambda: {}, 8766, spawn=spawn, which=cloudflared).get()
        # A registered tunnel that never answers is not handed to Twilio.
        output[:] = [b'INF |  https://brave-fox-12.trycloudflare.com  |\n',
                     b'INF Registered tunnel connection connIndex=0\n']

        async def unreachable(url, timeout):
            return False
        with self.assertRaises(TunnelError):
            await PublicUrl(lambda: {}, 8766, spawn=spawn, which=cloudflared,
                            probe=unreachable).get()
        docker = PublicUrl(lambda: {}, 8766, spawn=spawn, probe=probe,
                           which=lambda name: '/usr/bin/docker' if name == 'docker' else None)
        self.assertEqual(docker.command()[:4], ['docker', 'run', '--rm', '--network'])
        self.assertIn('colleague-tunnel-8766', docker.command())
        spawned.clear()
        self.assertEqual(await docker.get(), 'https://brave-fox-12.trycloudflare.com')
        self.assertEqual(spawned[0][:3], ('docker', 'rm', '-f'))
        await docker.close()
        self.assertFalse(PublicUrl(lambda: {}, 8766, which=lambda name: None).available())


class GatewayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        FakeLive.instances = []
        self.temp = tempfile.TemporaryDirectory()
        self.h = PhoneHarness(self.temp.name)
        self.current = PUBLIC
        app = create_gateway_app(self.h.line, self.h.service, current_url=lambda: self.current)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        await self.h.service.shutdown()
        self.temp.cleanup()

    async def post_signed(self, path, params, token='tw-secret'):
        signature = compute_signature(token, PUBLIC + path, params)
        return await self.client.post(path, data=params, headers={'X-Twilio-Signature': signature})

    async def test_status_requires_signature(self):
        record, _session = await self.h.dial()
        path = f'/twilio/status/{record["id"]}'
        response = await self.client.post(path, data={'CallStatus': 'ringing'})
        self.assertEqual(response.status, 403)
        response = await self.post_signed(path, {'CallStatus': 'ringing'}, token='wrong')
        self.assertEqual(response.status, 403)
        response = await self.post_signed(path, {'CallStatus': 'ringing'})
        self.assertEqual(response.status, 204)
        self.assertEqual(self.h.store.get(record['id'])['status'], 'ringing')
        # Without a current public address nothing is verified, and nothing is started.
        self.current = None
        response = await self.post_signed(path, {'CallStatus': 'ringing'})
        self.assertEqual(response.status, 403)

    async def test_media_stream_needs_the_call_token(self):
        record, session = await self.h.dial()
        ws = await self.client.ws_connect('/twilio/media')
        await ws.send_str(json.dumps({'event': 'connected'}))
        await ws.send_str(json.dumps({'event': 'start', 'start': {
            'streamSid': 'MZ1', 'callSid': 'CA123',
            'customParameters': {'callId': record['id'], 'token': 'nope'}}}))
        message = await ws.receive()
        self.assertEqual(message.type, WSMsgType.CLOSE)
        self.assertEqual(ws.close_code, 1008)
        self.assertFalse(session.connected.is_set())

    async def test_inbound_is_off_by_default_and_answers_when_enabled(self):
        params = {'CallSid': 'CA777', 'From': '+14155550100', 'To': '+15005550006'}
        response = await self.post_signed('/twilio/inbound', params)
        self.assertIn('<Reject/>', await response.text())
        self.h.env.update(COLLEAGUE_ACCEPT_INBOUND='1', COLLEAGUE_OWNER_NAME='Robin')
        response = await self.post_signed('/twilio/inbound', params)
        twiml = await response.text()
        self.assertIn('<Connect><Stream url="wss://abc-123.trycloudflare.com/twilio/media">', twiml)
        calls = self.h.service.list()
        self.assertEqual(calls[0]['direction'], 'inbound')
        self.assertIn(calls[0]['id'], twiml)


    async def test_recording_callback_is_signed_and_attached(self):
        record, _session = await self.h.dial()
        path = f'/twilio/recording/{record["id"]}'
        params = {'RecordingSid': 'RE1', 'RecordingStatus': 'completed', 'RecordingDuration': '42',
                  'RecordingUrl': 'https://api.twilio.com/2010-04-01/Accounts/AC1/Recordings/RE1'}
        response = await self.client.post(path, data=params)
        self.assertEqual(response.status, 403)
        response = await self.post_signed(path, params)
        self.assertEqual(response.status, 204)
        self.assertEqual(self.h.store.get(record['id'])['recording'], {
            'sid': 'RE1', 'seconds': 42,
            'url': 'https://api.twilio.com/2010-04-01/Accounts/AC1/Recordings/RE1.mp3'})
        response = await self.post_signed('/twilio/recording/call-missing', params)
        self.assertEqual(response.status, 204)

    async def test_inbound_calls_are_capped(self):
        self.h.env.update(COLLEAGUE_ACCEPT_INBOUND='1', COLLEAGUE_MAX_INBOUND='1')
        first = {'CallSid': 'CA1', 'From': '+14155550100', 'To': '+15005550006'}
        response = await self.post_signed('/twilio/inbound', first)
        self.assertIn('<Connect>', await response.text())
        response = await self.post_signed('/twilio/inbound', {**first, 'CallSid': 'CA2'})
        self.assertIn('<Reject reason="busy"/>', await response.text())
        self.assertEqual(len(self.h.service.list()), 1)


class SignalWireTests(unittest.IsolatedAsyncioTestCase):
    SW_ENV = {'OPENAI_API_KEY': 'sk-test', 'SIGNALWIRE_SPACE': 'https://Example.signalwire.com/',
              'SIGNALWIRE_PROJECT_ID': 'p-123', 'SIGNALWIRE_API_TOKEN': 'PT-secret',
              'SIGNALWIRE_SIGNING_KEY': 'PSK-signing', 'SIGNALWIRE_FROM_NUMBER': '+15005550199'}

    def test_credentials_choose_signalwire_when_it_is_set_up(self):
        from call_hooks import MissingCredentials, phone_provider, signalwire_space
        creds = DefaultCallHooks(environ=dict(self.SW_ENV)).credentials('local', 'twilio')
        self.assertEqual(creds['provider'], 'signalwire')
        self.assertEqual(creds['apiBase'], 'https://example.signalwire.com/api/laml/2010-04-01')
        self.assertEqual((creds['accountSid'], creds['fromNumber'], creds['signingKey']),
                         ('p-123', '+15005550199', 'PSK-signing'))
        self.assertEqual(signalwire_space('acme'), 'acme.signalwire.com')
        self.assertEqual(signalwire_space('bad host!'), '')
        # Twilio stays the choice when both are set, unless the setting says otherwise.
        both = {**ENV, **self.SW_ENV}
        self.assertEqual(phone_provider(both), 'twilio')
        self.assertEqual(phone_provider({**both, 'COLLEAGUE_PHONE_PROVIDER': 'signalwire'}), 'signalwire')
        with self.assertRaises(MissingCredentials) as caught:
            DefaultCallHooks(environ={'SIGNALWIRE_PROJECT_ID': 'p-123'}).credentials('local', 'twilio')
        self.assertEqual(caught.exception.missing, (
            'SIGNALWIRE_SPACE', 'SIGNALWIRE_API_TOKEN', 'SIGNALWIRE_FROM_NUMBER or COLLEAGUE_CALLER_ID'))

    async def test_calls_go_to_the_space_with_documented_parameters(self):
        from twilio_client import client_for
        captured = {}

        async def request(method, url, form):
            captured.update(url=url, form=form)
            return 201, {'sid': 'c-1'}
        creds = DefaultCallHooks(environ=dict(self.SW_ENV)).credentials('local', 'twilio')
        client = client_for(creds, request=request)
        await client.create_call(to='+14155550142', from_='+15005550199', twiml='<Response/>',
                                 status_callback='https://x/s', amd_callback='https://x/a',
                                 time_limit=690)
        self.assertEqual(captured['url'],
                         'https://example.signalwire.com/api/laml/2010-04-01/Accounts/p-123/Calls.json')
        form = dict(parse_qsl(captured['form']))
        self.assertEqual(form['AsyncAmdStatusCallback'], 'https://x/a')
        self.assertNotIn('TimeLimit', form)
        self.assertNotIn('AsyncAmdStatusCallbackMethod', form)

    async def test_gateway_accepts_signalwire_signatures(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        h = PhoneHarness(temp.name, env=dict(self.SW_ENV))
        app = create_gateway_app(h.line, h.service, current_url=lambda: PUBLIC)
        client = TestClient(TestServer(app))
        await client.start_server()
        self.addAsyncCleanup(client.close)
        self.addAsyncCleanup(h.service.shutdown)
        record, _session = await h.dial()
        path = f'/twilio/status/{record["id"]}'
        params = {'CallStatus': 'ringing'}
        for key, expected in (('wrong', 403), ('PSK-signing', 204)):
            signature = compute_signature(key, PUBLIC + path, params)
            response = await client.post(path, data=params, headers={'X-SignalWire-Signature': signature})
            self.assertEqual(response.status, expected)
        self.assertEqual(h.store.get(record['id'])['status'], 'ringing')


def fake_live_app(record):
    """A GPT-Live WebSocket speaking the documented event protocol."""
    from aiohttp import web

    async def sessions(request):
        record['auth'] = request.headers.get('Authorization')
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        heard = False
        async for message in ws:
            event = json.loads(message.data)
            kind = event['type']
            record.setdefault('kinds', []).append(kind)
            if kind == 'session.start':
                record['config'] = event['session']
                await ws.send_json({'type': 'session.started', 'session': {'id': 'sess_fake'}})
            elif kind == 'session.input_audio.append' and not heard:
                heard = True
                await ws.send_json({'type': 'session.input_transcript.delta', 'delta': 'Hello?',
                                    'start_ms': 0, 'end_ms': 300})
            elif kind == 'session.commentary.append':
                await ws.send_json({'type': 'session.output_transcript.delta',
                                    'delta': "Hi, I'm an AI assistant calling on behalf of Robin.",
                                    'start_ms': 500, 'end_ms': 2500})
                await ws.send_json({'type': 'session.output_audio.delta',
                                    'delta': base64.b64encode(b'\xff' * 160).decode(),
                                    'start_ms': 500, 'end_ms': 520})
            elif kind == 'session.close':
                await ws.send_json({'type': 'session.closed', 'reason': 'close_requested',
                                    'usage': {'seconds': 7}})
                await ws.close()
        return ws

    app = web.Application()
    app.router.add_get('/v1/live/sessions', sessions)
    return app


class RealSocketTests(unittest.IsolatedAsyncioTestCase):
    """The phone bridge over real aiohttp WebSockets on both sides."""

    async def asyncSetUp(self):
        from voice_core import LiveSession
        self.temp = tempfile.TemporaryDirectory()
        self.record = {}
        self.live_server = TestServer(fake_live_app(self.record))
        await self.live_server.start_server()
        live_url = f'ws://127.0.0.1:{self.live_server.port}/v1/live/sessions'
        self.h = PhoneHarness(self.temp.name)
        self.h.line.live_factory = lambda key, config: LiveSession(key, config, url=live_url)
        self.gateway = TestClient(TestServer(
            create_gateway_app(self.h.line, self.h.service, current_url=lambda: PUBLIC)))
        await self.gateway.start_server()

    async def asyncTearDown(self):
        await self.gateway.close()
        await self.live_server.close()
        await self.h.service.shutdown()
        self.temp.cleanup()

    async def test_call_through_gateway_and_live_sockets(self):
        record, session = await self.h.dial()
        twilio = await self.gateway.ws_connect('/twilio/media')
        await twilio.send_str(json.dumps({'event': 'connected', 'protocol': 'Call'}))
        await twilio.send_str(json.dumps({'event': 'start', 'start': {
            'streamSid': 'MZ9', 'callSid': 'CA123',
            'customParameters': {'callId': record['id'], 'token': session.token}}}))
        await until(lambda: self.record.get('config') is not None)
        await twilio.send_str(json.dumps({'event': 'media', 'streamSid': 'MZ9',
                                          'media': {'track': 'inbound', 'payload': 'f39/f39/'}}))
        media = None
        while media is None:
            message = json.loads((await asyncio.wait_for(twilio.receive(), 5)).data)
            if message['event'] == 'media':
                media = message
        self.assertEqual(media['streamSid'], 'MZ9')
        self.assertEqual(len(base64.b64decode(media['media']['payload'])), 160)
        mark = json.loads((await asyncio.wait_for(twilio.receive(), 5)).data)
        self.assertEqual(mark['event'], 'mark')
        await twilio.send_str(json.dumps({'event': 'mark', 'mark': mark['mark']}))
        await twilio.send_str(json.dumps({'event': 'stop', 'streamSid': 'MZ9'}))
        await twilio.close()
        self.h.line.on_status(record['id'], {'CallStatus': 'completed', 'CallDuration': '12'})
        done = await self.h.service.wait(record['id'], timeout=10)

        self.assertEqual(self.record['auth'], 'Bearer sk-test')
        self.assertEqual(self.record['config']['audio']['format'], {'type': 'audio/pcmu', 'rate': 8000})
        self.assertIn('session.input_audio.append', self.record['kinds'])
        self.assertEqual(self.record['kinds'][-1], 'session.close')
        self.assertEqual(done['status'], 'completed')
        self.assertEqual(done['endReason'], 'remote_hangup')
        self.assertEqual(done['usage']['voiceSeconds'], 7)
        self.assertEqual([line['speaker'] for line in done['result']['transcript']], ['other', 'agent'])
        disclosure = [e for e in self.h.service.events(record['id']) if e['type'] == 'call.disclosure']
        self.assertEqual(disclosure[0]['data'], {'verified': True, 'attempt': 1})


if __name__ == '__main__':
    unittest.main()
