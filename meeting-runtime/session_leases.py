"""Durable exclusive coding-agent session leases."""
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
import errno
import fcntl
import hashlib
import json
import os
import secrets
import socket
import threading

from agent_sessions import AGENT_PROVIDERS, AgentSessionRef
from schema_validation import (
    omit_none, optional_field, reject_secrets, reject_unknown_fields, require_enum,
    require_field, require_id, require_mapping, require_meeting_id, require_string,
    require_timestamp, require_version,
)


_GLOBAL_LOCKS_GUARD = threading.Lock()
_GLOBAL_LOCKS = {}
LEASE_VERSION = 1
LEASE_STATES = ('acquiring', 'in_meeting', 'finalizing', 'failed')
FINALIZATION_STATUSES = ('none', 'in_progress', 'completed', 'failed')
LEASE_FIELDS = (
    'version', 'leaseId', 'provider', 'sessionId', 'meetingId', 'state', 'acquiredAt',
    'lastHeartbeat', 'owner', 'activeDelegatedTurn', 'finalization',
)
OWNER_FIELDS = ('identity', 'pid', 'hostname')
FINALIZATION_FIELDS = ('status', 'recordedAt', 'reason')
DEFAULT_HEARTBEAT_TIMEOUT = 30


class LeaseError(Exception):
    """Base lease error."""


class LeaseConflictError(LeaseError):
    """The agent session is already owned by a meeting or delegated turn."""


class LeaseStateError(LeaseError):
    """The requested lease transition is not legal."""


class LeaseOwnershipError(LeaseError):
    """The supplied lease token does not match the persisted lease."""


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


def _utcnow():
    return datetime.now(timezone.utc)


def _iso(moment):
    if moment.tzinfo is None:
        raise ValueError('timestamps must be timezone-aware')
    return moment.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')


def default_owner_identity():
    hostname = socket.gethostname()
    pid = os.getpid()
    return {'identity': f'{hostname}:{pid}', 'pid': pid, 'hostname': hostname}


def lease_digest(provider, session_id):
    payload = f'{provider}\n{session_id}'.encode('utf-8')
    return hashlib.sha256(payload).hexdigest()


def _optional_timestamp(value, name):
    if value is None:
        return None
    return require_timestamp(value, name)


@dataclass(frozen=True)
class LeaseOwner:
    identity: str
    pid: int = None
    hostname: str = None

    def to_dict(self):
        return omit_none({'identity': self.identity, 'pid': self.pid, 'hostname': self.hostname})

    @classmethod
    def from_dict(cls, data):
        payload = require_mapping(data, 'owner')
        reject_unknown_fields(payload, OWNER_FIELDS, 'owner')
        pid = optional_field(payload, 'pid')
        if pid is not None and (isinstance(pid, bool) or not isinstance(pid, int)):
            raise ValueError('owner.pid must be an integer')
        return cls(
            identity=require_string(require_field(payload, 'identity', 'owner'), 'owner.identity',
                                    max_length=256),
            pid=pid,
            hostname=None if optional_field(payload, 'hostname') is None
            else require_string(payload['hostname'], 'owner.hostname', max_length=256),
        )


@dataclass(frozen=True)
class FinalizationState:
    status: str
    recorded_at: str = None
    reason: str = None

    def to_dict(self):
        return omit_none({
            'status': self.status,
            'recordedAt': self.recorded_at,
            'reason': self.reason,
        })

    @classmethod
    def from_dict(cls, data):
        payload = require_mapping(data, 'finalization')
        reject_unknown_fields(payload, FINALIZATION_FIELDS, 'finalization')
        reason = optional_field(payload, 'reason')
        return cls(
            status=require_enum(require_field(payload, 'status', 'finalization'),
                                'finalization.status', FINALIZATION_STATUSES),
            recorded_at=_optional_timestamp(optional_field(payload, 'recordedAt'),
                                            'finalization.recordedAt'),
            reason=None if reason is None else require_string(reason, 'finalization.reason',
                                                              allow_empty=True, max_length=1024),
        )


