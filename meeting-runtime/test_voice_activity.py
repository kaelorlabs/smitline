import array
import asyncio
import math
import struct
import time
import unittest

from voice_activity import (
    FULL_DBFS, QUIET_DBFS, RELEASE_SECONDS, VoiceActivity, chunk_dbfs, loudness, mouth_picture)

CHUNK = 240  # samples in 10 ms at 24 kHz


def tone(amplitude, samples=CHUNK):
    """A chunk of 16-bit PCM: a sine at `amplitude` of full scale."""
    return struct.pack(f'<{samples}h', *(round(amplitude * 32767 * math.sin(i / 3)) for i in range(samples)))


SILENCE = bytes(CHUNK * 2)
LOUD = tone(0.3)     # about -13 dBFS: wide open
SOFT = tone(0.01)    # about -43 dBFS: just audible
HUSH = tone(0.002)   # about -57 dBFS: silence to the eye


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


class LevelTests(unittest.TestCase):
    def test_measures_chunks_in_decibels_full_scale(self):
        self.assertEqual(chunk_dbfs(SILENCE), -math.inf)
        self.assertAlmostEqual(chunk_dbfs(tone(0.5, 2400)), -9.0, delta=0.3)  # a sine's RMS is -3 dB
        self.assertAlmostEqual(chunk_dbfs(struct.pack('<4h', 32767, -32767, 32767, -32767)), 0, delta=0.01)
        floats = array.array('f', [0.5, -0.5] * 120).tobytes()
        self.assertAlmostEqual(chunk_dbfs(floats, 4), -6.02, delta=0.01)

    def test_loudness_runs_from_quiet_to_full(self):
        self.assertEqual(loudness(-math.inf), 0)
        self.assertEqual(loudness(QUIET_DBFS), 0)
        self.assertEqual(loudness(FULL_DBFS), 1)
        self.assertEqual(loudness(0), 1)
        self.assertAlmostEqual(loudness((QUIET_DBFS + FULL_DBFS) / 2), 0.5)

    def test_mouth_pictures_round_to_the_nearest(self):
        self.assertEqual([mouth_picture(level, 5) for level in (0, 0.09, 0.11, 0.5, 0.99, 1.4)],
                         [0, 0, 1, 3, 5, 5])


class VoiceActivityTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.voice = VoiceActivity(clock=self.clock)
        self.changes = []
        self.voice.on_speaking.append(self.changes.append)

    def chunks(self, chunk, count=1):
        for _ in range(count):
            self.voice.feed(chunk)
            self.clock.now += 0.01

    def test_speaking_starts_with_the_first_audible_chunk_written(self):
        self.chunks(SILENCE, 5)
        self.chunks(HUSH, 5)
        self.assertEqual(self.changes, [])
        self.voice.feed(SOFT)
        self.assertEqual(self.changes, [True])
        self.assertTrue(self.voice.speaking)

    def test_a_pause_between_words_keeps_it_a_pause_between_sentences_ends_it(self):
        self.chunks(LOUD, 30)
        self.chunks(SILENCE, 15)   # 150 ms between words
        self.chunks(LOUD, 10)
        self.assertEqual(self.changes, [True])
        last = self.clock.now - 0.01
        while self.voice.speaking:
            self.chunks(SILENCE)
        self.assertEqual(self.changes, [True, False])
        self.assertAlmostEqual(self.clock.now - 0.01 - last, RELEASE_SECONDS, delta=0.0101)

    def test_the_queued_audio_opens_the_mouth_with_the_sound(self):
        self.voice.feed(SILENCE, (LOUD,))
        self.assertEqual(self.changes, [True])
        self.assertGreater(self.voice.level, 0.4)

    def test_the_mouth_opens_quickly_and_closes_more_slowly(self):
        self.chunks(LOUD, 1)
        self.assertEqual(self.voice.mouth(5), 2)  # half open after the first 10 ms
        self.chunks(LOUD, 2)
        self.assertEqual(self.voice.mouth(5), 4)
        self.chunks(LOUD, 2)
        self.assertEqual(self.voice.mouth(5), 5)
        self.chunks(SILENCE, 3)
        closing = self.voice.level
        self.assertTrue(0.4 < closing < 0.8, closing)  # a 60 ms decay, not a snap
        self.chunks(SILENCE, 15)
        self.assertEqual(self.voice.mouth(5), 0)
        self.chunks(SOFT, 1)
        self.assertLessEqual(self.voice.mouth(5), 1)

    def test_the_mouth_closes_on_its_own_when_no_chunk_comes(self):
        # A Daily microphone writes nothing between replies.
        self.chunks(LOUD, 5)
        self.assertEqual(self.voice.mouth(5), 5)
        self.clock.now += 0.1
        self.assertEqual(self.voice.mouth(5), 1)
        self.clock.now += 0.2
        self.assertTrue(self.voice.poll() is False)
        self.assertEqual(self.voice.level_now(), 0)

    def test_attach_follows_a_microphone(self):
        class Format:
            byte_depth = 4

        class Microphone:
            audio_format = Format()
        microphone = Microphone()
        self.voice.attach(microphone)
        self.assertEqual(microphone.on_chunk, self.voice.feed)
        self.assertIs(microphone.voice, self.voice)
        self.assertEqual(self.voice.sample_width, 4)

    def test_a_failing_listener_never_reaches_the_microphone(self):
        def broken(_value):
            raise RuntimeError('picture failed')
        self.voice.on_speaking.insert(0, broken)
        self.voice.on_level.append(broken)
        self.voice.feed(LOUD)
        self.assertEqual(self.changes, [True])


