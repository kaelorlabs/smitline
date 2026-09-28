"""Calls: accept a brief, run it on a line (phone or meeting), and publish the result."""
import asyncio
import os

from call_brief import BriefIncomplete, CallBrief
from call_hooks import CallRefused, LineNotReady, MissingCredentials, maybe_await
from call_result import (
    DEFAULT_SUMMARY_MODEL, ResponsesSummarizer, SummaryUnavailable, fallback_result,
    result_from_handoff, result_without_conversation, transcript_entries,
)
from call_store import TERMINAL, CallNotFound, new_call_id


MAX_WAIT_SECONDS = 300
# Long calls keep their opening (disclosure, early details) and the most recent lines.
TRANSCRIPT_HEAD = 60
TRANSCRIPT_TAIL = 340
# Statuses only move forward; a late Twilio callback must not undo progress.
STATUS_RANK = {
    'queued': 0, 'connecting': 1, 'ringing': 2, 'waiting': 2, 'in_progress': 3,
    'summarizing': 4, 'completed': 5, 'failed': 5, 'canceled': 5,
}


class CallError(Exception):
    def __init__(self, status, code, message, **details):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.details = details


class CallContext:
    """What a line uses to report progress on one call."""

    def __init__(self, service, call_id, brief, owner):
        self.service = service
        self.call_id = call_id
        self.brief = brief
        self.owner = owner
        self.transcript = []
        self.ended = False

    def credentials(self, provider):
        return self.service.hooks.credentials(self.owner, provider)

    def set_status(self, status, **fields):
        return self.service._set_status(self.call_id, status, **fields)

    def link(self, **refs):
        record = self.service.store.get(self.call_id)
        line = dict(record.get('line') or {})
        line.update({key: value for key, value in refs.items() if value is not None})
        self.service.store.update(self.call_id, line=line)

    def event(self, event_type, **data):
        self.service._event(self.call_id, event_type, **data)

    def add_transcript(self, speaker, text):
        text = str(text or '').strip()
        if not text:
            return
        entry = {'speaker': speaker, 'text': text}
        self.transcript.append(entry)
        if len(self.transcript) > TRANSCRIPT_HEAD + TRANSCRIPT_TAIL:
            del self.transcript[TRANSCRIPT_HEAD]
        self.service._event(self.call_id, 'call.transcript', **entry)

    async def finish(self, end_reason, *, usage=None, handoff=None, error=None):
        if self.ended:
            return
        self.ended = True
        await self.service._finalize(self, end_reason, usage=usage or {}, handoff=handoff,
                                     error=error)


