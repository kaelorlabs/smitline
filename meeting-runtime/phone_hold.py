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
# A woken voice's starting context: room kept for the notes and the note on why it woke.
RESUME_TOKEN_MARGIN = 400


def _now_iso():
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


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
    """Time on hold: how often, how long in all, and the hold going on now."""

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.times = 0
        self.ended_seconds = 0.0  # holds that are over
        self.since = None         # when the hold going on now started (clock)
        self.since_at = None      # the same, as an ISO time for the record
        self.mode = None          # 'listener' (the voice is off) or 'voice' (it waits quietly)
        self.wakes = 0
        self.timed_out = False

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

    def end(self, now=None):
        seconds = self.current(now)
        self.ended_seconds += seconds
        self.since = self.since_at = None
        return seconds

    def seconds(self, now=None):
        return self.ended_seconds + self.current(now)


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
        listener = await self._hold_listener()
        if self._hanging_up or self.finished.is_set():
            return
        self.holdbook.start('listener' if listener is not None else 'voice')
        self._unanswered = 0  # what went unanswered before the hold says nothing about after it
        self._write_note('On hold.')
        self.ctx.event('call.on_hold', times=self.holdbook.times,
                       detector=listener.name if listener is not None else None)
        self._publish_hold()
        if listener is None:
            # Nothing to listen with here: the voice stays on and waits quietly.
            await self._hold_tool_output(call, {
                'ok': True, 'waiting': True,
                'note': 'Wait quietly. Answer as soon as a person talks to you.'})
            return
        await self._go_to_sleep('hold')

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
        self._close_reason = reason
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
            await live.shutdown()
        return True

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
        if self._hanging_up or self.finished.is_set():
            self.live = live  # retired with the call
            return False
        self.live = live
        while self._frames:  # what was said since, then the line as it comes
            await live.send_audio(self._frames.popleft())
        self.asleep = False
        self._publish_hold()
        for kind, args in self._held_notes:
            if kind == 'deliver':
                await self.deliver(args)
            else:
                await self.instruct(args[0], silent=args[1])
        self._held_notes = []
        if why == 'person':
            self._waking = {'at': time.monotonic()}
            self._spawn(self._wake_guard(self._waking))
        return True

    async def _wake_guard(self, waking):
        """A woken voice that heard only music says nothing and calls nothing: it goes back to
        waiting once WAKE_GUARD_SECONDS have passed and nothing was said for WAKE_QUIET_SECONDS."""
        while self._waking is waking and not self._hanging_up:
            await asyncio.sleep(WAKE_GUARD_POLL)
            now = time.monotonic()
            said = max(self._other_spoke_at or 0.0, waking['at'])
            if now - waking['at'] >= WAKE_GUARD_SECONDS and now - said >= WAKE_QUIET_SECONDS:
                if self._waking is waking:
                    self.ctx.event('call.hold_rewait', why='quiet')
                    await self._go_to_sleep('hold')
                return

    def _voice_spoke_on_hold(self):
        """The voice made speech while the call was on hold: someone is there, or it thinks so,
        and the hold is over."""
        book = self.holdbook
        if not book.on_hold:
            return
        if self._waking is not None:
            self._waking = None
        elif not (book.mode == 'voice' and book.current() >= VOICE_HOLD_SETTLE):
            return
        self._end_hold('person')

    def _end_hold(self, by):
        book = self.holdbook
        if not book.on_hold:
            return
        seconds = book.end()
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
