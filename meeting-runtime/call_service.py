"""Calls: accept a brief, run it on a line (phone or meeting), and publish the result."""
import asyncio
import collections
import dataclasses
import os
import time

from call_brief import BriefIncomplete, CallBrief
from call_costs import call_cost, spend, with_cost
from call_hooks import (
    LOCAL_OWNER, CallRefused, LineNotReady, MissingCredentials, maybe_await, owner_settings,
)
from call_result import (
    DEFAULT_SUMMARY_MODEL, ResponsesSummarizer, SummaryUnavailable, fallback_result,
    result_from_handoff, result_without_conversation, transcript_entries,
)
from call_requests import (
    CONTEXT_LINES, KINDS, MAX_ASKED_BY, MAX_CONTEXT_LINE, MAX_DETAILS, MAX_REPLY_TEXT,
    MAX_REQUEST_TEXT, MAX_REQUESTS, MAX_STATUSES, OPEN_STATES, REQUEST_ID, chat_message, clean,
    clean_block, done_note, late_note, retell_note, status_note, told,
)
from call_store import TERMINAL, CallNotFound, new_call_id


MAX_WAIT_SECONDS = 300
# Following a call live: once something new is said, wait this long for more before answering,
# so one answer carries a whole exchange rather than a line.
LIVE_SETTLE_SECONDS = 2.0
# Events that wake a live wait: what was said, and status changes.
LIVE_EVENTS = frozenset({'call.transcript', 'call.status'})
# A request from the call wakes a live wait at once, without settling.
REQUEST_EVENTS = frozenset({'call.request'})
# An agent counts as following a call while it waits on it live, and this many seconds after
# its last contact (a wait, a reply): long enough to act on what a wait brought back.
FOLLOW_GRACE = 90.0
# Seconds a request may wait to be picked up before the voice is told it has not been.
PICKUP_SECONDS = 30.0
# Seconds after an answer reached the voice by which it should have been said on the call; if
# it was not (the voice was cut off, or moved on), it is given to the voice once more.
RETELL_AFTER = 35.0
# Extra seconds after that for an answer the voice is still in the middle of saying: the
# transcript has a sentence only once it is finished.
RETELL_GRACE = 10.0
# Seconds between checks for the phone provider's price after a call ends; providers fill
# it in within a minute or two, sometimes later.
PRICE_CHECK_DELAYS = (20, 60, 300, 1800)
# Older calls whose price is looked up in the background when the call list is read.
PRICE_BACKFILL_LIMIT = 50
# Long calls keep their opening (disclosure, early details) and the most recent lines.
TRANSCRIPT_HEAD = 60
TRANSCRIPT_TAIL = 340
# Statuses only move forward; a late Twilio callback must not undo progress.
STATUS_RANK = {
    'queued': 0, 'connecting': 1, 'ringing': 2, 'waiting': 2, 'in_progress': 3,
    'summarizing': 4, 'completed': 5, 'failed': 5, 'canceled': 5,
}
# Seconds a stop gives calls in progress to end (a phone call hangs up) and report what they
# used before what is left is cut off (SMITLINE_SHUTDOWN_GRACE).
SHUTDOWN_GRACE = 5.0
# Notes for later calls: one note's length, and what one call may start with.
MAX_NOTE = 1200
MAX_CARRIED = 10
MAX_CARRIED_CHARS = 4000


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
        # The agent's recent lines, with when they arrived: did it say what it was told to?
        self.agent_lines = collections.deque(maxlen=80)
        self.ended = False
        # Outbound phone calls: whether the opening was heard to say it is an AI (None: not heard).
        self.disclosure = None

    def credentials(self, provider):
        return self.service.hooks.credentials(self.owner, provider)

    def settings(self):
        """The owner's settings (name, phone, voice, recording), not the installation's."""
        return owner_settings(self.service.hooks, self.owner)

    def profile(self):
        """The owner's profile (level-1 context); empty when the hooks keep none."""
        getter = getattr(self.service.hooks, 'profile', None)
        if getter is None:
            return {}
        try:
            return getter(self.owner) or {}
        except Exception:
            return {}

    def set_status(self, status, **fields):
        return self.service._set_status(self.call_id, status, **fields)

    def link(self, **refs):
        record = self.service.store.get(self.call_id)
        line = dict(record.get('line') or {})
        line.update({key: value for key, value in refs.items() if value is not None})
        self.service.store.update(self.call_id, line=line)

    def event(self, event_type, **data):
        self.service._event(self.call_id, event_type, **data)

    def request(self, message):
        """A request message from the voice (call_requests.RequestDesk): ask, add, or cancel."""
        return self.service.note_request(self.call_id, message)

    def agent_following(self):
        """Whether the agent that started the call follows it now, so can answer requests."""
        return self.service.agent_following(self.call_id)

    def add_transcript(self, speaker, text):
        text = str(text or '').strip()
        if not text:
            return
        entry = {'speaker': speaker, 'text': text}
        if speaker == 'agent':
            self.agent_lines.append((time.monotonic(), text))
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


def carry_note(record):
    """What a finished call offers later calls: the agent's note, else its result's summary and
    details. None when it has neither."""
    note = record.get('carryNote') or {}
    if note.get('text'):
        return {'text': note['text'], 'by': 'agent'}
    result = record.get('result') or {}
    parts = [str(result.get('summary') or '').strip()]
    parts += [f'{item["label"]}: {item["value"]}' for item in result.get('details') or ()
              if isinstance(item, dict) and item.get('label') and item.get('value')]
    text = ' '.join(part for part in parts if part)
    return {'text': text[:MAX_NOTE], 'by': 'result'} if text else None