class TimingTests(unittest.IsolatedAsyncioTestCase):
    """Through the microphones' own pacing: on with the first audible chunk written, off within
    about 250 ms of the last."""

    async def follow(self, microphone, write, silence_after=0.45):
        voice = VoiceActivity()
        voice.attach(microphone)
        seen = []
        voice.on_speaking.append(lambda speaking: seen.append((time.monotonic(), speaking)))
        written = []
        tap = microphone.on_chunk

        def tapped(chunk, upcoming=()):
            if any(chunk) and not written:
                written.append(time.monotonic())
            tap(chunk, upcoming)
        microphone.on_chunk = tapped
        await asyncio.sleep(0.05)
        await write(LOUD * 30)  # 300 ms of voice
        await microphone.drain()
        last_written = time.monotonic()
        await asyncio.sleep(silence_after)
        self.assertEqual([speaking for _at, speaking in seen], [True, False])
        on, off = seen[0][0], seen[1][0]
        self.assertLess(on - written[0], 0.012)  # within one chunk of the first audible one written
        self.assertGreater(off - last_written, RELEASE_SECONDS - 0.04)
        self.assertLess(off - last_written, 0.25)

    async def test_a_daily_microphone(self):
        from daily_room import DailyMicrophone

        class Device:
            def write_frames(self, frames, completion=None):
                return len(frames) // 2
        microphone = DailyMicrophone(Device())
        microphone.start()
        try:
            await self.follow(microphone, microphone.write)
        finally:
            await microphone.close()

    async def test_a_browser_microphone(self):
        try:
            from joinly.providers.browser.devices.virtual_microphone import VirtualMicrophone
        except ImportError:
            self.skipTest('needs joinly')

        class Pipe:
            """The PulseAudio pipe: takes 10 ms of audio every 10 ms, as the real one does."""
            def write(self, data):
                pass

            async def drain(self):
                pass
        microphone = VirtualMicrophone(sample_rate=24000, byte_depth=2)
        microphone._writer = Pipe()
        microphone._queue = asyncio.Queue(maxsize=microphone.queue_size)
        pace = asyncio.create_task(microphone._pace_loop())
        try:
            await self.follow(microphone, microphone.write)
        finally:
            pace.cancel()
            await asyncio.gather(pace, return_exceptions=True)

    async def test_the_browser_microphone_shows_the_tap_what_is_queued(self):
        try:
            from joinly.providers.browser.devices.virtual_microphone import VirtualMicrophone
        except ImportError:
            self.skipTest('needs joinly')

        class Pipe:
            def __init__(self):
                self.data = []

            def write(self, data):
                self.data.append(data)

            async def drain(self):
                pass
        microphone = VirtualMicrophone(sample_rate=24000, byte_depth=2)
        microphone._writer = pipe = Pipe()
        microphone._queue = asyncio.Queue(maxsize=microphone.queue_size)
        seen = []
        microphone.on_chunk = lambda chunk, upcoming: seen.append((chunk, upcoming))
        await microphone._queue.put(LOUD)
        await microphone._queue.put(SOFT)
        pace = asyncio.create_task(microphone._pace_loop())
        await asyncio.sleep(0.035)
        pace.cancel()
        await asyncio.gather(pace, return_exceptions=True)
        self.assertEqual(seen[:2], [(LOUD, (SOFT,)), (SOFT, ())])
        self.assertEqual(pipe.data[:2], [LOUD, SOFT])
        self.assertEqual([chunk for chunk, _ in seen], pipe.data)  # silence too, as it is written


if __name__ == '__main__':
    unittest.main()
