"""Calls: accept a brief, run it on a line (phone or meeting), and publish the result."""
import asyncio
import dataclasses
import os

from call_brief import BriefIncomplete, CallBrief
from call_costs import call_cost, spend, with_cost
from call_hooks import CallRefused, LineNotReady, MissingCredentials, maybe_await
from call_result import (
    DEFAULT_SUMMARY_MODEL, ResponsesSummarizer, SummaryUnavailable, fallback_result,
    result_from_handoff, result_without_conversation, transcript_entries,
)
from call_store import TERMINAL, CallNotFound, new_call_id


MAX_WAIT_SECONDS = 300
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
        self.ended = False
        # Outbound phone calls: whether the opening was heard to say it is an AI (None: not heard).
        self.disclosure = None

    def credentials(self, provider):
        return self.service.hooks.credentials(self.owner, provider)

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
                 price_check_delays=PRICE_CHECK_DELAYS):
        self.store = store
        self.price_check_delays = tuple(price_check_delays)
        self._price_checked = set()
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

    def _brief(self, owner, payload):
        """Parse a brief after filling in what setup already knows (the owner's name, phone)."""
        if isinstance(payload, dict):
            defaults = getattr(self.hooks, 'brief_defaults', None)
            filled = dict(defaults(owner, payload) or {}) if defaults else {}
            filled.update({key: value for key, value in payload.items() if value not in (None, '')})
            payload = filled
        return CallBrief.from_dict(payload, environ=self.environ)

    async def check(self, payload, *, request=None):
        """Everything create() would refuse, reported instead of raised."""
        owner = await maybe_await(self.hooks.owner_for(request))
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
        """At startup, close calls a previous daemon left unfinished; nothing will finish them."""
        closed = []
        for record in self.store.list(limit=1000):
            if record['status'] in TERMINAL or record['id'] in self._contexts:
                continue
            self._close_unfinished(record['id'], 'The daemon stopped before the call finished.')
            closed.append(record['id'])
        return closed

    async def create(self, payload, *, request=None):
        owner = await maybe_await(self.hooks.owner_for(request))
        brief = self._brief(owner, payload)
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
        # Outbound calls only: an incoming caller ID can be faked, so no earlier call is read to it.
        carried = self._carried(owner, brief)
        if carried:
            brief = dataclasses.replace(brief, carried=tuple(note_line(item) for item in carried))
        return self._launch(brief, owner, line.start, direction='outbound', carried=carried)

    async def create_inbound(self, brief, owner, start):
        """Record a call someone else placed to us; `start(ctx)` runs it on the line."""
        return self._launch(brief, owner, start, direction='inbound')

    def _launch(self, brief, owner, start, *, direction, carried=()):
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
        context = CallContext(self, record['id'], brief, owner)
        self._contexts[record['id']] = context
        self._spawn(self._run(start, context), name='call-' + record['id'])
        return record

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

    def active_count(self, *, direction=None):
        count = 0
        for call_id in list(self._contexts):
            try:
                record = self.store.get(call_id)
            except CallNotFound:
                continue
            if direction is None or record.get('direction') == direction:
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

    async def recording(self, call_id, *, owner=None, fmt='wav'):
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

    def _contact_book(self):
        book = getattr(self.hooks, 'contact_book', None)
        if book is None:
            raise CallError(501, 'unsupported', 'these call hooks keep no contacts')
        return book

    def _saved_contacts(self):
        """{number: what the user saved}; empty when the hooks keep no contacts."""
        from contacts import ContactsUnreadable
        book = getattr(self.hooks, 'contact_book', None)
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
        saved = self._saved_contacts() if brief.channel == 'phone' else {}
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

    def save_note(self, call_id, text, *, owner=None):
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
        saved = self._saved_contacts()
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
        saved = self._saved_contacts()
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
            self._contact_book().update(number, changes)
        except ContactsUnreadable as error:
            raise CallError(503, 'contacts_unreadable', str(error)) from error
        except ValueError as error:
            raise CallError(422, 'invalid_request', str(error)) from error
        return self.contact(owner, number)

    def forget_contact(self, owner, number):
        """Drop the saved name, notes, and setting. Call records, and their notes, stay."""
        from contacts import ContactsUnreadable
        try:
            forgotten = self._contact_book().forget(number)
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

    def get(self, call_id, *, owner=None):
        record = self.store.get(call_id)
        if owner is not None and record.get('owner') != owner:
            raise CallNotFound(call_id)
        return with_cost(record, self.environ)

    def list(self, *, owner=None, limit=20):
        """Newest first; limit=None lists every call."""
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

    async def instruct(self, call_id, text, *, owner=None, silent=False):
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
            self._set_status(call_id, 'summarizing', endReason=end_reason)
            try:
                result = await self._build_result(context, end_reason, usage, handoff)
            except asyncio.CancelledError:
                # Shutting down mid-summary: never leave the call in 'summarizing'.
                self._close_unfinished(call_id, 'The daemon stopped while writing the result.')
                self._contexts.pop(call_id, None)
                raise
            if record['channel'] == 'phone' and record.get('direction') == 'outbound':
                result['disclosureVerified'] = context.disclosure
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
        record = self.store.get(call_id)
        if self._needs_price(record):
            self._price_checked.add(call_id)
            self._spawn(self._settle_price(call_id))
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
