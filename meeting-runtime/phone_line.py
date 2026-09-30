"""Phone calls: Twilio Media Streams on one side, a GPT-Live session on the other.

Twilio sends and receives G.711 mu-law at 8 kHz, which GPT-Live accepts
directly (audio/pcmu), so audio passes through without conversion. GPT-Live
owns turn-taking and barge-in. This module only paces output so that an
interruption leaves little buffered speech, watches for the end of the call,
and turns backend tool calls into telephony actions.
"""
import asyncio
import base64
import collections
import json
import os
import secrets
import time

from call_brief import CallBrief, default_voice, normalize_phone, validate_webhook_url
from barge_in import SpeechDetector
from call_costs import provider_price_from
from call_hooks import CallRefused
from call_hooks import LineNotReady as NotReady
from phone_prompts import (
    DEFAULT_BACKEND_MODEL, HANGUP_YIELDED, delegation_config, disclosure_reminder, discloses, greeting_name, machine_hint,
    opening_cue, voice_instructions,
)
from briefing import backend_background, contact_for, voice_input, voice_notes
from live_sip import LiveSideband, LiveSipClient, LiveSipError, openai_sip_uri, sip_session
from twilio_client import client_for, dial_twiml, sip_dial_twiml, stream_twiml
from voice_core import (
    PCMU8, LiveSession, backend_usage_from, function_call_from, session_config,
)


# The provider keeps this much speech buffered, so network jitter does not chop it; a
# barge-in clears it at once.
OUTPUT_LEAD_SECONDS = 0.6
# When nothing is left playing, speech starts only once this much of it has arrived (or has
# waited this long), so the provider holds a reserve from the first word: recordings showed
# 20 ms silent slots where GPT-Live's audio arrived just in time.
OUTPUT_CUSHION_SECONDS = 0.12
# Local barge-in (barge_in.py): speech that starts while this much of the assistant's is
# still unheard pauses it; once the other person has talked this long it is an
# interruption and the rest is dropped. Model audio arriving this soon after the drop is
# the abandoned rest of it.
BARGE_MIN_QUEUED = 0.15
BARGE_COMMIT_MS = 450
BARGE_STALE_SECONDS = 1.0
# Speech this long is a turn; the reply delay is measured from its end.
TURN_MIN_MS = 300
DRAIN_TIMEOUT = 8.0
# The opening starts when the other person has finished their hello (heard locally), or
# after this long if they say nothing.
OPENING_DELAY = 1.5
SILENCE_PROMPT_SECONDS = 40.0
SILENCE_END_SECONDS = 65.0
WRAP_UP_SECONDS = 60.0
RING_SECONDS = 30
# How long to wait for the media stream or a final status after dialing, before
# asking Twilio directly. Covers the ring time plus a lost callback.
CONNECT_TIMEOUT = RING_SECONDS + 45.0
LIVE_CONNECT_TIMEOUT = 20.0
# Extra time past maxMinutes before a connected call is ended regardless.
OVERRUN_SECONDS = 180.0
# Characters of the agent's first utterance to hear before judging the disclosure.
DISCLOSURE_WINDOW = 120
# A finished utterance this short (for example "Hi," cut off by a call screener) is
# not judged on its own; the next words are added to it.
DISCLOSURE_MIN_FINAL = 40
# GPT-Live stops talking when interrupted but sends no event for it. If the other person
# talks for this long while speech is still queued, and no new audio arrives for
# INTERRUPT_SETTLE seconds, it has yielded: the queued rest is dropped.
INTERRUPT_MIN_SPEECH_MS = 500
INTERRUPT_MIN_QUEUED = 0.25
INTERRUPT_SETTLE = 0.35
# How many times a hang-up may give way to the other person still talking.
MAX_HANGUP_YIELDS = 2
# On direct SIP the model's speech plays at OpenAI; its reflected copy going quiet for
# this long means the goodbye has finished.
SIP_SPEECH_TAIL = 1.2
TWILIO_TERMINAL = {
    'completed': 'hangup', 'busy': 'busy', 'no-answer': 'no_answer', 'failed': 'error',
    'canceled': 'canceled',
}


class UtteranceJoiner:
    """Join GPT-Live transcript fragments into readable lines per speaker."""

    def __init__(self, gap_ms=1500):
        self.gap_ms = gap_ms
        self.speaker = None
        self.parts = []
        self.last_end = None

    def add(self, speaker, text, start_ms=None, end_ms=None):
        flushed = []
        gap = (start_ms is not None and self.last_end is not None
               and start_ms - self.last_end > self.gap_ms)
        if self.parts and (speaker != self.speaker or gap):
            flushed = self.flush()
        self.speaker = speaker
        previous = self.parts[-1] if self.parts else ''
        if previous and text and previous[-1] in '.?!,;:' and text[0].isalnum():
            text = ' ' + text  # "Let me check." + "I can" must not read "check.I can"
        self.parts.append(text)
        if end_ms is not None:
            self.last_end = end_ms
        return flushed

    def flush(self):
        text = ''.join(self.parts).strip()
        speaker = self.speaker
        self.parts = []
        return [(speaker, ' '.join(text.split()))] if text else []


class ProviderClock:
    """The phone provider's audio clock, recovered from the timestamps on the audio it sends.

    Pacing by this computer's clock fails when that clock runs at the wrong rate: under WSL2
    the monotonic clock measured 6% slow, so speech went out slower than the provider played
    it and its buffer ran dry one silent 20 ms frame at a time. Twilio and SignalWire stamp
    every incoming frame with the milliseconds of audio since the stream began. Their time now
    is the stamp of the least delayed recent frame, plus the time since, at the measured rate.
    Until stamps arrive it is this computer's clock.
    """

    WINDOW = 20.0  # seconds of recent frames used for the rate and the reference
    MIN_SPAN = 2.0  # the rate is measured once frames span this long

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.rate = 1.0
        self._frames = collections.deque()  # (our time, their time), both in seconds
        self._offset = None

    def observe(self, stamp_ms):
        try:
            theirs = int(stamp_ms) / 1000
        except (TypeError, ValueError):
            return
        ours = self.clock()
        frames = self._frames
        frames.append((ours, theirs))
        while len(frames) > 2 and frames[0][0] < ours - self.WINDOW:
            frames.popleft()
        first_ours, first_theirs = frames[0]
        if ours - first_ours >= self.MIN_SPAN and theirs > first_theirs:
            self.rate = min(1.25, max(0.8, (theirs - first_theirs) / (ours - first_ours)))
        # A frame that arrived late would put their time behind; the earliest arrival is truest.
        self._offset = max(theirs_at - ours_at * self.rate for ours_at, theirs_at in frames)

    def __call__(self):
        now = self.clock()
        if self._offset is None:
            return now
        return self._offset + now * self.rate


