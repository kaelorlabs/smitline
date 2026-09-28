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
from phone_prompts import delegation_config, mentions_ai, voice_instructions
from tunnel import PublicUrl, TunnelError, configured_url
from twilio_client import TwilioClient, compute_signature, stream_twiml, valid_signature


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

    async def create_call(self, **kwargs):
        self.created.append(kwargs)
        return {'sid': 'CA123'}

    async def update_call(self, call_sid, **kwargs):
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

        async def public_url():
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
        await until(lambda: any(m['event'] == 'media' for m in ws.sent))
        media = next(m for m in ws.sent if m['event'] == 'media')
        self.assertEqual(media, {'event': 'media', 'streamSid': 'MZ1', 'media': {'payload': audio}})
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
        self.assertEqual(live.outputs, [('fc1', {'ok': True}, 'd1')])
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
        self.assertEqual(disclosure[0]['data'], {'verified': True})

    async def test_unanswered_call_needs_no_summary(self):
        record, _session = await self.h.dial()
        self.h.line.on_status(record['id'], {'CallStatus': 'no-answer', 'CallDuration': '0'})
        done = await self.h.service.wait(record['id'], timeout=5)
        self.assertEqual((done['endReason'], done['result']['outcome']), ('no_answer', 'not_reached'))
        self.assertEqual(FakeSummarizer.calls, 0)

    async def test_missing_disclosure_is_corrected(self):
        record, session = await self.h.dial()
        ws, live, task = await self.h.connect(record, session)
        live.push({'type': 'session.started', 'session': {}})
        live.push({'type': 'session.output_transcript.delta', 'delta': 'Hello there, I would like a table.',
                   'start_ms': 0, 'end_ms': 900})
        live.push({'type': 'session.input_transcript.delta', 'delta': 'Who is this?', 'start_ms': 1000,
                   'end_ms': 1400})
        await until(lambda: any(kind == 'session.instructions.append' for kind, _ in live.appends))
        ws.push({'event': 'stop'})
        await asyncio.wait_for(task, 3)
        done = await self.h.service.wait(record['id'], timeout=5)
        self.assertEqual(done['endReason'], 'remote_hangup')
        self.assertTrue(live.close_requested)

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
                             ['TWILIO_ACCOUNT_SID', 'TWILIO_AUTH_TOKEN', 'TWILIO_FROM_NUMBER'])


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

        async def send(message):
            sent.append(message)
        pacer = OutputPacer(send, clock=lambda: now[0], sleep=sleep)
        chunk = base64.b64encode(b'\xff' * 4000).decode()  # 0.5 s of mu-law
        for _ in range(3):
            pacer.offer(chunk)
        runner = asyncio.create_task(pacer.run())
        await until(lambda: len(sent) == 6)
        runner.cancel()
        self.assertEqual(slept, [0.2, 0.5])
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

        async def failing(method, url, form):
            return 400, {'code': 21211, 'message': "The 'To' number is not a valid phone number."}
        from twilio_client import TwilioError
        with self.assertRaises(TwilioError) as caught:
            await TwilioClient('AC1', 'tok', request=failing).update_call('CA1', status='completed')
        self.assertEqual(caught.exception.code, 21211)


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

        class Process:
            returncode = None

            def __init__(self):
                self.stderr = Stream([b'INF starting\n',
                                      b'INF |  https://brave-fox-12.trycloudflare.com  |\n'])

            def terminate(self):
                self.returncode = 0

            async def wait(self):
                return 0
        spawned = []

        async def spawn(*command, **kwargs):
            spawned.append(command)
            return Process()
        tunnel = PublicUrl(lambda: {}, 8766, spawn=spawn,
                           which=lambda name: '/usr/bin/cloudflared' if name == 'cloudflared' else None)
        self.assertEqual(await tunnel.get(), 'https://brave-fox-12.trycloudflare.com')
        self.assertEqual(await tunnel.get(), 'https://brave-fox-12.trycloudflare.com')
        self.assertEqual(len(spawned), 1)
        self.assertEqual(spawned[0][-1], 'http://127.0.0.1:8766')
        await tunnel.close()
        docker = PublicUrl(lambda: {}, 8766, spawn=spawn,
                           which=lambda name: '/usr/bin/docker' if name == 'docker' else None)
        self.assertEqual(docker.command()[:4], ['docker', 'run', '--rm', '--network'])
        self.assertFalse(PublicUrl(lambda: {}, 8766, which=lambda name: None).available())


class GatewayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        FakeLive.instances = []
        self.temp = tempfile.TemporaryDirectory()
        self.h = PhoneHarness(self.temp.name)

        async def public_url():
            return PUBLIC
        app = create_gateway_app(self.h.line, self.h.service, public_url=public_url)
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


if __name__ == '__main__':
    unittest.main()
