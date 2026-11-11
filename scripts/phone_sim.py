"""Simulated phone calls for phone menus, waiting on hold and connecting the owner. Smitline's real
phone line, call service and gateway call a fake business on a fake phone line on this computer,
with real GPT-Live; nobody is phoned.

The business is a phone menu (recordings made with OpenAI text to speech), a hold queue (music
and recorded announcements), and people played by GPT-Live: a representative, and in one scenario
a voice bot. The phone provider is a stand-in that connects each call's media stream to the
business through Smitline's own gateway and posts signed status callbacks. When Smitline rings the
owner (connectOwner), it answers as the owner: it hears the whisper of the <Gather>, presses 1, and
talks on the <Stream> that pressing 1 returns.

On hold, Smitline closes its GPT-Live session and its hold listener (hold_listener.py) hears the
line; someone talking starts a new session. Each wake is timed: what the business was playing, how
long the new session took to start, how much audio the line gave it before it had started (which
GPT-Live never hears), and how long until Smitline's voice was heard on the line.

    cd meeting-runtime
    OPENAI_API_KEY=... ~/.venvs/colleague/bin/python -u ../scripts/phone_sim.py --out DIR \\
        [--detector firered|silero] [--speech-cache DIR] [scenario ...]

Scenarios: hold-timeout, menu-hold-rep, voice-bot, connect-owner, long-hold-repeats (default: all,
about 25 minutes and $1-2 of OpenAI usage). Each writes OUT/<scenario>/: stereo.wav (left: the
business, right: what it heard from Smitline), mix.wav, owner.wav (connect-owner), record.json (the
call record and its result), events.json (each with t, seconds since the scenario started),
transcript.txt, business.txt (what the business did and heard, timed the same way),
voice-sessions.json (each GPT-Live session of Smitline's voice) and audio-trace.jsonl.
OUT/summary.md lists the checks and the wakes of every scenario. Recorded prompts are kept in
OUT/.speech; --speech-cache can point at an earlier run's .speech, so nothing is recorded again.
"""
import argparse
import asyncio
import base64
import bisect
import collections
import hashlib
import json
import math
import os
import random
import re
import socket
import sys
import tempfile
import time
import uuid
import wave
from array import array
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit
from xml.etree import ElementTree

RUNTIME = Path(__file__).resolve().parent.parent / 'meeting-runtime'
sys.path.insert(0, str(RUNTIME))

from aiohttp import ClientSession, ClientTimeout, WSMsgType, web  # noqa: E402

import hold_listener  # noqa: E402
from barge_in import frame_level_db  # noqa: E402
from call_costs import VOICE_MODEL, VOICE_PER_MINUTE  # noqa: E402
from call_hooks import DefaultCallHooks  # noqa: E402
from call_recorder import ULAW as ULAW_DECODE  # noqa: E402
from call_service import CallService  # noqa: E402
from call_store import CallStore  # noqa: E402
from phone_gateway import create_gateway_app  # noqa: E402
from phone_keys import COLUMNS, LAYOUT, ROWS  # noqa: E402
from phone_line import PhoneLine  # noqa: E402
from twilio_client import compute_signature  # noqa: E402
from voice_core import PCMU8, LiveSession, function_call_from, session_config  # noqa: E402

FRAME = 160  # 20 ms of 8 kHz mu-law
SILENCE = b'\xff' * FRAME
AUTH_TOKEN = 'sim-auth-token'
OWNER_NAME = 'Arun'
OWNER_PHONE = '+14165550100'
BUSINESS_PHONE = '+14155550142'
FROM_NUMBER = '+15005550006'
ACCOUNT = '4185550142'
# A Thursday, daytime where both numbers ring, so calling hours never get in the way.
NOW = datetime(2026, 11, 12, 17, 0, tzinfo=timezone.utc)
LOUD_DB = -38.0  # louder than this is someone talking (or music)
QUIET_DB = -45.0  # Smitline's audio stays below this while the business plays its hold
SPEECH_DB = -48.0  # GPT-Live audio quieter than this is a pause (phone_line.SILENT_FRAME_DB)
SPEECH_FRAMES = 3  # this many loud frames in a row (60 ms) start speech
TURN_FRAMES = 35  # this many quiet frames in a row (0.7 s) end a turn
# The person who picks up after a hold is answered in time when Smitline's voice is heard this
# soon after their first turn ends.
REPLY_IN_TIME = 4.0
TERMINAL = ('completed', 'failed', 'canceled')
PICKUP_NOTE = re.compile(r'^Someone picked up after (?:a few seconds|about a minute|about \d+ '
                         r'minutes|about [\d.]+ hours) on hold\.$')
GAVE_UP_NOTE = re.compile(r'^Nobody picked up after .+ on hold; Smitline hung up\.$')


def b64(audio):
    return base64.b64encode(audio).decode('ascii')


def frames_of(audio):
    """Whole 20 ms frames, the last one padded with silence."""
    if len(audio) % FRAME:
        audio = audio + b'\xff' * (FRAME - len(audio) % FRAME)
    return [audio[i:i + FRAME] for i in range(0, len(audio), FRAME)]


def words(text):
    return re.findall(r"[a-z0-9']+", str(text or '').lower())


def clipped(text, limit=80):
    text = ' '.join(str(text or '').split())
    return text if len(text) <= limit else text[:limit - 1] + '…'


# Audio: G.711 mu-law, touch tones ----------------------------------------------------------------

def _ulaw_byte(sample):
    """One 16-bit sample as G.711 mu-law (the inverse of call_recorder.ULAW)."""
    sign = 0x80 if sample < 0 else 0
    magnitude = min(abs(int(sample)), 32635) + 0x84
    exponent, mask = 7, 0x4000
    while exponent > 0 and not magnitude & mask:
        exponent -= 1
        mask >>= 1
    mantissa = (magnitude >> (exponent + 3)) & 0x0F
    return ~(sign | (exponent << 4) | mantissa) & 0xFF


_ULAW_ENCODE = None


def ulaw_encode(samples):
    """16-bit samples (any numbers; clipped to 16 bits) as mu-law bytes."""
    global _ULAW_ENCODE
    if _ULAW_ENCODE is None:
        _ULAW_ENCODE = bytes(_ulaw_byte(value) for value in range(-32768, 32768))
    table = _ULAW_ENCODE
    return bytes(table[max(-32768, min(32767, int(value))) + 32768] for value in samples)


def mix_ulaw(first, second):
    """Two runs of mu-law audio mixed into one (the shorter padded with silence)."""
    size = max(len(first), len(second))
    first = bytes(first) + b'\xff' * (size - len(first))
    second = bytes(second) + b'\xff' * (size - len(second))
    return ulaw_encode(ULAW_DECODE[a] + ULAW_DECODE[b] for a, b in zip(first, second))


def goertzel(samples, frequency, rate=8000):
    """The power of one frequency in a block of samples (for hearing touch tones)."""
    coeff = 2 * math.cos(2 * math.pi * frequency / rate)
    s1 = s2 = 0.0
    for sample in samples:
        s1, s2 = sample + coeff * s1 - s2, s1
    return s1 * s1 + s2 * s2 - coeff * s1 * s2


def block_key(samples, rate=8000):
    """The touch tone in one short block of samples (20 ms), or None: a loud block whose energy is
    mostly one row and one column tone of the keypad (phone_keys), both of them there. One strong
    note near a row tone (784 Hz in the hold music is 14 Hz from 770) is not a key."""
    count = len(samples)
    energy = sum(value * value for value in samples) / count if count else 0
    if energy <= 1e6:
        return None
    rows = {f: goertzel(samples, f, rate) for f in ROWS}
    columns = {f: goertzel(samples, f, rate) for f in COLUMNS}
    row = max(rows, key=rows.get)
    column = max(columns, key=columns.get)
    whole = energy * count * count / 2  # the frame's energy as Goertzel shows one tone
    if rows[row] < 0.1 * whole or columns[column] < 0.1 * whole or rows[row] + columns[column] <= 0.4 * whole:
        return None
    return LAYOUT[ROWS.index(row)][COLUMNS.index(column)]


def _lowpass(cutoff=3500.0, rate=24000, count=31):
    middle = (count - 1) / 2
    taps = []
    for n in range(count):
        x = n - middle
        ideal = 2 * cutoff / rate if x == 0 else math.sin(2 * math.pi * cutoff / rate * x) / (math.pi * x)
        taps.append(ideal * (0.54 - 0.46 * math.cos(2 * math.pi * n / (count - 1))))
    total = sum(taps)
    return [tap / total for tap in taps]


TAPS = _lowpass()


