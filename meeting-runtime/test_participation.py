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


class PacedMic(Mic):
    """Takes audio in real time, as the virtual microphone does once its buffer is full."""
    async def write(self, data):
        self.data.append(data)
        await asyncio.sleep(len(data) / 48000)


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

    async def leave(self):
        self.left = True


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
            # The talking picture ends voice_activity.RELEASE_SECONDS (0.2 s) after the last
            # audible audio written.
            await stream(silence, .3)
            self.assertEqual(state['floorState'], 'listening')
            self.assertFalse(participation.gate.muted)
            await stream(voice, .05)
            self.assertEqual(state['floorState'], 'speaking')
            self.assertEqual(seen[-3:], ['speaking', 'listening', 'speaking'])
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_silence_streaming_after_speech_does_not_count_as_speaking(self):
        # GPT-Live streams silence between replies. In the 2026-11-05 Zoom trial the queue and
        # the paced writer never ran dry, so the bot never counted as done speaking and never
        # left after its goodbye.
        adapter, microphone, state = Adapter(), PacedMic(), {}
        participation = Participation(adapter, microphone, state, quiet_seconds=.05)
        task = asyncio.create_task(participation.run())
        voice = struct.pack('<480h', *([500] * 480))
        silence = bytes(960)
        try:
            for _ in range(5):
                participation.offer(voice)
                await asyncio.sleep(.02)
            self.assertTrue(participation.speaking())
            quiet_after = None
            for frame in range(40):
                participation.offer(silence)
                await asyncio.sleep(.02)
                if quiet_after is None and not participation.speaking():
                    quiet_after = frame
            self.assertIsNotNone(quiet_after, 'still speaking after 0.8 s of streamed silence')
            self.assertLess(quiet_after, 15)
            # The gate stays open for the next reply, as before.
            self.assertFalse(participation.gate.muted)
            participation.offer(voice)
            self.assertTrue(participation.speaking())
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_the_talking_picture_follows_the_voice_as_played_not_as_delivered(self):
        # GPT-Live delivers a reply faster than it plays and then streams silence. The talking
        # picture used to end 0.8 s after the last audio delivered, here before the reply had
        # played; it now follows the 10 ms chunks the microphone writes to the meeting.
        import time
        from daily_room import DailyMicrophone
        from voice_activity import RELEASE_SECONDS, VoiceActivity

        class Device:
            def write_frames(self, frames, completion=None):
                return len(frames) // 2

        microphone = DailyMicrophone(Device())
        VoiceActivity().attach(microphone)
        microphone.start()
        state, changes = {}, []
        participation = Participation(
            Adapter(), microphone, state,
            on_presence=lambda: changes.append((time.monotonic(), state.get('floorState'))))
        task = asyncio.create_task(participation.run())
        voice = struct.pack('<2400h', *([3000] * 2400))  # 100 ms at 24 kHz
        silence = bytes(4800)
        try:
            await asyncio.sleep(.05)
            started = time.monotonic()
            for _ in range(15):  # a 1.5 s reply, delivered in 0.3 s
                participation.offer(voice)
                await asyncio.sleep(.02)
            for _ in range(20):  # then silence, in real time
                participation.offer(silence)
                await asyncio.sleep(.1)
            on = next(at for at, floor in changes if floor == 'speaking') - started
            off = next(at for at, floor in changes if floor == 'listening') - started
            self.assertLess(on, .05)  # with the first chunk written
            self.assertGreater(off, 1.5)  # not before the reply has played
            self.assertLess(off, 1.5 + .35)  # and soon after: RELEASE_SECONDS, with some slack
            self.assertEqual(RELEASE_SECONDS, .2)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await microphone.close()

    async def test_audible_speech_still_waiting_counts_however_long_it_waits(self):
        # A reply can arrive faster than it plays: it is speech until it has been written.
        clock = [100.0]
        participation = Participation(Adapter(), Mic(), {}, quiet_seconds=.5,
                                      clock=lambda: clock[0])
        voice = struct.pack('<480h', *([500] * 480))
        for _ in range(3):
            participation.offer(voice)
        participation.offer(bytes(960))
        clock[0] += 30
        self.assertTrue(participation.speaking())
        participation._discard_pending()
        self.assertFalse(participation.speaking())
        self.assertEqual(participation.audible_queued, 0)

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
        runtime = RuntimeConfig.from_environ({'SMITLINE_OWNER_NAME': 'Robin', 'SMITLINE_MEETING_INTRO': '1'})

        async def wait_until(check):
            for _ in range(200):
                if check():
                    return
                await asyncio.sleep(.01)
            self.fail('Timed out waiting for bridge event')

        def intros():
            return [event for event in socket.sent if event['type'] == 'session.commentary.append'
                    and "Robin's AI assistant" in event['content']]

        import meeting_intro
        with patch.object(bridge, 'ClientSession', socket.client), \
                patch.object(bridge, 'record', record), \
                patch.object(bridge, 'stop', asyncio.Event()), \
                patch.object(meeting_intro, 'INTRO_QUIET_SECONDS', 0.1):
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


