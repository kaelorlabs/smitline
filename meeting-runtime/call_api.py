"""HTTP routes for calls on the runtime daemon."""
import asyncio
import json
from pathlib import Path

from aiohttp import web

from call_brief import BriefIncomplete, available_voices
from call_service import CallError
from call_store import TERMINAL, CallNotFound


OPENAPI_PATH = Path(__file__).with_name('openapi.json')


def _error(status, code, message, **details):
    body = {'error': {'code': code, 'message': message}}
    body['error'].update(details)
    return web.json_response(body, status=status)


def register_call_routes(app, service, *, read_json, public_json, sse_poll_interval=0.25,
                         sse_heartbeat_interval=15.0):

    async def owner(request):
        from call_hooks import maybe_await
        return await maybe_await(service.hooks.owner_for(request))

    def handle(handler):
        async def wrapped(request):
            try:
                return await handler(request)
            except BriefIncomplete as error:
                return _error(422, 'brief_incomplete', str(error), **error.to_dict())
            except CallError as error:
                return _error(error.status, error.code, error.message, **error.details)
            except CallNotFound:
                return _error(404, 'not_found', 'call not found')
        return wrapped

    @handle
    async def check_call(request):
        payload = await read_json(request)
        return public_json(await service.check(payload, request=request))

    @handle
    async def create_call(request):
        payload = await read_json(request)
        record = await service.create(payload, request=request)
        return public_json(record, status=201)

    @handle
    async def list_calls(request):
        try:
            limit = max(1, min(int(request.query.get('limit', '20')), 100))
        except ValueError:
            return _error(422, 'invalid_request', 'limit must be a number')
        return public_json({'calls': service.list(owner=await owner(request), limit=limit)})

    @handle
    async def get_call(request):
        return public_json(service.get(request.match_info['callId'], owner=await owner(request)))

    @handle
    async def wait_call(request):
        try:
            timeout = float(request.query.get('timeout', '60'))
        except ValueError:
            return _error(422, 'invalid_request', 'timeout must be a number of seconds')
        record = await service.wait(request.match_info['callId'], timeout=timeout,
                                    owner=await owner(request))
        return public_json(record)

    @handle
    async def instruct_call(request):
        payload = await read_json(request)
        extra = set(payload) - {'text'}
        if extra:
            return _error(422, 'invalid_request', 'unknown fields: ' + ', '.join(sorted(extra)))
        result = await service.instruct(request.match_info['callId'], payload.get('text'),
                                        owner=await owner(request))
        return public_json(result)

    @handle
    async def end_call(request):
        record = await service.end(request.match_info['callId'], owner=await owner(request))
        return public_json(record)

    @handle
    async def transfer_call(request):
        result = await service.transfer(request.match_info['callId'], owner=await owner(request))
        return public_json(result)

    @handle
    async def call_events(request):
        call_id = request.match_info['callId']
        service.get(call_id, owner=await owner(request))
        if request.query.get('format') == 'json':
            # Polling clients (the local console) read a page of events after a cursor.
            events = service.events(call_id, after=request.query.get('after'))
            return public_json({'events': events[:500]})
        response = web.StreamResponse(status=200, headers={
            'Content-Type': 'text/event-stream',
            'Cache-Control': 'no-cache',
            'Connection': 'keep-alive',
        })
        await response.prepare(request)
        after = request.headers.get('Last-Event-ID')
        loop = asyncio.get_running_loop()
        last_heartbeat = loop.time()
        try:
            while True:
                for event in service.events(call_id, after=after):
                    chunk = (f'id: {event["id"]}\nevent: {event["type"]}\n'
                             f'data: {json.dumps(event, ensure_ascii=False)}\n\n')
                    await response.write(chunk.encode('utf-8'))
                    after = event['id']
                if service.get(call_id)['status'] in TERMINAL and not service.events(call_id, after=after):
                    break
                if loop.time() - last_heartbeat >= sse_heartbeat_interval:
                    await response.write(b': heartbeat\n\n')
                    last_heartbeat = loop.time()
                await service.wait_for_change(call_id, sse_poll_interval)
        except (asyncio.CancelledError, ConnectionResetError, BrokenPipeError):
            return response
        try:
            await response.write_eof()
        except Exception:
            pass
        return response

    async def list_voices(_request):
        from call_brief import default_voice
        env = service.environ
        return public_json({'default': default_voice(env), 'voices': list(available_voices(env))})

    async def openapi(_request):
        return web.json_response(json.loads(OPENAPI_PATH.read_text(encoding='utf-8')))

    app.router.add_post('/v1/calls/check', check_call)
    app.router.add_post('/v1/calls', create_call)
    app.router.add_get('/v1/calls', list_calls)
    app.router.add_get('/v1/calls/{callId}', get_call)
    app.router.add_get('/v1/calls/{callId}/wait', wait_call)
    app.router.add_get('/v1/calls/{callId}/events', call_events)
    app.router.add_post('/v1/calls/{callId}/instructions', instruct_call)
    app.router.add_post('/v1/calls/{callId}/end', end_call)
    app.router.add_post('/v1/calls/{callId}/transfer', transfer_call)
    app.router.add_get('/v1/voices', list_voices)
    app.router.add_get('/v1/openapi.json', openapi)
