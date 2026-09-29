"""The only public routes: Twilio (or SignalWire) call status, voicemail detection, recordings,
media, and inbound calls, plus OpenAI's signed webhook for calls handed to it over SIP.

Runs as its own listener on loopback (default 127.0.0.1:8766) so a tunnel or
proxy can expose it without exposing the daemon API. Every HTTP callback must
carry a valid X-Twilio-Signature. Media streams must present the per-call token
that was placed in the TwiML when the call was created.
"""
import asyncio
import json
import secrets

from aiohttp import WSMsgType, web

from call_hooks import MissingCredentials
from call_store import CallNotFound
from live_sip import incoming_call, verify_webhook
from phone_line import inbound_brief
from twilio_client import signing_keys, stream_twiml, valid_signature


GATEWAY_PORT = 8766
START_TIMEOUT = 10.0
XML = 'text/xml'
DEFAULT_MAX_INBOUND = 2
REJECT = '<?xml version="1.0" encoding="UTF-8"?><Response><Reject/></Response>'
BUSY = '<?xml version="1.0" encoding="UTF-8"?><Response><Reject reason="busy"/></Response>'


def create_gateway_app(line, service, *, current_url, owner='local'):
    """current_url: returns the https origin Twilio uses to reach us right now, or None.

    It must not start a tunnel: unsigned requests reach these routes too.
    """

    def keys():
        try:
            return signing_keys(service.hooks.credentials(owner, 'twilio'))
        except MissingCredentials:
            return []

    def base_url():
        base = current_url()
        if not base:
            raise web.HTTPForbidden(text='no public address')
        return base.rstrip('/')

    async def verified_params(request):
        params = dict(await request.post())
        url = base_url() + request.path_qs
        provided = (request.headers.get('X-Twilio-Signature')
                    or request.headers.get('X-SignalWire-Signature'))
        if not any(valid_signature(key, url, params, provided) for key in keys()):
            raise web.HTTPForbidden(text='invalid signature')
        return params

    async def status(request):
        params = await verified_params(request)
        line.on_status(request.match_info['callId'], params)
        return web.Response(status=204)

    async def amd(request):
        params = await verified_params(request)
        line.on_amd(request.match_info['callId'], params.get('AnsweredBy'))
        return web.Response(status=204)

    async def media(request):
        ws = web.WebSocketResponse(heartbeat=20)
        await ws.prepare(request)

        async def read_start():
            async for message in ws:
                if message.type != WSMsgType.TEXT:
                    continue
                data = json.loads(message.data)
                if data.get('event') == 'start':
                    return data.get('start') or {}
            return None

        try:
            start = await asyncio.wait_for(read_start(), START_TIMEOUT)
        except (asyncio.TimeoutError, ValueError):
            start = None
        parameters = (start or {}).get('customParameters') or {}
        session = line.claim(parameters.get('callId'), parameters.get('token'))
        if session is None:
            await ws.close(code=1008, message=b'unknown call')
            return ws
        await session.run(ws, start)
        return ws

    async def recording(request):
        params = await verified_params(request)
        if params.get('RecordingStatus', 'completed') == 'completed' and params.get('RecordingUrl'):
            try:
                service.attach_recording(request.match_info['callId'],
                                         sid=params.get('RecordingSid'),
                                         url=params['RecordingUrl'],
                                         seconds=params.get('RecordingDuration'))
            except CallNotFound:
                pass
        return web.Response(status=204)

    def max_inbound(env):
        try:
            return max(0, int(env.get('COLLEAGUE_MAX_INBOUND') or DEFAULT_MAX_INBOUND))
        except ValueError:
            return DEFAULT_MAX_INBOUND

    async def inbound(request):
        params = await verified_params(request)
        env = line.environ()
        if env.get('COLLEAGUE_ACCEPT_INBOUND') != '1':
            return web.Response(text=REJECT, content_type=XML)
        if service.active_count(direction='inbound') >= max_inbound(env):
            return web.Response(text=BUSY, content_type=XML)
        caller = params.get('From') or ''
        call_sid = params.get('CallSid') or ''
        try:
            brief = inbound_brief(env, caller)
        except ValueError:
            return web.Response(text=REJECT, content_type=XML)
        token = secrets.token_urlsafe(24)
        record = await service.create_inbound(
            brief, owner,
            lambda ctx: line.start_inbound(ctx, call_sid=call_sid, token=token))
        for _ in range(50):
            if line.session(record['id']) is not None:
                break
            await asyncio.sleep(0.02)
        stream_url = 'wss://' + base_url().split('://', 1)[1] + '/twilio/media'
        twiml = stream_twiml(stream_url, {'callId': record['id'], 'token': token})
        return web.Response(text=twiml, content_type=XML)

    async def openai_webhook(request):
        """OpenAI announces calls the provider handed to its SIP address (sip-webhook mode)."""
        raw = await request.text()
        try:
            secret = service.hooks.credentials(owner, 'sip').get('webhookSecret')
        except MissingCredentials:
            secret = None
        if not secret:
            raise web.HTTPForbidden(text='webhooks are not configured')
        try:
            event = verify_webhook(raw, request.headers, secret)
        except ValueError:
            raise web.HTTPForbidden(text='invalid signature')
        found = incoming_call(event)
        if found is not None and not line.on_sip_incoming(*found):
            # Not a call this installation placed: turn it away so it does not wait.
            asyncio.get_running_loop().create_task(reject_call(found[0]))
        return web.Response(status=200)

    async def reject_call(session_id):
        try:
            api_key = service.hooks.credentials(owner, 'openai')['apiKey']
            await line.sip_client_factory(api_key).reject(session_id, 486)
        except Exception:
            pass

    async def healthz(_request):
        return web.Response(text='ok')

    app = web.Application(client_max_size=64 * 1024)
    app.router.add_post('/twilio/status/{callId}', status)
    app.router.add_post('/twilio/amd/{callId}', amd)
    app.router.add_post('/twilio/recording/{callId}', recording)
    app.router.add_get('/twilio/media', media)
    app.router.add_post('/twilio/inbound', inbound)
    app.router.add_post('/openai/webhook', openai_webhook)
    app.router.add_get('/healthz', healthz)
    return app
