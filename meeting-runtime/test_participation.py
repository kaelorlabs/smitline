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

    async def test_floor_follows_the_voice_while_silence_keeps_streaming(self):
        # GPT-Live keeps sending silence between replies, so the queue never runs dry.
        adapter, microphone, state = Adapter(), Mic(), {}
        seen = []
        participation = Participation(
            adapter, microphone, state, quiet_seconds=.03,
            on_presence=lambda: seen.append(state.get('floorState')))
        task = asyncio.create_task(participation.run())
        voice = struct.pack('<120h', *([500] * 120))
        silence = bytes(240)

        async def stream(frame, seconds):
            for _ in range(int(seconds / .01)):
                participation.offer(frame)
                await asyncio.sleep(.01)
        try:
            await stream(voice, .05)
            self.assertEqual(state['floorState'], 'speaking')
            await stream(silence, .15)
            self.assertEqual(state['floorState'], 'listening')
            self.assertFalse(participation.gate.muted)
            await stream(voice, .05)
            self.assertEqual(state['floorState'], 'speaking')
            self.assertEqual(seen[-3:], ['speaking', 'listening', 'speaking'])
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


class HostAskingAdapter(Adapter):
    """Self-unmute is disabled by the host; the host can still ask Smitline to unmute."""

    def __init__(self):
        super().__init__()
        self.block = True
        self.host_asked = False
        self.accepted = 0

    async def accept_unmute_request(self):
        if not self.host_asked:
            return False
        self.host_asked = False
        self.accepted += 1
        self.state = 'open'
        return True


class Events:
    def __init__(self):
        self.events = []

    def event(self, kind, **kwargs):
        self.events.append(kind)

    def transcript(self, *args, **kwargs):
        pass


class HostUnmuteRequestTests(unittest.IsolatedAsyncioTestCase):
    async def test_host_request_arms_a_blocked_microphone(self):
        adapter, microphone, state, record = HostAskingAdapter(), Mic(), {}, Events()
        participation = Participation(adapter, microphone, state, quiet_seconds=.03, record=record)
        task = asyncio.create_task(participation.run())
        voice = struct.pack('<120h', *([500] * 120))
        try:
            await asyncio.sleep(.02)
            self.assertEqual(state['microphoneState'], 'blocked')
            self.assertFalse(await participation.platform_microphone_changed('muted'))
            participation.offer(voice)
            await asyncio.sleep(.05)
            self.assertEqual(microphone.data, [])
            adapter.host_asked = True
            self.assertTrue(await participation.platform_microphone_changed('muted'))
            self.assertEqual(state['microphoneState'], 'open')
            self.assertNotIn('error', state)
            self.assertEqual(record.events, ['host_unmute_accepted'])
            participation.offer(voice)
            await asyncio.sleep(.15)
            self.assertEqual(microphone.data, [voice])
            self.assertEqual(adapter.opens, 0)
            self.assertEqual(adapter.state, 'open')
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_no_request_or_unknown_state_never_unmutes(self):
        adapter, state = HostAskingAdapter(), {}
        participation = Participation(adapter, Mic(), state)
        adapter.host_asked = True
        self.assertFalse(await participation.accept_host_unmute('unknown'))
        self.assertEqual(adapter.accepted, 0)
        plain = Participation(Adapter(), Mic(), {})
        self.assertFalse(await plain.accept_host_unmute('muted'))

    async def test_host_mute_after_acceptance_is_still_respected(self):
        adapter, microphone, state = HostAskingAdapter(), Mic(), {}
        participation = Participation(adapter, microphone, state, quiet_seconds=.03)
        adapter.host_asked = True
        self.assertTrue(await participation.platform_microphone_changed('muted'))
        adapter.state = 'muted'
        self.assertTrue(await participation.platform_microphone_changed('muted'))
        self.assertEqual(state['floorState'], 'platform_muted')
        self.assertFalse(participation.platform_ready)

    async def test_bridge_accepts_host_request_then_speaks_the_disclosure(self):
        import bridge
        from runtime_config import RuntimeConfig

        socket = FakeLiveSocket(bridge)
        adapter, microphone, record = HostAskingAdapter(), Mic(), Events()
        runtime = RuntimeConfig.from_environ({'SMITLINE_OWNER_NAME': 'Robin'})

        async def wait_until(check):
            for _ in range(200):
                if check():
                    return
                await asyncio.sleep(.01)
            self.fail('Timed out waiting for bridge event')

        def intros():
            return [event for event in socket.sent if event['type'] == 'session.commentary.append'
                    and "Robin's AI assistant" in event['content']]

        with patch.object(bridge, 'ClientSession', socket.client), \
                patch.object(bridge, 'record', record), \
                patch.object(bridge, 'stop', asyncio.Event()):
            task = asyncio.create_task(bridge.run_voice(
                IdleSpeaker(), microphone, None, 'test-only', runtime, adapter))
            try:
                await socket.events.put({'type': 'session.started'})
                await asyncio.sleep(.3)
                self.assertEqual(intros(), [])
                adapter.host_asked = True
                await wait_until(lambda: intros())
                await asyncio.sleep(.3)
                self.assertEqual(len(intros()), 1)
                self.assertEqual(adapter.accepted, 1)
                self.assertIn('host_unmute_accepted', record.events)
                self.assertIn('ai_disclosure_cued', record.events)
                await socket.events.put({'type': 'session.closed', 'usage': {'seconds': 1}})
                await asyncio.wait_for(task, 2)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)


