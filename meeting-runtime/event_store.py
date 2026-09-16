"""Append-only JSONL event storage with meeting isolation and restart-safe recovery."""
from abc import ABC, abstractmethod
import errno
import fcntl
import json
import os
import stat
import threading
from pathlib import Path

from events import ColleagueEvent
from schema_validation import reject_secrets, require_meeting_id


_PATH_LOCK_GUARD = threading.Lock()
_PATH_LOCKS = {}
_EVENTS_NAME = 'events.jsonl'


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


def _directory_flags():
    flags = os.O_RDONLY | os.O_DIRECTORY
    if hasattr(os, 'O_NOFOLLOW'):
        flags |= os.O_NOFOLLOW
    return flags


def _is_unsafe_path_error(error):
    return error.errno in (
        errno.ELOOP,
        errno.ENOTDIR,
        getattr(errno, 'EMLINK', errno.ELOOP),
    )


def _raise_unsafe_path(error, message):
    if _is_unsafe_path_error(error):
        raise ValueError(message) from error
    raise error


def _canonical_event(event):
    if isinstance(event, ColleagueEvent):
        payload = event.to_dict()
    elif isinstance(event, dict):
        payload = event
    else:
        raise ValueError('event must be a ColleagueEvent or object')
    stored = ColleagueEvent.from_dict(payload)
    reject_secrets(stored.to_dict(), 'event')
    return stored


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
    """JSONL store contained under a resolved root directory fd.

    Meeting directories and events.jsonl are opened with mkdirat/openat from
    that fd and O_NOFOLLOW so a precreated last-component symlink cannot escape
    the store. Python does not expose a portable way to atomically replace a
    whole ancestor of the configured root after construction; the held root fd
    keeps the original directory on POSIX. O_NOFOLLOW is a POSIX flag used by
    this Linux/macOS runtime.
    """

    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, mode=0o700, exist_ok=True)
        if self.root.is_symlink():
            raise ValueError('event store root must not be a symlink')
        os.chmod(self.root, 0o700)
        try:
            self._root_fd = os.open(self.root, _directory_flags())
        except OSError as error:
            _raise_unsafe_path(error, 'event store root must not be a symlink')
        os.fchmod(self._root_fd, 0o700)

    def close(self):
        fd = getattr(self, '_root_fd', None)
        if fd is None:
            return
        os.close(fd)
        self._root_fd = None

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass

    def _lock_path(self, meeting_id):
        return str(self.root / meeting_id / _EVENTS_NAME)

    def _open_meeting_dir(self, meeting_id, *, create):
        meeting_id = require_meeting_id(meeting_id)
        if create:
            try:
                os.mkdir(meeting_id, 0o700, dir_fd=self._root_fd)
            except FileExistsError:
                pass
            except OSError as error:
                _raise_unsafe_path(error, 'meeting storage path must not be a symlink')
        try:
            dir_fd = os.open(meeting_id, _directory_flags(), dir_fd=self._root_fd)
        except FileNotFoundError:
            if create:
                raise
            return None
        except OSError as error:
            if error.errno == errno.ENOENT and not create:
                return None
            _raise_unsafe_path(error, 'meeting storage path must not be a symlink')
        try:
            info = os.fstat(dir_fd)
            if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
                raise ValueError('meeting storage path must be a directory')
            os.fchmod(dir_fd, 0o700)
            return dir_fd
        except Exception:
            os.close(dir_fd)
            raise

    def _open_events(self, meeting_id, *, create):
        dir_fd = self._open_meeting_dir(meeting_id, create=create)
        if dir_fd is None:
            return None, None
        try:
            exclusive = os.O_RDWR | os.O_CREAT | os.O_EXCL
            existing = os.O_RDWR if create else os.O_RDONLY
            if hasattr(os, 'O_NOFOLLOW'):
                existing |= os.O_NOFOLLOW
            try:
                fd = os.open(_EVENTS_NAME, exclusive if create else existing, 0o600, dir_fd=dir_fd)
            except FileExistsError:
                fd = os.open(_EVENTS_NAME, existing, dir_fd=dir_fd)
        except FileNotFoundError:
            os.close(dir_fd)
            if create:
                raise
            return None, None
        except OSError as error:
            os.close(dir_fd)
            if error.errno == errno.ENOENT and not create:
                return None, None
            _raise_unsafe_path(error, 'events.jsonl must not be a symlink')
        if create:
            os.fchmod(fd, 0o600)
        return dir_fd, fd

    def append(self, event):
        stored = _canonical_event(event)
        encoded = (json.dumps(stored.to_dict(), ensure_ascii=False) + '\n').encode('utf-8')
        with _path_lock(self._lock_path(stored.meeting_id)):
            dir_fd, fd = self._open_events(stored.meeting_id, create=True)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
                _truncate_incomplete_tail(fd)
                os.lseek(fd, 0, os.SEEK_END)
                _write_all(fd, encoded)
                os.fsync(fd)
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
                os.close(fd)
                os.close(dir_fd)
        return stored

    def replay(self, meeting_id):
        meeting_id = require_meeting_id(meeting_id)
        with _path_lock(self._lock_path(meeting_id)):
            dir_fd, fd = self._open_events(meeting_id, create=False)
            if fd is None:
                return []
            try:
                fcntl.flock(fd, fcntl.LOCK_SH)
                payload = _read_all(fd)
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
                os.close(fd)
                os.close(dir_fd)
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