@dataclass(frozen=True)
class SessionLease:
    version: int
    token: str
    provider: str
    session_id: str
    meeting_id: str
    state: str
    acquired_at: str
    last_heartbeat: str
    owner: LeaseOwner
    active_delegated_turn: str = None
    finalization: FinalizationState = None

    def to_dict(self):
        return {
            'version': self.version,
            'leaseId': self.token,
            'provider': self.provider,
            'sessionId': self.session_id,
            'meetingId': self.meeting_id,
            'state': self.state,
            'acquiredAt': self.acquired_at,
            'lastHeartbeat': self.last_heartbeat,
            'owner': self.owner.to_dict(),
            'activeDelegatedTurn': self.active_delegated_turn,
            'finalization': (self.finalization or FinalizationState('none')).to_dict(),
        }

    @classmethod
    def from_dict(cls, data):
        payload = require_mapping(data, 'lease')
        reject_unknown_fields(payload, LEASE_FIELDS, 'lease')
        turn = optional_field(payload, 'activeDelegatedTurn')
        finalization = optional_field(payload, 'finalization')
        return cls(
            version=require_version(require_field(payload, 'version', 'lease')),
            token=require_id(require_field(payload, 'leaseId', 'lease'), 'leaseId',
                             max_length=256),
            provider=require_enum(require_field(payload, 'provider', 'lease'),
                                  'provider', AGENT_PROVIDERS),
            session_id=require_id(require_field(payload, 'sessionId', 'lease'),
                                  'sessionId', max_length=256),
            meeting_id=require_meeting_id(require_field(payload, 'meetingId', 'lease')),
            state=require_enum(require_field(payload, 'state', 'lease'), 'state', LEASE_STATES),
            acquired_at=require_timestamp(require_field(payload, 'acquiredAt', 'lease'),
                                          'acquiredAt'),
            last_heartbeat=require_timestamp(require_field(payload, 'lastHeartbeat', 'lease'),
                                             'lastHeartbeat'),
            owner=LeaseOwner.from_dict(require_field(payload, 'owner', 'lease')),
            active_delegated_turn=None if turn is None else require_id(turn, 'activeDelegatedTurn'),
            finalization=FinalizationState('none') if finalization is None
            else FinalizationState.from_dict(finalization),
        )


def _session_ref(agent_session):
    if isinstance(agent_session, AgentSessionRef):
        return agent_session
    return AgentSessionRef.from_dict(agent_session)


def _replace(lease, **changes):
    payload = lease.to_dict()
    payload.update({
        'leaseId': changes.get('token', lease.token),
        'state': changes.get('state', lease.state),
        'lastHeartbeat': changes.get('last_heartbeat', lease.last_heartbeat),
        'activeDelegatedTurn': changes.get('active_delegated_turn', lease.active_delegated_turn),
        'finalization': changes['finalization'].to_dict() if 'finalization' in changes
        else lease.finalization.to_dict(),
        'owner': changes['owner'].to_dict() if 'owner' in changes else lease.owner.to_dict(),
    })
    return SessionLease.from_dict(payload)


