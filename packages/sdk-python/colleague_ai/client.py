"""Local Colleague AI Python SDK — transport-independent meeting lifecycle."""

from __future__ import annotations

import json
import os
import queue
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from http.client import HTTPConnection
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Dict, Iterator, Optional
from urllib.parse import quote

SCHEMA_VERSION = 1
SDK_VERSION = '1.0.0'

PROVIDERS = frozenset({'codex', 'cursor', 'claude-code', 'generic'})
SECRET_KEYS = frozenset({
    'authorization', 'cookie', 'set-cookie', 'token', 'accessToken', 'refreshToken',
    'apiKey', 'secret', 'password', 'credential', 'privateKey',
})
PLACEHOLDER_SESSIONS = frozenset({'', '--last', 'last'})
WORKSPACE_MODES = frozenset({'none', 'read-only', 'workspace-write'})
COMMAND_MODES = frozenset({'disabled', 'approval-required', 'allowed'})
EDIT_MODES = frozenset({'disabled', 'approval-required', 'allowed'})
NETWORK_MODES = frozenset({'disabled', 'approval-required', 'allowed'})
COMMIT_MODES = frozenset({'disabled', 'approval-required'})
PUSH_MODES = frozenset({'disabled', 'approval-required'})


def redact(value: Any) -> str:
    import re
    text = '' if value is None else str(value)
    text = re.sub(r'Bearer\s+\S+', 'Bearer [redacted]', text, flags=re.I)
    text = re.sub(r'\bsk-[A-Za-z0-9_-]{8,}\b', '[redacted]', text)
    text = re.sub(r'[A-Za-z0-9+/_-]{40,}', '[redacted]', text)
    return text


class ColleagueError(Exception):
    def __init__(self, message, *, code='runtime', status=None, archive_path=None, handoff=None):
        super().__init__(redact(message))
        self.code = code
        self.status = status
        self.archive_path = archive_path
        self.handoff = handoff


class ValidationError(ColleagueError):
    def __init__(self, message, **extra):
        extra.setdefault('code', 'validation')
        super().__init__(message, **extra)


class StartupError(ColleagueError):
    def __init__(self, message, **extra):
        extra.setdefault('code', 'startup')
        super().__init__(message, **extra)


class RuntimeError(ColleagueError):
    def __init__(self, message, **extra):
        extra.setdefault('code', extra.get('code') or 'runtime')
        super().__init__(message, **extra)


class FinalizationError(ColleagueError):
    def __init__(self, message, **extra):
        extra.setdefault('code', extra.get('code') or 'finalization')
        super().__init__(message, **extra)


class InterruptError(ColleagueError):
    def __init__(self, message='interrupted', **extra):
        extra.setdefault('code', 'interrupt')
        super().__init__(message, **extra)


def _is_mapping(value):
    return isinstance(value, dict)


def _reject_secrets(payload, path=''):
    if isinstance(payload, list):
        for index, item in enumerate(payload):
            _reject_secrets(item, f'{path}[{index}]')
        return
    if not _is_mapping(payload):
        return
    for key, value in payload.items():
        next_path = f'{path}.{key}' if path else key
        if key in SECRET_KEYS or any(part in key.lower() for part in ('secret', 'token', 'password', 'credential', 'authorization')):
            raise ValidationError(f'{next_path} must not contain secrets')
        _reject_secrets(value, next_path)


def _require_string(value, name, *, allow_empty=False, max_length=8000):
    if not isinstance(value, str):
        raise ValidationError(f'{name} must be a string')
    if not allow_empty and not value.strip():
        raise ValidationError(f'{name} must not be empty')
    if len(value) > max_length:
        raise ValidationError(f'{name} exceeds {max_length} characters')
    return value


def platform_for_url(url: str) -> str:
    from urllib.parse import urlparse
    parsed = urlparse(url)
    if parsed.scheme != 'https':
        raise ValidationError('url must be an https Zoom or Teams invitation')
    host = (parsed.hostname or '').lower()
    if host == 'zoom.us' or host.endswith('.zoom.us'):
        return 'zoom'
    if host in {'teams.microsoft.com', 'teams.live.com'}:
        return 'teams'
    raise ValidationError('url must be a Zoom or Teams invitation')


