"""Joining a meeting by phone: dial its number, key in its ID and passcode, take part by voice.

Zoom meetings, Teams meetings whose organizer has audio conferencing, and Workspace Google Meet
meetings have phone numbers. A meeting brief can carry them (dialIn: the number, the meeting ID
and the numeric phone passcode, or the invitation text they come in), or name the dial-in number
itself as `to`, even as a one-tap number such as +14388097799,,85951289482#,,,,*123456#.

Smitline joins by phone when the brief has no link, when its platform is in
SMITLINE_MEETING_DIAL_IN_FIRST, when there is no free meeting seat, or when the platform turned
the browser away (meeting_line.py). It is a phone call (phone_line.py) whose TwiML has the phone
provider key in the digits with its own DTMF, <Play digits> before <Connect><Stream>: Twilio and
SignalWire both document it, with w for half a second. The timing is the platform's one-tap number,
where each comma is a two-second pause. The voice comes on the line once the keys are in, so it
never hears or answers the bridge's prompts.

On the line it is the meeting voice (meeting_voice.py): it listens, answers when addressed, and
leaves when a person would. Nobody sees a name or a camera tile, so it says it is an AI out loud,
in the hello at the first pause once it is in. The bridge's announcements tell whether it waits to
be let in (a waiting room, a lobby, a host who has not started): that wait is not billed and ends
after WAITING_LIMIT. A bridge that refuses the ID or passcode ends the call at once.

The call stays a meeting: its result and end reasons are a meeting's, and usage.voiceSeconds is
the time in the meeting, what a meeting is billed by. The phone line's own usage (phoneSeconds,
the provider's price) goes into its cost.
"""
import asyncio
import re
import secrets
import time

from call_hooks import CallRefused
from dial_in_details import bridge_says, keys_seconds, play_digits
from meeting_intro import (
    INTRO_QUIET_SECONDS, addressing_instructions, intro_cue, intro_instructions,
    phone_identity_instructions,
)
from meeting_lifecycle import (
    LEAVE_ASK_SECONDS, LEAVE_MEETING_TOOL, LEAVE_QUIET_SECONDS, LEAVE_REASONS,
    LEAVING_INSTRUCTIONS, LeaveJudge, asks_to_leave, leave_end_reason, only_closing,
    says_goodbye,
)
from phone_line import GOODBYE_POLL, OVERRUN_SECONDS, RING_SECONDS, PhoneSession
from phone_line import owner_settings as phone_owner_settings


# Waiting to be let in (a waiting room, a lobby, a host who has not started) gives up after this
# long, as a browser meeting's join does; nothing is billed for it.
WAITING_LIMIT = 600.0
# With no announcement from the bridge this long after the keys, Smitline is in the meeting.
ADMIT_SETTLE = 8.0
# A sentence from the bridge ends at a full stop, or after this long without more words.
SENTENCE_PAUSE = 1.2
# How often the call's watchdog looks (waiting, maxMinutes, idle).
WATCH_EVERY = 1.0


# The way in ----------------------------------------------------------------------------------------

def dial_in_first(platform, settings):
    """SMITLINE_MEETING_DIAL_IN_FIRST names the platforms (zoom, teams, meet, or all) whose
    meetings join by phone when the brief has their dial-in, rather than by their link."""
    names = {item.strip().lower() for item in str(settings.get('SMITLINE_MEETING_DIAL_IN_FIRST') or '').split(',')}
    return 'all' in names or (platform is not None and platform in names)


def phone_problem(line, credentials):
    """Why this installation cannot dial in right now, as a CallRefused, or None. Dial-in always
    relays the call's audio through Smitline (the voice and the keys need the media stream)."""
    from call_hooks import MissingCredentials
    if line is None:
        return CallRefused('dial_in_unavailable', 'Joining a meeting by phone needs phone calls set '
                           'up on this installation.', status=503)
    try:
        credentials('openai')
        credentials('twilio')
    except MissingCredentials as error:
        return CallRefused('not_configured', str(error), status=503)
    if not line.gateway_ready:
        return CallRefused('gateway_unavailable', 'The phone gateway could not start; see the daemon '
                           'log. Set SMITLINE_GATEWAY_PORT to a free port.')
    available = getattr(line, '_public_available', None)
    if available is not None and not available():
        return CallRefused('no_public_url', 'The phone provider cannot reach this computer. Set '
                           'SMITLINE_PUBLIC_URL, or install cloudflared or Docker for a quick tunnel.')
    return None


