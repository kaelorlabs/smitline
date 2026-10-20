"""Meetings as calls: start an ordinary daemon meeting from a brief and follow it to its handoff."""
import asyncio

from briefing import EARLIER_CALLS_HEADING
from call_hooks import LOCAL_OWNER, CallRefused, owner_settings, read_env_file
from meeting_lifecycle import AGENT_ENDINGS

# What an owner's settings give its meeting container, over the shared .env.
MEETING_SETTINGS = ('SMITLINE_VOICE', 'SMITLINE_PARTICIPANT_NAME', 'SMITLINE_OWNER_NAME')
# Everything a pool meeting's container reads from its environment besides OPENAI_API_KEY
# (bridge.py, runtime_config.py, meeting_intro.py; test_meeting_pool checks this against the
# code). Nothing else of the installation's settings reaches it. The first four are per owner.
POOL_OWNER_SETTINGS = MEETING_SETTINGS + ('SMITLINE_EXTRA_VOICES',)
POOL_MEETING_SETTINGS = POOL_OWNER_SETTINGS + (
    'SMITLINE_MEETING_BACKEND_MODEL', 'SMITLINE_MEETING_WEB_SEARCH', 'SMITLINE_MEETING_INSTRUCTIONS',
    'SMITLINE_MEETING_INTRO', 'SMITLINE_MEETING_CAMERA_STYLE', 'SMITLINE_MEETING_IDLE_LEAVE_SECONDS',
)
MEETING_STATUS = {
    'joining': 'connecting',
    'waiting_for_admission': 'waiting',
    'live': 'in_progress',
}


def context_from_brief(brief):
    constraints = [f'Do not share: {item}' for item in brief.must_not_share]
    constraints += [f'May agree to: {item}' for item in brief.may_agree_to]
    if brief.success_criteria:
        constraints.append(f'Success criteria: {brief.success_criteria}')
    session = brief.session_context
    summary = '\n\n'.join(part for part in (
        session.get('summary'),
        '\n'.join(f'- {fact}' for fact in session.get('facts') or ()),
        # Before details, so the notes of earlier calls survive the 8,000-character limit.
        (EARLIER_CALLS_HEADING + ':\n' + '\n'.join(f'- {note}' for note in session['earlierCalls'])
         if session.get('earlierCalls') else ''),
        session.get('details'),
    ) if part)
    return {
        'version': 1,
        'objective': brief.objective,
        'currentTask': f'Take part in this meeting on behalf of {brief.on_behalf_of}.',
        'summary': summary[:8000],  # the meeting handoff allows 8,000 characters
        'decisions': list(session.get('decisions') or ()),
        'constraints': constraints,
        'openQuestions': list(brief.questions) + list(session.get('openQuestions') or ()),
        'importantFiles': [],
        'recentConversation': [],
    }


def meeting_environment(hooks, owner):
    """The OpenAI key, voice, and name a meeting for `owner` runs with. A setting the owner
    has not set is blank, never the shared .env's value, which is the installation's."""
    env = {'OPENAI_API_KEY': hooks.credentials(owner, 'openai')['apiKey']}
    settings = owner_settings(hooks, owner)
    env.update({key: str(settings.get(key) or '') for key in MEETING_SETTINGS})
    return env


def pool_meeting_environment(hooks, owner, meeting_env_file=None):
    """Everything a pool meeting's container gets, and nothing else: the OpenAI key from
    credentials(owner) and the settings in POOL_MEETING_SETTINGS, from the owner's settings
    with .env.meeting over them (for another owner, .env.meeting sets only the installation-wide
    ones). Provider keys, SIP and webhook secrets, and the rest of .env never reach it. A
    setting nobody set is left out; a value with a control character is left out too."""
    settings = owner_settings(hooks, owner)
    values = {key: settings.get(key) for key in POOL_MEETING_SETTINGS}
    for key, value in read_env_file(meeting_env_file).items():
        if key in values and (owner == LOCAL_OWNER or key not in POOL_OWNER_SETTINGS):
            values[key] = value
    env = {key: str(value) for key, value in values.items()
           if value and not any(ord(char) < 32 or ord(char) == 127 for char in str(value))}
    env['OPENAI_API_KEY'] = hooks.credentials(owner, 'openai')['apiKey']
    return env


def meeting_payload(brief):
    # The meeting's opening disclosure names onBehalfOf; a brief's voice overrides SMITLINE_VOICE.
    payload = {
        'meetingUrl': brief.to,
        'context': context_from_brief(brief),
        'onBehalfOf': brief.on_behalf_of,
    }
    if brief.voice:
        payload['voice'] = brief.voice
    if brief.camera:
        payload['camera'] = {key: value for key, value in brief.camera.items() if value is not None}
    return payload