def validate_agent_session(value):
    if not _is_mapping(value):
        raise ValidationError('agentSession is required')
    _reject_secrets(value, 'agentSession')
    known = {'provider', 'sessionId', 'workspace', 'model', 'metadata'}
    extra = set(value) - known
    if extra:
        raise ValidationError(f'agentSession.{next(iter(extra))} is not allowed')
    if value.get('provider') not in PROVIDERS:
        raise ValidationError('agentSession.provider must be one of: ' + ', '.join(sorted(PROVIDERS)))
    session_id = _require_string(value.get('sessionId'), 'agentSession.sessionId', max_length=256)
    if session_id in PLACEHOLDER_SESSIONS:
        raise ValidationError('agentSession.sessionId must be an originating thread id')
    workspace = _require_string(value.get('workspace'), 'agentSession.workspace', max_length=4096)
    if not Path(workspace).is_absolute():
        raise ValidationError('agentSession.workspace must be an absolute path')
    out = {
        'provider': value['provider'],
        'sessionId': session_id,
        'workspace': workspace,
    }
    if 'model' in value:
        out['model'] = _require_string(value['model'], 'agentSession.model', max_length=256)
    if 'metadata' in value:
        if not _is_mapping(value['metadata']):
            raise ValidationError('agentSession.metadata must be an object')
        out['metadata'] = {
            key: _require_string(item, f'agentSession.metadata.{key}', allow_empty=True, max_length=1024)
            for key, item in value['metadata'].items()
        }
    return out


def validate_permissions(value):
    if value is None:
        return {
            'workspace': 'read-only',
            'commands': 'approval-required',
            'edits': 'disabled',
            'network': 'approval-required',
            'commits': 'disabled',
            'pushes': 'disabled',
        }
    if not _is_mapping(value):
        raise ValidationError('permissions must be an object')
    _reject_secrets(value, 'permissions')
    known = {'workspace', 'commands', 'edits', 'network', 'commits', 'pushes'}
    extra = set(value) - known
    if extra:
        raise ValidationError(f'permissions.{next(iter(extra))} is not allowed')
    checks = {
        'workspace': WORKSPACE_MODES,
        'commands': COMMAND_MODES,
        'edits': EDIT_MODES,
        'network': NETWORK_MODES,
        'commits': COMMIT_MODES,
        'pushes': PUSH_MODES,
    }
    out = {}
    for field, allowed in checks.items():
        if value.get(field) not in allowed:
            raise ValidationError(f'permissions.{field} is invalid')
        out[field] = value[field]
    return out


def _string_list(value, name):
    if not isinstance(value, list):
        raise ValidationError(f'{name} must be an array')
    if len(value) > 200:
        raise ValidationError(f'{name} exceeds 200 items')
    return [_require_string(item, f'{name}[{index}]', allow_empty=True) for index, item in enumerate(value)]


def validate_context(value, *, required=True):
    if value is None:
        if required:
            raise ValidationError('context is required')
        return {
            'version': 1,
            'objective': 'Support this live meeting.',
            'currentTask': 'Join the meeting and help when asked.',
            'summary': '',
            'decisions': [],
            'constraints': [],
            'openQuestions': [],
            'importantFiles': [],
            'recentConversation': [],
        }
    if not _is_mapping(value):
        raise ValidationError('context must be an object')
    _reject_secrets(value, 'context')
    known = {
        'version', 'objective', 'currentTask', 'summary', 'decisions', 'constraints',
        'openQuestions', 'importantFiles', 'recentConversation', 'git',
    }
    extra = set(value) - known
    if extra:
        raise ValidationError(f'context.{next(iter(extra))} is not allowed')
    if value.get('version') != 1:
        raise ValidationError('context.version must be 1')
    conversation = value.get('recentConversation')
    if not isinstance(conversation, list):
        raise ValidationError('context.recentConversation must be an array')
    turns = []
    for index, turn in enumerate(conversation):
        if not _is_mapping(turn):
            raise ValidationError(f'context.recentConversation[{index}] must be an object')
        if turn.get('role') not in {'user', 'assistant'}:
            raise ValidationError(f'context.recentConversation[{index}].role is invalid')
        turns.append({
            'role': turn['role'],
            'text': _require_string(turn.get('text'), f'context.recentConversation[{index}].text', allow_empty=True),
        })
    out = {
        'version': 1,
        'objective': _require_string(value.get('objective'), 'context.objective', allow_empty=True),
        'currentTask': _require_string(value.get('currentTask'), 'context.currentTask', allow_empty=True),
        'summary': _require_string(value.get('summary'), 'context.summary', allow_empty=True),
        'decisions': _string_list(value.get('decisions'), 'context.decisions'),
        'constraints': _string_list(value.get('constraints'), 'context.constraints'),
        'openQuestions': _string_list(value.get('openQuestions'), 'context.openQuestions'),
        'importantFiles': _string_list(value.get('importantFiles'), 'context.importantFiles'),
        'recentConversation': turns,
    }
    if 'git' in value:
        if not _is_mapping(value['git']):
            raise ValidationError('context.git must be an object')
        out['git'] = dict(value['git'])
    return out


