"""Durable meeting snapshots and handoff files."""
from dataclasses import dataclass
from pathlib import Path
import errno
import fcntl
import json
import os
import stat
import threading

from agent_sessions import MeetingSession, upgrade_legacy_session
from meeting_handoff import MeetingHandoff, upgrade_legacy_handoff
from schema_validation import reject_secrets, require_meeting_id


_GLOBAL_LOCKS_GUARD = threading.Lock()
_GLOBAL_LOCKS = {}
_SNAPSHOT_NAME = 'snapshot.json'
_HANDOFF_NAME = 'handoff.json'


class MeetingRepositoryError(Exception):
    """Base meeting-repository error."""


class MeetingCorruptionError(MeetingRepositoryError):
    """A meeting record exists but cannot be parsed; it is not treated as absent."""


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


def _thread_lock(key):
    with _GLOBAL_LOCKS_GUARD:
        lock = _GLOBAL_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _GLOBAL_LOCKS[key] = lock
        return lock


def _write_all(fd, data):
    view = memoryview(data)
    offset = 0
    while offset < len(view):
        offset += os.write(fd, view[offset:])


def _read_all(fd):
    chunks = []
    while True:
        chunk = os.read(fd, 1024 * 64)
        if not chunk:
            break
        chunks.append(chunk)
    return b''.join(chunks)


@dataclass(frozen=True)
class MeetingRecord:
    session: MeetingSession
    handoff: MeetingHandoff = None


