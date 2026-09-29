import base64
import hashlib
import hmac
import json
import unittest

from aiohttp import WSMsgType

from live_sip import (
    LiveSideband, LiveSipClient, LiveSipError, incoming_call, openai_sip_uri, sip_session,
    verify_webhook,
)
from voice_core import PCMU8, session_config


class Message:
    def __init__(self, data):
        self.type = WSMsgType.TEXT
        self.data = json.dumps(data)


class FakeWs:
    def __init__(self, incoming):
        self.sent = []
        self.incoming = incoming

    async def send_json(self, payload):
        self.sent.append(payload)

    async def close(self):
        pass

    def __aiter__(self):
        async def generator():
            for item in self.incoming:
                yield Message(item)
        return generator()


def signed(body, secret, webhook_id='wh_1', timestamp=1_750_000_000):
    key = base64.b64decode(secret[6:])
    digest = hmac.new(key, f'{webhook_id}.{timestamp}.{body}'.encode(), hashlib.sha256).digest()
    return {'webhook-id': webhook_id, 'webhook-timestamp': str(timestamp),
            'webhook-signature': 'v1,' + base64.b64encode(digest).decode()}


class SipConfigTests(unittest.TestCase):
    def test_sip_sessions_drop_the_audio_format(self):
        config = session_config(instructions='Be brief.', audio_format=PCMU8, voice='cinder',
                                delegation={'type': 'client'})
        session = sip_session(config)
        self.assertEqual(session['audio'], {'output': {'voice': 'cinder'}})
        self.assertNotIn('type', session)
        accepted = sip_session(config, accept=True)
        self.assertEqual(accepted['type'], 'live')
        self.assertEqual(accepted['model'], 'gpt-live-1')
        self.assertEqual(config['audio']['format'], PCMU8)  # the original is untouched
        self.assertNotIn('audio', sip_session(session_config(instructions='x', audio_format=PCMU8)))

    def test_sip_uri_carries_correlation_headers(self):
        self.assertEqual(openai_sip_uri('proj_1'), 'sip:proj_1@sip.api.openai.com;transport=tls')
        self.assertEqual(openai_sip_uri('proj_1', {'X-Colleague-Call': 'call-0123456789abcdef'}),
                         'sip:proj_1@sip.api.openai.com;transport=tls?X-Colleague-Call=call-0123456789abcdef')


class WebhookTests(unittest.TestCase):
    SECRET = 'whsec_' + base64.b64encode(b'sixteen-byte-key').decode()

    def test_valid_signatures_pass_and_everything_else_fails(self):
        body = json.dumps({'type': 'live.transport.incoming', 'data': {'session_id': 'live_1'}})
        headers = signed(body, self.SECRET)
        self.assertEqual(verify_webhook(body, headers, self.SECRET, now=1_750_000_010)['data']['session_id'],
                         'live_1')
        # Rotation: any of several space-separated signatures may match.
        rotated = dict(headers, **{'webhook-signature': 'v1,AAAA ' + headers['webhook-signature']})
        verify_webhook(body.encode(), rotated, self.SECRET, now=1_750_000_010)
        for bad in (dict(headers, **{'webhook-signature': 'v1,AAAA'}),
                    {k: v for k, v in headers.items() if k != 'webhook-id'}):
            with self.assertRaises(ValueError):
                verify_webhook(body, bad, self.SECRET, now=1_750_000_010)
        with self.assertRaises(ValueError):
            verify_webhook(body + ' ', headers, self.SECRET, now=1_750_000_010)
        with self.assertRaises(ValueError):
            verify_webhook(body, headers, self.SECRET, now=1_750_000_000 + 301)

    def test_incoming_call_finds_the_session_and_headers(self):
        event = {'type': 'live.transport.incoming', 'data': {
            'type': 'sip', 'session_id': 'live_u0_abc',
            'sip_headers': [{'name': 'From', 'value': 'sip:+1415@x'},
                            {'name': 'X-Colleague-Call', 'value': 'call-0123456789abcdef'}]}}
        self.assertEqual(incoming_call(event),
                         ('live_u0_abc', {'From': 'sip:+1415@x', 'X-Colleague-Call': 'call-0123456789abcdef'}))
        legacy = {'type': 'live.call.incoming', 'data': {'session_id': 'live_2'}}
        self.assertEqual(incoming_call(legacy), ('live_2', {}))
        self.assertIsNone(incoming_call({'type': 'realtime.call.incoming', 'data': {'call_id': 'rtc_1'}}))


class SipClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_requests_match_the_documented_shapes(self):
        sent = []

        async def request(method, url, body):
            sent.append((method, url, body))
            if url.endswith('/live/sessions'):
                return 200, {'session': {'id': 'live_123'}, 'transport': {'type': 'sip'}}
            return 200, {}
        client = LiveSipClient('sk-test', request=request)
        trunk = {'provider_url': 'sips:sip.example.com:5061',
                 'auth': {'type': 'digest', 'username': 'u', 'password': 'p'},
                 'caller_number': '+14155550100'}
        session_id = await client.create_outbound({'model': 'gpt-live-1'}, destination='+14155550142',
                                                  trunk=trunk)
        self.assertEqual(session_id, 'live_123')
        self.assertEqual(sent[0], ('POST', 'https://api.openai.com/v1/live/sessions', {
            'session': {'model': 'gpt-live-1'},
            'transport': {'type': 'sip', 'destination': '+14155550142', 'trunk': trunk}}))
        await client.accept('live_9', {'type': 'live', 'model': 'gpt-live-1'})
        await client.reject('live_9', 603)
        await client.refer('live_9', 'tel:+14155550199')
        await client.hangup('live_9')
        self.assertEqual([(m, u.rsplit('/', 1)[-1], b) for m, u, b in sent[1:]], [
            ('POST', 'accept', {'session': {'type': 'live', 'model': 'gpt-live-1'}}),
            ('POST', 'reject', {'status_code': 603}),
            ('POST', 'refer', {'target_uri': 'tel:+14155550199'}),
            ('POST', 'hangup', None),
        ])

    async def test_errors_carry_openai_codes_and_hangup_tolerates_gone_sessions(self):
        async def forbidden(method, url, body):
            return 403, {'error': {'code': 'outbound_sip_not_enabled', 'message': 'Outbound SIP is not enabled.'}}
        with self.assertRaises(LiveSipError) as caught:
            await LiveSipClient('k', request=forbidden).create_outbound({}, destination='+1', trunk={})
        self.assertEqual((caught.exception.status, caught.exception.code), (403, 'outbound_sip_not_enabled'))

        async def gone(method, url, body):
            return 404, {'error': {'code': 'session_id_not_found'}}
        await LiveSipClient('k', request=gone).hangup('live_1')  # already ended: no error
        with self.assertRaises(LiveSipError):
            await LiveSipClient('k', request=gone).accept('live_1', {})


class SidebandTests(unittest.IsolatedAsyncioTestCase):
    async def test_attach_sends_no_start_and_skips_replayed_events(self):
        ws = FakeWs([
            {'type': 'transport.ringing', 'event_id': 'e1'},
            {'type': 'transport.ringing', 'event_id': 'e1'},  # replayed on attach
            {'type': 'transport.answered', 'event_id': 'e2'},
            {'type': 'session.closed', 'event_id': 'e3', 'reason': 'remote_hangup', 'usage': {'seconds': 41}},
        ])
        urls = []

        async def connect(url, key):
            urls.append(url)
            return ws, None
        async with LiveSideband('sk-test', 'live_7', connect=connect) as side:
            self.assertEqual(urls, ['wss://api.openai.com/v1/live/sessions/live_7/attach'])
            self.assertTrue(await side.append('session.instructions.append', 'Say hello.'))
            self.assertFalse(await side.send_audio(b'x'))
            kinds = [event['type'] async for event in side.events()]
        self.assertEqual(kinds, ['transport.ringing', 'transport.answered', 'session.closed'])
        self.assertEqual(ws.sent[0]['type'], 'session.instructions.append')
        self.assertIsNone(ws.sent[0]['delegation_id'])
        self.assertEqual(side.usage_seconds, 41)
        self.assertEqual(side.close_reason, 'remote_hangup')
