import asyncio
import io
import json
import struct
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

import daily_room
from daily_room import (
    CAMERA, MICROPHONE, SUBSCRIPTIONS, ClipPlayer, DailyCamera, DailyMicrophone, DailyRoom,
    DailySpeaker, joined_room, room_and_token)
from presence_pack import Clip, Mouth, PresencePack
from visual_presence import VISUAL_STATES, apply_platform_camera

try:
    from PIL import Image
    HAS_PILLOW = True
except ImportError:
    HAS_PILLOW = False


class FakeDevice:
    def __init__(self, kind, name, **settings):
        self.kind, self.name, self.settings = kind, name, settings
        self.written = []

    def write_frames(self, frames, completion=None):
        self.written.append(bytes(frames))
        return len(frames) // 2

    def write_frame(self, frame):
        self.written.append(bytes(frame))


class FakeSpeakerDevice:
    """daily-python 0.37's non-blocking virtual speaker, as virtual_speaker_device.rs has it:
    read_frames always returns b'' at once and the audio goes to the completion only. While the
    room plays (`plays`, one entry per read: bytes, or None for nothing), each read completes
    from a thread of its own, 20 ms after the last one, as Daily's playout does. While nothing
    plays it completes at once, on the caller's thread, with b''. A read while read_frames is
    still running (from inside a completion that came at once) is refused, as daily-python does."""

    def __init__(self, name='speaker', **settings):
        self.kind, self.name, self.settings = 'speaker', name, settings
        self.plays = []
        self.period = 0.02
        self.reads = 0
        self.waiting = 0
        self.most_waiting = 0
        self.fail = None
        self.swallow = 0   # this many reads are never answered
        self.slow = []     # extra seconds before the next reads with audio are answered
        self.delivered = []
        self.waiting_at = []  # reads waiting as each piece of audio was delivered
        self._lock = threading.Lock()
        self._busy = False
        self._due = 0.0
        self._jobs = []
        self._wake = threading.Condition(self._lock)
        self._thread = None

    def read_frames(self, frames, completion=None):
        if self._busy:
            raise RuntimeError('Already borrowed')
        if self.fail is not None:
            raise self.fail
        self._busy = True
        try:
            with self._lock:
                self.reads += 1
                if completion is not None:
                    self.waiting += 1
                    self.most_waiting = max(self.most_waiting, self.waiting)
                if completion is not None and self.swallow:
                    self.swallow -= 1
                    return b''  # lost: never answered
                data = self.plays.pop(0) if self.plays else None
                if completion is not None and data is not None:
                    extra = self.slow.pop(0) if self.slow else 0
                    self._due = max(self._due, time.monotonic()) + self.period + extra
                    self._jobs.append((self._due, completion, data))
                    if self._thread is None:
                        self._thread = threading.Thread(target=self._play, daemon=True,
                                                        name='daily-speaker-p')
                        self._thread.start()
                    self._wake.notify()
            if completion is not None and data is None:
                self._finish(completion, b'')  # nothing plays: answered at once, on this thread
            return b''
        finally:
            self._busy = False

    def _finish(self, completion, data):
        with self._lock:
            if data:
                self.waiting_at.append(self.waiting)
            self.waiting -= 1
            self.delivered.append(data)
        completion(data)

    def _play(self):
        while True:
            with self._lock:
                while not self._jobs:
                    self._wake.wait()
                due, completion, data = self._jobs[0]
                delay = due - time.monotonic()
                if delay > 0:
                    self._wake.wait(delay)
                    continue
                self._jobs.pop(0)
            self._finish(completion, data)


class FakeDaily:
    def __init__(self):
        self.devices, self.selected_speaker = {}, None

    def _device(self, kind, name, **settings):
        self.devices[kind] = FakeDevice(kind, name, **settings)
        return self.devices[kind]

    def create_microphone_device(self, name, **settings):
        return self._device('microphone', name, **settings)

    def create_speaker_device(self, name, **settings):
        self.devices['speaker'] = FakeSpeakerDevice(name, **settings)
        return self.devices['speaker']

    def create_camera_device(self, name, **settings):
        return self._device('camera', name, **settings)

    def select_speaker_device(self, name):
        self.selected_speaker = name


class FakeEventHandler:
    pass


class FakeCallClient:
    """daily-python's CallClient as far as DailyRoom uses it; completions may come from a thread."""
    instances = []

    def __init__(self, event_handler=None):
        self.handler = event_handler
        self.calls = []
        self.join_error = None
        self.threaded = False
        self.inputs_now = {'camera': {'isEnabled': False}, 'microphone': {'isEnabled': False}}
        # Someone is already there unless a test empties the room.
        self.people = {'local': {'id': 'local', 'info': {'isLocal': True, 'userName': 'Smitline AI'}},
                       'guest': {'id': 'guest', 'info': {'userName': 'Smith'}}}
        self.speaker = None
        self.chat_error = None
        FakeCallClient.instances.append(self)

    def _complete(self, completion, *args):
        if self.threaded:
            threading.Thread(target=completion, args=args).start()
        else:
            completion(*args)

    def set_user_name(self, name):
        self.calls.append(('set_user_name', name))

    def update_subscription_profiles(self, profiles, completion=None):
        self.calls.append(('subscriptions', profiles))

    def join(self, url, meeting_token=None, client_settings=None, completion=None):
        self.calls.append(('join', url, meeting_token, client_settings))
        self.handler.on_call_state_updated('joining')
        if self.join_error is None:
            for kind, value in (client_settings or {}).get('inputs', {}).items():
                self.inputs_now[kind] = dict(value)
            self.handler.on_call_state_updated('joined')
        self._complete(completion, {'meetingSession': {'id': 'x'}}, self.join_error)

    def inputs(self):
        return self.inputs_now

    def update_inputs(self, inputs, completion=None):
        self.calls.append(('update_inputs', inputs))
        for kind, value in inputs.items():
            self.inputs_now[kind] = {**self.inputs_now.get(kind, {}), **value}
        self._complete(completion, None)

    publishing_error = None

    def update_publishing(self, settings, completion=None):
        self.calls.append(('update_publishing', settings))
        self._complete(completion, self.publishing_error)

    def participants(self):
        return self.people

    def active_speaker(self):
        return self.speaker

    def send_prebuilt_chat_message(self, message, user_name=None, completion=None):
        self.calls.append(('chat', message, user_name))
        self._complete(completion, self.chat_error)

    def leave(self, completion=None):
        self.calls.append(('leave',))
        self.handler.on_call_state_updated('leaving')
        self.handler.on_call_state_updated('left')
        self._complete(completion, None)

    def release(self):
        self.calls.append(('release',))


