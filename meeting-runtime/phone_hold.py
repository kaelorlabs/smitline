"""Waiting on hold with the voice switched off, and waking it when someone starts talking.

GPT-Live bills every second its session is open, silence included, so a call waiting in a queue
for 40 minutes would pay for 40 minutes of a voice nobody hears. When the voice's backend calls
wait_on_hold, the voice session is closed and the phone line stays open: a local listener
(hold_listener.py) hears the line instead. When someone starts talking, unless it is a looped
announcement already heard or music, a new voice session starts with the call so far, and the
audio from just before they started is replayed to it, so the voice hears their first words. The
voice decides what it heard: a person, and it answers; a recording, and its backend calls
wait_on_hold again, and that audio never wakes it again.

Only calls placed by Smitline whose audio passes through it (PhoneSession; not direct SIP, not
incoming calls, not meetings joined by phone). Without a listener (its packages or model files
are missing) the voice stays on and waits quietly. Either way, time on hold does not count toward
maxMinutes, and maxHoldMinutes ends a hold nobody answers. See docs/phone.md#waiting-on-hold.
"""
import asyncio
import collections
import time
from datetime import datetime, timezone

from phone_prompts import back_from_hold, waited
from startup_input import MESSAGE_LIMIT, TOKEN_LIMIT, estimate_tokens


FRAME_MS = 20
# The line's audio kept while the voice is off, for replaying the start of what woke it.
BUFFER_SECONDS = 30
# Replayed from this long before the listener heard the speech start.
PREROLL_MS = 300
# A woken voice that neither speaks nor goes back on hold this long after waking, once nothing
# has been said on the line for WAKE_QUIET_SECONDS, goes back to waiting: it heard only music.
WAKE_GUARD_SECONDS = 20.0
WAKE_QUIET_SECONDS = 8.0
WAKE_GUARD_POLL = 0.5
# With the voice left on (no listener), what it says this soon after the hold starts ("okay,
# I'll hold") does not end the hold.
VOICE_HOLD_SETTLE = 3.0
# A woken voice that spoke has someone on the line once it has not gone back on hold for this
# long: it may say "okay" to a recording before its backend calls wait_on_hold again.
PICKUP_CONFIRM_SECONDS = 8.0
# A voice started again takes no audio until its session has started; it must start this soon.
WAKE_START_TIMEOUT = 20.0
# A voice switched off that does not say it closed within this long is cut off, so it stops
# billing and can be woken again.
SLEEP_CLOSE_TIMEOUT = 5.0
# The other side's words are judged (hold_judge.py) once they have stopped for this long.
JUDGE_QUIET = 1.0
# Sound on the line with no words for this long, while the voice has said nothing for
# VOICE_QUIET_FOR_HOLD, is hold music: the call goes on hold. Of the line's frames since its last
# words, at least MUSIC_SHARE must have been sound (louder than MUSIC_LEVEL_DB) rather than
# silence. Plain loudness, not barge_in's detector: that one adapts to steady sound and counted
# only 56% of the test hold music as sound; every frame of it is louder than -45 dB.
MUSIC_HOLD_SECONDS = 10.0
VOICE_QUIET_FOR_HOLD = 6.0
MUSIC_SHARE = 0.6
MUSIC_LEVEL_DB = -45.0
# A woken voice's starting context: room kept for the notes and the note on why it woke.
RESUME_TOKEN_MARGIN = 400


def _now_iso():
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


async def _cut(live):
    """Close a voice session's connection without waiting for it to say goodbye."""
    shutdown = getattr(live, 'shutdown', None)
    if shutdown is not None:
        try:
            await shutdown()
        except Exception:
            pass


def message(role, text):
    """One GPT-Live input message (startup_input): developer, user, or assistant."""
    kind = 'output_text' if role == 'assistant' else 'input_text'
    return {'type': 'message', 'role': role, 'content': [{'type': kind, 'text': text}]}


