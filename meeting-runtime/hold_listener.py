"""Hear a live person come back on a phone line that is on hold ("Hold for Me").

While a call waits on hold (banks, airlines and government lines keep people there for 15-60
minutes or more), phone_line closes GPT-Live, which bills every second it is open, and feeds
the line's audio here instead: one 20 ms mu-law frame at 8 kHz at a time. A small voice
activity model runs on the CPU. When speech starts, HoldListener waits DECIDE_MS (or for the
end of the speech, if that comes sooner) and then decides:

1. A repeat of a recording it was told about (remember_recording(): an announcement in the
   hold loop) is skipped. Recordings are compared by an audio fingerprint, so the same
   announcement wakes the call at most once.
2. Music or singing with no speech in it is skipped, when FireRedVAD's audio event model
   (hold_models/fireredvad_aed.onnx) is there to tell them apart.
3. Anything else is a Wake: phone_line reopens GPT-Live and replays the audio from the
   onset, so the voice hears the person's first words. GPT-Live has the last word. A false
   wake costs about a cent and the voice goes back to sleep; a missed person says hello to
   silence and hangs up. So the thresholds lean towards waking.

Two models sit behind the same interface, chosen by SMITLINE_HOLD_DETECTOR: 'firered' (the
default; FireRedVAD's streaming model, Apache-2.0, Xiaohongshu) and 'silero' (Silero VAD
v6, MIT). FireRedVAD hears 16 kHz audio, so the phone audio is upsampled for it (the band
above 4 kHz stays empty); 'silero' runs Silero's 8 kHz model on the phone audio as it is,
and 'silero16k' its 16 kHz model on the same upsampled audio, which heard more of the short
words in real calls. The model files are vendored in hold_models/ (see the README there);
SMITLINE_HOLD_MODELS names another folder.

numpy, onnxruntime and kaldi_native_fbank are imported by HoldListener.create(), never at
import time, so this module imports anywhere. create() raises HoldListenerUnavailable when a
package or a model file is missing.
"""
import collections
import dataclasses
import math
import os
import time

SAMPLE_RATE = 8000
DETECTORS = ('firered', 'silero', 'silero16k')
MODELS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'hold_models')
FIRERED_VAD = 'fireredvad_stream_vad_with_cache.onnx'
FIRERED_AED = 'fireredvad_aed.onnx'
FIRERED_CMVN = 'cmvn.ark'
SILERO_VAD = 'silero_vad.onnx'

# Decide this long after a speech onset, or when the speech ends, if that is sooner.
DECIDE_MS = 800
# The models run on this much audio at a time, not on every 20 ms frame (each run of the
# model costs about as much as ten 10 ms steps of it).
BATCH_MS = 100
# The models start cold: for a moment, sound that was already playing (hold music joined
# halfway) looks like an onset. Speech that starts this early is treated as going on already.
WARMUP_MS = 500
# Recent audio kept for remember_recording() and the music check.
BUFFER_MS = 12_000
# remember_recording() fingerprints from just before the wake's onset to this long after it,
# so a repeat is known however the detector happens to split it into stretches of speech.
FINGERPRINT_MS = 8000
# A repeat's onset can be heard a little earlier than the first time (other music before it).
FINGERPRINT_PRE_ROLL_MS = 600
MAX_RECORDINGS = 32
# Fingerprint similarity (0..1) at which speech counts as a remembered recording again.
REPEAT_MATCH = 0.65
# The music check also hears this much audio before the onset.
AED_CONTEXT_MS = 500
# Sound skipped as music is checked again this often while it lasts, so a person who starts
# talking over it is still heard. A remembered recording, and speech that was already going on
# when listen() was called (usually the recording the voice just heard, carrying on past what
# was fingerprinted), are left alone for LONG_RUN_MS before they are checked again.
RECHECK_MS = 1000
LONG_RUN_MS = 10_000


class HoldListenerUnavailable(Exception):
    """The hold listener cannot run here: a package or a model file is missing."""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


