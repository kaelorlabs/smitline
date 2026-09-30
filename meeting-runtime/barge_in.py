"""Hear the other person on a relayed phone call, so the assistant stops talking at once.

GPT-Live produces speech faster than it plays, so on a relayed call seconds of it can be
queued here when the other person starts talking. GPT-Live's own barge-in stops it
generating but cannot unsay what is already queued. SpeechDetector listens to the caller's
audio (the 8 kHz mu-law frames that also go to GPT-Live) and says when speech starts and
ends; phone_line pauses playback at the start, and drops the rest once it is clearly an
interruption rather than a short "mhm".
"""
import math

SAMPLE_RATE = 8000


def _mulaw_magnitude(byte):
    value = ~byte & 0xFF
    exponent = (value >> 4) & 0x07
    mantissa = value & 0x0F
    return (((mantissa << 3) + 0x84) << exponent) - 0x84


MULAW_MAGNITUDE = tuple(_mulaw_magnitude(byte) for byte in range(256))


def frame_level_db(frame):
    """Mean loudness of mu-law audio in dB below full scale (-100 for silence)."""
    if not frame:
        return -100.0
    mean = sum(MULAW_MAGNITUDE[byte] for byte in frame) / len(frame)
    return 20 * math.log10(mean / 32768) if mean > 0 else -100.0


class SpeechDetector:
    """Speech start and end on the caller's audio, from its loudness over the line's noise.

    start_ms of voiced audio (short gaps allowed) starts speech; end_ms of quiet ends it.
    While the assistant is playing, speech must be playing_margin_db louder still, so its
    own voice echoing back from the phone does not count.
    """

    def __init__(self, *, start_ms=160, end_ms=450, margin_db=14.0, playing_margin_db=6.0,
                 min_level_db=-45.0, floor_db=-60.0):
        self.start_ms = start_ms
        self.end_ms = end_ms
        self.margin_db = margin_db
        self.playing_margin_db = playing_margin_db
        self.min_level_db = min_level_db
        self.floor = floor_db
        self.speaking = False
        self._voiced_ms = 0
        self._speech_ms = 0
        self._silent_ms = 0
        self.last_speech_ms = 0

    @property
    def speech_ms(self):
        """How long the current speech has been voiced, without its trailing quiet."""
        return self._speech_ms - self._silent_ms if self.speaking else 0

    def feed(self, frame, *, playing=False):
        """Take one frame of mu-law audio; returns 'start', 'end', or None."""
        ms = len(frame) * 1000 // SAMPLE_RATE
        level = frame_level_db(frame)
        extra = self.playing_margin_db if playing else 0.0
        voiced = level > max(self.floor + self.margin_db + extra, self.min_level_db)
        if not voiced:
            # The floor follows the line's noise: down at once, up over about a second.
            self.floor = level if level < self.floor else self.floor + 0.02 * (level - self.floor)
        elif self.speaking:
            # Steady loud noise (a TV, a car) eventually becomes the floor, so speech ends.
            self.floor += 0.002 * (level - self.floor)
        if not self.speaking:
            self._voiced_ms = self._voiced_ms + ms if voiced else max(0, self._voiced_ms - ms)
            if self._voiced_ms >= self.start_ms:
                self.speaking = True
                self._speech_ms = self._voiced_ms
                self._silent_ms = 0
                return 'start'
            return None
        self._speech_ms += ms
        self._silent_ms = 0 if voiced else self._silent_ms + ms
        if self._silent_ms >= self.end_ms:
            self.last_speech_ms = self._speech_ms - self._silent_ms
            self.speaking = False
            self._voiced_ms = 0
            self._speech_ms = self._silent_ms = 0
            return 'end'
        return None