def validate_join_request(request):
    if not _is_mapping(request):
        raise ValidationError('join request must be an object')
    _reject_secrets(request)
    url = _require_string(request.get('url'), 'url')
    platform_for_url(url)
    return {
        'meetingUrl': url,
        'agentSession': validate_agent_session(request.get('agentSession')),
        'context': validate_context(request.get('context'), required=False),
        'permissions': validate_permissions(request.get('permissions')),
    }


def join_key(payload):
    session = payload['agentSession']
    return json.dumps({
        'url': payload['meetingUrl'],
        'provider': session['provider'],
        'sessionId': session['sessionId'],
        'workspace': session['workspace'],
    }, sort_keys=True)


def parse_sse_block(block: str):
    event = {'data': '', 'event': 'message', 'id': ''}
    for raw in block.split('\n'):
        line = raw.rstrip('\r')
        if not line or line.startswith(':'):
            continue
        if ':' in line:
            field, value = line.split(':', 1)
            if value.startswith(' '):
                value = value[1:]
        else:
            field, value = line, ''
        if field == 'data':
            event['data'] = f"{event['data']}\n{value}" if event['data'] else value
        elif field == 'event':
            event['event'] = value
        elif field == 'id':
            event['id'] = value
    if not event['data'] and not event['id']:
        return None
    payload = {}
    if event['data']:
        try:
            payload = json.loads(event['data'])
        except json.JSONDecodeError:
            payload = {'raw': event['data']}
    if event['id'] and 'id' not in payload:
        payload['id'] = event['id']
    if event['event'] and event['event'] != 'message' and 'type' not in payload:
        payload['type'] = event['event']
    return payload


def iterate_sse_text(text: str, seen=None):
    delivered = seen if seen is not None else set()
    last_event_id = ''
    for block in text.replace('\r\n', '\n').split('\n\n'):
        event = parse_sse_block(block)
        if not event:
            continue
        event_id = event.get('id')
        if event_id:
            if event_id in delivered:
                continue
            delivered.add(event_id)
            last_event_id = event_id
        yield event, last_event_id


def _map_http_error(status, payload, fallback):
    error = (payload or {}).get('error') or {}
    code = error.get('code') or fallback
    message = redact(error.get('message') or fallback)
    archive = error.get('archivePath') or (payload or {}).get('archivePath')
    handoff = (payload or {}).get('handoff')
    if status in {400, 422}:
        return ValidationError(message, status=status, code=code)
    if status == 503 or code in {'supervisor_unavailable', 'daemon_unavailable'}:
        return StartupError(message, status=status, code=code)
    if code in {'handoff_append_failed', 'finalization_failed'}:
        return FinalizationError(message, status=status, code=code, archive_path=archive, handoff=handoff)
    return RuntimeError(message, status=status, code=code)