@dataclasses.dataclass(frozen=True)
class Wake:
    """A person may be talking. phone_line replays from onset_ms_ago and wakes the voice."""
    onset_ms_ago: int   # the speech began this many ms before the end of the last frame fed
    speech_ms: int      # how much of the audio since the onset the detector took for speech
    # What the decision saw: vad (mean speech probability), repeat (best fingerprint match),
    # and the AED's shares of speech, singing and music frames when it ran.
    scores: dict = dataclasses.field(hash=False)


_np = _ort = _knf = None


def _numpy():
    global _np
    if _np is None:
        try:
            import numpy
        except ImportError:
            raise HoldListenerUnavailable('numpy is not installed') from None
        _np = numpy
    return _np


def _load_packages():
    global _ort, _knf
    _numpy()
    if _ort is None:
        try:
            import onnxruntime
        except ImportError:
            raise HoldListenerUnavailable('onnxruntime is not installed') from None
        _ort = onnxruntime
    if _knf is None:
        try:
            import kaldi_native_fbank
        except ImportError:
            raise HoldListenerUnavailable('kaldi_native_fbank is not installed') from None
        _knf = kaldi_native_fbank


_ULAW = None


def ulaw_decode(data):
    """G.711 mu-law bytes as float32 samples on the 16-bit scale (-32124..32124)."""
    global _ULAW
    np = _numpy()
    if _ULAW is None:
        code = ~np.arange(256) & 0xFF
        magnitude = ((((code & 0x0F) << 3) + 0x84) << ((code >> 4) & 0x07)) - 0x84
        _ULAW = np.where(code & 0x80, -magnitude, magnitude).astype(np.float32)
    return _ULAW[np.frombuffer(bytes(data), dtype=np.uint8)]


class Upsampler:
    """8 kHz to 16 kHz, a block at a time, for the 16 kHz models.

    A 97-tap windowed-sinc low-pass (3.8 kHz cutoff, Kaiser window) applied as two polyphase
    branches, so images of the 0-4 kHz band are suppressed by about 80 dB. The output lags the
    input by DELAY_16K samples (3 ms).
    """
    TAPS = 97
    CUTOFF_HZ = 3800
    BETA = 8.0
    DELAY_16K = (TAPS - 1) // 2

    def __init__(self):
        np = _numpy()
        n = np.arange(self.TAPS) - self.DELAY_16K
        cutoff = self.CUTOFF_HZ / (2 * SAMPLE_RATE)
        taps = 2 * cutoff * np.sinc(2 * cutoff * n) * np.kaiser(self.TAPS, self.BETA)
        taps *= 2 / taps.sum()      # zero stuffing halves the level; each branch sums to ~1
        self.even = taps[0::2]
        self.odd = np.append(taps[1::2], 0.0)
        self.history = np.zeros(len(self.even) - 1)

    def __call__(self, samples):
        np = _numpy()
        if not len(samples):
            return np.zeros(0, dtype=np.float32)
        x = np.concatenate((self.history, samples))
        self.history = x[len(x) - len(self.history):]
        out = np.empty(2 * len(samples), dtype=np.float32)
        out[0::2] = np.convolve(x, self.even, 'valid')
        out[1::2] = np.convolve(x, self.odd, 'valid')
        return out


def _fbank_options(rate, bins, low=20.0, high=0.0):
    """Kaldi log-mel filterbank options (FireRedVAD's front end at 16 kHz and 80 bins)."""
    options = _knf.FbankOptions()
    options.frame_opts.samp_freq = rate
    options.frame_opts.frame_length_ms = 25
    options.frame_opts.frame_shift_ms = 10
    options.frame_opts.dither = 0.0
    options.frame_opts.snip_edges = True
    options.mel_opts.num_bins = bins
    options.mel_opts.low_freq = low
    options.mel_opts.high_freq = high
    options.mel_opts.debug_mel = False
    return options


def _frames(fbank, first, end):
    np = _np
    if end <= first:
        return np.zeros((0, fbank.dim), dtype=np.float32)
    return np.stack([fbank.get_frame(i) for i in range(first, end)]).astype(np.float32)