def resume_input(seed, transcript, note):
    """A woken voice's starting input: the reference notes (`seed`, voice_input), the latest of
    the call's transcript that fits, and `note` on why it woke, within GPT-Live's limits."""
    seed = list(seed or ())
    used = sum(estimate_tokens(part.get('text', ''))
               for item in seed for part in item.get('content') or ())
    budget = TOKEN_LIMIT - used - estimate_tokens(note) - RESUME_TOKEN_MARGIN
    room = MESSAGE_LIMIT - len(seed) - 1
    lines = []
    for entry in reversed(transcript or ()):
        speaker, text = entry.get('speaker'), str(entry.get('text') or '').strip()
        if speaker not in ('agent', 'other') or not text:
            continue
        cost = estimate_tokens(text)
        if cost > budget or len(lines) >= room:
            break
        lines.append(message('assistant' if speaker == 'agent' else 'user', text))
        budget -= cost
    lines.reverse()
    return seed + lines + [message('developer', note)]


class HoldBook:
    """Time on hold: how often, how long in all, and the hold going on now. Also how much of it
    had the voice switched off, which costs only the phone line (usage.holdVoiceOffSeconds): a
    voice woken to check who is talking, or one waiting quietly without a listener, is billed."""

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.times = 0
        self.ended_seconds = 0.0  # holds that are over
        self.since = None         # when the hold going on now started (clock)
        self.since_at = None      # the same, as an ISO time for the record
        self.mode = None          # 'listener' (the voice is off) or 'voice' (it waits quietly)
        self.wakes = 0
        self.timed_out = False
        self.off_ended = 0.0      # time on hold with the voice off, stretches that are over
        self.off_since = None     # the voice went off during the hold going on now (clock)

    @property
    def on_hold(self):
        return self.since is not None

    def start(self, mode):
        self.times += 1
        self.since = self.clock()
        self.since_at = _now_iso()
        self.mode = mode

    def current(self, now=None):
        if self.since is None:
            return 0.0
        return max(0.0, (now if now is not None else self.clock()) - self.since)

    def voice_off(self, now=None):
        """The voice was switched off during this hold; nothing outside a hold counts."""
        if self.since is not None and self.off_since is None:
            self.off_since = now if now is not None else self.clock()

    def voice_on(self, now=None):
        """The voice is back (woken), or the hold is over: the stretch with it off ends."""
        if self.off_since is not None:
            now = now if now is not None else self.clock()
            self.off_ended += max(0.0, now - self.off_since)
            self.off_since = None

    def end(self, now=None):
        now = now if now is not None else self.clock()
        self.voice_on(now)
        seconds = self.current(now)
        self.ended_seconds += seconds
        self.since = self.since_at = None
        return seconds

    def seconds(self, now=None):
        return self.ended_seconds + self.current(now)

    def voice_off_seconds(self, now=None):
        if self.off_since is None:
            return self.off_ended
        return self.off_ended + max(0.0, (now if now is not None else self.clock()) - self.off_since)


