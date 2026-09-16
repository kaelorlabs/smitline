"""Append-only JSONL event storage with meeting isolation and restart-safe recovery."""
from abc import ABC, abstractmethod
import fcntl
import json
import os
import threading
from pathlib import Path

from events import ColleagueEvent
from schema_validation import reject_secrets, require_meeting_id


_PATH_LOCK_GUARD = threading.Lock()
_PATH_LOCKS = {}


class EventStore(ABC):
    @abstractmethod
    def append(self, event):
        """Validate and durably append one event. Returns the stored ColleagueEvent."""

    @abstractmethod
    def replay(self, meeting_id):
        """Return ordered valid events for a meeting, skipping malformed records."""


def _path_lock(path):
    key = str(path)
    with _PATH_LOCK_GUARD:
        lock = _PATH_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _PATH_LOCKS[key] = lock
        return lock


def _read_all(fd, size=None):
    remaining = os.fstat(fd).st_size if size is None else size
    chunks = []
    while remaining > 0:
        chunk = os.read(fd, remaining)
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b''.join(chunks)


def _write_all(fd, data):
    view = memoryview(data)
    offset = 0
    while offset < len(view):
        offset += os.write(fd, view[offset:])


def _truncate_incomplete_tail(fd):
    size = os.fstat(fd).st_size
    if size == 0:
        return
    os.lseek(fd, -1, os.SEEK_END)
    if os.read(fd, 1) == b'\n':
        return
    chunk_size = 8192
    position = size
    last_newline = -1
    while position > 0:
        start = max(0, position - chunk_size)
        os.lseek(fd, start, os.SEEK_SET)
        chunk = os.read(fd, position - start)
        index = chunk.rfind(b'\n')
        if index >= 0:
            last_newline = start + index
            break
        position = start
    incomplete_start = last_newline + 1 if last_newline >= 0 else 0
    os.lseek(fd, incomplete_start, os.SEEK_SET)
    incomplete = _read_all(fd, size - incomplete_start)
    recovered = False
    try:
        parsed = json.loads(incomplete.decode('utf-8'))
        ColleagueEvent.from_dict(parsed)
        recovered = True
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
        recovered = False
    if recovered:
        os.lseek(fd, 0, os.SEEK_END)
        _write_all(fd, b'\n')
    else:
        os.ftruncate(fd, incomplete_start)
    os.fsync(fd)


def _parse_line(line, meeting_id):
    try:
        event = ColleagueEvent.from_dict(json.loads(line))
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    if event.meeting_id != meeting_id:
        return None
    return event


class JsonlEventStore(EventStore):
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, mode=0o700, exist_ok=True)
        os.chmod(self.root, 0o700)

    def _events_path(self, meeting_id):
        meeting_id = require_meeting_id(meeting_id)
        return self.root / meeting_id / 'events.jsonl'

    def append(self, event):
        stored = event if isinstance(event, ColleagueEvent) else ColleagueEvent.from_dict(event)
        serialized = stored.to_dict()
        reject_secrets(serialized, 'event')
        encoded = (json.dumps(serialized, ensure_ascii=False) + '\n').encode('utf-8')
        path = self._events_path(stored.meeting_id)
        path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        os.chmod(path.parent, 0o700)
        with _path_lock(path):
            fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
            try:
                os.fchmod(fd, 0o600)
                fcntl.flock(fd, fcntl.LOCK_EX)
                _truncate_incomplete_tail(fd)
                os.lseek(fd, 0, os.SEEK_END)
                _write_all(fd, encoded)
                os.fsync(fd)
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
                os.close(fd)
        return stored

    def replay(self, meeting_id):
        path = self._events_path(meeting_id)
        if not path.exists():
            return []
        with _path_lock(path):
            fd = os.open(path, os.O_RDONLY)
            try:
                fcntl.flock(fd, fcntl.LOCK_SH)
                payload = _read_all(fd)
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
                os.close(fd)
        try:
            text = payload.decode('utf-8')
        except UnicodeDecodeError:
            text = payload.decode('utf-8', errors='replace')
        if not text:
            return []
        if text.endswith('\n'):
            lines = text.split('\n')[:-1]
        else:
            lines = text.split('\n')
        events = []
        for line in lines:
            event = _parse_line(line, meeting_id)
            if event is not None:
                events.append(event)
        return events