def fake_sdk():
    return SimpleNamespace(Daily=FakeDaily(), CallClient=FakeCallClient, EventHandler=FakeEventHandler)


ROOM = 'https://kaelor.daily.co/demo-room'


class DailyRoomTests(unittest.IsolatedAsyncioTestCase):
    def make_room(self, camera_size=(640, 360)):
        self.stages = []
        self.sdk = fake_sdk()
        room = DailyRoom(asyncio.Event(), self.stages.append, sdk=self.sdk, camera_size=camera_size)
        return room, room.client

    async def test_joins_with_its_devices_and_listens_to_microphones_only(self):
        room, client = self.make_room()
        await room.join(ROOM, 'Smitline AI')
        self.assertEqual(self.stages, ['opening_meeting', 'joining', 'admitted'])
        self.assertIn(('set_user_name', 'Smitline AI'), client.calls)
        self.assertIn(('subscriptions', SUBSCRIPTIONS), client.calls)
        _kind, url, token, settings = next(call for call in client.calls if call[0] == 'join')
        self.assertEqual((url, token), (ROOM, None))
        self.assertEqual(settings['inputs']['microphone']['settings']['deviceId'], MICROPHONE)
        self.assertEqual(settings['inputs']['camera'], {'isEnabled': False, 'settings': {'deviceId': CAMERA}})
        daily = self.sdk.Daily
        self.assertEqual(daily.devices['microphone'].settings,
                         {'sample_rate': 24000, 'channels': 1, 'non_blocking': True})
        self.assertEqual(daily.devices['speaker'].settings,
                         {'sample_rate': 24000, 'channels': 1, 'non_blocking': True})
        self.assertEqual(daily.selected_speaker, daily.devices['speaker'].name)
        self.assertEqual(daily.devices['camera'].settings, {'width': 640, 'height': 360, 'color_format': 'RGBA'})

    async def test_a_token_in_the_link_goes_to_daily_apart_from_the_room(self):
        self.assertEqual(room_and_token(ROOM + '?t=abc&lang=en'), (ROOM + '?lang=en', 'abc'))
        room, client = self.make_room()
        await room.join(ROOM + '?t=secret', 'Smitline AI')
        _kind, url, token, _settings = next(call for call in client.calls if call[0] == 'join')
        self.assertEqual((url, token), (ROOM, 'secret'))

    async def test_a_refused_join_says_why(self):
        room, client = self.make_room()
        client.join_error = 'not allowed'
        with self.assertRaisesRegex(RuntimeError, 'refused to join the room: not allowed'):
            await room.join(ROOM, 'Smitline AI')
        self.assertNotIn('admitted', self.stages)

    async def test_completions_from_dailys_own_threads_reach_the_event_loop(self):
        room, client = self.make_room()
        client.threaded = True
        await room.join(ROOM, 'Smitline AI')
        await room.mute()
        self.assertEqual(await room.get_microphone_state(), 'muted')

    async def test_rejects_a_link_that_is_not_a_daily_room(self):
        room, _client = self.make_room()
        with self.assertRaises(ValueError):
            await room.join('https://meet.google.com/aaa-bbbb-ccc', 'Smitline AI')

    async def test_microphone_state_follows_its_input_and_a_host_mute(self):
        room, client = self.make_room()
        self.assertEqual(await room.get_microphone_state(), 'unknown')
        await room.join(ROOM, 'Smitline AI')
        self.assertEqual(await room.get_microphone_state(), 'open')
        await room.mute()
        self.assertEqual(client.calls[-1], ('update_inputs', {'microphone': {'isEnabled': False}}))
        self.assertEqual(await room.get_microphone_state(), 'muted')
        await room.unmute()
        self.assertEqual(await room.get_microphone_state(), 'open')
        client.inputs_now['microphone']['isEnabled'] = False  # an owner muted it from the room
        self.assertEqual(await room.get_microphone_state(), 'muted')

    async def test_camera_turns_on_after_admission_like_the_browsers(self):
        room, _client = self.make_room()
        await room.join(ROOM, 'Smitline AI')
        self.assertEqual(await room.get_camera_state(), 'off')
        state = {}
        self.assertEqual(await apply_platform_camera(room, enabled=True, default_on=True, state=state), 'on')
        self.assertEqual(state['cameraState'], 'on')
        await room.disable_camera()
        self.assertEqual(await room.get_camera_state(), 'off')

    async def test_sends_the_camera_as_one_sharp_layer(self):
        # With simulcast's three layers a room was sent the 160x90 one (2026-11-05).
        room, client = self.make_room()
        await room.join(ROOM, 'Smitline AI')
        self.assertTrue(await room.send_one_sharp_layer(24))
        (_kind, settings), = [call for call in client.calls if call[0] == 'update_publishing']
        send = settings['camera']['sendSettings']
        # Daily's one-layer form: "low" alone, at full size, and nothing above it.
        self.assertEqual(send['maxQuality'], 'low')
        self.assertFalse(send['allowAdaptiveLayers'])
        self.assertEqual(list(send['encodings']), ['low'])
        self.assertEqual(send['encodings']['low']['maxFramerate'], 24)
        self.assertEqual(send['encodings']['low']['scaleResolutionDownBy'], 1)
        self.assertEqual(send['encodings']['low']['maxBitrate'], 1_500_000)
        client.publishing_error = 'not allowed'
        self.assertFalse(await room.send_one_sharp_layer(24))
        self.assertIn('camera publishing: not allowed', room.errors)
        audio_only, _client = self.make_room(camera_size=None)
        self.assertFalse(await audio_only.send_one_sharp_layer(24))

    async def test_a_1280x720_camera_gets_the_bitrate_it_needs(self):
        room, client = self.make_room(camera_size=(1280, 720))
        await room.join(ROOM, 'Smitline AI')
        self.assertTrue(await room.send_one_sharp_layer(24))
        (_kind, settings), = [call for call in client.calls if call[0] == 'update_publishing']
        self.assertEqual(settings['camera']['sendSettings']['encodings']['low']['maxBitrate'], 2_500_000)
        self.assertEqual(daily_room.camera_bitrate(640, 360), 1_500_000)

    async def test_turns_its_camera_off_while_in_the_room_only(self):
        room, client = self.make_room()
        await room.turn_camera_off()  # not in the room yet: nothing
        await room.join(ROOM, 'Smitline AI')
        await room.enable_camera()
        await room.turn_camera_off()
        self.assertEqual(await room.get_camera_state(), 'off')
        self.assertEqual(client.calls[-1], ('update_inputs', {'camera': {'isEnabled': False}}))
        await room.leave()
        calls = len(client.calls)
        await room.turn_camera_off()  # left: nothing
        self.assertEqual(len(client.calls), calls)
        audio_only, _client = self.make_room(camera_size=None)
        await audio_only.join(ROOM, 'Smitline AI')
        await audio_only.turn_camera_off()

    async def test_without_a_camera_device_the_room_is_audio_only(self):
        room, _client = self.make_room(camera_size=None)
        await room.join(ROOM, 'Smitline AI')
        self.assertNotIn('camera', self.sdk.Daily.devices)
        self.assertEqual(await room.get_camera_state(), 'off')
        with self.assertRaises(RuntimeError):
            await room.enable_camera()

    async def test_people_and_who_is_speaking(self):
        room, client = self.make_room()
        self.assertIsNone(await room.get_participant_count())
        await room.join(ROOM, 'Smitline AI')
        client.people.update({
            'p1': {'id': 'p1', 'info': {'userName': ' Bea '}},
            'p2': {'id': 'p2', 'info': {}},
        })
        self.assertEqual(await room.get_participant_count(), 4)
        self.assertEqual(await room.get_participant_names(), ['Smitline AI', 'Smith', 'Bea'])
        client.speaker = client.people['local']
        self.assertIsNone(await room.get_active_speaker())
        client.speaker = client.people['p1']
        self.assertEqual(await room.get_active_speaker(), 'Bea')

    async def test_waits_in_an_empty_room_for_someone_before_counting_as_admitted(self):
        room, client = self.make_room()
        room.admission_poll = 0.01
        client.people.pop('guest')
        joining = asyncio.create_task(room.join(ROOM, 'Smitline AI'))
        await asyncio.sleep(0.05)
        self.assertFalse(joining.done())
        self.assertEqual(self.stages, ['opening_meeting', 'joining', 'waiting_for_admission'])
        client.people['bea'] = {'id': 'bea', 'info': {'userName': 'Bea'}}
        await asyncio.wait_for(joining, 1)
        self.assertEqual(self.stages[-1], 'admitted')

    async def test_a_room_nobody_comes_to_gives_up_like_a_lobby(self):
        room, client = self.make_room()
        room.admission_poll, room.admission_timeout = 0.01, 0.05
        client.people.pop('guest')
        with self.assertRaisesRegex(RuntimeError, 'Nobody joined the Daily room in time'):
            await room.join(ROOM, 'Smitline AI')
        self.assertNotIn('admitted', self.stages)

    async def test_stopping_while_it_waits_ends_the_wait(self):
        room, client = self.make_room()
        room.admission_poll = 0.01
        client.people.pop('guest')
        joining = asyncio.create_task(room.join(ROOM, 'Smitline AI'))
        await asyncio.sleep(0.03)
        room.stop.set()
        with self.assertRaisesRegex(RuntimeError, 'or it was stopped'):
            await asyncio.wait_for(joining, 1)

    async def test_a_room_that_expires_while_it_waits_says_so(self):
        room, client = self.make_room()
        room.admission_poll = 0.01
        client.people.pop('guest')
        joining = asyncio.create_task(room.join(ROOM, 'Smitline AI'))
        await asyncio.sleep(0.03)
        client.handler.on_call_state_updated('left')
        with self.assertRaisesRegex(RuntimeError, 'ended before anyone joined'):
            await asyncio.wait_for(joining, 1)

    async def test_the_room_ending_without_us_ends_the_meeting(self):
        room, client = self.make_room()
        await room.join(ROOM, 'Smitline AI')
        self.assertFalse(await room.has_ended())
        client.handler.on_call_state_updated('left')  # ejected when the room expired
        await asyncio.sleep(0)
        self.assertTrue(await room.has_ended())

    async def test_leaving_is_once_and_frees_daily(self):
        room, client = self.make_room()
        await room.join(ROOM, 'Smitline AI')
        await room.leave()
        await room.leave()
        self.assertEqual([call for call in client.calls if call[0] in ('leave', 'release')],
                         [('leave',)])
        self.assertFalse(await room.has_ended())  # we left; nothing ended it from outside
        room.release()
        room.release()
        self.assertEqual([call for call in client.calls if call[0] in ('leave', 'release')],
                         [('leave',), ('release',)])

    async def test_a_leave_daily_refuses_says_why(self):
        room, client = self.make_room()
        await room.join(ROOM, 'Smitline AI')

        def refused(completion=None):
            client.calls.append(('leave',))
            client._complete(completion, 'not in a call')
        client.leave = refused
        with self.assertRaisesRegex(RuntimeError, 'did not let Smitline leave the room: not in a call'):
            await room.leave()

    async def test_chat_goes_to_the_prebuilt_chat_under_its_name(self):
        room, client = self.make_room()
        self.assertFalse(await room.chat_available())
        await room.join(ROOM, 'Smitline AI')
        self.assertTrue(await room.chat_available())
        await room.send_chat_message('Notes are ready')
        self.assertIn(('chat', 'Notes are ready', 'Smitline AI'), client.calls)
        client.chat_error = 'chat off'
        with self.assertRaisesRegex(RuntimeError, 'chat off'):
            await room.send_chat_message('again')