class OutputPacer:
    """Send model audio to the provider no more than a short lead ahead of playback.

    Audio goes out in whole 20 ms frames, so what the provider has buffered never exceeds the
    lead plus one frame, whatever size GPT-Live's deltas are; a delta's odd tail waits for the
    next one, or is padded with silence when speech stops. A recorded call showed SignalWire
    playing a 20 ms silent slot for every mark, so a mark goes out only when speech stops,
    where that slot falls in a pause. When the other person starts
    talking, pause() clears the provider and keeps what they have not heard yet: resume()
    carries on from there after a short "mhm", and flush() drops it after a real interruption.
    """

    FRAME_BYTES = 160  # 20 ms of 8 kHz mu-law
    SILENCE = b'\xff'  # mu-law zero
    # How long a delta's odd tail waits for the next delta before it is padded and sent.
    TAIL_WAIT = 0.06
    # A late frame this far past the end of playback left the listener a gap; a longer quiet
    # is a pause between turns, not a gap.
    GAP_MIN = 0.01
    GAP_MAX = 1.0

    def __init__(self, send, *, lead=OUTPUT_LEAD_SECONDS, cushion=OUTPUT_CUSHION_SECONDS,
                 clock=time.monotonic, sleep=asyncio.sleep, bytes_per_second=8000, trace=None):
        self._send = send
        self._trace = trace or (lambda kind, **data: None)
        self.lead = lead
        self.cushion = cushion
        self.clock = clock
        self.sleep = sleep
        self.bytes_per_second = bytes_per_second
        self.queue = collections.deque()
        self.queued_bytes = 0
        self.max_unplayed = 0.0
        self.play_until = 0.0
        self.sent_marks = 0
        self.played_marks = 0
        self.paused = False
        self.gaps = {'count': 0, 'totalMs': 0, 'starved': 0}
        self._sent = collections.deque()  # (payload, last, starts playing at)
        self._next = None  # the frame run() holds while waiting to send it
        self._tail = b''  # a delta's last bytes, short of a whole frame
        self._ready = asyncio.Event()
        self._continuous = False  # the next frame continues speech already playing
        self._drained = asyncio.Event()
        self._drained.set()

    def offer(self, payload):
        self._drained.clear()
        audio = base64.b64decode(payload)
        self.queued_bytes += len(audio)
        audio = self._tail + audio
        whole = len(audio) - len(audio) % self.FRAME_BYTES
        self._tail = audio[whole:]
        for start in range(0, whole, self.FRAME_BYTES):
            self._queue_frame(audio[start:start + self.FRAME_BYTES])
        self.max_unplayed = max(self.max_unplayed, self.unplayed_seconds())
        if not self.paused:
            self._ready.set()

    def _queue_frame(self, frame):
        self.queue.append((base64.b64encode(frame).decode('ascii'), False))

    def _pad_tail(self):
        """Speech stopped mid-frame: send the rest of the frame as silence."""
        frame = self._tail + self.SILENCE * (self.FRAME_BYTES - len(self._tail))
        self.queued_bytes += self.FRAME_BYTES - len(self._tail)
        self._tail = b''
        self._queue_frame(frame)

    def unplayed_seconds(self):
        """Speech not heard yet: queued here, plus sent to the provider but still playing."""
        return self.queued_bytes / self.bytes_per_second + max(0.0, self.play_until - self.clock())

    def playing(self):
        return not self.paused and self.play_until > self.clock()

    def pause(self):
        """Stop sending and take back what the provider has not played; the caller clears it."""
        now = self.clock()
        unheard = [(payload, last) for payload, last, starts in self._sent if starts >= now]
        if self._next is not None:
            unheard.append(self._next)
            self._next = None
        for payload, last in reversed(unheard):
            self.queue.appendleft((payload, last))
            self.queued_bytes += len(base64.b64decode(payload))
        self._sent.clear()
        self._trace('pause', unheard=len(unheard))
        self.play_until = now
        self.paused = True
        self._continuous = False
        self._ready.clear()

    def resume(self):
        self._trace('resume', queued=len(self.queue))
        self.paused = False
        if self.queue:
            self._ready.set()

    def flush(self):
        """Drop speech the model has stopped saying; the caller also clears the provider."""
        self._trace('flush', dropped=len(self.queue))
        self.queue.clear()
        self._sent.clear()
        self._next = None
        self._tail = b''
        self.queued_bytes = 0
        self.play_until = self.clock()
        self.played_marks = self.sent_marks
        self.paused = False
        self._continuous = False
        self._ready.clear()
        self._drained.set()

    def mark_played(self, name):
        try:
            self.played_marks = max(self.played_marks, int(str(name).rsplit('-', 1)[-1]))
        except ValueError:
            return
        if (self.played_marks >= self.sent_marks and not self.queue and not self._tail
                and not self.paused):
            self._drained.set()

    async def drained(self, timeout=DRAIN_TIMEOUT):
        try:
            await asyncio.wait_for(self._drained.wait(), timeout)
            return True
        except asyncio.TimeoutError:
            return False

    async def _cushioned(self, size):
        """Nothing is playing: wait until `cushion` seconds have arrived, or for that long.

        False when a pause or flush took the held frame meanwhile.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.cushion
        while self.queued_bytes / self.bytes_per_second < self.cushion:
            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            self._ready.clear()
            try:
                await asyncio.wait_for(self._ready.wait(), remaining)
            except asyncio.TimeoutError:
                break
            if self._next is None:
                return False
        return self._next is not None

    async def run(self):
        while True:
            starved = False
            while self.paused or not self.queue:
                starved = starved or not self.paused
                self._ready.clear()
                if self._tail and not self.paused:
                    try:
                        await asyncio.wait_for(self._ready.wait(), self.TAIL_WAIT)
                    except asyncio.TimeoutError:
                        self._pad_tail()
                    continue
                await self._ready.wait()
            payload, last = self._next = self.queue.popleft()
            size = len(base64.b64decode(payload))
            if self.play_until <= self.clock() and not await self._cushioned(size):
                continue  # paused or flushed while gathering the cushion
            self.queued_bytes = max(0, self.queued_bytes - size)
            seconds = size / self.bytes_per_second
            now = self.clock()
            ahead = self.play_until - now
            if ahead > self.lead:
                await self.sleep((ahead - self.lead) / getattr(self.clock, 'rate', 1.0))
                if self._next is None:  # paused or flushed meanwhile, which took this frame
                    continue
                now = self.clock()
            self._next = None
            late = now - self.play_until
            if self._continuous and self.GAP_MIN <= late <= self.GAP_MAX:
                self.gaps['count'] += 1
                self.gaps['totalMs'] += int(late * 1000)
                self.gaps['starved'] += int(starved)
            await self._send({'event': 'media', 'media': {'payload': payload}})
            starts = max(now, self.play_until)
            self._trace('send', ahead=round(starts - now, 4), queued=len(self.queue))
            self._sent.append((payload, last, starts))
            while self._sent and self._sent[0][2] + 0.02 < now:
                self._sent.popleft()
            if not self.queue and not self._tail:
                # Speech has stopped for now: one mark says when the provider finished playing it.
                self.sent_marks += 1
                await self._send({'event': 'mark', 'mark': {'name': f'out-{self.sent_marks}'}})
                self._trace('mark')
            self.play_until = starts + seconds
            self._continuous = True


def _error_code(error):
    return getattr(error, 'code', None) or type(error).__name__


class PhoneSession:
    """One phone call bridged to GPT-Live."""

    def __init__(self, line, ctx, *, api_key, twilio, from_number, token, inbound=False,
                 recording=False, owner_phone=None):
        self.line = line
        self.ctx = ctx
        self.brief = ctx.brief
        self.api_key = api_key
        self.twilio = twilio
        self.from_number = from_number
        self.token = token
        self.inbound = inbound
        self.recording = recording
        self.owner_phone = owner_phone
        self.call_sid = None
        self.stream_sid = None
        self.ws = None
        self.live = None
        self.pacer = None
        self.joiner = UtteranceJoiner()
        self.connected = asyncio.Event()
        self.finished = asyncio.Event()
        self.canceled = False
        self.end_reason = None
        self.error_detail = None
        self.phone_seconds = None
        self.backend_tokens = {'input': 0, 'cached': 0, 'output': 0, 'webSearches': 0}
        self.backend_model = None
        self.disclosure_checked = False
        self.disclosure_attempts = 0
        self.greet_name = None  # the person's name for the hello, when the profile or brief knows it
        self._agent_opening = ''
        self.heard_other = asyncio.Event()
        self.last_output_at = None
        self._other_spoke_at = None
        self._other_span = None  # [start_ms, end_ms] of what the other person is saying now
        self._awaiting_reply_since = None
        self._interrupt_check = None
        self._hangup_yields = 0
        self.stats = {'interruptionsFollowed': 0, 'hangupsYielded': 0, 'responseDelaysMs': [],
                      'pauses': 0, 'backchannels': 0, 'stopDelaysMs': []}
        self.detector = SpeechDetector()
        self._hello_heard = asyncio.Event()  # they finished their first words (heard locally)
        self._trace = None  # COLLEAGUE_AUDIO_TRACE=1: timing of audio events, for diagnosis
        self._trace_start = None
        self._barge = None  # 'paused' while deciding, 'dropped' after an interruption
        self._dropped_at = None
        self.last_activity = None
        self.started_at = None
        self._hanging_up = False
        self._closing_stream = False
        self._tasks = set()
        self._hangups = set()
        self._pending_hangup = None
        self._accept_done = asyncio.Event()
        self._accepted = None

    # Configuration -------------------------------------------------------

    def config(self):
        """The goal leads (voice instructions); background is reference (starting context for
        the voice, in full for the backend). See briefing.py."""
        env = self.line.environ()
        profile = self.ctx.profile()
        # The brief can name the person; otherwise the profile may know the number.
        contact = self.brief.contact or contact_for(profile, self.brief.to)
        self.greet_name = greeting_name(contact)
        session = self.brief.session_context
        owner = self.brief.on_behalf_of
        notes = voice_notes(profile, contact, session, owner)
        self.backend_model = env.get('COLLEAGUE_PHONE_BACKEND_MODEL') or DEFAULT_BACKEND_MODEL
        return session_config(
            instructions=voice_instructions(self.brief, inbound=self.inbound,
                                            recording=self.recording, contact=contact,
                                            has_notes=bool(notes),
                                            boundaries=(profile or {}).get('boundaries') or ()),
            audio_format=PCMU8,
            voice=self.brief.voice or default_voice(env),
            delegation=delegation_config(
                self.brief, model=self.backend_model,
                web_search=env.get('COLLEAGUE_PHONE_WEB_SEARCH') == '1', inbound=self.inbound,
                background=backend_background(profile, contact, session, owner)),
            seed_input=voice_input(notes),
        )

    # Bookkeeping ---------------------------------------------------------

    def _spawn(self, coroutine):
        task = asyncio.create_task(coroutine)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    def _start_hangup(self, reason, *, yield_to_speech=False):
        task = asyncio.create_task(self.hangup(reason, yield_to_speech=yield_to_speech))
        self._hangups.add(task)
        task.add_done_callback(self._hangups.discard)
        return task

    def fail(self, detail):
        """Record why the call broke; the result says so instead of 'nobody spoke'."""
        if self.end_reason in (None, 'hangup', 'remote_hangup'):
            self.end_reason = 'error'
        if self.error_detail is None:
            self.error_detail = detail
            self.ctx.event('call.error', detail=detail)

    # Twilio side ---------------------------------------------------------

    async def _twilio_send(self, message):
        if self.ws is None or self._closing_stream:
            return
        message = dict(message, streamSid=self.stream_sid)
        try:
            await self.ws.send_str(json.dumps(message))
        except Exception:
            pass

    async def run(self, ws, start):
        """Bridge until the call ends; `start` is Twilio's start event."""
        self.ws = ws
        self.stream_sid = start.get('streamSid')
        self.call_sid = self.call_sid or start.get('callSid')
        self.started_at = time.monotonic()
        self.last_activity = self.started_at
        self.connected.set()
        self.ctx.set_status('in_progress')
        if self.line.environ().get('COLLEAGUE_AUDIO_TRACE') == '1':
            self._trace, self._trace_start = [], time.monotonic()
        # The provider's clock paces the assistant's speech: this computer's may run fast or slow.
        self.provider_clock = ProviderClock()
        self.pacer = OutputPacer(self._twilio_send, clock=self.provider_clock, trace=self._note)
        manager = self.line.live_factory(self.api_key, self.config())
        try:
            try:
                live = await asyncio.wait_for(manager.__aenter__(), LIVE_CONNECT_TIMEOUT)
            except Exception as error:
                self.fail(f'The voice session could not start ({_error_code(error)}).')
                await self.hangup('error')
                return
            try:
                await self._bridge(live)
            finally:
                await manager.__aexit__(None, None, None)
        finally:
            for speaker, text in self.joiner.flush():
                self.ctx.add_transcript(speaker, text)
                if speaker == 'agent':
                    self._check_disclosure(final=True)
            for task in list(self._tasks):
                task.cancel()
            if self._hangups:
                await asyncio.wait(list(self._hangups), timeout=5)
            self._write_trace()
            self.finished.set()

    async def _bridge(self, live):
        self.live = live
        reader = asyncio.create_task(self._read_live(), name='live-events')
        ending = [
            asyncio.create_task(self._read_twilio(), name='twilio-in'),
            asyncio.create_task(self._watch(), name='call-watchdog'),
            reader,
        ]
        helpers = [
            asyncio.create_task(self.pacer.run(), name='twilio-out'),
            asyncio.create_task(self._open(), name='call-opening'),
        ]
        done, _pending = await asyncio.wait(ending, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            if not task.cancelled() and task.exception():
                self.fail(f'The call broke ({_error_code(task.exception())}).')
        if not reader.done():
            # Close gracefully so session.closed reports final usage.
            await live.close()
            try:
                await asyncio.wait_for(asyncio.shield(reader), 5.0)
            except (asyncio.TimeoutError, Exception):
                pass
        for task in ending + helpers:
            task.cancel()
        await asyncio.gather(*ending, *helpers, return_exceptions=True)
        await self._close_stream()
        if self.error_detail and self.call_sid and not self._hanging_up:
            # The voice side failed while the caller was still on the line.
            await self.hangup('error')

    async def _read_twilio(self):
        from aiohttp import WSMsgType
        async for message in self.ws:
            if message.type != WSMsgType.TEXT:
                if message.type in (WSMsgType.CLOSE, WSMsgType.CLOSING, WSMsgType.CLOSED,
                                    WSMsgType.ERROR):
                    break
                continue
            try:
                data = json.loads(message.data)
            except ValueError:
                continue
            event = data.get('event')
            if event == 'media':
                media = data.get('media') or {}
                if media.get('track', 'inbound') == 'inbound' and media.get('payload'):
                    self.provider_clock.observe(media.get('timestamp'))
                    if self._trace is not None:
                        self._note('in', ts=media.get('timestamp'), seq=data.get('sequenceNumber'))
                    await self.live.send_audio(media['payload'])
                    await self._hear(media['payload'])
            elif event == 'mark':
                self.pacer.mark_played((data.get('mark') or {}).get('name'))
            elif event == 'stop':
                break
        if self.end_reason is None:
            self.end_reason = 'remote_hangup'

    def _note(self, kind, **data):
        if self._trace is not None:
            self._trace.append({'t': round(time.monotonic() - self._trace_start, 4),
                                'kind': kind, **data})

    def _write_trace(self):
        """Save the trace next to the call record as audio-trace.jsonl (owner-only)."""
        if not self._trace:
            return
        try:
            path = self.ctx.service.store.root / self.ctx.call_id / 'audio-trace.jsonl'
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, 'w', encoding='utf-8') as handle:
                handle.writelines(json.dumps(entry) + '\n' for entry in self._trace)
        except (AttributeError, OSError):
            pass

    async def _hear(self, payload):
        """Stop talking the moment the other person does (see barge_in.py)."""
        if self.pacer is None:
            return
        try:
            frame = base64.b64decode(payload)
        except ValueError:
            return
        now = time.monotonic()
        change = self.detector.feed(frame, playing=self.pacer.playing())
        if change:
            self._note('vad_' + change)
        if change == 'end':
            self._hello_heard.set()
        if change == 'start':
            if (self._barge is None and not self._hanging_up
                    and self.pacer.unplayed_seconds() >= BARGE_MIN_QUEUED):
                self.pacer.pause()
                await self._twilio_send({'event': 'clear'})
                self._barge = 'paused'
                self.stats['pauses'] += 1
                self.stats['stopDelaysMs'].append(self.detector.speech_ms)
        elif change == 'end':
            if self._barge == 'paused':  # a short "mhm": carry on where it paused
                self.pacer.resume()
                self.stats['backchannels'] += 1
            self._barge = None
            if self.detector.last_speech_ms >= TURN_MIN_MS:
                # The reply delay runs from their last word, not from when the quiet after it
                # was long enough to call the turn over.
                self._awaiting_reply_since = now - self.detector.end_ms / 1000
        elif self._barge == 'paused' and self.detector.speech_ms >= BARGE_COMMIT_MS:
            self.pacer.flush()
            self._barge = 'dropped'
            self._dropped_at = now
            self.stats['interruptionsFollowed'] += 1
            self.ctx.event('call.interrupted')

    # GPT-Live side -------------------------------------------------------

    async def _read_live(self):
        async for event in self.live.events():
            if event.get('type') == 'session.output_audio.delta':
                self._output_audio(event)
            elif self._handle_live_event(event):
                return
        self._live_ended()

    def _output_audio(self, event):
        now = time.monotonic()
        self.last_activity = self.last_output_at = now
        self._note('delta', bytes=len(event.get('delta') or '') * 3 // 4)
        if (self._barge == 'dropped' and self._dropped_at is not None
                and now - self._dropped_at < BARGE_STALE_SECONDS):
            return  # the rest of what they interrupted, still arriving
        if self._awaiting_reply_since is not None:
            self.stats['responseDelaysMs'].append(int((now - self._awaiting_reply_since) * 1000))
            self._awaiting_reply_since = None
        if self.pacer is not None:
            self.pacer.offer(event['delta'])

    def _handle_live_event(self, event):
        """Events every transport handles the same way; True once the session has closed."""
        kind = event.get('type')
        if kind in ('session.input_transcript.delta', 'session.output_transcript.delta'):
            self._transcript(event)
        elif kind == 'response.event':
            usage = backend_usage_from(event)
            if usage:
                for key, value in usage.items():
                    self.backend_tokens[key] = self.backend_tokens.get(key, 0) + value
            call = function_call_from(event)
            if call and call['name'] == 'end_call':
                # Hanging up needs no tool result or further backend turn.
                reason = call['arguments'].get('reason')
                self.ctx.event('call.end_requested', reason=reason or 'completed')
                self._start_hangup('voicemail' if reason == 'voicemail_left' else 'hangup',
                                   yield_to_speech=reason != 'voicemail_left')
        elif kind == 'error':
            error = event.get('error') or {}
            self.ctx.event('call.voice_error', code=error.get('code') or 'live_error')
        elif kind == 'session.closed':
            reason = event.get('reason')
            if reason in ('content', 'connection_lost', 'expired'):
                self.fail(f'The voice session closed ({reason}).')
            elif reason == 'remote_hangup':
                self.end_reason = self.end_reason or 'remote_hangup'
            elif self.end_reason is None:
                self.end_reason = 'hangup'
            return True
        return False

    def _live_ended(self):
        if self.end_reason is None:
            self.ctx.event('call.voice_error', code='connection_lost')
            self.fail('The voice connection was lost during the call.')

    def _transcript(self, event):
        now = self.last_activity = time.monotonic()
        source = 'other' if 'input_transcript' in event['type'] else 'agent'
        delta = event.get('delta') or ''
        if source == 'other' and delta.strip():
            self.heard_other.set()
            self._other_spoke_at = now
            start, end = event.get('start_ms'), event.get('end_ms')
            if self._other_span is None or self.joiner.speaker != 'other':
                self._other_span = [start, end]
            elif end is not None:
                self._other_span[1] = end
            self._maybe_follow_interruption(now)
        if source == 'agent' and not self.disclosure_checked:
            self._agent_opening += delta
        for speaker, text in self.joiner.add(source, delta, event.get('start_ms'), event.get('end_ms')):
            self.ctx.add_transcript(speaker, text)
            if speaker == 'agent':
                self._check_disclosure(final=True)
        if source == 'agent' and self.joiner.speaker == 'agent':
            self._check_disclosure()

    def _maybe_follow_interruption(self, spoke_at):
        """Stop playing when GPT-Live has stopped talking because the other person interrupted.

        GPT-Live owns barge-in but sends no event for it, and it produces speech faster
        than it is played, so speech it has abandoned can still be queued. When the other
        person has talked for a moment and no new audio follows, the rest is dropped here
        and at the provider. A short backchannel ("mhm") does not count.
        """
        if self._barge is not None:
            return  # the local detector is already handling it
        if self.pacer is None or self.pacer.unplayed_seconds() < INTERRUPT_MIN_QUEUED:
            return
        span = self._other_span or [None, None]
        if span[0] is not None and span[1] is not None and span[1] - span[0] < INTERRUPT_MIN_SPEECH_MS:
            return
        if self._interrupt_check is not None and not self._interrupt_check.done():
            return
        self._interrupt_check = self._spawn(self._follow_interruption(spoke_at))

    async def _follow_interruption(self, spoke_at):
        await asyncio.sleep(INTERRUPT_SETTLE)
        if self.last_output_at is not None and self.last_output_at > spoke_at:
            return  # still talking: GPT-Live chose to continue
        if self.pacer is None or self.pacer.unplayed_seconds() < INTERRUPT_MIN_QUEUED:
            return
        self.pacer.flush()
        await self._twilio_send({'event': 'clear'})
        self.stats['interruptionsFollowed'] += 1
        self.ctx.event('call.interrupted')

    def audio_stats(self):
        def spread(values):
            values = sorted(values)
            return {'median': values[len(values) // 2], 'max': values[-1], 'count': len(values)}
        stats = {
            'maxUnplayedMs': int((self.pacer.max_unplayed if self.pacer else 0) * 1000),
            'interruptionsFollowed': self.stats['interruptionsFollowed'],
            'hangupsYielded': self.stats['hangupsYielded'],
            'pauses': self.stats['pauses'],
            'backchannels': self.stats['backchannels'],
        }
        if self.pacer is not None:
            stats['gaps'] = dict(self.pacer.gaps)
            # The provider's clock against this computer's; far from 1 means a drifting clock.
            stats['clockRate'] = round(self.provider_clock.rate, 4)
        if self.stats['responseDelaysMs']:
            stats['replyDelayMs'] = spread(self.stats['responseDelaysMs'])
        if self.stats['stopDelaysMs']:
            # Their speech heard before playback paused; the provider stops within one frame.
            stats['stopDelayMs'] = spread(self.stats['stopDelaysMs'])
        return stats

    def _check_disclosure(self, *, final=False):
        """Check, early in the agent's speech, that it said it is an AI acting for NAME.

        GPT-Live cannot be forced to say a fixed sentence, so the opening is checked
        as it is spoken, over everything the agent has said so far: a call screener or
        a quick "hello" can split the opening, and a fragment is not a miss. A miss
        triggers an instruction to disclose at once, and what the agent says next is
        checked again. The verdict goes into the result.
        """
        if self.disclosure_checked or self.inbound:
            return
        text = ' '.join(self._agent_opening.split())
        verified = discloses(text, self.brief.on_behalf_of, self.brief.language)
        if not verified and len(text) < (DISCLOSURE_MIN_FINAL if final else DISCLOSURE_WINDOW):
            return
        self.disclosure_attempts += 1
        self.ctx.disclosure = verified
        self.ctx.event('call.disclosure', verified=verified, attempt=self.disclosure_attempts)
        if verified or self.disclosure_attempts >= 2:
            self.disclosure_checked = True
            return
        self._agent_opening = ''  # judge what is said after the reminder
        if self.live is not None:
            self._spawn(self.live.append(
                'session.instructions.append', disclosure_reminder(self.brief)))

    async def _open(self):
        """Prompt the opening once the other side speaks, or after a short pause."""
        heard = [asyncio.ensure_future(self.heard_other.wait()),
                 asyncio.ensure_future(self._hello_heard.wait())]
        await asyncio.wait(heard, timeout=OPENING_DELAY, return_when=asyncio.FIRST_COMPLETED)
        for waiter in heard:
            waiter.cancel()
        await self.live.started.wait()
        await self.live.append('session.commentary.append',
                               opening_cue(self.brief, inbound=self.inbound, name=self.greet_name))
        self._note('opening_cue')
        await asyncio.Event().wait()

    async def _watch(self):
        limit = self.brief.max_minutes * 60
        warned = prompted = False
        while True:
            await asyncio.sleep(1)
            now = time.monotonic()
            elapsed = now - self.started_at
            if not warned and elapsed >= max(limit - WRAP_UP_SECONDS, limit * 0.8):
                warned = True
                await self.live.append('session.commentary.append',
                                       'Time is almost up. Wrap up politely and say goodbye.')
            if elapsed >= limit:
                await self.hangup('max_duration')
                return
            quiet = now - self.last_activity
            if not prompted and quiet >= SILENCE_PROMPT_SECONDS:
                prompted = True
                await self.live.append('session.commentary.append',
                                       'Nobody has spoken for a while. Ask if they are still there.')
            elif quiet < SILENCE_PROMPT_SECONDS:
                prompted = False
            if quiet >= SILENCE_END_SECONDS:
                await self.hangup('hangup')
                return

    # Actions -------------------------------------------------------------

    async def hangup(self, reason, *, yield_to_speech=False):
        if self._hanging_up:
            return
        self._hanging_up = True
        requested_at = time.monotonic()
        set_reason = self.end_reason is None
        if set_reason:
            self.end_reason = reason
        await self._let_speech_finish()
        if (yield_to_speech and self._hangup_yields < MAX_HANGUP_YIELDS
                and self._other_spoke_at is not None and self._other_spoke_at > requested_at
                and not self.finished.is_set()):
            # They kept talking after the goodbye: stay on the line and let the model answer.
            self._hanging_up = False
            self._hangup_yields += 1
            self.stats['hangupsYielded'] += 1
            if set_reason:
                self.end_reason = None
            self.ctx.event('call.hangup_yielded')
            if self.live is not None:
                await self.live.append('session.instructions.append', HANGUP_YIELDED)
            return
        await self._end_call()

    async def _let_speech_finish(self):
        if self.pacer is not None:
            await self.pacer.drained()

    async def _end_call(self):
        await self._close_stream()
        if self.call_sid:
            try:
                await self.twilio.update_call(self.call_sid, status='completed')
            except Exception:
                pass

    async def _close_stream(self):
        self._closing_stream = True
        if self.ws is not None:
            try:
                await self.ws.close()
            except Exception:
                pass

    async def machine_suspected(self, answered_by):
        """The network guessed that a machine answered. It mistakes call screeners for
        voicemail, so the model, which hears the call, decides; it ends the call with
        the reason voicemail_left only after leaving a message."""
        self.ctx.event('call.machine_suspected', answeredBy=answered_by)
        if self.live is not None:
            # Silent context: an instruction here made the model announce "I'll wait for the beep".
            await self.live.append('session.thinking.append', machine_hint(self.brief))

    async def instruct(self, text, *, silent=False):
        """Guidance acts now; a silent note is context the model uses when it helps."""
        if self.live is None:
            return False
        kind = 'session.thinking.append' if silent else 'session.instructions.append'
        return await self.live.append(kind, text)

    async def wrap_up(self):
        if self.live is None:
            return False
        await self.live.append('session.commentary.append',
                               'You need to end the call now. Thank them and say goodbye.')
        if self._pending_hangup is None:
            self._pending_hangup = self._spawn(self._delayed_hangup('hangup', 12.0))
        return True

    async def _delayed_hangup(self, reason, seconds):
        await asyncio.sleep(seconds)
        await self.hangup(reason)

    async def transfer(self):
        if not self.owner_phone:
            raise NotReady('Set COLLEAGUE_OWNER_PHONE to transfer calls to your phone.')
        if self._pending_hangup is not None:
            self._pending_hangup.cancel()
            self._pending_hangup = None
        if self.live is not None:
            await self.live.append(
                'session.commentary.append',
                f'Tell them briefly that you are connecting them with {self.brief.on_behalf_of} now.')
        await asyncio.sleep(1.0)
        await self._let_speech_finish()
        await self._hand_over()
        self.end_reason = 'transferred'
        self.ctx.event('call.transferred')
        return {'transferred': True}

    async def _hand_over(self):
        """Redirect the provider's call leg to the owner's phone."""
        fallback = (f'Sorry, I could not reach {self.brief.on_behalf_of} right now. '
                    'They will get back to you. Goodbye.')
        await self.twilio.update_call(
            self.call_sid, twiml=dial_twiml(self.owner_phone, self.from_number, fallback=fallback))


class SipPhoneSession(PhoneSession):
    """A call whose audio flows between the provider and OpenAI; this side only steers.

    The sideband carries call progress, transcripts, backend tool calls, and commands.
    Everything about the conversation (opening, disclosure check, hang-up rules, time
    limits, results) is shared with the relayed PhoneSession.
    """

    def __init__(self, line, ctx, *, api_key, client, from_number, twilio=None, owner_phone=None,
                 bridged=False):
        super().__init__(line, ctx, api_key=api_key, twilio=twilio, from_number=from_number,
                         token=secrets.token_urlsafe(24), recording=False, owner_phone=owner_phone)
        self.client = client
        self.bridged = bridged
        self.session_id = None
        self.answered_at = None
        self.incoming = asyncio.get_running_loop().create_future()

    def config(self):
        return sip_session(super().config())

    async def run_sip(self, session_id, *, answered=False):
        """Steer the call until the session closes."""
        self.session_id = session_id
        self.ctx.link(liveSessionId=session_id)
        manager = self.line.sideband_factory(self.api_key, session_id)
        try:
            live = await asyncio.wait_for(manager.__aenter__(), LIVE_CONNECT_TIMEOUT)
        except Exception as error:
            self.fail(f'The call control channel could not connect ({_error_code(error)}).')
            await self._end_call()
            self.finished.set()
            return
        self.live = live
        reader = asyncio.create_task(self._read_live(), name='sip-sideband')
        try:
            if answered:
                self._answered()
            else:
                self._spawn(self._ring_watch())
            limit = RING_SECONDS + 30 + self.brief.max_minutes * 60 + OVERRUN_SECONDS
            try:
                await asyncio.wait_for(asyncio.shield(reader), limit)
            except asyncio.TimeoutError:
                self.fail('The call ran past its time limit and was ended.')
                await self._end_call()
                try:
                    await asyncio.wait_for(asyncio.shield(reader), 15)
                except asyncio.TimeoutError:
                    pass
            except Exception as error:
                self.fail(f'The call broke ({_error_code(error)}).')
                await self._end_call()
        finally:
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)
            await manager.__aexit__(None, None, None)
            for speaker, text in self.joiner.flush():
                self.ctx.add_transcript(speaker, text)
                if speaker == 'agent':
                    self._check_disclosure(final=True)
            for task in list(self._tasks):
                task.cancel()
            if self._hangups:
                await asyncio.wait(list(self._hangups), timeout=5)
            if self.answered_at is not None and self.phone_seconds is None:
                self.phone_seconds = int(time.monotonic() - self.answered_at)
            self.finished.set()

    async def _read_live(self):
        async for event in self.live.events():
            kind = event.get('type')
            if kind == 'transport.ringing':
                self.ctx.set_status('ringing')
            elif kind == 'transport.answered':
                self._answered()
            elif kind == 'transport.failed':
                self._transport_failed(event)
            elif kind == 'session.output_audio.delta':
                self._output_audio(event)  # a reflected copy: timing only
            elif kind == 'session.input_audio.append':
                continue  # reflected caller audio
            elif self._handle_live_event(event):
                return
        self._live_ended()

    def _answered(self):
        if self.answered_at is not None:
            return
        now = time.monotonic()
        self.answered_at = self.started_at = self.last_activity = now
        self.connected.set()
        self.ctx.set_status('in_progress')
        self._spawn(self._watch())
        self._spawn(self._open())

    def _transport_failed(self, event):
        error = event.get('error') or {}
        message = str(error.get('message') or error.get('code') or 'the call could not be placed')
        self.ctx.event('call.transport_failed', code=error.get('code') or 'call_error')
        lowered = message.lower()
        if 'busy' in lowered:
            self.end_reason = self.end_reason or 'busy'
        elif any(word in lowered for word in ('no answer', 'no_answer', 'not answered', 'timeout')):
            self.end_reason = self.end_reason or 'no_answer'
        else:
            self.fail(f'The call could not be placed: {message}.')
        self._spawn(self._end_call())

    async def _ring_watch(self):
        await asyncio.sleep(RING_SECONDS + 15)
        if self.answered_at is None:
            self.end_reason = self.end_reason or 'no_answer'
            await self._end_call()

    async def _let_speech_finish(self):
        """OpenAI plays the speech; wait until its reflected copy has gone quiet."""
        deadline = time.monotonic() + DRAIN_TIMEOUT
        while time.monotonic() < deadline:
            if self.last_output_at is None or time.monotonic() - self.last_output_at >= SIP_SPEECH_TAIL:
                return
            await asyncio.sleep(0.1)

    async def _end_call(self):
        if self.session_id:
            try:
                await self.client.hangup(self.session_id)
            except Exception:
                pass
        if self.bridged and self.call_sid:
            try:
                await self.twilio.update_call(self.call_sid, status='completed')
            except Exception:
                pass

    async def _hand_over(self):
        if self.bridged and self.call_sid:
            return await super()._hand_over()
        await self.client.refer(self.session_id, f'tel:{self.owner_phone}')

    def accept_incoming(self, session_id):
        """The provider handed the call to OpenAI: accept it with this call's configuration."""
        if self.incoming.done():
            return False
        self.incoming.set_result(None)  # claimed: webhook retries are ignored from here on
        self._accepting = self._spawn(self._accept(session_id))
        return True

    async def _accept(self, session_id):
        try:
            await self.client.accept(session_id, sip_session(super().config(), accept=True))
        except Exception as error:
            self.fail(f'OpenAI did not take the call ({_error_code(error)}).')
            self._accepted = None
        else:
            self._accepted = session_id
        self._accept_done.set()


class PhoneLine:
    channel = 'phone'

    def __init__(self, *, public_url, environ, twilio_factory=None, live_factory=None,
                 status_grace=5.0, public_available=None, connect_timeout=CONNECT_TIMEOUT,
                 sip_client_factory=None, sideband_factory=None):
        """public_url: async callable returning the https base URL Twilio can reach."""
        self._public_url = public_url
        self._public_available = public_available
        self.environ = environ
        self.twilio_factory = twilio_factory or client_for
        self.live_factory = live_factory or (lambda key, config: LiveSession(key, config))
        self.sip_client_factory = sip_client_factory or (lambda key: LiveSipClient(key))
        self.sideband_factory = sideband_factory or (lambda key, session_id: LiveSideband(key, session_id))
        self.status_grace = status_grace
        self.connect_timeout = connect_timeout
        self.gateway_ready = True
        self.sessions = {}

    def ready(self, hooks, owner, brief):
        hooks.credentials(owner, 'openai')
        hooks.credentials(owner, 'twilio')
        if hooks.credentials(owner, 'sip')['mode'] == 'sip':
            return  # OpenAI dials out: nothing on this computer has to be reachable
        if not self.gateway_ready:
            raise CallRefused('gateway_unavailable', 'The phone gateway could not start; see the '
                              'daemon log. Set COLLEAGUE_GATEWAY_PORT to a free port.')
        if self._public_available is not None and not self._public_available():
            raise CallRefused('no_public_url', 'Twilio cannot reach this computer. Set '
                              'COLLEAGUE_PUBLIC_URL, or install cloudflared or Docker for a '
                              'quick tunnel.')

    def _recording(self):
        return self.environ().get('COLLEAGUE_RECORD_CALLS') == '1'

    def session(self, call_id):
        return self.sessions.get(call_id)

    def claim(self, call_id, token):
        """Match a Twilio stream to the session waiting for it."""
        session = self.sessions.get(call_id)
        if session is None or isinstance(session, SipPhoneSession):
            return None
        if not secrets.compare_digest(session.token, token or ''):
            return None
        if session.connected.is_set() or session.canceled or session.finished.is_set():
            return None
        return session

    async def start(self, ctx):
        mode = ctx.credentials('sip')['mode']
        if mode == 'sip':
            return await self._start_sip(ctx)
        if mode == 'sip-webhook':
            return await self._start_bridged(ctx)
        return await self._start_relay(ctx)

    def _sip_session(self, ctx, *, bridged=False):
        openai = ctx.credentials('openai')
        phone = ctx.credentials('twilio')
        env = self.environ()
        return SipPhoneSession(
            self, ctx, api_key=openai['apiKey'], client=self.sip_client_factory(openai['apiKey']),
            twilio=self.twilio_factory(phone) if bridged else None,
            from_number=env.get('COLLEAGUE_CALLER_ID') or phone['fromNumber'],
            owner_phone=env.get('COLLEAGUE_OWNER_PHONE'), bridged=bridged)

    async def _start_sip(self, ctx):
        """OpenAI dials out through the provider's SIP trunk; audio never touches this computer."""
        sip = ctx.credentials('sip')
        session = self._sip_session(ctx)
        self.sessions[ctx.call_id] = session
        try:
            ctx.set_status('connecting')
            if session.canceled:
                return await self._finish(ctx, session)
            trunk = {'provider_url': sip['trunkUrl'],
                     'auth': {'type': 'digest', 'username': sip['username'], 'password': sip['password']},
                     'caller_number': session.from_number}
            try:
                session_id = await session.client.create_outbound(
                    session.config(), destination=ctx.brief.to, trunk=trunk)
            except LiveSipError as error:
                if error.code == 'outbound_sip_not_enabled':
                    # Not enabled for this OpenAI organization yet: relay this call instead.
                    ctx.event('call.sip_unavailable', code=error.code)
                    self.sessions.pop(ctx.call_id, None)
                    return await self._start_relay(ctx)
                session.fail(f'OpenAI could not place the call ({error.code}).')
                return await self._finish(ctx, session)
            ctx.link(fromNumber=session.from_number)
            if session.canceled:
                session.session_id = session_id
                await session._end_call()
            await session.run_sip(session_id)
            return await self._finish(ctx, session)
        finally:
            self.sessions.pop(ctx.call_id, None)

    async def _start_bridged(self, ctx):
        """The provider dials the person, then hands the answered call to OpenAI's SIP address."""
        sip = ctx.credentials('sip')
        session = self._sip_session(ctx, bridged=True)
        self.sessions[ctx.call_id] = session
        try:
            ctx.set_status('connecting')
            base = (await self._public_url()).rstrip('/')
            if session.canceled:
                return await self._finish(ctx, session)
            uri = openai_sip_uri(sip['projectId'], {'X-Colleague-Call': ctx.call_id,
                                                    'X-Colleague-Token': session.token})
            created = await session.twilio.create_call(
                to=ctx.brief.to, from_=session.from_number, twiml=sip_dial_twiml(uri),
                status_callback=f'{base}/twilio/status/{ctx.call_id}', timeout=RING_SECONDS,
                time_limit=ctx.brief.max_minutes * 60 + 90)
            session.call_sid = created.get('sid')
            ctx.link(providerCallSid=session.call_sid, fromNumber=session.from_number)
            if session.canceled:
                await self._cancel_remote(session)
                return await self._finish(ctx, session)
            # The webhook route calls on_sip_incoming, which accepts the call.
            waits = [asyncio.create_task(session._accept_done.wait()),
                     asyncio.create_task(session.finished.wait())]
            done, pending = await asyncio.wait(waits, timeout=self.connect_timeout,
                                               return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            if session._accepted:
                await session.run_sip(session._accepted, answered=True)
            elif not done:
                await self._resolve_silent_call(session)
            else:
                session.finished.set()
            return await self._finish(ctx, session)
        finally:
            self.sessions.pop(ctx.call_id, None)

    def on_sip_incoming(self, session_id, headers):
        """OpenAI announced a call handed over by the provider; True if it is ours to accept."""
        session = self.sessions.get(headers.get('X-Colleague-Call') or '')
        if not isinstance(session, SipPhoneSession) or not session.bridged:
            return False
        if not secrets.compare_digest(session.token, headers.get('X-Colleague-Token') or ''):
            return False
        if session.incoming.done():
            return True  # a retried webhook for a call already claimed
        return session.accept_incoming(session_id)

    async def _start_relay(self, ctx):
        openai = ctx.credentials('openai')
        twilio_creds = ctx.credentials('twilio')
        env = self.environ()
        twilio = self.twilio_factory(twilio_creds)
        session = PhoneSession(
            self, ctx, api_key=openai['apiKey'], twilio=twilio,
            from_number=env.get('COLLEAGUE_CALLER_ID') or twilio_creds['fromNumber'],
            token=secrets.token_urlsafe(24), recording=self._recording(),
            owner_phone=env.get('COLLEAGUE_OWNER_PHONE'))
        self.sessions[ctx.call_id] = session
        try:
            ctx.set_status('connecting')
            base = (await self._public_url()).rstrip('/')
            if session.canceled:
                return await self._finish(ctx, session)
            stream_url = 'wss://' + base.split('://', 1)[1] + '/twilio/media'
            # COLLEAGUE_STREAM_REALTIME=1 lets SignalWire's player smooth packet delays and bursts.
            realtime = (getattr(twilio, 'flavor', 'twilio') == 'signalwire'
                        and self.environ().get('COLLEAGUE_STREAM_REALTIME') == '1')
            twiml = stream_twiml(stream_url, {'callId': ctx.call_id, 'token': session.token},
                                 realtime=realtime)
            created = await twilio.create_call(
                to=ctx.brief.to, from_=session.from_number, twiml=twiml,
                status_callback=f'{base}/twilio/status/{ctx.call_id}',
                amd_callback=f'{base}/twilio/amd/{ctx.call_id}',
                record=session.recording, timeout=RING_SECONDS,
                recording_callback=(f'{base}/twilio/recording/{ctx.call_id}'
                                    if session.recording else None),
                time_limit=ctx.brief.max_minutes * 60 + 90)
            session.call_sid = created.get('sid')
            ctx.link(providerCallSid=session.call_sid, fromNumber=session.from_number)
            if session.canceled:
                await self._cancel_remote(session)
                return await self._finish(ctx, session)
            await self._await_call(ctx, session)
            return await self._finish(ctx, session)
        finally:
            self.sessions.pop(ctx.call_id, None)

    async def _await_call(self, ctx, session):
        """Wait for the call to end, but never forever."""
        waits = [asyncio.create_task(session.connected.wait()),
                 asyncio.create_task(session.finished.wait())]
        done, pending = await asyncio.wait(waits, timeout=self.connect_timeout,
                                           return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        if not done:
            await self._resolve_silent_call(session)
            return
        limit = ctx.brief.max_minutes * 60 + OVERRUN_SECONDS
        try:
            await asyncio.wait_for(session.finished.wait(), limit)
        except asyncio.TimeoutError:
            session.fail('The call ran past its time limit and was ended.')
            await session.hangup('error')
            try:
                await asyncio.wait_for(session.finished.wait(), 15)
            except asyncio.TimeoutError:
                session.finished.set()

    async def _resolve_silent_call(self, session):
        """No stream and no final status: ask Twilio, then end the call either way."""
        status = None
        try:
            status = (await session.twilio.get_call(session.call_sid)).get('status')
        except Exception:
            pass
        if status in TWILIO_TERMINAL and status != 'failed':
            session.end_reason = session.end_reason or TWILIO_TERMINAL[status]
        elif status == 'in-progress':
            session.fail('The call was answered, but Twilio could not reach the phone gateway.')
            await self._hang_up_remote(session)
        else:
            session.fail('Twilio sent no call status; check that the public address works.')
            await self._cancel_remote(session)
        session.finished.set()

    async def _cancel_remote(self, session):
        if not session.call_sid:
            return
        try:
            await session.twilio.update_call(session.call_sid, status='canceled')
        except Exception:
            await self._hang_up_remote(session)

    async def _hang_up_remote(self, session):
        if not session.call_sid:
            return
        try:
            await session.twilio.update_call(session.call_sid, status='completed')
        except Exception:
            pass

    async def _finish(self, ctx, session):
        if session.phone_seconds is None and session.connected.is_set():
            try:
                await asyncio.wait_for(self._await_duration(session), self.status_grace)
            except asyncio.TimeoutError:
                pass
        reason = session.end_reason or ('canceled' if session.canceled else 'hangup')
        error = session.error_detail if reason == 'error' else None
        usage = self._usage(session)
        try:
            usage['phoneProvider'] = ctx.credentials('twilio').get('provider') or 'twilio'
        except Exception:
            pass
        await ctx.finish(reason, usage=usage, error=error)

    async def provider_price(self, credentials, record):
        """What Twilio or SignalWire charged for a finished call; None until it says."""
        sid = (record.get('line') or {}).get('providerCallSid')
        if not sid:
            return None
        body = await self.twilio_factory(credentials('twilio')).get_call(sid)
        return provider_price_from(body)

    async def start_inbound(self, ctx, *, call_sid, token):
        openai = ctx.credentials('openai')
        twilio_creds = ctx.credentials('twilio')
        env = self.environ()
        session = PhoneSession(
            self, ctx, api_key=openai['apiKey'], twilio=self.twilio_factory(twilio_creds),
            from_number=twilio_creds.get('twilioNumber') or twilio_creds['fromNumber'],
            token=token, inbound=True,
            # Incoming calls are not recorded, so the greeting must not say they are.
            recording=False, owner_phone=env.get('COLLEAGUE_OWNER_PHONE'))
        session.call_sid = call_sid
        self.sessions[ctx.call_id] = session
        try:
            ctx.set_status('connecting')
            ctx.link(providerCallSid=call_sid)
            try:
                await asyncio.wait_for(session.connected.wait(), 30)
            except asyncio.TimeoutError:
                session.fail('The incoming call ended before its audio connected.')
                session.finished.set()
            if not session.finished.is_set():
                await self._await_call(ctx, session)
            await self._finish(ctx, session)
        finally:
            self.sessions.pop(ctx.call_id, None)

    async def _await_duration(self, session):
        while session.phone_seconds is None:
            await asyncio.sleep(0.2)

    @staticmethod
    def _usage(session):
        usage = {'voiceSeconds': int(getattr(session.live, 'usage_seconds', 0) or 0)}
        if session.phone_seconds is not None:
            usage['phoneSeconds'] = session.phone_seconds
        if any(session.backend_tokens.values()):
            usage['backendTokens'] = {key: value for key, value in session.backend_tokens.items()
                                      if value or key in ('input', 'output')}
            usage['backendModel'] = session.backend_model
        if session.connected.is_set():
            usage['audio'] = session.audio_stats()
        return usage

    # Twilio callbacks ----------------------------------------------------

    def on_status(self, call_id, params):
        session = self.sessions.get(call_id)
        if session is None:
            return False
        status = params.get('CallStatus')
        if params.get('CallDuration'):
            try:
                session.phone_seconds = int(params['CallDuration'])
            except ValueError:
                pass
        if status == 'ringing':
            session.ctx.set_status('ringing')
        elif status in TWILIO_TERMINAL:
            if session.phone_seconds is None:
                session.phone_seconds = 0
            if not session.connected.is_set():
                if status == 'failed':
                    code = params.get('ErrorCode') or params.get('SipResponseCode')
                    session.fail('Twilio could not place the call'
                                 + (f' (error {code}).' if code else '.'))
                else:
                    session.end_reason = session.end_reason or TWILIO_TERMINAL[status]
                session.finished.set()
        return True

    def on_amd(self, call_id, answered_by):
        session = self.sessions.get(call_id)
        if session is None:
            return False
        session.ctx.event('call.answered_by', answeredBy=answered_by or 'unknown')
        if (answered_by or '').startswith('machine') or answered_by == 'fax':
            session._spawn(session.machine_suspected(answered_by))
        return True

    # Line interface --------------------------------------------------------

    async def instruct(self, call_id, text, *, silent=False):
        session = self.sessions.get(call_id)
        return bool(session and await session.instruct(text, silent=silent))

    async def end(self, call_id):
        session = self.sessions.get(call_id)
        if session is None:
            return False
        if session.connected.is_set():
            await session.wrap_up()
            return True
        # Not connected yet: start() checks this flag after each step, so a call
        # still being set up is never dialed, and one already dialed is canceled.
        session.canceled = True
        session.end_reason = session.end_reason or 'canceled'
        if isinstance(session, SipPhoneSession):
            await session._end_call()
            if not session.session_id:
                session.finished.set()
            return True
        await self._cancel_remote(session)
        session.finished.set()
        return True

    async def transfer(self, call_id):
        session = self.sessions.get(call_id)
        if session is None or not session.connected.is_set():
            raise NotReady('The call is not connected.')
        return await session.transfer()


def inbound_brief(environ, caller):
    """The brief for a call someone placed to us. Withheld numbers are still answered."""
    context = None
    try:
        to = normalize_phone(caller or '')
    except ValueError:
        to = environ.get('TWILIO_FROM_NUMBER') or environ.get('COLLEAGUE_CALLER_ID') or '+10000000000'
        context = 'The caller withheld their number. Ask for a callback number.'
    notify = None
    try:
        if environ.get('COLLEAGUE_NOTIFY_WEBHOOK'):
            notify = {'webhookUrl': validate_webhook_url(environ['COLLEAGUE_NOTIFY_WEBHOOK'])}
    except ValueError as error:
        print(f'COLLEAGUE_NOTIFY_WEBHOOK is ignored: {error}', flush=True)
        notify = None
    payload = {
        'channel': 'phone',
        'to': to,
        'onBehalfOf': environ.get('COLLEAGUE_OWNER_NAME') or 'the owner',
        'objective': 'Answer an incoming call and take a clear message with a callback number.',
    }
    if context:
        payload['context'] = context
    if notify:
        payload['notify'] = notify
    return CallBrief.from_dict(payload)
