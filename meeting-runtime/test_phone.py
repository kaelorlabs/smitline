import asyncio
from datetime import datetime, timezone
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
from phone_line import OutputPacer, PhoneLine, ProviderClock, UtteranceJoiner, inbound_brief
from phone_prompts import (
    backend_instructions, delegation_config, discloses, mentions_ai, opening_cue, voice_instructions,
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
        return {'sid': call_sid, 'status': self.remote_status,
                'price': getattr(self, 'price', None), 'price_unit': 'USD'}

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
        # 11 AM in San Francisco, where the test numbers ring: inside calling hours.
        self.hooks = DefaultCallHooks(
            environ=self.env, store=self.store,
            clock=lambda: datetime(2026, 9, 30, 18, 0, tzinfo=timezone.utc))

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

    async def test_provider_price_reads_the_finished_call(self):
        self.h.twilio.price = '-0.02100'
        price = await self.h.line.provider_price(lambda kind: {}, {'line': {'providerCallSid': 'CA1'}})
        self.assertEqual(price, {'amount': 0.021, 'currency': 'USD'})
        self.h.twilio.price = None
        self.assertIsNone(await self.h.line.provider_price(lambda kind: {}, {'line': {'providerCallSid': 'CA1'}}))
        self.assertIsNone(await self.h.line.provider_price(lambda kind: {}, {'line': {}}))

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
        self.assertIn("Hi, I'm calling on behalf of Robin about ...", config['instructions'])
        self.assertEqual(self.h.store.get(record['id'])['status'], 'in_progress')

        live.push({'type': 'session.started', 'session': {'id': 'sess_1'}})
        ws.push({'event': 'media', 'media': {'track': 'inbound', 'payload': 'f39/'}})
        live.push({'type': 'session.input_transcript.delta', 'delta': 'Hello?', 'start_ms': 0, 'end_ms': 400})
        await until(lambda: any(kind == 'session.commentary.append' for kind, _ in live.appends))
        self.assertEqual(live.audio, ['f39/'])
        live.push({'type': 'session.output_transcript.delta',
                   'delta': "Hi, I'm an AI assistant calling on behalf of Robin.", 'start_ms': 600, 'end_ms': 2000})
        audio = base64.b64encode(b'\x2a' * 800).decode()
        live.push({'type': 'session.output_audio.delta', 'delta': audio})
        await until(lambda: any(m['event'] == 'mark' for m in ws.sent))
        media = [m for m in ws.sent if m['event'] == 'media']
        self.assertEqual({m['streamSid'] for m in media}, {'MZ1'})
        # Sent as 20 ms frames: 800 bytes of mu-law is five 160-byte frames.
        self.assertEqual(len(media), 5)
        self.assertEqual(b''.join(base64.b64decode(m['media']['payload']) for m in media), b'\x2a' * 800)
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
        self.assertEqual(done['usage']['backendModel'], 'gpt-5.6-terra')
        self.assertEqual(done['usage']['phoneProvider'], 'twilio')
        # The phone line is estimated (one started minute) until Twilio reports its price.
        self.assertEqual(done['cost']['phone'], 0.014)
        self.assertTrue(done['cost']['estimated'])
        self.assertEqual(done['cost']['openai'], round(33 / 60 * 0.05 + (300 * 2 + 20 * 12) / 1e6, 6))
        self.assertEqual(set(done['usage']['audio']), {'maxUnplayedMs', 'interruptionsFollowed',
                                                       'hangupsYielded', 'pauses', 'backchannels',
                                                       'gaps', 'clockRate', 'stretch'})
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

    def test_voicemail_and_guidance_rules_are_in_the_instructions(self):
        text = voice_instructions(CallBrief.from_dict(brief()))
        self.assertIn('reply to Robin directly', text)
        self.assertIn('Never ask them to call this number back', text)
        self.assertIn('picks up while you are leaving the message, stop and talk', text)
        self.assertIn('mailbox is not set up or is full', text)
        self.assertIn('say nothing to it and end the call with the reason other', text)
        self.assertIn('Never read them out', text)

    def test_disclosure_check(self):
        said = [
            ("Hi, I'm an AI assistant calling on behalf of Robin Rao.", 'Robin Rao', True),
            ("Hey Sam, this is Robin's AI assistant. He asked me to call.", 'Robin', True),
            ("Hey, it's Robin's assistant, calling about the launch.", 'Robin', False),
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
        live.push({'type': 'session.output_transcript.delta',
                   'delta': "Hello there, I'm calling on behalf of Robin about a table for tonight.",
                   'start_ms': 0, 'end_ms': 900})
        live.push({'type': 'session.input_transcript.delta', 'delta': 'Okay?', 'start_ms': 1000,
                   'end_ms': 1400})
        live.push({'type': 'session.output_transcript.delta',
                   'delta': 'I would like to book a table for four people tonight at seven.',
                   'start_ms': 1600, 'end_ms': 2500})
        await asyncio.sleep(0.2)
        # Who and why first is the opening, not a miss: the disclosure is due by the second turn.
        self.assertFalse(any(e['type'] == 'call.disclosure' for e in self.h.service.events(record['id'])))
        live.push({'type': 'session.input_transcript.delta', 'delta': 'Who is this?', 'start_ms': 2700,
                   'end_ms': 3100})
        await until(lambda: any(kind == 'session.instructions.append' for kind, _ in live.appends))
        # The next utterance is checked again; a second miss is final.
        live.push({'type': 'session.output_transcript.delta',
                   'delta': 'Sorry about that, I would just like a table for four people tonight.',
                   'start_ms': 3300, 'end_ms': 4200})
        live.push({'type': 'session.input_transcript.delta', 'delta': 'Okay.', 'start_ms': 4400,
                   'end_ms': 4800})
        await until(lambda: sum(e['type'] == 'call.disclosure'
                                for e in self.h.service.events(record['id'])) == 2)
        ws.push({'event': 'stop'})
        await asyncio.wait_for(task, 3)
        done = await self.h.service.wait(record['id'], timeout=5)
        self.assertEqual(done['endReason'], 'remote_hangup')
        self.assertTrue(live.close_requested)
        self.assertIs(done['result']['disclosureVerified'], False)

    async def test_the_disclosure_in_the_second_turn_needs_no_reminder(self):
        record, session = await self.h.dial()
        ws, live, task = await self.h.connect(record, session)
        live.push({'type': 'session.started', 'session': {}})
        live.push({'type': 'session.output_transcript.delta',
                   'delta': "Hi, I'm calling on behalf of Robin about a table for four tonight.",
                   'start_ms': 0, 'end_ms': 2000})
        live.push({'type': 'session.input_transcript.delta', 'delta': 'Sure, go ahead.', 'start_ms': 2200,
                   'end_ms': 2800})
        live.push({'type': 'session.output_transcript.delta',
                   'delta': "I'm Robin's AI assistant. Do you have a table for four at seven?",
                   'start_ms': 3000, 'end_ms': 5000})
        await until(lambda: any(e['type'] == 'call.disclosure' for e in self.h.service.events(record['id'])))
        disclosure = [e['data'] for e in self.h.service.events(record['id']) if e['type'] == 'call.disclosure']
        self.assertEqual(disclosure, [{'verified': True, 'attempt': 1}])
        self.assertFalse(any(kind == 'session.instructions.append' for kind, _ in live.appends))
        ws.push({'event': 'stop'})
        await asyncio.wait_for(task, 3)
        done = await self.h.service.wait(record['id'], timeout=5)
        self.assertIs(done['result']['disclosureVerified'], True)

    async def test_a_call_ended_after_the_opening_has_no_disclosure_verdict(self):
        record, session = await self.h.dial()
        ws, live, task = await self.h.connect(record, session)
        live.push({'type': 'session.started', 'session': {}})
        live.push({'type': 'session.output_transcript.delta',
                   'delta': "Hi, I'm calling on behalf of Robin about a table for four tonight.",
                   'start_ms': 0, 'end_ms': 2000})
        await asyncio.sleep(0.2)
        ws.push({'event': 'stop'})
        await asyncio.wait_for(task, 3)
        done = await self.h.service.wait(record['id'], timeout=5)
        self.assertFalse(any(e['type'] == 'call.disclosure' for e in self.h.service.events(record['id'])))
        self.assertIsNone(done['result'].get('disclosureVerified'))

    async def test_the_session_gets_the_three_levels_of_context(self):
        from briefing import save_profile
        self.h.hooks.env_file = Path(self.temp.name) / '.env'
        save_profile(Path(self.temp.name) / '.colleague' / 'profile.json', {
            'about': 'Robin builds Smitline.', 'boundaries': ['Never discuss money.'],
            'people': [{'name': 'Sam', 'relationship': 'close friend', 'phone': '+14155550143'}]})
        record, session = await self.h.dial(
            to='+14155550143', objective='Ask Sam whether to launch now or wait',
            questions=['Launch now or wait, and why?'],
            context={'summary': 'Smitline lets agents make calls.',
                     'details': 'Pricing: about 6 cents a minute.'})
        ws, live, task = await self.h.connect(record, session)
        config = live.config
        notes = config['input'][0]['content'][0]['text']
        self.assertIn('Smitline lets agents make calls.', notes)
        self.assertIn("Speaking with: Sam, Robin's close friend", notes)
        self.assertNotIn('Pricing', notes)
        instructions = config['instructions']
        self.assertIn("You are calling Sam, Robin's close friend. Greet them by name", instructions)
        self.assertIn('"Hey Sam, I\'m calling on behalf of Robin about ..." with the reason in place of '
                      'the dots. Then stop and let them answer.', instructions)
        self.assertIn('Find out:\n- Launch now or wait, and why?', instructions)
        self.assertIn('your context holds reference notes', instructions)
        self.assertIn('Robin always wants these kept, on every call:\n- Never discuss money.', instructions)
        self.assertNotIn('Pricing', instructions)
        backend = config['delegation']['responses']['instructions']
        self.assertIn('Pricing: about 6 cents a minute.', backend)
        self.assertIn('- Never discuss money.', backend)
        # A note from the agent mid-call is silent context, not an instruction.
        live.push({'type': 'session.started', 'session': {}})
        self.assertEqual(await self.h.service.instruct(record['id'], 'He tried it yesterday.', silent=True),
                         {'delivered': True})
        self.assertIn(('session.thinking.append', 'He tried it yesterday.'), live.appends)
        ws.push({'event': 'stop'})
        await asyncio.wait_for(task, 3)

    async def test_without_background_the_voice_gets_no_reference_notes(self):
        record, session = await self.h.dial()
        ws, live, task = await self.h.connect(record, session)
        self.assertNotIn('input', live.config)
        self.assertNotIn('reference notes', live.config['instructions'])
        ws.push({'event': 'stop'})
        await asyncio.wait_for(task, 3)

    async def test_a_screener_splitting_the_opening_is_not_a_missed_disclosure(self):
        record, session = await self.h.dial()
        ws, live, task = await self.h.connect(record, session)
        live.push({'type': 'session.started', 'session': {}})
        # An iPhone call screener talks over the first word of the opening.
        live.push({'type': 'session.input_transcript.delta', 'delta': "I'll see if this person is",
                   'start_ms': 0, 'end_ms': 900})
        live.push({'type': 'session.output_transcript.delta', 'delta': 'Hi,', 'start_ms': 950, 'end_ms': 1100})
        live.push({'type': 'session.input_transcript.delta', 'delta': 'available', 'start_ms': 1150,
                   'end_ms': 1500})
        live.push({'type': 'session.output_transcript.delta',
                   'delta': "I'm an AI assistant calling on behalf of Robin.", 'start_ms': 1600, 'end_ms': 3500})
        await until(lambda: any(e['type'] == 'call.disclosure' for e in self.h.service.events(record['id'])))
        disclosure = [e['data'] for e in self.h.service.events(record['id']) if e['type'] == 'call.disclosure']
        self.assertEqual(disclosure, [{'verified': True, 'attempt': 1}])
        self.assertFalse(any(kind == 'session.instructions.append' for kind, _ in live.appends))
        ws.push({'event': 'stop'})
        await asyncio.wait_for(task, 3)

    async def test_the_machine_verdict_is_a_hint_not_the_end(self):
        record, session = await self.h.dial()
        ws, live, task = await self.h.connect(record, session)
        live.push({'type': 'session.started', 'session': {}})
        # Carriers call screeners "machine"; the model hears a person and carries on.
        self.h.line.on_amd(record['id'], 'machine_end_silence')
        await until(lambda: any('guesses that a machine' in text for _kind, text in live.appends))
        # Sent as silent context, so the model does not answer it out loud.
        self.assertEqual(next(kind for kind, text in live.appends if 'guesses that a machine' in text),
                         'session.thinking.append')
        self.assertIsNone(session.end_reason)
        live.push({'type': 'response.event', 'delegation_id': 'd1', 'event': {
            'type': 'response.output_item.done', 'item': {
                'type': 'function_call', 'call_id': 'fc1', 'name': 'end_call',
                'arguments': '{"reason": "completed"}'}}})
        await until(lambda: ws.closed)
        await asyncio.wait_for(task, 3)
        done = await self.h.service.wait(record['id'], timeout=5)
        self.assertEqual(done['endReason'], 'hangup')

    async def test_leaving_a_voicemail_is_reported_as_voicemail(self):
        record, session = await self.h.dial()
        ws, live, task = await self.h.connect(record, session)
        live.push({'type': 'session.started', 'session': {}})
        live.push({'type': 'response.event', 'delegation_id': 'd1', 'event': {
            'type': 'response.output_item.done', 'item': {
                'type': 'function_call', 'call_id': 'fc1', 'name': 'end_call',
                'arguments': '{"reason": "voicemail_left"}'}}})
        await until(lambda: ws.closed)
        await asyncio.wait_for(task, 3)
        done = await self.h.service.wait(record['id'], timeout=5)
        self.assertEqual(done['endReason'], 'voicemail')

    async def test_hangup_gives_way_when_they_keep_talking(self):
        record, session = await self.h.dial()
        ws, live, task = await self.h.connect(record, session)
        live.push({'type': 'session.started', 'session': {}})
        live.push({'type': 'session.output_audio.delta', 'delta': base64.b64encode(b'\x2a' * 800).decode()})
        await until(lambda: any(m['event'] == 'mark' for m in ws.sent))
        live.push({'type': 'response.event', 'delegation_id': 'd1', 'event': {
            'type': 'response.output_item.done', 'item': {
                'type': 'function_call', 'call_id': 'fc1', 'name': 'end_call',
                'arguments': '{"reason": "completed"}'}}})
        await until(lambda: session._hanging_up)
        # They answer the goodbye before it has finished playing.
        live.push({'type': 'session.input_transcript.delta', 'delta': 'Wait, one more thing',
                   'start_ms': 5000, 'end_ms': 5800})
        await until(lambda: session._other_spoke_at is not None)
        mark = next(m for m in ws.sent if m['event'] == 'mark')
        ws.push({'event': 'mark', 'mark': mark['mark']})
        await until(lambda: any(e['type'] == 'call.hangup_yielded' for e in self.h.service.events(record['id'])))
        self.assertFalse(ws.closed)
        self.assertIsNone(session.end_reason)
        self.assertTrue(any('spoke after you said goodbye' in text for _kind, text in live.appends))
        # The next goodbye, with nobody talking over it, ends the call.
        live.push({'type': 'response.event', 'delegation_id': 'd1', 'event': {
            'type': 'response.output_item.done', 'item': {
                'type': 'function_call', 'call_id': 'fc2', 'name': 'end_call',
                'arguments': '{"reason": "completed"}'}}})
        await until(lambda: ws.closed)
        await asyncio.wait_for(task, 3)
        done = await self.h.service.wait(record['id'], timeout=5)
        self.assertEqual(done['endReason'], 'hangup')
        self.assertEqual(done['usage']['audio']['hangupsYielded'], 1)

    async def test_an_interruption_drops_speech_the_model_abandoned(self):
        record, session = await self.h.dial()
        ws, live, task = await self.h.connect(record, session)
        live.push({'type': 'session.started', 'session': {}})
        # Two seconds of speech arrive at once; only the first 0.3 s go out right away.
        live.push({'type': 'session.output_audio.delta', 'delta': base64.b64encode(b'\x2a' * 16000).decode()})
        await until(lambda: session.pacer.unplayed_seconds() > 1.0)
        # A short "mhm" is a backchannel, not an interruption.
        live.push({'type': 'session.input_transcript.delta', 'delta': 'Mhm', 'start_ms': 0, 'end_ms': 250})
        await asyncio.sleep(0.5)
        self.assertFalse(any(m['event'] == 'clear' for m in ws.sent))
        # Talking over it for longer, with no new speech from the model, stops playback.
        live.push({'type': 'session.input_transcript.delta', 'delta': 'Actually hold on, I',
                   'start_ms': 1000, 'end_ms': 1800})
        await until(lambda: any(m['event'] == 'clear' for m in ws.sent))
        self.assertLess(session.pacer.unplayed_seconds(), 0.1)
        ws.push({'event': 'stop'})
        await asyncio.wait_for(task, 3)
        done = await self.h.service.wait(record['id'], timeout=5)
        audio = done['usage']['audio']
        self.assertEqual(audio['interruptionsFollowed'], 1)
        self.assertGreaterEqual(audio['maxUnplayedMs'], 1900)

    async def test_speaking_over_it_stops_playback_at_once(self):
        from test_barge_in import QUIET, SPEECH
        quiet = base64.b64encode(QUIET).decode()
        speech = base64.b64encode(SPEECH).decode()
        record, session = await self.h.dial()
        ws, live, task = await self.h.connect(record, session)
        live.push({'type': 'session.started', 'session': {}})
        for _ in range(25):
            ws.push({'event': 'media', 'media': {'track': 'inbound', 'payload': quiet}})
        live.push({'type': 'session.output_audio.delta', 'delta': base64.b64encode(b'\x2a' * 24000).decode()})
        await until(lambda: session.pacer.unplayed_seconds() > 2.0)

        def clears():
            return sum(1 for m in ws.sent if m['event'] == 'clear')
        # "Mhm" (200 ms): playback pauses after 160 ms, then carries on where it paused.
        for _ in range(10):
            ws.push({'event': 'media', 'media': {'track': 'inbound', 'payload': speech}})
        await until(lambda: session.pacer.paused)
        self.assertEqual(clears(), 1)
        unheard = session.pacer.unplayed_seconds()
        for _ in range(25):
            ws.push({'event': 'media', 'media': {'track': 'inbound', 'payload': quiet}})
        await until(lambda: not session.pacer.paused)
        self.assertGreater(unheard, 2.0)
        # Talking over it: paused within 160 ms, and after 700 ms the rest is dropped.
        for _ in range(45):
            ws.push({'event': 'media', 'media': {'track': 'inbound', 'payload': speech}})
        await until(lambda: session._barge == 'dropped')
        self.assertEqual(clears(), 2)
        self.assertEqual(session.pacer.unplayed_seconds(), 0)
        # The abandoned rest of that answer, still arriving, is not played.
        live.push({'type': 'session.output_audio.delta', 'delta': base64.b64encode(b'\x2a' * 800).decode()})
        await asyncio.sleep(0.05)
        self.assertEqual(session.pacer.unplayed_seconds(), 0)
        # They stop; the reply delay runs from the end of their turn to the next speech.
        for _ in range(25):
            ws.push({'event': 'media', 'media': {'track': 'inbound', 'payload': quiet}})
        await until(lambda: session._barge is None)
        session._dropped_at -= 5
        live.push({'type': 'session.output_audio.delta', 'delta': base64.b64encode(b'\x2a' * 800).decode()})
        await until(lambda: session.pacer.unplayed_seconds() > 0)
        ws.push({'event': 'stop'})
        await asyncio.wait_for(task, 3)
        audio = (await self.h.service.wait(record['id'], timeout=5))['usage']['audio']
        self.assertEqual((audio['pauses'], audio['backchannels'], audio['interruptionsFollowed']), (2, 1, 1))
        self.assertEqual(audio['stopDelayMs'], {'median': 160, 'max': 160, 'count': 2})
        self.assertEqual(audio['replyDelayMs']['count'], 1)
        self.assertGreaterEqual(audio['replyDelayMs']['median'], 450)  # from their last word

    async def test_audio_trace_is_written_when_asked(self):
        self.h.env['COLLEAGUE_AUDIO_TRACE'] = '1'
        record, session = await self.h.dial()
        ws, live, task = await self.h.connect(record, session)
        live.push({'type': 'session.started', 'session': {}})
        live.push({'type': 'session.output_audio.delta', 'delta': base64.b64encode(b'\x2a' * 1600).decode()})
        ws.push({'event': 'media', 'sequenceNumber': '3', 'media': {'track': 'inbound', 'payload': 'f39/', 'timestamp': '60'}})
        await until(lambda: session.pacer.sent_marks == 1)
        ws.push({'event': 'stop'})
        await asyncio.wait_for(task, 3)
        path = self.h.store.root / record['id'] / 'audio-trace.jsonl'
        kinds = [json.loads(line)['kind'] for line in path.read_text().splitlines()]
        self.assertIn('delta', kinds)
        self.assertEqual(kinds.count('send'), 10)
        self.assertIn('mark', kinds)
        self.assertIn({'kind': 'in', 'ts': '60', 'seq': '3'},
                      [{k: v for k, v in json.loads(line).items() if k != 't'} for line in path.read_text().splitlines()])
        self.assertEqual(oct(path.stat().st_mode & 0o777), '0o600')

    async def test_a_call_screener_is_heard_out_before_the_opening(self):
        from test_barge_in import QUIET, SPEECH
        record, session = await self.h.dial()
        ws, live, task = await self.h.connect(record, session)
        live.push({'type': 'session.started', 'session': {}})

        def say(frames):
            for frame in frames:
                ws.push({'event': 'media', 'media': {'track': 'inbound',
                                                     'payload': base64.b64encode(frame).decode()}})
        # "Hi, the person you are calling is using a screening service" (2 s), a 0.6 s pause,
        # "go ahead and say your name" (1 s), then quiet.
        say([QUIET] * 5 + [SPEECH] * 100 + [QUIET] * 30 + [SPEECH] * 50)
        await asyncio.sleep(1.0)
        self.assertFalse(any(kind == 'session.commentary.append' for kind, _ in live.appends))
        say([QUIET] * 100)
        await until(lambda: any(kind == 'session.commentary.append' for kind, _ in live.appends), timeout=3)
        ws.push({'event': 'stop'})
        await asyncio.wait_for(task, 3)

    async def test_the_opening_follows_a_hello_heard_locally(self):
        from test_barge_in import QUIET, SPEECH
        record, session = await self.h.dial()
        ws, live, task = await self.h.connect(record, session)
        live.push({'type': 'session.started', 'session': {}})
        for frame in [QUIET] * 5 + [SPEECH] * 20 + [QUIET] * 25:
            ws.push({'event': 'media', 'media': {'track': 'inbound', 'payload': base64.b64encode(frame).decode()}})
        # Well before the 1.5 s fallback, once their hello has ended.
        await until(lambda: any(kind == 'session.commentary.append' for kind, _ in live.appends), timeout=1.2)
        ws.push({'event': 'stop'})
        await asyncio.wait_for(task, 3)

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
        self.assertIn("Hi, I'm calling on behalf of Robin about ...", text)
        self.assertIn("In your next turn, before you ask for anything, say plainly that you are "
                      "Robin's AI assistant", text)
        self.assertNotIn('call is recorded', text)
        self.assertIn('Also say in your first sentence that the call is recorded.',
                      voice_instructions(parsed, recording=True))
        self.assertIn('- 6:30 to 7:30pm', text)
        self.assertIn('- card number', text)
        self.assertIn('language with tag es', text)
        self.assertIn('rehearsal', voice_instructions(CallBrief.from_dict(brief(rehearsal=True))))
        self.assertIn("reached Robin's AI assistant", voice_instructions(parsed, inbound=True))
        config = delegation_config(parsed, model='gpt-5.6-luna', web_search=True)
        self.assertEqual([tool.get('name', tool['type']) for tool in config['responses']['tools']],
                         ['end_call', 'web_search'])
        self.assertEqual(config['responses']['model'], 'gpt-5.6-luna')

    def test_the_opening_cue_says_who_and_why_and_the_ai_part_follows(self):
        parsed = CallBrief.from_dict(brief())
        cue = opening_cue(parsed, name='Sam')
        self.assertIn('"Hey Sam, I\'m calling on behalf of Robin about ..."', cue)
        self.assertIn("In your next turn, say that you are Robin's AI assistant", cue)
        self.assertNotIn('recorded', cue)
        self.assertIn('what about, and that the call is recorded,', opening_cue(parsed, recording=True))
        self.assertEqual(opening_cue(parsed, inbound=True, recording=True),
                         "Greet the caller: say they have reached Robin's AI assistant. "
                         'Say that the call is recorded.')

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
        chunk = base64.b64encode(b'\x2a' * 4000).decode()  # 0.5 s of mu-law
        for _ in range(3):
            pacer.offer(chunk)
        runner = asyncio.create_task(pacer.run())
        # 1.5 s of audio goes out as 75 frames of 20 ms, and one mark when it stops.
        await until(lambda: len(sent) == 76)
        runner.cancel()
        self.assertEqual([m['mark']['name'] for m in sent if m['event'] == 'mark'], ['out-1'])
        self.assertEqual(sent[-1]['event'], 'mark')
        self.assertLessEqual(max(ahead), pacer.lead + 0.02 + 1e-9)
        self.assertAlmostEqual(sum(slept), 1.5 - pacer.lead - 0.02, delta=0.021)
        self.assertFalse(await pacer.drained(0.01))
        pacer.mark_played('out-1')
        self.assertTrue(await pacer.drained(0.01))

    async def test_speech_starts_with_a_cushion(self):
        sent = []

        async def send(message):
            sent.append((asyncio.get_running_loop().time(), message))
        pacer = OutputPacer(send, cushion=0.1)
        runner = asyncio.create_task(pacer.run())
        loop = asyncio.get_running_loop()
        started = loop.time()
        pacer.offer(base64.b64encode(b'' * 320).decode())  # 40 ms: not enough yet
        await asyncio.sleep(0.03)
        self.assertEqual(sent, [])
        pacer.offer(base64.b64encode(b'' * 640).decode())  # now 120 ms: start at once
        await until(lambda: len([m for _, m in sent if m['event'] == 'media']) == 6)
        self.assertLess(sent[0][0] - started, 0.08)
        # A lone short delta still plays, after at most the cushion.
        pacer.flush()
        sent.clear()
        started = loop.time()
        pacer.offer(base64.b64encode(b'' * 160).decode())
        await until(lambda: sent)
        self.assertGreaterEqual(sent[0][0] - started, 0.09)
        runner.cancel()

    async def test_pauses_stretch_to_keep_a_reserve_and_shrink_when_too_far_ahead(self):
        now = [0.0]

        async def sleep(seconds):
            now[0] += seconds
        sent = []

        async def send(message):
            sent.append(message)
        voice, quiet = bytes([0x2a]), bytes([0xff])  # mu-law: loud, and silence
        pacer = OutputPacer(send, clock=lambda: now[0], sleep=sleep, cushion=0, target=0.3, most=1.2)
        runner = asyncio.create_task(pacer.run())
        # A short pause below the target: its silent frame is sent again until 0.3 s is buffered.
        pacer.offer(base64.b64encode(voice * 160 + quiet * 160).decode())
        await until(lambda: pacer.play_until - now[0] >= 0.3)
        media = [m for m in sent if m['event'] == 'media']
        self.assertEqual(len(media), 15)  # 20 ms of speech and 280 ms of pause
        self.assertEqual(pacer.stretch['addedMs'], 260)
        # Far ahead (over 1.2 s waiting): silent frames are dropped, speech never is.
        now[0] += 0.3
        sent.clear()
        pacer.offer(base64.b64encode(voice * 12000 + quiet * 4000 + voice * 4000).decode())  # speech, pause, speech
        await until(lambda: not pacer.queue and not pacer._next)
        self.assertGreater(pacer.stretch['trimmedMs'], 0)
        voiced = [m for m in sent if m['event'] == 'media'
                  and base64.b64decode(m['media']['payload'])[:1] == voice]
        self.assertEqual(len(voiced) * 20, 2000)  # all of the speech
        runner.cancel()

    async def test_marks_go_out_only_in_silence_or_when_audio_stops(self):
        sent = []

        async def send(message):
            sent.append(message)
        pacer = OutputPacer(send, cushion=0, target=0)
        runner = asyncio.create_task(pacer.run())
        pacer.offer(base64.b64encode(bytes([0x2a]) * 160).decode())  # speech ending the stream
        await until(lambda: sent and sent[-1]['event'] == 'mark')  # after it stops arriving
        self.assertEqual([m['event'] for m in sent], ['media', 'mark'])
        runner.cancel()

    async def test_odd_tails_join_the_next_delta_or_are_padded(self):
        sent = []

        async def send(message):
            sent.append(message)
        pacer = OutputPacer(send)
        runner = asyncio.create_task(pacer.run())
        # 100 bytes, then 300: one whole frame of the joined audio, then a 80-byte tail.
        pacer.offer(base64.b64encode(b'\x01' * 100).decode())
        pacer.offer(base64.b64encode(b'\x02' * 300).decode())
        await until(lambda: len([m for m in sent if m['event'] == 'media']) == 3)
        frames = [base64.b64decode(m['media']['payload']) for m in sent if m['event'] == 'media']
        self.assertEqual([len(frame) for frame in frames], [160, 160, 160])
        self.assertEqual(frames[0], b'\x01' * 100 + b'\x02' * 60)
        self.assertEqual(frames[2], b'\x02' * 80 + b'\xff' * 80)  # padded once speech stopped
        await until(lambda: sent[-1]['event'] == 'mark')
        self.assertEqual(sum(1 for m in sent if m['event'] == 'mark'), 1)
        runner.cancel()

    def test_provider_clock_follows_the_provider_not_this_computer(self):
        ours = [100.0]
        clock = ProviderClock(clock=lambda: ours[0])
        self.assertEqual(clock(), 100.0)  # no stamps yet: this computer's clock
        # This computer's clock runs 8% slow: 20 ms frames arrive every 18.5 ms of ours,
        # and one in ten arrives 40 ms late.
        for frame in range(500):
            ours[0] = 100.0 + frame * 0.02 / 1.08 + (0.04 if frame % 10 == 3 else 0.0)
            clock.observe(str(frame * 20))
        self.assertAlmostEqual(clock.rate, 1.08, delta=0.01)
        ours[0] = 100.0 + 500 * 0.02 / 1.08
        self.assertAlmostEqual(clock(), 10.0, delta=0.005)  # their time, from on-time frames
        clock.observe('garbage')  # ignored
        self.assertAlmostEqual(clock.rate, 1.08, delta=0.01)

    async def test_pacing_follows_a_faster_provider_clock(self):
        ours = [0.0]
        provider = ProviderClock(clock=lambda: ours[0])
        for frame in range(200):  # the provider's clock runs 8% faster than ours
            ours[0] = frame * 0.02 / 1.08
            provider.observe(str(frame * 20))
        slept = []

        async def sleep(seconds):
            slept.append(seconds)
            ours[0] += seconds
        sent = []

        async def send(message):
            sent.append(message)
        pacer = OutputPacer(send, clock=provider, sleep=sleep, cushion=0)
        pacer.offer(base64.b64encode(b'\x2a' * 16000).decode())  # 2 s of speech
        runner = asyncio.create_task(pacer.run())
        await until(lambda: len([m for m in sent if m['event'] == 'media']) == 100)
        runner.cancel()
        # 2 s of the provider's time pass in 2 / 1.08 s of ours, less the lead.
        self.assertAlmostEqual(sum(slept), (2.0 - pacer.lead - 0.02) / 1.08, delta=0.03)

    async def test_pause_takes_back_unheard_speech_and_resume_carries_on(self):
        now = [0.0]
        sent = []
        gate = asyncio.Event()

        async def sleep(seconds):
            await gate.wait()  # time stands still until the test lets it move
            now[0] += seconds

        async def send(message):
            sent.append(message)
        pacer = OutputPacer(send, clock=lambda: now[0], sleep=sleep, lead=0.1)
        frames = [bytes([index]) * 160 for index in range(10)]  # 0.2 s in 10 distinct frames
        pacer.offer(base64.b64encode(b''.join(frames)).decode())
        runner = asyncio.create_task(pacer.run())
        await until(lambda: len([m for m in sent if m['event'] == 'media']) == 6)
        # 0.12 s have gone out and nothing has played yet; pause takes all of it back.
        pacer.pause()
        self.assertTrue(pacer.paused)
        self.assertAlmostEqual(pacer.unplayed_seconds(), 0.2)
        self.assertFalse(pacer.playing())
        await asyncio.sleep(0.05)
        self.assertEqual(len([m for m in sent if m['event'] == 'media']), 6)
        pacer.resume()
        gate.set()
        await until(lambda: len([m for m in sent if m['event'] == 'media']) == 16)
        media = [m['media']['payload'] for m in sent if m['event'] == 'media']
        # After the pause the whole of it plays again, from the first unheard frame, in order.
        self.assertEqual([base64.b64decode(m) for m in media[6:]], frames)
        pacer.flush()
        self.assertEqual(pacer.unplayed_seconds(), 0)
        self.assertTrue(await pacer.drained(0.01))
        runner.cancel()

    async def test_a_late_frame_mid_speech_counts_as_a_gap(self):
        now = [0.0]

        async def sleep(seconds):
            now[0] += seconds

        async def send(message):
            pass
        pacer = OutputPacer(send, clock=lambda: now[0], sleep=sleep)
        runner = asyncio.create_task(pacer.run())
        pacer.offer(base64.b64encode(b'\x2a' * 160).decode())
        await until(lambda: pacer.play_until > 0)
        now[0] += 0.1  # the model's next audio arrives 80 ms after the first frame played out
        pacer.offer(base64.b64encode(b'\x2a' * 160).decode())
        await until(lambda: pacer.gaps['count'] == 1)
        self.assertEqual(pacer.gaps, {'count': 1, 'totalMs': 80, 'starved': 1, 'audible': 1})
        now[0] += 5.0  # a pause between turns is not a gap
        pacer.offer(base64.b64encode(b'\x2a' * 160).decode())
        await asyncio.sleep(0.05)
        self.assertEqual(pacer.gaps['count'], 1)
        runner.cancel()

    def test_joiner(self):
        joiner = UtteranceJoiner(gap_ms=1000)
        self.assertEqual(joiner.add('other', 'Hel', 0, 200), [])
        self.assertEqual(joiner.add('other', 'lo?', 200, 400), [])
        self.assertEqual(joiner.add('agent', 'Hi ', 500, 700), [('other', 'Hello?')])
        self.assertEqual(joiner.add('agent', 'there', 3000, 3200), [('agent', 'Hi')])
        self.assertEqual(joiner.flush(), [('agent', 'there')])
        # Fragments from separate turns keep a space between sentences.
        joiner.add('agent', 'Let me check.', 0, 100)
        joiner.add('agent', 'I can place calls.', 150, 300)
        self.assertEqual(joiner.flush(), [('agent', 'Let me check. I can place calls.')])


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
        self.assertIn('<Stream url="wss://x" realtime="true">', stream_twiml('wss://x', {}, realtime=True))
        self.assertNotIn('realtime', stream_twiml('wss://x', {}))
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
        self.assertIn(('RecordingChannels', 'dual'), parse_qsl(captured['form']))
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

        async def probe(url, timeout, problems=None):
            probed.append((url, timeout))
            return True
        tunnel = PublicUrl(lambda: {}, 8766, spawn=spawn, which=cloudflared, probe=probe)
        self.assertIsNone(tunnel.current())
        self.assertEqual(await tunnel.get(), 'https://brave-fox-12.trycloudflare.com')
        self.assertEqual(await tunnel.get(), 'https://brave-fox-12.trycloudflare.com')
        self.assertEqual(tunnel.current(), 'https://brave-fox-12.trycloudflare.com')
        self.assertEqual(len(spawned), 1)
        self.assertEqual(spawned[0][-1], 'http://127.0.0.1:8766')
        # Reuse checks the running tunnel again, briefly.
        self.assertEqual(probed, [('https://brave-fox-12.trycloudflare.com/healthz', 45.0),
                                  ('https://brave-fox-12.trycloudflare.com/healthz', 6.0)])
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

        async def unreachable(url, timeout, problems=None):
            problems.append('ClientConnectorDNSError')
            return False
        logged = []
        spawned.clear()
        with self.assertRaises(TunnelError) as caught:
            await PublicUrl(lambda: {}, 8766, spawn=spawn, which=cloudflared, probe=unreachable,
                            log=lambda line, **_: logged.append(line)).get()
        # It tries a fresh tunnel once more, and says why neither worked.
        self.assertEqual(len(spawned), 2)
        self.assertEqual(len(logged), 2)
        self.assertIn('ClientConnectorDNSError', str(caught.exception))
        docker = PublicUrl(lambda: {}, 8766, spawn=spawn, probe=probe,
                           which=lambda name: '/usr/bin/docker' if name == 'docker' else None)
        self.assertEqual(docker.command()[:4], ['docker', 'run', '--rm', '--network'])
        self.assertIn('smitline-tunnel-8766', docker.command())
        spawned.clear()
        self.assertEqual(await docker.get(), 'https://brave-fox-12.trycloudflare.com')
        self.assertEqual(spawned[0][:3], ('docker', 'rm', '-f'))
        await docker.close()
        self.assertFalse(PublicUrl(lambda: {}, 8766, which=lambda name: None).available())

    async def test_a_tunnel_that_stopped_answering_is_replaced_before_reuse(self):
        names = iter(['brave-fox-12', 'calm-owl-34'])

        class Stream:
            def __init__(self, lines):
                self.lines = list(lines)

            async def readline(self):
                return self.lines.pop(0) if self.lines else b''

        class Process:
            def __init__(self):
                name = next(names)
                self.returncode = None
                self.stderr = Stream([f'INF |  https://{name}.trycloudflare.com  |\n'.encode(),
                                      b'INF Registered tunnel connection connIndex=0\n'])

            def terminate(self):
                self.returncode = 0

            async def wait(self):
                return 0
        processes = []

        async def spawn(*command, **kwargs):
            processes.append(Process())
            return processes[-1]
        # The first tunnel answers once, then dies (the computer slept); the second answers.
        answers = {'https://brave-fox-12.trycloudflare.com/healthz': [True, False],
                   'https://calm-owl-34.trycloudflare.com/healthz': [True]}

        async def probe(url, timeout, problems=None):
            ok = answers[url].pop(0)
            if not ok:
                problems.append('HTTP 530')
            return ok
        logged = []
        tunnel = PublicUrl(lambda: {}, 8766, spawn=spawn, which=lambda name: '/usr/bin/cloudflared',
                           probe=probe, log=lambda line, **_: logged.append(line))
        self.assertEqual(await tunnel.get(), 'https://brave-fox-12.trycloudflare.com')
        self.assertEqual(await tunnel.get(), 'https://calm-owl-34.trycloudflare.com')
        self.assertEqual(len(processes), 2)
        self.assertEqual(processes[0].returncode, 0)  # the stale cloudflared was stopped
        self.assertIn('stopped answering (HTTP 530); starting a fresh one', logged[1])
        await tunnel.close()


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

    async def test_a_call_is_recorded_on_request_and_downloaded(self):
        record, _session = await self.h.dial(record=True)
        created = self.h.twilio.created[-1]
        self.assertTrue(created['record'])
        self.assertEqual(created['recording_callback'], f'{PUBLIC}/twilio/recording/{record["id"]}')
        # Not asked for and not turned on for every call: no recording.
        other, _ = await self.h.dial(to='+14155550199')
        self.assertFalse(self.h.twilio.created[-1]['record'])
        with self.assertRaises(CallError) as caught:
            await self.h.service.recording(record['id'])
        self.assertEqual(caught.exception.code, 'not_ready')
        await self.post_signed(f'/twilio/recording/{record["id"]}', {
            'RecordingSid': 'RE1', 'RecordingStatus': 'completed', 'RecordingDuration': '42',
            'RecordingUrl': 'https://api.twilio.com/2010-04-01/Accounts/AC1/Recordings/RE1'})
        fetched = []

        async def download(url):
            fetched.append(url)
            return b'RIFF....WAVE', 'audio/x-wav'
        self.h.twilio.download = download
        body, content_type = await self.h.service.recording(record['id'])
        self.assertEqual((body, content_type), (b'RIFF....WAVE', 'audio/x-wav'))
        await self.h.service.recording(record['id'], fmt='mp3')
        self.assertEqual(fetched, ['https://api.twilio.com/2010-04-01/Accounts/AC1/Recordings/RE1.wav',
                                   'https://api.twilio.com/2010-04-01/Accounts/AC1/Recordings/RE1.mp3'])
        with self.assertRaises(CallError) as caught:
            await self.h.service.recording(record['id'], fmt='flac')
        self.assertEqual(caught.exception.status, 422)
        # Credentials never go to a host other than the provider's API.
        self.h.store.update(record['id'], recording={'sid': 'RE1', 'url': 'https://evil.example/Recordings/RE1.mp3'})
        with self.assertRaises(CallError) as caught:
            await self.h.service.recording(record['id'])
        self.assertEqual(caught.exception.code, 'not_ready')
        self.assertEqual(len(fetched), 2)
        # Direct SIP calls cannot be recorded yet, so asking is refused before dialing.
        self.h.env.update(COLLEAGUE_PHONE_AUDIO='sip', COLLEAGUE_SIP_TRUNK_URL='sips:t.example',
                          COLLEAGUE_SIP_USERNAME='u', COLLEAGUE_SIP_PASSWORD='p')
        with self.assertRaises(CallError) as caught:
            await self.h.service.create(brief(record=True, to='+14155550123'))
        self.assertEqual(caught.exception.code, 'recording_unavailable')

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


class FakeSipClient:
    def __init__(self, *, refuse=None):
        self.calls = []
        self.refuse = refuse

    async def create_outbound(self, session, *, destination, trunk):
        self.calls.append(('create', session, destination, trunk))
        if self.refuse:
            from live_sip import LiveSipError
            raise LiveSipError(403, self.refuse, 'not enabled')
        return 'live_sip_1'

    async def accept(self, session_id, session):
        self.calls.append(('accept', session_id, session))

    async def reject(self, session_id, status_code=486):
        self.calls.append(('reject', session_id, status_code))

    async def hangup(self, session_id):
        self.calls.append(('hangup', session_id))
        for side in FakeSideband.instances:
            if side.session_id == session_id:
                side.push({'type': 'session.closed', 'reason': 'close_requested', 'usage': {'seconds': 21}})

    async def refer(self, session_id, target_uri):
        self.calls.append(('refer', session_id, target_uri))


class FakeSideband(FakeLive):
    instances = []

    def __init__(self, key, session_id):
        super().__init__(key, {})
        self.session_id = session_id
        self.started.set()
        FakeSideband.instances.append(self)


SIP_ENV = {**ENV, 'COLLEAGUE_PHONE_AUDIO': 'sip', 'COLLEAGUE_SIP_TRUNK_URL': 'sips:acme-openai.dapp.signalwire.com:5061',
           'COLLEAGUE_SIP_USERNAME': '+15005550006', 'COLLEAGUE_SIP_PASSWORD': 'sip-secret',
           'COLLEAGUE_OWNER_PHONE': '+14155550199'}


class SipPhoneTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        FakeLive.instances = []
        FakeSideband.instances = []
        FakeSummarizer.calls = 0
        self.temp = tempfile.TemporaryDirectory()
        self.h = PhoneHarness(self.temp.name, env=dict(SIP_ENV))
        self.client = FakeSipClient()
        self.h.line.sip_client_factory = lambda key: self.client
        self.h.line.sideband_factory = FakeSideband

    async def asyncTearDown(self):
        await self.h.service.shutdown()
        self.temp.cleanup()

    async def sideband(self):
        await until(lambda: FakeSideband.instances)
        return FakeSideband.instances[-1]

    async def test_openai_dials_out_and_this_side_only_steers(self):
        # Nothing on this computer needs to be reachable for OpenAI to dial out.
        self.h.line.gateway_ready = False
        record = await self.h.service.create(brief())
        side = await self.sideband()
        kind, session, destination, trunk = self.client.calls[0]
        self.assertEqual((kind, destination), ('create', '+14155550142'))
        self.assertNotIn('format', session['audio'])
        self.assertNotIn('type', session)
        self.assertEqual(trunk, {'provider_url': 'sips:acme-openai.dapp.signalwire.com:5061',
                                 'auth': {'type': 'digest', 'username': '+15005550006', 'password': 'sip-secret'},
                                 'caller_number': '+15005550006'})
        self.assertEqual(self.h.twilio.created, [])
        side.push({'type': 'transport.ringing', 'event_id': 'e1'})
        await until(lambda: self.h.store.get(record['id'])['status'] == 'ringing')
        side.push({'type': 'transport.answered', 'event_id': 'e2'})
        await until(lambda: self.h.store.get(record['id'])['status'] == 'in_progress')
        side.push({'type': 'session.input_transcript.delta', 'delta': 'Hello?', 'start_ms': 0, 'end_ms': 400})
        await until(lambda: any(kind == 'session.commentary.append' for kind, _ in side.appends))
        side.push({'type': 'session.output_transcript.delta',
                   'delta': "Hi, I'm an AI assistant calling on behalf of Robin.", 'start_ms': 600, 'end_ms': 2500})
        side.push({'type': 'session.output_audio.delta', 'delta': 'AAAA', 'start_ms': 600, 'end_ms': 620})
        side.push({'type': 'session.input_audio.append', 'audio': 'AAAA'})
        # An instruction from the agent reaches the call through the sideband.
        self.assertEqual(await self.h.service.instruct(record['id'], 'Mention the patio.'), {'delivered': True})
        side.push({'type': 'response.event', 'delegation_id': 'd1', 'event': {
            'type': 'response.output_item.done', 'item': {
                'type': 'function_call', 'call_id': 'fc1', 'name': 'end_call',
                'arguments': '{"reason": "completed"}'}}})
        done = await self.h.service.wait(record['id'], timeout=8)
        self.assertEqual(done['status'], 'completed')
        self.assertEqual(done['endReason'], 'hangup')
        self.assertIn(('hangup', 'live_sip_1'), self.client.calls)
        self.assertEqual(done['line']['liveSessionId'], 'live_sip_1')
        self.assertEqual(done['usage']['voiceSeconds'], 21)
        self.assertIn('phoneSeconds', done['usage'])
        self.assertIs(done['result']['disclosureVerified'], True)
        self.assertEqual([line['speaker'] for line in done['result']['transcript']], ['other', 'agent'])

    async def test_the_callee_hanging_up_ends_the_call(self):
        record = await self.h.service.create(brief())
        side = await self.sideband()
        side.push({'type': 'transport.answered', 'event_id': 'e2'})
        side.push({'type': 'session.input_transcript.delta', 'delta': 'No thanks.', 'start_ms': 0, 'end_ms': 400})
        side.push({'type': 'session.closed', 'reason': 'remote_hangup', 'usage': {'seconds': 9}})
        done = await self.h.service.wait(record['id'], timeout=8)
        self.assertEqual(done['endReason'], 'remote_hangup')

    async def test_a_call_nobody_answers(self):
        record = await self.h.service.create(brief())
        side = await self.sideband()
        side.push({'type': 'transport.failed', 'event_id': 'e3',
                   'error': {'type': 'call_error', 'code': 'provider_invite_failed', 'message': 'No answer'}})
        done = await self.h.service.wait(record['id'], timeout=8)
        self.assertEqual((done['endReason'], done['result']['outcome']), ('no_answer', 'not_reached'))
        self.assertIn(('hangup', 'live_sip_1'), self.client.calls)

    async def test_without_outbound_sip_the_call_is_relayed_instead(self):
        self.client.refuse = 'outbound_sip_not_enabled'
        record = await self.h.service.create(brief())
        await until(lambda: self.h.twilio.created)
        events = [e['type'] for e in self.h.service.events(record['id'])]
        self.assertIn('call.sip_unavailable', events)
        self.assertIn('<Connect><Stream', self.h.twilio.created[0]['twiml'])
        await self.h.service.end(record['id'])

    async def test_missing_trunk_settings_are_named(self):
        self.h.env.pop('COLLEAGUE_SIP_PASSWORD')
        with self.assertRaises(CallError) as caught:
            await self.h.service.create(brief())
        self.assertEqual(caught.exception.details['missing'], ['COLLEAGUE_SIP_PASSWORD'])

    async def test_taking_over_refers_the_call_to_the_owner(self):
        record = await self.h.service.create(brief())
        side = await self.sideband()
        side.push({'type': 'transport.answered', 'event_id': 'e2'})
        await until(lambda: self.h.store.get(record['id'])['status'] == 'in_progress')
        result = await self.h.service.transfer(record['id'])
        self.assertEqual(result, {'transferred': True})
        self.assertIn(('refer', 'live_sip_1', 'tel:+14155550199'), self.client.calls)
        side.push({'type': 'session.closed', 'reason': 'close_requested', 'usage': {'seconds': 30}})
        done = await self.h.service.wait(record['id'], timeout=8)
        self.assertEqual(done['endReason'], 'transferred')


class SipWebhookTests(unittest.IsolatedAsyncioTestCase):
    SECRET = 'whsec_' + base64.b64encode(b'sixteen-byte-key').decode()

    async def asyncSetUp(self):
        FakeLive.instances = []
        FakeSideband.instances = []
        self.temp = tempfile.TemporaryDirectory()
        env = {**ENV, 'COLLEAGUE_PHONE_AUDIO': 'sip-webhook', 'OPENAI_PROJECT_ID': 'proj_abc',
               'OPENAI_WEBHOOK_SECRET': self.SECRET}
        self.h = PhoneHarness(self.temp.name, env=env)
        self.client = FakeSipClient()
        self.h.line.sip_client_factory = lambda key: self.client
        self.h.line.sideband_factory = FakeSideband
        app = create_gateway_app(self.h.line, self.h.service, current_url=lambda: PUBLIC)
        self.gateway = TestClient(TestServer(app))
        await self.gateway.start_server()

    async def asyncTearDown(self):
        await self.gateway.close()
        await self.h.service.shutdown()
        self.temp.cleanup()

    def signed(self, event):
        import hashlib, hmac, time
        body = json.dumps(event)
        stamp = str(int(time.time()))
        key = base64.b64decode(self.SECRET[6:])
        digest = hmac.new(key, f'wh_1.{stamp}.{body}'.encode(), hashlib.sha256).digest()
        return body, {'webhook-id': 'wh_1', 'webhook-timestamp': stamp,
                      'webhook-signature': 'v1,' + base64.b64encode(digest).decode(),
                      'Content-Type': 'application/json'}

    async def test_the_provider_dials_and_openai_hands_the_call_back(self):
        record = await self.h.service.create(brief())
        await until(lambda: self.h.twilio.created)
        twiml = self.h.twilio.created[0]['twiml']
        self.assertIn('<Sip codecs="PCMU,PCMA,OPUS">sip:proj_abc@sip.api.openai.com;transport=tls?X-Colleague-Call=' + record['id'], twiml)
        token = self.h.line.session(record['id']).token
        self.assertIn('X-Colleague-Token=' + token, twiml.replace('&amp;', '&'))
        # A forged webhook is refused.
        body, headers = self.signed({'type': 'live.transport.incoming', 'data': {'session_id': 'live_x'}})
        bad = dict(headers, **{'webhook-signature': 'v1,AAAA'})
        self.assertEqual((await self.gateway.post('/openai/webhook', data=body, headers=bad)).status, 403)
        # OpenAI announces the call with our headers: it is accepted and steered.
        event = {'type': 'live.transport.incoming', 'data': {'type': 'sip', 'session_id': 'live_in_1', 'sip_headers': [
            {'name': 'X-Colleague-Call', 'value': record['id']}, {'name': 'X-Colleague-Token', 'value': token}]}}
        body, headers = self.signed(event)
        self.assertEqual((await self.gateway.post('/openai/webhook', data=body, headers=headers)).status, 200)
        side = await until_value(lambda: FakeSideband.instances and FakeSideband.instances[-1])
        accept = next(call for call in self.client.calls if call[0] == 'accept')
        self.assertEqual(accept[1], 'live_in_1')
        self.assertEqual(accept[2]['type'], 'live')
        await until(lambda: self.h.store.get(record['id'])['status'] == 'in_progress')
        # A retried webhook for the same call changes nothing.
        self.assertEqual((await self.gateway.post('/openai/webhook', data=body, headers=headers)).status, 200)
        self.assertEqual(sum(call[0] == 'accept' for call in self.client.calls), 1)
        side.push({'type': 'session.closed', 'reason': 'remote_hangup', 'usage': {'seconds': 12}})
        done = await self.h.service.wait(record['id'], timeout=8)
        self.assertEqual(done['endReason'], 'remote_hangup')

    async def test_calls_this_installation_did_not_place_are_turned_away(self):
        event = {'type': 'live.transport.incoming', 'data': {'type': 'sip', 'session_id': 'live_stranger',
                                                            'sip_headers': [{'name': 'X-Colleague-Call', 'value': 'call-0000000000000000'}]}}
        body, headers = self.signed(event)
        self.assertEqual((await self.gateway.post('/openai/webhook', data=body, headers=headers)).status, 200)
        await until(lambda: ('reject', 'live_stranger', 486) in self.client.calls)


async def until_value(getter, timeout=3.0):
    await until(lambda: bool(getter()), timeout)
    return getter()


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
                                    'delta': base64.b64encode(b'\x2a' * 160).decode(),
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
