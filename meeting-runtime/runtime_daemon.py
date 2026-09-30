"""Authenticated daemon for meetings over schemas, EventStore, and MeetingRepository."""
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

from agent_sessions import MeetingSession, optional_on_behalf_of, optional_voice
from context_handoff import ContextHandoff
from event_store import JsonlEventStore
from meeting_handoff import MeetingHandoff
from meeting_repository import MeetingCorruptionError, MeetingRepository
from meeting_urls import platform_for_url
from schema_validation import reject_secrets, reject_unknown_fields, require_enum


# onBehalfOf names the owner in the spoken AI disclosure; voice picks a GPT-Live voice.
CREATE_FIELDS = ('meetingUrl', 'context', 'camera', 'onBehalfOf', 'voice')
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
    async def start(self, meeting, camera_settings=None):
        raise RuntimeError('meeting supervisor is not configured')

    async def add_context(self, meeting_id, context):
        raise RuntimeError('meeting supervisor is not configured')

    async def cancel(self, meeting_id):
        raise RuntimeError('meeting supervisor is not configured')


def require_loopback_bind(host):
    try:
        address = ipaddress.ip_address(host)
    except ValueError as error:
        raise ValueError('daemon bind address must be a loopback IP') from error
    if not address.is_loopback:
        raise ValueError('daemon bind address must be a loopback IP')
    return host


def require_server_bind(host, api_tokens):
    """Server mode may bind any IP, but only with at least one long-lived API token."""
    try:
        ipaddress.ip_address(host)
    except ValueError as error:
        raise ValueError('daemon bind address must be an IP address') from error
    if api_tokens is None or len(api_tokens) == 0:
        raise ValueError('server mode requires an API token: run api_tokens.py create --name NAME')
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


def _new_id(prefix):
    return prefix + secrets.token_hex(8)