def pcm(value, ms):
    """`ms` of 24 kHz audio at one sample value (0 for silence)."""
    return struct.pack(f'<{24 * ms}h', *([value] * (24 * ms)))


async def until(check, seconds=3.0):
    for _ in range(int(seconds * 100)):
        if check():
            return
        await asyncio.sleep(.01)
    raise AssertionError('timed out')


class InterruptionTests(unittest.IsolatedAsyncioTestCase):
    """Talked over, the meeting voice stops, as a phone call does: GPT-Live stops generating but
    sends no event, and what it already made can be seconds ahead of what has been heard."""

    async def start(self, microphone=None):
        self.adapter, self.microphone = Adapter(), microphone or PacedMic()
        self.state, self.record = {}, RecordingArchive()
        self.participation = Participation(self.adapter, self.microphone, self.state,
                                           quiet_seconds=.05, record=self.record)
        self.task = asyncio.create_task(self.participation.run())
        self.addAsyncCleanup(self.stop)

    async def stop(self):
        self.task.cancel()
        await asyncio.gather(self.task, return_exceptions=True)

    def voiced_written(self):
        return sum(len(data) for data in self.microphone.data if any(data))

    async def say(self, seconds):
        """GPT-Live's reply arrives at once, in 100 ms pieces; wait until it is being heard."""
        for _ in range(int(seconds * 10)):
            self.participation.offer(pcm(900, 100))
        await until(lambda: self.voiced_written() > 0)

    async def test_talked_over_it_drops_what_it_has_not_said(self):
        await self.start()
        await self.say(2.0)
        self.assertGreater(self.participation.unsaid_seconds(), 1.5)
        self.participation.heard(0, 300)
        self.participation.heard(300, 700)  # they have talked for 700 ms
        await asyncio.sleep(self.participation.interrupt_settle + .1)
        self.assertEqual(self.participation.unsaid_seconds(), 0)
        written = self.voiced_written()
        await asyncio.sleep(.3)
        self.assertLessEqual(self.voiced_written(), written)  # nothing more of it is heard
        self.assertLess(written, 48000)  # well under a second of the two
        self.assertEqual(self.state['interruptionsFollowed'], 1)
        (kind, details), = [event for event in self.record.events if event[0] == 'interrupted']
        self.assertGreater(details['unsaid_ms'], 1000)
        self.assertEqual(self.state['floorState'], 'listening')
        # Its next reply is heard as usual.
        self.participation.offer(pcm(700, 100))
        await until(lambda: any(data == pcm(700, 100) for data in self.microphone.data))

    async def test_a_short_mhm_does_not_stop_it(self):
        await self.start()
        await self.say(1.5)
        self.participation.heard(0, 200)
        await asyncio.sleep(self.participation.interrupt_settle + .1)
        self.assertGreater(self.participation.unsaid_seconds(), .5)
        self.assertEqual(self.state['interruptionsFollowed'], 0)

    async def test_talk_it_carries_on_over_does_not_stop_it(self):
        await self.start()
        await self.say(1.0)
        self.participation.heard(0, 800)
        await asyncio.sleep(.1)
        self.participation.offer(pcm(900, 100))  # GPT-Live keeps talking: it chose to
        await asyncio.sleep(self.participation.interrupt_settle)
        self.assertEqual(self.state['interruptionsFollowed'], 0)
        self.assertGreater(self.participation.unsaid_seconds(), .3)

    async def test_their_words_start_anew_after_the_voices_own(self):
        await self.start()
        await self.say(1.5)
        self.participation.heard(0, 300)
        self.participation.said()  # the voice spoke in between: not one long interruption
        self.participation.heard(900, 1100)
        await asyncio.sleep(self.participation.interrupt_settle + .1)
        self.assertEqual(self.state['interruptionsFollowed'], 0)

    async def test_little_left_to_say_is_left_alone(self):
        await self.start()
        self.participation.offer(pcm(900, 100))
        await until(lambda: self.voiced_written() > 0)
        self.participation.heard(0, 900)
        await asyncio.sleep(self.participation.interrupt_settle + .1)
        self.assertEqual(self.state['interruptionsFollowed'], 0)

    async def test_silence_after_the_last_speech_is_not_unsaid(self):
        participation = Participation(Adapter(), Mic(), {})
        for piece in (pcm(900, 100), pcm(0, 100), pcm(900, 100), pcm(0, 200)):
            participation.queue.put_nowait((piece, any(piece)))
        self.assertAlmostEqual(participation.unsaid_seconds(), .3)