class HoldMixin:
    """Hold for PhoneSession: needs its live, pacer, ctx, brief, joiner and spawn helpers, and its
    _connect_voice (returns a started session or None) and _retire_voice."""

    # Calls placed by Smitline with the audio through it; see can_hold().
    hold_possible = True

    def _init_hold(self):
        self.holdbook = HoldBook()
        # The voice is off: on hold, or while the owner talks with them (phone_owner.py).
        self.asleep = False
        self._close_reason = None   # why the voice session is being closed on purpose
        self._waking = None         # a woken voice still deciding what it heard
        self._listener = None
        self._listener_error = None
        self._frames = collections.deque(maxlen=BUFFER_SECONDS * 1000 // FRAME_MS)
        self._wake_ready = asyncio.Event()
        self._wake_why = None       # (why, Wake or None): what the sleeping voice wakes for
        self._held_notes = []       # notes for the voice that came while it was off
        # What is talking on the line (hold_judge.py), and the sound with no words (hold music).
        self._judge = None
        self._judge_off = False
        self._unjudged = ''
        self._judge_timer = None
        self._words_at = None
        self._frames_since_words = 0
        self._loud_since_words = 0
        self._music_hold = None

    def can_hold(self):
        return self.hold_possible and not self.inbound

    # The hold's record ---------------------------------------------------------------------

    def _write_note(self, text):
        """A transcript line about what happened on the line; nobody said it."""
        for speaker, said in self.joiner.flush():
            self.ctx.add_transcript(speaker, said)
            if speaker == 'agent':
                self._check_disclosure(said)
        self.ctx.add_transcript('note', text)

    def hold_state(self):
        book = self.holdbook
        state = {'onHold': book.on_hold, 'seconds': int(book.seconds()), 'times': book.times}
        if book.on_hold:
            state['since'] = book.since_at
        if self._listener is not None:
            state['detector'] = self._listener.name
        elif book.mode == 'voice':
            state['detector'] = None
        if book.wakes:
            state['wakes'] = book.wakes
        if self._listener is not None:
            skipped = {key: value for key, value in (self._listener.stats or {}).items()
                       if key in ('skippedRepeats', 'skippedMusic') and value}
            state.update(skipped)
        if book.timed_out:
            state['timedOut'] = True
        return state

    def _publish_hold(self):
        publish = getattr(self.ctx, 'set_hold', None)
        if publish is not None:
            publish(self.hold_state())

    # Going on hold ------------------------------------------------------------------------

    async def _hold_listener(self):
        """The listener for this call, made the first time it holds; None when it cannot be."""
        if self._listener is not None or self._listener_error is not None:
            return self._listener
        factory = getattr(self.line, 'hold_listener_factory', None)
        try:
            if factory is not None:
                self._listener = factory(self.line.environ())
            else:
                from hold_listener import HoldListener
                loop = asyncio.get_running_loop()
                self._listener = await loop.run_in_executor(
                    None, HoldListener.create, dict(self.line.environ()))
        except Exception as error:  # packages or model files missing, a model that will not load
            self._listener_error = str(error)[:200] or type(error).__name__
            self.ctx.event('call.hold_listener_unavailable', reason=self._listener_error)
            self._listener = None
        return self._listener

    async def _wait_on_hold(self, call):
        """The backend's wait_on_hold: the call is on hold, or what woke the voice was not a person."""
        if self._waking is not None:
            # A recording or music woke it: that audio never wakes it again.
            if self._listener is not None:
                self._listener.remember_recording()
            self.ctx.event('call.hold_rewait', why='recording')
            await self._go_to_sleep('hold')
            return
        if self.holdbook.on_hold:
            await self._hold_tool_output(call, {'ok': True, 'waiting': True})
            return
        await self._start_hold('voice', call=call)

    async def _start_hold(self, by, *, why=None, call=None):
        """The call is on hold: the voice's backend said so ('voice'), or the line did ('line':
        its words, judged, or hold music)."""
        listener = await self._hold_listener()
        if self._hanging_up or self.finished.is_set() or self.holdbook.on_hold or self.asleep:
            return
        self.holdbook.start('listener' if listener is not None else 'voice')
        self._unanswered = 0  # what went unanswered before the hold says nothing about after it
        self._write_note('On hold.')
        self.ctx.event('call.on_hold', times=self.holdbook.times, by=by,
                       detector=listener.name if listener is not None else None,
                       **({'why': why} if why else {}))
        self._publish_hold()
        if listener is None:
            # Nothing to listen with here: the voice stays on and waits quietly.
            note = 'Wait quietly. Answer as soon as a person talks to you.'
            if call is not None:
                await self._hold_tool_output(call, {'ok': True, 'waiting': True, 'note': note})
            elif self.live is not None:
                await self.live.append('session.instructions.append',
                                       f'The call is on hold. {note}')
            return
        await self._go_to_sleep('hold')

    # What is talking on the line ------------------------------------------------------------

    def _hold_judge(self):
        """The judge of the other side's words (hold_judge.py); None when it is turned off."""
        if self._judge is None and not self._judge_off:
            environ = self.line.environ()
            model = str(environ.get('SMITLINE_HOLD_JUDGE_MODEL') or '').strip()
            if model.lower() == 'off':
                self._judge_off = True
                return None
            factory = getattr(self.line, 'hold_judge_factory', None)
            if factory is not None:
                self._judge = factory(self.api_key, model or None)
            else:
                from hold_judge import HoldJudge
                self._judge = HoldJudge(self.api_key, model=model or None)
        return self._judge

    def _heard_words(self, delta, now):
        """The other side said something (GPT-Live's transcript): judged once they pause."""
        self._words_at = now
        self._frames_since_words = self._loud_since_words = 0
        if not self.can_hold() or self._hanging_up or self.asleep:
            return
        if self.holdbook.on_hold and self._waking is None:
            return  # the voice was left on to wait: it answers whoever talks
        self._unjudged += delta
        if self._judge_timer is not None and not self._judge_timer.done():
            self._judge_timer.cancel()
        self._judge_timer = self._spawn(self._judge_after_quiet())

    async def _judge_after_quiet(self):
        await asyncio.sleep(JUDGE_QUIET)
        text, self._unjudged = ' '.join(self._unjudged.split()), ''
        if len(text.split()) < 2 or self._hanging_up:
            return
        judge = self._hold_judge()
        if judge is None:
            return
        waking = self._waking
        earlier = [(entry.get('speaker'), entry.get('text')) for entry in self.ctx.transcript[-12:]
                   if entry.get('speaker') in ('agent', 'other')]
        from hold_judge import sure
        found, confidence = await judge.decide(earlier, text)
        self.ctx.event('call.line_judged', kind=found or 'unknown', confidence=round(confidence, 2),
                       waking=waking is not None)
        kind = sure(found, confidence)  # unsure: the free signals and timers decide instead
        if self._hanging_up or self.finished.is_set():
            return
        if waking is not None:
            # The voice was woken: what it heard is someone picking up, or the hold loop again.
            if self._waking is not waking:
                return
            if kind == 'person':
                self._waking = None
                self._end_hold('person', at=waking['at'])
            elif kind in ('hold', 'menu'):
                if self._listener is not None:
                    self._listener.remember_recording()
                self.ctx.event('call.hold_rewait', why='recording')
                await self._go_to_sleep('hold')
            return
        if kind == 'hold' and not self.holdbook.on_hold and not self.asleep and self.live is not None:
            await self._start_hold('line', why='words')

    def _line_sound(self, loud, now):
        """Each 20 ms of the line while the call is not on hold: sound with no words for a while,
        and the voice quiet, is hold music."""
        if (not self.can_hold() or self.holdbook.on_hold or self.asleep or self._hanging_up
                or self.keying or self.voicemail_heard or self.started_at is None):
            return
        self._frames_since_words += 1
        if loud:
            self._loud_since_words += 1
        since_words = now - (self._words_at or self.started_at)
        voice_quiet = now - (self._speech_out_at or self.started_at)
        # The line must have been heard for the whole stretch (frames, not just the clock), so a
        # short burst of sound cannot pass for minutes of music.
        heard = self._frames_since_words * FRAME_MS / 1000
        if (since_words >= MUSIC_HOLD_SECONDS and heard >= 0.9 * MUSIC_HOLD_SECONDS
                and voice_quiet >= VOICE_QUIET_FOR_HOLD
                and self._loud_since_words >= MUSIC_SHARE * self._frames_since_words
                and (self._music_hold is None or self._music_hold.done())):
            self._music_hold = self._spawn(self._start_hold('line', why='music'))

    def judge_usage(self):
        """The judge's tokens for the call's usage, when it was asked anything."""
        judge = self._judge
        if judge is None or not any(judge.tokens.values()):
            return {}
        return {'holdJudgeTokens': {key: value for key, value in judge.tokens.items()
                                    if value or key in ('input', 'output')},
                'holdJudgeModel': judge.model}

    async def _hold_tool_output(self, call, output):
        if self.live is not None:
            from call_requests import tool_output
            await self.live.submit_function_output(call['call_id'], tool_output(output))

    async def _go_to_sleep(self, reason):
        """Switch the voice off ('hold' or 'owner'); the line stays open. False when it was not on."""
        if self.asleep or self.live is None or self._hanging_up:
            return False
        self._waking = None
        if reason == 'hold' and self._listener is not None:
            self._listener.listen()
        self._frames.clear()
        self.asleep = True  # from here the line's audio is kept for the voice that wakes
        self.holdbook.voice_off()
        self._close_reason = reason
        # What was said so far becomes lines of its own: the next voice starts with them, and its
        # transcript's timing starts again.
        for speaker, said in self.joiner.flush():
            self.ctx.add_transcript(speaker, said)
            if speaker == 'agent':
                self._check_disclosure(said)
        self.joiner.last_end = None
        if self.pacer is not None:
            if reason == 'hold':
                await self.pacer.speech_finished(timeout=3.0)  # a short "sure" may finish
            self.pacer.flush()
            await self._twilio_send({'event': 'clear'})
        live = self.live
        self._note('voice_off', reason=reason)
        try:
            await live.close()  # session.closed brings its final usage; _run_voice goes on
        except Exception:
            await _cut(live)
            return True
        self._spawn(self._closed_or_cut(live))
        return True

    async def _closed_or_cut(self, live):
        """A session switched off that never says it closed is cut off: it stops billing, and
        _run_voice can go on to wait for the next wake."""
        try:
            await asyncio.wait_for(live.closed.wait(), SLEEP_CLOSE_TIMEOUT)
        except asyncio.TimeoutError:
            self._note('voice_cut')
            await _cut(live)

    # While on hold ------------------------------------------------------------------------

    def _hear_hold(self, payload, frame):
        """The listener hears the line while the voice is off; what it wakes for starts it again."""
        listener = self._listener
        if listener is None or not self.holdbook.on_hold or frame is None:
            return
        wake = listener.feed(frame)
        if (wake is not None and self.asleep and self._wake_why is None
                and not self._owner_on_line()):
            self._wake_why = ('person', wake)
            self._wake_ready.set()

    def _wake_owner_left(self):
        """The owner left the call while the other person may still be there: the voice comes
        back to see them off."""
        if self.asleep and self._wake_why is None and not self._hanging_up:
            self._wake_why = ('owner_left', None)
            self._wake_ready.set()

    async def _sleep(self):
        """The voice is off: start it again when there is a reason. False when the call is over."""
        while not (self._hanging_up or self.finished.is_set()):
            await self._wake_ready.wait()
            self._wake_ready.clear()
            why, wake = self._wake_why or (None, None)
            self._wake_why = None
            if why is None or self._hanging_up:
                continue
            if why == 'person' and not self.holdbook.on_hold:
                continue
            return await self._wake(why, wake)
        return False

    async def _wake(self, why, wake):
        from phone_prompts import owner_left_note
        if why == 'person':
            # Keep only the audio from just before they started: the rest was the hold.
            keep = -(-(int(wake.onset_ms_ago) + PREROLL_MS) // FRAME_MS)
            while len(self._frames) > keep:
                self._frames.popleft()
            self.holdbook.wakes += 1
            self.ctx.event('call.hold_wake', wake=self.holdbook.wakes,
                           speechMs=int(getattr(wake, 'speech_ms', 0) or 0))
            note = back_from_hold(self.brief, self.holdbook.current())
        else:
            keep = 1000 // FRAME_MS  # their last second, if they are still talking
            while len(self._frames) > keep:
                self._frames.popleft()
            note = owner_left_note(self.brief)
        self._note('voice_on', why=why, replay=len(self._frames))
        live = await self._connect_voice(self.config(resume_note=note))
        if live is None:
            self._start_hangup('error', disclose=False)
            return False
        self.live = live  # retired with the call, if it is ending
        if self._hanging_up or self.finished.is_set():
            return False
        # Its events are read from here on (_run_voice). It takes no audio until its session has
        # started, so the line's audio is still kept, and replayed once it has (_replay).
        self._spawn(self._replay(live, why))
        return True

    async def _replay(self, live, why):
        """The woken voice's session has started: give it what was said since just before they
        started talking, then the line as it comes."""
        try:
            await asyncio.wait_for(live.started.wait(), WAKE_START_TIMEOUT)
        except asyncio.TimeoutError:
            if self.live is live and not self._hanging_up:
                self.fail('The voice session did not start again.')
                self._start_hangup('error', disclose=False)
            return
        if self.live is not live or self._hanging_up:
            return
        while self._frames:
            await live.send_audio(self._frames.popleft())
        self.asleep = False  # in the same step as the last frame: none is lost or out of order
        self.holdbook.voice_on()
        self._note('replayed')
        self._publish_hold()
        held, self._held_notes = self._held_notes, []
        for kind, args in held:
            if kind == 'deliver':
                await self.deliver(args)
            else:
                await self.instruct(args[0], silent=args[1])
        if why == 'person':
            waking = {'at': time.monotonic(), 'spoke_at': None}
            self._waking = waking
            self._spawn(self._wake_guard(waking))

    async def _wake_guard(self, waking):
        """A woken voice that heard only music says nothing and calls nothing: it goes back to
        waiting once WAKE_GUARD_SECONDS have passed and nothing was said for WAKE_QUIET_SECONDS."""
        while self._waking is waking and not self._hanging_up:
            await asyncio.sleep(WAKE_GUARD_POLL)
            if waking['spoke_at'] is not None:
                continue  # it spoke: _confirm_pickup decides
            now = time.monotonic()
            said = max(self._other_spoke_at or 0.0, waking['at'])
            if now - waking['at'] >= WAKE_GUARD_SECONDS and now - said >= WAKE_QUIET_SECONDS:
                if self._waking is waking:
                    self.ctx.event('call.hold_rewait', why='quiet')
                    await self._go_to_sleep('hold')
                return

    def _voice_spoke_on_hold(self):
        """The voice made speech while the call was on hold. A woken voice has someone there once
        it does not go back on hold soon after (_confirm_pickup); a voice left on (no listener),
        once the hold has settled."""
        book = self.holdbook
        if not book.on_hold:
            return
        waking = self._waking
        if waking is not None:
            if waking['spoke_at'] is None:
                waking['spoke_at'] = time.monotonic()
                self._spawn(self._confirm_pickup(waking))
            return
        if book.mode == 'voice' and book.current() >= VOICE_HOLD_SETTLE:
            self._end_hold('person')

    async def _confirm_pickup(self, waking):
        """The woken voice spoke. Unless it goes back on hold within PICKUP_CONFIRM_SECONDS (it said
        "okay" to a recording first), someone is there: the hold ended when it first spoke."""
        await asyncio.sleep(PICKUP_CONFIRM_SECONDS)
        if self._waking is waking and not self._hanging_up:
            self._waking = None
            self._end_hold('person', at=waking['spoke_at'])

    def _end_hold(self, by, at=None):
        book = self.holdbook
        if not book.on_hold:
            return
        seconds = book.end(at)
        self.ctx.event('call.hold_ended', seconds=int(seconds), by=by)
        if by == 'person':
            self._write_note(f'Someone picked up after {waited(seconds)} on hold.')
        self._publish_hold()
        if by == 'person' and self.brief.connect_owner and not self.calling_owner:
            self._spawn(self.ring_owner('pickup'))

    async def _hold_timeout(self):
        book = self.holdbook
        book.timed_out = True
        total = book.seconds()
        self.ctx.event('call.hold_timeout', seconds=int(total))
        self._end_hold('timeout')
        self._write_note(f'Nobody picked up after {waited(total)} on hold; Smitline hung up.')
        await self.hangup('hold_timeout', disclose=False)  # nobody is there to hear a goodbye

    def _hold_due(self, now):
        return self.holdbook.on_hold and self.holdbook.seconds(now) >= self.brief.hold_minutes * 60
