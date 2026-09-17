"""Authenticated loopback daemon over schemas, EventStore, and SessionLeaseStore."""
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
import asyncio
import hashlib
import hmac
import ipaddress
import json
import os
import secrets
import threading

from aiohttp import web

from agent_sessions import AGENT_PROVIDERS, MeetingSession
from approvals import (
    DEFAULT_TTL_SECONDS, build_approval, event_decision_payload,
    event_request_payload, expire_if_needed, public_approval,
)
from context_handoff import ContextHandoff
from event_store import JsonlEventStore
from meeting_handoff import MeetingHandoff
from meeting_repository import MeetingCorruptionError, MeetingRecord, MeetingRepository
from meeting_urls import platform_for_url
from permissions import DEFAULT_PERMISSIONS, bound_requested_permissions, permission_mode
from schema_validation import reject_secrets, reject_unknown_fields, require_enum, require_id
from session_continuity import validate_agent_session
from session_leases import LeaseConflictError, LeaseCorruptionError, LeaseStateError, SessionLeaseStore
from git_actions import CommitRequest, GitOperation, PushRequest, build_result
from git_broker import GitBroker
from workspace_isolation import IsolationError


CREATE_FIELDS = ('meetingUrl', 'agentSession', 'context', 'permissions', 'camera', 'screenShare')
CREATE_APPROVAL_FIELDS = (
    'category', 'permission', 'summary', 'scope', 'delegationId', 'ttlSeconds', 'action',
)
DECIDE_APPROVAL_FIELDS = ('decision', 'approvalId', 'decidedAt')
CREATE_COMMIT_FIELDS = (
    'version', 'id', 'meetingId', 'delegationId', 'expectedHead', 'message', 'files',
    'artifactId',
)
CREATE_PUSH_FIELDS = (
    'version', 'id', 'meetingId', 'delegationId', 'commitSha', 'remote', 'branch',
)
LIFECYCLE_STATES = ('joining', 'waiting_for_admission', 'live', 'ended')
FORWARD_TRANSITIONS = {
    'joining': frozenset({'waiting_for_admission', 'live', 'ended'}),
    'waiting_for_admission': frozenset({'live', 'ended'}),
    'live': frozenset({'ended'}),
    'ended': frozenset(),
}
LIFECYCLE_EVENTS = {
    'joining': 'meeting.joining',
    'waiting_for_admission': 'meeting.waiting_for_admission',
    'live': 'meeting.live',
    'ended': 'meeting.ended',
}
DEFAULT_MAX_BODY = 256 * 1024
DEFAULT_SSE_POLL = 0.05
DEFAULT_SSE_HEARTBEAT = 15.0


class DaemonError(Exception):
    def __init__(self, status, code, message):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


class UnconfiguredMeetingSupervisor:
    async def start(self, meeting, camera_settings=None, screen_share_settings=None):
        raise RuntimeError('meeting supervisor is not configured')

    async def add_context(self, meeting_id, context):
        raise RuntimeError('meeting supervisor is not configured')

    async def cancel(self, meeting_id):
        raise RuntimeError('meeting supervisor is not configured')

    async def retry_handoff(self, meeting_id):
        raise RuntimeError('meeting supervisor is not configured')


def require_loopback_bind(host):
    try:
        address = ipaddress.ip_address(host)
    except ValueError as error:
        raise ValueError('daemon bind address must be a loopback IP') from error
    if not address.is_loopback:
        raise ValueError('daemon bind address must be a loopback IP')
    return host


def _utcnow():
    return datetime.now(timezone.utc)


def _iso(moment):
    return moment.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _json_error(status, code, message):
    return web.json_response({'error': {'code': code, 'message': message}}, status=status)


def _public_json(payload, status=200):
    ensure_public(payload)
    return web.json_response(payload, status=status)