class SessionLeaseStore:
    """Exclusive per-(provider, sessionId) leases stored under a resolved root fd."""

    def __init__(self, root, *, clock=None, owner_factory=None, heartbeat_timeout=None):
        self._root_fd = None
        self.root = Path(root).resolve()
        if self.root.exists() and not self.root.is_dir():
            raise ValueError('lease store root must be a directory')
        self.root.mkdir(parents=True, mode=0o700, exist_ok=True)
        os.chmod(self.root, 0o700)
        try:
            self._root_fd = os.open(self.root, _directory_flags())
        except OSError as error:
            if error.errno == errno.ENOTDIR:
                raise ValueError('lease store root must be a directory') from error
            _raise_unsafe_path(error, 'lease store root must not be a symlink')
        os.fchmod(self._root_fd, 0o700)
        self._clock = clock or _utcnow
        self._owner_factory = owner_factory or default_owner_identity
        timeout = DEFAULT_HEARTBEAT_TIMEOUT if heartbeat_timeout is None else heartbeat_timeout
        self._heartbeat_timeout = timedelta(seconds=timeout)

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
            raise RuntimeError('lease store is closed')

    def _now(self):
        moment = self._clock()
        if moment.tzinfo is None:
            raise ValueError('clock must return a timezone-aware datetime')
        return moment.astimezone(timezone.utc)

    def _owner(self):
        owner = self._owner_factory()
        if isinstance(owner, LeaseOwner):
            return owner
        if isinstance(owner, str):
            owner = {'identity': owner}
        return LeaseOwner.from_dict(owner)

    def _names(self, provider, session_id):
        digest = lease_digest(provider, session_id)
        return digest + '.json', digest + '.lock'

    def _thread_lock(self, lock_name):
        key = str(self.root / lock_name)
        with _GLOBAL_LOCKS_GUARD:
            lock = _GLOBAL_LOCKS.get(key)
            if lock is None:
                lock = threading.Lock()
                _GLOBAL_LOCKS[key] = lock
            return lock

    def _open_named(self, name, flags, mode=0o600):
        self._ensure_open()
        try:
            return os.open(name, flags, mode, dir_fd=self._root_fd)
        except OSError as error:
            _raise_unsafe_path(error, 'lease path must not be a symlink')

    @contextmanager
    def _locked(self, provider, session_id):
        self._ensure_open()
        lease_name, lock_name = self._names(provider, session_id)
        thread_lock = self._thread_lock(lock_name)
        with thread_lock:
            exclusive = os.O_RDWR | os.O_CREAT | os.O_EXCL
            existing = os.O_RDWR
            if hasattr(os, 'O_NOFOLLOW'):
                existing |= os.O_NOFOLLOW
            try:
                lock_fd = self._open_named(lock_name, exclusive)
            except FileExistsError:
                lock_fd = self._open_named(lock_name, existing)
            try:
                os.fchmod(lock_fd, 0o600)
                fcntl.flock(lock_fd, fcntl.LOCK_EX)
                yield lease_name
            finally:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
                os.close(lock_fd)

    def _load(self, lease_name):
        flags = os.O_RDONLY
        if hasattr(os, 'O_NOFOLLOW'):
            flags |= os.O_NOFOLLOW
        try:
            fd = self._open_named(lease_name, flags)
        except FileNotFoundError:
            return None
        except OSError as error:
            if error.errno == errno.ENOENT:
                return None
            raise
        try:
            chunks = []
            while True:
                chunk = os.read(fd, 1024 * 64)
                if not chunk:
                    break
                chunks.append(chunk)
        finally:
            os.close(fd)
        if not chunks:
            return None
        payload = json.loads(b''.join(chunks).decode('utf-8'))
        reject_secrets(payload, 'lease')
        return SessionLease.from_dict(payload)

    def _store(self, lease_name, lease):
        payload = lease.to_dict()
        reject_secrets(payload, 'lease')
        encoded = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        tmp_name = lease_name + '.tmp'
        exclusive = os.O_RDWR | os.O_CREAT | os.O_EXCL
        fd = None
        try:
            try:
                os.unlink(tmp_name, dir_fd=self._root_fd)
            except FileNotFoundError:
                pass
            except OSError as error:
                _raise_unsafe_path(error, 'lease path must not be a symlink')
            fd = self._open_named(tmp_name, exclusive)
            os.fchmod(fd, 0o600)
            view = memoryview(encoded)
            offset = 0
            while offset < len(view):
                offset += os.write(fd, view[offset:])
            os.fsync(fd)
        finally:
            if fd is not None:
                os.close(fd)
        os.replace(tmp_name, lease_name, src_dir_fd=self._root_fd, dst_dir_fd=self._root_fd)
        os.fsync(self._root_fd)

    def _unlink_lease(self, lease_name):
        try:
            os.unlink(lease_name, dir_fd=self._root_fd)
        except FileNotFoundError:
            return
        except OSError as error:
            _raise_unsafe_path(error, 'lease path must not be a symlink')
        os.fsync(self._root_fd)

    def _require_token(self, lease, token):
        if lease is None:
            raise LeaseStateError('no lease exists for this session')
        if token != lease.token:
            raise LeaseOwnershipError('lease token does not match')
        return lease

    def _require_state(self, lease, allowed, action):
        if lease.state not in allowed:
            raise LeaseStateError(f'cannot {action} while lease is {lease.state}')

    def _heartbeat_expired(self, lease, now):
        last = datetime.fromisoformat(lease.last_heartbeat.replace('Z', '+00:00'))
        return now - last >= self._heartbeat_timeout

    def acquire(self, agent_session, meeting_id):
        session = _session_ref(agent_session)
        meeting_id = require_meeting_id(meeting_id)
        now = _iso(self._now())
        owner = self._owner()
        with self._locked(session.provider, session.session_id) as lease_name:
            current = self._load(lease_name)
            if current is not None:
                raise LeaseConflictError('agent session is already leased')
            lease = SessionLease.from_dict({
                'version': LEASE_VERSION,
                'leaseId': secrets.token_urlsafe(32),
                'provider': session.provider,
                'sessionId': session.session_id,
                'meetingId': meeting_id,
                'state': 'acquiring',
                'acquiredAt': now,
                'lastHeartbeat': now,
                'owner': owner.to_dict(),
                'activeDelegatedTurn': None,
                'finalization': {'status': 'none'},
            })
            self._store(lease_name, lease)
            return lease

    def get(self, provider, session_id):
        provider = require_enum(provider, 'provider', AGENT_PROVIDERS)
        session_id = require_id(session_id, 'sessionId', max_length=256)
        with self._locked(provider, session_id) as lease_name:
            return self._load(lease_name)

    def enter_meeting(self, provider, session_id, token):
        return self._mutate(
            provider, session_id, token, action='enter meeting',
            allowed=('acquiring',),
            changes={'state': 'in_meeting'},
        )

    def heartbeat(self, provider, session_id, token):
        return self._mutate(
            provider, session_id, token, action='heartbeat',
            allowed=('acquiring', 'in_meeting', 'finalizing'),
            changes={},
        )

    def start_turn(self, provider, session_id, token, turn_id):
        turn_id = require_id(turn_id, 'turnId')

        def apply(lease, now):
            self._require_state(lease, ('in_meeting',), 'start a delegated turn')
            if lease.active_delegated_turn:
                raise LeaseConflictError('a delegated turn is already active')
            return {'active_delegated_turn': turn_id}

        return self._mutate(provider, session_id, token, action='start turn', apply=apply)

    def finish_turn(self, provider, session_id, token, turn_id):
        turn_id = require_id(turn_id, 'turnId')

        def apply(lease, now):
            self._require_state(lease, ('in_meeting',), 'finish a delegated turn')
            if lease.active_delegated_turn != turn_id:
                raise LeaseStateError('delegated turn does not match the active turn')
            return {'active_delegated_turn': None}

        return self._mutate(provider, session_id, token, action='finish turn', apply=apply)

    def begin_finalization(self, provider, session_id, token):
        def apply(lease, now):
            self._require_state(lease, ('in_meeting',), 'begin finalization')
            if lease.active_delegated_turn:
                raise LeaseConflictError('cannot finalize while a delegated turn is active')
            return {
                'state': 'finalizing',
                'finalization': FinalizationState('in_progress', recorded_at=_iso(now)),
            }

        return self._mutate(provider, session_id, token, action='begin finalization', apply=apply)

    def complete_finalization(self, provider, session_id, token):
        def apply(lease, now):
            self._require_state(lease, ('finalizing',), 'complete finalization')
            if lease.finalization.status != 'in_progress':
                raise LeaseStateError('finalization is not in progress')
            return {
                'finalization': FinalizationState('completed', recorded_at=_iso(now)),
            }

        return self._mutate(provider, session_id, token, action='complete finalization', apply=apply)

    def fail(self, provider, session_id, token, reason=''):
        def apply(lease, now):
            self._require_state(lease, ('acquiring', 'in_meeting', 'finalizing'), 'fail')
            return {
                'state': 'failed',
                'active_delegated_turn': None,
                'finalization': FinalizationState(
                    'failed', recorded_at=_iso(now), reason=reason or None),
            }

        return self._mutate(provider, session_id, token, action='fail', apply=apply)

    def release(self, provider, session_id, token):
        provider = require_enum(provider, 'provider', AGENT_PROVIDERS)
        session_id = require_id(session_id, 'sessionId', max_length=256)
        with self._locked(provider, session_id) as lease_name:
            lease = self._require_token(self._load(lease_name), token)
            finalized = lease.state == 'finalizing' and lease.finalization.status == 'completed'
            failed = lease.state == 'failed'
            if not (finalized or failed):
                raise LeaseStateError('lease cannot be released before finalization or failure')
            self._unlink_lease(lease_name)
            return None

    def recover(self, provider, session_id, expected_token, *, owner_is_dead):
        provider = require_enum(provider, 'provider', AGENT_PROVIDERS)
        session_id = require_id(session_id, 'sessionId', max_length=256)
        expected_token = require_id(expected_token, 'token', max_length=256)
        now = self._now()
        with self._locked(provider, session_id) as lease_name:
            lease = self._load(lease_name)
            if lease is None:
                raise LeaseStateError('no lease exists for this session')
            if lease.token != expected_token:
                raise LeaseOwnershipError('lease token does not match')
            if not self._heartbeat_expired(lease, now):
                raise LeaseStateError('lease heartbeat has not expired')
            if not owner_is_dead:
                raise LeaseStateError('lease owner is still alive')
            recovered = _replace(
                lease,
                last_heartbeat=_iso(now),
                state='failed',
                active_delegated_turn=None,
                finalization=FinalizationState(
                    'failed', recorded_at=_iso(now), reason='stale owner confirmed dead'),
            )
            self._store(lease_name, recovered)
            self._unlink_lease(lease_name)
            return recovered

    def _mutate(self, provider, session_id, token, *, action, allowed=None, changes=None, apply=None):
        provider = require_enum(provider, 'provider', AGENT_PROVIDERS)
        session_id = require_id(session_id, 'sessionId', max_length=256)
        token = require_id(token, 'token', max_length=256)
        now = self._now()
        with self._locked(provider, session_id) as lease_name:
            lease = self._require_token(self._load(lease_name), token)
            if apply is not None:
                changes = apply(lease, now)
            else:
                self._require_state(lease, allowed, action)
            updates = dict(changes or {})
            updates['last_heartbeat'] = _iso(now)
            updated = _replace(lease, **updates)
            self._store(lease_name, updated)
            return updated
