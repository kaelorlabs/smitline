"""The voice as the meeting hears it: whether the agent is speaking, and how open its mouth is.

The microphones (joinly's VirtualMicrophone for a browser, DailyMicrophone for a Daily room) write
GPT-Live's voice to the meeting in 10 ms chunks, in real time. Each chunk passes through `feed` as
it is written, with the chunks queued right behind it, so the camera follows what people hear.
GPT-Live itself delivers faster than real time and streams silence between replies, so going by
what it delivered left the mouth moving after the voice had stopped.

- Speaking starts with the first audible chunk written (or queued right behind it) and ends
  RELEASE_SECONDS after the last one: a pause between words keeps the talking picture, a pause
  between sentences ends it.
- The mouth level (0 closed to 1 wide open) follows each chunk's loudness, opening quickly and
  closing a little more slowly. It takes the queued chunks into account, so the mouth opens with
  the sound rather than after it.
"""
import array
import asyncio
import contextlib
import math
import time

CHUNK_SECONDS = 0.01
RELEASE_SECONDS = 0.2
# A chunk quieter than this (RMS, dB full scale) is silence; at FULL_DBFS the mouth is wide open.
QUIET_DBFS = -48.0
FULL_DBFS = -16.0
ATTACK_SECONDS = 0.015
DECAY_SECONDS = 0.06


def chunk_dbfs(chunk, sample_width=2):
    """The RMS level of a chunk of mono PCM (16-bit, or 32-bit float), in dB full scale."""
    if not bytes(chunk).strip(b'\x00'):
        return -math.inf  # digital silence, which a browser microphone writes between replies
    if sample_width == 4:
        samples, full = array.array('f', bytes(chunk[:len(chunk) // 4 * 4])), 1.0
    else:
        samples, full = array.array('h', bytes(chunk[:len(chunk) // 2 * 2])), 32768.0
    if not samples:
        return -math.inf
    power = sum(sample * sample for sample in samples) / len(samples)
    if power <= 0:
        return -math.inf
    return 10 * math.log10(power) - 20 * math.log10(full)


def loudness(dbfs):
    """0 for silence to 1 for a loud voice, linear in decibels between QUIET_DBFS and FULL_DBFS."""
    return min(1.0, max(0.0, (dbfs - QUIET_DBFS) / (FULL_DBFS - QUIET_DBFS)))


def mouth_picture(level, levels):
    """Which of `levels` mouth pictures a level from 0 to 1 shows; 0 is the closed mouth."""
    return min(levels, max(0, math.floor(level * levels + 0.5)))


class VoiceActivity:
    """Speaking or not, and the mouth level, from the chunks a microphone writes to the meeting.

    `attach` hooks it to a microphone (its `on_chunk`). `on_speaking` callbacks get True or False
    when speaking starts or ends, `on_level` callbacks the mouth level after each chunk; both run
    on the event loop. `level_now()` may be read from any thread (the Daily camera's)."""

    def __init__(self, *, release=RELEASE_SECONDS, clock=time.monotonic, sample_width=2):
        self.release = release
        self.clock = clock
        self.sample_width = sample_width
        self.speaking = False
        self.level = 0.0
        self.last_audible = -math.inf
        self.last_fed = -math.inf
        self.on_speaking = []
        self.on_level = []
        self._timer = None

    def attach(self, microphone):
        """Follow what this microphone writes; returns it."""
        audio_format = getattr(microphone, 'audio_format', None)
        self.sample_width = getattr(audio_format, 'byte_depth', None) or self.sample_width
        microphone.on_chunk = self.feed
        microphone.voice = self
        return microphone

    def feed(self, chunk, upcoming=()):
        """A chunk is being written to the meeting now; `upcoming` are the chunks queued behind it."""
        now = self.clock()
        loud = [loudness(chunk_dbfs(piece, self.sample_width)) for piece in (chunk, *upcoming)]
        target = max(loud)
        elapsed = min(max(now - self.last_fed, CHUNK_SECONDS), 1.0) if self.last_fed > -math.inf else CHUNK_SECONDS
        self.last_fed = now
        rate = ATTACK_SECONDS if target > self.level else DECAY_SECONDS
        self.level += (target - self.level) * (1 - math.exp(-elapsed / rate))
        if self.level < 0.01:
            self.level = 0.0
        if target > 0:
            self.last_audible = now
            if not self.speaking:
                self._set_speaking(True)
            self._arm()
        else:
            self.poll()
        self._tell(self.on_level, self.level)

    def poll(self):
        """End speaking once RELEASE_SECONDS have passed since the last audible chunk."""
        if self.speaking and self.clock() - self.last_audible >= self.release:
            self.level = 0.0
            self._set_speaking(False)
            self._tell(self.on_level, 0.0)
        return self.speaking

    def level_now(self):
        """The mouth level now: the last chunk's, closing on its own when no chunk comes."""
        level, fed = self.level, self.last_fed
        if not self.speaking or level <= 0:
            return 0.0
        quiet = self.clock() - fed - CHUNK_SECONDS
        return level * math.exp(-quiet / DECAY_SECONDS) if quiet > 0 else level

    def mouth(self, levels):
        """The mouth picture for the level now: 0 (closed) to `levels` (wide open)."""
        return mouth_picture(self.level_now(), levels)

    def _set_speaking(self, speaking):
        self.speaking = speaking
        if not speaking and self._timer is not None:
            self._timer.cancel()
            self._timer = None
        self._tell(self.on_speaking, speaking)

    def _arm(self):
        """A timer ends speaking when the chunks stop coming (a Daily microphone writes none
        between replies); it is armed once and moved on when it fires early."""
        if self._timer is not None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # no event loop (a test feeding by hand): poll() decides
        self._timer = loop.call_later(self.release, self._expire)

    def _expire(self):
        self._timer = None
        if not self.speaking:
            return
        left = self.release - (self.clock() - self.last_audible)
        if left > 0:
            self._timer = asyncio.get_running_loop().call_later(left, self._expire)
        else:
            self.poll()

    @staticmethod
    def _tell(listeners, value):
        for listener in list(listeners):
            # The picture must never stop the voice.
            with contextlib.suppress(Exception):
                listener(value)
