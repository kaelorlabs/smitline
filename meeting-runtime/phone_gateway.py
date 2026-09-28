"""The only routes Twilio reaches: call status, voicemail detection, media stream, inbound calls.

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
from phone_line import inbound_brief
from twilio_client import stream_twiml, valid_signature


GATEWAY_PORT = 8766
START_TIMEOUT = 10.0
XML = 'text/xml'


def create_gateway_app(line, service, *, current_url, owner='local'):
    """current_url: returns the https origin Twilio uses to reach us right now, or None.

    It must not start a tunnel: unsigned requests reach these routes too.
    """

    def auth_token():
        try:
            return service.hooks.credentials(owner, 'twilio')['authToken']
        except MissingCredentials:
            return None

    def base_url():
        base = current_url()
        if not base:
            raise web.HTTPForbidden(text='no public address')
        return base.rstrip('/')

    async def verified_params(request):
        params = dict(await request.post())
        url = base_url() + request.path_qs
        if not valid_signature(auth_token(), url, params,
                               request.headers.get('X-Twilio-Signature')):
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

    async def inbound(request):
        params = await verified_params(request)
        env = line.environ()
        if env.get('COLLEAGUE_ACCEPT_INBOUND') != '1':
            return web.Response(
                text='<?xml version="1.0" encoding="UTF-8"?><Response><Reject/></Response>',
                content_type=XML)
        caller = params.get('From') or ''
        call_sid = params.get('CallSid') or ''
        try:
            brief = inbound_brief(env, caller)
        except ValueError:
            return web.Response(
                text='<?xml version="1.0" encoding="UTF-8"?><Response><Reject/></Response>',
                content_type=XML)
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

    async def healthz(_request):
        return web.Response(text='ok')

    app = web.Application(client_max_size=64 * 1024)
    app.router.add_post('/twilio/status/{callId}', status)
    app.router.add_post('/twilio/amd/{callId}', amd)
    app.router.add_get('/twilio/media', media)
    app.router.add_post('/twilio/inbound', inbound)
    app.router.add_get('/healthz', healthz)
    return app