def way_in(meeting_line, settings, owner, brief, credentials):
    """('phone', why) when this meeting joins by phone, ('link', None) when by its link. By phone:
    the brief has no link (no_link); its platform dials in first (dial_in_first); or no meeting seat
    is free now and the phone line could dial in (no_seat)."""
    if not getattr(brief, 'dial_in', None):
        return 'link', None
    if brief.joins_by_phone:
        return 'phone', 'no_link'
    phone = getattr(meeting_line, 'phone', None)
    if phone is None:
        return 'link', None
    if dial_in_first(brief.platform, settings):
        return 'phone', 'dial_in_first'
    problem = getattr(getattr(meeting_line.daemon, 'supervisor', None), 'capacity_problem', None)
    if callable(problem) and problem(owner) and phone_problem(phone, credentials) is None:
        return 'phone', 'no_seat'
    return 'link', None


def ready(line, credentials):
    """Raise when this installation cannot dial in now (MeetingLine.ready for a dial-in)."""
    problem = phone_problem(line, credentials)
    if problem is not None:
        raise problem


async def preflight(line):
    """For a brief check: whether the provider can reach this computer now, in words, or None."""
    from tunnel import TunnelError
    try:
        await line._public_url()
    except TunnelError as error:
        return str(error)
    return None


# Platform answers that a person in the meeting can do nothing about: the browser was turned away.
REFUSED_ENDINGS = frozenset({'authentication_required'})


def fall_back(meeting_line, ctx, handoff):
    """True when a meeting the browser could not get into dials in instead: the platform turned
    the browser away before it was in, the brief has the dial-in, and the phone line can dial."""
    if ((handoff or {}).get('endReason') not in REFUSED_ENDINGS
            or ctx.call_id in meeting_line._canceled or ctx.call_id in meeting_line._over_time
            or not getattr(ctx.brief, 'dial_in', None)
            or phone_problem(getattr(meeting_line, 'phone', None), ctx.credentials) is not None):
        return False
    forget = getattr(meeting_line.daemon, 'forget_captions', None)
    meeting_id = meeting_line._meetings.pop(ctx.call_id, None)
    if forget is not None and meeting_id is not None:
        forget(meeting_id)
    return True


# The voice on the line -----------------------------------------------------------------------------

def voice_config(brief, settings):
    """The GPT-Live session for a meeting joined by phone: the meeting's participation policy, its
    name and leaving rules, the spoken hello, the brief's context, on the phone's G.711 audio."""
    import call_requests
    import meeting_voice
    from call_brief import default_voice
    from meeting_line import context_from_brief
    from phone_prompts import DEFAULT_BACKEND_MODEL
    from runtime_state import DEFAULT_PARTICIPANT_NAME
    from startup_input import context_text, handoff_from_state, handoff_to_session_input
    from voice_core import PCMU8, session_config
    owner = brief.on_behalf_of
    name = ' '.join(str(settings.get('SMITLINE_PARTICIPANT_NAME') or '').split())[:80] or DEFAULT_PARTICIPANT_NAME
    guidance = ' '.join(str(settings.get('SMITLINE_MEETING_INSTRUCTIONS') or '').split())[:2000]
    can_do = getattr(brief, 'can_do', None)
    handoff = handoff_from_state({'context': context_from_brief(brief)})
    instructions = meeting_voice.policy(name, by_phone=True)
    instructions += addressing_instructions(name)
    instructions += LEAVING_INSTRUCTIONS
    instructions += phone_identity_instructions(owner)
    if can_do:
        instructions += call_requests.voice_instructions(owner, can_do, meeting=True)
    instructions += intro_instructions(owner, bool(can_do))
    if guidance:
        instructions += ('\nOrganizer-provided meeting guidance: ' + guidance + '\nUse this guidance to '
                         'understand the meeting and your role where it is compatible with the '
                         'policies above.')
    if brief.language:
        instructions += (f'\nStart in the language with tag {brief.language}, and follow the '
                         'languages policy from there.')
    tools = [LEAVE_MEETING_TOOL]
    if can_do:
        tools.append(call_requests.ask_agent_tool(owner))
    if settings.get('SMITLINE_MEETING_WEB_SEARCH') == '1':
        tools.append({'type': 'web_search'})
    delegation = {'type': 'responses', 'responses': {
        'model': str(settings.get('SMITLINE_MEETING_BACKEND_MODEL') or '').strip() or DEFAULT_BACKEND_MODEL,
        'instructions': meeting_voice.backend_instructions(
            name, owner, can_do=can_do, guidance=guidance, context=context_text(handoff)),
        'tool_choice': 'auto',
        'tools': tools,
    }}
    return session_config(instructions=instructions, audio_format=PCMU8,
                          voice=brief.voice or default_voice(settings), delegation=delegation,
                          seed_input=handoff_to_session_input(handoff))


