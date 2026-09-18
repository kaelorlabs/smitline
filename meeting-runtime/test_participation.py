import asyncio
import base64
import struct
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from participation import Participation


class Mic:
    def __init__(self):
        self._queue = asyncio.Queue()
        self.data = []

    async def write(self, data):
        self.data.append(data)


class WrappedMic:
    """Matches Joinly's amplitude writer: no private queue on the wrapper."""
    def __init__(self):
        self.data = []
        self.drains = 0

    async def write(self, data):
        self.data.append(data)

    async def drain(self):
        self.drains += 1

    def discard_pending(self):
        pass


class Adapter:
    def __init__(self):
        self.state = 'muted'
        self.opens = 0
        self.block = False

    async def mute(self):
        self.state = 'muted'

    async def unmute(self):
        if self.block:
            raise RuntimeError('host disabled microphone')
        self.state = 'open'
        self.opens += 1

    async def get_microphone_state(self):
        return self.state

    async def get_active_speaker(self):
        return None

    async def send_chat_message(self, text):
        return {'status': 'submitted'}

    async def has_ended(self):
        return False

    async def chat_available(self):
        return False

    async def get_participant_count(self):
        return 2


class ParticipationTests(unittest.IsolatedAsyncioTestCase):
    async def test_wrapped_microphone_drains_without_private_queue_access(self):
        adapter, microphone, state = Adapter(), WrappedMic(), {}
        participation = Participation(adapter, microphone, state, quiet_seconds=.03)
        task = asyncio.create_task(participation.run())
        try:
            voice = struct.pack('<120h', *([500] * 120))
            participation.offer(voice)
            await asyncio.sleep(.15)
            self.assertFalse(task.done())
            self.assertEqual(microphone.data, [voice])
            self.assertGreaterEqual(microphone.drains, 1)
            self.assertTrue(participation.gate.muted)
            self.assertEqual(state['floorState'], 'listening')
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_reply_uses_stable_platform_connection_and_virtual_gate(self):
        adapter, microphone, state = Adapter(), Mic(), {}
        participation = Participation(adapter, microphone, state, quiet_seconds=.03)
        task = asyncio.create_task(participation.run())
        try:
            voice = struct.pack('<120h', *([500] * 120))
            participation.offer(voice)
            await asyncio.sleep(.15)
            self.assertEqual(adapter.opens, 1)
            self.assertEqual(microphone.data, [voice])
            self.assertEqual(adapter.state, 'open')
            self.assertTrue(participation.gate.muted)
            self.assertTrue(state['muted'])
            self.assertEqual(state['floorState'], 'listening')
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_playback_drives_speaking_presence_then_restores_listening(self):
        adapter, microphone, state = Adapter(), Mic(), {}
        seen = []
        participation = Participation(
            adapter, microphone, state, quiet_seconds=.03,
            on_presence=lambda: seen.append(state.get('floorState')))
        task = asyncio.create_task(participation.run())
        try:
            participation.offer(struct.pack('<120h', *([500] * 120)))
            for _ in range(40):
                if 'speaking' in seen:
                    break
                await asyncio.sleep(.01)
            self.assertIn('speaking', seen)
            await asyncio.sleep(.15)
            self.assertEqual(state['floorState'], 'listening')
            self.assertEqual(seen[-1], 'listening')
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_external_platform_mute_is_respected(self):
        adapter, microphone, state = Adapter(), Mic(), {}
        participation = Participation(adapter, microphone, state, quiet_seconds=.03)
        task = asyncio.create_task(participation.run())
        try:
            await asyncio.sleep(.02)
            adapter.state = 'muted'
            changed = await participation.platform_microphone_changed('muted')
            participation.offer(struct.pack('<120h', *([500] * 120)))
            await asyncio.sleep(.05)
            self.assertTrue(changed)
            self.assertEqual(adapter.opens, 1)
            self.assertEqual(microphone.data, [])
            self.assertEqual(state['floorState'], 'platform_muted')
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_operator_unmute_resumes_playback_without_clicking_unmute(self):
        adapter, microphone, state = Adapter(), Mic(), {}
        participation = Participation(adapter, microphone, state, quiet_seconds=.03)
        task = asyncio.create_task(participation.run())
        voice = struct.pack('<120h', *([500] * 120))
        try:
            await asyncio.sleep(.02)
            adapter.state = 'muted'
            await participation.platform_microphone_changed('muted')
            participation.offer(voice)
            await asyncio.sleep(.05)
            self.assertEqual(microphone.data, [])
            self.assertEqual(adapter.opens, 1)
            adapter.state = 'open'
            await participation.platform_microphone_changed('open')
            participation.offer(voice)
            await asyncio.sleep(.15)
            self.assertEqual(microphone.data, [voice])
            self.assertEqual(adapter.opens, 1)
            self.assertEqual(state['microphoneState'], 'open')
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_blocked_microphone_never_plays(self):
        adapter, microphone, state = Adapter(), Mic(), {}
        adapter.block = True
        participation = Participation(adapter, microphone, state, quiet_seconds=.03)
        task = asyncio.create_task(participation.run())
        try:
            participation.offer(struct.pack('<120h', *([500] * 120)))
            await asyncio.sleep(.05)
            self.assertEqual(microphone.data, [])
            self.assertEqual(state['microphoneState'], 'blocked')
            self.assertGreater(state['discarded_audio_bytes'], 0)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_live_session_keeps_forwarding_meeting_audio(self):
        import bridge
        from runtime_config import RuntimeConfig

        class Socket:
            def __init__(self):
                self.events = asyncio.Queue()
                self.sent = []

            async def send_json(self, value):
                self.sent.append(value)

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

            def __aiter__(self):
                return self

            async def __anext__(self):
                value = await self.events.get()
                return SimpleNamespace(type=bridge.WSMsgType.TEXT, json=lambda: value)

            async def close(self):
                pass

        socket = Socket()

        class Client:
            def __init__(self, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

            def ws_connect(self, *args, **kwargs):
                return socket

        class Speaker:
            def __init__(self):
                self.queue = asyncio.Queue()

            async def read(self):
                return SimpleNamespace(data=await self.queue.get())

        class Record:
            def event(self, *args, **kwargs):
                pass

            def transcript(self, *args, **kwargs):
                pass

        adapter, microphone, speaker = Adapter(), Mic(), Speaker()

        async def wait_until(check):
            for _ in range(100):
                if check():
                    return
                await asyncio.sleep(.01)
            self.fail('Timed out waiting for bridge event')

        with patch.object(bridge, 'ClientSession', Client), patch.object(bridge, 'record', Record()), patch.object(bridge, 'stop', asyncio.Event()):
            task = asyncio.create_task(bridge.run_voice(speaker, microphone, None, 'test-only', RuntimeConfig.from_environ({}), adapter))
            try:
                await socket.events.put({'type': 'session.started'})
                await speaker.queue.put(b'continuous input')
                await wait_until(lambda: any(event['type'] == 'session.input_audio.append' for event in socket.sent))
                inputs = [base64.b64decode(event['audio']) for event in socket.sent if event['type'] == 'session.input_audio.append']
                self.assertEqual(inputs, [b'continuous input'])
                await socket.events.put({'type': 'session.closed', 'usage': {'seconds': 1}})
                await asyncio.wait_for(task, 2)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def test_client_delegation_keeps_forwarding_meeting_audio(self):
        import bridge
        from delegation_router import DelegationRouter
        from runtime_config import RuntimeConfig
        from test_delegation_router import FakeProvider

        class Socket:
            def __init__(self):
                self.events = asyncio.Queue()
                self.sent = []

            async def send_json(self, value):
                self.sent.append(value)

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

            def __aiter__(self):
                return self

            async def __anext__(self):
                value = await self.events.get()
                return SimpleNamespace(type=bridge.WSMsgType.TEXT, json=lambda: value)

            async def close(self):
                pass

        socket = Socket()

        class Client:
            def __init__(self, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

            def ws_connect(self, *args, **kwargs):
                return socket

        class Speaker:
            def __init__(self):
                self.queue = asyncio.Queue()

            async def read(self):
                return SimpleNamespace(data=await self.queue.get())

        class Record:
            def event(self, *args, **kwargs):
                pass

            def transcript(self, *args, **kwargs):
                pass

            directory = '/tmp'

        async def wait_until(check):
            for _ in range(200):
                if check():
                    return
                await asyncio.sleep(.01)
            self.fail('Timed out waiting for bridge event')

        provider = FakeProvider(delay=0.2)
        router = DelegationRouter(providers={'codex': provider})
        adapter, microphone, speaker = Adapter(), Mic(), Speaker()
        with patch.object(bridge, 'ClientSession', Client), patch.object(bridge, 'record', Record()), patch.object(bridge, 'stop', asyncio.Event()):
            task = asyncio.create_task(bridge.run_voice(
                speaker, microphone, None, 'test-only', RuntimeConfig.from_environ({}), adapter,
                router=router))
            try:
                await wait_until(lambda: any(event['type'] == 'session.start' for event in socket.sent))
                start = next(event for event in socket.sent if event['type'] == 'session.start')
                self.assertEqual(start['session']['delegation'], {'type': 'client'})
                self.assertFalse(start['session']['store'])
                await socket.events.put({'type': 'session.started'})
                await socket.events.put({'type': 'session.future.unknown', 'delta': 'ignore'})
                await socket.events.put({
                    'type': 'session.input_transcript.delta',
                    'delta': 'What does the worker lock do?',
                    'start_ms': 100, 'end_ms': 400, 'event_id': 'tr-live-1',
                })
                await socket.events.put({
                    'type': 'session.delegation.created',
                    'offset_ms': 400,
                    'delegation': {'id': 'item_live_1', 'type': 'delegation', 'target': 'client'},
                })
                await provider.started.wait()
                await speaker.queue.put(b'while-working')
                await wait_until(lambda: any(
                    event['type'] == 'session.input_audio.append'
                    and base64.b64decode(event['audio']) == b'while-working'
                    for event in socket.sent))
                await wait_until(lambda: any(
                    event['type'] == 'session.commentary.append'
                    and event.get('delegation_id') == 'item_live_1'
                    for event in socket.sent))
                await socket.events.put({'type': 'session.closed', 'usage': {'seconds': 1}})
                await asyncio.wait_for(task, 2)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
