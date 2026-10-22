"""Listening in: the audio tap, the phone and meeting taps, and the listen endpoint."""
import asyncio
import base64
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from api_tokens import ApiTokenStore
from call_service import CallError
from listening import LISTENER_QUEUE, AudioTap
from meeting_line import MeetingLine, RemoteTap
from runtime_daemon import create_app
from test_call_service import FakeLine, ServiceHarness, brief
from test_phone import ENV, PhoneHarness, until


async def collect(tap, into, ready):
    async for chunk in tap.chunks():
        into.append(chunk)
        ready.set()


class AudioTapTests(unittest.IsolatedAsyncioTestCase):
    async def test_listeners_get_the_format_then_both_sides_until_it_closes(self):
        tap = AudioTap('mulaw', 8000)
        tap.push('other', 'AAAA')  # nobody listening yet: nothing kept
        got, ready = [], asyncio.Event()
        task = asyncio.create_task(collect(tap, got, ready))
        await ready.wait()
        self.assertEqual(got, [{'type': 'format', 'codec': 'mulaw', 'rate': 8000}])
        self.assertEqual(tap.listening, 1)
        tap.push('other', 'f39/')
        tap.push('agent', 'KioqKg==')
        tap.push('nobody', 'AAAA')
        tap.push('agent', '')
        tap.close()
        await asyncio.wait_for(task, 1)
        self.assertEqual(got[1:], [{'side': 'other', 'audio': 'f39/'}, {'side': 'agent', 'audio': 'KioqKg=='}])
        self.assertEqual(tap.listening, 0)
        tap.push('other', 'AAAA')  # closed: ignored

    async def test_a_slow_listener_loses_the_oldest_audio_never_the_call(self):
        tap = AudioTap('pcm16', 24000)
        chunks = tap.chunks()
        self.assertEqual((await chunks.__anext__())['type'], 'format')
        for n in range(LISTENER_QUEUE + 50):
            tap.push('meeting', str(n))
        tap.close()
        rest = [chunk async for chunk in chunks]
        self.assertEqual(len(rest), LISTENER_QUEUE - 1)  # the close took one place
        self.assertEqual(rest[-1]['audio'], str(LISTENER_QUEUE + 49))


class PhoneListenTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.h = PhoneHarness(self.temp.name, env=dict(ENV, SMITLINE_LISTEN='1'))

    async def asyncTearDown(self):
        await self.h.service.shutdown()
        self.temp.cleanup()

    async def test_both_sides_of_a_relayed_call(self):
        record, session = await self.h.dial()
        ws, live, task = await self.h.connect(record, session)
        tap = self.h.service.listen(record['id'], owner='local')
        got, ready = [], asyncio.Event()
        listener = asyncio.create_task(collect(tap, got, ready))
        await ready.wait()
        live.push({'type': 'session.started', 'session': {'id': 'sess_1'}})
        ws.push({'event': 'media', 'media': {'track': 'inbound', 'payload': 'f39/'}})
        live.push({'type': 'session.output_audio.delta', 'delta': base64.b64encode(b'\x2a' * 320).decode()})
        await until(lambda: {chunk.get('side') for chunk in got} >= {'other', 'agent'})
        self.assertEqual(got[0], {'type': 'format', 'codec': 'mulaw', 'rate': 8000})
        self.assertIn({'side': 'other', 'audio': 'f39/'}, got)
        agent = b''.join(base64.b64decode(c['audio']) for c in got if c.get('side') == 'agent')
        self.assertEqual(agent, b'\x2a' * 320)
        events = [e['type'] for e in self.h.service.events(record['id'])]
        self.assertIn('call.listening', events)
        # The call ends: so does the stream.
        await ws.close()
        await asyncio.wait_for(listener, 3)
        await asyncio.wait_for(task, 3)

    async def test_refusals(self):
        record, session = await self.h.dial()
        # Not connected yet: nothing passes through to hear. (The tap exists, but listen on the
        # service only says no once the call has ended or its audio cannot pass.)
        session.tap = None
        with self.assertRaises(CallError) as caught:
            self.h.service.listen(record['id'], owner='local')
        self.assertEqual(caught.exception.code, 'listen_unavailable')
        with self.assertRaises(Exception):
            self.h.service.listen(record['id'], owner='someone-else')

    async def test_off_unless_the_owner_turns_it_on(self):
        off = PhoneHarness(tempfile.mkdtemp(), env=dict(ENV))
        try:
            record, _session = await off.dial()
            with self.assertRaises(CallError) as caught:
                off.service.listen(record['id'], owner='local')
            self.assertEqual((caught.exception.status, caught.exception.code), (403, 'listening_off'))
            self.assertNotIn('call.listening', [e['type'] for e in off.service.events(record['id'])])
        finally:
            await off.service.shutdown()

    def test_direct_sip_has_nothing_to_tap(self):
        self.h.line.sessions['call-sip'] = SimpleNamespace(tap=None)
        self.assertIsNone(self.h.line.listen('call-sip'))
        closed = AudioTap('mulaw', 8000)
        closed.close()
        self.h.line.sessions['call-done'] = SimpleNamespace(tap=closed)
        self.assertIsNone(self.h.line.listen('call-done'))
        self.assertIsNone(self.h.line.listen('call-unknown'))


class ListenEndpointTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.h = ServiceHarness(self.temp.name, environ={'OPENAI_API_KEY': 'sk-test', 'SMITLINE_LISTEN': '1'})
        self.tap = AudioTap('mulaw', 8000)
        self.h.line.listen = lambda call_id: self.tap
        app = create_app(root=Path(self.temp.name) / 'daemon', auth_token='launch-token',
                         call_service=self.h.service,
                         api_tokens=ApiTokenStore(Path(self.temp.name) / 'api-tokens.json'))
        self.client = TestClient(TestServer(app))
        await self.client.start_server()
        self.auth = {'Authorization': 'Bearer launch-token'}

    async def asyncTearDown(self):
        await self.client.close()
        await self.h.service.shutdown()
        self.temp.cleanup()

    async def test_streams_ndjson_until_the_call_ends(self):
        call = await (await self.client.post('/v1/calls', json=brief(), headers=self.auth)).json()
        await until(lambda: self.h.service.get(call['id'], owner='local')['status'] == 'in_progress')
        response = await self.client.get(f'/v1/calls/{call["id"]}/listen', headers=self.auth)
        self.assertEqual((response.status, response.headers['Content-Type']), (200, 'application/x-ndjson'))
        self.assertEqual(json.loads(await response.content.readline())['codec'], 'mulaw')
        self.tap.push('other', 'f39/')
        self.assertEqual(json.loads(await response.content.readline()), {'side': 'other', 'audio': 'f39/'})
        self.tap.close()
        self.assertEqual(await response.content.read(), b'')
        self.h.line.proceed.set()
        done = await (await self.client.get(f'/v1/calls/{call["id"]}/wait?timeout=5', headers=self.auth)).json()
        response = await self.client.get(f'/v1/calls/{done["id"]}/listen', headers=self.auth)
        self.assertEqual((response.status, (await response.json())['error']['code']), (409, 'call_ended'))


class MeetingListenTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_supervisor_knows_the_running_container_s_listen(self):
        from meeting_supervisor import MeetingPool, ProductionMeetingSupervisor, UrlHealthClient
        supervisor = ProductionMeetingSupervisor.__new__(ProductionMeetingSupervisor)
        supervisor.health = UrlHealthClient('http://127.0.0.1:18101/health')
        supervisor._active_id = 'mtg-a'
        supervisor._control_token = 'tok-a'
        self.assertEqual(supervisor.listen_url('mtg-a'), 'http://127.0.0.1:18101/listen')
        self.assertEqual(supervisor.control_headers('mtg-a'), {'Authorization': 'Bearer tok-a'})
        self.assertIsNone(supervisor.listen_url('mtg-b'))
        self.assertIsNone(supervisor.control_headers('mtg-b'))
        supervisor.health = SimpleNamespace()
        self.assertIsNone(supervisor.listen_url('mtg-a'))
        pool = MeetingPool.__new__(MeetingPool)
        pool._meetings = {'mtg-a': {'supervisor': SimpleNamespace(listen_url=lambda mid: f'url-{mid}')}}
        self.assertEqual(pool.listen_url('mtg-a'), 'url-mtg-a')
        self.assertIsNone(pool.listen_url('mtg-z'))

    async def test_the_meeting_line_relays_the_container(self):
        async def listen(request):
            if request.headers.get('Authorization') != 'Bearer tok-1':
                return web.json_response({'error': 'forbidden'}, status=403)
            response = web.StreamResponse(headers={'Content-Type': 'application/x-ndjson'})
            await response.prepare(request)
            for item in ({'type': 'format', 'codec': 'pcm16', 'rate': 24000},
                         {'side': 'meeting', 'audio': 'AAAA'}):
                await response.write((json.dumps(item) + '\n').encode())
            await response.write(b'not json\n')
            await response.write((json.dumps({'side': 'agent', 'audio': 'AQAB'}) + '\n').encode())
            return response
        app = web.Application()
        app.router.add_get('/listen', listen)
        server = TestServer(app)
        await server.start_server()
        try:
            headers = {'mtg-1': {'Authorization': 'Bearer tok-1'}}
            daemon = SimpleNamespace(supervisor=SimpleNamespace(
                listen_url=lambda mid: str(server.make_url('/listen')) if mid == 'mtg-1' else None,
                control_headers=headers.get))
            line = MeetingLine(daemon)
            self.assertIsNone(line.listen('call-unknown'))
            line._meetings['call-1'] = 'mtg-1'
            tap = line.listen('call-1')
            self.assertIsInstance(tap, RemoteTap)
            got = [chunk async for chunk in tap.chunks()]
            self.assertEqual(got, [{'type': 'format', 'codec': 'pcm16', 'rate': 24000},
                                   {'side': 'meeting', 'audio': 'AAAA'},
                                   {'side': 'agent', 'audio': 'AQAB'}])
            # Without the meeting's token (adopted after a restart) there is nothing to relay.
            headers.clear()
            self.assertIsNone(line.listen('call-1'))
        finally:
            await server.close()

    async def test_the_bridge_taps_what_gpt_live_hears_and_says(self):
        import bridge
        from runtime_config import RuntimeConfig
        from test_participation import Adapter, FakeLiveSocket, Mic, RecordingArchive
        socket = FakeLiveSocket(bridge)
        tap = AudioTap('pcm16', 24000)

        class Speaker:
            def __init__(self):
                self.queue = asyncio.Queue()

            async def read(self):
                return SimpleNamespace(data=await self.queue.get())
        speaker = Speaker()
        got, ready = [], asyncio.Event()
        listener = asyncio.create_task(collect(tap, got, ready))
        await ready.wait()
        with patch.object(bridge, 'ClientSession', socket.client), \
                patch.object(bridge, 'record', RecordingArchive()), \
                patch.object(bridge, 'listening', tap), \
                patch.object(bridge, 'stop', asyncio.Event()):
            task = asyncio.create_task(bridge.run_voice(
                speaker, Mic(), None, 'test-only',
                RuntimeConfig.from_environ({'SMITLINE_MEETING_INTRO': '0'}), Adapter()))
            try:
                await socket.events.put({'type': 'session.started'})
                await speaker.queue.put(b'\x01\x00' * 4)
                await socket.events.put({'type': 'session.output_audio.delta',
                                         'delta': base64.b64encode(b'\x02\x00' * 4).decode()})
                await until(lambda: {c.get('side') for c in got} >= {'meeting', 'agent'})
                self.assertIn({'side': 'meeting', 'audio': base64.b64encode(b'\x01\x00' * 4).decode()}, got)
                self.assertIn({'side': 'agent', 'audio': base64.b64encode(b'\x02\x00' * 4).decode()}, got)
                await socket.events.put({'type': 'session.closed', 'usage': {'seconds': 1}})
                await asyncio.wait_for(task, 2)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                tap.close()
                await asyncio.wait_for(listener, 1)