class RuntimeDaemon:
    """Meeting daemon.

    Threading contract: the aiohttp event loop owns async entry points
    (create, context, cancel). Synchronous supervisor callbacks (transition,
    apply_presence, store_handoff, record_finalization_failure) may run on
    that loop or on a worker thread. Each meeting_id has an asyncio.Lock
    that serializes async entry points, including supervisor I/O, and a
    threading.RLock used only around durable local writes — never across
    await. Sync callbacks take the RLock for their full body so identical
    concurrent handoffs stay exactly-once without hopping threads.
    Different meetings use different locks and do not block each other.
    Callers must not wait on the event loop while holding the RLock.
    """

    def __init__(
        self,
        root,
        *,
        supervisor=None,
        clock=None,
        event_store=None,
        meeting_store=None,
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
        self.sse_poll_interval = sse_poll_interval
        self.sse_heartbeat_interval = sse_heartbeat_interval
        self._lock_guard = threading.Lock()
        self._async_locks = {}
        self._write_locks = {}

    def close(self):
        for store in (self.meetings, self.events):
            closer = getattr(store, 'close', None)
            if closer is not None:
                closer()

    def public_meeting(self, session):
        payload = session.to_dict()
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

    def _persist(self, session):
        return self.meetings.put(session)

    def _replace_session(self, session, **changes):
        payload = session.to_dict()
        payload.update(changes)
        return MeetingSession.from_dict(payload)

    def _create_failure(self, error, phase):
        if isinstance(error, DaemonError):
            return error
        if phase == 'start':
            message = 'meeting supervisor failed to start'
            if str(error) == 'meeting supervisor is not configured':
                message = str(error)
            return DaemonError(503, 'supervisor_unavailable', message)
        return DaemonError(503, 'create_failed', 'meeting create did not complete')

    def _persist_create_abort(self, session, reason):
        ended = self._replace_session(session, state='ended')
        try:
            self.meetings.put(ended)
        except Exception:
            pass
        try:
            self._append_once(session.id, 'meeting.ended', reason=reason)
        except Exception:
            pass

    async def _abort_create(self, session, started, phase, error):
        reason = 'supervisor_start_failed' if phase == 'start' else 'create_failed'
        if started:
            try:
                await self.supervisor.cancel(session.id)
            except Exception:
                pass
        with self._serialize_writes(session.id):
            self._persist_create_abort(session, reason)
        raise self._create_failure(error, phase) from error

    async def create_meeting(self, payload):
        if not isinstance(payload, dict):
            raise DaemonError(422, 'invalid_request', 'meeting must be an object')
        try:
            reject_unknown_fields(payload, CREATE_FIELDS, 'meeting')
        except ValueError as error:
            raise DaemonError(422, 'invalid_request', str(error)) from error
        try:
            meeting_url = payload['meetingUrl']
            platform = platform_for_url(meeting_url)
        except (KeyError, TypeError, ValueError) as error:
            raise DaemonError(422, 'invalid_request', 'meetingUrl is invalid') from error
        meeting_id = _new_id('mtg-')
        started_at = _iso(self._now())
        try:
            from visual_presence import parse_camera_settings
            camera = parse_camera_settings(payload.get('camera'))
            session = MeetingSession.from_dict({
                'id': meeting_id,
                'platform': platform,
                'meetingUrl': meeting_url,
                'context': payload.get('context'),
                'onBehalfOf': optional_on_behalf_of(payload.get('onBehalfOf')),
                'voice': optional_voice(payload.get('voice')),
                'state': 'joining',
                'startedAt': started_at,
                'cameraEnabled': camera['enabled'],
                'cameraState': 'off' if not camera['enabled'] else 'starting',
                'visualState': 'joining',
            })
        except (TypeError, ValueError) as error:
            raise DaemonError(422, 'invalid_request', str(error)) from error
        camera_settings = {
            'cameraEnabled': camera['enabled'],
            'cameraDefaultOn': camera['defaultOn'],
            'cameraAvatarDataUri': camera['avatarDataUri'],
        }
        started = False
        phase = 'snapshot'
        async with self._async_lock(meeting_id):
            try:
                with self._serialize_writes(meeting_id):
                    self.meetings.put(session)
                    phase = 'joining_event'
                    self._append(meeting_id, 'meeting.joining')
                    self._append(meeting_id, 'presence.updated',
                                 cameraEnabled=session.camera_enabled,
                                 cameraState=session.camera_state,
                                 visualState=session.visual_state)
                phase = 'start'
                started = True
                await self.supervisor.start(session, camera_settings=camera_settings)
                return session
            except Exception as error:
                await self._abort_create(session, started, phase, error)

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
                self._persist(updated)
            try:
                await self.supervisor.add_context(meeting_id, context)
            except DaemonError:
                raise
            except Exception as error:
                raise DaemonError(
                    503, 'supervisor_context_failed',
                    'meeting supervisor failed to update context') from error
            return updated

    async def cancel_meeting(self, meeting_id):
        async with self._async_lock(meeting_id):
            with self._serialize_writes(meeting_id):
                record = self._record(meeting_id)
                if record.session.state == 'ended':
                    return record.session
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
                self._persist(ended)
                self._append_once(meeting_id, 'meeting.ended', reason='cancelled')
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
            self._persist(updated)
            event_type = LIFECYCLE_EVENTS[state]
            extra = {}
            if event_type == 'meeting.ended':
                extra['reason'] = reason or 'ended'
            self._append_once(meeting_id, event_type, **extra)
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
            self._persist(updated)
            public = omit_none({
                'cameraEnabled': updated.camera_enabled,
                'cameraState': updated.camera_state,
                'visualState': updated.visual_state,
                'degradedReason': updated.degraded_reason,
            })
            if 'cameraEnabled' in public and 'cameraState' in public and 'visualState' in public:
                self._append(meeting_id, 'presence.updated', **public)
            return updated

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
        if record.handoff is not None and record.handoff.to_dict() != handoff.to_dict():
            raise DaemonError(409, 'conflict', 'a different handoff is already stored')
        if session.state != 'ended':
            session = self._replace_session(session, state='ended')
            self._persist(session)
        self._append_once(session.id, 'meeting.ended', reason='handoff_ready')
        if record.handoff is None:
            self.meetings.store_handoff(handoff)
        self._append_once(session.id, 'handoff.ready', handoff=handoff.to_dict())
        self._require_handoff_event_order(session.id)
        return handoff

    def _require_handoff_event_order(self, meeting_id):
        types = [event.type for event in self.events.replay(meeting_id)]
        if 'meeting.ended' not in types or 'handoff.ready' not in types:
            raise DaemonError(503, 'handoff_incomplete', 'handoff events are not durable')
        if types.index('meeting.ended') > types.index('handoff.ready'):
            raise DaemonError(503, 'handoff_incomplete', 'handoff events are not durable')

    def record_finalization_failure(self, meeting_id, reason):
        with self._serialize_writes(meeting_id):
            record = self._record(meeting_id)
            session = record.session
            if session.state != 'ended':
                session = self._replace_session(session, state='ended')
                self._persist(session)
                self._append_once(meeting_id, 'meeting.ended', reason=reason)
            return session


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
    meeting_store=None,
    call_service=None,
    call_service_factory=None,
    api_tokens=None,
    server_mode=False,
    on_stop=None,
):
    if server_mode:
        require_server_bind(bind_host, api_tokens)
    else:
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
        meeting_store=meeting_store,
        sse_poll_interval=sse_poll_interval,
        sse_heartbeat_interval=sse_heartbeat_interval,
    )

    @web.middleware
    async def auth_middleware(request, handler):
        if request.path.startswith('/v1/'):
            header = request.headers.get('Authorization', '')
            scheme, _, credential = header.partition(' ')
            accepted = scheme == 'Bearer' and bool(credential) and (
                _bearer_matches(credential, token)
                or (api_tokens is not None and api_tokens.verify(credential) is not None))
            if not accepted:
                # A competing startup can rotate the host-only file before it
                # discovers that this daemon already owns the port. Restore the
                # live daemon's credential so local clients can reread and retry.
                if on_auth_token is not None:
                    on_auth_token(token)
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

    def request_stop():
        # The same graceful exit as Ctrl-C: web.run_app then runs every cleanup, which ends
        # calls in progress with their transcript, stops meetings, and closes the tunnel.
        def graceful_exit():
            raise web.GracefulExit()
        asyncio.get_running_loop().call_later(0.2, graceful_exit)

    async def stop_daemon(request):
        # Only the per-launch token on this computer may stop the daemon, not a
        # server-mode API token held by a remote program.
        _, _, credential = request.headers.get('Authorization', '').partition(' ')
        if not _bearer_matches(credential, token):
            return _json_error(403, 'forbidden', 'only the local token on this computer can stop Smitline')
        (on_stop or request_stop)()
        return web.json_response({'stopping': True}, status=202)

    app = web.Application(
        middlewares=(auth_middleware, error_middleware),
        client_max_size=max_body_bytes,
    )
    app.runtime_daemon = daemon
    app.bind_host = bind_host
    app.router.add_post('/v1/meetings', create_meeting)
    app.router.add_get('/v1/meetings/{meetingId}', get_meeting)
    app.router.add_post('/v1/meetings/{meetingId}/context', update_context)
    app.router.add_post('/v1/meetings/{meetingId}/cancel', cancel_meeting)
    app.router.add_get('/v1/meetings/{meetingId}/events', get_events)
    app.router.add_get('/v1/meetings/{meetingId}/handoff', get_handoff)
    app.router.add_post('/v1/daemon/stop', stop_daemon)
    if call_service is None and call_service_factory is not None:
        call_service = call_service_factory(daemon)
    app.call_service = call_service
    if call_service is not None:
        from call_api import register_call_routes
        register_call_routes(app, call_service, read_json=read_json, public_json=_public_json,
                             sse_heartbeat_interval=sse_heartbeat_interval)

    async def on_cleanup(_app):
        if call_service is not None:
            await call_service.shutdown()
        daemon.close()

    app.on_cleanup.append(on_cleanup)
    return app


def serve(root, *, host='127.0.0.1', port=8765, **kwargs):
    require_loopback_bind(host)
    app = create_app(root=root, bind_host=host, **kwargs)
    web.run_app(app, host=host, port=port, print=None)
    return app
