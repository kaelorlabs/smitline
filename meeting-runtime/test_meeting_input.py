import struct
import unittest
from types import SimpleNamespace

from meeting_input import INPUT_SILENT_SECONDS, InputMonitor, pcm_level


def chunk(value, samples=480):
    """20 ms at 24 kHz of one sample value: its mean level is that value."""
    return struct.pack(f'<{samples}h', *([value] * samples))


class LevelTests(unittest.TestCase):
    def test_silence_speech_and_the_peak(self):
        self.assertEqual(pcm_level(bytes(960)), (-100.0, 0))
        self.assertEqual(pcm_level(b''), (-100.0, 0))
        level, peak = pcm_level(chunk(1000))
        self.assertAlmostEqual(level, -30.3, places=1)
        self.assertEqual(peak, 1000)
        self.assertEqual(pcm_level(chunk(-32768))[1], 32768)
        self.assertEqual(pcm_level(chunk(500) + b'\x01')[1], 500)  # an odd byte is left out


class InputMonitorTests(unittest.TestCase):
    def setUp(self):
        self.now = [100.0]
        self.monitor = InputMonitor(clock=lambda: self.now[0])

    def test_counts_what_came_in_and_how_much_was_voiced(self):
        monitor = self.monitor
        for value in (0, 2000, 2000, 20, -3000):
            monitor.feed(chunk(value))
            self.now[0] += 0.02
        self.assertEqual(monitor.bytes_in, 5 * 960)
        self.assertEqual(monitor.voiced_ms, 60)  # the quiet hum (-64 dB) is sound, not a voice
        self.assertEqual(monitor.peak, 3000)
        self.assertAlmostEqual(monitor.last_voiced_at, 100.08)
        self.assertEqual(monitor.stats(), {'bytes': 4800, 'voiced_ms': 60, 'peak': 3000})
        self.assertEqual(monitor.stats(SimpleNamespace(dropped_ms=40))['dropped_ms'], 40)
        self.assertNotIn('dropped_ms', monitor.stats(SimpleNamespace()))  # a browser cannot tell

    def test_people_shown_speaking_with_nothing_coming_in_is_reported_once(self):
        # The 2026-11-05 Daily room: Daily showed two people taking turns, GPT-Live got silence.
        monitor = self.monitor
        reports = []
        for second in range(30):
            monitor.feed(bytes(960))
            name = 'Arun' if second % 6 < 3 else 'Bea'
            reports.append(monitor.speaker(name))
            self.now[0] += 1
        found = [report for report in reports if report]
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0], {'seconds': INPUT_SILENT_SECONDS, 'bytes': 960 * 11, 'peak': 0})
        self.assertEqual(reports.index(found[0]), INPUT_SILENT_SECONDS)

    def test_one_person_talking_to_a_deaf_room_is_reported(self):
        # A platform that keeps naming the last speaker: Arun alone, never heard.
        monitor = self.monitor
        reports = []
        for _ in range(12):
            monitor.feed(bytes(960))
            reports.append(monitor.speaker('Arun'))
            self.now[0] += 1
        self.assertEqual(sum(1 for report in reports if report), 1)

    def test_people_who_are_heard_are_not_reported(self):
        monitor = self.monitor
        monitor.feed(chunk(2500))  # their first words come in before the meeting names them
        self.now[0] += 1.5
        self.assertIsNone(monitor.speaker('Arun'))
        for _ in range(30):  # they stop; the platform keeps naming the last speaker
            self.now[0] += 1
            monitor.feed(bytes(960))
            self.assertIsNone(monitor.speaker('Arun'))
        # Later someone new speaks and is heard a moment after being named.
        self.assertIsNone(monitor.speaker('Bea'))
        self.now[0] += 0.5
        monitor.feed(chunk(300))
        for _ in range(15):
            self.now[0] += 1
            self.assertIsNone(monitor.speaker('Bea'))
        self.assertFalse(monitor.silent_reported)

    def test_nobody_speaking_is_not_a_deaf_room(self):
        monitor = self.monitor
        for _ in range(30):
            monitor.feed(bytes(960))
            self.assertIsNone(monitor.speaker(None))
            self.now[0] += 1
        self.assertFalse(monitor.silent_reported)


if __name__ == '__main__':
    unittest.main()
