"""Tests for hold_listener, the local detector behind Hold for Me.

Most tests need numpy, onnxruntime and kaldi_native_fbank (requirements-daemon.txt; the
smitline image has them) and the model files in hold_models/, and skip without them. The audio
in test_fixtures/hold/ is 8 kHz mu-law from Smitline's own phone simulation (see the README
there). The measured wake latency and CPU use are printed at the end of the run.
"""
import os
import shutil
import sys
import tempfile
import unittest

import hold_listener
from hold_listener import HoldListener, HoldListenerUnavailable, Wake

try:
    import numpy as np
except ImportError:
    np = None

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'test_fixtures', 'hold')
RATE = 8000
FRAME = 160
SILENCE = b'\xff'
# rep_greeting.wav: hold music until 1.34 s, a silent line, then the rep. Her voiced "Thanks
# for holding" starts at 3.26 s (the first 10 ms over -55 dBFS after the silence) and "this is
# Dana in billing" ends at 5.1 s.
REP_SPEECH_S = 3.26
# announcement.wav: hold music, then (the music pauses for it) "Your call is important to
# us..." from 1.54 s to 9.13 s. announcement_again.wav: the same recording from 1.64 s to
# 9.23 s, over another stretch of the music 10 dB down.
ANNOUNCEMENT_S = 1.54
ANNOUNCEMENT_AGAIN_S = 1.64
MEASURED = {}


def fixture(name):
    """The mu-law bytes of a test WAV (G.711 mu-law, 8 kHz, mono)."""
    with open(os.path.join(FIXTURES, name), 'rb') as file:
        data = file.read()
    assert data[:4] == b'RIFF' and data[8:12] == b'WAVE', name
    at = 12
    while at + 8 <= len(data):
        chunk, size = data[at:at + 4], int.from_bytes(data[at + 4:at + 8], 'little')
        if chunk == b'fmt ':
            # format 7 (mu-law), one channel, 8000 samples a second
            assert data[at + 8:at + 16] == b'\x07\x00\x01\x00\x40\x1f\x00\x00', name
        elif chunk == b'data':
            return data[at + 8:at + 8 + size]
        at += 8 + size + size % 2
    raise ValueError(f'{name} has no audio')


def unavailable(detector):
    try:
        HoldListener.create({'SMITLINE_HOLD_DETECTOR': detector})
    except HoldListenerUnavailable as error:
        return error.reason
    return None


UNAVAILABLE = {detector: unavailable(detector) for detector in hold_listener.DETECTORS}


def ulaw(samples):
    """G.711 mu-law bytes of samples on the 16-bit scale."""
    s = np.clip(np.round(np.asarray(samples, dtype=np.float64)), -32635, 32635).astype(np.int64)
    sign = np.where(s < 0, 0x80, 0)
    value = np.abs(s) + 0x84
    exponent = np.clip(np.floor(np.log2(value)).astype(np.int64) - 7, 0, 7)
    mantissa = (value >> (exponent + 3)) & 0x0F
    return (~(sign | (exponent << 4) | mantissa) & 0xFF).astype(np.uint8).tobytes()


def g711(code):
    """One mu-law byte decoded the long way (ITU-T G.711)."""
    code = ~code & 0xFF
    magnitude = ((((code & 0x0F) << 3) + 0x84) << ((code >> 4) & 0x07)) - 0x84
    return -magnitude if code & 0x80 else magnitude


def decoded(data):
    return hold_listener.ulaw_decode(data).astype(np.float64)


def at_level(samples, dbfs):
    rms = float(np.sqrt(np.mean(np.square(samples)))) or 1.0
    return samples * (32768 * 10 ** (dbfs / 20) / rms)


def tones(freqs, seconds, dbfs=-20, on=None, off=None):
    t = np.arange(int(seconds * RATE)) / RATE
    x = sum(np.sin(2 * np.pi * f * t) for f in freqs)
    if on:
        x = x * ((t % (on + off)) < on)
    return ulaw(at_level(x, dbfs))


def keypad(digits='15900951' * 3):
    keys = {'0': (941, 1336), '1': (697, 1209), '5': (770, 1336), '9': (852, 1477)}
    t = np.arange(int(0.1 * RATE)) / RATE
    gap = np.zeros(len(t))
    x = np.concatenate([np.concatenate((np.sin(2 * np.pi * keys[d][0] * t)
                                        + np.sin(2 * np.pi * keys[d][1] * t), gap))
                        for d in digits])
    return ulaw(at_level(x, -20))