def _read_cmvn(path):
    """Kaldi global CMVN statistics (cmvn.ark, binary or text) as (mean, 1/std) per bin."""
    np = _np
    with open(path, 'rb') as file:
        data = file.read()
    marker = data.find(b'\0B')
    if marker >= 0:
        kind = data[marker + 2:marker + 5]
        if kind not in (b'DM ', b'FM '):
            raise ValueError(f'{path}: unsupported matrix type {kind!r}')
        at = marker + 5
        rows = int.from_bytes(data[at + 1:at + 5], 'little')
        cols = int.from_bytes(data[at + 6:at + 10], 'little')
        dtype = '<f8' if kind == b'DM ' else '<f4'
        stats = np.frombuffer(data, dtype=dtype, count=rows * cols, offset=at + 10)
        stats = stats.reshape(rows, cols).astype(np.float64)
    else:
        text = data.decode().split('[', 1)[-1].split(']', 1)[0]
        stats = np.array([[float(v) for v in line.split()]
                          for line in text.strip().splitlines() if line.strip()])
    if stats.shape[0] != 2 or stats[0, -1] < 1:
        raise ValueError(f'{path}: not CMVN statistics')
    count = stats[0, -1]
    mean = stats[0, :-1] / count
    variance = np.maximum(stats[1, :-1] / count - mean * mean, 1e-20)
    return mean.astype(np.float32), (1 / np.sqrt(variance)).astype(np.float32)


_SESSIONS = {}


def _session(path):
    """One shared onnxruntime session per model file, on one thread (the state is ours)."""
    path = os.path.abspath(path)
    session = _SESSIONS.get(path)
    if session is None:
        options = _ort.SessionOptions()
        options.intra_op_num_threads = 1
        options.inter_op_num_threads = 1
        options.execution_mode = _ort.ExecutionMode.ORT_SEQUENTIAL
        options.log_severity_level = 3
        try:
            session = _ort.InferenceSession(path, sess_options=options,
                                            providers=['CPUExecutionProvider'])
        except Exception as error:
            raise HoldListenerUnavailable(
                f'cannot load {os.path.basename(path)}: {error}') from None
        _SESSIONS[path] = session
    return session


class _Ring:
    """The last `size` rows of a stream, addressed by absolute row number."""

    def __init__(self, size, width=None):
        np = _np
        self.size = size
        self.data = np.zeros(size if width is None else (size, width), dtype=np.float32)
        self.end = 0

    @property
    def start(self):
        return max(0, self.end - self.size)

    def append(self, rows):
        n = len(rows)
        if n > self.size:
            rows = rows[n - self.size:]
            self.end += n - self.size
            n = self.size
        at = self.end % self.size
        first = min(n, self.size - at)
        self.data[at:at + first] = rows[:first]
        self.data[:n - first] = rows[first:]
        self.end += n

    def get(self, begin, end):
        np = _np
        begin, end = max(begin, self.start), min(end, self.end)
        if end <= begin:
            return self.data[:0]
        return self.data[np.arange(begin, end) % self.size]