def idle_leave_seconds(settings):
    """SMITLINE_MEETING_IDLE_LEAVE_SECONDS as a browser meeting reads it (0 never leaves)."""
    from meeting_lifecycle import DEFAULT_IDLE_LEAVE_SECONDS
    value = str(settings.get('SMITLINE_MEETING_IDLE_LEAVE_SECONDS') or '').strip()
    return int(value) if value.isdigit() and int(value) <= 86400 else DEFAULT_IDLE_LEAVE_SECONDS


class MeetingSide:
    """The call's context as a meeting joined by phone reports to it: the people on the line are
    the meeting (speaker meeting), as in a meeting joined by link."""

    def __init__(self, ctx):
        self._ctx = ctx

    def __getattr__(self, name):
        return getattr(self._ctx, name)

    def add_transcript(self, speaker, text):
        self._ctx.add_transcript('meeting' if speaker == 'other' else speaker, text)


class DialInSession(PhoneSession):
    """A meeting joined by phone: a phone call carrying the meeting voice."""

    # A bridge's "say your name after the tone" is not a voicemail greeting.
    voicemail_possible = False

    def __init__(self, line, ctx, **kwargs):
        from call_requests import MAX_REQUESTS
        from runtime_state import DEFAULT_PARTICIPANT_NAME
        super().__init__(line, MeetingSide(ctx), recording=False, **kwargs)
        self.settings = phone_owner_settings(ctx, line.environ())
        self.participant_name = (' '.join(str(self.settings.get('SMITLINE_PARTICIPANT_NAME') or '').split())[:80]
                                 or DEFAULT_PARTICIPANT_NAME)
        if self.desk is not None:
            self.desk.limit = MAX_REQUESTS['meeting']  # a meeting's, as the call service counts them
        self.admitted = asyncio.Event()
        self._disclosed_outside = False
        self.live_at = None  # when it got into the meeting: its meeting time counts from here
        self.waiting_since = None
        self.left = None  # why it left by itself: left_<reason> or idle
        self.refusal = None  # what the bridge said when it refused the keys
        self._sentence = ''  # the bridge's words before Smitline is in, not yet a whole sentence
        self._sentence_at = None
        self._prompt_at = None  # when the bridge last asked for keys (it is not people)
        self._words_at = None  # when anyone, the voice included, last said something
        self._asked_to_leave_at = None
        self._judged = None  # the goodbye the leave check last decided to stay after
        self.judge = LeaveJudge(self.api_key)

    def config(self):
        return voice_config(self.brief, self.settings)

    # In the meeting, or waiting to be let in -------------------------------------------------------

    def _stream_started(self):
        """The keys are in and the voice is on the line; whether Smitline is in the meeting yet
        is for the bridge to say (_heard_sentence)."""

    def _admit(self):
        if self.admitted.is_set() or self._hanging_up:
            return
        # In from the start, or from the bridge's last prompt, unless it waited to be let in:
        # that wait is not meeting time.
        self.live_at = (time.monotonic() if self.waiting_since is not None
                        else self._prompt_at or self.started_at)
        # Anything it said before it was in (to a waiting room) did not reach the meeting.
        self._disclosed_outside = self.disclosed.is_set()
        self.admitted.set()
        self.ctx.set_status('in_progress')

    def _sentences(self, *, flush=False):
        """Judge the bridge's words before Smitline is in, a whole sentence at a time."""
        while self._sentence:
            match = re.search(r'[.?!](?:\s|$)', self._sentence)
            if match:
                sentence, self._sentence = self._sentence[:match.end()], self._sentence[match.end():]
            elif flush:
                sentence, self._sentence = self._sentence, ''
            else:
                return
            self._heard_sentence(sentence)

    def _heard_sentence(self, sentence):
        if self.admitted.is_set() or self._hanging_up or not re.search(r'\w\w', sentence):
            return  # in already, leaving, or not words (music written as notes)
        kind = bridge_says(sentence)
        if kind == 'refused':
            self.refusal = ' '.join(sentence.split())[:200]
            self.ctx.event('call.dial_in_refused', said=self.refusal)
            self._start_hangup('dial_in_refused', disclose=False)
        elif kind == 'waiting':
            if self.waiting_since is None:
                self.waiting_since = time.monotonic()
                self.ctx.set_status('waiting')
                self.ctx.event('call.waiting')
        elif kind == 'prompt':
            self._prompt_at = time.monotonic()
        else:
            self._admit()

    def _transcript(self, event):
        super()._transcript(event)
        delta = event.get('delta') or ''
        if not delta.strip():
            return
        now = time.monotonic()
        self._words_at = now
        if 'input_transcript' not in event['type']:
            return
        if asks_to_leave(self._other_turn[-240:]):
            self._asked_to_leave_at = now
        if not self.admitted.is_set():
            self._sentence += delta
            self._sentence_at = now
            self._sentences()

    async def _open(self):
        """The spoken AI disclosure, the hello in Arun's words, said once at the first real pause
        after Smitline is in, never over anyone: nobody sees a name or a tile on the phone."""
        await self.admitted.wait()
        await self.live.started.wait()
        loop = asyncio.get_running_loop()
        quiet_since = None
        while not self.finished.is_set() and not self._hanging_up:
            if self.disclosed.is_set() and not self._disclosed_outside:
                return  # it already said it in the meeting, answering someone
            if self._agent_speaking() or self._other_speaking():
                quiet_since = None
            elif quiet_since is None:
                quiet_since = loop.time()
            elif loop.time() - quiet_since >= INTRO_QUIET_SECONDS:
                sent = await self.live.append('session.commentary.append', intro_cue(
                    self.brief.on_behalf_of, bool(getattr(self.brief, 'can_do', None))))
                self.ctx.event('call.disclosure_cued', sent=bool(sent))
                return
            await asyncio.sleep(0.1)

    async def _watch(self):
        """Waiting to be let in, the meeting's maxMinutes from the moment it is in, and leaving
        when nobody has said anything for SMITLINE_MEETING_IDLE_LEAVE_SECONDS."""
        limit = self.brief.max_minutes * 60
        idle = idle_leave_seconds(self.settings)
        while True:
            await asyncio.sleep(WATCH_EVERY)
            self._claims_due()
            now = time.monotonic()
            if self._sentence and now - self._sentence_at >= SENTENCE_PAUSE:
                self._sentences(flush=True)
            if self._hanging_up:
                continue
            if not self.admitted.is_set():
                if self.waiting_since is not None:
                    if now - self.waiting_since >= WAITING_LIMIT:
                        self.ctx.event('call.not_admitted', waitedSeconds=int(now - self.waiting_since))
                        await self.hangup('not_admitted', disclose=False)
                        return
                elif (now - max(self.started_at, self._prompt_at or self.started_at) >= ADMIT_SETTLE
                      and not self._sentence):
                    self._admit()  # nothing said it waits, or asked for more: a quiet meeting
                continue
            if now - self.live_at >= limit:
                self.ctx.event('call.max_duration')
                await self.hangup('max_duration')
                return
            if idle and now - max(self._words_at or self.live_at, self.live_at) >= idle:
                self._left('idle')
                await self.hangup('hangup', disclose=False)
                return

    def meeting_seconds(self):
        """Time in the meeting: from when it was in to the end of the call."""
        if self.live_at is None:
            return 0
        ended = self.stream_ended_at if self.stream_ended_at is not None else time.monotonic()
        return max(0, int(ended - self.live_at))

    # Leaving --------------------------------------------------------------------------------------

    def _left(self, why):
        if self.left is None:
            self.left = why
            self.ctx.event('call.agent_left', reason=why)

    def _handle_live_event(self, event):
        if event.get('type') == 'response.event':
            from voice_core import function_call_from
            call = function_call_from(event)
            if call and call['name'] == 'leave_meeting' and not self._hanging_up:
                # Leaving needs no tool result or further backend turn, as in a browser meeting.
                reason = call['arguments'].get('reason')
                self._left(leave_end_reason(reason if reason in LEAVE_REASONS else 'other'))
                self._start_hangup('hangup')
        return super()._handle_live_event(event)

    async def _after_goodbye(self):
        """The voice said goodbye: once that has been heard and the meeting is quiet for a moment,
        leave, without waiting for the backend's leave_meeting. At once when someone asked it to
        leave; otherwise when the leave check of the end of the meeting says so, as a meeting
        joined by link does. Anything more than thanks or bye from them carries the meeting on."""
        goodbye = self._agent_latest
        quiet_since = None
        while not self.finished.is_set() and not self._hanging_up:
            await asyncio.sleep(GOODBYE_POLL)
            if not says_goodbye(self._agent_latest) or not only_closing(self._said_after_goodbye):
                return  # it said more, or they did: the next goodbye starts this again
            if self._agent_speaking() or self._other_speaking():
                quiet_since = None
                continue
            now = time.monotonic()
            quiet_since = quiet_since or now
            if now - quiet_since < LEAVE_QUIET_SECONDS:
                continue
            asked = self._asked_to_leave_at
            if asked is not None and now - asked <= LEAVE_ASK_SECONDS:
                reason = 'asked_to_leave'
            else:
                if self._judged == goodbye:
                    return  # it already decided to stay after this goodbye
                self._judged = goodbye
                lines = [dict(entry) for entry in self.ctx.transcript[-29:]]
                lines.append({'speaker': 'agent', 'text': self._agent_latest})
                decided, reason = await self.judge.decide(lines, self.participant_name)
                self.ctx.event('call.leave_judged', leave=decided, reason=reason)
                if (not decided or self._hanging_up or self._agent_latest != goodbye
                        or not only_closing(self._said_after_goodbye)):
                    return
            self.ctx.event('call.goodbye_hangup')
            self._left(leave_end_reason(reason))
            self._start_hangup('hangup')
            return

    async def wrap_up(self):
        """end_call: leave the meeting now, as a meeting joined by link does (no goodbye cue).
        An AI disclosure the voice owes still comes first."""
        if self.live is None:
            return False
        self.end_reason = self.end_reason or 'canceled'
        self._start_hangup('canceled')
        return True

    # How it ended, as a meeting ----------------------------------------------------------------

    def outcome(self):
        """(end reason, handoff, error) for the call, in a meeting's terms. A call with nothing
        said gets a short handoff saying what happened, which the result is made from."""
        reason = self.end_reason or ('canceled' if self.canceled else 'hangup')
        waited = int(time.monotonic() - self.waiting_since) if self.waiting_since else 0
        number = (self.brief.dial_in or {}).get('number', '')
        notes = {
            'busy': f"The meeting's dial-in number ({number}) was busy.",
            'no_answer': f"The meeting's dial-in number ({number}) did not answer.",
            'not_admitted': f'Smitline dialed in and waited {max(1, round(waited / 60))} minutes to be '
                            'let into the meeting, but nobody let it in.',
            'dial_in_refused': ("The meeting's phone line did not accept the meeting ID or passcode"
                                + (f': "{self.refusal}"' if self.refusal else '') + '.'),
        }
        if reason == 'error':
            return 'error', None, self.error_detail or "The call to the meeting's dial-in number failed."
        if reason in ('hangup', 'remote_hangup'):
            reason = 'meeting_ended'
        summary = notes.get(reason)
        if summary is None and self.connected.is_set() and not self.ctx.transcript:
            summary = 'Smitline joined the meeting by phone, but nobody spoke.'
        handoff = {'summary': summary, 'endReason': self.left or reason} if summary else None
        return reason, handoff, None


