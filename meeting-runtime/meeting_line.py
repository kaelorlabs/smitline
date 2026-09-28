"""Meetings as calls: start an ordinary daemon meeting from a brief and follow it to its handoff."""
import asyncio


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
    return {
        'version': 1,
        'objective': brief.objective,
        'currentTask': f'Take part in this meeting on behalf of {brief.on_behalf_of}.',
        'summary': brief.context or '',
        'decisions': [],
        'constraints': constraints,
        'openQuestions': [],
        'importantFiles': [],
        'recentConversation': [],
    }


def meeting_payload(brief, call_id, workspace):
    agent_session = brief.agent_session or {
        'provider': 'generic',
        'sessionId': call_id,
        'workspace': workspace,
        'metadata': {'continuity': 'context', 'source': 'call-api'},
    }
    return {
        'meetingUrl': brief.to,
        'agentSession': agent_session,
        'context': context_from_brief(brief),
    }


class MeetingLine:
    channel = 'meeting'

    def __init__(self, daemon, *, workspace, poll_interval=2.0, handoff_timeout=120.0,
                 sleep=asyncio.sleep, monotonic=None):
        self.daemon = daemon
        self.workspace = str(workspace)
        self.poll_interval = poll_interval
        self.handoff_timeout = handoff_timeout
        self._sleep = sleep
        self._monotonic = monotonic
        self._meetings = {}
        self._canceled = set()
        self._starting = set()
        self._over_time = set()

    def ready(self, hooks, owner, brief):
        hooks.credentials(owner, 'openai')

    def _now(self):
        if self._monotonic is not None:
            return self._monotonic()
        return asyncio.get_running_loop().time()

    async def start(self, ctx):
        ctx.set_status('connecting')
        self._starting.add(ctx.call_id)
        try:
            session = await self.daemon.create_meeting(
                meeting_payload(ctx.brief, ctx.call_id, self.workspace))
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
            session = await self.daemon.get_meeting(meeting_id)
            if session.state != last:
                last = session.state
                if session.state == 'live' and started is None:
                    started = self._now()
                status = MEETING_STATUS.get(session.state)
                if status:
                    ctx.set_status(status)
            if session.state == 'ended':
                break
            if started is not None and self._now() - started >= limit and ctx.call_id not in self._over_time:
                self._over_time.add(ctx.call_id)
                ctx.event('call.max_duration')
                await self.daemon.cancel_meeting(meeting_id)
            await self._sleep(self.poll_interval)
        handoff = await self._await_handoff(meeting_id)
        seconds = int(self._now() - started) if started is not None else 0
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

    async def _await_handoff(self, meeting_id):
        deadline = self._now() + self.handoff_timeout
        while self._now() < deadline:
            try:
                handoff = await self.daemon.get_handoff(meeting_id)
            except Exception:
                await self._sleep(self.poll_interval)
                continue
            return handoff.to_dict() if hasattr(handoff, 'to_dict') else dict(handoff)
        return None

    async def instruct(self, call_id, text):
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