class _Pipe:
    """The PulseAudio pipe a browser's virtual microphone writes to."""

    def __init__(self):
        self.data = []

    def write(self, chunk):
        self.data.append(bytes(chunk))

    async def drain(self):
        pass


def browser_microphone():
    """joinly's VirtualMicrophone, pacing 10 ms chunks into a pipe, without PulseAudio."""
    from joinly.providers.browser.devices.virtual_microphone import VirtualMicrophone
    microphone = VirtualMicrophone(sample_rate=24000, byte_depth=2)
    pipe = _Pipe()
    microphone._writer = pipe
    microphone._queue = asyncio.Queue(maxsize=microphone.queue_size)
    microphone._pace_task = asyncio.create_task(microphone._pace_loop())
    return microphone, pipe


class PacedSpeaker:
    """The room's audio in 20 ms chunks, in real time: `loud_for` seconds of someone talking, then
    silence."""

    def __init__(self, loud_for=0.0, value=3000):
        self.loud_until = None
        self.loud_for, self.value = loud_for, value
        self.next_at = None

    async def read(self):
        loop = asyncio.get_running_loop()
        now = loop.time()
        if self.next_at is None:
            self.next_at, self.loud_until = now, now + self.loud_for
        self.next_at += 0.02
        await asyncio.sleep(max(0, self.next_at - now))
        loud = self.next_at <= self.loud_until
        return SimpleNamespace(data=pcm(self.value if loud else 0, 20))