class LoopbackTransport:
    def __init__(self, *, root=None, host='127.0.0.1', port=None, request=None, spawn_daemon=None,
                 is_port_open=None, read_auth=None, autostart=True, startup_timeout_s=8.0):
        self.root = Path(root or os.getcwd())
        self.host = host
        self.port = int(port or os.environ.get('COLLEAGUE_DAEMON_PORT') or 8765)
        self._request = request
        self._spawn_daemon = spawn_daemon if spawn_daemon is not None else (
            self._default_spawn if autostart else None
        )
        self._is_port_open = is_port_open or self._default_port_open
        self._read_auth = read_auth or self._default_read_auth
        self._startup_timeout_s = startup_timeout_s
        self._token = ''
        self._start_lock = threading.Lock()

    def _default_port_open(self):
        sock = socket.socket()
        sock.settimeout(0.3)
        try:
            sock.connect((self.host, self.port))
            return True
        except OSError:
            return False
        finally:
            sock.close()

    def _default_read_auth(self):
        path = self.root / '.colleague' / 'daemon.auth'
        try:
            raw = path.read_text(encoding='utf-8')
        except OSError:
            return ''
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return raw.strip()
        token = parsed.get('token')
        return token if isinstance(token, str) else ''

    def _default_spawn(self):
        script = self.root / 'start-runtime-daemon.sh'
        return subprocess.Popen(
            ['bash', str(script)],
            cwd=str(self.root),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            env={
                **os.environ,
                'COLLEAGUE_ROOT': str(self.root),
                'COLLEAGUE_DAEMON_HOST': self.host,
                'COLLEAGUE_DAEMON_PORT': str(self.port),
            },
        )

    def _ensure_daemon(self):
        if self._is_port_open():
            if not self._token:
                self._token = self._read_auth()
            return
        if self._spawn_daemon is None:
            raise StartupError('runtime daemon is not running', code='daemon_unavailable')
        with self._start_lock:
            if self._is_port_open():
                self._token = self._read_auth()
                return
            self._spawn_daemon()
            deadline = time.time() + self._startup_timeout_s
            while time.time() < deadline:
                if self._is_port_open():
                    self._token = self._read_auth()
                    return
                time.sleep(0.05)
            raise StartupError('runtime daemon did not become ready', code='daemon_unavailable')

    def _http(self, method, path, body=None, headers=None, last_event_id='', retried=False):
        self._ensure_daemon()
        if not self._token:
            self._token = self._read_auth()
        if self._request:
            try:
                return self._request(method, path, body=body, token=self._token, last_event_id=last_event_id)
            except ColleagueError as error:
                if error.status == 401 and not retried:
                    self._token = self._read_auth()
                    if self._token:
                        return self._http(method, path, body=body, headers=headers,
                                          last_event_id=last_event_id, retried=True)
                raise
        payload = None if body is None else json.dumps(body).encode('utf-8')
        request_headers = {
            'Authorization': f'Bearer {self._token}',
            **(headers or {}),
        }
        if payload is not None:
            request_headers['Content-Type'] = 'application/json'
        if last_event_id:
            request_headers['Last-Event-ID'] = last_event_id
        req = urllib.request.Request(
            f'http://{self.host}:{self.port}{path}',
            data=payload,
            method=method,
            headers=request_headers,
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as response:
                raw = response.read()
                status = response.status
        except urllib.error.HTTPError as error:
            raw = error.read()
            status = error.code
        except OSError as error:
            raise StartupError(redact(error), code='daemon_unavailable') from error
        try:
            parsed = json.loads(raw.decode('utf-8') or '{}')
        except json.JSONDecodeError:
            parsed = {'error': {'message': raw[:200].decode('utf-8', 'replace')}}
        if status == 401 and not retried:
            self._token = self._read_auth()
            if self._token:
                return self._http(method, path, body=body, headers=headers, last_event_id=last_event_id, retried=True)
        if status >= 400:
            raise _map_http_error(status, parsed, f'{method} {path} failed')
        return parsed

    def create_meeting(self, payload):
        try:
            return self._http('POST', '/v1/meetings', payload)
        except ValidationError:
            raise
        except ColleagueError as error:
            if error.status and error.status < 500 and error.status != 409 and not isinstance(error, StartupError):
                raise StartupError(str(error), status=error.status, code=error.code) from error
            raise

    def get_meeting(self, meeting_id):
        return self._http('GET', f'/v1/meetings/{quote(meeting_id)}')

    def update_context(self, meeting_id, context):
        return self._http('POST', f'/v1/meetings/{quote(meeting_id)}/context', context)

    def cancel_meeting(self, meeting_id):
        return self._http('POST', f'/v1/meetings/{quote(meeting_id)}/cancel', {})

    def get_handoff(self, meeting_id):
        return self._http('GET', f'/v1/meetings/{quote(meeting_id)}/handoff')

    def retry_handoff(self, meeting_id):
        return self._http('POST', f'/v1/meetings/{quote(meeting_id)}/handoff/retry', {})

    def events(self, meeting_id, *, last_event_id='', seen=None, stop=None):
        delivered = seen if seen is not None else set()
        cursor = last_event_id
        while stop is None or not stop.is_set():
            try:
                if self._request:
                    payload = self._request(
                        'GET',
                        f'/v1/meetings/{quote(meeting_id)}/events',
                        token=self._token,
                        last_event_id=cursor,
                        stream=True,
                    )
                    chunks = payload if isinstance(payload, list) else [payload]
                    for item in chunks:
                        if not item:
                            continue
                        event_id = item.get('id')
                        if event_id:
                            if event_id in delivered:
                                continue
                            delivered.add(event_id)
                            cursor = event_id
                        yield item
                    time.sleep(0.05)
                    continue
                connection = HTTPConnection(self.host, self.port, timeout=30)
                headers = {
                    'Authorization': f'Bearer {self._token}',
                    'Accept': 'text/event-stream',
                }
                if cursor:
                    headers['Last-Event-ID'] = cursor
                connection.request('GET', f'/v1/meetings/{quote(meeting_id)}/events', headers=headers)
                response = connection.getresponse()
                if response.status == 401:
                    self._token = self._read_auth()
                    connection.close()
                    continue
                buffer = ''
                while stop is None or not stop.is_set():
                    chunk = response.read(256)
                    if not chunk:
                        break
                    buffer += chunk.decode('utf-8', 'replace').replace('\r\n', '\n')
                    while '\n\n' in buffer:
                        block, buffer = buffer.split('\n\n', 1)
                        event = parse_sse_block(block)
                        if not event:
                            continue
                        event_id = event.get('id')
                        if event_id:
                            if event_id in delivered:
                                continue
                            delivered.add(event_id)
                            cursor = event_id
                        yield event
                connection.close()
            except Exception:
                if stop is not None and stop.is_set():
                    return
                time.sleep(0.1)


def create_loopback_transport(**options):
    return LoopbackTransport(**options)


class MeetingHandle:
    def __init__(self, session, transport: LoopbackTransport):
        self.id = session['id']
        self._session = session
        self._transport = transport
        self._listeners: Dict[str, set] = {}
        self._seen = set()
        self._stop = threading.Event()
        self._cancelled = False
        self._handoff = None
        self._error = None
        self._done = threading.Event()
        self._append_failed = None
        self._lock = threading.Lock()
        self._pump = threading.Thread(target=self._run_pump, daemon=True)
        self._poll = threading.Thread(target=self._poll_handoff, daemon=True)
        self._pump.start()
        self._poll.start()

    async def status(self):
        self._session = self._transport.get_meeting(self.id)
        return self._session

    async def add_context(self, context):
        validated = validate_context(context)
        self._session = self._transport.update_context(self.id, validated)
        return self._session

    async def cancel(self):
        if self._cancelled:
            return self._session
        self._cancelled = True
        try:
            self._session = self._transport.cancel_meeting(self.id)
        except ColleagueError as error:
            if error.status in {404, 409}:
                return self._session
            raise
        return self._session

    async def retry_finalization(self):
        return self._transport.retry_handoff(self.id)

    def on(self, name: str, handler: Callable[[Any], None]):
        if not callable(handler):
            raise ValidationError('event handler must be a function')
        self._listeners.setdefault(name, set()).add(handler)
        return lambda: self._listeners.get(name, set()).discard(handler)

    def _emit(self, event):
        kind = event.get('type') or ''
        for handler in list(self._listeners.get('event', ())):
            handler(event)
        if kind.startswith('meeting.'):
            for handler in list(self._listeners.get('state', ())):
                handler(event)
        if kind.startswith('transcript.'):
            for handler in list(self._listeners.get('transcript', ())):
                handler(event)
        if kind.startswith('delegation.'):
            for handler in list(self._listeners.get('delegation', ())):
                handler(event)
        if kind in {'approval.required', 'approval_required'}:
            for handler in list(self._listeners.get('approval_required', ())):
                handler(event)

    def _run_pump(self):
        try:
            for event in self._transport.events(self.id, seen=self._seen, stop=self._stop):
                if self._stop.is_set():
                    return
                self._emit(event)
                if event.get('type') == 'handoff.ready' and event.get('handoff'):
                    self._resolve(event['handoff'])
                    return
                if event.get('type') == 'handoff.append_failed':
                    self._append_failed = event
        except Exception:
            return

    def _resolve(self, handoff):
        with self._lock:
            if self._done.is_set():
                return
            self._handoff = handoff
            self._stop.set()
            self._done.set()

    def _reject(self, error):
        with self._lock:
            if self._done.is_set():
                return
            self._error = error
            self._stop.set()
            self._done.set()

    def _poll_handoff(self):
        delay = 0.04
        while not self._done.is_set():
            try:
                handoff = self._transport.get_handoff(self.id)
                if handoff and handoff.get('meetingId'):
                    self._resolve(handoff)
                    return
            except FinalizationError as error:
                if error.handoff and error.handoff.get('partial'):
                    self._resolve(error.handoff)
                    return
                if self._append_failed and self._append_failed.get('retryable'):
                    try:
                        self._resolve(self._transport.retry_handoff(self.id))
                        return
                    except ColleagueError:
                        pass
                elif self._append_failed and self._append_failed.get('retryable') is False:
                    self._reject(FinalizationError(
                        str(error),
                        archive_path=error.archive_path or f'recordings/{self.id}',
                        status=error.status,
                    ))
                    return
            except StartupError:
                pass
            except ColleagueError:
                pass
            time.sleep(delay)
            delay = min(delay * 1.5, 0.25)

    async def finished(self):
        import asyncio
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._done.wait)
        if self._error is not None:
            raise self._error
        return self._handoff

    async def events(self) -> AsyncIterator[dict]:
        import asyncio
        pending: queue.Queue = queue.Queue()

        def push(event):
            pending.put(event)

        off = self.on('event', push)
        try:
            while True:
                try:
                    event = pending.get_nowait()
                except queue.Empty:
                    if self._done.is_set():
                        try:
                            event = pending.get_nowait()
                        except queue.Empty:
                            return
                    else:
                        await asyncio.sleep(0.02)
                        continue
                yield event
        finally:
            off()