def ensure_public(value):
    reject_secrets(value, 'response')
    if isinstance(value, dict):
        if 'leaseId' in value:
            raise RuntimeError('refused to return lease ownership secret')
        for item in value.values():
            ensure_public(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            ensure_public(item)


def public_lease_status(lease):
    if lease is None:
        return None
    payload = lease.to_dict()
    payload.pop('leaseId', None)
    ensure_public(payload)
    return payload


def _new_id(prefix):
    return prefix + secrets.token_hex(8)


class RuntimeDaemon:
    """Loopback meeting daemon.

    Threading contract: the aiohttp event loop owns async entry points
        (create, context, cancel, retry_handoff). Synchronous supervisor callbacks
        (transition, store_handoff, prepare_finalization, note_append_failure,
        record_finalization_failure) may run on
    that loop or on a worker thread. Each meeting_id has an asyncio.Lock
    that serializes async entry points, including supervisor I/O, and a
    threading.RLock used only around durable local writes — never across
    await. Sync callbacks take the RLock for their full body so identical
    concurrent handoff/finalization stays exactly-once without hopping
    threads. Different meetings use different locks and do not block each
    other. Callers must not wait on the event loop while holding the
    RLock.
    """

    def __init__(
        self,
        root,
        *,
        supervisor=None,
        clock=None,
        event_store=None,
        lease_store=None,
        meeting_store=None,
        git_broker=None,
        jobs_dir=None,
        visual_analyzer=None,
        provider_registry=None,
        sse_poll_interval=DEFAULT_SSE_POLL,
        sse_heartbeat_interval=DEFAULT_SSE_HEARTBEAT,
    ):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, mode=0o700, exist_ok=True)
        os.chmod(self.root, 0o700)
        self.supervisor = supervisor or UnconfiguredMeetingSupervisor()
        self._clock = clock or _utcnow
        self.meetings = meeting_store or MeetingRepository(self.root / 'meetings')
        self.events = event_store or JsonlEventStore(self.root / 'events')
        self.leases = lease_store or SessionLeaseStore(self.root / 'leases', clock=self._clock)
        self.sse_poll_interval = sse_poll_interval
        self.sse_heartbeat_interval = sse_heartbeat_interval
        self._lock_guard = threading.Lock()
        self._async_locks = {}
        self._write_locks = {}
        self._approval_waiters = {}
        from artifact_store import ArtifactStore
        self.artifacts = ArtifactStore(self.root / '.colleague' / 'artifacts')
        self.git_broker = git_broker or GitBroker(artifacts=self.artifacts)
        self._git_runners = {}
        self._git_cancels = {}
        self.jobs_dir = Path(jobs_dir) if jobs_dir is not None else (self.root / 'jobs')
        from screen_share_pipeline import ScreenShareHost
        from visual_analysis import UnavailableVisualAnalysisProvider
        self.screen_share_host = ScreenShareHost(
            artifacts=self.artifacts,
            analyzer=visual_analyzer or UnavailableVisualAnalysisProvider(),
            now=lambda: _iso(self._now()))
        if provider_registry is None:
            from providers.registry import ProviderRegistry
            provider_registry = ProviderRegistry()
        self.provider_registry = provider_registry

    def close(self):
        for store in (self.meetings, self.events, self.leases):
            closer = getattr(store, 'close', None)
            if closer is not None:
                closer()

    def list_providers(self):
        registry = self.provider_registry
        payload = {'providers': registry.capabilities()}
        ensure_public(payload)
        return payload

    def public_meeting(self, session):
        payload = session.to_dict()
        provider_id = session.agent_session.provider
        payload['providerCapabilities'] = self.provider_registry.capabilities(provider_id)
        ensure_public(payload)
        return payload

    def _now(self):
        moment = self._clock()
        if moment.tzinfo is None:
            raise ValueError('clock must return a timezone-aware datetime')
        return moment.astimezone(timezone.utc)

    def _async_lock(self, meeting_id):
        with self._lock_guard:
            lock = self._async_locks.get(meeting_id)
            if lock is None:
                lock = asyncio.Lock()
                self._async_locks[meeting_id] = lock
            return lock

    def _write_lock(self, meeting_id):
        with self._lock_guard:
            lock = self._write_locks.get(meeting_id)
            if lock is None:
                lock = threading.RLock()
                self._write_locks[meeting_id] = lock
            return lock

    @contextmanager
    def _serialize_writes(self, meeting_id):
        lock = self._write_lock(meeting_id)
        lock.acquire()
        try:
            yield
        finally:
            lock.release()

    def _append(self, meeting_id, event_type, **payload):
        event = {
            'version': 1,
            'id': _new_id('evt-'),
            'meetingId': meeting_id,
            'timestamp': _iso(self._now()),
            'type': event_type,
        }
        event.update(payload)
        return self.events.append(event)

    def _has_event(self, meeting_id, event_type):
        return any(event.type == event_type for event in self.events.replay(meeting_id))

    def _append_once(self, meeting_id, event_type, **payload):
        if self._has_event(meeting_id, event_type):
            return None
        return self._append(meeting_id, event_type, **payload)

    def _record(self, meeting_id):
        try:
            record = self.meetings.get(meeting_id)
        except MeetingCorruptionError as error:
            raise DaemonError(409, 'corrupt_snapshot', str(error)) from error
        if record is None:
            raise DaemonError(404, 'not_found', 'meeting not found')
        return record

    def _persist(self, session, lease_token=None):
        token = lease_token
        if token is None:
            current = self.meetings.get(session.id)
            token = None if current is None else current.lease_token
        return self.meetings.put(session, token)

    def _replace_session(self, session, **changes):
        payload = session.to_dict()
        payload.update(changes)
        return MeetingSession.from_dict(payload)

    def _active_record(self, provider, session_id, lease):
        try:
            record = self.meetings.get(lease.meeting_id)
        except MeetingCorruptionError as error:
            raise DaemonError(409, 'corrupt_snapshot', str(error)) from error
        if record is None:
            return None
        agent = record.session.agent_session
        if agent.provider != provider or agent.session_id != session_id:
            raise DaemonError(409, 'conflict', 'meeting record does not match the active lease')
        if record.lease_token and record.lease_token != lease.token:
            raise DaemonError(409, 'conflict', 'meeting lease token does not match')
        return record

    def _fail_lease(self, record, reason):
        session = record.session
        agent = session.agent_session
        token = record.lease_token
        lease = self.leases.get(agent.provider, agent.session_id)
        if lease is None:
            self.meetings.clear_lease_token(session.id)
            self._append_once(session.id, 'agent_session.released', sessionId=agent.session_id)
            return
        if not token:
            token = lease.token
        if record.lease_token and record.lease_token != lease.token:
            raise DaemonError(409, 'conflict', 'meeting lease token does not match')
        try:
            if lease.state != 'failed':
                self.leases.fail(agent.provider, agent.session_id, token, reason=reason)
            self.leases.release(agent.provider, agent.session_id, token)
        except LeaseStateError as error:
            raise DaemonError(409, 'conflict', str(error)) from error
        except LeaseConflictError as error:
            raise DaemonError(409, 'conflict', str(error)) from error
        if self.leases.get(agent.provider, agent.session_id) is not None:
            raise DaemonError(503, 'lease_release_failed', 'lease release did not succeed')
        self.meetings.clear_lease_token(session.id)
        self._append_once(session.id, 'agent_session.released', sessionId=agent.session_id)

    def _create_failure(self, error, phase):
        if isinstance(error, DaemonError):
            return error
        if phase == 'start':
            message = 'meeting supervisor failed to start'
            if str(error) == 'meeting supervisor is not configured':
                message = str(error)
            return DaemonError(503, 'supervisor_unavailable', message)
        return DaemonError(503, 'create_failed', 'meeting create did not complete')

    def _persist_create_abort(self, session, lease, record, reason):
        token = lease.token if lease is not None else (None if record is None else record.lease_token)
        ended = self._replace_session(session, state='ended')
        try:
            record = self.meetings.put(ended, token)
        except Exception:
            if record is None:
                record = MeetingRecord(session=ended, lease_token=token)
            else:
                record = MeetingRecord(
                    session=ended, lease_token=token, handoff=record.handoff)
        try:
            self._append_once(session.id, 'meeting.ended', reason=reason)
        except Exception:
            pass
        return record

    async def _abort_create(self, session, lease, record, started, phase, error):
        reason = 'supervisor_start_failed' if phase == 'start' else 'create_failed'
        if started:
            try:
                await self.supervisor.cancel(session.id)
            except Exception:
                pass
        cleanup_error = None
        with self._serialize_writes(session.id):
            record = self._persist_create_abort(session, lease, record, reason)
            try:
                self._fail_lease(record, reason)
            except Exception as fail_error:
                cleanup_error = fail_error
        if self.leases.get(session.agent_session.provider, session.agent_session.session_id) is not None:
            raise DaemonError(
                503, 'retry_required', 'meeting create cleanup did not complete') from (
                    cleanup_error or error)
        raise self._create_failure(error, phase) from error

    async def create_meeting(self, payload):
        reject_unknown_fields(payload, CREATE_FIELDS, 'meeting')
        try:
            meeting_url = payload['meetingUrl']
            platform = platform_for_url(meeting_url)
        except (KeyError, ValueError) as error:
            raise DaemonError(422, 'invalid_request', 'meetingUrl is invalid') from error
        meeting_id = _new_id('mtg-')
        started_at = _iso(self._now())
        try:
            from visual_presence import parse_camera_settings
            from screen_share import parse_screen_share_settings
            camera = parse_camera_settings(payload.get('camera'))
            screen_share = parse_screen_share_settings(payload.get('screenShare'))
            requested = payload.get('permissions')
            if requested is None:
                requested = DEFAULT_PERMISSIONS.to_dict()
            session = MeetingSession.from_dict({
                'id': meeting_id,
                'platform': platform,
                'meetingUrl': meeting_url,
                'agentSession': payload.get('agentSession'),
                'context': payload.get('context'),
                'permissions': requested,
                'state': 'joining',
                'startedAt': started_at,
                'cameraEnabled': camera['enabled'],
                'cameraState': 'off' if not camera['enabled'] else 'starting',
                'visualState': 'joining',
            })
            validate_agent_session(session.agent_session)
            provider_id = session.agent_session.provider
            if provider_id not in AGENT_PROVIDERS:
                raise ValueError('unknown coding agent provider')
            if provider_id not in ('codex', 'generic'):
                registry = getattr(self, 'provider_registry', None)
                adapter = registry.get(provider_id) if registry is not None else None
                if adapter is None:
                    raise ValueError('unknown coding agent provider')
                check = adapter.validate_session
                import inspect
                result = check(session.agent_session)
                if inspect.isawaitable(result):
                    result = await result
                if isinstance(result, dict) and result.get('ok') is False:
                    raise ValueError(result.get('code') or result.get('error') or 'provider session is invalid')
            session = self._replace_session(
                session,
                permissions=bound_requested_permissions(
                    session.permissions, session.agent_session).to_dict(),
            )
        except (TypeError, ValueError) as error:
            raise DaemonError(422, 'invalid_request', str(error)) from error
        camera_settings = {
            'cameraEnabled': camera['enabled'],
            'cameraDefaultOn': camera['defaultOn'],
            'cameraAvatarDataUri': camera['avatarDataUri'],
        }
        screen_share_settings = dict(screen_share)
        agent = session.agent_session
        try:
            lease = self.leases.acquire(agent, meeting_id)
        except LeaseConflictError as error:
            raise DaemonError(409, 'conflict', 'agent session is already leased') from error
        record = None
        started = False
        phase = 'snapshot'
        async with self._async_lock(meeting_id):
            try:
                with self._serialize_writes(meeting_id):
                    record = self.meetings.put(session, lease.token)
                    phase = 'locked_event'
                    self._append(meeting_id, 'agent_session.locked', sessionId=agent.session_id)
                    phase = 'joining_event'
                    self._append(meeting_id, 'meeting.joining')
                    self._append(meeting_id, 'presence.updated',
                                 cameraEnabled=session.camera_enabled,
                                 cameraState=session.camera_state,
                                 visualState=session.visual_state)
                    self._init_screen_share_locked(meeting_id, screen_share_settings)
                phase = 'start'
                started = True
                await self.supervisor.start(
                    session, camera_settings=camera_settings,
                    screen_share_settings=screen_share_settings)
                phase = 'enter_meeting'
                with self._serialize_writes(meeting_id):
                    self.leases.enter_meeting(agent.provider, agent.session_id, lease.token)
                return session
            except Exception as error:
                await self._abort_create(session, lease, record, started, phase, error)

    async def get_meeting(self, meeting_id):
        return self._record(meeting_id).session

    async def update_context(self, meeting_id, payload):
        try:
            context = ContextHandoff.from_dict(payload)
        except (TypeError, ValueError) as error:
            raise DaemonError(422, 'invalid_request', str(error)) from error
        async with self._async_lock(meeting_id):
            with self._serialize_writes(meeting_id):
                record = self._record(meeting_id)
                if record.session.state == 'ended':
                    raise DaemonError(409, 'conflict', 'cannot update context after the meeting has ended')
                updated = self._replace_session(record.session, context=context.to_dict())
                self._persist(updated, record.lease_token)
            try:
                await self.supervisor.add_context(meeting_id, context)
            except DaemonError:
                raise
            except Exception as error:
                raise DaemonError(
                    503, 'supervisor_context_failed',
                    'meeting supervisor failed to update context') from error
            with self._serialize_writes(meeting_id):
                if record.lease_token:
                    agent = updated.agent_session
                    self.leases.heartbeat(agent.provider, agent.session_id, record.lease_token)
                return updated

    async def cancel_meeting(self, meeting_id):
        async with self._async_lock(meeting_id):
            with self._serialize_writes(meeting_id):
                record = self._record(meeting_id)
                session = record.session
                if session.state == 'ended':
                    self._cancel_pending_locked(meeting_id, reason='cancelled')
                    return session
            try:
                await self.supervisor.cancel(meeting_id)
            except DaemonError:
                raise
            except Exception as error:
                raise DaemonError(
                    503, 'supervisor_cancel_failed',
                    'meeting supervisor failed to cancel') from error
            with self._serialize_writes(meeting_id):
                record = self._record(meeting_id)
                if record.session.state == 'ended':
                    return record.session
                ended = self._replace_session(record.session, state='ended')
                self._persist(ended, record.lease_token)
                self._append_once(meeting_id, 'meeting.ended', reason='cancelled')
                self._cancel_pending_locked(meeting_id, reason='cancelled')
                if record.lease_token:
                    agent = ended.agent_session
                    self.leases.heartbeat(agent.provider, agent.session_id, record.lease_token)
                return ended

    async def get_handoff(self, meeting_id):
        record = self._record(meeting_id)
        if record.handoff is None:
            raise DaemonError(404, 'not_found', 'handoff is not ready')
        return record.handoff

    def record_event(self, meeting_id, event_type, **payload):
        self._record(meeting_id)
        return self._append(meeting_id, event_type, **payload)

    def transition(self, meeting_id, state, *, reason=None):
        require_enum(state, 'state', LIFECYCLE_STATES)
        with self._serialize_writes(meeting_id):
            record = self._record(meeting_id)
            current = record.session.state
            if state == current:
                return record.session
            if state not in FORWARD_TRANSITIONS[current]:
                raise DaemonError(409, 'conflict', f'cannot transition from {current} to {state}')
            updated = self._replace_session(record.session, state=state)
            self._persist(updated, record.lease_token)
            event_type = LIFECYCLE_EVENTS[state]
            extra = {}
            if event_type == 'meeting.ended':
                extra['reason'] = reason or 'ended'
            self._append_once(meeting_id, event_type, **extra)
            if event_type == 'meeting.ended':
                self._cancel_pending_locked(meeting_id, reason=extra['reason'])
            if record.lease_token:
                agent = updated.agent_session
                self.leases.heartbeat(agent.provider, agent.session_id, record.lease_token)
            return updated

    def apply_presence(self, meeting_id, **fields):
        from schema_validation import omit_none
        from agent_sessions import CAMERA_STATES, DEGRADED_REASONS, VISUAL_STATES
        allowed = {
            'cameraEnabled': fields.get('cameraEnabled'),
            'cameraState': fields.get('cameraState'),
            'visualState': fields.get('visualState'),
            'degradedReason': fields.get('degradedReason'),
        }
        if allowed['cameraState'] not in CAMERA_STATES and allowed['cameraState'] is not None:
            return self._record(meeting_id).session
        if allowed['visualState'] not in VISUAL_STATES and allowed['visualState'] is not None:
            return self._record(meeting_id).session
        if allowed['degradedReason'] not in DEGRADED_REASONS and allowed['degradedReason'] is not None:
            return self._record(meeting_id).session
        with self._serialize_writes(meeting_id):
            record = self._record(meeting_id)
            payload = record.session.to_dict()
            changed = False
            for key, value in allowed.items():
                if value is None and key != 'degradedReason':
                    continue
                if key == 'degradedReason' and 'degradedReason' not in fields:
                    continue
                if payload.get(key) != value:
                    if value is None:
                        payload.pop(key, None)
                    else:
                        payload[key] = value
                    changed = True
            if not changed:
                return record.session
            updated = MeetingSession.from_dict(payload)
            self._persist(updated, record.lease_token)
            public = omit_none({
                'cameraEnabled': updated.camera_enabled,
                'cameraState': updated.camera_state,
                'visualState': updated.visual_state,
                'degradedReason': updated.degraded_reason,
            })
            if 'cameraEnabled' in public and 'cameraState' in public and 'visualState' in public:
                self._append(meeting_id, 'presence.updated', **public)
            return updated

    def _approval_workspace(self, meeting_id):
        return self._record(meeting_id).session.agent_session.workspace

    def _waiter(self, approval_id):
        with self._lock_guard:
            event = self._approval_waiters.get(approval_id)
            if event is None:
                event = threading.Event()
                self._approval_waiters[approval_id] = event
            return event

    def _signal_approval(self, approval_id):
        self._waiter(approval_id).set()

    def _public_approval_list(self, records):
        return [public_approval(item) for item in records]

    def public_approvals(self, meeting_id):
        with self._serialize_writes(meeting_id):
            records = self._expire_locked(meeting_id)
            return self._public_approval_list(records)

    def _expire_locked(self, meeting_id):
        changed = []

        def mutate(records):
            updated = []
            for raw in records:
                next_record = expire_if_needed(raw, now=self._now())
                if raw.get('status') == 'pending' and next_record.get('status') == 'expired':
                    changed.append(next_record)
                updated.append(next_record)
            return updated

        try:
            stored = self.meetings.update_approvals(
                meeting_id, mutate, workspace=self._approval_workspace(meeting_id))
        except FileNotFoundError as error:
            raise DaemonError(404, 'not_found', 'meeting not found') from error
        for item in changed:
            self._append(meeting_id, 'approval.expired', approvalId=item['id'])
            self._signal_approval(item['id'])
        if changed:
            self._sync_approval_presence_locked(meeting_id)
        return stored

    def _sync_approval_presence_locked(self, meeting_id):
        record = self._record(meeting_id)
        pending = False
        stored = self.meetings.list_approvals(
            meeting_id, workspace=record.session.agent_session.workspace) or []
        for item in stored:
            if item.get('status') == 'pending':
                pending = True
                break
        if pending and record.session.state != 'ended':
            self.apply_presence(meeting_id, visualState='needs_attention')
        elif record.session.state == 'live' and record.session.visual_state == 'needs_attention':
            self.apply_presence(meeting_id, visualState='listening')
        elif record.session.state == 'ended' and record.session.visual_state != 'ended':
            self.apply_presence(meeting_id, visualState='ended')

    def _cancel_pending_locked(self, meeting_id, reason='meeting_ended'):
        changed = []
        label = str(reason or 'meeting_ended')[:64]

        def mutate(records):
            updated = []
            for raw in records:
                if raw.get('status') == 'pending':
                    item = dict(raw)
                    item['status'] = 'cancelled'
                    item['resolvedAt'] = _iso(self._now())
                    changed.append(item)
                    updated.append(item)
                else:
                    updated.append(raw)
            return updated

        try:
            self.meetings.update_approvals(
                meeting_id, mutate, workspace=self._approval_workspace(meeting_id))
        except FileNotFoundError:
            changed = []
        for item in changed:
            self._append(meeting_id, 'approval.cancelled', approvalId=item['id'], reason=label)
            self._signal_approval(item['id'])
        if changed:
            self._sync_approval_presence_locked(meeting_id)
        self._cancel_git_ops_locked(meeting_id, label)
        self._pause_screen_share_locked(meeting_id, reason='cancelled' if 'cancel' in label else 'ended')

    def _enrich_handoff(self, handoff, session):
        payload = handoff.to_dict()
        if payload.get('permissions') is None:
            payload['permissions'] = session.permissions.to_dict()
        if payload.get('approvals') is None:
            stored = self.meetings.list_approvals(
                session.id, workspace=session.agent_session.workspace) or []
            payload['approvals'] = self._public_approval_list(stored)
        work = list(payload.get('workPerformed') or [])
        artifacts = list(payload.get('artifacts') or [])
        existing_tasks = {item.get('taskId') for item in work}
        existing_arts = {item.get('artifactId') for item in artifacts}
        for op in self.meetings.list_git_ops(session.id) or []:
            result = op.get('result') or {}
            status = result.get('status') or op.get('status')
            if status in ('requested', 'approved', 'running'):
                continue
            work_status = 'completed' if status == 'completed' else (
                'cancelled' if status == 'cancelled' else 'failed')
            if op.get('id') not in existing_tasks:
                work.append({
                    'taskId': op['id'],
                    'summary': result.get('summary') or 'Recorded reviewed files',
                    'status': work_status,
                })
            for artifact_id in result.get('artifactIds') or []:
                if artifact_id not in existing_arts:
                    artifacts.append({'artifactId': artifact_id, 'path': artifact_id})
                    existing_arts.add(artifact_id)
        screen = self.meetings.get_screen_share(session.id) or {}
        for observation in screen.get('observations') or []:
            obs_id = observation.get('id')
            if obs_id and obs_id not in existing_tasks:
                work.append({
                    'taskId': obs_id,
                    'summary': observation.get('summary') or 'Shared-content observation',
                    'status': 'completed',
                })
                existing_tasks.add(obs_id)
            frame_id = observation.get('frameArtifactId')
            if frame_id and frame_id not in existing_arts:
                artifacts.append({'artifactId': frame_id, 'path': frame_id})
                existing_arts.add(frame_id)
        payload['workPerformed'] = work
        payload['artifacts'] = artifacts
        return MeetingHandoff.from_dict(payload)

    def _find_approval(self, records, approval_id):
        for item in records:
            if item.get('id') == approval_id:
                return item
        return None

    def create_approval(self, meeting_id, payload):
        reject_unknown_fields(payload, CREATE_APPROVAL_FIELDS, 'approval')
        reject_secrets(payload, 'approval')
        category = payload.get('category') or payload.get('permission') or payload.get('action')
        ttl = payload.get('ttlSeconds', DEFAULT_TTL_SECONDS)
        if ttl is None:
            ttl = DEFAULT_TTL_SECONDS
        if not isinstance(ttl, int) or isinstance(ttl, bool) or ttl < 1 or ttl > 3600:
            raise DaemonError(422, 'invalid_request', 'ttlSeconds must be between 1 and 3600')
        with self._serialize_writes(meeting_id):
            record = self._record(meeting_id)
            if record.session.state == 'ended':
                raise DaemonError(409, 'conflict', 'cannot create approvals after the meeting has ended')
            mode = permission_mode(record.session.permissions, category)
            if mode != 'approval-required':
                raise DaemonError(
                    422, 'invalid_request',
                    'this action is not configured for approval')
            try:
                built = build_approval(
                    approval_id=_new_id('appr-'),
                    meeting_id=meeting_id,
                    category=category,
                    summary=payload.get('summary'),
                    created_at=_iso(self._now()),
                    ttl_seconds=ttl,
                    delegation_id=payload.get('delegationId'),
                    scope=payload.get('scope'),
                    workspace=record.session.agent_session.workspace,
                )
            except (TypeError, ValueError) as error:
                raise DaemonError(422, 'invalid_request', str(error)) from error
            stored_payload = built.to_dict()

            def mutate(records):
                records.append(stored_payload)
                return records

            self.meetings.update_approvals(
                meeting_id, mutate, workspace=record.session.agent_session.workspace)
            self._append(
                meeting_id, 'approval.required', request=event_request_payload(built))
            self._waiter(stored_payload['id'])
            self._sync_approval_presence_locked(meeting_id)
            return public_approval(stored_payload)

    def list_approvals(self, meeting_id):
        self._record(meeting_id)
        with self._serialize_writes(meeting_id):
            records = self._expire_locked(meeting_id)
            return {'approvals': self._public_approval_list(records)}

    def get_approval(self, meeting_id, approval_id):
        require_id(approval_id, 'approvalId')
        self._record(meeting_id)
        with self._serialize_writes(meeting_id):
            records = self._expire_locked(meeting_id)
            found = self._find_approval(records, approval_id)
            if found is None:
                raise DaemonError(404, 'not_found', 'approval not found')
            if found.get('meetingId') != meeting_id:
                raise DaemonError(409, 'conflict', 'approval does not belong to this meeting')
            return public_approval(found)

    def decide_approval(self, meeting_id, approval_id, payload):
        reject_unknown_fields(payload, DECIDE_APPROVAL_FIELDS, 'decision')
        reject_secrets(payload, 'decision')
        require_id(approval_id, 'approvalId')
        if payload.get('approvalId') not in (None, approval_id):
            raise DaemonError(409, 'conflict', 'approvalId does not match the request')
        try:
            decision = require_enum(payload.get('decision'), 'decision', ('approved', 'denied'))
        except ValueError as error:
            raise DaemonError(422, 'invalid_request', str(error)) from error
        with self._serialize_writes(meeting_id):
            record = self._record(meeting_id)
            records = self._expire_locked(meeting_id)
            found = self._find_approval(records, approval_id)
            if found is None:
                raise DaemonError(404, 'not_found', 'approval not found')
            if found.get('meetingId') != meeting_id:
                raise DaemonError(409, 'conflict', 'approval does not belong to this meeting')
            status = found.get('status')
            if status == decision:
                return public_approval(found)
            if status != 'pending':
                raise DaemonError(409, 'conflict', 'approval decision is stale')
            updated = dict(found)
            updated['status'] = decision
            updated['decision'] = decision
            updated['resolvedAt'] = _iso(self._now())

            def mutate(current):
                out = []
                for item in current:
                    if item.get('id') == approval_id:
                        out.append(updated)
                    else:
                        out.append(item)
                return out

            self.meetings.update_approvals(
                meeting_id, mutate, workspace=record.session.agent_session.workspace)
            event_type = 'approval.approved' if decision == 'approved' else 'approval.denied'
            self._append(
                meeting_id, event_type,
                decision=event_decision_payload(updated, updated['resolvedAt']))
            self._signal_approval(approval_id)
            self._sync_approval_presence_locked(meeting_id)
            return public_approval(updated)

    def consume_approval(self, meeting_id, approval_id):
        require_id(approval_id, 'approvalId')
        with self._serialize_writes(meeting_id):
            record = self._record(meeting_id)
            records = self._expire_locked(meeting_id)
            found = self._find_approval(records, approval_id)
            if found is None:
                raise DaemonError(404, 'not_found', 'approval not found')
            if found.get('status') != 'approved':
                return public_approval(found)
            if found.get('consumed'):
                raise DaemonError(409, 'conflict', 'approval authorization was already used')
            updated = dict(found)
            updated['consumed'] = True

            def mutate(current):
                out = []
                for item in current:
                    out.append(updated if item.get('id') == approval_id else item)
                return out

            self.meetings.update_approvals(
                meeting_id, mutate, workspace=record.session.agent_session.workspace)
            return public_approval(updated)

    async def wait_for_decision(self, meeting_id, approval_id, *, timeout=None):
        require_id(approval_id, 'approvalId')
        waiter = self._waiter(approval_id)
        deadline = None if timeout is None else (asyncio.get_event_loop().time() + float(timeout))
        while True:
            current = self.get_approval(meeting_id, approval_id)
            if current.get('status') != 'pending':
                if current.get('status') == 'approved':
                    return self.consume_approval(meeting_id, approval_id)
                return current
            remaining = 0.05
            if deadline is not None:
                remaining = min(remaining, max(0.0, deadline - asyncio.get_event_loop().time()))
                if remaining <= 0:
                    return self.get_approval(meeting_id, approval_id)
            await asyncio.to_thread(waiter.wait, remaining)
            waiter.clear()

    def _public_git_op(self, payload):
        return GitOperation.from_dict(payload).public_dict()

    def _find_git_op(self, meeting_id, operation_id):
        for item in self.meetings.list_git_ops(meeting_id) or []:
            if item.get('id') == operation_id:
                return item
        return None

    def _git_approval_summary(self, kind, request):
        if kind == 'commit':
            count = len(request.get('files') or [])
            label = 'file' if count == 1 else 'files'
            return 'Record {0} reviewed {1}'.format(count, label)
        return 'Send the approved snapshot to the configured remote'

    def _git_approval_scope(self, kind, request):
        if kind == 'commit':
            names = ','.join(item['path'] for item in request.get('files') or [])[:128]
            return {
                'host': 'workspace',
                'files': names or 'reviewed',
                'head': request['expectedHead'],
            }
        return {
            'host': 'workspace',
            'remote': request['remote'],
            'branch': request['branch'][:128],
            'commit': request['commitSha'],
        }

    def _replace_git_op(self, meeting_id, operation):
        payload = GitOperation.from_dict(operation).to_dict()

        def mutate(current):
            out = [item for item in current if item.get('id') != payload['id']]
            out.append(payload)
            return out

        self.meetings.update_git_ops(meeting_id, mutate)
        return payload

    def _cancel_git_ops_locked(self, meeting_id, reason):
        label = str(reason or 'meeting_ended')[:64]
        changed = []

        def mutate(ops):
            updated = []
            for raw in ops:
                if raw.get('status') in ('requested', 'approved', 'running'):
                    item = dict(raw)
                    item['status'] = 'cancelled'
                    item['updatedAt'] = _iso(self._now())
                    item['result'] = build_result(
                        operation_id=item['id'], meeting_id=meeting_id, kind=item['kind'],
                        status='cancelled', summary='Git operation was cancelled',
                    ).to_dict()
                    changed.append(item)
                    updated.append(item)
                else:
                    updated.append(raw)
            return updated

        try:
            self.meetings.update_git_ops(meeting_id, mutate)
        except FileNotFoundError:
            return
        for item in changed:
            self._append(
                meeting_id, 'git.action.cancelled', operationId=item['id'], reason=label)
            cancel = self._git_cancels.get(meeting_id + ':' + item['id'])
            if cancel is not None:
                cancel.set()

    def _ensure_git_runner(self, meeting_id, operation_id):
        key = meeting_id + ':' + operation_id
        with self._lock_guard:
            if self._git_runners.get(key):
                return
            self._git_runners[key] = True
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            with self._lock_guard:
                self._git_runners.pop(key, None)
            return

        def _clear(_task):
            with self._lock_guard:
                self._git_runners.pop(key, None)

        loop.create_task(self._run_git_op(meeting_id, operation_id)).add_done_callback(_clear)

    def _finish_git_op(self, meeting_id, operation, *, status, summary, outcome=None,
                       artifact_ids=None, conflict=False):
        outcome = outcome or {}
        kind = operation['kind']
        result = build_result(
            operation_id=operation['id'], meeting_id=meeting_id, kind=kind, status=status,
            summary=summary, commit_sha=outcome.get('commitSha'),
            parent_sha=outcome.get('parentSha'), tree_sha=outcome.get('treeSha'),
            remote=outcome.get('remote'), branch=outcome.get('branch'),
            artifact_ids=artifact_ids, conflict=conflict,
        ).to_dict()
        stored = dict(operation)
        stored['status'] = status
        stored['updatedAt'] = _iso(self._now())
        stored['result'] = result
        self._replace_git_op(meeting_id, stored)
        event = 'git.action.completed' if status == 'completed' else (
            'git.action.cancelled' if status == 'cancelled' else 'git.action.failed')
        if event == 'git.action.cancelled':
            self._append(meeting_id, event, operationId=operation['id'], reason='cancelled')
        else:
            self._append(meeting_id, event, result=result)
        return self._public_git_op(stored)

    async def _run_git_op(self, meeting_id, operation_id):
        operation = self._find_git_op(meeting_id, operation_id)
        if operation is None or operation.get('status') == 'completed':
            return
        approval_id = operation.get('approvalId')
        if not approval_id:
            self._finish_git_op(
                meeting_id, operation, status='failed', summary='Git operation is missing approval')
            return
        decision = await self.wait_for_decision(meeting_id, approval_id)
        expected = 'commits' if operation['kind'] == 'commit' else 'pushes'
        if decision.get('status') != 'approved' or decision.get('category') != expected:
            mapped = 'cancelled' if decision.get('status') == 'cancelled' else (
                'denied' if decision.get('status') == 'denied' else 'failed')
            self._finish_git_op(
                meeting_id, operation, status=mapped, summary='Git operation was not approved')
            return
        stored = dict(operation)
        stored['status'] = 'running'
        stored['updatedAt'] = _iso(self._now())
        self._replace_git_op(meeting_id, stored)
        record = self._record(meeting_id)
        workspace = record.session.agent_session.workspace
        key = meeting_id + ':' + operation_id
        cancel = threading.Event()
        self._git_cancels[key] = cancel
        try:
            if cancel.is_set():
                raise IsolationError('cancelled')
            if operation['kind'] == 'commit':
                request = CommitRequest.from_dict(operation['request'])
                outcome = await asyncio.to_thread(
                    self.git_broker.commit, request, workspace=workspace, cancel=cancel)
                meta = self.artifacts.put(
                    meeting_id, kind='git-commit',
                    body={
                        'commitSha': outcome['commitSha'],
                        'parentSha': outcome['parentSha'],
                        'treeSha': outcome['treeSha'],
                        'files': outcome.get('files') or [],
                    },
                    description='Recorded reviewed files')
                self._append(meeting_id, 'artifact.created', artifact={
                    'id': meta['id'], 'kind': 'git-commit', 'path': meta['path'],
                    'createdAt': _iso(self._now()), 'mediaType': meta.get('mediaType'),
                    'description': 'Recorded reviewed files',
                })
                count = len(outcome.get('files') or [])
                label = 'file' if count == 1 else 'files'
                self._finish_git_op(
                    meeting_id, stored, status='completed',
                    summary='Recorded {0} reviewed {1}'.format(count, label),
                    outcome=outcome, artifact_ids=[meta['id']])
                return
            request = PushRequest.from_dict(operation['request'])
            outcome = await asyncio.to_thread(
                self.git_broker.push, request, workspace=workspace, cancel=cancel)
            meta = self.artifacts.put(
                meeting_id, kind='git-push',
                body={
                    'commitSha': outcome['commitSha'],
                    'remote': outcome['remote'],
                    'branch': outcome['branch'],
                },
                description='Sent the approved snapshot to the configured remote')
            self._append(meeting_id, 'artifact.created', artifact={
                'id': meta['id'], 'kind': 'git-push', 'path': meta['path'],
                'createdAt': _iso(self._now()), 'mediaType': meta.get('mediaType'),
                'description': 'Sent the approved snapshot to the configured remote',
            })
            self._finish_git_op(
                meeting_id, stored, status='completed',
                summary='Sent the approved snapshot to the configured remote',
                outcome=outcome, artifact_ids=[meta['id']])
        except IsolationError as error:
            text = str(error)
            if 'cancelled' in text:
                status = 'cancelled'
                summary = 'Git operation was cancelled'
                conflict = False
            elif any(token in text for token in (
                    'expectedHead', 'hash', 'staged', 'HEAD', 'tip', 'ancestor')):
                status = 'conflict'
                summary = 'The workspace changed before the git operation'
                conflict = True
            else:
                status = 'failed'
                summary = 'Git operation failed'
                conflict = False
            self._finish_git_op(
                meeting_id, stored, status=status, summary=summary, conflict=conflict)
        except (TypeError, ValueError, OSError):
            self._finish_git_op(
                meeting_id, stored, status='failed', summary='Git operation failed')
        finally:
            self._git_cancels.pop(key, None)

    async def create_commit(self, meeting_id, payload):
        return await self._create_git_op(meeting_id, 'commit', payload)

    async def create_push(self, meeting_id, payload):
        return await self._create_git_op(meeting_id, 'push', payload)

    async def _create_git_op(self, meeting_id, kind, payload):
        fields = CREATE_COMMIT_FIELDS if kind == 'commit' else CREATE_PUSH_FIELDS
        reject_unknown_fields(payload, fields, kind + ' request')
        reject_secrets(payload, kind + ' request')
        body = dict(payload)
        if body.get('meetingId') not in (None, meeting_id):
            raise DaemonError(409, 'conflict', 'meetingId does not match the request')
        body['meetingId'] = meeting_id
        if not body.get('id'):
            body['id'] = _new_id('cmt-' if kind == 'commit' else 'psh-')
        try:
            request = (
                CommitRequest.from_dict(body) if kind == 'commit' else PushRequest.from_dict(body))
        except (TypeError, ValueError) as error:
            raise DaemonError(422, 'invalid_request', str(error)) from error
        request_payload = request.to_dict()
        operation_id = request_payload['id']
        created = False
        with self._serialize_writes(meeting_id):
            record = self._record(meeting_id)
            if record.session.state == 'ended':
                raise DaemonError(
                    409, 'conflict', 'cannot create git operations after the meeting has ended')
            existing = self._find_git_op(meeting_id, operation_id)
            if existing is not None and existing.get('status') == 'completed':
                return self._public_git_op(existing)
            if existing is not None and existing.get('status') in (
                    'requested', 'approved', 'running'):
                self._ensure_git_runner(meeting_id, operation_id)
                return self._public_git_op(existing)
            category = 'commits' if kind == 'commit' else 'pushes'
            mode = permission_mode(record.session.permissions, category)
            if mode != 'approval-required':
                raise DaemonError(
                    422, 'invalid_request', 'this action is not configured for approval')
            approval = self.create_approval(meeting_id, {
                'category': category,
                'summary': self._git_approval_summary(kind, request_payload),
                'scope': self._git_approval_scope(kind, request_payload),
                'delegationId': request_payload.get('delegationId'),
            })
            now = _iso(self._now())
            stored = {
                'id': operation_id,
                'kind': kind,
                'meetingId': meeting_id,
                'status': 'requested',
                'request': request_payload,
                'approvalId': approval['id'],
                'createdAt': now,
                'updatedAt': now,
            }
            stored = GitOperation.from_dict(stored).to_dict()
            self._replace_git_op(meeting_id, stored)
            self._append(
                meeting_id, 'git.action.requested',
                operation=GitOperation.from_dict(stored).public_dict())
            created = True
        self._ensure_git_runner(meeting_id, operation_id)
        return self._public_git_op(stored)

    def list_commits(self, meeting_id):
        self._record(meeting_id)
        ops = [item for item in (self.meetings.list_git_ops(meeting_id) or [])
               if item.get('kind') == 'commit']
        return {'commits': [self._public_git_op(item) for item in ops]}

    def list_pushes(self, meeting_id):
        self._record(meeting_id)
        ops = [item for item in (self.meetings.list_git_ops(meeting_id) or [])
               if item.get('kind') == 'push']
        return {'pushes': [self._public_git_op(item) for item in ops]}

    async def get_commit(self, meeting_id, operation_id):
        return await self._get_git_op(meeting_id, operation_id, 'commit')

    async def get_push(self, meeting_id, operation_id):
        return await self._get_git_op(meeting_id, operation_id, 'push')

    async def _get_git_op(self, meeting_id, operation_id, kind):
        require_id(operation_id, 'operationId')
        self._record(meeting_id)
        found = self._find_git_op(meeting_id, operation_id)
        if found is None or found.get('kind') != kind:
            raise DaemonError(404, 'not_found', 'git operation not found')
        if found.get('status') in ('requested', 'approved', 'running'):
            self._ensure_git_runner(meeting_id, operation_id)
        return self._public_git_op(found)

    def _init_screen_share_locked(self, meeting_id, settings):
        from screen_share import public_status
        analyzer = self.screen_share_host.analyzer_available()
        status = public_status({
            'enabled': settings['enabled'],
            'available': False,
            'active': False,
            'paused': False,
            'capturing': False,
            'degradedReason': None if settings['enabled'] else 'disabled',
            'captureIntervalMs': settings['captureIntervalMs'],
            'analyzerAvailable': analyzer,
            'retention': {
                'maxFrames': settings['maxFrames'],
                'maxBytes': settings['maxBytes'],
                'retentionSeconds': settings['retentionSeconds'],
            },
        })
        self.meetings.update_screen_share(meeting_id, lambda current: {
            'settings': settings,
            'status': status,
            'observations': [],
            'paused': False,
        })
        if settings['enabled']:
            self._append(meeting_id, 'screen_share.started', status=status)

    def _screen_share_record(self, meeting_id):
        self._record(meeting_id)
        stored = self.meetings.get_screen_share(meeting_id)
        if stored is None:
            raise DaemonError(404, 'not_found', 'meeting not found')
        return stored

    def get_screen_share(self, meeting_id):
        stored = self._screen_share_record(meeting_id)
        from screen_share import public_status
        return {
            'status': public_status(dict(stored.get('status') or {}, paused=stored.get('paused'))),
            'settings': {
                'enabled': stored['settings']['enabled'],
                'captureIntervalMs': stored['settings']['captureIntervalMs'],
                'retention': {
                    'maxFrames': stored['settings']['maxFrames'],
                    'maxBytes': stored['settings']['maxBytes'],
                    'retentionSeconds': stored['settings']['retentionSeconds'],
                },
            },
            'observations': list(stored.get('observations') or []),
        }

    def list_screen_share_observations(self, meeting_id):
        stored = self._screen_share_record(meeting_id)
        return {'observations': list(stored.get('observations') or [])}

    def set_screen_share_paused(self, meeting_id, paused):
        stored = self._screen_share_record(meeting_id)
        if not stored['settings']['enabled']:
            raise DaemonError(409, 'conflict', 'screen-share understanding is disabled')
        record = self._record(meeting_id)
        if record.session.state == 'ended' and not paused:
            raise DaemonError(409, 'conflict', 'cannot resume screen share after the meeting has ended')
        from screen_share_pipeline import ScreenShareBus
        from screen_share import public_status
        bus = ScreenShareBus(self.jobs_dir, meeting_id)
        bus.write_control(paused=bool(paused))
        reason = 'paused' if paused else None

        def mutate(current):
            status = dict(current.get('status') or {})
            status['paused'] = bool(paused)
            status['active'] = False if paused else status.get('available')
            status['capturing'] = False if paused else status.get('capturing')
            status['degradedReason'] = reason
            current['paused'] = bool(paused)
            current['status'] = public_status(status)
            return current

        updated = self.meetings.update_screen_share(meeting_id, mutate)
        if paused:
            self._append(meeting_id, 'screen_share.stopped', reason='paused')
        elif stored['settings']['enabled']:
            self._append(meeting_id, 'screen_share.started', status=updated['status'])
        return self.get_screen_share(meeting_id)

    def _pause_screen_share_locked(self, meeting_id, reason='ended'):
        stored = self.meetings.get_screen_share(meeting_id)
        if stored is None or not stored.get('settings', {}).get('enabled'):
            return
        if stored.get('paused') and (stored.get('status') or {}).get('degradedReason') in (
                'ended', 'cancelled', 'share_stopped'):
            return
        from screen_share import public_status
        mapped = reason if reason in ('ended', 'cancelled', 'share_stopped', 'paused') else 'ended'

        def mutate(current):
            status = dict(current.get('status') or {})
            status.update({'paused': True, 'active': False, 'capturing': False,
                           'degradedReason': mapped})
            current['paused'] = True
            current['status'] = public_status(status)
            return current

        try:
            self.meetings.update_screen_share(meeting_id, mutate)
        except FileNotFoundError:
            return
        self._append(meeting_id, 'screen_share.stopped', reason=mapped)

    def apply_screen_share_health(self, meeting_id, health=None):
        payload = health or {}
        stored = self.meetings.get_screen_share(meeting_id)
        if stored is None or not stored.get('settings', {}).get('enabled'):
            return None
        from screen_share import public_status
        merged = dict(stored.get('status') or {})
        for key in ('available', 'active', 'capturing', 'paused', 'lastObservationAt',
                    'degradedReason', 'analyzerAvailable'):
            if key in payload:
                merged[key] = payload[key]
        if stored.get('paused'):
            merged['paused'] = True
            merged['active'] = False
            merged['capturing'] = False
            merged['degradedReason'] = merged.get('degradedReason') or 'paused'

        def mutate(current):
            current['status'] = public_status(merged)
            return current

        try:
            self.meetings.update_screen_share(meeting_id, mutate)
        except FileNotFoundError:
            return None
        return merged

    async def ingest_screen_share_inbox(self, meeting_id):
        stored = self.meetings.get_screen_share(meeting_id)
        if stored is None or not stored['settings']['enabled'] or stored.get('paused'):
            return []
        from screen_share_pipeline import ScreenShareBus
        bus = ScreenShareBus(self.jobs_dir, meeting_id)
        results = []
        for path, meta in bus.list_inbox():
            png = path.read_bytes()
            result = await self.screen_share_host.ingest_png(
                meeting_id, png, settings=stored['settings'],
                emit=lambda event_type, **payload: self._append(meeting_id, event_type, **payload),
                store_observation=lambda observation: self._store_observation(meeting_id, observation, bus),
                captured_at=meta.get('capturedAt'),
            )
            bus.drop_inbox(path)
            results.append(result)
        return results

    def _store_observation(self, meeting_id, observation, bus=None):
        from screen_share import VisualObservation, public_status

        def mutate(current):
            items = list(current.get('observations') or [])
            items.append(observation)
            settings = current['settings']
            max_frames = settings['maxFrames']
            current['observations'] = items[-max_frames:]
            status = dict(current.get('status') or {})
            status['lastObservationAt'] = observation.get('timestamp')
            current['status'] = public_status(status)
            return current

        self.meetings.update_screen_share(meeting_id, mutate)
        if bus is not None:
            bus.write_outbox(VisualObservation.from_dict(observation))

    def list_artifacts(self, meeting_id):
        self._record(meeting_id)
        return {'artifacts': self.artifacts.list(meeting_id)}

    def get_artifact(self, meeting_id, artifact_id):
        self._record(meeting_id)
        try:
            return self.artifacts.get(meeting_id, artifact_id)
        except FileNotFoundError as error:
            raise DaemonError(404, 'not_found', 'artifact not found') from error
        except ValueError as error:
            raise DaemonError(422, 'invalid_request', str(error)) from error

    def read_artifact_body(self, meeting_id, artifact_id):
        self._record(meeting_id)
        try:
            return self.artifacts.read_body(meeting_id, artifact_id)
        except FileNotFoundError as error:
            raise DaemonError(404, 'not_found', 'artifact not found') from error
        except ValueError as error:
            raise DaemonError(422, 'invalid_request', str(error)) from error

    def heartbeat_lease(self, provider, session_id):
        lease, record = self._lease_pair(provider, session_id)
        if lease is None:
            raise DaemonError(404, 'not_found', 'lease not found')
        token = lease.token
        if record is not None and record.lease_token and record.lease_token != token:
            raise DaemonError(409, 'conflict', 'meeting lease token does not match')
        return self.leases.heartbeat(provider, session_id, token)

    def store_handoff(self, handoff):
        if isinstance(handoff, dict):
            try:
                handoff = MeetingHandoff.from_dict(handoff)
            except (TypeError, ValueError) as error:
                raise DaemonError(422, 'invalid_request', str(error)) from error
        if not isinstance(handoff, MeetingHandoff):
            raise DaemonError(422, 'invalid_request', 'handoff is invalid')
        with self._serialize_writes(handoff.meeting_id):
            return self._store_handoff_locked(handoff)

    def _store_handoff_locked(self, handoff):
        record = self._record(handoff.meeting_id)
        session = record.session
        if handoff.started_at != session.started_at:
            raise DaemonError(422, 'invalid_request', 'handoff does not match the meeting')
        agent = session.agent_session
        token = record.lease_token
        lease = None
        if token:
            lease = self.leases.get(agent.provider, agent.session_id)
        elif record.handoff is None:
            lease = self.leases.get(agent.provider, agent.session_id)
        if record.handoff is None:
            if lease is not None:
                if token and lease.token != token:
                    raise DaemonError(409, 'conflict', 'meeting lease token does not match')
                if lease.active_delegated_turn:
                    raise DaemonError(
                        409, 'conflict', 'cannot finalize while a delegated turn is active')
                if lease.state == 'in_meeting':
                    try:
                        self.leases.begin_finalization(
                            agent.provider, agent.session_id, token or lease.token)
                    except (LeaseConflictError, LeaseStateError) as error:
                        raise DaemonError(409, 'conflict', str(error)) from error
        self._cancel_pending_locked(session.id, reason='handoff_ready')
        handoff = self._enrich_handoff(handoff, session)
        if record.handoff is not None and record.handoff.to_dict() != handoff.to_dict():
            raise DaemonError(409, 'conflict', 'a different handoff is already stored')
        if session.state != 'ended':
            session = self._replace_session(session, state='ended')
            self._persist(session, token)
        self._append_once(session.id, 'meeting.ended', reason='handoff_ready')
        if record.handoff is None:
            self.meetings.store_handoff(handoff)
        self._append_once(session.id, 'handoff.ready', handoff=handoff.to_dict())
        self._require_handoff_event_order(session.id)
        self._complete_release_after_handoff(session, token)
        return handoff

    def prepare_finalization(self, meeting_id):
        with self._serialize_writes(meeting_id):
            record = self._record(meeting_id)
            agent = record.session.agent_session
            token = record.lease_token
            if not token:
                return record.session
            lease = self.leases.get(agent.provider, agent.session_id)
            if lease is None:
                return record.session
            if lease.token != token:
                raise DaemonError(409, 'conflict', 'meeting lease token does not match')
            if lease.active_delegated_turn:
                try:
                    self.leases.finish_turn(
                        agent.provider, agent.session_id, token, lease.active_delegated_turn)
                except (LeaseStateError, LeaseConflictError) as error:
                    raise DaemonError(409, 'conflict', str(error)) from error
                lease = self.leases.get(agent.provider, agent.session_id)
                if lease is None:
                    return record.session
            if lease.state == 'in_meeting':
                try:
                    self.leases.begin_finalization(agent.provider, agent.session_id, token)
                except (LeaseConflictError, LeaseStateError) as error:
                    raise DaemonError(409, 'conflict', str(error)) from error
            elif lease.state == 'finalizing':
                try:
                    self.leases.heartbeat(agent.provider, agent.session_id, token)
                except (LeaseConflictError, LeaseStateError):
                    pass
            return record.session

    def note_append_failure(self, meeting_id, handoff_id, reason):
        with self._serialize_writes(meeting_id):
            record = self._record(meeting_id)
            session = record.session
            if session.state != 'ended':
                session = self._replace_session(session, state='ended')
                self._persist(session, record.lease_token)
                self._append_once(meeting_id, 'meeting.ended', reason='handoff_append_failed')
            self._append_once(
                meeting_id,
                'handoff.append_failed',
                reason=str(reason or 'exact append failed')[:240],
                handoffId=handoff_id,
                retryable=True,
            )
            if record.lease_token:
                agent = session.agent_session
                try:
                    self.leases.heartbeat(agent.provider, agent.session_id, record.lease_token)
                except (LeaseConflictError, LeaseStateError, DaemonError):
                    pass
            return session

    async def retry_handoff_append(self, meeting_id):
        async with self._async_lock(meeting_id):
            record = self._record(meeting_id)
            if record.handoff is not None:
                return record.handoff
            try:
                await self.supervisor.retry_handoff(meeting_id)
            except DaemonError:
                raise
            except Exception as error:
                raise DaemonError(
                    503, 'supervisor_retry_failed',
                    'meeting supervisor failed to retry handoff') from error
            record = self._record(meeting_id)
            if record.handoff is not None:
                return record.handoff
            raise DaemonError(409, 'handoff_append_failed', 'exact append still failed')

    def _require_handoff_event_order(self, meeting_id):
        types = [event.type for event in self.events.replay(meeting_id)]
        if 'meeting.ended' not in types or 'handoff.ready' not in types:
            raise DaemonError(503, 'handoff_incomplete', 'handoff events are not durable')
        if types.index('meeting.ended') > types.index('handoff.ready'):
            raise DaemonError(503, 'handoff_incomplete', 'handoff events are not durable')

    def _complete_release_after_handoff(self, session, token):
        self._require_handoff_event_order(session.id)
        agent = session.agent_session
        lease = self.leases.get(agent.provider, agent.session_id)
        if lease is None:
            self.meetings.clear_lease_token(session.id)
            self._append_once(session.id, 'agent_session.released', sessionId=agent.session_id)
            return
        if not token:
            token = lease.token
        try:
            if lease.state == 'in_meeting':
                if lease.active_delegated_turn:
                    raise DaemonError(
                        409, 'conflict', 'cannot finalize while a delegated turn is active')
                self.leases.begin_finalization(agent.provider, agent.session_id, token)
                lease = self.leases.get(agent.provider, agent.session_id)
            if lease is not None and lease.state == 'finalizing':
                if lease.finalization.status == 'in_progress':
                    self.leases.complete_finalization(agent.provider, agent.session_id, token)
                self.leases.release(agent.provider, agent.session_id, token)
            elif lease is not None and lease.state == 'failed':
                self.leases.release(agent.provider, agent.session_id, token)
        except DaemonError:
            raise
        except LeaseStateError as error:
            raise DaemonError(409, 'conflict', str(error)) from error
        except LeaseConflictError as error:
            raise DaemonError(409, 'conflict', str(error)) from error
        if self.leases.get(agent.provider, agent.session_id) is not None:
            raise DaemonError(503, 'lease_release_failed', 'lease release did not succeed')
        self.meetings.clear_lease_token(session.id)
        self._append_once(session.id, 'agent_session.released', sessionId=agent.session_id)

    def record_finalization_failure(self, meeting_id, reason):
        with self._serialize_writes(meeting_id):
            record = self._record(meeting_id)
            session = record.session
            if session.state != 'ended':
                session = self._replace_session(session, state='ended')
                self._persist(session, record.lease_token)
                self._append_once(meeting_id, 'meeting.ended', reason=reason)
                record = self.meetings.get(meeting_id) or MeetingRecord(
                    session=session, lease_token=record.lease_token, handoff=record.handoff)
            self._fail_lease(record, reason)
            return session

    def _lease_pair(self, provider, session_id):
        provider = require_enum(provider, 'provider', AGENT_PROVIDERS)
        session_id = require_id(session_id, 'sessionId', max_length=256)
        try:
            lease = self.leases.get(provider, session_id)
        except LeaseCorruptionError as error:
            raise DaemonError(409, 'corrupt_snapshot', str(error)) from error
        if lease is None:
            return None, None
        record = self._active_record(provider, session_id, lease)
        return lease, record

    def lease_status(self, provider, session_id):
        lease, _record = self._lease_pair(provider, session_id)
        if lease is None:
            raise DaemonError(404, 'not_found', 'lease not found')
        return public_lease_status(lease)

    def heartbeat_http_lease(self, provider, session_id):
        lease = self.heartbeat_lease(provider, session_id)
        return public_lease_status(lease)

    def release_http_lease(self, provider, session_id):
        lease, record = self._lease_pair(provider, session_id)
        if lease is None:
            raise DaemonError(404, 'not_found', 'lease not found')
        token = lease.token
        if record is not None and record.lease_token and record.lease_token != token:
            raise DaemonError(409, 'conflict', 'meeting lease token does not match')
        try:
            self.leases.release(provider, session_id, token)
        except LeaseStateError as error:
            raise DaemonError(409, 'conflict', str(error)) from error
        if self.leases.get(provider, session_id) is not None:
            raise DaemonError(503, 'lease_release_failed', 'lease release did not succeed')
        if record is not None:
            self.meetings.clear_lease_token(record.session.id)
            self._append_once(record.session.id, 'agent_session.released', sessionId=session_id)
        return None


