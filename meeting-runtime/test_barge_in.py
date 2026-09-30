import unittest

from barge_in import SpeechDetector, frame_level_db


def mulaw(sample):
    """G.711 mu-law encoding of one 16-bit sample."""
    sign = 0x80 if sample < 0 else 0
    value = min(abs(sample), 32635) + 0x84
    exponent, mask = 7, 0x4000
    while exponent > 0 and not value & mask:
        exponent -= 1
        mask >>= 1
    mantissa = (value >> (exponent + 3)) & 0x0F
    return ~(sign | (exponent << 4) | mantissa) & 0xFF


def tone(amplitude, ms=20):
    """ms of a 1 kHz square wave at the given amplitude, as 8 kHz mu-law."""
    return bytes(mulaw(amplitude if (i // 4) % 2 else -amplitude) for i in range(8 * ms))


SILENCE = tone(0)
QUIET = tone(60)       # line hiss, about -55 dBFS
SPEECH = tone(3000)    # ordinary phone speech, about -21 dBFS
ECHO = tone(400)       # the assistant's own voice leaking back, about -38 dBFS


def feed(detector, frame, count, playing=False):
    return [change for _ in range(count) if (change := detector.feed(frame, playing=playing))]


class SpeechDetectorTests(unittest.TestCase):
    def test_levels(self):
        self.assertEqual(frame_level_db(b''), -100.0)
        self.assertLess(frame_level_db(SILENCE), -70)
        self.assertAlmostEqual(frame_level_db(SPEECH), -20.8, delta=1.0)

    def test_speech_starts_after_160_ms_and_ends_after_450_ms_of_quiet(self):
        detector = SpeechDetector()
        self.assertEqual(feed(detector, QUIET, 50), [])
        self.assertEqual(feed(detector, SPEECH, 7), [])
        self.assertEqual(detector.feed(SPEECH), 'start')
        self.assertEqual(detector.speech_ms, 160)
        feed(detector, SPEECH, 20)
        self.assertEqual(detector.speech_ms, 560)
        self.assertEqual(feed(detector, QUIET, 22), [])
        self.assertEqual(detector.feed(QUIET), 'end')
        self.assertEqual(detector.last_speech_ms, 560)
        self.assertFalse(detector.speaking)

    def test_a_click_is_not_speech(self):
        detector = SpeechDetector()
        feed(detector, QUIET, 50)
        for _ in range(10):
            self.assertEqual(feed(detector, SPEECH, 2) + feed(detector, QUIET, 3), [])

    def test_echo_of_its_own_voice_does_not_count_while_playing(self):
        detector = SpeechDetector()
        feed(detector, QUIET, 50)
        self.assertEqual(feed(detector, ECHO, 50, playing=True), [])
        self.assertEqual(feed(detector, SPEECH, 8, playing=True), ['start'])

    def test_steady_noise_becomes_the_floor(self):
        detector = SpeechDetector()
        feed(detector, QUIET, 50)
        self.assertEqual(feed(detector, SPEECH, 8), ['start'])
        # A loud TV that never stops is eventually treated as the room's noise.
        self.assertIn('end', feed(detector, SPEECH, 3000))


if __name__ == '__main__':
    unittest.main()
