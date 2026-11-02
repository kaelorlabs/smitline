import array
import base64
import os
import tempfile
import unittest
import wave
from pathlib import Path

from call_recorder import MAX_AHEAD, ULAW, CallRecorder


def frame(byte, samples=160):
    return base64.b64encode(bytes([byte]) * samples).decode()


class CallRecorderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'recording.wav'

    def tearDown(self):
        self.temp.cleanup()

    def read(self):
        with wave.open(str(self.path)) as audio:
            self.assertEqual((audio.getnchannels(), audio.getframerate(), audio.getsampwidth()),
                             (2, 8000, 2))
            samples = array.array('h', audio.readframes(audio.getnframes()))
        return list(samples[0::2]), list(samples[1::2])

    def test_mu_law_matches_g711(self):
        self.assertEqual((ULAW[0xFF], ULAW[0x7F]), (0, 0))
        self.assertEqual((ULAW[0x00], ULAW[0x80]), (-32124, 32124))

    def test_both_sides_line_up_on_the_incoming_clock(self):
        recorder = CallRecorder(self.path)
        recorder.agent(frame(0x2A))  # the voice speaks...
        recorder.other(frame(0x9A))  # ...heard against the other person's next frame
        recorder.other(frame(0xFF))  # then both are quiet
        self.assertEqual(recorder.close(), 1)
        left, right = self.read()
        self.assertEqual(left[:160], [ULAW[0x9A]] * 160)
        self.assertEqual(right[:160], [ULAW[0x2A]] * 160)
        self.assertEqual(set(left[160:] + right[160:]), {0})
        self.assertEqual(len(left), 320)
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o600)

    def test_the_voices_last_words_are_kept(self):
        recorder = CallRecorder(self.path)
        recorder.other(frame(0xFF))
        recorder.agent(frame(0x2A, 800))  # said after the other person's last frame
        recorder.close()
        left, right = self.read()
        self.assertEqual(len(right), 960)
        self.assertEqual(right[160:], [ULAW[0x2A]] * 800)
        self.assertEqual(set(left), {0})

    def test_a_stalled_incoming_side_does_not_hold_the_voice_forever(self):
        recorder = CallRecorder(self.path)
        for _ in range(150):  # 3 seconds of the voice, nothing coming in
            recorder.agent(frame(0x2A))
        self.assertEqual(recorder.frames, 150 * 160 - MAX_AHEAD)  # all but the last 2 s written
        recorder.close()
        left, right = self.read()
        self.assertEqual(len(right), 150 * 160)
        self.assertEqual(set(right), {ULAW[0x2A]})
        self.assertEqual(set(left), {0})

    def test_nothing_heard_leaves_no_file(self):
        recorder = CallRecorder(self.path)
        self.assertEqual(recorder.close(), 0)
        self.assertEqual(list(Path(self.temp.name).iterdir()), [])

    def test_partial_until_closed_and_closing_twice_is_harmless(self):
        recorder = CallRecorder(self.path)
        recorder.other(frame(0x9A))
        self.assertFalse(self.path.exists())
        self.assertTrue(Path(str(self.path) + '.partial').exists())
        self.assertEqual(recorder.close(), 1)
        self.assertEqual(recorder.close(), 1)
        recorder.other(frame(0x9A))  # after the end: ignored
        self.assertEqual(len(self.read()[0]), 160)

    def test_bad_payloads_are_skipped(self):
        recorder = CallRecorder(self.path)
        recorder.other('not base64!!')
        recorder.agent(None)
        recorder.other(frame(0x9A))
        recorder.close()
        self.assertEqual(len(self.read()[0]), 160)


if __name__ == '__main__':
    unittest.main()