class DailyAudioTests(unittest.IsolatedAsyncioTestCase):
    async def test_microphone_writes_10_ms_chunks_and_pads_the_last(self):
        device = FakeDevice('microphone', 'mic')
        microphone = DailyMicrophone(device)
        microphone.start()
        await microphone.write(b'\x01' * 1000)
        await microphone.drain()
        await microphone.close()
        self.assertEqual(microphone.chunk_size, 480)
        self.assertEqual([len(chunk) for chunk in device.written], [480, 480, 480])
        self.assertEqual(device.written[2], b'\x01' * 40 + b'\x00' * 440)

    async def test_microphone_paces_the_voice_in_real_time(self):
        device = FakeDevice('microphone', 'mic')
        microphone = DailyMicrophone(device)
        microphone.start()
        started = time.monotonic()
        await microphone.write(b'\x01' * 480 * 20)  # 200 ms of voice
        await microphone.drain()
        elapsed = time.monotonic() - started
        await microphone.close()
        self.assertEqual(len(device.written), 20)
        self.assertGreater(elapsed, 0.12)  # 200 ms, less the lead and the two queued chunks

    async def test_microphone_drops_what_was_not_yet_handed_over(self):
        device = FakeDevice('microphone', 'mic')
        microphone = DailyMicrophone(device)
        await microphone._queue.put(b'\x01' * 480)
        await microphone._queue.put(b'\x02' * 480)
        microphone.discard_pending()
        await asyncio.wait_for(microphone.drain(), 1)
        self.assertEqual(device.written, [])

    @staticmethod
    def voice(number, size=960):
        """A distinct, audible 20 ms chunk: every sample is `number` times 100."""
        return struct.pack(f'<{size // 2}h', *([number * 100] * (size // 2)))

    async def read_until(self, speaker, check, limit=200):
        chunks = []
        for _ in range(limit):
            chunks.append((await asyncio.wait_for(speaker.read(), 1)).data)
            if check(chunks):
                return chunks
        self.fail('the speaker never gave what was expected')

    async def test_speaker_hands_on_the_rooms_audio_in_order_from_the_completions(self):
        # daily-python gives a non-blocking speaker's audio only to the completion, from its own
        # thread; reading without one fed GPT-Live silence for a whole meeting (2026-11-05).
        device = FakeSpeakerDevice()
        played = [self.voice(number) for number in range(1, 31)]
        device.plays = list(played)
        speaker = DailySpeaker(device)
        speaker.start()
        try:
            chunks = await self.read_until(speaker, lambda got: played[-1] in got)
        finally:
            speaker.close()
        heard = [chunk for chunk in chunks if chunk != bytes(960)]
        self.assertEqual(heard, played)  # every chunk, in order, none twice
        self.assertEqual(speaker.reads, 30)
        self.assertEqual(device.most_waiting, 1)  # one read waits at a time
        self.assertEqual(speaker.dropped_ms, 0)
        self.assertEqual(speaker.received_bytes, 30 * 960)

    async def test_speaker_paces_20_ms_chunks_with_silence_while_nothing_plays(self):
        # GPT-Live keeps time by its input, so the stream never stops. With nobody else in the
        # room daily-python answers a read at once, with nothing, on the caller's thread.
        device = FakeSpeakerDevice()
        speaker = DailySpeaker(device)
        speaker.start()
        started = time.monotonic()
        try:
            chunks = [await asyncio.wait_for(speaker.read(), 1) for _ in range(10)]
        finally:
            elapsed = time.monotonic() - started
            speaker.close()
        self.assertEqual({chunk.data for chunk in chunks}, {bytes(960)})
        self.assertEqual([chunk.time_ns for chunk in chunks], [i * 20_000_000 for i in range(10)])
        self.assertGreater(elapsed, 0.15)  # paced in real time, not as fast as it can
        # It asks again on the next tick, never in a loop: about one read per 20 ms.
        self.assertLessEqual(device.reads, 13)
        self.assertGreaterEqual(speaker.empty_reads, 8)
        self.assertEqual(speaker.read_errors, 0)  # never re-armed inside a completion

    async def test_a_quiet_room_passes_its_silence_on_and_fills_gaps(self):
        # People in the room with nothing to say: Daily plays their silence. A read that finds
        # nothing in between leaves silence in its place, and the stream goes on.
        device = FakeSpeakerDevice()
        device.plays = [bytes(960)] * 5 + [None] + [self.voice(7)] + [bytes(960)] * 20
        speaker = DailySpeaker(device)
        speaker.start()
        try:
            chunks = await self.read_until(speaker, lambda got: self.voice(7) in got)
        finally:
            speaker.close()
        self.assertTrue(all(chunk in (bytes(960), self.voice(7)) for chunk in chunks))
        self.assertEqual(chunks.count(self.voice(7)), 1)
        self.assertGreaterEqual(speaker.reads, 6)
        self.assertEqual(speaker.empty_reads, 1)

    async def test_the_microphone_shows_each_chunk_and_the_ones_queued_behind_it(self):
        device = FakeDevice('microphone', 'mic')
        microphone = DailyMicrophone(device)
        seen = []
        microphone.on_chunk = lambda chunk, upcoming: seen.append((chunk[:1], [c[:1] for c in upcoming]))
        await microphone._queue.put(b'\x01' * 480)
        await microphone._queue.put(b'\x02' * 480)
        microphone.start()
        await microphone.drain()
        await microphone.close()
        self.assertEqual(seen, [(b'\x01', [b'\x02']), (b'\x02', [])])
        self.assertEqual(len(device.written), 2)

    async def test_audio_in_bigger_or_smaller_pieces_comes_out_in_20_ms_chunks(self):
        speaker = DailySpeaker(FakeSpeakerDevice())
        speaker._arrived(b'\x03' * 1500)
        self.assertEqual(speaker.next_chunk(), b'\x03' * 960)
        self.assertEqual(speaker.next_chunk(), b'\x03' * 540 + bytes(420))
        speaker._arrived(b'\x04' * 400)
        self.assertEqual(speaker.next_chunk(), b'\x04' * 400 + bytes(560))

    async def test_more_than_half_a_second_ahead_the_oldest_audio_is_dropped_and_counted(self):
        speaker = DailySpeaker(FakeSpeakerDevice())
        burst = b''.join(self.voice(number) for number in range(1, 41))  # 800 ms at once
        speaker._arrived(burst)
        self.assertEqual(len(speaker._pending), 25 * 960)
        self.assertEqual(speaker.overflow_bytes, 15 * 960)
        self.assertEqual(speaker.dropped_ms, 300)
        self.assertEqual(speaker.next_chunk(), self.voice(16))  # the oldest 15 chunks went

    async def test_ahead_of_the_clock_it_skips_silence_but_never_speech(self):
        speaker = DailySpeaker(FakeSpeakerDevice())
        silence = bytes(960)
        speaker._arrived(self.voice(1) + silence * 4 + self.voice(2) * 3 + silence)
        out = [speaker.next_chunk() for _ in range(7)]
        # More than three chunks ahead, one silent chunk is skipped a tick until only three
        # wait; the speech after them is all passed on.
        self.assertEqual(out, [self.voice(1), silence, silence] + [self.voice(2)] * 3 + [silence])
        self.assertEqual(speaker.skipped_bytes, 2 * 960)
        self.assertEqual(speaker.dropped_ms, 0)
        speaker._arrived(self.voice(3) * 6)
        out = [speaker.next_chunk() for _ in range(8)]
        # Speech is never skipped, however far ahead.
        self.assertEqual(out, [self.voice(3)] * 6 + [silence] * 2)
        self.assertEqual(speaker.skipped_bytes, 2 * 960)

    async def test_completions_after_close_or_after_the_loop_ended_are_ignored(self):
        device = FakeSpeakerDevice()
        device.plays = [self.voice(1)] * 3
        speaker = DailySpeaker(device)
        speaker.start()
        await asyncio.sleep(0.01)
        speaker.close()
        reads = device.reads
        await asyncio.sleep(0.1)  # the read that was waiting completes after close
        self.assertEqual(device.reads, reads)  # and asks for nothing more
        self.assertEqual(len(speaker._pending), 0)
        # A completion from Daily's thread once the event loop has gone raises nothing there.
        closed_loop = asyncio.new_event_loop()
        closed_loop.close()
        late = DailySpeaker(FakeSpeakerDevice())
        late.loop = closed_loop
        errors = []
        thread = threading.Thread(target=lambda: self._call(late._completed, self.voice(1), errors))
        thread.start()
        thread.join(1)
        self.assertEqual(errors, [])

    @staticmethod
    def _call(function, data, errors):
        try:
            function(data)
        except Exception as error:  # what Daily's thread would see
            errors.append(error)

    async def test_a_read_that_fails_is_counted_and_asked_again_on_the_next_tick(self):
        device = FakeSpeakerDevice()
        device.fail = RuntimeError('no speaker device has been attached')
        speaker = DailySpeaker(device)
        speaker.start()
        try:
            chunks = [await asyncio.wait_for(speaker.read(), 1) for _ in range(5)]
            self.assertEqual({chunk.data for chunk in chunks}, {bytes(960)})
            self.assertGreaterEqual(speaker.read_errors, 4)
            self.assertLessEqual(speaker.read_errors, 8)  # once a tick, not in a loop
            device.fail = None
            device.plays = [self.voice(5)]
            await self.read_until(speaker, lambda got: self.voice(5) in got)
        finally:
            speaker.close()

    async def test_a_read_daily_never_answers_is_asked_again(self):
        # Waiting forever on it would leave the meeting deaf again, with nothing to show for it.
        device = FakeSpeakerDevice()
        device.swallow = 1
        played = [self.voice(number) for number in range(1, 6)]
        device.plays = list(played)
        speaker = DailySpeaker(device, read_timeout=0.1)
        speaker.start()
        try:
            chunks = await self.read_until(speaker, lambda got: played[-1] in got)
        finally:
            speaker.close()
        self.assertEqual([chunk for chunk in chunks if chunk != bytes(960)], played)
        self.assertEqual(speaker.lost_reads, 1)
        self.assertEqual(speaker.stats()['lost_reads'], 1)

    async def test_a_read_that_answers_after_it_was_given_up_only_adds_its_audio(self):
        device = FakeSpeakerDevice()
        device.slow = [0.2]  # the first read answers after the next one was asked for
        played = [self.voice(number) for number in range(1, 8)]
        device.plays = list(played)
        speaker = DailySpeaker(device, read_timeout=0.15)
        speaker.start()
        try:
            chunks = await self.read_until(speaker, lambda got: played[-1] in got)
        finally:
            speaker.close()
        self.assertEqual([chunk for chunk in chunks if chunk != bytes(960)], played)
        self.assertEqual(speaker.lost_reads, 1)
        # The given-up read and its replacement wait together; once the late one has answered,
        # one read at a time again.
        self.assertEqual(device.waiting_at, [2] + [1] * (len(played) - 1))

    async def test_a_reader_that_falls_behind_loses_the_oldest_audio_once_it_reads(self):
        speaker = DailySpeaker(FakeSpeakerDevice(), max_chunks=2)
        for value in (b'a', b'b', b'c'):
            speaker._put(value)
        # Before anyone reads (GPT-Live still starting), the oldest simply gives way.
        self.assertEqual(speaker.dropped, 0)
        self.assertEqual([(await speaker.read()).data for _ in range(2)], [b'b', b'c'])
        for value in (b'd', b'e', b'f'):
            speaker._put(value)
        self.assertEqual(speaker.dropped, 1)
        self.assertEqual(speaker.dropped_ms, 20)
        self.assertEqual([(await speaker.read()).data for _ in range(2)], [b'e', b'f'])

    async def test_once_the_reader_stops_what_still_comes_in_is_not_counted_as_dropped(self):
        # 2026-11-05: after the meeting ended, the room's audio kept coming in for a second and
        # a half and was logged as GPT-Live falling behind.
        speaker = DailySpeaker(FakeSpeakerDevice(), max_chunks=2)
        speaker._put(b'a')
        self.assertEqual((await speaker.read()).data, b'a')
        speaker.stop_listening()
        for value in (b'b', b'c', b'd', b'e'):
            speaker._put(value)
        self.assertEqual(speaker.dropped, 0)
        self.assertEqual([(await speaker.read()).data for _ in range(2)], [b'd', b'e'])
        for value in (b'f', b'g', b'h'):
            speaker._put(value)
        self.assertEqual(speaker.dropped, 0)  # a read after stopping does not count drops again


def pack_of(clips, states, arrival=None, fps=12):
    return PresencePack(
        name='test', width=64, height=36, fps=fps,
        clips={name: Clip(tuple(frames), ('image/webp',) * len(frames), loop) for name, (frames, loop) in clips.items()},
        states={state: states.get(state, states['listening']) for state in VISUAL_STATES},
        arrival=arrival)


PACK = pack_of(
    {'offstage': (['o'], True), 'arrive': (['a0', 'a1', 'a2'], False), 'listening': (['l0', 'l1'], True),
     'speaking': (['s0', 's1'], True), 'leave': (['g0', 'g1'], False)},
    {'joining': 'offstage', 'listening': 'listening', 'speaking': 'speaking', 'finalizing': 'leave',
     'ended': 'offstage'},
    arrival='arrive')


def play(player, ticks):
    return [player.pack.clips[clip].frames[index] for clip, index in (player.tick() for _ in range(ticks))]


class ClipPlayerTests(unittest.TestCase):
    def test_walks_in_once_then_loops_the_state(self):
        player = ClipPlayer(PACK)
        self.assertEqual(play(player, 2), ['o', 'o'])
        player.set_state('listening')
        self.assertEqual(play(player, 7), ['a0', 'a1', 'a2', 'l0', 'l1', 'l0', 'l1'])

    def test_an_arrival_is_never_cut_short(self):
        player = ClipPlayer(PACK)
        player.set_state('listening')
        self.assertEqual(play(player, 1), ['a0'])
        player.set_state('speaking')
        self.assertEqual(play(player, 4), ['a1', 'a2', 's0', 's1'])

    def test_a_goodbye_plays_once_and_holds_its_last_frame(self):
        player = ClipPlayer(PACK)
        player.set_state('listening')
        play(player, 3)
        player.set_state('finalizing')
        self.assertEqual(play(player, 4), ['g0', 'g1', 'g1', 'g1'])
        player.set_state('ended')
        self.assertEqual(play(player, 2), ['o', 'o'])

    def test_the_arrival_plays_only_the_first_time(self):
        player = ClipPlayer(PACK)
        player.set_state('listening')
        play(player, 4)
        player.set_state('joining')
        player.set_state('speaking')
        self.assertEqual(play(player, 2), ['s0', 's1'])

    def test_states_that_share_a_looping_clip_do_not_restart_it(self):
        player = ClipPlayer(PACK)
        player.set_state('listening')
        play(player, 4)  # a0 a1 a2 l0
        player.set_state('working')  # shows listening's clip too
        self.assertEqual(play(player, 2), ['l1', 'l0'])

    def test_a_pack_without_an_arrival_switches_at_once(self):
        player = ClipPlayer(pack_of({'listening': (['l0'], True), 'speaking': (['s0'], True)},
                                    {'listening': 'listening', 'speaking': 'speaking'}))
        player.set_state('speaking')
        self.assertEqual(play(player, 2), ['s0', 's0'])

    def test_a_character_that_never_arrived_stays_off_stage_when_the_meeting_ends(self):
        for last in ('finalizing', 'ended'):
            with self.subTest(last=last):
                player = ClipPlayer(PACK)
                player.set_state(last)
                self.assertEqual(play(player, 2), ['o', 'o'])
                self.assertTrue(player.settled)

    def test_a_goodbye_two_states_share_plays_once(self):
        pack = pack_of(
            {'offstage': (['o'], True), 'listening': (['l0'], True), 'leave': (['g0', 'g1', 'g2'], False)},
            {'joining': 'offstage', 'listening': 'listening', 'finalizing': 'leave', 'ended': 'leave'})
        player = ClipPlayer(pack)
        player.set_state('listening')
        play(player, 2)
        player.set_state('finalizing')
        self.assertEqual(play(player, 2), ['g0', 'g1'])
        self.assertFalse(player.settled)
        player.set_state('ended')
        self.assertEqual(play(player, 3), ['g2', 'g2', 'g2'])
        self.assertTrue(player.settled)

    def test_settles_once_a_clip_has_played_to_its_end(self):
        player = ClipPlayer(PACK)
        player.set_state('listening')
        play(player, 3)  # the arrival
        self.assertFalse(player.settled)
        play(player, 1)
        self.assertFalse(player.settled)
        play(player, 1)  # the listening loop's last frame
        self.assertTrue(player.settled)


@unittest.skipUnless(HAS_PILLOW, 'needs Pillow')
class DailyCameraTests(unittest.TestCase):
    COLOURS = {'offstage': (0, 0, 0), 'arrive': (255, 0, 0), 'listening': (0, 255, 0)}

    def webp(self, colour, size):
        out = io.BytesIO()
        Image.new('RGB', size, colour).save(out, 'WEBP', lossless=True)
        return out.getvalue()

    def pack(self, fps=12, size=(64, 36)):
        frames = {name: [self.webp(colour, size)] * (2 if name == 'arrive' else 1)
                  for name, colour in self.COLOURS.items()}
        return PresencePack(
            name='test', width=size[0], height=size[1], fps=fps,
            clips={'offstage': Clip(tuple(frames['offstage']), ('image/webp',), True),
                   'arrive': Clip(tuple(frames['arrive']), ('image/webp',) * 2, False),
                   'listening': Clip(tuple(frames['listening']), ('image/webp',), True)},
            states={state: 'offstage' if state in ('joining', 'ended') else 'listening' for state in VISUAL_STATES},
            arrival='arrive')

    @staticmethod
    def centre(frame, width=64, height=36):
        offset = ((height // 2) * width + width // 2) * 4
        return tuple(frame[offset:offset + 3])

    def test_plays_the_pack_for_the_presence_state_blending_into_each_clip(self):
        camera = DailyCamera(FakeDevice('camera', 'cam'), None, pack=self.pack())
        self.assertEqual(camera.blend_frames, 3)  # a quarter second at 12 fps
        self.assertEqual(self.centre(camera.next_frame()), (0, 0, 0))
        camera.set_visual_state('listening')
        frames = [self.centre(camera.next_frame()) for _ in range(8)]
        # The arrival (red) fades in from the empty stage, the listening clip (green) from it.
        self.assertLess(frames[0][0], frames[1][0])
        self.assertEqual(frames[1][1], 0)
        self.assertTrue(any(red > 0 and green > 0 for red, green, _ in frames[2:5]))
        self.assertEqual(frames[-1], (0, 255, 0))
        self.assertEqual(len(camera.next_frame()), 64 * 36 * 4)

    def test_a_change_of_state_mid_loop_blends_instead_of_jumping(self):
        camera = DailyCamera(FakeDevice('camera', 'cam'), None, pack=self.pack())
        camera.set_visual_state('listening')
        for _ in range(8):
            camera.next_frame()
        camera.set_visual_state('ended')  # back to the empty stage (black)
        greens = [self.centre(camera.next_frame())[1] for _ in range(5)]
        self.assertEqual(greens, sorted(greens, reverse=True))
        self.assertGreater(greens[0], 0)
        self.assertEqual(greens[-1], 0)

    def test_draws_the_caption_under_the_picture(self):
        pack = self.pack(size=(320, 180))
        plain = DailyCamera(FakeDevice('camera', 'cam'), None, pack=pack).next_frame()
        captioned = DailyCamera(FakeDevice('camera', 'cam'), None, pack=pack,
                                caption="Smith's AI assistant").next_frame()
        changed = [i // 4 // 320 for i in range(0, len(plain), 4) if plain[i:i + 3] != captioned[i:i + 3]]
        self.assertTrue(changed)
        self.assertGreater(min(changed), 180 * 0.8)  # only the caption's band, near the bottom

    def test_without_a_pack_it_shows_the_still_mark(self):
        frame = DailyCamera(FakeDevice('camera', 'cam'), None, caption='AI assistant').next_frame()
        self.assertEqual(len(frame), 640 * 360 * 4)
        self.assertEqual(self.centre(frame, 640, 360), (255, 255, 255))  # the middle bar
        self.assertEqual(tuple(frame[0:3]), (0x30, 0x32, 0x3B))

    def test_writes_frames_on_its_own_thread_until_stopped(self):
        device = FakeDevice('camera', 'cam')
        camera = DailyCamera(device, None, pack=self.pack(fps=30))
        camera.start()
        time.sleep(0.2)
        asyncio.run(camera.stop())
        count = len(device.written)
        time.sleep(0.1)
        self.assertGreaterEqual(count, 3)
        self.assertLessEqual(len(device.written), count + 1)

    class Voice:
        """A VoiceActivity as the camera sees it: the mouth picture for the voice now."""
        level = 0

        def mouth(self, levels):
            return min(levels, self.level)

    def mouth_pack(self, fps=24, size=(64, 36)):
        body, listen = solid_webp((0, 255, 0), size), solid_webp((0, 0, 255), size)
        shapes = (solid_webp((255, 0, 0), (8, 6)), solid_webp((255, 255, 0), (8, 6)))
        return PresencePack(
            name='test', width=size[0], height=size[1], fps=fps,
            clips={'listening': Clip((listen,), ('image/webp',), True),
                   'speaking': Clip((body, body), ('image/webp',) * 2, True,
                                    Mouth(10, 12, 8, 6, shapes, ('image/webp',) * 2))},
            states={state: 'speaking' if state == 'speaking' else 'listening' for state in VISUAL_STATES},
            version=2)

    @staticmethod
    def pixel(frame, x, y, width=64):
        offset = (y * width + x) * 4
        return tuple(frame[offset:offset + 3])

    def test_draws_the_mouth_the_voice_has_into_its_box(self):
        voice = self.Voice()
        camera = DailyCamera(FakeDevice('camera', 'cam'), None, pack=self.mouth_pack(), voice=voice)
        camera.set_visual_state('speaking')
        for _ in range(4):
            camera.next_frame()  # past the blend into the clip
        frame = camera.next_frame()
        self.assertEqual(self.pixel(frame, 12, 14), (0, 255, 0))  # closed: the clip's own frame
        voice.level = 2
        frame = camera.next_frame()
        self.assertEqual(self.pixel(frame, 12, 14), (255, 255, 0))  # the loudest mouth
        self.assertEqual(self.pixel(frame, 17, 17), (255, 255, 0))  # its box's far corner
        self.assertEqual(self.pixel(frame, 18, 14), (0, 255, 0))    # and nothing outside it
        self.assertEqual(self.pixel(frame, 12, 18), (0, 255, 0))
        voice.level = 1
        self.assertEqual(self.pixel(camera.next_frame(), 12, 14), (255, 0, 0))
        camera.set_visual_state('listening')  # the listening clip has no mouth
        for _ in range(4):
            frame = camera.next_frame()
        self.assertEqual(self.pixel(frame, 12, 14), (0, 0, 255))

    def test_blends_quickly_into_and_out_of_a_clip_with_a_mouth(self):
        camera = DailyCamera(FakeDevice('camera', 'cam'), None, pack=self.mouth_pack(), voice=self.Voice())
        self.assertEqual((camera.quick_blend_frames, camera.blend_frames), (2, 6))
        camera.next_frame()
        camera.set_visual_state('speaking')
        greens = [self.pixel(camera.next_frame(), 30, 20)[1] for _ in range(3)]
        self.assertTrue(0 < greens[0] < greens[1] < 255, greens)
        self.assertEqual(greens[2], 255)
        camera.set_visual_state('listening')
        blues = [self.pixel(camera.next_frame(), 30, 20)[2] for _ in range(3)]
        self.assertEqual(blues[2], 255)

    def test_decodes_the_speaking_clip_and_its_mouths_once(self):
        decoded = []

        class Counting(DailyCamera):
            def _decode(self, data, size=None):
                decoded.append(len(data))
                return super()._decode(data, size)
        voice = self.Voice()
        camera = Counting(FakeDevice('camera', 'cam'), None, pack=self.mouth_pack(), voice=voice)
        camera._prepare()
        self.assertEqual(len(decoded), 4)  # the body, two mouths, and the listening clip it starts on
        camera.set_visual_state('speaking')
        for level in (0, 1, 2, 1, 0, 2, 2, 1):
            voice.level = level
            camera.next_frame()
        self.assertEqual(len(decoded), 4)

    def test_the_still_picture_goes_off_a_moment_after_the_end(self):
        camera = DailyCamera(FakeDevice('camera', 'cam'), None)
        camera.next_frame()
        self.assertFalse(camera.finished())
        camera.set_visual_state('ended')
        camera.next_frame()
        self.assertFalse(camera.finished())
        camera.next_frame()  # a second at 2 frames a second
        self.assertTrue(camera.finished())


def solid_webp(colour, size):
    out = io.BytesIO()
    Image.new('RGB', size, colour).save(out, 'WEBP', lossless=True)
    return out.getvalue()


@unittest.skipUnless(HAS_PILLOW, 'needs Pillow')
class DailyCameraEndTests(unittest.IsolatedAsyncioTestCase):
    def pack(self):
        frames = {name: solid_webp(colour, (64, 36)) for name, colour in
                  (('offstage', (0, 0, 0)), ('arrive', (255, 0, 0)), ('listening', (0, 255, 0)))}
        return PresencePack(
            name='test', width=64, height=36, fps=30,
            clips={'offstage': Clip((frames['offstage'],), ('image/webp',), True),
                   'arrive': Clip((frames['arrive'],) * 2, ('image/webp',) * 2, False),
                   'listening': Clip((frames['listening'],), ('image/webp',), True)},
            states={state: 'offstage' if state in ('joining', 'ended') else 'listening'
                    for state in VISUAL_STATES},
            arrival='arrive')

    async def test_turns_the_room_camera_off_once_the_meeting_has_ended(self):
        turned_off = []

        async def turn_off():
            turned_off.append(True)
        device = FakeDevice('camera', 'cam')
        camera = DailyCamera(device, None, pack=self.pack(), turn_off=turn_off)
        camera.start()
        camera.set_visual_state('listening')
        await asyncio.sleep(0.15)
        self.assertFalse(camera.off)
        camera.set_visual_state('ended')  # the empty stage blends in, then the camera goes off
        for _ in range(50):
            if turned_off:
                break
            await asyncio.sleep(0.02)
        self.assertEqual(turned_off, [True])
        self.assertTrue(camera.off)
        count = len(device.written)
        await asyncio.sleep(0.1)
        self.assertEqual(len(device.written), count)  # no more frames
        self.assertEqual(self.centre(device.written[-1]), (0, 0, 0))  # the last was the empty stage
        await camera.stop()
        self.assertEqual(turned_off, [True])  # once

    @staticmethod
    def centre(frame):
        offset = (18 * 64 + 32) * 4
        return tuple(frame[offset:offset + 3])

    async def test_stopping_turns_the_camera_off_unless_the_end_did(self):
        turned_off = []

        async def turn_off():
            turned_off.append(True)
        camera = DailyCamera(FakeDevice('camera', 'cam'), None, turn_off=turn_off)
        camera.start()
        await camera.stop()
        await camera.stop()
        self.assertEqual(turned_off, [True])
        audio_only = DailyCamera(None, None, turn_off=turn_off)
        await audio_only.stop()
        self.assertEqual(turned_off, [True])


class JoinedRoomTests(unittest.IsolatedAsyncioTestCase):
    def runtime(self, **changes):
        values = dict(camera_enabled=True, camera_style='still', camera_pack='', participant_name='Smitline AI')
        values.update(changes)
        return SimpleNamespace(**values)

    async def test_joins_yields_the_parts_and_always_leaves(self):
        stages, state = [], {}
        sdk = fake_sdk()
        async with joined_room(ROOM, self.runtime(), asyncio.Event(), stages.append, state,
                               caption='AI assistant', sdk=sdk) as (room, speaker, camera):
            self.assertEqual(stages[-1], 'admitted')
            self.assertIs(camera.audio_writer.device, sdk.Daily.devices['microphone'])
            self.assertIs(speaker.device, sdk.Daily.devices['speaker'])
            self.assertEqual(state['capabilities']['camera'], True)
            # The camera's mouth and the talking picture follow what the microphone writes, and
            # the camera can turn the room's camera off at the end.
            self.assertIs(camera.voice, camera.audio_writer.voice)
            self.assertEqual(camera.audio_writer.on_chunk, camera.voice.feed)
            self.assertEqual(camera.turn_off, room.turn_camera_off)
            await room.enable_camera()
        # Out of the room first, so the tile goes with it: turning the camera off is then
        # skipped, and daily-python is released last.
        self.assertEqual([call[0] for call in room.client.calls[-2:]], ['leave', 'release'])
        self.assertNotIn(('update_inputs', {'camera': {'isEnabled': False}}), room.client.calls)
        self.assertEqual(state['leaveState'], 'left')

    async def test_it_leaves_the_room_before_its_devices_stop(self):
        # A tile must go at once, not freeze as the camera and microphone stop first (2026-11-05).
        sdk = fake_sdk()
        seen = {}
        async with joined_room(ROOM, self.runtime(), asyncio.Event(), lambda _stage: None, {},
                               caption='AI assistant', sdk=sdk) as (room, speaker, camera):
            leave = room.client.leave

            def leaving(completion=None):
                seen.update(camera=not camera._closed.is_set(),
                            microphone=camera.audio_writer._task is not None,
                            speaker=speaker._task is not None)
                return leave(completion=completion)
            room.client.leave = leaving
        self.assertEqual(seen, {'camera': True, 'microphone': True, 'speaker': True})
        self.assertEqual(room.client.calls[-1], ('release',))

    async def test_a_pack_that_cannot_be_read_leaves_the_still_picture(self):
        state = {}
        with tempfile.TemporaryDirectory() as empty:
            async with joined_room(ROOM, self.runtime(camera_style='mascot', camera_pack=empty),
                                   asyncio.Event(), lambda _stage: None, state, sdk=fake_sdk()) as (_room, _speaker, camera):
                self.assertIsNone(camera.pack)
        self.assertIn('no manifest.json', state['cameraPackError'])

    async def test_a_failed_join_still_frees_daily(self):
        sdk = fake_sdk()
        original = FakeCallClient.join

        def refused(client, *args, **kwargs):
            client.join_error = 'room is full'
            return original(client, *args, **kwargs)

        sdk.CallClient = type('RefusingClient', (FakeCallClient,), {'join': refused})
        with self.assertRaisesRegex(RuntimeError, 'room is full'):
            async with joined_room(ROOM, self.runtime(), asyncio.Event(), lambda _stage: None, {}, sdk=sdk):
                pass
        client = FakeCallClient.instances[-1]
        self.assertIn(('release',), client.calls)


if __name__ == '__main__':
    unittest.main()