class MeetingRepository:
    """Meeting snapshots contained under a resolved root directory fd."""

    def __init__(self, root):
        self._root_fd = None
        self.root = Path(root).resolve()
        if self.root.exists() and not self.root.is_dir():
            raise ValueError('meeting repository root must be a directory')
        self.root.mkdir(parents=True, mode=0o700, exist_ok=True)
        if self.root.is_symlink():
            raise ValueError('meeting repository root must not be a symlink')
        os.chmod(self.root, 0o700)
        try:
            self._root_fd = os.open(self.root, _directory_flags())
        except OSError as error:
            if error.errno == errno.ENOTDIR:
                raise ValueError('meeting repository root must be a directory') from error
            _raise_unsafe_path(error, 'meeting repository root must not be a symlink')
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

    def _ensure_open(self):
        if getattr(self, '_root_fd', None) is None:
            raise RuntimeError('meeting repository is closed')

    def _lock(self, meeting_id):
        return _thread_lock(str(self.root / meeting_id))

    def _open_named(self, dir_fd, name, flags, mode=0o600):
        try:
            return os.open(name, flags, mode, dir_fd=dir_fd)
        except OSError as error:
            _raise_unsafe_path(error, 'meeting storage path must not be a symlink')

    def _open_meeting_dir(self, meeting_id, *, create):
        self._ensure_open()
        meeting_id = require_meeting_id(meeting_id)
        os.fchmod(self._root_fd, 0o700)
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

    def _atomic_write(self, dir_fd, name, payload):
        reject_secrets(payload, name)
        encoded = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        tmp_name = name + '.tmp'
        exclusive = os.O_RDWR | os.O_CREAT | os.O_EXCL
        fd = None
        try:
            try:
                os.unlink(tmp_name, dir_fd=dir_fd)
            except FileNotFoundError:
                pass
            except OSError as error:
                _raise_unsafe_path(error, 'meeting storage path must not be a symlink')
            fd = self._open_named(dir_fd, tmp_name, exclusive)
            os.fchmod(fd, 0o600)
            _write_all(fd, encoded)
            os.fsync(fd)
        finally:
            if fd is not None:
                os.close(fd)
        os.replace(tmp_name, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
        os.fsync(dir_fd)

    def _read_named(self, dir_fd, name):
        flags = os.O_RDONLY
        if hasattr(os, 'O_NOFOLLOW'):
            flags |= os.O_NOFOLLOW
        try:
            fd = self._open_named(dir_fd, name, flags)
        except FileNotFoundError:
            return None
        except OSError as error:
            if error.errno == errno.ENOENT:
                return None
            raise
        try:
            os.fchmod(fd, 0o600)
            return _read_all(fd)
        finally:
            os.close(fd)

    def _parse_object(self, raw, label):
        if raw is None:
            return None
        if not raw or not raw.strip():
            raise MeetingCorruptionError(f'{label} is empty')
        try:
            text = raw.decode('utf-8')
        except UnicodeDecodeError as error:
            raise MeetingCorruptionError(f'{label} is not valid UTF-8') from error
        if not text.strip():
            raise MeetingCorruptionError(f'{label} is empty')
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as error:
            raise MeetingCorruptionError(f'{label} is not valid JSON') from error
        if not isinstance(payload, dict):
            raise MeetingCorruptionError(f'{label} is not an object')
        return payload

    def _load_record(self, dir_fd):
        snapshot_raw = self._read_named(dir_fd, _SNAPSHOT_NAME)
        if snapshot_raw is None:
            return None
        payload = self._parse_object(snapshot_raw, 'meeting snapshot')
        try:
            reject_secrets(payload, 'meeting snapshot')
            session = MeetingSession.from_dict(upgrade_legacy_session(payload))
        except (TypeError, ValueError) as error:
            raise MeetingCorruptionError('meeting snapshot does not match the schema') from error
        handoff = None
        handoff_raw = self._read_named(dir_fd, _HANDOFF_NAME)
        if handoff_raw is not None:
            handoff_payload = self._parse_object(handoff_raw, 'meeting handoff')
            try:
                reject_secrets(handoff_payload, 'meeting handoff')
                handoff = MeetingHandoff.from_dict(upgrade_legacy_handoff(handoff_payload))
            except (TypeError, ValueError) as error:
                raise MeetingCorruptionError('meeting handoff does not match the schema') from error
        return MeetingRecord(session=session, handoff=handoff)

    def get(self, meeting_id):
        meeting_id = require_meeting_id(meeting_id)
        with self._lock(meeting_id):
            dir_fd = self._open_meeting_dir(meeting_id, create=False)
            if dir_fd is None:
                return None
            try:
                fcntl.flock(dir_fd, fcntl.LOCK_SH)
                return self._load_record(dir_fd)
            finally:
                fcntl.flock(dir_fd, fcntl.LOCK_UN)
                os.close(dir_fd)

    def put(self, session):
        if not isinstance(session, MeetingSession):
            raise ValueError('session must be a MeetingSession')
        meeting_id = require_meeting_id(session.id)
        with self._lock(meeting_id):
            dir_fd = self._open_meeting_dir(meeting_id, create=True)
            try:
                fcntl.flock(dir_fd, fcntl.LOCK_EX)
                snapshot = session.to_dict()
                reject_secrets(snapshot, 'meeting snapshot')
                self._atomic_write(dir_fd, _SNAPSHOT_NAME, snapshot)
                os.fsync(self._root_fd)
            finally:
                fcntl.flock(dir_fd, fcntl.LOCK_UN)
                os.close(dir_fd)
        return self.get(meeting_id)

    def store_handoff(self, handoff):
        if not isinstance(handoff, MeetingHandoff):
            raise ValueError('handoff must be a MeetingHandoff')
        meeting_id = require_meeting_id(handoff.meeting_id)
        with self._lock(meeting_id):
            dir_fd = self._open_meeting_dir(meeting_id, create=False)
            if dir_fd is None:
                raise FileNotFoundError('meeting does not exist')
            try:
                fcntl.flock(dir_fd, fcntl.LOCK_EX)
                record = self._load_record(dir_fd)
                if record is None:
                    raise MeetingCorruptionError('meeting snapshot is empty')
                payload = handoff.to_dict()
                reject_secrets(payload, 'meeting handoff')
                self._atomic_write(dir_fd, _HANDOFF_NAME, payload)
            finally:
                fcntl.flock(dir_fd, fcntl.LOCK_UN)
                os.close(dir_fd)
        return self.get(meeting_id)

    def list_ids(self):
        self._ensure_open()
        os.fchmod(self._root_fd, 0o700)
        names = os.listdir(self._root_fd)
        meeting_ids = []
        for name in names:
            try:
                meeting_ids.append(require_meeting_id(name))
            except ValueError:
                continue
        return tuple(sorted(meeting_ids))