class Colleague:
    def __init__(self, *, transport=None, **options):
        self._transport = transport or create_loopback_transport(**options)
        self._inflight = {}
        self._handles = {}
        self._lock = threading.Lock()

    async def join_meeting(self, request):
        payload = validate_join_request(request)
        key = join_key(payload)
        with self._lock:
            existing = self._inflight.get(key)
            if existing is not None:
                waiter = existing
            else:
                waiter = threading.Event()
                holder = {'handle': None, 'error': None, 'ready': waiter}
                self._inflight[key] = holder
                existing = None
        if existing is not None:
            import asyncio
            await asyncio.get_running_loop().run_in_executor(None, existing['ready'].wait)
            if existing['error']:
                raise existing['error']
            return existing['handle']
        try:
            handle = self._join(payload, key)
            holder['handle'] = handle
            return handle
        except Exception as error:
            holder['error'] = error
            raise
        finally:
            waiter.set()
            with self._lock:
                self._inflight.pop(key, None)

    def _join(self, payload, key):
        with self._lock:
            for handle in self._handles.values():
                if join_key({
                    'meetingUrl': handle._session['meetingUrl'],
                    'agentSession': handle._session['agentSession'],
                }) == key:
                    return handle
        session = self._transport.create_meeting(payload)
        with self._lock:
            existing = self._handles.get(session['id'])
            if existing:
                return existing
            handle = MeetingHandle(session, self._transport)
            self._handles[session['id']] = handle
            return handle