class _Features16k:
    """FireRedVAD's front end on 8 kHz audio: upsample, 80 log-mel bins at 16 kHz (25 ms frames
    every 10 ms, int16-scale samples, no dither), then its global CMVN."""
    STEP = SAMPLE_RATE // 100
    # Frame i covers 16 kHz samples [160 i, 160 i + 400) of the upsampled audio, which lags by
    # Upsampler.DELAY_16K. Its 10 ms step, in 8 kHz samples, starts here:
    OFFSET = (200 - Upsampler.DELAY_16K) // 2 - STEP // 2

    def __init__(self, cmvn, keep_frames=0):
        self.mean, self.istd = cmvn
        self.upsampler = Upsampler()
        self.fbank = _knf.OnlineFbank(_fbank_options(2 * SAMPLE_RATE, 80))
        self.done = 0
        self.ring = _Ring(keep_frames, 80) if keep_frames else None

    @classmethod
    def frame_start(cls, index):
        return cls.STEP * index + cls.OFFSET

    def push(self, samples):
        """Features for the frames these samples complete, and the index of the first."""
        self.fbank.accept_waveform(2 * SAMPLE_RATE, self.upsampler(samples))
        first, end = self.done, self.fbank.num_frames_ready
        features = (_frames(self.fbank, first, end) - self.mean) * self.istd
        if end > first:
            self.fbank.pop(end - first)
            self.done = end
            if self.ring is not None:
                self.ring.append(features)
        return first, features

    @classmethod
    def first_frame(cls, at):
        """The first frame whose step starts at or after 8 kHz sample `at`."""
        return -(-(at - cls.OFFSET) // cls.STEP)

    def window(self, begin, onset, end):
        """Kept features of the frames whose steps start in [begin, end) (8 kHz samples), and
        the row of the first one at or after `onset`."""
        first = max(self.first_frame(begin), self.ring.start)
        features = self.ring.get(first, self.first_frame(end))
        return features, min(len(features), max(0, self.first_frame(onset) - first))

    @classmethod
    def of(cls, cmvn, audio, begin, onset, end):
        """The same for audio from the ring, computed afresh (for the AED next to Silero),
        with 50 ms of lead-in for the upsampler and the first frame."""
        start = max(audio.start, begin - SAMPLE_RATE // 20)
        features = cls(cmvn).push(audio.get(start, end))[1]
        first = max(0, cls.first_frame(begin - start))
        features = features[first:]
        return features, min(len(features), max(0, cls.first_frame(onset - start) - first))


@dataclasses.dataclass(frozen=True)
class _Tuning:
    hop: int             # 8 kHz samples per speech probability
    smooth: int          # moving average over this many probabilities
    threshold: float     # speech starts when the average reaches this
    release: float       # and goes on while it stays at or above this
    min_speech_ms: int   # an onset needs this much speech
    end_ms: int          # this long under `release` ends the speech
    pad_ms: int          # the onset is put this far before the first speech step


class _FireRedVad:
    """FireRedVAD's streaming DFSMN (with its caches) on upsampled phone audio, 10 ms a step."""
    TUNING = _Tuning(hop=80, smooth=5, threshold=0.5, release=0.35, min_speech_ms=200,
                     end_ms=200, pad_ms=150)

    def __init__(self, folder, cmvn):
        np = _np
        self.session = _session(os.path.join(folder, FIRERED_VAD))
        shape = self.session.get_inputs()[1].shape
        self.caches = np.zeros([d if isinstance(d, int) else 1 for d in shape], np.float32)
        self.features = _Features16k(cmvn, keep_frames=BUFFER_MS // 10)

    def process(self, samples):
        """(start, probability) for each 10 ms step these samples complete."""
        first, features = self.features.push(samples)
        if not len(features):
            return []
        probs, self.caches = self.session.run(
            None, {'feat': features[None], 'caches_in': self.caches})
        return [(self.features.frame_start(first + k), float(p))
                for k, p in enumerate(probs[0, :, 0])]

    def aed_features(self, audio, begin, onset, end):
        return self.features.window(begin, onset, end)


class _SileroVad:
    """Silero VAD v6, 32 ms a step: windows of 256 samples at 8 kHz after 32 samples of
    context (its own wrapper's format), or with rate=16000 windows of 512 samples of the
    upsampled audio after 64.

    Its defaults (0.5, ending under 0.35, 250 ms of speech) woke on only 76% of the
    utterances in real calls at 8 kHz (88% at 16 kHz): short words seldom hold 0.5 for
    250 ms. 0.3, ending under 0.25, and 160 ms reach 85% (92%) without waking on hold music,
    noise or tones. Ending under 0.15 reached 90% (95%) but woke on hold music starting
    again right after an announcement.
    """
    TUNING = _Tuning(hop=256, smooth=1, threshold=0.3, release=0.25, min_speech_ms=160,
                     end_ms=200, pad_ms=150)

    def __init__(self, folder, cmvn, rate=SAMPLE_RATE):
        np = _np
        self.session = _session(os.path.join(folder, SILERO_VAD))
        self.upsampler = Upsampler() if rate != SAMPLE_RATE else None
        self.window = 256 * rate // SAMPLE_RATE
        self.state = np.zeros((2, 1, 128), dtype=np.float32)
        self.context = np.zeros(self.window // 8, dtype=np.float32)
        self.rate = np.array(rate, dtype=np.int64)
        self.leftover = np.zeros(0, dtype=np.float32)
        self.done = 0
        self.cmvn = cmvn

    def process(self, samples):
        np = _np
        if self.upsampler:
            samples = self.upsampler(samples)
        x = np.concatenate((self.leftover, samples / 32768.0)).astype(np.float32)
        steps = []
        for k in range(len(x) // self.window):
            window = x[k * self.window:(k + 1) * self.window]
            out, self.state = self.session.run(None, {
                'input': np.concatenate((self.context, window))[None],
                'state': self.state, 'sr': self.rate})
            self.context = window[-len(self.context):]
            start = (self.done - Upsampler.DELAY_16K) // 2 if self.upsampler else self.done
            steps.append((start, float(out[0, 0])))
            self.done += self.window
        self.leftover = x[len(steps) * self.window:]
        return steps

    def aed_features(self, audio, begin, onset, end):
        return _Features16k.of(self.cmvn, audio, begin, onset, end)


class _Aed:
    """FireRedVAD's audio event model: speech, singing and music, per 10 ms frame.

    It labels each frame independently, so speech over music is speech and music at once.
    Speech heard out from its onset had a speech share of 0.32 or more in the tests, even
    under music as loud as the voice; synthetic singing mostly stayed under 0.1. Through a
    phone line it is less sure of music (0.25-0.65 on the simulated hold music), so a frame
    counts as singing or music from 0.3 here, not FireRedVAD's 0.5.
    """
    # Speech, singing, music: a frame counts as each from these probabilities.
    THRESHOLDS = (0.4, 0.3, 0.3)
    # Skip when singing or music is in at least this share of the frames since the speech
    # began, and speech in less than SPEECH_SHARE of them.
    MUSIC_SHARE = 0.5
    SPEECH_SHARE = 0.2

    def __init__(self, folder):
        self.session = _session(os.path.join(folder, FIRERED_AED))

    def scores(self, features, onset_frame):
        """Share of the frames from onset_frame on that are speech, singing and music."""
        np = _np
        if len(features) <= onset_frame:
            return None
        probs = self.session.run(None, {'feat': features[None].astype(np.float32)})[0][0]
        probs = probs[onset_frame:]
        shares = (probs >= np.array(self.THRESHOLDS)).mean(axis=0)
        return {'speech': round(float(shares[0]), 3), 'singing': round(float(shares[1]), 3),
                'music': round(float(shares[2]), 3)}

    @classmethod
    def is_music(cls, scores):
        return (max(scores['singing'], scores['music']) >= cls.MUSIC_SHARE
                and scores['speech'] < cls.SPEECH_SHARE)


class _Fingerprints:
    """Remembered recordings, compared by normalised cross-correlation over every time shift.

    The features are 64 log-mel bins of the 8 kHz audio (100-3800 Hz, 10 ms apart), each
    frame less its own mean (its loudness), and each bin less its mean over the stretch being
    compared. What is left is the shape of the spectrum moving in time: the same recording
    again matches it (0.74-1.0 in the tests, with different music under it at up to -6 dB),
    other speech in the same synthetic voice stays under 0.6 even with the same words.
    """
    BINS = 64
    MIN_FRAMES = 30    # 300 ms: shorter stretches are not compared

    def __init__(self):
        self.options = _fbank_options(SAMPLE_RATE, self.BINS, low=100.0, high=3800.0)
        self.items = []

    def features(self, samples):
        np = _np
        fbank = _knf.OnlineFbank(self.options)
        fbank.accept_waveform(SAMPLE_RATE, samples)
        fbank.input_finished()
        features = _frames(fbank, 0, fbank.num_frames_ready).astype(np.float64)
        return features - features.mean(axis=1, keepdims=True)

    def remember(self, samples):
        recording = self._recording(samples)
        if recording is None:
            return False
        self.items.append(recording)
        del self.items[:-MAX_RECORDINGS]
        return True

    def best_match(self, samples, partial=()):
        """How well `samples` match the best remembered recording (0..1). `partial` are the
        stretches heard so far of recordings still being taken, all from before `samples`."""
        recordings = self.items + [r for r in map(self._recording, partial) if r is not None]
        heard = self._recording(samples) if recordings else None
        if heard is None:
            return 0.0
        return max(self._match(*sorted((heard, recording), key=len)) for recording in recordings)

    def _recording(self, samples):
        features = self.features(samples)
        return _Recording(features) if len(features) >= self.MIN_FRAMES else None

    @staticmethod
    def _match(short, long):
        """The best normalised correlation of `short` with any stretch of `long`, each stretch
        and `short` taken less their means per bin."""
        np = _np
        length, bins = short.features.shape
        mean = short.total[-1] / length
        spread = float(short.square[-1].sum()) - length * float(mean @ mean)
        if spread < 1e-9:
            return 0.0
        size = 1 << (len(long) + length).bit_length()
        shifts = len(long) - length + 1
        products = (np.conj(short.spectrum(size)) * long.spectrum(size)).sum(axis=1)
        sums = long.total[length:] - long.total[:shifts]
        dot = np.fft.irfft(products, size)[:shifts] - sums @ mean
        energy = (long.square[length:] - long.square[:shifts] - sums * sums / length).sum(axis=1)
        valid = energy > 1e-6 * length * bins
        scores = np.where(valid, dot / np.sqrt(spread * np.where(valid, energy, 1.0)), 0.0)
        return float(scores.max())


class _Recording:
    """Fingerprint features of a stretch of audio, with what comparing against them takes:
    running sums per bin, and their spectrum at each FFT size asked for."""

    def __init__(self, features):
        np = _np
        self.features = features
        zero = np.zeros((1, features.shape[1]))
        self.total = np.concatenate((zero, np.cumsum(features, axis=0)))
        self.square = np.concatenate((zero, np.cumsum(features * features, axis=0)))
        self._spectra = {}

    def __len__(self):
        return len(self.features)

    def spectrum(self, size):
        if size not in self._spectra:
            self._spectra[size] = _np.fft.rfft(self.features, size, axis=0)
        return self._spectra[size]


class HoldListener:
    """Watches a held phone line for a person; see the module docstring.

    feed() takes every 20 ms frame of the line, before and after listen(). It returns a Wake
    when, since the last listen(), new speech began that was neither a remembered recording
    nor music. After a Wake it returns None until listen() is called again.
    """

    @classmethod
    def create(cls, environ=None):
        """A listener with the detector SMITLINE_HOLD_DETECTOR names ('firered' by default;
        'silero', or 'silero16k' for Silero's 16 kHz model on the upsampled audio), with its
        models in SMITLINE_HOLD_MODELS (hold_models/ by default). Raises
        HoldListenerUnavailable saying why there cannot be one."""
        environ = os.environ if environ is None else environ
        name = (environ.get('SMITLINE_HOLD_DETECTOR') or 'firered').strip().lower()
        if name not in DETECTORS:
            raise HoldListenerUnavailable(
                f'unknown hold detector {name!r} (use {", ".join(DETECTORS)})')
        folder = environ.get('SMITLINE_HOLD_MODELS') or MODELS_DIR
        needed = [FIRERED_VAD, FIRERED_CMVN] if name == 'firered' else [SILERO_VAD]
        for file in needed:
            if not os.path.isfile(os.path.join(folder, file)):
                raise HoldListenerUnavailable(f'{file} is missing from {folder}')
        _load_packages()
        has_aed = all(os.path.isfile(os.path.join(folder, f)) for f in (FIRERED_AED, FIRERED_CMVN))
        try:
            cmvn = _read_cmvn(os.path.join(folder, FIRERED_CMVN)) if (
                name == 'firered' or has_aed) else None
        except (OSError, ValueError) as error:
            raise HoldListenerUnavailable(f'cannot read {FIRERED_CMVN}: {error}') from None
        if name == 'firered':
            vad = _FireRedVad(folder, cmvn)
        else:
            vad = _SileroVad(folder, cmvn, 2 * SAMPLE_RATE if name == 'silero16k' else SAMPLE_RATE)
        aed = _Aed(folder) if has_aed else None
        return cls(name, vad, aed)

    def __init__(self, name, vad, aed):
        self.name = name
        self._vad = vad
        self._aed = aed
        self._tuning = vad.TUNING
        self._fingerprints = _Fingerprints()
        self._audio = _Ring(BUFFER_MS * SAMPLE_RATE // 1000)
        self._batch = []
        self._batched = 0
        self._levels = collections.deque(maxlen=BUFFER_MS * SAMPLE_RATE // 1000 // vad.TUNING.hop)
        self._window = collections.deque(maxlen=self._tuning.smooth)
        # Speech as the detector hears it, in 8 kHz sample numbers. A "run" is one stretch of it.
        self._pending = None          # where speech started that is not long enough yet
        self._in_run = False
        self._run = 0                 # number of the current (or last) run
        self._run_end = 0
        self._speech_end = 0
        self._quiet = 0               # samples under `release` at the end of the current run
        # What listen() asked for.
        self._listening = False
        self._candidate = None        # (onset, speech, decide_at, run): new speech heard out
        self._hold_off = None         # (run, recheck_at): speech skipped or already going on
        self._wake_onset = None       # onset of the last Wake
        self._remembered = None       # the onset remember_recording() last took
        self._taking = []             # (begin, end) of recordings still coming in
        self.stats = {'wakes': 0, 'skippedRepeats': 0, 'skippedMusic': 0,
                      'seconds': 0.0, 'cpuMs': 0.0}

    @property
    def listening(self):
        return self._listening

    def listen(self):
        """Start (or resume) watching for a new person: at the start of each hold, and after
        each false wake. Speech already going on now is not new."""
        self._listening = True
        self._candidate = None
        self._hold_off = (self._run, self._now() + self._ms(LONG_RUN_MS)) if self._in_run else None

    def feed(self, frame):
        """Take one 20 ms mu-law frame. Returns a Wake, or None."""
        started = time.thread_time_ns()
        try:
            samples = ulaw_decode(frame)
            self._audio.append(samples)
            self._batch.append(samples)
            self.stats['seconds'] = round(self._audio.end / SAMPLE_RATE, 3)
            if self._taking:
                self._take_recordings()
            if self._audio.end - self._batched < self._ms(BATCH_MS):
                return None
            batch = _np.concatenate(self._batch)
            self._batch.clear()
            self._batched = self._audio.end
            wake = None
            for start, probability in self._vad.process(batch):
                self._step(start, probability)
                wake = wake or self._check()
            return wake
        finally:
            self.stats['cpuMs'] = round(
                self.stats['cpuMs'] + (time.thread_time_ns() - started) / 1e6, 3)

    def remember_recording(self):
        """The speech behind the last Wake was a recording (an announcement in the hold loop):
        fingerprint it, from just before its onset to FINGERPRINT_MS after it (the part not
        heard yet as it comes in), so the same audio never wakes the call again. Returns
        whether there was a Wake to remember."""
        started = time.thread_time_ns()
        try:
            onset = self._wake_onset
            if onset is None or onset == self._remembered:
                return False
            self._remembered = onset
            self._taking.append((onset - self._ms(FINGERPRINT_PRE_ROLL_MS),
                                 onset + self._ms(FINGERPRINT_MS)))
            self._take_recordings()
            return True
        finally:
            self.stats['cpuMs'] = round(
                self.stats['cpuMs'] + (time.thread_time_ns() - started) / 1e6, 3)

    def _take_recordings(self):
        for taking in [t for t in self._taking if t[1] <= self._now()]:
            self._taking.remove(taking)
            self._fingerprints.remember(self._audio.get(*taking))

    # Speech tracking

    @staticmethod
    def _ms(ms):
        return ms * SAMPLE_RATE // 1000

    def _now(self):
        return self._audio.end

    def _step(self, start, probability):
        tuning = self._tuning
        self._window.append(probability)
        level = sum(self._window) / len(self._window)
        self._levels.append((start, level))
        if self._in_run:
            if level >= tuning.release:
                self._quiet = 0
                self._speech_end = start + tuning.hop
            else:
                self._quiet += tuning.hop
                if self._quiet >= self._ms(tuning.end_ms):
                    self._in_run = False
                    self._run_end = self._speech_end
                    if self._hold_off and self._hold_off[0] == self._run:
                        self._hold_off = None
            return
        if level >= tuning.threshold and self._pending is None:
            self._pending = start
        elif level < tuning.release:
            self._pending = None
        if self._pending is not None and start + tuning.hop - self._pending >= self._ms(
                tuning.min_speech_ms):
            self._run += 1
            self._in_run = True
            self._quiet = 0
            self._speech_end = start + tuning.hop
            if self._listening and self._candidate is None and self._hold_off is None:
                if self._pending < self._ms(WARMUP_MS):
                    self._hold_off = (self._run, self._now() + self._ms(LONG_RUN_MS))
                else:
                    onset = max(self._pending - self._ms(tuning.pad_ms), self._run_end,
                                self._audio.start)
                    self._candidate = (onset, self._pending, onset + self._ms(DECIDE_MS),
                                       self._run)
            self._pending = None

    def _check(self):
        now = self._now()
        if self._candidate:
            onset, speech, decide_at, run = self._candidate
            if now >= decide_at or run != self._run or not self._in_run:
                self._candidate = None
                return self._decide(onset, speech, now, run, first=True)
        if (self._hold_off and self._in_run and self._hold_off[0] == self._run
                and now >= self._hold_off[1]):
            onset = now - self._ms(DECIDE_MS)
            return self._decide(onset, onset, now, self._run, first=False)
        return None

    def _decide(self, onset, speech, now, run, first):
        """Hear out the audio from `onset` (padded; `speech` is the first speech step) to now:
        a Wake, or a skip (counted the first time)."""
        tuning = self._tuning
        heard = [level for start, level in self._levels if onset <= start < now]
        speech_ms = sum(tuning.hop for level in heard if level >= tuning.threshold) * 1000 // SAMPLE_RATE
        scores = {'vad': round(sum(heard) / len(heard), 3) if heard else 0.0, 'repeat': 0.0}
        skip = None
        partial = [self._audio.get(begin, min(end, onset)) for begin, end in self._taking]
        if self._fingerprints.items or partial:
            scores['repeat'] = round(
                self._fingerprints.best_match(self._audio.get(onset, now), partial), 3)
            if scores['repeat'] >= REPEAT_MATCH:
                skip = 'skippedRepeats'
        if skip is None and self._aed is not None:
            features, first_frame = self._vad.aed_features(
                self._audio, speech - self._ms(AED_CONTEXT_MS), speech, now)
            aed = self._aed.scores(features, first_frame)
            if aed:
                scores.update(aed)
                if _Aed.is_music(aed):
                    skip = 'skippedMusic'
        if skip:
            if first:
                self.stats[skip] += 1
            wait = LONG_RUN_MS if skip == 'skippedRepeats' else RECHECK_MS
            still = self._in_run and run == self._run
            self._hold_off = (run, now + self._ms(wait)) if still else None
            return None
        self._listening = False
        self._hold_off = None
        self._wake_onset = onset
        self.stats['wakes'] += 1
        return Wake(onset_ms_ago=(now - onset) * 1000 // SAMPLE_RATE, speech_ms=speech_ms,
                    scores=scores)