class MeetingVoiceTests(unittest.IsolatedAsyncioTestCase):
    """bridge.run_voice with the room's audio, the voice and the hello, on both kinds of meeting."""

    async def run_meeting(self, script, *, microphone=None, speaker=None, adapter=None, env=None,
                          patches=()):
        import contextlib
        import copy
        import bridge
        from runtime_config import RuntimeConfig
        socket = FakeLiveSocket(bridge)
        record = RecordingArchive()
        state = copy.deepcopy(bridge.state)
        state.update(input_bytes=0)  # other tests run the voice on the module's own state
        state.pop('input', None)
        stop = asyncio.Event()
        runtime = RuntimeConfig.from_environ(env or {'SMITLINE_MEETING_INTRO': '0'})
        with contextlib.ExitStack() as stack:
            for target, name, value in (
                    (bridge, 'ClientSession', socket.client), (bridge, 'record', record),
                    (bridge, 'state', state), (bridge, 'stop', stop), *patches):
                stack.enter_context(patch.object(target, name, value))
            task = asyncio.create_task(bridge.run_voice(
                speaker or IdleSpeaker(), microphone or Mic(), None, 'test-only', runtime,
                adapter or Adapter()))
            try:
                await socket.events.put({'type': 'session.started'})
                await script(socket, state, record)
                stop.set()
                await until(lambda: any(e['type'] == 'session.close' for e in socket.sent), 5)
                await socket.events.put({'type': 'session.closed', 'usage': {'seconds': 3}})
                await asyncio.wait_for(task, 3)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        return SimpleNamespace(socket=socket, record=record, state=state)

    async def talked_over(self, microphone, written):
        """GPT-Live sends three seconds of reply at once, someone talks over it for 800 ms, and
        GPT-Live then sends only the silence it streams between replies."""
        reply = base64.b64encode(pcm(900, 200)).decode()
        silence = base64.b64encode(pcm(0, 200)).decode()
        heard = {}

        async def script(socket, state, record):
            for _ in range(15):
                await socket.events.put({'type': 'session.output_audio.delta', 'delta': reply})
            await socket.events.put({'type': 'session.output_transcript.delta',
                                     'delta': 'Here is the long answer you asked for.',
                                     'start_ms': 0, 'end_ms': 3000})
            await until(lambda: written() >= 0.2 * 48000)
            await socket.events.put({'type': 'session.input_transcript.delta',
                                     'delta': 'Sorry, wait,', 'start_ms': 3200, 'end_ms': 3600})
            await socket.events.put({'type': 'session.input_transcript.delta',
                                     'delta': ' one thing first.', 'start_ms': 3600,
                                     'end_ms': 4000})
            for _ in range(3):
                await socket.events.put({'type': 'session.output_audio.delta', 'delta': silence})
                await asyncio.sleep(.2)
            heard['soon_after'] = written()
            await asyncio.sleep(.4)
            heard['later'] = written()
        run = await self.run_meeting(script, microphone=microphone)
        return run, heard

    async def test_a_daily_room_voice_stops_when_talked_over(self):
        from daily_room import DailyMicrophone
        from test_daily_room import FakeDevice
        device = FakeDevice('microphone', 'mic')
        microphone = DailyMicrophone(device)
        microphone.start()
        self.addAsyncCleanup(microphone.close)

        def written():
            return sum(len(frame) for frame in device.written if any(frame))
        run, heard = await self.talked_over(microphone, written)
        self.assertEqual(run.state['interruptionsFollowed'], 1)
        self.assertIn('interrupted', [kind for kind, _ in run.record.events])
        self.assertLessEqual(heard['later'] - heard['soon_after'], 960)  # at most 10 ms more
        self.assertLess(heard['later'], 1.5 * 48000)  # of a three-second reply

    async def test_a_browser_meetings_voice_stops_when_talked_over(self):
        import bridge
        from runtime_config import RuntimeConfig
        microphone, pipe = browser_microphone()

        async def stop_pacing():
            microphone._pace_task.cancel()
            await asyncio.gather(microphone._pace_task, return_exceptions=True)
        self.addAsyncCleanup(stop_pacing)
        runtime = RuntimeConfig.from_environ({'SMITLINE_MEETING_CAMERA_STYLE': 'animated'})
        writer = bridge.camera_feed_for(runtime, microphone).audio_writer  # as main() hands it on
        self.assertIsNot(writer, microphone)

        def written():
            return sum(len(chunk) for chunk in pipe.data if any(chunk))
        run, heard = await self.talked_over(writer, written)
        self.assertEqual(run.state['interruptionsFollowed'], 1)
        self.assertLessEqual(heard['later'] - heard['soon_after'], 960)
        self.assertLess(heard['later'], 1.5 * 48000)

    async def test_the_hello_waits_for_a_pause_in_the_rooms_audio(self):
        # Transcripts trail the audio by a second or more; going by them alone, the hello cut in
        # over people still talking. Here someone talks for 1.2 s and no words arrive at all.
        import meeting_intro
        loop = asyncio.get_running_loop()
        times = {}

        def hello(socket):
            return [e for e in socket.sent if e['type'] == 'session.commentary.append']

        async def script(socket, state, record):
            times['live'] = loop.time()
            await until(lambda: hello(socket), 5)
            times['hello'] = loop.time()
        await self.run_meeting(
            script, speaker=PacedSpeaker(loud_for=1.2),
            env={'SMITLINE_MEETING_INTRO': '1', 'SMITLINE_OWNER_NAME': 'Robin'},
            patches=[(meeting_intro, 'INTRO_QUIET_SECONDS', 0.3)])
        self.assertGreater(times['hello'] - times['live'], 1.2 + 0.3 - 0.05)

    async def test_a_room_that_is_heard_records_what_came_in(self):
        async def script(socket, state, record):
            await until(lambda: state.get('input', {}).get('voiced_ms', 0) >= 200)
        run = await self.run_meeting(script, speaker=PacedSpeaker(loud_for=5))
        self.assertGreater(run.state['input']['peak'], 2000)
        (details,) = [details for kind, details in run.record.events if kind == 'input_audio']
        self.assertGreaterEqual(details['voiced_ms'], 200)
        self.assertEqual(details['bytes'], run.state['input_bytes'])
        self.assertNotIn('input_silent', [kind for kind, _ in run.record.events])

    async def test_people_talking_to_a_deaf_room_are_recorded_once(self):
        import meeting_input

        class TalkingAdapter(Adapter):
            speaker_poll_seconds = .05

            async def get_active_speaker(self):
                return 'Arun'

        async def script(socket, state, record):
            await until(lambda: any(kind == 'input_silent' for kind, _ in record.events))
            await asyncio.sleep(.3)
        run = await self.run_meeting(
            script, speaker=PacedSpeaker(loud_for=0), adapter=TalkingAdapter(),
            patches=[(meeting_input, 'INPUT_SILENT_SECONDS', 0.4)])
        silent = [details for kind, details in run.record.events if kind == 'input_silent']
        self.assertEqual(len(silent), 1)
        self.assertEqual(silent[0]['peak'], 0)
        (details,) = [details for kind, details in run.record.events if kind == 'input_audio']
        self.assertEqual(details['voiced_ms'], 0)

    async def test_process_stopped_says_what_came_in(self):
        import bridge
        state = {'stage': 'finished', 'finalized': True, 'output_bytes': 9600, 'input_bytes': 960,
                 'input': {'bytes': 960, 'voiced_ms': 0, 'peak': 0, 'dropped_ms': 0},
                 'interruptionsFollowed': 2}
        details = bridge.process_stopped_details(state)
        self.assertEqual(details['input'], {'bytes': 960, 'voiced_ms': 0, 'peak': 0, 'dropped_ms': 0})
        self.assertEqual(details['output_audio_bytes'], 9600)
        self.assertEqual(details['interruptions_followed'], 2)
        # A meeting whose voice never started still says how much came in: nothing.
        self.assertEqual(bridge.process_stopped_details(
            {'stage': 'needs_attention', 'finalized': False})['input'], {'bytes': 0})


