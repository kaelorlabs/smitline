"""Durable local storage for calls: one private directory per call."""
from datetime import datetime, timezone
from pathlib import Path
import json
import os
import re
import secrets
import threading

from schema_validation import reject_secrets


CALL_ID = re.compile(r'^call-[0-9a-f]{16}$')
STATUSES = (
    'queued', 'connecting', 'ringing', 'waiting', 'in_progress', 'summarizing',
    'completed', 'failed', 'canceled',
)
TERMINAL = frozenset({'completed', 'failed', 'canceled'})
END_REASONS = (
    'hangup', 'remote_hangup', 'no_answer', 'busy', 'voicemail', 'max_duration', 'canceled',
    'meeting_ended', 'transferred', 'error',
)
RECORD_VERSION = 1


class CallNotFound(KeyError):
    pass


def new_call_id():
    return 'call-' + secrets.token_hex(8)


def require_call_id(value):
    if not isinstance(value, str) or not CALL_ID.fullmatch(value):
        raise CallNotFound(value)
    return value


def utcnow_iso(clock=None):
    moment = clock() if clock else datetime.now(timezone.utc)
    return moment.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')


def _atomic_write(path, body):
    tmp = path.with_name(f'.{path.name}.{secrets.token_hex(4)}.tmp')
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, body.encode('utf-8'))
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)


class CallStore:
    """Snapshots in call.json, append-only events in events.jsonl.

    All writes happen on the daemon's event loop or under the store lock, so a
    call's snapshot and event sequence never interleave between writers.
    """

    def __init__(self, root, *, clock=None):
        self.root = Path(root)
        self.root.mkdir(parents=True, mode=0o700, exist_ok=True)
        os.chmod(self.root, 0o700)
        self.clock = clock
        self._lock = threading.RLock()
        self._sequences = {}

    def now(self):
        return utcnow_iso(self.clock)

    def _dir(self, call_id):
        return self.root / require_call_id(call_id)

    def create(self, record):
        call_id = require_call_id(record['id'])
        reject_secrets(record, 'call')
        with self._lock:
            directory = self._dir(call_id)
            directory.mkdir(mode=0o700)
            _atomic_write(directory / 'call.json', json.dumps(record, ensure_ascii=False, indent=2))
            (directory / 'events.jsonl').touch(mode=0o600)
            self._sequences[call_id] = 0
        return record

    def get(self, call_id):
        path = self._dir(call_id) / 'call.json'
        try:
            return json.loads(path.read_text(encoding='utf-8'))
        except FileNotFoundError as error:
            raise CallNotFound(call_id) from error

    def update(self, call_id, **changes):
        with self._lock:
            record = self.get(call_id)
            record.update(changes)
            record['updatedAt'] = self.now()
            reject_secrets(record, 'call')
            _atomic_write(self._dir(call_id) / 'call.json',
                          json.dumps(record, ensure_ascii=False, indent=2))
            return record

    def list(self, *, owner=None, limit=20):
        records = []
        for entry in self.root.iterdir():
            if not CALL_ID.fullmatch(entry.name):
                continue
            try:
                record = self.get(entry.name)
            except (CallNotFound, ValueError):
                continue
            if owner is not None and record.get('owner') != owner:
                continue
            records.append(record)
        records.sort(key=lambda item: item.get('createdAt') or '', reverse=True)
        return records[:limit]

    def _next_sequence(self, call_id):
        if call_id not in self._sequences:
            self._sequences[call_id] = len(self.events(call_id))
        self._sequences[call_id] += 1
        return self._sequences[call_id]

    def append_event(self, call_id, event_type, **data):
        reject_secrets(data, 'call event')
        with self._lock:
            path = self._dir(call_id) / 'events.jsonl'
            if not path.exists():
                raise CallNotFound(call_id)
            event = {
                'id': str(self._next_sequence(call_id)),
                'type': event_type,
                'at': self.now(),
                'callId': call_id,
                'data': data,
            }
            with open(path, 'a', encoding='utf-8') as handle:
                handle.write(json.dumps(event, ensure_ascii=False) + '\n')
            return event

    def events(self, call_id, *, after=None):
        path = self._dir(call_id) / 'events.jsonl'
        try:
            lines = path.read_text(encoding='utf-8').splitlines()
        except FileNotFoundError as error:
            raise CallNotFound(call_id) from error
        events = [json.loads(line) for line in lines if line.strip()]
        if after is None:
            return events
        try:
            after_number = int(after)
        except (TypeError, ValueError):
            return events
        return [event for event in events if int(event['id']) > after_number]

    def append_usage(self, entry):
        reject_secrets(entry, 'usage')
        with self._lock:
            path = self.root / 'usage.jsonl'
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            try:
                os.write(fd, (json.dumps(entry, ensure_ascii=False) + '\n').encode('utf-8'))
            finally:
                os.close(fd)