def noise(seconds, dbfs, seed=1):
    return ulaw(at_level(np.random.default_rng(seed).standard_normal(int(seconds * RATE)), dbfs))


def music(seconds):
    """The hold music looped to `seconds`, joined with 40 ms crossfades (no clicks)."""
    clip = decoded(fixture('hold_music.wav'))
    n = int(0.04 * RATE)
    ramp = np.linspace(0.0, 1.0, n)
    out = clip
    while len(out) < seconds * RATE:
        out = np.concatenate((out[:-n], out[-n:] * (1 - ramp) + clip[:n] * ramp, clip[n:]))
    return ulaw(out[:int(seconds * RATE)])


VOWELS = ((730, 1090, 2440), (530, 1840, 2480), (390, 1990, 2550), (570, 840, 2410),
          (440, 1020, 2240))


def singing(seconds, seed=3):
    """A crude singer over the hold music: held vowels (harmonics shaped by vowel formants)
    with vibrato, a note every 0.35-0.8 s around 165-330 Hz."""
    rng = np.random.default_rng(seed)
    notes, total = [], int(seconds * RATE)
    while sum(map(len, notes)) < total:
        n = int(rng.uniform(0.35, 0.8) * RATE)
        f0 = 165 * 2 ** (rng.choice((0, 2, 4, 5, 7, 9, 11, 12)) / 12)
        formants = VOWELS[int(rng.integers(len(VOWELS)))]
        t = np.arange(n) / RATE
        pitch = f0 * (1 + 0.015 * np.sin(2 * np.pi * 5.5 * t) * np.minimum(t / 0.15, 1))
        phase = 2 * np.pi * np.cumsum(pitch) / RATE
        k = np.arange(1, int(3400 // f0) + 1)
        gains = sum(1 / (1 + ((k * f0 - f) / (60 + 0.05 * f)) ** 2) for f in formants) / k
        edge = np.minimum(1, np.minimum(np.arange(n), n - np.arange(n)) / (0.03 * RATE))
        notes.append((gains[:, None] * np.sin(k[:, None] * phase)).sum(axis=0) * (0.3 + 0.7 * edge))
    voice = at_level(np.concatenate(notes)[:total], -25)
    return ulaw(voice + at_level(decoded(music(seconds)), -30))


JOIN = int(0.03 * RATE)


def join(*clips):
    """Clips as one continuous line, each blending into the next over 30 ms (a hard cut
    clicks, and a click can sound like the start of speech)."""
    out = decoded(clips[0])
    ramp = np.linspace(0.0, 1.0, JOIN)
    for clip in clips[1:]:
        x = decoded(clip)
        out = np.concatenate((out[:-JOIN], out[-JOIN:] * (1 - ramp) + x[:JOIN] * ramp, x[JOIN:]))
    return ulaw(out)


class Line:
    """Plays phone_line's part. Clips are joined into one line as join() does and fed 20 ms
    at a time; like GPT-Live finding that a wake was a recording, it calls
    remember_recording() and listen() `decide_s` after any wake that was not a person."""

    def __init__(self, listener, decide_s=2.5):
        self.listener = listener
        self.decide_s = decide_s
        self.seconds = 0.0            # audio fed so far
        self.started = 0.0            # where the last clip played starts on the line
        self.wakes = []               # (seconds when it woke, Wake, what was playing)
        self._due = None
        self._placed = 0              # samples placed on the line (fed, or waiting in _rest)
        self._held = None             # the end of the last clip, to blend into the next
        self._rest = b''              # less than a frame, fed with the next clip

    def play(self, data, what, continues=False):
        """Add a clip to the line, blended into the last one (or straight after it, if it
        `continues` it), and feed it."""
        x = decoded(data)
        start = self._placed
        if self._held is not None:
            if continues:
                x = np.concatenate((self._held, x))
                start += len(self._held)
            else:
                ramp = np.linspace(0.0, 1.0, len(self._held))
                x[:len(ramp)] = self._held * (1 - ramp) + x[:len(ramp)] * ramp
        self.started = start / RATE
        self._held, x = x[-JOIN:], x[:-JOIN]
        self._placed += len(x)
        data = self._rest + ulaw(x)
        whole = len(data) - len(data) % FRAME
        for i in range(0, whole, FRAME):
            self.seconds += FRAME / RATE
            wake = self.listener.feed(data[i:i + FRAME])
            if self._due is not None and self.seconds >= self._due:
                self._due = None
                self.listener.remember_recording()
                self.listener.listen()
            if wake:
                self.wakes.append((self.seconds, wake, what))
                if what != 'person':
                    self._due = self.seconds + self.decide_s
        self._rest = data[whole:]
        return self


@unittest.skipUnless(np, 'needs numpy')
class AudioTests(unittest.TestCase):
    def test_mulaw_decoding_follows_g711(self):
        samples = hold_listener.ulaw_decode(bytes(range(256)))
        self.assertEqual(samples.tolist(), [float(g711(code)) for code in range(256)])
        self.assertEqual((samples[0xFF], samples[0x7F]), (0, 0))
        self.assertEqual((samples[0x00], samples[0x80]), (-32124, 32124))

    def test_mulaw_round_trip_is_within_one_step(self):
        x = np.linspace(-32000, 32000, 20001)
        error = np.abs(decoded(ulaw(x)) - x)
        self.assertTrue(np.all(error <= np.maximum(8, np.abs(x) / 16)))

    def test_upsampling_keeps_the_phone_band_and_suppresses_images(self):
        for f in (300, 1000, 3000):
            with self.subTest(hz=f):
                x = 8000 * np.sin(2 * np.pi * f * np.arange(RATE) / RATE)
                y = hold_listener.Upsampler()(x)[200:]
                spectrum = np.abs(np.fft.rfft(y * np.hanning(len(y))))
                bins = np.fft.rfftfreq(len(y), 1 / (2 * RATE))
                tone = spectrum[np.argmin(np.abs(bins - f))]
                image = spectrum[np.argmin(np.abs(bins - (RATE - f)))]
                gain = np.sqrt(np.mean(y ** 2)) / np.sqrt(np.mean(x ** 2))
                self.assertAlmostEqual(20 * np.log10(gain), 0.0, delta=0.3)
                self.assertLess(20 * np.log10(image / tone), -70)

    def test_upsampling_a_frame_at_a_time_matches_one_block(self):
        x = np.random.default_rng(1).standard_normal(4000) * 3000
        whole = hold_listener.Upsampler()(x)
        upsampler = hold_listener.Upsampler()
        frames = np.concatenate([upsampler(x[i:i + FRAME]) for i in range(0, len(x), FRAME)])
        np.testing.assert_allclose(frames, whole, atol=0.05)
        self.assertEqual(len(upsampler(np.zeros(0))), 0)

    def test_upsampling_delay(self):
        x = np.zeros(400)
        x[100] = 1000.0
        y = hold_listener.Upsampler()(x)
        self.assertEqual(int(np.argmax(y)), 200 + hold_listener.Upsampler.DELAY_16K)


class CreateTests(unittest.TestCase):
    def test_unknown_detector(self):
        with self.assertRaises(HoldListenerUnavailable) as caught:
            HoldListener.create({'SMITLINE_HOLD_DETECTOR': 'webrtc'})
        self.assertIn("'webrtc'", caught.exception.reason)

    def test_missing_model_files(self):
        with tempfile.TemporaryDirectory() as empty:
            for detector, file in (('', 'fireredvad_stream_vad_with_cache.onnx'),
                                   ('firered', 'fireredvad_stream_vad_with_cache.onnx'),
                                   ('silero', 'silero_vad.onnx'),
                                   ('silero16k', 'silero_vad.onnx')):
                with self.subTest(detector=detector):
                    with self.assertRaises(HoldListenerUnavailable) as caught:
                        HoldListener.create({'SMITLINE_HOLD_DETECTOR': detector,
                                             'SMITLINE_HOLD_MODELS': empty})
                    self.assertIn(f'{file} is missing', caught.exception.reason)
            open(os.path.join(empty, 'fireredvad_stream_vad_with_cache.onnx'), 'wb').close()
            with self.assertRaises(HoldListenerUnavailable) as caught:
                HoldListener.create({'SMITLINE_HOLD_MODELS': empty})
            self.assertIn('cmvn.ark is missing', caught.exception.reason)

    def test_missing_packages(self):
        missing = object()
        saved = hold_listener._ort, sys.modules.get('onnxruntime', missing)
        hold_listener._ort = None
        sys.modules['onnxruntime'] = None     # import onnxruntime now fails
        try:
            with tempfile.TemporaryDirectory() as folder:
                open(os.path.join(folder, 'silero_vad.onnx'), 'wb').close()
                with self.assertRaises(HoldListenerUnavailable) as caught:
                    HoldListener.create({'SMITLINE_HOLD_DETECTOR': 'silero',
                                         'SMITLINE_HOLD_MODELS': folder})
        finally:
            hold_listener._ort = saved[0]
            if saved[1] is missing:
                del sys.modules['onnxruntime']
            else:
                sys.modules['onnxruntime'] = saved[1]
        self.assertIn('is not installed', caught.exception.reason)

    @unittest.skipIf(UNAVAILABLE['silero'], UNAVAILABLE['silero'])
    def test_broken_model_file(self):
        with tempfile.TemporaryDirectory() as folder:
            with open(os.path.join(folder, 'silero_vad.onnx'), 'wb') as file:
                file.write(b'not a model')
            with self.assertRaises(HoldListenerUnavailable) as caught:
                HoldListener.create({'SMITLINE_HOLD_DETECTOR': 'silero',
                                     'SMITLINE_HOLD_MODELS': folder})
        self.assertIn('cannot load silero_vad.onnx', caught.exception.reason)

    @unittest.skipIf(UNAVAILABLE['firered'] or UNAVAILABLE['silero'],
                     UNAVAILABLE['firered'] or UNAVAILABLE['silero'])
    def test_runs_without_the_music_model(self):
        for detector, files in (('firered', ('fireredvad_stream_vad_with_cache.onnx', 'cmvn.ark')),
                                ('silero', ('silero_vad.onnx',))):
            with self.subTest(detector=detector), tempfile.TemporaryDirectory() as folder:
                for file in files:
                    shutil.copy(os.path.join(hold_listener.MODELS_DIR, file), folder)
                listener = HoldListener.create({'SMITLINE_HOLD_DETECTOR': detector,
                                                'SMITLINE_HOLD_MODELS': folder})
                listener.listen()
                line = Line(listener).play(music(3), 'music')
                line.play(fixture('rep_greeting.wav'), 'person')
                self.assertEqual([what for _, _, what in line.wakes], ['person'])
                self.assertEqual(set(line.wakes[0][1].scores), {'vad', 'repeat'})

    def test_music_rule(self):
        is_music = hold_listener._Aed.is_music
        self.assertTrue(is_music({'speech': 0.0, 'singing': 0.9, 'music': 1.0}))
        self.assertTrue(is_music({'speech': 0.1, 'singing': 0.0, 'music': 0.7}))
        self.assertFalse(is_music({'speech': 0.6, 'singing': 0.0, 'music': 1.0}))
        self.assertFalse(is_music({'speech': 0.0, 'singing': 0.2, 'music': 0.3}))


@unittest.skipIf(UNAVAILABLE['firered'], UNAVAILABLE['firered'])
class FingerprintTests(unittest.TestCase):
    def test_the_same_recording_matches_and_other_sounds_do_not(self):
        hold_listener._load_packages()
        announcement = decoded(fixture('announcement.wav'))
        again = decoded(fixture('announcement_again.wav'))
        rep = decoded(fixture('rep_greeting.wav'))
        piece = lambda x, start: x[int(start * RATE):int((start + 0.95) * RATE)]
        fingerprints = hold_listener._Fingerprints()
        self.assertTrue(fingerprints.remember(announcement[int(0.94 * RATE):int(9.2 * RATE)]))
        match = fingerprints.best_match
        # The repeat, over other music: its first second, and later on.
        for start in (ANNOUNCEMENT_AGAIN_S - 0.15, ANNOUNCEMENT_AGAIN_S + 0.3, 5.0, 8.0):
            self.assertGreater(match(piece(again, start)), hold_listener.REPEAT_MATCH + 0.05)
        # Other speech in the same synthetic voice, and the hold music.
        self.assertLess(match(piece(rep, REP_SPEECH_S - 0.15)), hold_listener.REPEAT_MATCH - 0.2)
        self.assertLess(match(piece(rep, REP_SPEECH_S + 1.5)), hold_listener.REPEAT_MATCH - 0.2)
        self.assertLess(match(piece(decoded(fixture('hold_music.wav')), 3.0)),
                        hold_listener.REPEAT_MATCH - 0.2)
        # Too little audio to tell is no match.
        self.assertEqual(match(announcement[int(2 * RATE):int(2.2 * RATE)]), 0.0)


class _Detector:
    """The same cases for each detector; subclasses name it."""
    detector = None

    def listener(self):
        return HoldListener.create({'SMITLINE_HOLD_DETECTOR': self.detector})

    def measured(self, **values):
        MEASURED.setdefault(self.detector, {}).update(values)

    def test_name_and_stats(self):
        listener = self.listener()
        self.assertEqual(listener.name, self.detector)
        self.assertEqual(set(listener.stats),
                         {'wakes', 'skippedRepeats', 'skippedMusic', 'seconds', 'cpuMs'})
        line = Line(listener).play(music(2), 'music')
        self.assertEqual(listener.stats['seconds'], round(line.seconds, 3))
        self.assertGreater(line.seconds, 1.9)
        self.assertGreater(listener.stats['cpuMs'], 0)
        self.assertEqual(listener.stats['wakes'], 0)

    def test_silence_noise_and_tones_never_wake(self):
        sounds = {'silence': SILENCE * 10 * RATE, 'hiss': noise(10, -60),
                  'loud noise': noise(10, -30, seed=2),
                  'ringback': tones((440, 480), 12, on=2, off=4),
                  'busy': tones((480, 620), 6, on=0.5, off=0.5),
                  'keypad': keypad(), 'test tone': tones((1000,), 6)}
        for what, sound in sounds.items():
            with self.subTest(sound=what):
                listener = self.listener()
                listener.listen()
                self.assertEqual(Line(listener).play(sound, what).wakes, [])

    def test_hold_music_never_wakes(self):
        listener = self.listener()
        listener.listen()
        self.assertEqual(Line(listener).play(music(60), 'music').wakes, [])
        self.measured(cpu_ms_per_minute=listener.stats['cpuMs'])
        self.assertLess(listener.stats['cpuMs'], 6000)   # under 10% of one core

    def test_singing_over_hold_music_does_not_wake(self):
        listener = self.listener()
        listener.listen()
        self.assertEqual(Line(listener).play(singing(20), 'singing').wakes, [])
        self.measured(singing_skipped=listener.stats['skippedMusic'])

    def test_the_rep_wakes_it_soon_after_she_starts_talking(self):
        listener = self.listener()
        listener.listen()
        line = Line(listener).play(music(4), 'music').play(fixture('rep_greeting.wav'), 'person')
        self.assertEqual(len(line.wakes), 1)
        woke, wake, what = line.wakes[0]
        self.assertEqual(what, 'person')
        self.assertIsInstance(wake, Wake)
        self.assertIsInstance(wake.onset_ms_ago, int)
        self.assertIsInstance(wake.speech_ms, int)
        speech = line.started + REP_SPEECH_S
        latency = woke - speech
        replay = woke - wake.onset_ms_ago / 1000 - speech
        self.measured(latency_ms=round(latency * 1000), replay_from_ms=round(replay * 1000))
        self.assertGreater(latency, 0.3)
        self.assertLess(latency, 1.2)
        self.assertLessEqual(replay, 0.03)    # the replay holds her first word...
        self.assertGreater(replay, -0.4)      # ...and not much before it
        self.assertGreaterEqual(wake.speech_ms, 300)
        self.assertGreater(wake.scores['vad'], 0.3)
        self.assertEqual(wake.scores['repeat'], 0.0)
        self.assertGreater(wake.scores['speech'], 0.5)
        self.assertEqual(listener.stats['wakes'], 1)

    def test_speech_over_music_wakes_it(self):
        listener = self.listener()
        listener.listen()
        line = Line(listener).play(music(3), 'music')
        line.play(fixture('announcement_again.wav'), 'recording')
        self.assertTrue(line.wakes)
        woke, wake, _ = line.wakes[0]
        self.assertGreater(woke - (line.started + ANNOUNCEMENT_AGAIN_S), 0.3)
        self.assertLess(woke - (line.started + ANNOUNCEMENT_AGAIN_S), 1.3)
        self.assertGreaterEqual(wake.scores['speech'], 0.5)

    def test_an_announcement_wakes_it_once_then_its_repeats_are_skipped(self):
        listener = self.listener()
        listener.listen()
        line = Line(listener).play(music(3), 'music')
        announcement = line.play(fixture('announcement.wav'), 'recording').started
        line.play(music(5), 'music')
        first = list(line.wakes)
        self.assertTrue(first)
        self.assertEqual({what for _, _, what in first}, {'recording'})
        self.assertLess(first[0][0] - (announcement + ANNOUNCEMENT_S), 1.3)
        # The same recording again, over other music, and once more as it was.
        line.play(fixture('announcement_again.wav'), 'recording').play(music(5), 'music')
        line.play(fixture('announcement.wav'), 'recording').play(music(5), 'music')
        self.assertEqual(line.wakes[len(first):], [])
        self.assertGreaterEqual(listener.stats['skippedRepeats'], 2)
        # A person still wakes it.
        line.play(fixture('rep_greeting.wav'), 'person')
        self.assertEqual([what for _, _, what in line.wakes[len(first):]], ['person'])
        self.assertLess(line.wakes[-1][1].scores['repeat'], hold_listener.REPEAT_MATCH - 0.15)
        self.assertEqual(listener.stats['wakes'], len(first) + 1)
        self.measured(announcement_wakes=len(first), repeats_skipped=listener.stats['skippedRepeats'])

    def test_nothing_wakes_it_before_listen_or_after_a_wake(self):
        listener = self.listener()
        rep = fixture('rep_greeting.wav')
        line = Line(listener).play(music(3), 'music').play(rep, 'person')
        self.assertEqual(line.wakes, [])
        listener.listen()
        self.assertTrue(listener.listening)
        line.play(rep, 'person')
        self.assertEqual(len(line.wakes), 1)
        self.assertFalse(listener.listening)
        line.play(rep, 'person')          # still fed while the voice is awake: no wake
        self.assertEqual(len(line.wakes), 1)
        self.assertEqual(listener.stats['seconds'], round(line.seconds, 3))
        listener.listen()
        line.play(rep, 'person')
        self.assertEqual(len(line.wakes), 2)

    def test_speech_already_going_on_at_listen_is_not_new(self):
        listener = self.listener()
        rep = fixture('rep_greeting.wav')
        cut, end = int((REP_SPEECH_S + 0.5) * RATE), int(5.1 * RATE)
        line = Line(listener).play(music(3), 'music').play(rep[:cut], 'person')
        listener.listen()
        line.play(rep[cut:end], 'person', continues=True).play(SILENCE * RATE, 'silence')
        self.assertEqual(line.wakes, [])
        line.play(rep, 'person')
        self.assertEqual(len(line.wakes), 1)

    def test_remember_recording_needs_a_wake(self):
        listener = self.listener()
        self.assertFalse(listener.remember_recording())
        listener.listen()
        line = Line(listener).play(music(2), 'music').play(fixture('rep_greeting.wav'), 'person')
        self.assertEqual(len(line.wakes), 1)
        self.assertTrue(listener.remember_recording())
        self.assertFalse(listener.remember_recording())

    def test_frames_of_other_sizes(self):
        listener = self.listener()
        listener.listen()
        data = join(music(2), fixture('rep_greeting.wav'))
        wakes = [wake for i in range(0, len(data), 240) if (wake := listener.feed(data[i:i + 240]))]
        self.assertEqual(len(wakes), 1)


@unittest.skipIf(UNAVAILABLE['firered'], UNAVAILABLE['firered'])
class FireRedTests(_Detector, unittest.TestCase):
    detector = 'firered'

    def test_is_the_default(self):
        self.assertEqual(HoldListener.create({}).name, 'firered')

    def test_its_music_model_skips_singing(self):
        listener = self.listener()
        listener.listen()
        Line(listener).play(singing(20), 'singing')
        self.assertGreater(listener.stats['skippedMusic'], 0)


@unittest.skipIf(UNAVAILABLE['silero'], UNAVAILABLE['silero'])
class SileroTests(_Detector, unittest.TestCase):
    detector = 'silero'


@unittest.skipIf(UNAVAILABLE['silero16k'], UNAVAILABLE['silero16k'])
class Silero16kTests(_Detector, unittest.TestCase):
    detector = 'silero16k'


def tearDownModule():
    for detector, values in MEASURED.items():
        sys.stderr.write(f'\nhold_listener {detector}: '
                         + ', '.join(f'{k} {v}' for k, v in sorted(values.items())))
    if MEASURED:
        sys.stderr.write('\n')


if __name__ == '__main__':
    unittest.main()