def call_seconds_limit(brief):
    """The longest the provider's call may run: the keys, the wait to be let in, the meeting."""
    return int(keys_seconds(brief.dial_in['keys']) + WAITING_LIMIT + brief.max_minutes * 60 + 90)


async def join_by_phone(line, ctx, why):
    """Dial the meeting, have the provider key in the ID and passcode, and take part by phone until
    it ends; the call then finishes as a meeting. `line` is the PhoneLine; `why` says why this
    meeting joins by phone (call.dial_in): no_link, dial_in_first, no_seat or platform_refused."""
    from twilio_client import stream_twiml
    dial = ctx.brief.dial_in
    openai = ctx.credentials('openai')
    creds = ctx.credentials('twilio')
    twilio = line.twilio_factory(creds)
    session = DialInSession(line, ctx, api_key=openai['apiKey'], twilio=twilio,
                            from_number=creds['fromNumber'], token=secrets.token_urlsafe(24))
    line.sessions[ctx.call_id] = session
    try:
        ctx.set_status('connecting')
        ctx.link(joinedBy='phone', platform=dial.get('platform'))
        ctx.event('call.dial_in', reason=why, number=dial['number'],
                  **({'platform': dial['platform']} if dial.get('platform') else {}))
        base = (await line._public_url()).rstrip('/')
        if session.canceled:
            return await finish(line, ctx, session)
        realtime = (getattr(twilio, 'flavor', 'twilio') == 'signalwire'
                    and line.environ().get('SMITLINE_STREAM_REALTIME') == '1')
        twiml = stream_twiml('wss://' + base.split('://', 1)[1] + '/twilio/media',
                             {'callId': ctx.call_id, 'token': session.token}, realtime=realtime,
                             digits=play_digits(dial['keys']))
        # No answering-machine check (a bridge is a machine, and the voice comes on after the keys),
        # and never recorded: meeting audio is not recorded.
        created = await twilio.create_call(
            to=dial['number'], from_=session.from_number, twiml=twiml,
            status_callback=f'{base}/twilio/status/{ctx.call_id}', timeout=RING_SECONDS,
            time_limit=call_seconds_limit(ctx.brief))
        session.call_sid = created.get('sid')
        ctx.link(providerCallSid=session.call_sid, fromNumber=session.from_number)
        if session.canceled:
            await line._cancel_remote(session)
            return await finish(line, ctx, session)
        await _await_call(line, ctx, session)
        return await finish(line, ctx, session)
    finally:
        line.sessions.pop(ctx.call_id, None)


