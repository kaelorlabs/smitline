import asyncio
import time
import unittest
from types import SimpleNamespace

import daily_hearing_check as hearing
from meeting_input import VOICED_DB, pcm_level
from test_daily_room import fake_sdk


class BurstTests(unittest.TestCase):
    def test_every_20_ms_of_the_tone_is_voiced(self):
        sound = hearing.burst(1.0)
        self.assertEqual(len(sound), 48000)
        levels = [pcm_level(sound[at:at + 960])[0] for at in range(0, len(sound), 960)]
        self.assertGreater(min(levels), VOICED_DB + 6)
        self.assertLess(max(pcm_level(sound)[1], 0), 32767)


class ReportTests(unittest.TestCase):
    def listen(self, pieces):
        """pieces: (start time, voiced?) of consecutive 20 ms chunks."""
        listener = hearing.Listener()
        for at, voiced in pieces:
            listener.feed(hearing.burst(0.02) if voiced else bytes(960), at + 0.02)
        return listener

    def test_each_burst_is_matched_to_when_it_came_in(self):
        pieces = [(100 + n * 0.02, 10 <= n < 60 or 110 <= n < 160) for n in range(200)]
        listener = self.listen(pieces)
        self.assertEqual([round(at, 2) for at in listener.onsets], [100.2, 102.2])
        played = [{'burst': 0, 'start': 100.1, 'end': 101.1}, {'burst': 1, 'start': 102.1, 'end': 103.1}]
        result, code = hearing.report(played, listener, {'dropped_ms': 0})
        self.assertEqual(code, 0)
        self.assertTrue(result['heard'])
        self.assertEqual(result['latency_ms'], [100, 100])
        self.assertEqual(result['voiced_ms'], 2000)
        self.assertEqual(result['played_ms'], 2000)
        self.assertEqual(result['speaker'], {'dropped_ms': 0})

    def test_nothing_heard_some_heard_and_could_not_run(self):
        played = [{'burst': 0, 'start': 100.0, 'end': 101.0}, {'burst': 1, 'start': 102.0, 'end': 103.0}]
        deaf = self.listen([(100 + n * 0.02, False) for n in range(200)])
        result, code = hearing.report(played, deaf)
        self.assertEqual((code, result['heard'], result['bursts_heard']), (1, False, 0))
        half = self.listen([(100 + n * 0.02, n < 50) for n in range(200)])
        self.assertEqual(hearing.report(played, half)[1], 2)
        result, code = hearing.report([], deaf, failure='the talker could not join: no room')
        self.assertEqual(code, 3)
        self.assertIn('no room', result['failure'])


class CheckTests(unittest.IsolatedAsyncioTestCase):
    """check() end to end on the fake daily-python: the room plays the talker's bursts into the
    bot's speaker, through completions only, as daily-python does."""

    def fake_talker(self, sdk, *, deaf=False):
        async def start(_url, bursts, seconds, gap):
            talker = SimpleNamespace(events=asyncio.Queue())

            async def play():
                await talker.events.put({'talker': 'joined'})
                while 'speaker' not in sdk.Daily.devices:
                    await asyncio.sleep(0.01)
                device = sdk.Daily.devices['speaker']
                await asyncio.sleep(0.1)
                sound = hearing.burst(seconds)
                for index in range(bursts):
                    start = time.time()
                    if not deaf:
                        with device._lock:
                            device.plays.extend(sound[at:at + 960] for at in range(0, len(sound), 960))
                    await asyncio.sleep(seconds)
                    await talker.events.put({'burst': index, 'start': start, 'end': time.time()})
                    await asyncio.sleep(gap)
                await talker.events.put({'talker': 'done'})

            task = asyncio.create_task(play())

            async def stop():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            talker.stop = stop
            return talker
        return start

    async def run_check(self, **options):
        sdk = fake_sdk()
        return await hearing.check(
            'https://kaelor.daily.co/check-room', start_talker=self.fake_talker(sdk, **options),
            sdk=sdk, bursts=3, seconds=0.4, gap=0.4, deadline=20)

    async def test_a_room_that_is_heard(self):
        result, code = await self.run_check()
        self.assertEqual(code, 0, result)
        self.assertEqual(result['bursts_heard'], 3)
        self.assertLess(result['latency_max_ms'], 300)
        self.assertGreaterEqual(result['voiced_ms'], 0.8 * result['played_ms'])
        self.assertGreater(result['level_db'], -30)
        self.assertEqual(result['speaker']['read_errors'], 0)

    async def test_a_deaf_room(self):
        result, code = await self.run_check(deaf=True)
        self.assertEqual(code, 1, result)
        self.assertEqual(result['voiced_ms'], 0)
        self.assertEqual(result['bursts_played'], 3)

    async def test_a_talker_that_never_plays(self):
        async def start(_url, *_args):
            talker = SimpleNamespace(events=asyncio.Queue())
            await talker.events.put({'talker': 'failed', 'error': 'the talker could not join: gone'})

            async def stop():
                pass
            talker.stop = stop
            return talker
        result, code = await hearing.check('https://kaelor.daily.co/check-room',
                                           start_talker=start, sdk=fake_sdk(), deadline=10)
        self.assertEqual(code, 3, result)
        self.assertIn('could not join', result['failure'])


if __name__ == '__main__':
    unittest.main()