def _bearer_matches(provided, expected):
    left = hashlib.sha256(provided.encode('utf-8')).digest()
    right = hashlib.sha256(expected.encode('utf-8')).digest()
    return hmac.compare_digest(left, right)


def create_app(
    *,
    root,
    bind_host='127.0.0.1',
    auth_token=None,
    supervisor=None,
    clock=None,
    on_auth_token=None,
    max_body_bytes=DEFAULT_MAX_BODY,
    sse_poll_interval=DEFAULT_SSE_POLL,
    sse_heartbeat_interval=DEFAULT_SSE_HEARTBEAT,
    event_store=None,
    lease_store=None,
    meeting_store=None,
    jobs_dir=None,
    visual_analyzer=None,
    provider_registry=None,
):
    require_loopback_bind(bind_host)
    token = secrets.token_urlsafe(32) if auth_token is None else auth_token
    if not isinstance(token, str) or not token:
        raise ValueError('auth_token must be a non-empty string')
    if on_auth_token is not None:
        on_auth_token(token)
    daemon = RuntimeDaemon(
        root,
        supervisor=supervisor,
        clock=clock,
        event_store=event_store,
        lease_store=lease_store,
        meeting_store=meeting_store,
        jobs_dir=jobs_dir,
        visual_analyzer=visual_analyzer,
        provider_registry=provider_registry,
        sse_poll_interval=sse_poll_interval,
        sse_heartbeat_interval=sse_heartbeat_interval,
    )

    @web.middleware
    async def auth_middleware(request, handler):
        if request.path.startswith('/v1/'):
            header = request.headers.get('Authorization', '')
            scheme, _, credential = header.partition(' ')
            if scheme != 'Bearer' or not credential or not _bearer_matches(credential, token):
                return _json_error(401, 'unauthorized', 'authorization required')
        return await handler(request)

    @web.middleware
    async def error_middleware(request, handler):
        try:
            return await handler(request)
        except web.HTTPException as error:
            if error.status_code == 413:
                return _json_error(413, 'payload_too_large', 'request body is too large')
            if error.status_code >= 400:
                return _json_error(error.status_code, 'http_error', error.reason or str(error))
            raise
        except DaemonError as error:
            return _json_error(error.status, error.code, error.message)
        except MeetingCorruptionError as error:
            return _json_error(409, 'corrupt_snapshot', str(error))
        except LeaseCorruptionError as error:
            return _json_error(409, 'corrupt_snapshot', str(error))
        except ValueError as error:
            return _json_error(422, 'invalid_request', str(error))
        except Exception:
            return _json_error(500, 'internal_error', 'internal error')

    async def read_json(request, *, allow_empty=False):
        length = request.content_length
        if length is not None and length > max_body_bytes:
            raise DaemonError(413, 'payload_too_large', 'request body is too large')
        raw = await request.read()
        if len(raw) > max_body_bytes:
            raise DaemonError(413, 'payload_too_large', 'request body is too large')
        if not raw:
            if allow_empty:
                return {}
            raise DaemonError(422, 'invalid_request', 'JSON body is required')
        if request.content_type != 'application/json':
            raise DaemonError(415, 'unsupported_media_type', 'Content-Type must be application/json')
        try:
            payload = json.loads(raw.decode('utf-8'))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise DaemonError(422, 'invalid_request', 'JSON body is invalid') from error
        if not isinstance(payload, dict):
            raise DaemonError(422, 'invalid_request', 'JSON body must be an object')
        reject_secrets(payload, 'request')
        return payload

    async def create_meeting(request):
        payload = await read_json(request)
        session = await daemon.create_meeting(payload)
        return _public_json(daemon.public_meeting(session), status=201)

    async def get_meeting(request):
        session = await daemon.get_meeting(request.match_info['meetingId'])
        return _public_json(daemon.public_meeting(session))

    async def update_context(request):
        payload = await read_json(request)
        session = await daemon.update_context(request.match_info['meetingId'], payload)
        return _public_json(daemon.public_meeting(session))

    async def cancel_meeting(request):
        await read_json(request, allow_empty=True)
        session = await daemon.cancel_meeting(request.match_info['meetingId'])
        return _public_json(daemon.public_meeting(session))

    async def get_handoff(request):
        handoff = await daemon.get_handoff(request.match_info['meetingId'])
        return _public_json(handoff.to_dict())

    async def retry_handoff(request):
        await read_json(request, allow_empty=True)
        handoff = await daemon.retry_handoff_append(request.match_info['meetingId'])
        return _public_json(handoff.to_dict())

    async def list_approvals(request):
        payload = daemon.list_approvals(request.match_info['meetingId'])
        return _public_json(payload)

    async def list_artifacts(request):
        payload = daemon.list_artifacts(request.match_info['meetingId'])
        return _public_json(payload)

    async def get_artifact(request):
        payload = daemon.get_artifact(
            request.match_info['meetingId'], request.match_info['artifactId'])
        return _public_json(payload)

    async def get_artifact_content(request):
        meta, data = daemon.read_artifact_body(
            request.match_info['meetingId'], request.match_info['artifactId'])
        media = meta.get('mediaType')
        kind = meta.get('kind')
        allowed = ('application/json', 'text/plain', 'application/octet-stream',
                   'image/png', 'image/jpeg')
        if kind == 'screenshot' and media in ('image/png', 'image/jpeg'):
            pass
        elif media not in allowed:
            media = 'application/octet-stream'
        if kind == 'screenshot' and media not in ('image/png', 'image/jpeg'):
            raise DaemonError(415, 'unsupported_media_type', 'image artifact type is invalid')
        filename = meta.get('id') or 'artifact'
        return web.Response(
            body=data,
            content_type=media,
            headers={
                'Content-Disposition': 'attachment; filename="' + filename + '"',
                'X-Content-Type-Options': 'nosniff',
                'Cache-Control': 'no-store',
            },
        )

    async def create_approval(request):
        payload = await read_json(request)
        approval = daemon.create_approval(request.match_info['meetingId'], payload)
        return _public_json(approval, status=201)

    async def get_approval(request):
        approval = daemon.get_approval(
            request.match_info['meetingId'], request.match_info['approvalId'])
        return _public_json(approval)

    async def decide_approval(request):
        payload = await read_json(request)
        approval = daemon.decide_approval(
            request.match_info['meetingId'], request.match_info['approvalId'], payload)
        return _public_json(approval)

    async def create_commit(request):
        payload = await read_json(request)
        result = await daemon.create_commit(request.match_info['meetingId'], payload)
        return _public_json(result, status=201)

    async def list_commits(request):
        return _public_json(daemon.list_commits(request.match_info['meetingId']))

    async def get_commit(request):
        result = await daemon.get_commit(
            request.match_info['meetingId'], request.match_info['operationId'])
        return _public_json(result)

    async def create_push(request):
        payload = await read_json(request)
        result = await daemon.create_push(request.match_info['meetingId'], payload)
        return _public_json(result, status=201)

    async def list_pushes(request):
        return _public_json(daemon.list_pushes(request.match_info['meetingId']))

    async def get_push(request):
        result = await daemon.get_push(
            request.match_info['meetingId'], request.match_info['operationId'])
        return _public_json(result)

    async def get_screen_share(request):
        return _public_json(daemon.get_screen_share(request.match_info['meetingId']))

    async def list_screen_share_observations(request):
        return _public_json(daemon.list_screen_share_observations(request.match_info['meetingId']))

    async def pause_screen_share(request):
        await read_json(request, allow_empty=True)
        return _public_json(daemon.set_screen_share_paused(request.match_info['meetingId'], True))

    async def resume_screen_share(request):
        await read_json(request, allow_empty=True)
        return _public_json(daemon.set_screen_share_paused(request.match_info['meetingId'], False))

    async def get_events(request):
        meeting_id = request.match_info['meetingId']
        daemon._record(meeting_id)
        headers = {
            'Content-Type': 'text/event-stream',
            'Cache-Control': 'no-cache',
            'Connection': 'keep-alive',
        }
        response = web.StreamResponse(status=200, headers=headers)
        await response.prepare(request)
        after_id = request.headers.get('Last-Event-ID')
        unknown_resume = after_id is not None
        last_heartbeat = asyncio.get_event_loop().time()
        try:
            while True:
                events = daemon.events.replay(meeting_id)
                emit = []
                if after_id is None:
                    emit = list(events)
                else:
                    found = False
                    tail = []
                    for event in events:
                        if found:
                            tail.append(event)
                        elif event.id == after_id:
                            found = True
                    if found:
                        emit = tail
                        unknown_resume = False
                    elif unknown_resume and events:
                        emit = list(events)
                        unknown_resume = False
                for event in emit:
                    payload = event.to_dict()
                    ensure_public(payload)
                    chunk = (
                        f'id: {event.id}\n'
                        f'event: {event.type}\n'
                        f'data: {json.dumps(payload, ensure_ascii=False)}\n\n'
                    )
                    await response.write(chunk.encode('utf-8'))
                    after_id = event.id
                now = asyncio.get_event_loop().time()
                if now - last_heartbeat >= daemon.sse_heartbeat_interval:
                    await response.write(b': heartbeat\n\n')
                    last_heartbeat = now
                await asyncio.sleep(daemon.sse_poll_interval)
        except (asyncio.CancelledError, ConnectionResetError, BrokenPipeError):
            return response
        finally:
            try:
                await response.write_eof()
            except Exception:
                pass
        return response

    async def lease_status(request):
        payload = daemon.lease_status(request.match_info['provider'], request.match_info['sessionId'])
        return _public_json(payload)

    async def lease_heartbeat(request):
        await read_json(request, allow_empty=True)
        payload = daemon.heartbeat_http_lease(
            request.match_info['provider'], request.match_info['sessionId'])
        return _public_json(payload)

    async def lease_release(request):
        daemon.release_http_lease(request.match_info['provider'], request.match_info['sessionId'])
        return web.Response(status=204)

    async def list_providers(request):
        return _public_json(daemon.list_providers())

    app = web.Application(
        middlewares=(auth_middleware, error_middleware),
        client_max_size=max_body_bytes,
    )
    app.runtime_daemon = daemon
    app.bind_host = bind_host
    app.router.add_post('/v1/meetings', create_meeting)
    app.router.add_get('/v1/providers', list_providers)
    app.router.add_get('/v1/meetings/{meetingId}', get_meeting)
    app.router.add_post('/v1/meetings/{meetingId}/context', update_context)
    app.router.add_post('/v1/meetings/{meetingId}/cancel', cancel_meeting)
    app.router.add_get('/v1/meetings/{meetingId}/events', get_events)
    app.router.add_get('/v1/meetings/{meetingId}/handoff', get_handoff)
    app.router.add_post('/v1/meetings/{meetingId}/handoff/retry', retry_handoff)
    app.router.add_post('/v1/meetings/{meetingId}/approvals/{approvalId}/decision', decide_approval)
    app.router.add_get('/v1/meetings/{meetingId}/approvals/{approvalId}', get_approval)
    app.router.add_post('/v1/meetings/{meetingId}/approvals', create_approval)
    app.router.add_get('/v1/meetings/{meetingId}/approvals', list_approvals)
    app.router.add_get('/v1/meetings/{meetingId}/commits/{operationId}', get_commit)
    app.router.add_post('/v1/meetings/{meetingId}/commits', create_commit)
    app.router.add_get('/v1/meetings/{meetingId}/commits', list_commits)
    app.router.add_get('/v1/meetings/{meetingId}/pushes/{operationId}', get_push)
    app.router.add_post('/v1/meetings/{meetingId}/pushes', create_push)
    app.router.add_get('/v1/meetings/{meetingId}/pushes', list_pushes)
    app.router.add_get('/v1/meetings/{meetingId}/screen-share/observations', list_screen_share_observations)
    app.router.add_post('/v1/meetings/{meetingId}/screen-share/pause', pause_screen_share)
    app.router.add_post('/v1/meetings/{meetingId}/screen-share/resume', resume_screen_share)
    app.router.add_get('/v1/meetings/{meetingId}/screen-share', get_screen_share)
    app.router.add_get('/v1/meetings/{meetingId}/artifacts/{artifactId}/content', get_artifact_content)
    app.router.add_get('/v1/meetings/{meetingId}/artifacts/{artifactId}', get_artifact)
    app.router.add_get('/v1/meetings/{meetingId}/artifacts', list_artifacts)
    app.router.add_post('/v1/agent-sessions/{provider}/{sessionId}/lease', lease_heartbeat)
    app.router.add_delete('/v1/agent-sessions/{provider}/{sessionId}/lease', lease_release)
    app.router.add_get('/v1/agent-sessions/{provider}/{sessionId}/status', lease_status)

    async def on_cleanup(_app):
        daemon.close()

    app.on_cleanup.append(on_cleanup)
    return app


def serve(root, *, host='127.0.0.1', port=8765, **kwargs):
    require_loopback_bind(host)
    app = create_app(root=root, bind_host=host, **kwargs)
    web.run_app(app, host=host, port=port, print=None)
    return app