def note_line(item):
    """One carried note as the voice and the backend read it."""
    when = str(item.get('at') or '')[:10]
    return f'{item["contact"]}{f" ({when})" if when else ""}: {item["text"]}'


class CallService:
    def __init__(self, store, *, hooks, lines, summarizer_factory=None, environ=None,
                 price_check_delays=PRICE_CHECK_DELAYS, shutdown_grace=SHUTDOWN_GRACE,
                 follow_grace=FOLLOW_GRACE, pickup_seconds=PICKUP_SECONDS, clock=time.monotonic,
                 retell_after=RETELL_AFTER, retell_grace=RETELL_GRACE):
        self.store = store
        self.retell_after = retell_after
        self.retell_grace = retell_grace
        self.follow_grace = follow_grace
        self.pickup_seconds = pickup_seconds
        self._clock = clock
        self._waiting = {}  # call id -> live waits in progress
        self._followed = {}  # call id -> clock time of the agent's last contact
        self.price_check_delays = tuple(price_check_delays)
        self._price_checked = set()
        self.hooks = hooks
        self.lines = dict(lines or {})
        self._environ = environ
        self._summarizer_factory = summarizer_factory or self._default_summarizer
        self.shutdown_grace = shutdown_grace
        self._contexts = {}
        self._tasks = set()
        self._call_tasks = {}  # call id -> the task running it on its line
        self._changed = {}
        # Calls closed before they could finish (a stop, a restart): their usage is still
        # recorded, once each, so a hosted deployment settles what call_started held.
        self._unsettled = []

    @property
    def environ(self):
        if self._environ is not None:
            return self._environ
        return getattr(self.hooks, 'environ', os.environ)

    def _default_summarizer(self, owner):
        key = self.hooks.credentials(owner, 'openai')['apiKey']
        model = self.environ.get('SMITLINE_SUMMARY_MODEL') or DEFAULT_SUMMARY_MODEL
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

    async def owner_for(self, request):
        """The owner a request acts for; a refusal (no owner, a bad one) becomes a CallError."""
        try:
            return await maybe_await(self.hooks.owner_for(request))
        except CallRefused as error:
            raise CallError(error.status, error.code, error.message, **error.details) from error

    def settings(self, owner):
        return owner_settings(self.hooks, owner)

    def _brief(self, owner, payload):
        """Parse a brief after filling in what setup already knows (the owner's name, phone)."""
        if isinstance(payload, dict):
            defaults = getattr(self.hooks, 'brief_defaults', None)
            filled = dict(defaults(owner, payload) or {}) if defaults else {}
            filled.update({key: value for key, value in payload.items() if value not in (None, '')})
            payload = filled
        return CallBrief.from_dict(payload, environ=self.settings(owner))

    async def check(self, payload, *, request=None):
        """Everything create() would refuse, reported instead of raised."""
        owner = await self.owner_for(request)
        brief = self._brief(owner, payload)
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
        # How many earlier calls' notes the call would start with.
        try:
            carried = len(self._carried(owner, brief))
        except CallError as error:
            problems.append(error.message)
            carried = 0
        return {'ok': not problems, 'brief': brief.to_dict(), 'problems': problems, 'carried': carried}

    def reconcile(self):
        """At startup, close calls a previous daemon left unfinished; nothing will finish them.
        Their usage is recorded as for any finished call."""
        closed = []
        for record in self.store.list(limit=1000):
            if record['status'] in TERMINAL or record['id'] in self._contexts:
                continue
            self._close_unfinished(record['id'], 'The daemon stopped before the call finished.')
            closed.append(record['id'])
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return closed  # No event loop yet: shutdown records them.
        if closed:
            self._spawn(self._settle_unfinished(), name='settle-unfinished-calls')
        return closed

    async def create(self, payload, *, request=None):
        owner = await self.owner_for(request)
        brief = self._brief(owner, payload)
        line = self.lines.get(brief.channel)
        if line is None:
            raise CallError(503, 'channel_unavailable',
                            f'{brief.channel} calls are not available on this installation')
        try:
            await maybe_await(self.hooks.precheck(owner, brief))
            line.ready(self.hooks, owner, brief)
        except CallRefused as error:
            raise CallError(error.status, error.code, error.message, **error.details) from error
        except MissingCredentials as error:
            raise CallError(503, 'not_configured', str(error),
                            provider=error.provider, missing=list(error.missing)) from error
        # Outbound calls only: an incoming caller ID can be faked, so no earlier call is read to it.
        carried = self._carried(owner, brief)
        if carried:
            brief = dataclasses.replace(brief, carried=tuple(note_line(item) for item in carried))
        return await self._launch(brief, owner, line.start, direction='outbound', carried=carried)

    async def create_inbound(self, brief, owner, start):
        """Record a call someone else placed to us; `start(ctx)` runs it on the line."""
        return await self._launch(brief, owner, start, direction='inbound')

    async def _launch(self, brief, owner, start, *, direction, carried=()):
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
        if carried:
            # Exactly what this call knew of earlier calls, for the console and the result.
            record['carried'] = list(carried)
        self.store.create(record)
        self._event(record['id'], 'call.created', channel=brief.channel, direction=direction)
        await self._call_started(owner, record)
        context = CallContext(self, record['id'], brief, owner)
        self._contexts[record['id']] = context
        task = self._spawn(self._run(start, context), name='call-' + record['id'])
        self._call_tasks[record['id']] = task
        task.add_done_callback(lambda _done, call_id=record['id']: self._call_tasks.pop(call_id, None))
        return record

    async def _call_started(self, owner, record):
        """Tell the hooks a call is about to start (a hosted service places a balance hold).

        A refusal, or any failure, ends the call before its line starts, so no record is left
        waiting for a line that never ran.
        """
        started = getattr(self.hooks, 'call_started', None)
        if started is None:
            return
        try:
            await maybe_await(started(owner, record))
        except CallRefused as error:
            self._refuse(record['id'], error.code, error.message)
            raise CallError(error.status, error.code, error.message, **error.details) from error
        except Exception as error:
            self._event(record['id'], 'call.started_hook_failed', error=type(error).__name__)
            self._refuse(record['id'], 'call_start_failed', 'The call could not be started.')
            raise CallError(503, 'call_start_failed', 'The call could not be started.') from error

    def _refuse(self, call_id, code, message):
        self.store.update(call_id, error=message)
        self._set_status(call_id, 'failed', endReason='refused', detail=code)

    def _spawn(self, coroutine, name=None):
        task = asyncio.create_task(coroutine, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

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

    def active_count(self, *, direction=None, owner=None):
        """Calls in progress, of one direction and one owner when given."""
        count = 0
        for call_id in list(self._contexts):
            try:
                record = self.store.get(call_id)
            except CallNotFound:
                continue
            if direction is not None and record.get('direction') != direction:
                continue
            if owner is not None and record.get('owner') != owner:
                continue
            count += 1
        return count

    def attach_recording(self, call_id, *, sid, url, seconds=None):
        """Twilio finished a recording. Fetching it needs the Twilio account credentials."""
        recording = {'sid': sid, 'url': str(url) + '.mp3'}
        try:
            recording['seconds'] = int(seconds)
        except (TypeError, ValueError):
            pass
        self.store.update(call_id, recording=recording)
        self._event(call_id, 'call.recording', **recording)

    async def recording(self, call_id, *, owner, fmt='wav'):
        """A recorded call's audio from the phone provider, as (bytes, content type)."""
        if fmt not in ('wav', 'mp3'):
            raise CallError(422, 'invalid_request', 'format must be wav or mp3')
        record = self.get(call_id, owner=owner)
        fetch = getattr(self.lines.get(record['channel']), 'recording_audio', None)
        if not record.get('recording') or fetch is None:
            if record['status'] not in TERMINAL:
                raise CallError(409, 'not_ready', 'The recording is ready a minute or so after '
                                'the call ends.')
            raise CallError(404, 'no_recording', 'This call has no recording. Recorded calls get '
                            'one a minute or so after they end; to record a call, start it with '
                            'record: true.')
        from twilio_client import TwilioError
        try:
            body, content_type = await fetch(
                lambda provider: self.hooks.credentials(record['owner'], provider), record, fmt)
        except LineNotReady as error:
            raise CallError(409, 'not_ready', str(error)) from error
        except MissingCredentials as error:
            raise CallError(503, 'not_configured', str(error)) from error
        except TwilioError as error:
            raise CallError(502, 'provider_error', 'The phone provider did not return the '
                            f'recording ({error.status}).') from error
        default = 'audio/wav' if fmt == 'wav' else 'audio/mpeg'
        return body, (content_type.split(';')[0].strip() or default)

    def profile(self, owner):
        getter = getattr(self.hooks, 'profile', None)
        if getter is None:
            raise CallError(501, 'unsupported', 'these call hooks keep no profile')
        return getter(owner) or {}

    def update_profile(self, owner, update):
        from briefing import merge_profile
        saver = getattr(self.hooks, 'save_profile', None)
        if saver is None:
            raise CallError(501, 'unsupported', 'these call hooks keep no profile')
        try:
            merged = merge_profile(self.profile(owner), update)
        except ValueError as error:
            raise CallError(422, 'invalid_request', str(error)) from error
        return saver(owner, merged)

    def do_not_call(self, owner):
        getter = getattr(self.hooks, 'do_not_call', None)
        if getter is None:
            raise CallError(501, 'unsupported', 'these call hooks keep no do-not-call list')
        return getter(owner)

    def update_do_not_call(self, owner, update):
        updater = getattr(self.hooks, 'update_do_not_call', None)
        if updater is None:
            raise CallError(501, 'unsupported', 'these call hooks keep no do-not-call list')
        try:
            return updater(owner, update)
        except ValueError as error:
            raise CallError(422, 'invalid_request', str(error)) from error

    # Contacts, tasks, and notes for later calls ------------------------------

    def _owner_contact_book(self, owner):
        """hooks.contact_book(owner); hooks written before owners keep one book attribute,
        which is the local owner's: another owner has none."""
        book = getattr(self.hooks, 'contact_book', None)
        if callable(book):
            return book(owner)
        return book if owner == LOCAL_OWNER else None

    def _contact_book(self, owner):
        book = self._owner_contact_book(owner)
        if book is None:
            raise CallError(501, 'unsupported', 'these call hooks keep no contacts')
        return book

    def _saved_contacts(self, owner):
        """{number: what the user saved}; empty when the hooks keep no contacts."""
        from contacts import ContactsUnreadable
        book = self._owner_contact_book(owner)
        if book is None:
            return {}
        try:
            return {entry['number']: entry for entry in book.entries() if entry.get('number')}
        except ContactsUnreadable as error:
            raise CallError(503, 'contacts_unreadable', str(error)) from error

    def _profile_or_empty(self, owner):
        getter = getattr(self.hooks, 'profile', None)
        try:
            return (getter(owner) or {}) if getter else {}
        except Exception:
            return {}

    def _contact_name(self, number, saved, profile, records=()):
        """The saved name, else a profile person with this number, else the latest brief's name."""
        from briefing import contact_for
        name = (saved.get(number) or {}).get('name') or (contact_for(profile, number) or {}).get('name')
        if name:
            return name
        for record in records:
            briefed = ((record.get('brief') or {}).get('contact') or {}).get('name')
            if briefed:
                return briefed
        return ''

    def _phone_records(self, owner):
        return [record for record in self.store.list(owner=owner, limit=None)
                if record.get('channel') == 'phone' and (record.get('brief') or {}).get('to')]

    def _notes(self, records, saved, profile, by_number):
        """Notes of finished calls, newest first, within MAX_CARRIED and MAX_CARRIED_CHARS."""
        notes, total = [], 0
        for record in records:
            if record['status'] not in TERMINAL:
                continue
            note = carry_note(record)
            if note is None:
                continue
            if len(notes) >= MAX_CARRIED or total + len(note['text']) > MAX_CARRIED_CHARS:
                break
            brief = record.get('brief') or {}
            if record.get('channel') == 'phone':
                number = brief.get('to')
                label = self._contact_name(number, saved, profile, by_number.get(number, ())) or number
            else:
                label = 'Meeting'
            notes.append({'callId': record['id'], 'contact': label,
                          'at': record.get('endedAt') or record.get('createdAt'),
                          'text': note['text'], 'by': note['by']})
            total += len(note['text'])
        return notes

    def _carried(self, owner, brief):
        """The notes of earlier calls an outbound call starts with; [] when it asks for none.

        By task and by call only when the brief asks (carryFrom); by contact when it asks, or
        when the user switched on the contact's autoContext and the brief does not say false.
        """
        wanted = brief.carry_from or {}
        saved = self._saved_contacts(owner) if brief.channel == 'phone' else {}
        automatic = bool((saved.get(brief.to) or {}).get('autoContext'))
        by_contact = brief.channel == 'phone' and wanted.get('contact', automatic)
        by_task = bool(wanted.get('task') and brief.task)
        named = wanted.get('calls') or []
        if not (by_contact or by_task or named):
            return []
        records = self.store.list(owner=owner, limit=None)
        known = {record['id'] for record in records}
        missing = [call_id for call_id in named if call_id not in known]
        if missing:
            raise CallError(422, 'invalid_request', 'carryFrom.calls names calls that do not exist: '
                            + ', '.join(missing))
        task_id = (brief.task or {}).get('id')

        def wanted_record(record):
            earlier = record.get('brief') or {}
            return (record['id'] in named
                    or (by_task and (earlier.get('task') or {}).get('id') == task_id)
                    or (by_contact and record.get('channel') == 'phone' and earlier.get('to') == brief.to))
        chosen = [record for record in records if wanted_record(record)]
        by_number = {}
        for record in records:
            if record.get('channel') == 'phone':
                by_number.setdefault((record.get('brief') or {}).get('to'), []).append(record)
        return self._notes(chosen, saved, self._profile_or_empty(owner), by_number)

    def save_note(self, call_id, text, *, owner):
        """Save a note on a finished call for later calls to start with."""
        record = self.get(call_id, owner=owner)
        if record['status'] not in TERMINAL:
            raise CallError(409, 'conflict', 'a note can be saved once the call has ended')
        if not isinstance(text, str) or not text.strip():
            raise CallError(422, 'invalid_request', 'text is required')
        text = ' '.join(text.split())
        if len(text) > MAX_NOTE:
            raise CallError(422, 'invalid_request', f'text must be at most {MAX_NOTE} characters')
        return with_cost(self.store.update(call_id, carryNote={'text': text, 'by': 'agent',
                                                               'at': self.store.now()}),
                         self.environ)

    def _contact_entry(self, number, records, saved, profile):
        entry = saved.get(number) or {}
        latest = records[0] if records else {}
        return {
            'number': number,
            'name': self._contact_name(number, saved, profile, records),
            # Whether the user saved anything for this number (a name, notes, or the setting).
            'saved': bool(entry),
            'notes': entry.get('notes', ''),
            'autoContext': bool(entry.get('autoContext')),
            'calls': len(records),
            'lastCallAt': latest.get('createdAt'),
            'lastObjective': (latest.get('brief') or {}).get('objective'),
        }

    def contacts(self, owner):
        """Every number called or calling, with what the user saved about it, latest call first."""
        saved = self._saved_contacts(owner)
        profile = self._profile_or_empty(owner)
        by_number = {}
        for record in self._phone_records(owner):
            by_number.setdefault(record['brief']['to'], []).append(record)
        numbers = list(by_number) + [number for number in saved if number not in by_number]
        entries = [self._contact_entry(number, by_number.get(number, []), saved, profile) for number in numbers]
        entries.sort(key=lambda item: item['lastCallAt'] or '', reverse=True)
        return {'contacts': entries}

    def contact(self, owner, number):
        """One contact, its calls, and the notes a new call to it would start with."""
        from call_brief import normalize_phone
        try:
            number = normalize_phone(number, 'number')
        except ValueError as error:
            raise CallError(422, 'invalid_request', str(error)) from error
        saved = self._saved_contacts(owner)
        profile = self._profile_or_empty(owner)
        records = [record for record in self._phone_records(owner) if record['brief']['to'] == number]
        if not records and number not in saved:
            raise CallError(404, 'not_found', 'no calls to or from this number, and nothing saved for it')
        entry = self._contact_entry(number, records, saved, profile)
        entry['history'] = [{
            'id': record['id'], 'direction': record.get('direction'), 'status': record['status'],
            'createdAt': record.get('createdAt'), 'objective': (record.get('brief') or {}).get('objective'),
            'task': (record.get('brief') or {}).get('task'),
            'outcome': (record.get('result') or {}).get('outcome'),
            'summary': (record.get('result') or {}).get('summary'),
        } for record in records]
        entry['nextCall'] = self._notes(records, saved, profile, {number: records})
        return entry

    def update_contact(self, owner, number, changes):
        from contacts import ContactsUnreadable
        try:
            self._contact_book(owner).update(number, changes)
        except ContactsUnreadable as error:
            raise CallError(503, 'contacts_unreadable', str(error)) from error
        except ValueError as error:
            raise CallError(422, 'invalid_request', str(error)) from error
        return self.contact(owner, number)

    def forget_contact(self, owner, number):
        """Drop the saved name, notes, and setting. Call records, and their notes, stay."""
        from contacts import ContactsUnreadable
        try:
            forgotten = self._contact_book(owner).forget(number)
        except ContactsUnreadable as error:
            raise CallError(503, 'contacts_unreadable', str(error)) from error
        except ValueError as error:
            raise CallError(422, 'invalid_request', str(error)) from error
        return {'forgotten': forgotten}

    def _honor_do_not_call(self, record, result):
        """The person asked not to be called again: put their number on the do-not-call list."""
        brief = record.get('brief') or {}
        if not result.get('doNotCall') or record['channel'] != 'phone' or \
                record.get('direction') != 'outbound' or brief.get('rehearsal'):
            return
        updater = getattr(self.hooks, 'update_do_not_call', None)
        if updater is None:
            return
        try:
            updater(record['owner'], {'add': [{
                'number': brief['to'], 'callId': record['id'],
                'reason': 'Asked on a call not to be called again.',
            }]})
        except Exception as error:
            self._event(record['id'], 'call.do_not_call_failed', error=type(error).__name__)
            return
        self._event(record['id'], 'call.do_not_call', number=brief['to'])

    def get(self, call_id, *, owner):
        """One of the owner's calls; another owner's call is not found."""
        record = self.store.get(call_id)
        if record.get('owner') != owner:
            raise CallNotFound(call_id)
        return with_cost(record, self.environ)

    def list(self, *, owner, limit=20):
        """The owner's calls, newest first; limit=None lists every call."""
        if owner is None:
            raise ValueError('owner is required')
        return [with_cost(record, self.environ)
                for record in self.store.list(owner=owner, limit=limit)]

    @staticmethod
    def spend(records, *, tz_offset_minutes=0):
        return spend(records, tz_offset_minutes=tz_offset_minutes)

    # Phone prices ----------------------------------------------------------

    def _needs_price(self, record):
        line = self.lines.get(record.get('channel'))
        return (record.get('status') in TERMINAL
                and getattr(line, 'provider_price', None) is not None
                and bool((record.get('line') or {}).get('providerCallSid'))
                and 'phonePrice' not in (record.get('usage') or {}))

    async def _settle_price(self, call_id):
        for delay in self.price_check_delays:
            await asyncio.sleep(delay)
            if await self._check_price(call_id):
                return

    async def _check_price(self, call_id):
        """Record the provider's price for a call; True once it is recorded or never will be."""
        record = self.store.get(call_id)
        if not self._needs_price(record):
            return True
        owner = record.get('owner')
        try:
            price = await self.lines[record['channel']].provider_price(
                lambda provider: self.hooks.credentials(owner, provider), record)
        except Exception:
            return False
        if price is None:
            return False
        usage = dict(record.get('usage') or {}, phonePrice=price)
        self.store.update(call_id, usage=usage,
                          cost=call_cost(dict(record, usage=usage), self.environ))
        self._signal(call_id)
        return True

    def backfill_prices(self, records):
        """Look up, once per run, the provider's price for finished calls that lack it."""
        due = [record['id'] for record in records
               if record['id'] not in self._price_checked and self._needs_price(record)]
        due = due[:PRICE_BACKFILL_LIMIT]
        if not due:
            return
        self._price_checked.update(due)

        async def run():
            for call_id in due:
                try:
                    await self._check_price(call_id)
                except CallNotFound:
                    pass
        self._spawn(run())

    def events(self, call_id, *, after=None):
        return self.store.events(call_id, after=after)

    async def wait(self, call_id, *, owner, timeout=60, since=None, only=None):
        """The call once it is terminal or the timeout passes. With `since` (an event cursor,
        '' for the start), it also comes back once something new was said, with `live`: the
        transcript lines and requests after the cursor, and the cursor to pass next time. A
        request comes back at once. only='requests' comes back for requests alone and leaves out
        the transcript, for long meetings. While an agent waits live, it follows the call."""
        timeout = max(0.0, min(float(timeout), MAX_WAIT_SECONDS))
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        settle_until = None
        live = since is not None
        self.get(call_id, owner=owner)
        if live:
            self._waiting[call_id] = self._waiting.get(call_id, 0) + 1
        try:
            while True:
                record = self.get(call_id, owner=owner)
                remaining = deadline - loop.time()
                if live and record['status'] not in TERMINAL:
                    events = self.events(call_id, after=since or None)
                    if any(event['type'] in REQUEST_EVENTS for event in events):
                        remaining = 0
                    elif (only != 'requests' and settle_until is None
                          and any(event['type'] in LIVE_EVENTS for event in events)):
                        settle_until = min(deadline, loop.time() + LIVE_SETTLE_SECONDS)
                if settle_until is not None:
                    remaining = min(remaining, settle_until - loop.time())
                if record['status'] in TERMINAL or remaining <= 0:
                    return record if not live else dict(record, live=self._live(call_id, since, only))
                changed = self._changed.setdefault(call_id, asyncio.Event())
                try:
                    await asyncio.wait_for(changed.wait(), remaining)
                except asyncio.TimeoutError:
                    pass
        finally:
            if live:
                self._waiting[call_id] -= 1
                if not self._waiting[call_id]:
                    del self._waiting[call_id]
                if call_id in self._contexts:
                    self._followed[call_id] = self._clock()

    def _live(self, call_id, since, only=None):
        """What was said and asked after the cursor `since`, and the cursor for next time."""
        events = self.events(call_id, after=since or None)
        cursor = events[-1]['id'] if events else (since or '0')
        live = {'cursor': cursor}
        if only != 'requests':
            live['transcript'] = [{'speaker': event['data'].get('speaker'),
                                   'text': event['data'].get('text'), 'at': event.get('at')}
                                  for event in events if event['type'] == 'call.transcript']
        live['requests'] = [dict(event['data'], at=event.get('at'))
                            for event in events if event['type'] in REQUEST_EVENTS]
        return live

    async def wait_for_change(self, call_id, timeout):
        changed = self._changed.setdefault(call_id, asyncio.Event())
        try:
            await asyncio.wait_for(changed.wait(), timeout)
        except asyncio.TimeoutError:
            pass

    def listen(self, call_id, *, owner):
        """What to stream to the owner listening in: an object whose chunks() yields the
        format, then each side's audio until the call ends. Off unless the owner's
        SMITLINE_LISTEN=1, because people on the call may need to agree to being heard."""
        record = self.get(call_id, owner=owner)
        if owner_settings(self.hooks, owner).get('SMITLINE_LISTEN') != '1':
            raise CallError(403, 'listening_off',
                            'Listening in is off. Set SMITLINE_LISTEN=1 where the people you call '
                            'agree to it, or local law allows it.')
        if record['status'] in TERMINAL:
            raise CallError(409, 'call_ended', 'The call has ended.')
        line = self.lines.get(record['channel'])
        source = getattr(line, 'listen', None)
        tap = source(call_id) if source is not None else None
        if tap is None:
            raise CallError(409, 'listen_unavailable',
                            'This call cannot be heard yet: it has not connected, or its audio '
                            'does not pass through Smitline (direct SIP).')
        self._event(call_id, 'call.listening')
        return tap

    async def instruct(self, call_id, text, *, owner, silent=False):
        record = self.get(call_id, owner=owner)
        if record['status'] in TERMINAL or record['status'] == 'summarizing':
            raise CallError(409, 'conflict', 'the call has already ended')
        text = str(text or '').strip()
        if not text:
            raise CallError(422, 'invalid_request', 'instruction text is required')
        line = self.lines[record['channel']]
        # Lines that predate silent notes take (call_id, text); pass the flag only when set.
        delivered = await (line.instruct(call_id, text[:2000], silent=True) if silent
                           else line.instruct(call_id, text[:2000]))
        self._event(call_id, 'call.instruction', text=text[:2000], delivered=bool(delivered),
                    silent=bool(silent))
        return {'delivered': bool(delivered)}

    # Requests (call_requests.py) -----------------------------------------

    def agent_following(self, call_id):
        if self._waiting.get(call_id):
            return True
        seen = self._followed.get(call_id)
        return seen is not None and self._clock() - seen <= self.follow_grace

    def _change_request(self, call_id, request_id, change):
        """Apply change(request) to one of the call's requests and store it; the request after."""
        record = self.store.get(call_id)
        requests = [dict(item) for item in record.get('requests') or ()]
        for request in requests:
            if request['id'] == request_id:
                change(request)
                request['updatedAt'] = self.store.now()
                self.store.update(call_id, requests=requests)
                return request
        return None

    def note_request(self, call_id, message):
        """A request message from the voice: a new request (ask), more for one (add), or a cancel.
        Ignored (None) when the brief gave the agent nothing to do, or the message does not fit."""
        record = self.store.get(call_id)
        if not (record.get('brief') or {}).get('canDo') or not isinstance(message, dict):
            return None
        action, request_id = message.get('type'), message.get('id')
        if not isinstance(request_id, str) or not REQUEST_ID.fullmatch(request_id):
            return None
        requests = record.get('requests') or []
        existing = next((item for item in requests if item['id'] == request_id), None)
        text = clean(message.get('text'), MAX_REQUEST_TEXT)
        now = self.store.now()
        if action == 'ask':
            limit = MAX_REQUESTS.get(record.get('channel'), MAX_REQUESTS['phone'])
            if existing is not None or len(requests) >= limit or not text:
                return None
            request = {'id': request_id,
                       'kind': message.get('kind') if message.get('kind') in KINDS else 'question',
                       'text': text, 'state': 'open', 'openedAt': now, 'updatedAt': now}
            asked_by = clean(message.get('askedBy'), MAX_ASKED_BY)
            if asked_by:
                request['askedBy'] = asked_by
            if message.get('source') == 'speech':
                # The voice said it passed this on without asking its backend: what they said.
                request['source'] = 'speech'
            context = message.get('context')
            if not isinstance(context, list):
                # The phone line sends none: the call's own last lines.
                said = self._contexts[call_id].transcript if call_id in self._contexts else ()
                context = [f'{entry["speaker"]}: {entry["text"]}' for entry in said[-CONTEXT_LINES:]]
            context = [line for line in (clean(line, MAX_CONTEXT_LINE)
                                         for line in context[-CONTEXT_LINES:]) if line]
            if context:
                request['context'] = context
            request['thread'] = [{'from': 'call', 'type': 'ask', 'text': text, 'at': now}]
            self.store.update(call_id, requests=list(requests) + [request])
            self._event(call_id, 'call.request', type='ask',
                        **{key: request[key] for key in ('id', 'kind', 'text', 'askedBy', 'context',
                                                         'source') if key in request})
            if self.agent_following(call_id):
                self._spawn(self._check_pickup(call_id, request_id))
            return request
        if action not in ('add', 'cancel') or existing is None or existing['state'] not in OPEN_STATES:
            return None
        if action == 'add' and (not text or sum(1 for entry in existing.get('thread') or ()
                                                if entry['from'] == 'call') > MAX_STATUSES):
            return None

        def change(request):
            entry = {'from': 'call', 'type': action, 'at': now}
            if text:
                entry['text'] = text
            request['thread'] = list(request.get('thread') or ()) + [entry]
            if action == 'cancel':
                request['state'] = 'canceled'
        request = self._change_request(call_id, request_id, change)
        self._event(call_id, 'call.request', type=action, id=request_id,
                    **({'text': text} if text else {}))
        return request

    async def _check_pickup(self, call_id, request_id):
        await asyncio.sleep(self.pickup_seconds)
        record = self.store.get(call_id)
        request = next((item for item in record.get('requests') or ()
                        if item['id'] == request_id), None)
        if (request is None or request['state'] != 'open' or record['status'] in TERMINAL
                or record['status'] == 'summarizing'):
            return
        self._event(call_id, 'call.request_late', id=request_id)
        await self._deliver(record, late_note(request, owner=record['brief'].get('onBehalfOf')))

    async def _check_told(self, call_id, request_id, text, sent_at):
        """Retell an answer once when, RETELL_AFTER seconds after the voice got it, nothing it
        said carried it: it was cut off, or never said it. A sentence still being said then
        gets up to RETELL_GRACE seconds to finish."""
        await asyncio.sleep(self.retell_after)
        waited = 0.0
        while True:
            context = self._contexts.get(call_id)
            record = self.store.get(call_id)
            if context is None or record['status'] != 'in_progress':
                return
            said = ' '.join(line for at, line in context.agent_lines if at >= sent_at)
            if told(text, said):
                return
            if waited >= self.retell_grace:
                break
            step = min(0.5, self.retell_grace - waited)
            await asyncio.sleep(step)
            waited += step
        request = next((item for item in record.get('requests') or ()
                        if item['id'] == request_id), None)
        if request is None:
            return
        delivered = await self._deliver(record, retell_note(
            request, text, owner=record['brief'].get('onBehalfOf')))
        self._event(call_id, 'call.request_retold', id=request_id, delivered=delivered)

    async def _deliver(self, record, note):
        """Give the voice a note; False when the line could not (the call ended, no channel)."""
        line = self.lines.get(record['channel'])
        try:
            deliver = getattr(line, 'deliver', None)
            if deliver is not None:
                return bool(await deliver(record['id'], note))
            return bool(await line.instruct(record['id'], note['text'],
                                            silent=note['kind'] == 'thinking'))
        except Exception:
            return False

    async def reply(self, call_id, request_id, *, owner, type, text, announce=False, ok=True,
                    details=None):
        """The agent answers a request: status (any number; the first says it is picked up) or
        done (once; it closes the request). The voice gets it while the call runs; done is still
        recorded after the call ends."""
        record = self.get(call_id, owner=owner)
        if call_id in self._contexts:
            self._followed[call_id] = self._clock()
        request = next((item for item in record.get('requests') or ()
                        if item['id'] == request_id), None)
        if request is None:
            raise CallError(404, 'request_not_found', f'request {request_id} not found on this call')
        if type not in ('status', 'done'):
            raise CallError(422, 'invalid_request', 'type must be status or done')
        text = clean(text, MAX_REPLY_TEXT)
        if not text:
            raise CallError(422, 'invalid_request', 'text is required')
        details = clean_block(details, MAX_DETAILS) or None
        if request['state'] not in OPEN_STATES:
            raise CallError(409, 'request_closed',
                            f'request {request_id} is already {request["state"]}')
        ended = record['status'] in TERMINAL or record['status'] == 'summarizing'
        if type == 'status' and ended:
            raise CallError(409, 'call_ended', 'The call has ended. Send done to record the answer.')
        if type == 'status' and sum(1 for entry in request.get('thread') or ()
                                    if entry.get('type') == 'status') >= MAX_STATUSES:
            raise CallError(429, 'too_many_updates',
                            f'request {request_id} has had {MAX_STATUSES} status updates; send done')
        owner_name = record['brief'].get('onBehalfOf')
        if type == 'status':
            note = status_note(request, text, owner=owner_name, announce=bool(announce))
        else:
            note = done_note(request, text, owner=owner_name, ok=bool(ok))
        if details:
            note['details'] = details
            note['chat'] = chat_message(request, details, owner=owner_name)
        entry = {'from': 'agent', 'type': type, 'text': text, 'at': self.store.now()}
        if type == 'status' and announce:
            entry['announce'] = True
        if type == 'done':
            entry['ok'] = bool(ok)
        if details:
            entry['details'] = details
        position = []

        def change(request):
            # Stored before it is delivered, so a second done for it is refused meanwhile.
            request['thread'] = list(request.get('thread') or ()) + [entry]
            position.append(len(request['thread']) - 1)
            request['state'] = 'done' if type == 'done' else 'working'
            if type == 'done':
                request['ok'] = bool(ok)
                request['answer'] = text
        self._change_request(call_id, request_id, change)
        sent_at = time.monotonic()
        delivered = False if ended else await self._deliver(record, note)
        if delivered and note['kind'] == 'instructions':
            # Something to tell them: check that it was said.
            self._spawn(self._check_told(call_id, request_id, text, sent_at))

        def mark(request):
            request['thread'][position[0]] = dict(request['thread'][position[0]], delivered=delivered)
        request = self._change_request(call_id, request_id, mark)
        self._event(call_id, 'call.request_reply', id=request_id, type=type, text=text,
                    delivered=delivered, **({'announce': True} if entry.get('announce') else {}),
                    **({'ok': bool(ok)} if type == 'done' else {}))
        return {'request': request, 'delivered': delivered}

    async def end(self, call_id, *, owner):
        record = self.get(call_id, owner=owner)
        if record['status'] in TERMINAL:
            return record
        line = self.lines[record['channel']]
        ended = await line.end(call_id)
        context = self._contexts.get(call_id)
        if not ended and context is not None and record['status'] in ('queued', 'connecting'):
            await context.finish('canceled')
        return self.store.get(call_id)

    async def transfer(self, call_id, *, owner):
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
        if record['channel'] == 'meeting':
            # A meeting is judged from its transcript like a call; the handoff only stands in
            # when nothing was said.
            if not transcript:
                if handoff is not None:
                    return result_from_handoff(handoff, duration_seconds=duration)
                if end_reason == 'canceled' and not duration:
                    return result_without_conversation('canceled', duration_seconds=duration)
                return fallback_result(end_reason, transcript, duration, 'no meeting handoff was produced')
        else:
            simple = result_without_conversation(end_reason, transcript=transcript,
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
            self._set_status(call_id, 'failed', endReason='error', error=error, usage=usage,
                             cost=call_cost(dict(record, usage=usage), self.environ))
        else:
            # The line's usage is kept now, so a stop while the result is written loses none.
            self._set_status(call_id, 'summarizing', endReason=end_reason, usage=usage,
                             cost=call_cost(dict(record, usage=usage), self.environ))
            try:
                result = await self._build_result(context, end_reason, usage, handoff)
            except asyncio.CancelledError:
                # Shutting down mid-summary: never leave the call in 'summarizing'.
                self._close_unfinished(call_id, 'The daemon stopped while writing the result.')
                self._contexts.pop(call_id, None)
                raise
            if record['channel'] == 'phone' and record.get('direction') == 'outbound':
                result['disclosureVerified'] = context.disclosure
            # Requests nobody finished during the call are follow-ups; the agent can still
            # answer them with done.
            still_open = [{key: request[key] for key in ('id', 'kind', 'text', 'askedBy')
                           if key in request}
                          for request in self.store.get(call_id).get('requests') or ()
                          if request['state'] in OPEN_STATES]
            if still_open:
                result['openRequests'] = still_open
            if 'summaryTokens' in result:
                usage = dict(usage, summaryTokens=result.pop('summaryTokens'))
            if 'summaryModel' in result:
                usage = dict(usage, summaryModel=result.pop('summaryModel'))
            status = 'canceled' if result['outcome'] == 'canceled' else 'completed'
            record = self.store.update(call_id, result=result, usage=usage,
                                       cost=call_cost(dict(record, usage=usage), self.environ),
                                       **({'error': error} if error else {}))
            self._event(call_id, 'call.result', outcome=result['outcome'])
            self._honor_do_not_call(record, result)
            self._set_status(call_id, status, endReason=end_reason)
        self._contexts.pop(call_id, None)
        self._followed.pop(call_id, None)
        record = self.store.get(call_id)
        if self._needs_price(record):
            self._price_checked.add(call_id)
            self._spawn(self._settle_price(call_id))
        await self._record_usage(record)
        try:
            delivery = await self.hooks.notify(record['owner'], record)
        except Exception as notify_error:
            delivery = {'delivered': False, 'error': type(notify_error).__name__}
        if delivery is not None:
            self._event(call_id, 'call.webhook', **delivery)

    async def _record_usage(self, record):
        """Tell the hooks what a finished call used, with the call's owner."""
        try:
            await maybe_await(self.hooks.record_usage(record['owner'], record, record.get('usage') or {}))
        except Exception:
            self._event(record['id'], 'call.usage_failed')

    async def _settle_unfinished(self):
        """Record the usage of calls closed before they could finish, once each, with what is
        known of them (the line's usage when it got that far)."""
        while self._unsettled:
            call_id = self._unsettled.pop(0)
            try:
                record = self.store.get(call_id)
            except CallNotFound:
                continue
            await self._record_usage(record)

    async def shutdown(self):
        active = list(self._contexts)
        for call_id in active:
            try:
                await self.end(call_id, owner=self.store.get(call_id).get('owner'))
            except Exception:
                pass
        # Each line was asked to end its call (a phone call hangs up). Give them a moment to
        # do so and report what the call used, then cut off what is left.
        running = [task for task in map(self._call_tasks.get, active) if task is not None]
        if running and self.shutdown_grace > 0:
            await asyncio.wait(running, timeout=self.shutdown_grace)
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        for call_id in active:
            if self.store.get(call_id)['status'] not in TERMINAL:
                self._close_unfinished(call_id, 'The daemon stopped before the call finished.')
        self._contexts.clear()
        await self._settle_unfinished()

    def _close_unfinished(self, call_id, message):
        """Mark a call that can no longer finish as failed, keeping what was said; its usage
        is recorded by _settle_unfinished."""
        transcript = [{'speaker': e['data'].get('speaker'), 'text': e['data'].get('text')}
                      for e in self.store.events(call_id) if e['type'] == 'call.transcript']
        result = fallback_result('error', transcript, 0, message.rstrip('.').lower())
        result['outcome'] = 'failed'
        self.store.update(call_id, result=result, error=message)
        self._set_status(call_id, 'failed', endReason='error')
        self._unsettled.append(call_id)