async def _await_call(line, ctx, session):
    """Wait for the call to end, but never forever: the stream connects once the keys are in."""
    waits = [asyncio.create_task(session.connected.wait()),
             asyncio.create_task(session.finished.wait())]
    done, pending = await asyncio.wait(
        waits, timeout=line.connect_timeout + keys_seconds(ctx.brief.dial_in['keys']),
        return_when=asyncio.FIRST_COMPLETED)
    for task in pending:
        task.cancel()
    if not done:
        await line._resolve_silent_call(session)
        return
    limit = WAITING_LIMIT + ctx.brief.max_minutes * 60 + OVERRUN_SECONDS
    try:
        await asyncio.wait_for(session.finished.wait(), limit)
    except asyncio.TimeoutError:
        session.fail('The call ran past its time limit and was ended.')
        await session.hangup('error')
        try:
            await asyncio.wait_for(session.finished.wait(), 15)
        except asyncio.TimeoutError:
            session.finished.set()


async def finish(line, ctx, session):
    """Finish the call as a meeting: usage.voiceSeconds is the time in the meeting, and the phone
    line's own usage (phoneSeconds, its provider) stays for the cost."""
    if session.tap is not None:
        session.tap.close()
    if session.phone_seconds is None and session.connected.is_set():
        try:
            await asyncio.wait_for(line._await_duration(session), line.status_grace)
        except asyncio.TimeoutError:
            pass
    if session.phone_seconds is None and session.connected.is_set():
        session.phone_seconds = session.connected_seconds()
    usage = line._usage(session)
    usage['voiceSeconds'] = session.meeting_seconds()
    try:
        usage['phoneProvider'] = ctx.credentials('twilio').get('provider') or 'twilio'
    except Exception:
        pass
    reason, handoff, error = session.outcome()
    await ctx.finish(reason, usage=usage, handoff=handoff, error=error)