class MeetingLine:
    channel = 'meeting'

    def __init__(self, daemon, *, poll_interval=2.0, handoff_timeout=120.0,
                 sleep=asyncio.sleep, monotonic=None, transcript=None):
        self.daemon = daemon
        # transcript(meeting_id): the finished meeting's transcript entries, read from its archive.
        self._transcript = transcript or (lambda _meeting_id: [])
        self.poll_interval = poll_interval
        self.handoff_timeout = handoff_timeout
        self._sleep = sleep
        self._monotonic = monotonic
        self._meetings = {}
        self._canceled = set()
        self._starting = set()
        self._over_time = set()

    def ready(self, hooks, owner, brief):
        # No meeting container for it (the one container runs the local owner's meetings only,
        # or is busy; the pool is full, or out of ports): refused now, with the code, rather
        # than after the call was accepted. The supervisor checks again when it starts, for
        # calls that race.
        problem = getattr(getattr(self.daemon, 'supervisor', None), 'capacity_problem', None)
        problem = problem(owner) if callable(problem) else None
        if problem:
            status, code, message = problem
            raise CallRefused(code, message, status=status)
        hooks.credentials(owner, 'openai')

    def _now(self):
        if self._monotonic is not None:
            return self._monotonic()
        return asyncio.get_running_loop().time()

    async def start(self, ctx):
        ctx.set_status('connecting')
        self._starting.add(ctx.call_id)
        try:
            session = await self.daemon.create_meeting(meeting_payload(ctx.brief), owner=ctx.owner)
        finally:
            self._starting.discard(ctx.call_id)
        self._meetings[ctx.call_id] = session.id
        ctx.link(meetingId=session.id, platform=session.platform)
        if ctx.call_id in self._canceled:
            # Ended while the meeting was still being created: stop it now.
            await self.daemon.cancel_meeting(session.id)
        await self._follow(ctx, session.id)

    async def _follow(self, ctx, meeting_id):
        last = None
        started = None
        limit = ctx.brief.max_minutes * 60
        while True:
            session = await self.daemon.get_meeting(meeting_id, owner=ctx.owner)
            if session.state != last:
                last = session.state
                if session.state == 'live' and started is None:
                    started = self._now()
                status = MEETING_STATUS.get(session.state)
                if status:
                    ctx.set_status(status)
            if session.state == 'ended':
                # Meeting time ends here, not when the handoff arrives up to a few minutes later.
                ended = self._now()
                break
            if started is not None and self._now() - started >= limit and ctx.call_id not in self._over_time:
                self._over_time.add(ctx.call_id)
                ctx.event('call.max_duration')
                await self.daemon.cancel_meeting(meeting_id)
            await self._sleep(self.poll_interval)
        handoff = await self._await_handoff(meeting_id, ctx.owner)
        # The result is judged from what was said, so the call gets the meeting's transcript.
        try:
            entries = self._transcript(meeting_id)
        except Exception:
            entries = []
        for entry in entries:
            ctx.add_transcript(entry['speaker'], entry['text'])
        seconds = int(ended - started) if started is not None else 0
        left = (handoff or {}).get('endReason')
        if left in AGENT_ENDINGS:
            ctx.event('call.agent_left', reason=left)
        if ctx.call_id in self._over_time:
            reason = 'max_duration'
        elif ctx.call_id in self._canceled:
            reason = 'canceled'
        else:
            reason = 'meeting_ended'
        self._meetings.pop(ctx.call_id, None)
        self._canceled.discard(ctx.call_id)
        self._over_time.discard(ctx.call_id)
        await ctx.finish(reason, usage={'voiceSeconds': seconds}, handoff=handoff)

    async def _await_handoff(self, meeting_id, owner):
        deadline = self._now() + self.handoff_timeout
        while self._now() < deadline:
            try:
                handoff = await self.daemon.get_handoff(meeting_id, owner=owner)
            except Exception:
                await self._sleep(self.poll_interval)
                continue
            return handoff.to_dict() if hasattr(handoff, 'to_dict') else dict(handoff)
        return None

    async def instruct(self, call_id, text, *, silent=False):
        # Meetings take guidance through the meeting context and the portal; a
        # live instruction channel for meetings is not implemented yet.
        return False

    async def end(self, call_id):
        meeting_id = self._meetings.get(call_id)
        if meeting_id is None:
            if call_id not in self._starting:
                return False
            # start() cancels the meeting as soon as it exists.
            self._canceled.add(call_id)
            return True
        self._canceled.add(call_id)
        await self.daemon.cancel_meeting(meeting_id)
        return True