class CallService:
    def __init__(self, store, *, hooks, lines, summarizer_factory=None, environ=None):
        self.store = store
        self.hooks = hooks
        self.lines = dict(lines or {})
        self._environ = environ
        self._summarizer_factory = summarizer_factory or self._default_summarizer
        self._contexts = {}
        self._tasks = set()
        self._changed = {}

    @property
    def environ(self):
        if self._environ is not None:
            return self._environ
        return getattr(self.hooks, 'environ', os.environ)

    def _default_summarizer(self, owner):
        key = self.hooks.credentials(owner, 'openai')['apiKey']
        model = self.environ.get('COLLEAGUE_SUMMARY_MODEL') or DEFAULT_SUMMARY_MODEL
        return ResponsesSummarizer(key, model=model)

    # Events and status ---------------------------------------------------

    def _signal(self, call_id):
        changed = self._changed.pop(call_id, None)
        if changed is not None:
            changed.set()

    def _event(self, call_id, event_type, **data):
        event = self.store.append_event(call_id, event_type, **data)
        self._signal(call_id)
        return event

    def _set_status(self, call_id, status, **fields):
        record = self.store.get(call_id)
        if record['status'] in TERMINAL or (record['status'] == status and not fields):
            return record
        if STATUS_RANK[status] < STATUS_RANK[record['status']]:
            return record
        changes = dict(fields)
        now = self.store.now()
        if status == 'in_progress' and not record.get('answeredAt'):
            changes['answeredAt'] = now
        if status in TERMINAL:
            changes.setdefault('endedAt', now)
        record = self.store.update(call_id, status=status, **changes)
        public = {key: value for key, value in fields.items() if key in ('endReason', 'detail')}
        self._event(call_id, 'call.status', status=status, **public)
        return record

    # Public operations ---------------------------------------------------

    async def check(self, payload, *, request=None):
        """Everything create() would refuse, reported instead of raised."""
        brief = CallBrief.from_dict(payload)
        owner = await maybe_await(self.hooks.owner_for(request))
        line = self.lines.get(brief.channel)
        problems = []
        if line is None:
            problems.append(f'{brief.channel} calls are not available on this installation')
        else:
            for step in (lambda: self.hooks.precheck(owner, brief),
                         lambda: line.ready(self.hooks, owner, brief)):
                try:
                    await maybe_await(step())
                except MissingCredentials as error:
                    problems.append(str(error))
                except CallRefused as error:
                    problems.append(error.message)
        return {'ok': not problems, 'brief': brief.to_dict(), 'problems': problems}

    def reconcile(self):
        """At startup, close calls a previous daemon left unfinished; nothing will finish them."""
        closed = []
        for record in self.store.list(limit=1000):
            if record['status'] in TERMINAL or record['id'] in self._contexts:
                continue
            self._close_unfinished(record['id'], 'The daemon stopped before the call finished.')
            closed.append(record['id'])
        return closed

    async def create(self, payload, *, request=None):
        brief = CallBrief.from_dict(payload)
        owner = await maybe_await(self.hooks.owner_for(request))
        line = self.lines.get(brief.channel)
        if line is None:
            raise CallError(503, 'channel_unavailable',
                            f'{brief.channel} calls are not available on this installation')
        try:
            await maybe_await(self.hooks.precheck(owner, brief))
            line.ready(self.hooks, owner, brief)
        except CallRefused as error:
            raise CallError(403, error.code, error.message) from error
        except MissingCredentials as error:
            raise CallError(503, 'not_configured', str(error),
                            provider=error.provider, missing=list(error.missing)) from error
        return self._launch(brief, owner, line.start, direction='outbound')

    async def create_inbound(self, brief, owner, start):
        """Record a call someone else placed to us; `start(ctx)` runs it on the line."""
        return self._launch(brief, owner, start, direction='inbound')

    def _launch(self, brief, owner, start, *, direction):
        now = self.store.now()
        record = {
            'version': 1,
            'id': new_call_id(),
            'owner': owner,
            'channel': brief.channel,
            'direction': direction,
            'status': 'queued',
            'brief': brief.to_dict(),
            'createdAt': now,
            'updatedAt': now,
            'line': {},
            'result': None,
            'usage': {},
        }
        self.store.create(record)
        self._event(record['id'], 'call.created', channel=brief.channel, direction=direction)
        context = CallContext(self, record['id'], brief, owner)
        self._contexts[record['id']] = context
        task = asyncio.create_task(self._run(start, context), name='call-' + record['id'])
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return record

    async def _run(self, start, context):
        try:
            await start(context)
        except asyncio.CancelledError:
            # Only shutdown cancels a call task; ending a call goes through the line.
            if not context.ended:
                context.ended = True
                self._close_unfinished(context.call_id, 'The daemon stopped before the call finished.')
                self._contexts.pop(context.call_id, None)
            raise
        except Exception as error:
            await context.finish('error', error=f'{type(error).__name__}: {str(error)[:200]}')

    def get(self, call_id, *, owner=None):
        record = self.store.get(call_id)
        if owner is not None and record.get('owner') != owner:
            raise CallNotFound(call_id)
        return record

    def list(self, *, owner=None, limit=20):
        return self.store.list(owner=owner, limit=limit)

    def events(self, call_id, *, after=None):
        return self.store.events(call_id, after=after)

    async def wait(self, call_id, *, timeout=60, owner=None):
        timeout = max(0.0, min(float(timeout), MAX_WAIT_SECONDS))
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            record = self.get(call_id, owner=owner)
            remaining = deadline - loop.time()
            if record['status'] in TERMINAL or remaining <= 0:
                return record
            changed = self._changed.setdefault(call_id, asyncio.Event())
            try:
                await asyncio.wait_for(changed.wait(), remaining)
            except asyncio.TimeoutError:
                pass

    async def wait_for_change(self, call_id, timeout):
        changed = self._changed.setdefault(call_id, asyncio.Event())
        try:
            await asyncio.wait_for(changed.wait(), timeout)
        except asyncio.TimeoutError:
            pass

    async def instruct(self, call_id, text, *, owner=None):
        record = self.get(call_id, owner=owner)
        if record['status'] in TERMINAL or record['status'] == 'summarizing':
            raise CallError(409, 'conflict', 'the call has already ended')
        text = str(text or '').strip()
        if not text:
            raise CallError(422, 'invalid_request', 'instruction text is required')
        line = self.lines[record['channel']]
        delivered = await line.instruct(call_id, text[:2000])
        self._event(call_id, 'call.instruction', text=text[:2000], delivered=bool(delivered))
        return {'delivered': bool(delivered)}

    async def end(self, call_id, *, owner=None):
        record = self.get(call_id, owner=owner)
        if record['status'] in TERMINAL:
            return record
        line = self.lines[record['channel']]
        ended = await line.end(call_id)
        context = self._contexts.get(call_id)
        if not ended and context is not None and record['status'] in ('queued', 'connecting'):
            await context.finish('canceled')
        return self.store.get(call_id)

    async def transfer(self, call_id, *, owner=None):
        record = self.get(call_id, owner=owner)
        if record['status'] != 'in_progress':
            raise CallError(409, 'conflict', 'only a connected call can be transferred')
        line = self.lines[record['channel']]
        transfer = getattr(line, 'transfer', None)
        if transfer is None:
            raise CallError(409, 'unsupported', f'{record["channel"]} calls cannot be transferred')
        from twilio_client import TwilioError
        try:
            return await transfer(call_id)
        except LineNotReady as error:
            raise CallError(409, 'not_ready', str(error)) from error
        except TwilioError as error:
            raise CallError(502, 'provider_error', f'Twilio refused the transfer: {error.message}') from error

    # Finishing -----------------------------------------------------------

    async def _build_result(self, context, end_reason, usage, handoff):
        record = self.store.get(context.call_id)
        duration = usage.get('voiceSeconds') or 0
        transcript = transcript_entries(context.transcript)
        if handoff is not None:
            return result_from_handoff(handoff, transcript=transcript, duration_seconds=duration)
        if record['channel'] == 'meeting':
            if end_reason == 'canceled' and not duration:
                return result_without_conversation('canceled', duration_seconds=duration)
            return fallback_result(end_reason, transcript, duration, 'no meeting handoff was produced')
        simple =result_without_conversation(end_reason, transcript=transcript,
                                             duration_seconds=duration)
        if simple is not None:
            return simple
        try:
            summarizer = self._summarizer_factory(context.owner)
            result = await summarizer.summarize(record['brief'], transcript,
                                                duration_seconds=duration)
            if end_reason == 'voicemail' and result['outcome'] in ('achieved', 'partial'):
                result['outcome'] = 'voicemail'
            return result
        except (SummaryUnavailable, MissingCredentials) as error:
            return fallback_result(end_reason, transcript, duration, str(error))
        except Exception as error:
            return fallback_result(end_reason, transcript, duration, type(error).__name__)

    async def _finalize(self, context, end_reason, *, usage, handoff, error):
        call_id = context.call_id
        record = self.store.get(call_id)
        if record['status'] in TERMINAL:
            return
        if error and not context.transcript and handoff is None:
            self._set_status(call_id, 'failed', endReason='error', error=error, usage=usage)
        else:
            self._set_status(call_id, 'summarizing', endReason=end_reason)
            try:
                result = await self._build_result(context, end_reason, usage, handoff)
            except asyncio.CancelledError:
                # Shutting down mid-summary: never leave the call in 'summarizing'.
                self._close_unfinished(call_id, 'The daemon stopped while writing the result.')
                self._contexts.pop(call_id, None)
                raise
            if 'summaryTokens' in result:
                usage = dict(usage, summaryTokens=result.pop('summaryTokens'))
            status = 'canceled' if result['outcome'] == 'canceled' else 'completed'
            record = self.store.update(call_id, result=result, usage=usage,
                                       **({'error': error} if error else {}))
            self._event(call_id, 'call.result', outcome=result['outcome'])
            self._set_status(call_id, status, endReason=end_reason)
        self._contexts.pop(call_id, None)
        record = self.store.get(call_id)
        try:
            await maybe_await(self.hooks.record_usage(record['owner'], record, record.get('usage') or {}))
        except Exception:
            self._event(call_id, 'call.usage_failed')
        try:
            delivery = await self.hooks.notify(record['owner'], record)
        except Exception as notify_error:
            delivery = {'delivered': False, 'error': type(notify_error).__name__}
        if delivery is not None:
            self._event(call_id, 'call.webhook', **delivery)

    async def shutdown(self):
        active = list(self._contexts)
        for call_id in active:
            try:
                await self.end(call_id)
            except Exception:
                pass
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        for call_id in active:
            if self.store.get(call_id)['status'] not in TERMINAL:
                self._close_unfinished(call_id, 'The daemon stopped before the call finished.')
        self._contexts.clear()

    def _close_unfinished(self, call_id, message):
        """Mark a call that can no longer finish as failed, keeping what was said."""
        transcript = [{'speaker': e['data'].get('speaker'), 'text': e['data'].get('text')}
                      for e in self.store.events(call_id) if e['type'] == 'call.transcript']
        result = fallback_result('error', transcript, 0, message.rstrip('.').lower())
        result['outcome'] = 'failed'
        self.store.update(call_id, result=result, error=message)
        self._set_status(call_id, 'failed', endReason='error')