class BridgeNameTests(unittest.IsolatedAsyncioTestCase):
    async def test_joinly_knows_the_name_the_bot_joined_under(self):
        # Meet and Teams find the active speaker by skipping the bot's own tile, which they
        # look up by JOINLY_NAME; another owner's meeting joins under that owner's name.
        import json
        import os
        import tempfile
        from pathlib import Path
        import bridge
        from joinly.settings import get_settings

        class Stop(Exception):
            pass
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp) / 'runtime.json'
            state.write_text(json.dumps({'version': 1, 'meetingId': 'mtg-name00000001',
                                         'meetingUrl': 'https://zoom.us/j/123456789',
                                         'participantName': "Ana's assistant"}))
            with patch.dict(os.environ, {'SMITLINE_RUNTIME_STATE': str(state)}), \
                    patch.object(bridge, 'meeting_state_from_environ', side_effect=Stop):
                with self.assertRaises(Stop):
                    await bridge.main()
                self.assertEqual(get_settings().name, "Ana's assistant")


class AgentLeavesTests(unittest.IsolatedAsyncioTestCase):
    """The meeting agent leaves by itself; leaving closes GPT-Live, so its costs stop."""

    async def _run(self, runtime, script, microphone=None, adapter=None):
        import copy
        import bridge
        socket = FakeLiveSocket(bridge)
        record = RecordingArchive()
        state = copy.deepcopy(bridge.state)
        stop = asyncio.Event()
        stages = []

        def stage(value):
            stages.append(value)
            state['stage'] = value

        async def wait_until(check, seconds=2):
            for _ in range(int(seconds * 100)):
                if check():
                    return
                await asyncio.sleep(.01)
            self.fail('Timed out waiting for bridge event')

        with patch.object(bridge, 'ClientSession', socket.client), \
                patch.object(bridge, 'record', record), \
                patch.object(bridge, 'state', state), \
                patch.object(bridge, 'stage', stage), \
                patch.object(bridge, 'stop', stop):
            task = asyncio.create_task(bridge.run_voice(
                IdleSpeaker(), microphone or Mic(), None, 'test-only', runtime, adapter or Adapter()))
            try:
                await socket.events.put({'type': 'session.started'})
                await script(socket, wait_until)
                await wait_until(lambda: any(e['type'] == 'session.close' for e in socket.sent), 15)
                await socket.events.put({'type': 'session.closed', 'usage': {'seconds': 42}})
                await asyncio.wait_for(task, 3)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        return SimpleNamespace(socket=socket, record=record, state=state, stop=stop, stages=stages)

    @staticmethod
    def leave_call(reason):
        import json
        return {'type': 'response.event', 'delegation_id': 'dlg-9', 'event': {
            'type': 'response.output_item.done', 'item': {
                'type': 'function_call', 'call_id': 'fc-9', 'name': 'leave_meeting',
                'arguments': json.dumps({'reason': reason})}}}

    async def test_asked_to_leave_says_goodbye_then_leaves_and_closes_the_session(self):
        from runtime_config import RuntimeConfig
        import bridge
        runtime = RuntimeConfig.from_environ({'SMITLINE_MEETING_INTRO': '0'})
        goodbye = base64.b64encode(struct.pack('<480h', *([900] * 480))).decode()
        closed_while_speaking = []

        async def script(socket, _wait_until):
            await socket.events.put({'type': 'session.input_transcript.delta',
                                     'delta': 'Thanks Smitline, you can drop off now.',
                                     'start_ms': 0, 'end_ms': 900, 'event_id': 'in-1'})
            await socket.events.put(self.leave_call('asked_to_leave'))
            # The goodbye keeps arriving for two seconds after the tool call; the agent
            # stays until it has been heard.
            for _ in range(8):
                await socket.events.put({'type': 'session.output_audio.delta', 'delta': goodbye})
                await asyncio.sleep(.25)
                closed_while_speaking.append(
                    any(e['type'] == 'session.close' for e in socket.sent))

        with patch.object(bridge, 'LEAVE_SPEECH_TIMEOUT', 10.0):
            run = await self._run(runtime, script)
        self.assertEqual(set(closed_while_speaking), {False})
        self.assertEqual(run.state['endReason'], 'left_asked_to_leave')
        self.assertIn(('agent_left', {'reason': 'asked_to_leave'}), run.record.events)
        self.assertEqual(run.stages[-2:], ['meeting_ended', 'finished'])
        self.assertTrue(run.stop.is_set())
        # Leaving needs no tool result, so no further backend turn is asked for.
        self.assertFalse([e for e in run.socket.sent if 'function_call_output' in str(e)])
        # The session reported its final usage: the meeting is billed up to here.
        self.assertTrue(run.state['finalized'])
        self.assertEqual(run.state['usage_seconds'], 42)
        self.assertEqual(sum(e['type'] == 'session.close' for e in run.socket.sent), 1)

    async def test_leaves_after_its_goodbye_while_gpt_live_streams_silence(self):
        # The 2026-11-05 Zoom trial: asked to hang up, the voice said goodbye without asking its
        # backend to leave, and GPT-Live went on streaming silence. The goodbye alone must end it.
        from runtime_config import RuntimeConfig
        runtime = RuntimeConfig.from_environ({'SMITLINE_MEETING_INTRO': '0'})
        # 200 ms pieces arriving a little faster than they play, as GPT-Live's do: the paced
        # writer never runs dry, which kept the gate open for the whole trial meeting.
        speech = base64.b64encode(struct.pack('<4800h', *([900] * 4800))).decode()
        silence = base64.b64encode(bytes(9600)).decode()

        async def script(socket, _wait_until):
            await socket.events.put({'type': 'session.input_transcript.delta',
                                     'delta': 'Hmm, okay. Hang up, please. You can hang up now.',
                                     'start_ms': 0, 'end_ms': 900, 'event_id': 'in-1'})
            for number, words in enumerate((' Got it', ', leaving now.', ' Bye.')):
                await socket.events.put({'type': 'session.output_transcript.delta', 'delta': words,
                                         'start_ms': 1000 + number * 200,
                                         'end_ms': 1200 + number * 200, 'event_id': f'out-{number}'})
                await socket.events.put({'type': 'session.output_audio.delta', 'delta': speech})
                await asyncio.sleep(.18)
            for _ in range(45):  # 8 s of GPT-Live's silence
                if any(e['type'] == 'session.close' for e in socket.sent):
                    break
                await socket.events.put({'type': 'session.output_audio.delta', 'delta': silence})
                await asyncio.sleep(.18)
            # It must leave while the silence still streams, not once it stops.
            self.assertTrue(any(e['type'] == 'session.close' for e in socket.sent),
                            'still in the meeting after 8 s of streamed silence')
        run = await self._run(runtime, script, microphone=PacedMic())
        self.assertEqual(run.state['endReason'], 'left_asked_to_leave')
        self.assertIn(('goodbye_leave', {}), run.record.events)
        self.assertIn(('agent_left', {'reason': 'asked_to_leave'}), run.record.events)

    async def test_the_voice_is_told_who_is_speaking_and_the_transcript_names_them(self):
        import bridge
        from runtime_config import RuntimeConfig
        runtime = RuntimeConfig.from_environ({'SMITLINE_MEETING_INTRO': '0',
                                              'SMITLINE_OWNER_NAME': 'Arun Raj'})
        # Arun talks, pauses, talks again (told once), then Priya takes the floor.
        speakers = iter(['Arun Raj'] * 4 + [None] * 3 + ['Arun Raj'] * 4 + ['Priya'] * 4
                        + ['Smitline AI'] * 4)

        class SpeakingAdapter(Adapter):
            speaker_poll_seconds = .05

            async def get_active_speaker(self):
                return next(speakers, None)

        def told(socket):
            return [e['content'] for e in socket.sent
                    if e.get('type') == 'session.thinking.append' and 'is speaking now' in e['content']]

        async def script(socket, wait_until):
            await wait_until(lambda: len(told(socket)) >= 2, 5)
            await asyncio.sleep(.5)  # the agent's own name never counts as a speaker
            await socket.events.put({'type': 'session.input_transcript.delta',
                                     'delta': 'Thanks, everyone.', 'start_ms': 0, 'end_ms': 500,
                                     'event_id': 'in-9'})
            await asyncio.sleep(.2)
            await socket.events.put(self.leave_call('completed'))
        with patch.object(bridge, 'LEAVE_SPEECH_TIMEOUT', 1.0), \
                patch.object(bridge, 'SPEAKER_HOLD_SECONDS', .1):
            run = await self._run(runtime, script, adapter=SpeakingAdapter())
        notes = told(run.socket)
        self.assertEqual(len(notes), 2)
        self.assertTrue(notes[0].startswith(
            'Arun Raj (probably Arun, the person you work for) is speaking now.'))
        self.assertTrue(notes[1].startswith('Priya is speaking now.'))
        meeting_lines = [extra for speaker, _text, _muted, extra in run.record.transcripts
                         if speaker == 'meeting']
        self.assertEqual(meeting_lines[-1].get('name'), 'Priya')
        self.assertEqual(run.state['captions'][-1].get('name'), 'Priya')

    async def test_the_archive_says_who_the_meeting_showed(self):
        # The call's result says who was there, also people who never spoke, from the archive.
        import bridge
        from runtime_config import RuntimeConfig
        runtime = RuntimeConfig.from_environ({'SMITLINE_MEETING_INTRO': '0'})

        class Listed(Adapter):
            counted = 0

            async def get_participant_names(self):
                return ['Smitline AI', 'Ana Diaz (Host)']

            async def get_participant_count(self):
                Listed.counted += 1
                return 2  # everyone, itself included
        adapter = Listed()

        async def script(socket, wait_until):
            await wait_until(lambda: Listed.counted >= 2, 8)  # two readings, one count recorded
            await socket.events.put(self.leave_call('completed'))
        with patch.object(bridge, 'LEAVE_SPEECH_TIMEOUT', 1.0):
            run = await self._run(runtime, script, adapter=adapter)
        self.assertIn(('participants', {'count': 1, 'names': ['Ana Diaz']}), run.record.events)
        counts = [data for kind, data in run.record.events if kind == 'participant_count']
        self.assertEqual(counts, [{'count': 2}])

    async def test_a_refused_join_is_noted_as_a_word_for_the_result(self):
        import copy
        import bridge
        from adapters_base import AuthenticationRequired, GuestRefused
        record, state = RecordingArchive(), copy.deepcopy(bridge.state)
        with patch.object(bridge, 'record', record), patch.object(bridge, 'state', state):
            self.assertIsNone(bridge.note_join_refused(RuntimeError('Zoom meeting ended or is invalid')))
            self.assertNotIn('joinRefused', state)
            self.assertEqual(bridge.note_join_refused(GuestRefused("Google said: You can't join")),
                             'guest_refused')
            self.assertEqual(state['joinRefused'], 'guest_refused')
            self.assertEqual(bridge.note_join_refused(
                AuthenticationRequired('Teams lets only people with access in', kind='restricted')),
                'restricted')
        self.assertEqual(record.events, [('join_refused', {'refusal': 'guest_refused'}),
                                         ('join_refused', {'refusal': 'restricted'})])

    async def test_a_second_leave_call_is_ignored_and_unknown_reasons_are_other(self):
        from runtime_config import RuntimeConfig
        import bridge
        runtime = RuntimeConfig.from_environ({'SMITLINE_MEETING_INTRO': '0'})

        async def script(socket, _wait_until):
            await socket.events.put(self.leave_call('made_up'))
            await socket.events.put(self.leave_call('completed'))
        with patch.object(bridge, 'LEAVE_SPEECH_TIMEOUT', 1.0):
            run = await self._run(runtime, script)
        self.assertEqual(run.state['endReason'], 'left_other')
        self.assertEqual([kind for kind, _ in run.record.events].count('agent_left'), 1)

    async def test_waits_for_speech_before_leaving_but_not_forever(self):
        from participation import Participation
        state = {}
        clock = [0.0]
        participation = Participation(Adapter(), Mic(), state, quiet_seconds=.5,
                                      clock=lambda: clock[0])
        participation.last_output_audio = 0.0
        self.assertTrue(participation.speaking())
        loop = asyncio.get_running_loop()
        started = loop.time()
        await participation.finish_speaking(.3, settle=.05)  # still "speaking": gives up
        self.assertGreaterEqual(loop.time() - started, .29)
        clock[0] = 10.0
        self.assertFalse(participation.speaking())
        started = loop.time()
        await participation.finish_speaking(5, settle=.05)
        self.assertLess(loop.time() - started, 1)

    async def test_leaves_when_nobody_has_spoken_for_the_idle_limit(self):
        import dataclasses
        from runtime_config import RuntimeConfig
        runtime = dataclasses.replace(
            RuntimeConfig.from_environ({'SMITLINE_MEETING_INTRO': '0'}), idle_leave_seconds=1)

        async def script(_socket, _wait_until):
            pass
        run = await self._run(runtime, script)
        self.assertEqual(run.state['endReason'], 'idle')
        self.assertIn(('auto_leave', {'reason': 'idle'}), run.record.events)
        self.assertTrue(run.state['finalized'])

    async def test_speech_keeps_an_idle_meeting_open(self):
        import dataclasses
        import bridge
        from runtime_config import RuntimeConfig
        runtime = dataclasses.replace(
            RuntimeConfig.from_environ({'SMITLINE_MEETING_INTRO': '0'}), idle_leave_seconds=4)

        async def script(socket, _wait_until):
            for second in range(5):
                await asyncio.sleep(1)
                await socket.events.put({'type': 'session.input_transcript.delta',
                                         'delta': f'still talking {second}', 'start_ms': 0,
                                         'end_ms': 10, 'event_id': f'talk-{second}'})
            self.assertFalse(any(e['type'] == 'session.close' for e in socket.sent))
            await socket.events.put(self.leave_call('completed'))
        with patch.object(bridge, 'LEAVE_SPEECH_TIMEOUT', 1.0):
            run = await self._run(runtime, script)
        self.assertEqual(run.state['endReason'], 'left_completed')