def pcm24_to_ulaw8(pcm):
    """Text to speech's 24 kHz 16-bit PCM as a phone line's 8 kHz mu-law (low-passed, then every
    third sample)."""
    samples = memoryview(pcm[:len(pcm) // 2 * 2]).cast('h').tolist()
    half = len(TAPS) // 2
    padded = [0] * half + samples + [0] * half
    width = len(TAPS)
    return ulaw_encode(sum(a * b for a, b in zip(padded[i:i + width], TAPS))
                       for i in range(0, len(samples), 3))


def hold_music(seconds, rate=8000, seed=3):
    """A soft loop of plucked chords over a bass note: sound with no words, like hold music."""
    rng = random.Random(seed)
    chords = ((261.63, 329.63, 392.00), (196.00, 246.94, 293.66),
              (220.00, 261.63, 329.63), (174.61, 220.00, 261.63))
    beat = rate // 2
    out = []
    step = 0
    while len(out) < seconds * rate:
        chord = chords[(step // 4) % len(chords)]
        note = chord[rng.randrange(3)] * (2 if rng.random() < 0.35 else 1)
        root = chord[0] / 2
        for n in range(beat):
            t = n / rate
            envelope = min(1.0, n / 240) * math.exp(-3.0 * t)
            out.append(2600 * envelope * math.sin(2 * math.pi * note * t)
                       + 900 * math.sin(2 * math.pi * root * len(out) / rate))
        step += 1
    return ulaw_encode(out[:int(seconds * rate)])


def write_wav(path, *tracks):
    """mu-law tracks as a 16-bit WAV: one track is mono, two are left and right."""
    count = max(len(track) for track in tracks)
    channels = len(tracks)
    samples = array('h', [0]) * (count * channels)
    for channel, track in enumerate(tracks):
        padded = bytes(track) + b'\xff' * (count - len(track))
        samples[channel::channels] = array('h', (ULAW_DECODE[byte] for byte in padded))
    if sys.byteorder == 'big':
        samples.byteswap()
    with wave.open(str(path), 'wb') as out:
        out.setnchannels(channels)
        out.setsampwidth(2)
        out.setframerate(8000)
        out.writeframes(samples.tobytes())


TTS_URL = 'https://api.openai.com/v1/audio/speech'
# (model, the menu's voice, the owner's voice): the first that works is used.
TTS_MODELS = (('gpt-4o-mini-tts', 'coral', 'echo'), ('tts-1', 'nova', 'echo'))


class Speech:
    """Recorded prompts, made with OpenAI text to speech once and kept in a cache folder (named by
    who says them and the words, as the October simulator named them, so its cache still works)."""

    def __init__(self, key, cache):
        self.key = key
        self.cache = Path(cache)
        self.cache.mkdir(parents=True, exist_ok=True)
        self.model = None
        self.made_seconds = 0.0  # audio made by text to speech in this run (not from the cache)

    def path(self, text, who='menu'):
        name = hashlib.sha1(f'{who}|{text}'.encode()).hexdigest()[:20]
        return self.cache / f'{name}.ulaw'

    async def clip(self, text, who='menu'):
        path = self.path(text, who)
        if path.exists():
            return path.read_bytes()
        audio = pcm24_to_ulaw8(await self._speak(text, who))
        partial = path.with_name(path.name + '.partial')
        partial.write_bytes(audio)
        partial.replace(path)
        self.made_seconds += len(audio) / 8000
        return audio

    async def _speak(self, text, who):
        problems = []
        async with ClientSession(timeout=ClientTimeout(total=90)) as http:
            for model in ([self.model] if self.model else TTS_MODELS):
                name, menu_voice, owner_voice = model
                body = {'model': name, 'input': text, 'response_format': 'pcm',
                        'voice': owner_voice if who == 'owner' else menu_voice}
                if name.startswith('gpt-'):
                    body['instructions'] = ('A relaxed person on a phone call.' if who == 'owner'
                                            else 'A clear, calm, recorded phone system announcement.')
                async with http.post(TTS_URL, json=body,
                                     headers={'Authorization': f'Bearer {self.key}'}) as response:
                    if response.status == 200:
                        self.model = model
                        return await response.read()
                    problems.append(f'{name} {response.status}: {(await response.text())[:160]}')
        raise RuntimeError('text to speech failed: ' + '; '.join(problems))


# One side of a media stream ------------------------------------------------------------------

class KeyListener:
    """The keys the business's phone system hears: touch tones in the audio Smitline sends,
    20 ms at a time (Twilio), and keys pressed on the stream itself (SignalWire)."""

    def __init__(self):
        self.keys = asyncio.Queue()
        self.heard = []  # (key, 'tone' or 'stream')
        self.current = None
        self.run = 0

    def feed(self, frame):
        found = block_key([ULAW_DECODE[byte] for byte in frame])
        if found is not None and found == self.current:
            self.run += 1
            if self.run == 2:  # 40 ms of a tone is a key
                self.press(found, 'tone')
        else:
            self.current, self.run = found, (1 if found is not None else 0)

    def press(self, key, how):
        self.heard.append((key, how))
        self.keys.put_nowait(key)


class StreamEnd:
    """One side of a call's media stream, run the way the phone provider runs it: this side's
    audio goes to Smitline in 20 ms frames at the pace of a phone line, and Smitline's audio is
    played as it would be, buffered and in order, with its marks answered once played. Both are
    kept a frame a tick, and when each tick played, for timing what was heard."""

    def __init__(self, ws, stream_sid, log):
        self.ws = ws
        self.stream_sid = stream_sid
        self.log = log
        self.queue = collections.deque()  # frames this side has yet to say
        self.sent = bytearray()           # what this side said, a frame a tick
        self.heard = bytearray()          # what Smitline sent, placed where it plays
        self.tick_times = array('d')      # when each tick played (time.monotonic)
        self.tick = 0
        self.keys = KeyListener()
        self.listeners = []               # called with each frame of Smitline's audio as it plays
        self.marks = []
        self.closed = asyncio.Event()
        self.hung_up_by = None
        self.loud_ticks = 0
        self.last_loud = -10 ** 9
        self._tasks = []

    def start(self):
        self._tasks = [asyncio.create_task(self._sending()), asyncio.create_task(self._receiving())]

    async def _send(self, payload):
        try:
            await self.ws.send_str(json.dumps(dict(payload, streamSid=self.stream_sid)))
        except Exception:
            self.closed.set()

    async def _sending(self):
        start = time.monotonic()
        while not self.closed.is_set():
            frame = self.queue.popleft() if self.queue else SILENCE
            self.sent += frame
            self.tick_times.append(time.monotonic())
            await self._send({'event': 'media', 'sequenceNumber': str(self.tick + 2),
                              'media': {'track': 'inbound', 'chunk': str(self.tick + 1),
                                        'timestamp': str(self.tick * 20), 'payload': b64(frame)}})
            playing = bytes(self.heard[self.tick * FRAME:(self.tick + 1) * FRAME])
            playing += b'\xff' * (FRAME - len(playing))
            if frame_level_db(playing) > LOUD_DB:
                self.loud_ticks += 1
                self.last_loud = self.tick
            self.keys.feed(playing)
            for listener in list(self.listeners):
                listener(playing)
            self.tick += 1
            while self.marks and self.marks[0][1] <= self.tick:
                name = self.marks.pop(0)[0]
                await self._send({'event': 'mark', 'sequenceNumber': str(self.tick + 2),
                                  'mark': {'name': name}})
            delay = start + self.tick * 0.02 - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            elif delay < -1.0:  # fell behind: carry on from now rather than send a burst
                start = time.monotonic() - self.tick * 0.02

    async def _receiving(self):
        try:
            async for message in self.ws:
                if message.type != WSMsgType.TEXT:
                    if message.type in (WSMsgType.CLOSE, WSMsgType.CLOSING, WSMsgType.CLOSED,
                                        WSMsgType.ERROR):
                        break
                    continue
                data = json.loads(message.data)
                event = data.get('event')
                if event == 'media':
                    audio = base64.b64decode((data.get('media') or {}).get('payload') or '')
                    at = self.tick * FRAME
                    if len(self.heard) < at:
                        self.heard += b'\xff' * (at - len(self.heard))
                    self.heard += audio
                elif event == 'mark':
                    self.marks.append(((data.get('mark') or {}).get('name'),
                                       -(-len(self.heard) // FRAME)))
                elif event == 'clear':
                    del self.heard[(self.tick + 1) * FRAME:]
                    self.marks = [(name, self.tick) for name, _due in self.marks]
                elif event == 'dtmf':
                    digit = str((data.get('dtmf') or {}).get('digit') or '')
                    if digit:
                        self.keys.press(digit, 'stream')
        except Exception as error:
            self.log(f'stream error: {error!r}')
        finally:
            if not self.closed.is_set():
                self.hung_up_by = self.hung_up_by or 'smitline'
            self.closed.set()

    def play(self, audio):
        self.queue.extend(frames_of(audio))

    def stop_playing(self):
        self.queue.clear()

    async def drained(self, below=0):
        while len(self.queue) > below and not self.closed.is_set():
            await asyncio.sleep(0.02)

    def quiet_seconds(self):
        return (self.tick - self.last_loud) * 0.02

    async def wait_reply(self, *, min_speech=0.4, quiet=1.4, timeout=30.0):
        """Wait for the far side to say something and finish: True, or False on timeout."""
        begin = self.loud_ticks
        deadline = time.monotonic() + timeout
        while not self.closed.is_set() and time.monotonic() < deadline:
            if (self.loud_ticks - begin) * 0.02 >= min_speech and self.quiet_seconds() >= quiet:
                return True
            await asyncio.sleep(0.05)
        return False

    async def hang_up(self, by):
        if self.closed.is_set():
            return
        self.hung_up_by = by
        await self._send({'event': 'stop', 'stop': {}})
        self.closed.set()
        try:
            await self.ws.close()
        except Exception:
            pass

    async def finish(self):
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    # Reading the two tracks afterwards

    def tick_at(self, moment):
        """The tick that was playing at `moment` (time.monotonic)."""
        return bisect.bisect_left(self.tick_times, moment)

    def time_of(self, tick):
        if tick is None or not self.tick_times:
            return None
        if tick < len(self.tick_times):
            return self.tick_times[tick]
        return self.tick_times[-1] + (tick - len(self.tick_times) + 1) * 0.02

    @staticmethod
    def level(track, tick):
        frame = bytes(track[tick * FRAME:(tick + 1) * FRAME])
        return frame_level_db(frame) if frame else -100.0

    def speech_from(self, track, start, stop=None):
        """The first tick from `start` (before `stop`) where SPEECH_FRAMES loud frames begin."""
        last = len(track) // FRAME if stop is None else min(stop, len(track) // FRAME)
        run = 0
        for tick in range(max(0, start), last):
            if self.level(track, tick) > LOUD_DB:
                run += 1
                if run == SPEECH_FRAMES:
                    return tick - run + 1
            else:
                run = 0
        return None

    def quiet_from(self, track, start):
        """The first tick from `start` where TURN_FRAMES quiet frames begin: the turn is over."""
        total = len(track) // FRAME
        run = 0
        for tick in range(max(0, start), max(start, total) + TURN_FRAMES):
            if tick >= total or self.level(track, tick) <= LOUD_DB:
                run += 1
                if run == TURN_FRAMES:
                    return tick - run + 1
            else:
                run = 0
        return None

    def loudest(self, start_tick, end_tick):
        """The loudest 20 ms of Smitline's audio between two ticks, in dB."""
        top = len(self.heard) // FRAME
        return max((self.level(self.heard, tick) for tick in range(max(0, start_tick), min(end_tick, top))),
                   default=-100.0)


def stream_of(twiml):
    """The media stream URL and parameters in <Connect><Stream> TwiML (twilio_client.stream_twiml)."""
    stream = ElementTree.fromstring(twiml).find('.//Stream')
    if stream is None:
        raise ValueError(f'no Stream in {twiml[:200]}')
    return stream.get('url'), {item.get('name'): item.get('value') for item in stream.findall('Parameter')}


def gather_of(twiml):
    """What the owner's phone plays when it answers (twilio_client.gather_twiml): the whisper in a
    <Gather> that posts the key pressed to its action, then what is said with no key."""
    root = ElementTree.fromstring(twiml)
    gather = root.find('Gather')
    if gather is None:
        raise ValueError(f'no Gather in {twiml[:200]}')
    say = gather.find('Say')
    return {
        'action': gather.get('action'),
        'method': gather.get('method'),
        'numDigits': gather.get('numDigits'),
        'timeout': gather.get('timeout'),
        'whisper': ' '.join(((say.text if say is not None else '') or '').split()),
        'loop': say.get('loop') if say is not None else None,
        'after': ' '.join(' '.join((item.text or '').split()) for item in root.findall('Say')),
        'hangup': root.find('Hangup') is not None,
    }


def said_in(twiml):
    """The words of every <Say> in TwiML (say_twiml, when the owner presses another key)."""
    return ' '.join(' '.join((item.text or '').split())
                    for item in ElementTree.fromstring(twiml).iter('Say'))


# People played by GPT-Live -------------------------------------------------------------------

def tool(name, description):
    return {'type': 'function', 'name': name, 'description': description, 'strict': True,
            'parameters': {'type': 'object', 'properties': {}, 'required': [],
                           'additionalProperties': False}}


HANG_UP = tool('hang_up', 'Hang up the phone, once you have said goodbye.')
TRANSFER = tool('transfer_call', 'Transfer the caller to the team that can help, once you have '
                'told them you are transferring them.')


class Turns:
    """A GPT-Live session's words, logged a line a turn."""

    def __init__(self, log, label):
        self.log = log
        self.label = label
        self.text = ''
        self.at = 0.0
        self.all = []

    def add(self, delta):
        self.text += delta or ''
        self.at = time.monotonic()

    def flush(self, force=False):
        if self.text.strip() and (force or time.monotonic() - self.at > 1.2):
            line = ' '.join(self.text.split())
            self.all.append(line)
            self.log(f'{self.label}: {line}')
            self.text = ''


class Business:
    """The business a scenario calls: its menu, hold queue, and people. Each thing it plays is
    kept as a span of ticks (scenario.spans), for telling what woke Smitline's voice."""

    def __init__(self, sim, end, scenario):
        self.sim = sim
        self.end = end
        self.scenario = scenario
        self.log = scenario.log

    def span(self, kind, label, text=None, start=None):
        span = {'kind': kind, 'label': label, 'text': text,
                'start': self.end.tick if start is None else start, 'end': None}
        self.scenario.spans.append(span)
        return span

    async def say(self, text, kind='recording'):
        self.log(f'menu: {text}')
        clip = await self.sim.speech.clip(text)
        await self.end.drained()
        span = self.span(kind, kind, text)
        self.end.play(clip)
        span['end'] = span['start'] + len(frames_of(clip))
        await self.end.drained()

    async def key(self, timeout):
        try:
            return await asyncio.wait_for(self.end.keys.keys.get(), timeout)
        except asyncio.TimeoutError:
            return None

    async def menu(self, prompt, *, valid, tries=3, wait=8.0):
        """Play the options until a valid key: a key stops the prompt, as menus do."""
        clip = await self.sim.speech.clip(prompt)
        for attempt in range(tries):
            self.log(f'menu: {prompt}')
            await self.end.drained()
            span = self.span('menu', 'menu', prompt)
            self.end.play(clip)
            key = await self.key(len(self.end.queue) * 0.02 + wait)
            span['end'] = self.end.tick
            if key is not None:
                self.end.stop_playing()
                self.log(f'menu heard key {key}')
                if key in valid:
                    return key
                await self.say('Sorry, that is not a valid option.')
            elif attempt < tries - 1:
                await self.say('We did not receive your selection.')
        return None

    async def digits(self, *, until='#', timeout=20.0, gap=6.0):
        entered = ''
        key = await self.key(timeout)
        while key is not None and key != until and len(entered) < 20:
            entered += key
            key = await self.key(gap)
        self.log(f'menu heard digits: {entered or "(none)"}' + (' then #' if key == until else ''))
        return entered

    async def hold(self, seconds, *, message=None, first=15.0, every=25.0):
        """Hold music, with a recorded announcement now and then, for `seconds`."""
        self.log(f'hold for {seconds:.0f} s' + (f', with "{message}"' if message else ', music only'))
        clip = await self.sim.speech.clip(message) if message else None
        music = self.sim.music
        await self.end.drained()
        span = self.span('hold', 'hold music')
        begin = time.monotonic()
        next_message = begin + first if clip else None
        position = 0
        try:
            while not self.end.closed.is_set() and time.monotonic() - begin < seconds:
                if next_message is not None and time.monotonic() >= next_message:
                    await self.end.drained()
                    self.log(f'hold announcement: {message}')
                    announced = self.span('announcement', 'announcement', message)
                    self.end.play(clip)
                    announced['end'] = announced['start'] + len(frames_of(clip))
                    await self.end.drained()
                    next_message = time.monotonic() + every
                    continue
                self.end.play(music[position:position + 8000])
                position = (position + 8000) % len(music)
                await self.end.drained(below=10)
            await self.end.drained()
        finally:  # also when the call ends during the hold
            span['end'] = self.end.tick

    async def person(self, label, instructions, *, voice, first_words, opener=None,
                     tools=(HANG_UP,), max_seconds=240, keys_to_voice=False, reprompt_after=None):
        """Someone played by GPT-Live, hearing what Smitline sends, who opens with `first_words`
        (the checks look for them in what Smitline's voice heard). Returns the tool they ended
        with ('hang_up', 'transfer_call'), 'closed' when the call ended, or 'time'."""
        opener = opener or f'You just picked up the call. Say: "{first_words}"'
        config = session_config(
            instructions=instructions, audio_format=PCMU8, voice=voice,
            delegation={'type': 'responses', 'responses': {
                'model': 'gpt-5.6-luna', 'instructions': instructions, 'tools': list(tools),
                'tool_choice': 'auto'}})
        ears = asyncio.Queue()
        talked = {'run': 0}
        end = self.end
        log = self.log
        said = Turns(log, label)
        heard = Turns(log, f'{label} heard')
        ended = {'how': None}
        finished = asyncio.Event()
        span = self.span('person', label, first_words)
        person = {'label': label, 'voice': voice, 'seconds': 0, 'billed': False,
                  'began': time.monotonic()}
        self.scenario.people.append(person)

        def listen(frame):
            ears.put_nowait(frame)
            # Talked over for 300 ms: stop, as a person would.
            if frame_level_db(frame) > -35:
                talked['run'] += 1
                if talked['run'] == 15 and len(end.queue) > 10:
                    end.stop_playing()
                    log(f'{label} stops: Smitline is talking')
            else:
                talked['run'] = 0

        live = None
        try:
            async with LiveSession(self.sim.key, config) as live:
                async def read():
                    async for event in live.events():
                        kind = event.get('type')
                        if kind == 'session.output_audio.delta':
                            end.play(base64.b64decode(event.get('delta') or ''))
                        elif kind == 'session.output_transcript.delta':
                            said.add(event.get('delta'))
                        elif kind == 'session.input_transcript.delta':
                            heard.add(event.get('delta'))
                        elif kind == 'response.event':
                            call = function_call_from(event)
                            if call and call['name'] in {t['name'] for t in tools}:
                                ended['how'] = call['name']
                                log(f'{label} uses {call["name"]}')
                                finished.set()
                        elif kind == 'error':
                            log(f'{label} voice error: {(event.get("error") or {}).get("code")}')
                        elif kind == 'session.closed':
                            finished.set()
                            return

                async def feed():
                    batch = bytearray()
                    while True:
                        batch += await ears.get()
                        if len(batch) >= FRAME * 5:
                            await live.send_audio(bytes(batch))
                            batch.clear()

                async def lines():
                    while True:
                        await asyncio.sleep(0.3)
                        said.flush()
                        heard.flush()

                async def keys():
                    while True:
                        key = await end.keys.keys.get()
                        log(f'{label} is told: the caller pressed {key}')
                        await live.append('session.instructions.append',
                                          f'The caller pressed {key} on the keypad.')

                async def reprompt():
                    """A voice bot asks again when the caller says nothing, as real ones do."""
                    asked = 0
                    while asked < 2:
                        await asyncio.sleep(0.5)
                        if (not end.queue and end.quiet_seconds() >= reprompt_after
                                and time.monotonic() - said.at >= reprompt_after):
                            asked += 1
                            log(f'{label} is told: the caller said nothing')
                            await live.append('session.instructions.append',
                                              'The caller has said nothing. Ask your question again, briefly.')
                            said.at = time.monotonic()

                reader = asyncio.create_task(read())
                helpers = []
                try:
                    await asyncio.wait_for(live.started.wait(), 20)
                    end.listeners.append(listen)
                    helpers = [asyncio.create_task(feed()), asyncio.create_task(lines())]
                    if keys_to_voice:
                        helpers.append(asyncio.create_task(keys()))
                    if reprompt_after:
                        helpers.append(asyncio.create_task(reprompt()))
                    await live.append('session.commentary.append', opener)
                    closed = asyncio.create_task(end.closed.wait())
                    done = asyncio.create_task(finished.wait())
                    helpers += [closed, done]
                    await asyncio.wait({closed, done}, timeout=max_seconds,
                                       return_when=asyncio.FIRST_COMPLETED)
                    how = ended['how'] or ('closed' if end.closed.is_set() else 'time')
                    if ended['how']:
                        await end.drained()  # what it said before using the tool is heard
                        await asyncio.sleep(0.6)
                finally:
                    if listen in end.listeners:
                        end.listeners.remove(listen)
                    for task in helpers:
                        task.cancel()
                    await live.close()  # its final usage comes with session.closed
                    try:
                        await asyncio.wait_for(reader, 5)
                    except BaseException:
                        reader.cancel()
        finally:
            span['end'] = end.tick
            said.flush(force=True)
            heard.flush(force=True)
            self.scenario.voices.append(said.all)
            billed = int(getattr(live, 'usage_seconds', 0) or 0)
            person['billed'] = bool(billed)
            person['seconds'] = billed or round(time.monotonic() - person['began'])
        return how


# The phone provider ---------------------------------------------------------------------------

class Provider:
    """The phone provider: dials the business, rings the owner, and reports each call's status
    through Smitline's gateway, signed, as Twilio does. Its flavor tells the line how to press
    keys (SignalWire: on the media stream; Twilio: as tones in the audio)."""

    def __init__(self, sim, scenario):
        self.sim = sim
        self.scenario = scenario
        self.flavor = scenario.flavor
        self.calls = {}
        self.updates = []
        self.tasks = []
        self.dialed = []

    async def create_call(self, *, to, from_, twiml, status_callback=None, **options):
        sid = 'CA' + uuid.uuid4().hex
        owner = '/twilio/owner-status/' in (status_callback or '')
        self.dialed.append({'to': to, 'from': from_, 'owner': owner,
                            'timeLimit': options.get('time_limit'), 'timeout': options.get('timeout')})
        self.scenario.log(f'provider: dialing {"the owner" if owner else "the business"} at {to}')
        work = (self._owner(sid, twiml, status_callback) if owner
                else self._business(sid, twiml, status_callback))
        self.tasks.append(asyncio.create_task(work))
        return {'sid': sid}

    async def update_call(self, call_sid, **kwargs):
        self.updates.append((call_sid, kwargs))
        end = self.calls.get(call_sid)
        if kwargs.get('status') in ('completed', 'canceled'):
            self.scenario.log(f'provider: Smitline ends call {call_sid[:10]}')
            if end is not None:
                await end.hang_up('smitline')
        return {}

    async def get_call(self, call_sid):
        return {'sid': call_sid, 'status': 'completed', 'price': None, 'price_unit': 'USD'}

    async def _run(self, end, work):
        run = asyncio.create_task(work)
        closed = asyncio.create_task(end.closed.wait())
        await asyncio.wait({run, closed}, return_when=asyncio.FIRST_COMPLETED)
        if run.done() and not run.cancelled() and run.exception():
            error = run.exception()
            self.scenario.log(f'simulation error: {error!r}')
            self.scenario.errors.append(repr(error))
        if not run.done():
            run.cancel()
            await asyncio.gather(run, return_exceptions=True)
        closed.cancel()

    async def _business(self, sid, twiml, status_callback):
        scenario = self.scenario
        url, parameters = stream_of(twiml)
        scenario.business_token = parameters.get('token')
        if not (parameters.get('callId') and parameters.get('token')):
            scenario.errors.append(f'the business call has no callId and token: {sorted(parameters)}')
        await self.sim.post(status_callback, {'CallSid': sid, 'CallStatus': 'ringing'})
        await asyncio.sleep(2.0)
        end = await self.sim.open_stream(url, parameters, sid, scenario.log)
        self.calls[sid] = end
        scenario.business_end = end
        answered = time.monotonic()
        await self._run(end, scenario.business(Business(self.sim, end, scenario)))
        await end.hang_up('business')
        scenario.log(f'provider: the call is over ({end.hung_up_by} hung up)')
        await asyncio.sleep(0.5)
        await self.sim.post(status_callback, {'CallSid': sid, 'CallStatus': 'completed',
                                              'CallDuration': str(int(time.monotonic() - answered) + 1)})
        await end.finish()

    async def _owner(self, sid, twiml, status_callback):
        """The owner's phone (phone_owner.ring_owner): it rings, says the whisper of the <Gather>,
        and the owner presses scenario.owner_key; the gateway's answer to that key is a <Stream>
        to talk on (1), or words and a hang-up (anything else)."""
        scenario = self.scenario
        gather = gather_of(twiml)
        scenario.owner_twiml = gather
        scenario.whisper = gather['whisper']
        call_id = urlsplit(status_callback).path.rstrip('/').rsplit('/', 1)[-1]
        expected = f'/twilio/owner/{call_id}'
        if (urlsplit(gather['action'] or '').path != expected
                or (gather['method'] or '').upper() != 'POST' or gather['numDigits'] != '1'):
            scenario.errors.append(f"the owner's <Gather> does not post one key to {expected}: "
                                   f"{ {key: gather[key] for key in ('action', 'method', 'numDigits')} }")
        scenario.log(f"provider: the owner's phone rings; they hear: \"{gather['whisper']}\"")
        await self.sim.post(status_callback, {'CallSid': sid, 'CallStatus': 'ringing'})
        if scenario.owner is None:
            await asyncio.sleep(26)
            scenario.log('provider: the owner does not answer')
            await self.sim.post(status_callback, {'CallSid': sid, 'CallStatus': 'no-answer',
                                                  'CallDuration': '0'})
            return
        answered = time.monotonic()
        await asyncio.sleep(3.0 + len(gather['whisper']) / 16)  # picks up and listens to the whisper
        scenario.log(f'provider: the owner presses {scenario.owner_key}')
        reply = await self.sim.post(gather['action'], {'CallSid': sid, 'Digits': scenario.owner_key})
        if '<Stream' not in reply:
            scenario.log(f'provider: the owner hears "{said_in(reply)}" and the call to them ends')
            await self.sim.post(status_callback, {'CallSid': sid, 'CallStatus': 'completed',
                                                  'CallDuration': str(int(time.monotonic() - answered) + 1)})
            return
        url, parameters = stream_of(reply)
        token = parameters.get('token')
        scenario.owner_params = {key: value for key, value in parameters.items() if key != 'token'}
        scenario.facts['ownerStreamOwnToken'] = bool(token) and token != scenario.business_token
        end = await self.sim.open_stream(url, parameters, sid, scenario.log)
        self.calls[sid] = end
        scenario.owner_end = end
        await self._run(end, scenario.owner(self.sim, end, scenario.log))
        await end.hang_up('owner')
        await asyncio.sleep(0.5)
        await self.sim.post(status_callback, {'CallSid': sid, 'CallStatus': 'completed',
                                              'CallDuration': str(int(time.monotonic() - answered))})
        await end.finish()


# Smitline, on this computer --------------------------------------------------------------------

class TimedStore(CallStore):
    """The call store, noting when each event was written on this computer's clock, so events
    line up with the business's audio."""

    def __init__(self, root):
        super().__init__(root)
        self.times = {}  # (call id, event id) -> time.monotonic()

    def append_event(self, call_id, event_type, **data):
        event = super().append_event(call_id, event_type, **data)
        self.times[(call_id, event['id'])] = time.monotonic()
        return event


def _speech(delta):
    """GPT-Live output audio (base64 mu-law) with speech in it, not only its streamed silence."""
    try:
        audio = base64.b64decode(delta or '')
    except ValueError:
        return False
    return any(frame_level_db(audio[i:i + FRAME]) >= SPEECH_DB for i in range(0, len(audio), FRAME))


class TimedLive(LiveSession):
    """Smitline's GPT-Live session, unchanged, timed: when it connected, when GPT-Live said it had
    started, when its first speech came and when it closed; and how many audio frames the line
    gave it before it had started, which LiveSession.send_audio drops (GPT-Live never hears
    them)."""

    def __init__(self, api_key, config, sessions):
        super().__init__(api_key, config)
        self.times = {'created': time.monotonic(), 'connected': None, 'started': None,
                      'firstSpeech': None, 'closed': None}
        self.frames_sent = 0
        self.frames_dropped = 0
        sessions.append(self)

    async def __aenter__(self):
        entered = await super().__aenter__()
        self.times['connected'] = time.monotonic()
        return entered

    async def send_audio(self, audio):
        sent = await super().send_audio(audio)
        if sent:
            self.frames_sent += 1
        elif not self.started.is_set():
            self.frames_dropped += 1
        return sent

    async def events(self):
        async for event in super().events():
            kind = event.get('type')
            now = time.monotonic()
            if kind == 'session.started' and self.times['started'] is None:
                self.times['started'] = now
            elif (kind == 'session.output_audio.delta' and self.times['firstSpeech'] is None
                  and _speech(event.get('delta'))):
                self.times['firstSpeech'] = now
            elif kind == 'session.closed' and self.times['closed'] is None:
                self.times['closed'] = now
            yield event

    async def shutdown(self):
        if self.times['closed'] is None and self.times['connected'] is not None:
            self.times['closed'] = time.monotonic()
        await super().shutdown()


def listener_check(env):
    """(detector, None) when the hold listener loads here as the line loads it, else (None, why).
    Without one, calls on hold keep the voice on and wait quietly."""
    try:
        listener = hold_listener.HoldListener.create(dict(env))
    except Exception as error:
        return None, str(error) or type(error).__name__
    return listener.name, None


class Sim:
    def __init__(self, key, out, port, *, detector=None, speech_cache=None):
        self.key = key
        self.out = Path(out)
        self.port = port
        self.detector = detector
        self.base = f'http://127.0.0.1:{port}'
        self.speech = Speech(key, speech_cache or self.out / '.speech')
        self.music = hold_music(30)
        self.provider = None
        self.scenario = None

    async def __aenter__(self):
        self.temp = tempfile.TemporaryDirectory(prefix='phone-sim-')
        self.env = {'OPENAI_API_KEY': self.key, 'TWILIO_ACCOUNT_SID': 'ACsim',
                    'TWILIO_AUTH_TOKEN': AUTH_TOKEN, 'TWILIO_FROM_NUMBER': FROM_NUMBER,
                    'SMITLINE_OWNER_NAME': OWNER_NAME, 'SMITLINE_OWNER_PHONE': OWNER_PHONE,
                    # The audio timing of each call, kept as audio-trace.jsonl in its folder.
                    'SMITLINE_AUDIO_TRACE': '1'}
        if self.detector:
            # The environ PhoneLine gives HoldListener.create (phone_hold.HoldMixin._hold_listener).
            self.env['SMITLINE_HOLD_DETECTOR'] = self.detector
        self.store = TimedStore(Path(self.temp.name) / 'calls')
        hooks = DefaultCallHooks(environ=self.env, store=self.store, clock=lambda: NOW)

        async def public_url():
            return self.base

        def voice(key, config):
            sessions = self.scenario.voice_sessions if self.scenario is not None else []
            return TimedLive(key, config, sessions)

        # No hold_listener_factory: the line makes its listener as it does in the daemon.
        self.line = PhoneLine(public_url=public_url, environ=lambda: self.env,
                              twilio_factory=lambda creds: self.provider, live_factory=voice)
        self.service = CallService(self.store, hooks=hooks, lines={'phone': self.line},
                                   environ=self.env)
        app = create_gateway_app(self.line, self.service, current_url=lambda: self.base)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        listening = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listening.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listening.bind(('127.0.0.1', self.port))
        self.port = listening.getsockname()[1]
        await web.SockSite(self.runner, listening).start()
        self.base = f'http://127.0.0.1:{self.port}'
        self.http = ClientSession(timeout=ClientTimeout(total=None, sock_connect=10))
        return self

    async def __aexit__(self, *exc):
        await self.service.shutdown()
        await self.http.close()
        await self.runner.cleanup()
        self.temp.cleanup()

    async def post(self, url, params):
        """A signed provider callback to the gateway."""
        path = urlsplit(url).path
        signature = compute_signature(AUTH_TOKEN, self.base + path, params)
        async with self.http.post(self.base + path, data=params,
                                  headers={'X-Twilio-Signature': signature}) as response:
            text = await response.text()
            if response.status >= 300:
                raise RuntimeError(f'{path} answered {response.status}')
            return text

    async def open_stream(self, url, parameters, call_sid, log):
        ws = await self.http.ws_connect('ws://' + url.split('://', 1)[1])
        stream_sid = 'MZ' + uuid.uuid4().hex
        await ws.send_str(json.dumps({'event': 'connected', 'protocol': 'Call', 'version': '1.0.0'}))
        await ws.send_str(json.dumps({'event': 'start', 'sequenceNumber': '1', 'streamSid': stream_sid,
                                      'start': {'streamSid': stream_sid, 'callSid': call_sid,
                                                'tracks': ['inbound'], 'customParameters': parameters,
                                                'mediaFormat': {'encoding': 'audio/x-mulaw',
                                                                'sampleRate': 8000, 'channels': 1}}}))
        end = StreamEnd(ws, stream_sid, log)
        end.start()
        return end

    async def run(self, scenario):
        scenario.started = time.monotonic()
        scenario.detector = self.detector
        self.scenario = scenario
        self.provider = Provider(self, scenario)
        if scenario.flavor == 'signalwire':
            scenario.log('provider: SignalWire (keys are pressed on the media stream)')
        record = await self.service.create(dict(scenario.brief))
        call_id = record['id']
        scenario.log(f'Smitline placed call {call_id}')
        for _ in range(100):  # the line's session, read once the call is over (its listener)
            scenario.session = self.line.session(call_id)
            if scenario.session is not None:
                break
            await asyncio.sleep(0.05)
        deadline, ended = time.monotonic() + 900, False
        while True:
            record = await self.service.wait(call_id, timeout=30, owner='local')
            if record['status'] in TERMINAL:
                break
            if time.monotonic() > deadline:
                if ended:
                    scenario.errors.append('the call did not finish after it was ended')
                    break
                scenario.errors.append('the call ran past 15 minutes; the simulator ended it')
                await self.service.end(call_id, owner='local')
                deadline, ended = time.monotonic() + 120, True
        for outcome in await asyncio.gather(*self.provider.tasks, return_exceptions=True):
            if isinstance(outcome, BaseException):
                scenario.errors.append(repr(outcome))
        scenario.record = record
        scenario.events = self.service.events(call_id)
        scenario.event_times = {event['id']: self.store.times.get((call_id, event['id']))
                                for event in scenario.events}
        listener = getattr(scenario.session, '_listener', None)
        if listener is not None:
            scenario.listener = {'name': listener.name, 'stats': dict(listener.stats or {})}
        scenario.listener_error = getattr(scenario.session, '_listener_error', None)
        scenario.log(f'result: {record["status"]}, endReason {record.get("endReason")}, '
                     f'outcome {(record.get("result") or {}).get("outcome")}')
        self.save(scenario)
        return record

    def save(self, scenario):
        folder = self.out / scenario.name
        folder.mkdir(parents=True, exist_ok=True)
        business = scenario.business_end
        if business is not None:
            write_wav(folder / 'stereo.wav', business.sent, business.heard)
            write_wav(folder / 'mix.wav', mix_ulaw(business.sent, business.heard))
        if scenario.owner_end is not None:
            write_wav(folder / 'owner.wav', scenario.owner_end.sent, scenario.owner_end.heard)
        events = [dict(event, t=scenario.t(event)) for event in scenario.events]
        files = {
            'record.json': json.dumps(scenario.record, indent=2, ensure_ascii=False),
            'events.json': json.dumps(events, indent=2, ensure_ascii=False),
            'transcript.txt': ''.join(f'{speaker}: {text}\n' for speaker, text, _at in scenario.lines_said()),
            'business.txt': '\n'.join(scenario.lines) + '\n',
            'voice-sessions.json': json.dumps(scenario.session_rows(), indent=2),
        }
        trace = self.store.root / scenario.record['id'] / 'audio-trace.jsonl'
        if trace.exists():
            files['audio-trace.jsonl'] = trace.read_text()
        for name, text in files.items():
            if self.key in text:
                raise RuntimeError(f'{name} would contain the API key; not written')
            (folder / name).write_text(text)


# Scenarios ---------------------------------------------------------------------------------------

INTRO = ('Thank you for calling Northwind Telecom. All of our representatives are busy helping '
         'other customers. Your call will be answered in the order it was received.')
STAY = 'Your call is important to us. Please stay on the line.'
STAY_LONG = ('Your call is important to us. Please stay on the line, and the next available '
             'representative will be with you shortly.')
BILL_FACTS = (f'Account {ACCOUNT} belongs to Arun Raj. His bill was $65 a month and is $85 since '
              'October, because his 12-month promotion ended on October 1. You can offer a $10 a '
              'month loyalty credit for the next 12 months, and nothing more.')
INSTALL_FACTS = (f'Account {ACCOUNT} belongs to Arun Raj. His internet installation is booked for '
                 'Thursday, November 19, between 8 AM and noon. Saturday, November 21, from 9 to 11 '
                 'AM is free, and you can move it there.')
OUTAGE_FACTS = (f'Account {ACCOUNT} belongs to Arun Raj, at 12 Maple Avenue. There has been an '
                'internet outage on Maple Avenue since 7 this morning: a cable was cut, a crew is '
                'on site, and service should be back by 6 PM today. You can add a one-day service '
                'credit to the account.')


def person_instructions(name, team, facts, extra=''):
    return (f'You are {name}, a friendly {team} representative at Northwind Telecom, answering a '
            'phone call that was waiting in the hold queue. Talk like a real person on the phone: '
            f'short and natural, under 25 words a turn. {facts} Before discussing the account, ask '
            "for the account holder's name and the account number or the phone number on it. If "
            'the caller says they are an AI assistant calling for the customer, that is fine: help '
            'them. If they ask you to hold while the account holder joins, say sure and wait '
            f'quietly; then talk with the account holder. {extra}When everything is settled, thank '
            'them, say goodbye, and then use hang_up.')


BOT_INSTRUCTIONS = (
    "You are the automated virtual assistant on Northwind Telecom's phone line: a voice bot, not a "
    'person, and you say so if asked. Ask what the caller is calling about, in a few words. When '
    'it is about an installation or a technician appointment, say: "To confirm, press 1 if this is '
    'about an existing appointment, or 2 for something new." You are told which key the caller '
    'pressed. Then say exactly "Thanks. Transferring you to our scheduling team now. Please hold." '
    'and use transfer_call. Keep every turn under 20 words.')


async def hold_timeout_business(b):
    await b.say(INTRO)
    await b.hold(600, message=STAY, first=20, every=20)


async def menu_hold_rep_business(b):
    await b.say('Thank you for calling Northwind Telecom. Para español, oprima el nueve.')
    key = await b.menu('For billing, press 2. For technical support, press 3. For all other '
                       'questions, press 0. To hear these options again, press star.',
                       valid='230')
    if key != '2':
        b.log(f'the caller did not choose billing ({key})')
    await b.say('Billing. Please enter your ten digit account number, followed by the pound key.')
    account = await b.digits()
    b.scenario.facts['account'] = account
    if account != ACCOUNT:
        await b.say("Sorry, we couldn't find that account.")
    await b.say('Thank you. All of our billing representatives are helping other customers. Your '
                'call will be answered in the order it was received.')
    await b.hold(45, message=STAY_LONG, first=15, every=22)
    how = await b.person('Dana (rep)', person_instructions('Dana', 'billing', BILL_FACTS),
                         voice='willow',
                         first_words='Thanks for holding, this is Dana in billing. Who am I speaking with?')
    b.scenario.facts['rep_ended'] = how


async def voice_bot_business(b):
    how = await b.person('Northwind bot', BOT_INSTRUCTIONS, voice='beacon', tools=(TRANSFER,),
                         keys_to_voice=True, max_seconds=120, reprompt_after=7.0,
                         first_words="Hi, you've reached Northwind Telecom. I'm the virtual "
                                     "assistant. In a few words, tell me what you're calling about.")
    b.scenario.facts['bot_ended'] = how
    if how != 'transfer_call':
        b.log('the bot never transferred the call')
        return
    await b.hold(35)
    how = await b.person('Sam (scheduling)', person_instructions('Sam', 'scheduling', INSTALL_FACTS),
                         voice='stone',
                         first_words='Thanks for waiting, scheduling, this is Sam. How can I help?')
    b.scenario.facts['rep_ended'] = how


async def connect_owner_business(b):
    await b.say('Thank you for calling Northwind Telecom.')
    await b.menu('For billing, press 2. For technical support, press 3.', valid='23')
    await b.say('Please hold for the next available billing representative.')
    await b.hold(25, message=STAY, first=12, every=20)
    how = await b.person(
        'Dana (rep)', person_instructions(
            'Dana', 'billing', BILL_FACTS,
            extra=('If the account holder hands the call back to their assistant, say you are '
                   'welcome and wait for the assistant to speak; say goodbye to the assistant '
                   'before you hang up. ')),
        voice='willow', max_seconds=300,
        first_words='Thanks for holding, this is Dana in billing. Who am I speaking with?')
    b.scenario.facts['rep_ended'] = how


async def connect_owner_owner(sim, end, log):
    """The owner joins after pressing 1, talks with the rep, hands back to the voice and hangs up
    while the rep is still there, so the voice comes back to see them off."""
    await asyncio.sleep(1.0)
    for text in ("Hi, this is Arun, the account holder. Thanks for waiting.",
                 "I'm calling about my bill. It went up by twenty dollars in October. Can you do "
                 "anything about that?",
                 "Okay, the ten dollar credit works for me. Thanks so much. My assistant will take "
                 "it from here."):
        log(f'owner: {text}')
        end.play(await sim.speech.clip(text, 'owner'))
        await end.drained()
        if not await end.wait_reply(timeout=30):
            log('owner: (no answer heard)')
    await asyncio.sleep(1.5)
    log('owner hangs up')
    await end.hang_up('owner')


async def long_hold_repeats_business(b):
    await b.say(INTRO)
    # About three minutes, the same announcement every 30 seconds or so (it is 7.6 s long).
    await b.hold(180, message=STAY_LONG, first=30, every=23)
    how = await b.person('Priya (rep)', person_instructions('Priya', 'technical support', OUTAGE_FACTS),
                         voice='vesper', max_seconds=180,
                         first_words='Thanks for holding, this is Priya in technical support. Who '
                                     'am I speaking with?')
    b.scenario.facts['rep_ended'] = how


class Scenario:
    def __init__(self, name, about, brief, business, *, flavor='twilio', owner=None, owner_key='1',
                 checks=None):
        self.name = name
        self.about = about
        self.brief = brief
        self.business = business
        self.flavor = flavor
        self.owner = owner
        self.owner_key = owner_key
        self.checks = checks or (lambda scenario: [])
        self.lines = []          # what the business did and heard
        self.spans = []          # what the business played, in ticks of its stream
        self.voices = []         # what each of its people said
        self.people = []         # its people's GPT-Live sessions
        self.facts = {}
        self.errors = []
        self.started = None
        self.business_end = None
        self.owner_end = None
        self.business_token = None
        self.whisper = None
        self.owner_twiml = None
        self.owner_params = None
        self.record = None
        self.events = []
        self.event_times = {}    # event id -> time.monotonic()
        self.session = None      # the line's PhoneSession
        self.voice_sessions = []  # Smitline's voice: TimedLive, in order
        self.listener = None
        self.listener_error = None
        self.detector = None     # asked for; None is the line's default

    def log(self, text):
        at = time.monotonic() - (self.started or time.monotonic())
        line = f'{at:6.1f}s  {text}'
        self.lines.append(line)
        print(f'[{self.name}] {line}', flush=True)

    # Facts the checks read.

    @property
    def result(self):
        return (self.record or {}).get('result') or {}

    def hold(self):
        return (self.record or {}).get('hold') or {}

    def usage(self):
        return (self.record or {}).get('usage') or {}

    def mono(self, event):
        return self.event_times.get(event['id'])

    def t(self, event):
        at = self.mono(event)
        return None if at is None or self.started is None else round(at - self.started, 3)

    def events_of(self, kind):
        return [event for event in self.events if event['type'] == kind]

    def lines_said(self):
        """(speaker, text, time.monotonic()) of every transcript line, notes included."""
        return [((event.get('data') or {}).get('speaker'), (event.get('data') or {}).get('text'),
                 self.mono(event)) for event in self.events_of('call.transcript')]

    def notes(self):
        return [text for speaker, text, _at in self.lines_said() if speaker == 'note']

    def stream_seconds(self):
        return round(self.business_end.tick * 0.02) if self.business_end else None

    def hold_played(self):
        """Seconds the business played its hold queue."""
        return sum((span['end'] - span['start']) * 0.02 for span in self.spans
                   if span['kind'] == 'hold' and span['end'] is not None)

    def voice_open(self):
        """Seconds Smitline's voice sessions were open, measured here."""
        total = 0.0
        for live in self.voice_sessions:
            began = live.times['started'] or live.times['connected']
            if began is not None:
                total += (live.times['closed'] or time.monotonic()) - began
        return total

    def quiet_on_hold(self):
        """How loud Smitline was while the business played its hold: it should be silent."""
        end = self.business_end
        spans = [span for span in self.spans if span['kind'] == 'hold' and span['end'] is not None]
        if end is None or not spans:
            return None
        # A second into the hold, Smitline's last words before it have played.
        return max(end.loudest(span['start'] + 50, span['end']) for span in spans)

    def span_at(self, tick):
        """What the business was playing at a tick: the latest thing started by then that had not
        ended two seconds before (an announcement within the hold music, say)."""
        playing = [span for span in self.spans if span['start'] <= tick
                   and (span['end'] is None or tick <= span['end'] + 100)]
        return max(playing, key=lambda span: span['start'], default=None)

    def session_rows(self):
        def since(moment):
            return None if moment is None else round(moment - self.started, 3)
        return [{'n': index + 1, **{key: since(value) for key, value in live.times.items()},
                 'framesSent': live.frames_sent, 'framesDroppedBeforeStart': live.frames_dropped,
                 'usageSeconds': live.usage_seconds, 'closeReason': live.close_reason}
                for index, live in enumerate(self.voice_sessions)]

    def wake_rows(self):
        """Each time Smitline's voice came back on: a wake on hold (call.hold_wake) or the owner
        leaving (call.owner_left). Seconds are from the wake."""
        end = self.business_end
        triggers = [event for event in self.events
                    if event['type'] in ('call.hold_wake', 'call.owner_left') and self.mono(event)]
        rows = []
        for index, event in enumerate(triggers):
            at = self.mono(event)
            until = self.mono(triggers[index + 1]) if index + 1 < len(triggers) else None
            woke = event['type'] == 'call.hold_wake'
            row = {'n': index + 1, 'why': 'hold' if woke else 'owner left',
                   'at': at - self.started, 'speechMs': (event.get('data') or {}).get('speechMs')}
            live = next((live for live in self.voice_sessions if live.times['created'] >= at - 0.05
                         and (until is None or live.times['created'] < until)), None)
            if live is not None:
                row['started'] = None if live.times['started'] is None else live.times['started'] - at
                row['dropped'] = live.frames_dropped
                # GPT-Live's first speech, before Smitline paced it out to the line.
                first = live.times['firstSpeech']
                row['spoke'] = None if first is None else first - at
            rewait = next((other for other in self.events_of('call.hold_rewait')
                           if self.mono(other) and self.mono(other) >= at
                           and (until is None or self.mono(other) < until)), None)
            if rewait is not None:
                row['rewait'] = ((rewait.get('data') or {}).get('why'), self.mono(rewait) - at)
            if end is not None and end.tick_times:
                tick = end.tick_at(at)
                span = self.span_at(tick)
                row['what'] = describe(span)
                # Smitline's speech counts until this voice went back to waiting, or the next wake.
                stop = self.mono(rewait) if rewait is not None else until
                heard = end.speech_from(end.heard, tick, None if stop is None else end.tick_at(stop))
                row['heard'] = None if heard is None else end.time_of(heard) - at
                if woke and span is not None and span['kind'] == 'person':
                    onset = end.speech_from(end.sent, span['start'])
                    if onset is not None:
                        row['onset'] = end.time_of(onset) - at
                        over = end.quiet_from(end.sent, onset)
                        row['turnOver'] = None if over is None else end.time_of(over) - at
            rows.append(row)
        return rows

    def pickup_span(self):
        """The person who picked up after the business's first hold."""
        holds = [span for span in self.spans if span['kind'] == 'hold' and span['end'] is not None]
        if not holds:
            return None
        return next((span for span in self.spans if span['kind'] == 'person'
                     and span['start'] >= holds[0]['end'] - 5), None)

    def pickup_reply(self):
        """When the person who picked up started talking and finished their first turn (in what
        the business sent), and when Smitline's voice was first heard after their first words."""
        end, person = self.business_end, self.pickup_span()
        if end is None or person is None:
            return None
        label = person['label']
        onset = end.speech_from(end.sent, person['start'])
        if onset is None:
            return {'gap': None, 'text': f'{label} never spoke'}
        over = end.quiet_from(end.sent, onset)
        heard = end.speech_from(end.heard, onset)

        def at(tick):
            return end.time_of(tick) - self.started

        if heard is None:
            return {'gap': None, 'text': f'{label} picked up at {at(onset):.1f} s; Smitline never answered'}
        gap = None if over is None else end.time_of(heard) - end.time_of(over)
        return {'gap': gap, 'text': (
            f'{label} answered: their first words at {at(onset):.1f} s, first turn over at '
            f'{"?" if over is None else f"{at(over):.1f}"} s; Smitline heard at {at(heard):.1f} s, '
            f'{"?" if gap is None else f"{gap:+.1f}"} s after their turn (at most {REPLY_IN_TIME:g} s)')}

    def first_words_heard(self):
        """What Smitline's voice transcribed first from the person who picked up, against what
        they said first: the replay on waking should give it their first words."""
        end, person = self.business_end, self.pickup_span()
        if end is None or person is None or not person['text']:
            return None
        began = end.time_of(person['start'])
        line = next((text for speaker, text, at in self.lines_said()
                     if speaker == 'other' and at is not None and at >= began), None)
        expected = words(person['text'])[:6]
        if line is None:
            return {'ratio': 0.0, 'text': f"Smitline's voice transcribed nothing from {person['label']}"}
        got = set(words(line))
        ratio = sum(word in got for word in expected) / len(expected)
        return {'ratio': ratio, 'text': (f"the voice heard {person['label']}'s first words "
                                         f'("{clipped(person["text"], 50)}"): "{clipped(line, 100)}"')}


def describe(span):
    if span is None:
        return 'nothing'
    if span['kind'] == 'person':
        return span['label']
    if span['kind'] == 'hold':
        return 'hold music'
    return f'{span["kind"]} "{clipped(span["text"], 40)}"'


def brief(objective, **extra):
    payload = {'channel': 'phone', 'to': BUSINESS_PHONE, 'onBehalfOf': OWNER_NAME,
               'objective': objective, 'maxMinutes': 8}
    payload.update(extra)
    return payload


# Checks: (ok, what was seen). OK is PASS in the summary; anything else is CHECK, to look at.

def fmt_db(value):
    return 'nothing' if value is None else f'{value:.1f} dB'


def detector_check(s):
    asked = s.detector or 'firered'
    hold = s.hold()
    used = hold.get('detector', 'missing') if hold else 'missing'
    why = next(((event.get('data') or {}).get('reason')
                for event in s.events_of('call.hold_listener_unavailable')), None)
    text = f'detector in the record: {used!r} (asked for {asked})'
    if why:
        text += f'; the listener did not load ({why}), so the voice stayed on while holding'
    return used == asked, text


def hold_record_check(s):
    """The record's hold field once the call is over (phone_hold.HoldMixin.hold_state)."""
    hold = s.hold()
    if not hold:
        return False, 'the record has no hold field'
    played = s.hold_played()
    problems = []
    if hold.get('onHold') is not False:
        problems.append(f'onHold {hold.get("onHold")}')
    if 'since' in hold:
        problems.append('since is still there')
    if not hold.get('times'):
        problems.append(f'times {hold.get("times")}')
    # The call holds from the voice's wait_on_hold (often during the "please hold" recording before
    # the music) to the voice answering the person who picked up, a few seconds past the music.
    seconds = hold.get('seconds')
    if not isinstance(seconds, int) or abs(seconds - played) > max(25, played / 4):
        problems.append(f'seconds {seconds} against {played:.0f} s of hold played')
    if s.usage().get('holdSeconds') != seconds:
        problems.append(f'usage.holdSeconds {s.usage().get("holdSeconds")}')
    return not problems, (f'hold record {hold}; the business played {played:.0f} s of hold'
                          + (f': {"; ".join(problems)}' if problems else ''))


def voice_off_check(s):
    """While the call holds (and while the owner talks), Smitline's GPT-Live session is closed."""
    usage = s.usage()
    connected = usage.get('phoneSeconds') or s.stream_seconds()
    voice = usage.get('voiceSeconds')
    held = usage.get('holdSeconds') or 0
    owner = usage.get('ownerTalkedSeconds') or 0
    text = (f'voice off while holding: usage.voiceSeconds {voice} against {connected} s connected '
            f'(on hold {held} s, owner talking {owner} s; {len(s.voice_sessions)} voice sessions, '
            f'open {s.voice_open():.0f} s as measured here)')
    if held + owner < 20:
        return voice is not None, text + '; too little hold to judge'
    return bool(voice is not None and connected and voice <= connected - (held + owner) / 2), text


def common_checks(s):
    record = s.record or {}
    loudest = s.quiet_on_hold()
    held = s.events_of('call.on_hold')
    noted = [text for text in s.notes() if text == 'On hold.']
    return [
        (record.get('status') == 'completed', f'status {record.get("status")}'),
        (not s.errors, f'simulation errors: {s.errors or "none"}'),
        (loudest is not None and loudest < QUIET_DB,
         f'Smitline silent while the business played its hold (loudest {fmt_db(loudest)})'),
        (bool(held and noted), f'the call went on hold: {len(held)} call.on_hold, '
                               f'{len(noted)} "On hold." notes'),
        detector_check(s),
        hold_record_check(s),
        voice_off_check(s),
    ]


def pickup_checks(s):
    notes = s.notes()
    picked = [text for text in notes if PICKUP_NOTE.match(text)]
    ended = [event.get('data') for event in s.events_of('call.hold_ended')]
    reply = s.pickup_reply()
    heard = s.first_words_heard()
    return [
        (bool(picked), f'pickup noted: {picked[0] if picked else "no"}'),
        (any((item or {}).get('by') == 'person' for item in ended), f'call.hold_ended: {ended}'),
        (bool(reply and reply['gap'] is not None and -0.5 <= reply['gap'] <= REPLY_IN_TIME),
         reply['text'] if reply else 'nobody picked up after the hold'),
        (bool(heard and heard['ratio'] >= 0.6), heard['text'] if heard else 'no first words to look for'),
    ]


def outcome_check(s, *good):
    outcome = s.result.get('outcome')
    return outcome in good, f'outcome {outcome}'


def hold_timeout_checks(s):
    hold = s.hold()
    notes = s.notes()
    gave_up = [text for text in notes if GAVE_UP_NOTE.match(text)]
    ended = [event.get('data') for event in s.events_of('call.hold_ended')]
    return common_checks(s) + [
        (s.record.get('endReason') == 'hold_timeout', f'endReason {s.record.get("endReason")}'),
        outcome_check(s, 'not_reached'),
        (hold.get('timedOut') is True, f'hold.timedOut {hold.get("timedOut")}'),
        (bool(gave_up), f'note: {gave_up[0] if gave_up else "none says nobody picked up"}'),
        (bool(s.events_of('call.hold_timeout')) and any((item or {}).get('by') == 'timeout'
                                                        for item in ended),
         f'call.hold_timeout x{len(s.events_of("call.hold_timeout"))}, call.hold_ended: {ended}'),
        (not any(PICKUP_NOTE.match(text) for text in notes), 'nobody noted as picking up'),
        (s.business_end is not None and s.business_end.hung_up_by == 'smitline',
         f'Smitline hung up ({s.business_end.hung_up_by if s.business_end else "no call"})'),
    ]


def menu_hold_rep_checks(s):
    pressed = [(event.get('data') or {}).get('keys') for event in s.events_of('call.keys_pressed')]
    heard = ''.join(key for key, _how in (s.business_end.keys.heard if s.business_end else ()))
    return common_checks(s) + pickup_checks(s) + [
        (pressed[:1] == ['2'], f'keys pressed (call.keys_pressed): {pressed}'),
        (s.facts.get('account') == ACCOUNT, f'account the menu got: {s.facts.get("account")}'),
        (heard.startswith('2'), f'the business heard keys {heard or "none"}'),
        outcome_check(s, 'achieved', 'partial'),
    ]


def voice_bot_checks(s):
    keys = s.business_end.keys.heard if s.business_end else []
    return common_checks(s) + pickup_checks(s) + [
        (s.facts.get('bot_ended') == 'transfer_call', f'the bot: {s.facts.get("bot_ended")}'),
        (('1', 'stream') in keys, f'keys on the stream: {keys}'),
        outcome_check(s, 'achieved', 'partial'),
    ]


def connect_owner_checks(s):
    notes = s.notes()
    usage = s.usage()
    ringing_note = f"Ringing {OWNER_NAME}'s phone."
    joined_note = f'{OWNER_NAME} joined the call.'
    left_note = f'{OWNER_NAME} left the call.'
    away_note = f"{OWNER_NAME} couldn't come to the phone."
    ringing = [event.get('data') for event in s.events_of('call.owner_ringing')]
    connected = s.events_of('call.owner_connected')
    gone = s.events_of('call.owner_left')
    unavailable = [event.get('data') for event in s.events_of('call.owner_unavailable')]
    said = [(speaker, text) for speaker, text, _at in s.lines_said()]
    joined = next((i for i, line in enumerate(said) if line == ('note', joined_note)), None)
    left = next((i for i, line in enumerate(said) if line == ('note', left_note)), None)
    while_owner = ([text for speaker, text in said[joined + 1:left] if speaker == 'agent']
                   if joined is not None and left is not None else None)
    gather = s.owner_twiml or {}
    back = next((row for row in s.wake_rows() if row['why'] == 'owner left'), None)
    return common_checks(s) + pickup_checks(s) + [
        (ringing == [{'why': 'pickup'}] and ringing_note in notes,
         f'call.owner_ringing: {ringing}; "{ringing_note}" noted: {ringing_note in notes}'),
        (bool(s.whisper and s.whisper.startswith(f'Hi {OWNER_NAME}, it is Smitline.')
              and 'Press 1' in s.whisper), f'the owner heard: {s.whisper}'),
        (gather.get('numDigits') == '1' and gather.get('loop') == '2' and bool(gather.get('hangup')),
         f"the owner's <Gather>: numDigits {gather.get('numDigits')}, said {gather.get('loop')} "
         f"times, then \"{gather.get('after')}\" and a hang-up: {gather.get('hangup')}"),
        (bool(s.owner_params and s.owner_params.get('leg') == 'owner'
              and s.facts.get('ownerStreamOwnToken')),
         f"the owner's <Stream> after pressing 1: {s.owner_params}, a token of its own: "
         f"{s.facts.get('ownerStreamOwnToken')}"),
        (bool(connected) and joined_note in notes,
         f'call.owner_connected x{len(connected)}; "{joined_note}" noted: {joined_note in notes}'),
        (while_owner == [], f'the voice said nothing while {OWNER_NAME} talked: {while_owner}'),
        (bool(gone) and left_note in notes,
         f'call.owner_left x{len(gone)}; "{left_note}" noted: {left_note in notes}'),
        (not unavailable and away_note not in notes,
         f'call.owner_unavailable: {unavailable or "none"}; "{away_note}" noted: {away_note in notes}'),
        ((usage.get('ownerLegSeconds') or 0) > 0 and (usage.get('ownerTalkedSeconds') or 0) > 0,
         f'ownerLegSeconds {usage.get("ownerLegSeconds")}, '
         f'ownerTalkedSeconds {usage.get("ownerTalkedSeconds")}'),
        (bool(back and back.get('heard') is not None),
         f'the voice came back after {OWNER_NAME} left: ' + (
             'no' if back is None else
             f'session started {seconds(back.get("started"))}, '
             f'Smitline heard {seconds(back.get("heard"))} after call.owner_left')),
    ]


def long_hold_checks(s):
    hold = s.hold()
    rewaits = [(event.get('data') or {}).get('why') for event in s.events_of('call.hold_rewait')]
    announced = len([span for span in s.spans if span['kind'] == 'announcement'])
    return common_checks(s) + pickup_checks(s) + [
        ((hold.get('skippedRepeats') or 0) > 0,
         f'repeats skipped: skippedRepeats {hold.get("skippedRepeats")} of {announced} announcements'),
        (1 <= (hold.get('wakes') or 0) <= 3,
         f'wakes {hold.get("wakes")} (the first announcement and the rep; at most one more)'),
        ('recording' in rewaits, f'call.hold_rewait: {rewaits}'),
        outcome_check(s, 'achieved', 'partial'),
    ]


SCENARIOS = {
    'hold-timeout': lambda: Scenario(
        'hold-timeout', 'A queue that never answers, with maxHoldMinutes 1.',
        brief('Ask Northwind Telecom whether there is an internet outage on Arun\'s street.',
              maxHoldMinutes=1, context=f'Northwind account number: {ACCOUNT}.'),
        hold_timeout_business, checks=hold_timeout_checks),
    'menu-hold-rep': lambda: Scenario(
        'menu-hold-rep', 'A keypad menu, an account number, a queue, then a billing rep (Twilio: tones).',
        brief("Ask Northwind Telecom why Arun's October bill went up by $20, and get it lowered if "
              'possible.', context=f'Northwind account number: {ACCOUNT}. The bill went from $65 to '
              '$85 in October.', mayAgreeTo=['a monthly credit or discount of any size'],
              mustNotShare=['payment card details']),
        menu_hold_rep_business, checks=menu_hold_rep_checks),
    'voice-bot': lambda: Scenario(
        'voice-bot', 'A voice bot that takes a key press, music-only hold, then a scheduler (SignalWire).',
        brief('Move Arun\'s Northwind Telecom internet installation from Thursday to Saturday '
              'morning if possible.', context=f'Northwind account number: {ACCOUNT}. The '
              'installation is booked for Thursday, November 19, 8 AM to noon.',
              mayAgreeTo=['any Saturday morning time slot']),
        voice_bot_business, flavor='signalwire', checks=voice_bot_checks),
    'connect-owner': lambda: Scenario(
        'connect-owner', 'Through a menu and a queue; when the rep picks up, Arun is rung, presses 1 '
        'and talks, then hangs up and the voice sees the rep off.',
        brief("Get Arun connected with Northwind Telecom billing about his October bill.",
              context=f'Northwind account number: {ACCOUNT}.', connectOwner=True),
        connect_owner_business, owner=connect_owner_owner, checks=connect_owner_checks),
    'long-hold-repeats': lambda: Scenario(
        'long-hold-repeats', 'About three minutes on hold with the same announcement every 30 s or '
        'so, then a rep: the repeats should not wake the voice.',
        brief("Ask Northwind Telecom whether there is an internet outage on Arun's street and when "
              'it will be fixed.', context=f'Northwind account number: {ACCOUNT}. Arun lives at 12 '
              'Maple Avenue.', mayAgreeTo=['a service credit']),
        long_hold_repeats_business, checks=long_hold_checks),
}


# The summary -------------------------------------------------------------------------------------

def seconds(value, sign=True):
    if value is None:
        return '–'
    return f'{value:+.1f} s' if sign else f'{value:.1f} s'


def wake_table(rows):
    if not rows:
        return ['No wakes: the voice never came back on by itself.']
    lines = ['| # | at | why | what the business was playing | their first words | session started | '
             'audio dropped before it started | Smitline heard | then |',
             '|---|---|---|---|---|---|---|---|---|']
    for row in rows:
        if 'rewait' in row:
            then = f'back to waiting {seconds(row["rewait"][1])} ({row["rewait"][0]})'
        elif row.get('heard') is not None and row.get('turnOver') is not None:
            then = f'answered {row["heard"] - row["turnOver"]:+.1f} s after their first turn'
        else:
            then = ''
        dropped = row.get('dropped')
        heard = 'silent' if row.get('heard') is None else seconds(row['heard'])
        if row.get('heard') is not None and row.get('spoke') is not None:
            heard += f' (GPT-Live spoke {seconds(row["spoke"])})'
        lines.append(
            f'| {row["n"]} | {row["at"]:.1f} s | {row["why"]} | {row.get("what", "")} | '
            f'{seconds(row.get("onset"))} | {seconds(row.get("started"))} | '
            f'{"–" if dropped is None else f"{dropped} frames ({dropped * 0.02:.1f} s)"} | '
            f'{heard} | {then} |')
    return lines


def openai_cost(s):
    """(by the record: Smitline's voice, backend and summary; the business's people)."""
    cost = (s.record or {}).get('cost') or {}
    people = sum(person['seconds'] for person in s.people) / 60 * VOICE_PER_MINUTE[VOICE_MODEL]
    return cost.get('openai'), people


def scenario_summary(scenario):
    """One scenario's part of summary.md."""
    lines = [f'## {scenario.name}', '', scenario.about, '']
    if scenario.record is None:
        return lines + [f'- The scenario failed before its call ended: {"; ".join(scenario.errors)}', '']
    record = scenario.record
    result = scenario.result
    usage = scenario.usage()
    smitline, people = openai_cost(scenario)
    stats = (scenario.listener or {}).get('stats') or {}
    lines += [f'- Outcome: **{result.get("outcome")}**, endReason `{record.get("endReason")}`; '
              f'{result.get("summary")}',
              f'- Hold: {record.get("hold")}',
              f'- Line and voice: connected {usage.get("phoneSeconds")} s; Smitline\'s GPT-Live '
              f'{usage.get("voiceSeconds")} s in {len(scenario.voice_sessions)} sessions; on hold '
              f'{usage.get("holdSeconds", 0)} s; owner talking {usage.get("ownerTalkedSeconds", 0)} s',
              f'- Listener: {(scenario.listener or {}).get("name") or scenario.listener_error or "none"}'
              + (f', heard {stats.get("seconds")} s of the line with {stats.get("cpuMs")} ms of CPU; '
                 f'wakes {stats.get("wakes")}, skippedRepeats {stats.get("skippedRepeats")}, '
                 f'skippedMusic {stats.get("skippedMusic")}' if stats else ''),
              f'- OpenAI: about ${smitline} for Smitline (voice, backend, summary; the record\'s '
              f'cost.openai) and ${people:.3f} for the business\'s people '
              f'({sum(person["seconds"] for person in scenario.people)} s of GPT-Live); the '
              'phone part of the record\'s cost is the simulated provider\'s',
              '', 'Wakes:', '']
    lines += wake_table(scenario.wake_rows())
    lines += ['', 'Checks:', '']
    for ok, text in scenario.checks(scenario):
        lines.append(f'- {"PASS" if ok else "CHECK"}: {text}')
    lines += ['', 'Transcript:', '', '```']
    lines += [f'{speaker}: {text}' for speaker, text, _at in scenario.lines_said()]
    return lines + ['```', '']


def summary(scenarios, out, *, detector=None, listener=None):
    """OUT/summary.md for the scenarios of this run, and each scenario's own part in its folder
    (OUT/<scenario>/summary.md), which a later run of other scenarios into OUT leaves alone."""
    loaded, why = listener or (None, None)
    head = ['# Hold for Me: simulated calls', '',
            f'Run {datetime.now().strftime("%Y-%m-%d %H:%M")}; detector asked for: '
            f'{detector or "firered (the line default)"}; the hold listener '
            + (f'loads here ({loaded}).' if loaded else f'does not load here ({why}): calls on hold '
               'keep the voice on.'),
            '',
            'Each folder has stereo.wav (left: the business, right: Smitline), mix.wav, the call '
            'record, events (t: seconds since the scenario started), the transcript, what the '
            "business did (business.txt), and each GPT-Live session of Smitline's voice "
            '(voice-sessions.json). Wakes: seconds from the wake (the call.hold_wake event, or '
            'call.owner_left); "Smitline heard" is its first 60 ms of speech at the business.', '']
    lines = list(head)
    for scenario in scenarios:
        part = scenario_summary(scenario)
        lines += part
        folder = Path(out) / scenario.name
        if folder.is_dir():
            (folder / 'summary.md').write_text('\n'.join(head + part))
    (Path(out) / 'summary.md').write_text('\n'.join(lines))
    return lines


def parse_args(argv=None):
    parser = argparse.ArgumentParser(prog='phone_sim.py', description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('scenarios', nargs='*', metavar='scenario',
                        help=f'any of {", ".join(SCENARIOS)} (default: all)')
    parser.add_argument('--out', default='phone-sim', help='where the results go (default: phone-sim)')
    parser.add_argument('--port', type=int, default=0, help='the gateway port (default: a free one)')
    parser.add_argument('--detector', choices=hold_listener.DETECTORS,
                        help='the hold listener\'s speech detector, as SMITLINE_HOLD_DETECTOR in '
                             'the line\'s environment (default: unset, which is firered)')
    parser.add_argument('--speech-cache', metavar='DIR',
                        help='the folder of recorded prompts (default: OUT/.speech)')
    args = parser.parse_args(argv)
    unknown = [name for name in args.scenarios if name not in SCENARIOS]
    if unknown:
        parser.error(f'unknown scenario {", ".join(unknown)} (choose from {", ".join(SCENARIOS)})')
    args.scenarios = args.scenarios or list(SCENARIOS)
    return args


async def main(argv=None):
    args = parse_args(argv)
    key = os.environ.get('OPENAI_API_KEY')
    if not key:
        sys.exit('Set OPENAI_API_KEY.')
    Path(args.out).mkdir(parents=True, exist_ok=True)
    env = {'SMITLINE_HOLD_DETECTOR': args.detector} if args.detector else {}
    listener = listener_check(env)
    if listener[0]:
        print(f'The hold listener loads here: {listener[0]}.', flush=True)
    else:
        print(f'The hold listener does not load here ({listener[1]}); calls on hold will keep the '
              'voice on.', flush=True)
    done = []
    async with Sim(key, args.out, args.port, detector=args.detector,
                   speech_cache=args.speech_cache) as sim:
        for name in args.scenarios:
            scenario = SCENARIOS[name]()
            print(f'\n=== {name}: {scenario.about}', flush=True)
            try:
                await sim.run(scenario)
            except Exception as error:
                scenario.errors.append(repr(error))
                scenario.log(f'the scenario failed: {error!r}')
            done.append(scenario)
            summary(done, args.out, detector=args.detector, listener=listener)
        if sim.speech.made_seconds:
            print(f'\nText to speech made {sim.speech.made_seconds:.0f} s of new prompts.', flush=True)
    for line in summary(done, args.out, detector=args.detector, listener=listener):
        if line.startswith(('## ', '- PASS', '- CHECK', '- Outcome', '- Line', '- OpenAI')):
            print(line)


if __name__ == '__main__':
    asyncio.run(main())
