"""The room's audio on its way to GPT-Live: how loud it is, when it pauses, and whether it comes in.

bridge.send_audio passes every chunk it sends GPT-Live through InputMonitor.feed. From that:
- the hello waits for a real pause in the room's audio, not only in the transcript, which trails
  the words by a second or more (bridge's introduce_once);
- the archive's input_audio and process_stopped events say how much audio came in, how much of
  it was voiced and how loud it got, so a meeting that heard nothing shows it;
- input_silent is recorded once when the meeting shows someone starting to speak and nothing comes
  in for INPUT_SILENT_SECONDS. On 2026-11-05 a Daily room fed GPT-Live silence for a whole meeting
  while Daily showed two people taking turns.
"""
import array
import math
import time

BYTES_PER_SECOND = 24000 * 2  # the room's audio for GPT-Live: 24 kHz, 16-bit, mono
# A chunk louder than this (its mean level, in dB below full scale) is someone talking; quieter
# is a pause. Phone calls draw the same line for the voice's own audio (phone_line.SILENT_FRAME_DB).
VOICED_DB = -48.0
# Quieter than this all through, nothing came in at all: digital silence, as from a speaker that
# is never read. Speech, even soft speech, is louder.
SOUND_DB = -70.0
INPUT_SILENT_SECONDS = 10.0
# A meeting names a new speaker a moment after their first words, so sound that came in this long
# before still counts as hearing them.
SPEAKER_LAG_SECONDS = 3.0


def pcm_level(data):
    """(mean level in dB below full scale, peak sample) of 16-bit PCM; (-100.0, 0) for silence."""
    samples = array.array('h', bytes(data[:len(data) // 2 * 2]))
    if not samples:
        return -100.0, 0
    peak = max(max(samples), -min(samples))
    mean = sum(map(abs, samples)) / len(samples)
    return (20 * math.log10(mean / 32768) if mean else -100.0), peak


class InputMonitor:
    """What came in from the room (feed), and whether people the meeting shows speaking were
    heard (speaker)."""

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.bytes_in = 0
        self.voiced_bytes = 0
        self.peak = 0
        self.last_voiced_at = None  # when someone was last heard talking
        self.last_sound_at = None   # when anything at all last came in
        self.silent_reported = False
        self._speaker = None
        self._unheard_since = None  # someone was shown starting to speak then, and not yet heard

    @property
    def voiced_ms(self):
        return self.voiced_bytes * 1000 // BYTES_PER_SECOND

    def feed(self, data):
        level, peak = pcm_level(data)
        now = self.clock()
        self.bytes_in += len(data)
        self.peak = max(self.peak, peak)
        if level > SOUND_DB:
            self.last_sound_at = now
        if level > VOICED_DB:
            self.voiced_bytes += len(data)
            self.last_voiced_at = now

    def speaker(self, name):
        """Note whom the meeting shows speaking now (None: nobody). A new name is someone
        starting to speak, so their audio should come in. Returns input_silent's details once:
        the first time nothing at all came in for INPUT_SILENT_SECONDS after that."""
        now = self.clock()
        if name and name != self._speaker and self._unheard_since is None:
            self._unheard_since = now
        self._speaker = name
        since = self._unheard_since
        if since is None:
            return None
        if self.last_sound_at is not None and self.last_sound_at >= since - SPEAKER_LAG_SECONDS:
            self._unheard_since = None  # they were heard
            return None
        if self.silent_reported or now - since < INPUT_SILENT_SECONDS:
            return None
        self.silent_reported = True
        return {'seconds': round(now - since, 1), 'bytes': self.bytes_in, 'peak': self.peak}

    def stats(self, speaker=None):
        """What came in so far: bytes, voiced milliseconds and the peak sample, and the room
        audio the speaker lost on the way when it counts that (Daily rooms do; browsers cannot)."""
        stats = {'bytes': self.bytes_in, 'voiced_ms': self.voiced_ms, 'peak': self.peak}
        dropped = getattr(speaker, 'dropped_ms', None)
        if isinstance(dropped, int):
            stats['dropped_ms'] = dropped
        return stats