class IdleSpeaker:
    async def read(self):
        await asyncio.Event().wait()


class FakeLiveSocket:
    """A GPT-Live WebSocket stand-in for bridge.run_voice."""

    def __init__(self, bridge):
        self.bridge = bridge
        self.events = asyncio.Queue()
        self.sent = []
        socket = self

        class Client:
            def __init__(self, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

            def ws_connect(self, *args, **kwargs):
                return socket

        self.client = Client

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
        return SimpleNamespace(type=self.bridge.WSMsgType.TEXT, json=lambda: value)

    async def close(self):
        pass


class RecordingArchive:
    def __init__(self):
        self.events = []
        self.transcripts = []

    def event(self, kind, **kwargs):
        self.events.append((kind, kwargs))

    def transcript(self, speaker, text, muted, **kwargs):
        self.transcripts.append((speaker, text, muted, kwargs))


class BridgeBackendTests(unittest.IsolatedAsyncioTestCase):
    async def test_responses_delegation_transcripts_and_backend_usage(self):
        import bridge
        from runtime_config import RuntimeConfig

        socket = FakeLiveSocket(bridge)
        adapter, microphone, record = Adapter(), Mic(), RecordingArchive()
        import copy
        state = copy.deepcopy(bridge.state)
        state['muted'] = False

        async def wait_until(check):
            for _ in range(200):
                if check():
                    return
                await asyncio.sleep(.01)
            self.fail('Timed out waiting for bridge event')

        with patch.object(bridge, 'ClientSession', socket.client), \
                patch.object(bridge, 'record', record), \
                patch.object(bridge, 'state', state), \
                patch.object(bridge, 'stop', asyncio.Event()):
            task = asyncio.create_task(bridge.run_voice(
                IdleSpeaker(), microphone, None, 'test-only',
                RuntimeConfig.from_environ({'SMITLINE_MEETING_INTRO': '0'}), adapter))
            try:
                await wait_until(lambda: any(e['type'] == 'session.start' for e in socket.sent))
                start = next(e for e in socket.sent if e['type'] == 'session.start')
                self.assertEqual(start['session']['delegation']['type'], 'responses')
                self.assertFalse(start['session']['store'])
                await socket.events.put({'type': 'session.started'})
                await socket.events.put({'type': 'session.future.unknown', 'delta': 'ignore'})
                await socket.events.put({
                    'type': 'session.input_transcript.delta',
                    'delta': 'What was the Q3 number?',
                    'start_ms': 100, 'end_ms': 400, 'event_id': 'tr-live-1',
                })
                await socket.events.put({
                    'type': 'session.output_transcript.delta',
                    'delta': 'It was 800,000.', 'start_ms': 500, 'end_ms': 900,
                    'event_id': 'tr-live-2',
                })
                await socket.events.put({
                    'type': 'response.event', 'delegation_id': 'dlg-1',
                    'event': {'type': 'response.created'},
                })
                await wait_until(lambda: state['backend_status'] == 'working')
                await socket.events.put({
                    'type': 'response.event', 'delegation_id': 'dlg-1',
                    'event': {'type': 'response.completed', 'response': {
                        'usage': {'input_tokens': 120, 'output_tokens': 30,
                                  'input_tokens_details': {'cached_tokens': 20}},
                        'output': [{'type': 'web_search_call'}]}},
                })
                await wait_until(lambda: state['backend_status'] == 'idle')
                self.assertEqual(state['backend_tokens'],
                                 {'input': 120, 'cached': 20, 'output': 30, 'webSearches': 1})
                self.assertEqual(
                    [(speaker, text) for speaker, text, _muted, _extra in record.transcripts],
                    [('meeting', 'What was the Q3 number?'), ('agent', 'It was 800,000.')])
                self.assertEqual(record.transcripts[0][3]['event_id'], 'tr-live-1')
                self.assertEqual([c['speaker'] for c in state['captions']], ['meeting', 'agent'])
                await socket.events.put({'type': 'session.closed', 'usage': {'seconds': 1}})
                await asyncio.wait_for(task, 2)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
