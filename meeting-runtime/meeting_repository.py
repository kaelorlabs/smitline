"""Durable meeting snapshots, private lease tokens, and handoff files."""
from dataclasses import dataclass
from pathlib import Path
import errno
import fcntl
import json
import os
import stat
import threading

from agent_sessions import MeetingSession
from meeting_handoff import MeetingHandoff
from schema_validation import reject_secrets, require_meeting_id


_GLOBAL_LOCKS_GUARD = threading.Lock()
_GLOBAL_LOCKS = {}
_SNAPSHOT_NAME = 'snapshot.json'
_LEASE_NAME = 'lease.json'
_HANDOFF_NAME = 'handoff.json'
_APPROVALS_NAME = 'approvals.json'
_GIT_OPS_NAME = 'git-ops.json'
_SCREEN_SHARE_NAME = 'screen-share.json'
_OWNER_FIELDS = ('leaseId',)
_APPROVAL_FILE_FIELDS = ('version', 'approvals')
_GIT_OPS_FILE_FIELDS = ('version', 'operations')
_SCREEN_SHARE_FILE_FIELDS = ('version', 'settings', 'status', 'observations', 'paused')


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
    lease_token: str = None
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
            session = MeetingSession.from_dict(payload)
        except (TypeError, ValueError) as error:
            raise MeetingCorruptionError('meeting snapshot does not match the schema') from error
        lease_raw = self._read_named(dir_fd, _LEASE_NAME)
        lease_token = None
        if lease_raw is not None:
            lease_payload = self._parse_object(lease_raw, 'meeting lease record')
            extra = set(lease_payload) - set(_OWNER_FIELDS)
            if extra:
                raise MeetingCorruptionError('meeting lease record does not match the schema')
            token = lease_payload.get('leaseId')
            if not isinstance(token, str) or not token:
                raise MeetingCorruptionError('meeting lease record does not match the schema')
            lease_token = token
        handoff = None
        handoff_raw = self._read_named(dir_fd, _HANDOFF_NAME)
        if handoff_raw is not None:
            handoff_payload = self._parse_object(handoff_raw, 'meeting handoff')
            try:
                reject_secrets(handoff_payload, 'meeting handoff')
                handoff = MeetingHandoff.from_dict(handoff_payload)
            except (TypeError, ValueError) as error:
                raise MeetingCorruptionError('meeting handoff does not match the schema') from error
        return MeetingRecord(session=session, lease_token=lease_token, handoff=handoff)

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

    def put(self, session, lease_token=None):
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
                if lease_token is not None:
                    self._atomic_write(dir_fd, _LEASE_NAME, {'leaseId': lease_token})
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

    def clear_lease_token(self, meeting_id):
        meeting_id = require_meeting_id(meeting_id)
        with self._lock(meeting_id):
            dir_fd = self._open_meeting_dir(meeting_id, create=False)
            if dir_fd is None:
                return None
            try:
                fcntl.flock(dir_fd, fcntl.LOCK_EX)
                try:
                    os.unlink(_LEASE_NAME, dir_fd=dir_fd)
                except FileNotFoundError:
                    pass
                except OSError as error:
                    _raise_unsafe_path(error, 'meeting storage path must not be a symlink')
                os.fsync(dir_fd)
            finally:
                fcntl.flock(dir_fd, fcntl.LOCK_UN)
                os.close(dir_fd)
        return self.get(meeting_id)

    def _read_approvals(self, dir_fd, *, workspace=None):
        from approvals import ApprovalRecord
        raw = self._read_named(dir_fd, _APPROVALS_NAME)
        if raw is None:
            return []
        payload = self._parse_object(raw, 'meeting approvals')
        extra = set(payload) - set(_APPROVAL_FILE_FIELDS)
        if extra:
            raise MeetingCorruptionError('meeting approvals do not match the schema')
        items = payload.get('approvals')
        if items is None:
            return []
        if not isinstance(items, list):
            raise MeetingCorruptionError('meeting approvals do not match the schema')
        try:
            reject_secrets(payload, 'meeting approvals')
            return [ApprovalRecord.from_dict(item, workspace=workspace).to_dict() for item in items]
        except (TypeError, ValueError) as error:
            raise MeetingCorruptionError('meeting approvals do not match the schema') from error

    def list_approvals(self, meeting_id, *, workspace=None):
        meeting_id = require_meeting_id(meeting_id)
        with self._lock(meeting_id):
            dir_fd = self._open_meeting_dir(meeting_id, create=False)
            if dir_fd is None:
                return None
            try:
                fcntl.flock(dir_fd, fcntl.LOCK_SH)
                if self._read_named(dir_fd, _SNAPSHOT_NAME) is None:
                    return None
                return self._read_approvals(dir_fd, workspace=workspace)
            finally:
                fcntl.flock(dir_fd, fcntl.LOCK_UN)
                os.close(dir_fd)

    def update_approvals(self, meeting_id, mutator, *, workspace=None):
        meeting_id = require_meeting_id(meeting_id)
        with self._lock(meeting_id):
            dir_fd = self._open_meeting_dir(meeting_id, create=False)
            if dir_fd is None:
                raise FileNotFoundError('meeting does not exist')
            try:
                fcntl.flock(dir_fd, fcntl.LOCK_EX)
                if self._read_named(dir_fd, _SNAPSHOT_NAME) is None:
                    raise FileNotFoundError('meeting does not exist')
                current = list(self._read_approvals(dir_fd, workspace=workspace))
                updated = list(mutator(list(current)))
                if updated == current:
                    return updated
                payload = {'version': 1, 'approvals': updated}
                reject_secrets(payload, 'meeting approvals')
                self._atomic_write(dir_fd, _APPROVALS_NAME, payload)
                return updated
            finally:
                fcntl.flock(dir_fd, fcntl.LOCK_UN)
                os.close(dir_fd)

    def _read_git_ops(self, dir_fd):
        from git_actions import GitOperation
        raw = self._read_named(dir_fd, _GIT_OPS_NAME)
        if raw is None:
            return []
        payload = self._parse_object(raw, 'meeting git operations')
        extra = set(payload) - set(_GIT_OPS_FILE_FIELDS)
        if extra:
            raise MeetingCorruptionError('meeting git operations do not match the schema')
        items = payload.get('operations')
        if items is None:
            return []
        if not isinstance(items, list):
            raise MeetingCorruptionError('meeting git operations do not match the schema')
        try:
            reject_secrets(payload, 'meeting git operations')
            return [GitOperation.from_dict(item).to_dict() for item in items]
        except (TypeError, ValueError) as error:
            raise MeetingCorruptionError('meeting git operations do not match the schema') from error

    def list_git_ops(self, meeting_id):
        meeting_id = require_meeting_id(meeting_id)
        with self._lock(meeting_id):
            dir_fd = self._open_meeting_dir(meeting_id, create=False)
            if dir_fd is None:
                return None
            try:
                fcntl.flock(dir_fd, fcntl.LOCK_SH)
                if self._read_named(dir_fd, _SNAPSHOT_NAME) is None:
                    return None
                return self._read_git_ops(dir_fd)
            finally:
                fcntl.flock(dir_fd, fcntl.LOCK_UN)
                os.close(dir_fd)

    def update_git_ops(self, meeting_id, mutator):
        meeting_id = require_meeting_id(meeting_id)
        with self._lock(meeting_id):
            dir_fd = self._open_meeting_dir(meeting_id, create=False)
            if dir_fd is None:
                raise FileNotFoundError('meeting does not exist')
            try:
                fcntl.flock(dir_fd, fcntl.LOCK_EX)
                if self._read_named(dir_fd, _SNAPSHOT_NAME) is None:
                    raise FileNotFoundError('meeting does not exist')
                current = list(self._read_git_ops(dir_fd))
                updated = list(mutator(list(current)))
                if updated == current:
                    return updated
                payload = {'version': 1, 'operations': updated}
                reject_secrets(payload, 'meeting git operations')
                self._atomic_write(dir_fd, _GIT_OPS_NAME, payload)
                return updated
            finally:
                fcntl.flock(dir_fd, fcntl.LOCK_UN)
                os.close(dir_fd)

    def _read_screen_share(self, dir_fd):
        from screen_share import parse_screen_share_settings, public_status, VisualObservation
        raw = self._read_named(dir_fd, _SCREEN_SHARE_NAME)
        if raw is None:
            return {
                'version': 1,
                'settings': parse_screen_share_settings(None),
                'status': public_status({'enabled': False, 'degradedReason': 'disabled'}),
                'observations': [],
                'paused': False,
            }
        payload = self._parse_object(raw, 'meeting screen share')
        extra = set(payload) - set(_SCREEN_SHARE_FILE_FIELDS)
        if extra:
            raise MeetingCorruptionError('meeting screen share does not match the schema')
        try:
            reject_secrets(payload, 'meeting screen share')
            settings = parse_screen_share_settings(payload.get('settings'))
            status = public_status(payload.get('status') or {'enabled': settings['enabled']})
            observations = []
            for item in payload.get('observations') or []:
                observations.append(VisualObservation.from_dict(item).to_dict())
            return {
                'version': 1,
                'settings': settings,
                'status': status,
                'observations': observations,
                'paused': bool(payload.get('paused')),
            }
        except (TypeError, ValueError) as error:
            raise MeetingCorruptionError('meeting screen share does not match the schema') from error

    def get_screen_share(self, meeting_id):
        meeting_id = require_meeting_id(meeting_id)
        with self._lock(meeting_id):
            dir_fd = self._open_meeting_dir(meeting_id, create=False)
            if dir_fd is None:
                return None
            try:
                fcntl.flock(dir_fd, fcntl.LOCK_SH)
                if self._read_named(dir_fd, _SNAPSHOT_NAME) is None:
                    return None
                return self._read_screen_share(dir_fd)
            finally:
                fcntl.flock(dir_fd, fcntl.LOCK_UN)
                os.close(dir_fd)

    def update_screen_share(self, meeting_id, mutator):
        meeting_id = require_meeting_id(meeting_id)
        with self._lock(meeting_id):
            dir_fd = self._open_meeting_dir(meeting_id, create=False)
            if dir_fd is None:
                raise FileNotFoundError('meeting does not exist')
            try:
                fcntl.flock(dir_fd, fcntl.LOCK_EX)
                if self._read_named(dir_fd, _SNAPSHOT_NAME) is None:
                    raise FileNotFoundError('meeting does not exist')
                current = self._read_screen_share(dir_fd)
                updated = mutator(dict(current))
                if not isinstance(updated, dict):
                    raise ValueError('screen share state is invalid')
                payload = {
                    'version': 1,
                    'settings': updated.get('settings') or current['settings'],
                    'status': updated.get('status') or current['status'],
                    'observations': list(updated.get('observations') or []),
                    'paused': bool(updated.get('paused')),
                }
                reject_secrets(payload, 'meeting screen share')
                self._atomic_write(dir_fd, _SCREEN_SHARE_NAME, payload)
                return payload
            finally:
                fcntl.flock(dir_fd, fcntl.LOCK_UN)
                os.close(dir_fd)

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
