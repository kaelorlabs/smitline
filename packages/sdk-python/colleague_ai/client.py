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
    import re
    from urllib.parse import urlparse
    parsed = urlparse(url)
    if parsed.scheme != 'https':
        raise ValidationError('url must be an https Zoom, Teams, or Google Meet invitation')
    host = (parsed.hostname or '').lower()
    if host == 'zoom.us' or host.endswith('.zoom.us'):
        return 'zoom'
    if host in {'teams.microsoft.com', 'teams.live.com'}:
        return 'teams'
    path = parsed.path or ''
    if host == 'meet.google.com' and re.fullmatch(r'/[a-z]{3}-[a-z]{4}-[a-z]{3}/?', path, re.I):
        return 'meet'
    raise ValidationError('url must be a Zoom, Teams, or Google Meet invitation')


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
    if out['workspace'] == 'none':
        if out['commands'] == 'allowed':
            raise ValidationError('commands cannot be allowed when workspace is none')
        if out['edits'] != 'disabled' or out['commits'] != 'disabled' or out['pushes'] != 'disabled':
            raise ValidationError('workspace none cannot authorize edits, commits, or pushes')
    if out['workspace'] == 'read-only' and out['edits'] == 'allowed':
        raise ValidationError('edits cannot be allowed without workspace-write')
    if out['workspace'] != 'workspace-write' and (out['commits'] != 'disabled' or out['pushes'] != 'disabled'):
        raise ValidationError('commits and pushes require workspace-write')
    if out['pushes'] == 'approval-required' and out['commits'] != 'approval-required':
        raise ValidationError('pushes require commits to be approval-required')
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
        git = value['git']
        extra = set(git) - {'branch', 'commit', 'dirty'}
        if extra:
            raise ValidationError(f'context.git.{next(iter(extra))} is not allowed')
        out['git'] = {}
        if 'branch' in git:
            out['git']['branch'] = _require_string(git['branch'], 'context.git.branch')
        if 'commit' in git:
            out['git']['commit'] = _require_string(git['commit'], 'context.git.commit')
        if 'dirty' in git:
            if not isinstance(git['dirty'], bool):
                raise ValidationError('context.git.dirty must be a boolean')
            out['git']['dirty'] = git['dirty']
    return out


def validate_join_request(request):
    if not _is_mapping(request):
        raise ValidationError('join request must be an object')
    _reject_secrets(request)
    url = _require_string(request.get('url'), 'url')
    platform_for_url(url)
    payload = {
        'meetingUrl': url,
        'agentSession': validate_agent_session(request.get('agentSession')),
        'context': validate_context(request.get('context'), required=False),
        'permissions': validate_permissions(request.get('permissions')),
    }
    if 'camera' in request:
        payload['camera'] = _validate_camera(request.get('camera'))
    if 'screenShare' in request:
        payload['screenShare'] = _validate_screen_share(request.get('screenShare'))
    return payload


def _validate_screen_share(value):
    if not _is_mapping(value):
        raise ValidationError('screenShare must be an object')
    known = {'enabled', 'captureIntervalMs', 'minChange', 'maxFrames', 'maxBytes', 'retentionSeconds',
             'settleTicks'}
    extra = set(value) - known
    if extra:
        raise ValidationError(f'screenShare.{next(iter(extra))} is not allowed')
    out = {}
    if 'enabled' in value:
        if not isinstance(value['enabled'], bool):
            raise ValidationError('screenShare.enabled must be a boolean')
        out['enabled'] = value['enabled']
    if 'captureIntervalMs' in value:
        interval = value['captureIntervalMs']
        if not isinstance(interval, int) or isinstance(interval, bool) or interval < 2000 or interval > 15000:
            raise ValidationError('screenShare.captureIntervalMs is out of bounds')
        out['captureIntervalMs'] = interval
    if 'minChange' in value:
        change = value['minChange']
        if not isinstance(change, (int, float)) or isinstance(change, bool) or change < 0 or change > 1:
            raise ValidationError('screenShare.minChange is out of bounds')
        out['minChange'] = float(change)
    if 'maxFrames' in value:
        frames = value['maxFrames']
        if not isinstance(frames, int) or isinstance(frames, bool) or frames < 1 or frames > 50:
            raise ValidationError('screenShare.maxFrames is out of bounds')
        out['maxFrames'] = frames
    if 'maxBytes' in value:
        size = value['maxBytes']
        if not isinstance(size, int) or isinstance(size, bool) or size < 50_000 or size > 12_000_000:
            raise ValidationError('screenShare.maxBytes is out of bounds')
        out['maxBytes'] = size
    if 'retentionSeconds' in value:
        seconds = value['retentionSeconds']
        if not isinstance(seconds, int) or isinstance(seconds, bool) or seconds < 30 or seconds > 6 * 3600:
            raise ValidationError('screenShare.retentionSeconds is out of bounds')
        out['retentionSeconds'] = seconds
    if 'settleTicks' in value:
        ticks = value['settleTicks']
        if not isinstance(ticks, int) or isinstance(ticks, bool) or ticks < 0 or ticks > 5:
            raise ValidationError('screenShare.settleTicks is out of bounds')
        out['settleTicks'] = ticks
    return out


def _validate_camera(value):
    if not _is_mapping(value):
        raise ValidationError('camera must be an object')
    known = {'enabled', 'defaultOn', 'avatarDataUri'}
    extra = set(value) - known
    if extra:
        raise ValidationError(f'camera.{next(iter(extra))} is not allowed')
    out = {}
    if 'enabled' in value:
        if not isinstance(value['enabled'], bool):
            raise ValidationError('camera.enabled must be a boolean')
        out['enabled'] = value['enabled']
    if 'defaultOn' in value:
        if not isinstance(value['defaultOn'], bool):
            raise ValidationError('camera.defaultOn must be a boolean')
        out['defaultOn'] = value['defaultOn']
    if 'avatarDataUri' in value:
        uri = _require_string(value['avatarDataUri'], 'camera.avatarDataUri', max_length=120000)
        if not uri.startswith('data:image/'):
            raise ValidationError('camera.avatarDataUri must be an image data URI')
        out['avatarDataUri'] = uri
    return out


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
    mapped = _map_http_error_class(status, payload, fallback)
    error = (payload or {}).get('error') or {}
    mapped.details = {key: value for key, value in error.items() if key not in ('code', 'message')}
    return mapped


def _map_http_error_class(status, payload, fallback):
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
                 is_port_open=None, read_auth=None, autostart=True, startup_timeout_s=60.0):
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

    def _http(self, method, path, body=None, headers=None, last_event_id='', retried=False, raw=False,
              timeout=30):
        self._ensure_daemon()
        if not self._token:
            self._token = self._read_auth()
        if self._request:
            try:
                if raw:
                    try:
                        return self._request(method, path, body=body, token=self._token,
                                             last_event_id=last_event_id, raw=True)
                    except TypeError:
                        return self._request(method, path, body=body, token=self._token,
                                             last_event_id=last_event_id)
                return self._request(method, path, body=body, token=self._token, last_event_id=last_event_id)
            except ColleagueError as error:
                if error.status == 401 and not retried:
                    self._token = self._read_auth()
                    if self._token:
                        return self._http(method, path, body=body, headers=headers,
                                          last_event_id=last_event_id, retried=True, raw=raw,
                                          timeout=timeout)
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
        media_type = 'application/octet-stream'
        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                raw_body = response.read()
                status = response.status
                media_type = response.headers.get('Content-Type') or media_type
        except urllib.error.HTTPError as error:
            raw_body = error.read()
            status = error.code
            media_type = error.headers.get('Content-Type') if error.headers else media_type
        except OSError as error:
            raise StartupError(redact(error), code='daemon_unavailable') from error
        if status == 401 and not retried:
            self._token = self._read_auth()
            if self._token:
                return self._http(method, path, body=body, headers=headers, last_event_id=last_event_id,
                                  retried=True, raw=raw)
        if status >= 400:
            try:
                parsed = json.loads(raw_body.decode('utf-8') or '{}')
            except json.JSONDecodeError:
                parsed = {'error': {'message': raw_body[:200].decode('utf-8', 'replace')}}
            raise _map_http_error(status, parsed, f'{method} {path} failed')
        if raw:
            return {'mediaType': media_type, 'body': raw_body}
        try:
            return json.loads(raw_body.decode('utf-8') or '{}')
        except json.JSONDecodeError:
            return {'error': {'message': raw_body[:200].decode('utf-8', 'replace')}}

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

    def list_approvals(self, meeting_id):
        return self._http('GET', f'/v1/meetings/{quote(meeting_id)}/approvals')

    def get_approval(self, meeting_id, approval_id):
        return self._http('GET', f'/v1/meetings/{quote(meeting_id)}/approvals/{quote(approval_id)}')

    def create_approval(self, meeting_id, payload):
        return self._http('POST', f'/v1/meetings/{quote(meeting_id)}/approvals', payload)

    def decide_approval(self, meeting_id, approval_id, decision):
        body = {'decision': decision} if isinstance(decision, str) else dict(decision or {})
        return self._http(
            'POST',
            f'/v1/meetings/{quote(meeting_id)}/approvals/{quote(approval_id)}/decision',
            body,
        )

    def list_artifacts(self, meeting_id):
        return self._http('GET', f'/v1/meetings/{quote(meeting_id)}/artifacts')

    def get_artifact(self, meeting_id, artifact_id):
        return self._http(
            'GET', f'/v1/meetings/{quote(meeting_id)}/artifacts/{quote(artifact_id)}')

    def get_artifact_content(self, meeting_id, artifact_id):
        return self._http(
            'GET',
            f'/v1/meetings/{quote(meeting_id)}/artifacts/{quote(artifact_id)}/content',
            raw=True,
        )

    def create_commit(self, meeting_id, payload):
        return self._http('POST', f'/v1/meetings/{quote(meeting_id)}/commits', payload)

    def list_commits(self, meeting_id):
        return self._http('GET', f'/v1/meetings/{quote(meeting_id)}/commits')

    def get_commit(self, meeting_id, operation_id):
        return self._http(
            'GET', f'/v1/meetings/{quote(meeting_id)}/commits/{quote(operation_id)}')

    def create_push(self, meeting_id, payload):
        return self._http('POST', f'/v1/meetings/{quote(meeting_id)}/pushes', payload)

    def list_pushes(self, meeting_id):
        return self._http('GET', f'/v1/meetings/{quote(meeting_id)}/pushes')

    def get_push(self, meeting_id, operation_id):
        return self._http(
            'GET', f'/v1/meetings/{quote(meeting_id)}/pushes/{quote(operation_id)}')

    def get_screen_share(self, meeting_id):
        return self._http('GET', f'/v1/meetings/{quote(meeting_id)}/screen-share')

    def pause_screen_share(self, meeting_id):
        return self._http('POST', f'/v1/meetings/{quote(meeting_id)}/screen-share/pause', {})

    def resume_screen_share(self, meeting_id):
        return self._http('POST', f'/v1/meetings/{quote(meeting_id)}/screen-share/resume', {})

    def list_screen_share_observations(self, meeting_id):
        return self._http('GET', f'/v1/meetings/{quote(meeting_id)}/screen-share/observations')

    def list_providers(self):
        return self._http('GET', '/v1/providers')

    def runner_status(self):
        return self._http('GET', '/v1/runner')

    def pair_runner(self, payload=None):
        return self._http('POST', '/v1/runner/pair', payload or {})

    def complete_runner_pair(self, payload):
        return self._http('POST', '/v1/runner/pair/complete', payload)

    def unpair_runner(self):
        return self._http('POST', '/v1/runner/unpair', {})

    def check_call(self, brief):
        return self._http('POST', '/v1/calls/check', brief)

    def start_call(self, brief):
        return self._http('POST', '/v1/calls', brief)

    def get_call(self, call_id):
        return self._http('GET', f'/v1/calls/{quote(call_id)}')

    def wait_for_call(self, call_id, timeout_seconds=60):
        timeout = max(0, min(float(timeout_seconds or 0), 300))
        return self._http('GET', f'/v1/calls/{quote(call_id)}/wait?timeout={timeout:g}',
                          timeout=timeout + 15)

    def list_calls(self, limit=20):
        limit = max(1, min(int(limit or 20), 100))
        return self._http('GET', f'/v1/calls?limit={limit}')

    def instruct_call(self, call_id, text):
        return self._http('POST', f'/v1/calls/{quote(call_id)}/instructions', {'text': text})

    def end_call(self, call_id):
        return self._http('POST', f'/v1/calls/{quote(call_id)}/end', {})

    def transfer_call(self, call_id):
        return self._http('POST', f'/v1/calls/{quote(call_id)}/transfer', {})

    def list_voices(self):
        return self._http('GET', '/v1/voices')

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
        self._emitted = []
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

    async def list_approvals(self):
        return self._transport.list_approvals(self.id)

    async def get_approval(self, approval_id):
        if not approval_id:
            raise ValidationError('approvalId is required')
        return self._transport.get_approval(self.id, approval_id)

    async def decide_approval(self, approval_id, decision):
        if not approval_id:
            raise ValidationError('approvalId is required')
        value = decision if isinstance(decision, str) else (decision or {}).get('decision')
        if value not in {'approved', 'denied'}:
            raise ValidationError('decision must be approved or denied')
        return self._transport.decide_approval(self.id, approval_id, {'decision': value})

    async def list_artifacts(self):
        return self._transport.list_artifacts(self.id)

    async def get_artifact(self, artifact_id):
        if not artifact_id:
            raise ValidationError('artifactId is required')
        return self._transport.get_artifact(self.id, artifact_id)

    async def get_artifact_content(self, artifact_id):
        if not artifact_id:
            raise ValidationError('artifactId is required')
        return self._transport.get_artifact_content(self.id, artifact_id)

    async def create_commit(self, payload):
        return self._transport.create_commit(self.id, payload)

    async def list_commits(self):
        return self._transport.list_commits(self.id)

    async def get_commit(self, operation_id):
        if not operation_id:
            raise ValidationError('operationId is required')
        return self._transport.get_commit(self.id, operation_id)

    async def create_push(self, payload):
        return self._transport.create_push(self.id, payload)

    async def list_pushes(self):
        return self._transport.list_pushes(self.id)

    async def get_push(self, operation_id):
        if not operation_id:
            raise ValidationError('operationId is required')
        return self._transport.get_push(self.id, operation_id)

    async def get_screen_share(self):
        return self._transport.get_screen_share(self.id)

    async def pause_screen_share(self):
        return self._transport.pause_screen_share(self.id)

    async def resume_screen_share(self):
        return self._transport.resume_screen_share(self.id)

    async def list_screen_share_observations(self):
        return self._transport.list_screen_share_observations(self.id)

    def on(self, name: str, handler: Callable[[Any], None]):
        if not callable(handler):
            raise ValidationError('event handler must be a function')
        self._listeners.setdefault(name, set()).add(handler)
        return lambda: self._listeners.get(name, set()).discard(handler)

    def _emit(self, event):
        kind = event.get('type') or ''
        with self._lock:
            self._emitted.append(event)
            listeners = {
                'event': list(self._listeners.get('event', ())),
                'state': list(self._listeners.get('state', ())),
                'transcript': list(self._listeners.get('transcript', ())),
                'delegation': list(self._listeners.get('delegation', ())),
                'approval': list(self._listeners.get('approval', ())),
                'approval_required': list(self._listeners.get('approval_required', ())),
                'workspace': list(self._listeners.get('workspace', ())),
                'git': list(self._listeners.get('git', ())),
                'artifact': list(self._listeners.get('artifact', ())),
                'screen_share': list(self._listeners.get('screen_share', ())),
            }
        for handler in listeners['event']:
            handler(event)
        if kind.startswith('meeting.'):
            for handler in listeners['state']:
                handler(event)
        if kind.startswith('transcript.'):
            for handler in listeners['transcript']:
                handler(event)
        if kind.startswith('delegation.'):
            for handler in listeners['delegation']:
                handler(event)
        if kind.startswith('approval.'):
            for handler in listeners['approval']:
                handler(event)
        if kind in {'approval.required', 'approval_required'}:
            for handler in listeners['approval_required']:
                handler(event)
        if kind.startswith('workspace.action.'):
            for handler in listeners.get('workspace', ()):
                handler(event)
        if kind.startswith('git.action.'):
            for handler in listeners.get('git', ()):
                handler(event)
        if kind == 'artifact.created':
            for handler in listeners.get('artifact', ()):
                handler(event)
        if kind.startswith('screen_share.'):
            for handler in listeners.get('screen_share', ()):
                handler(event)

    def _run_pump(self):
        try:
            for event in self._transport.events(self.id, seen=self._seen, stop=self._stop):
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
        delivered = set()

        def push(event):
            event_id = event.get('id')
            if event_id:
                if event_id in delivered:
                    return
                delivered.add(event_id)
            pending.put(event)

        off = self.on('event', push)
        try:
            with self._lock:
                snapshot = list(self._emitted)
            for event in snapshot:
                push(event)
            while True:
                try:
                    event = pending.get_nowait()
                except queue.Empty:
                    if self._done.is_set() and not self._pump.is_alive():
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

    async def list_providers(self):
        return self._transport.list_providers()

    async def runner_status(self):
        return self._transport.runner_status()

    async def pair_runner(self, payload=None):
        return self._transport.pair_runner(payload)

    async def complete_runner_pair(self, payload):
        return self._transport.complete_runner_pair(payload)

    async def unpair_runner(self):
        return self._transport.unpair_runner()

    async def check_call(self, brief):
        """Validate a call brief and report missing configuration without dialing."""
        return self._transport.check_call(brief)

    async def start_call(self, brief):
        """Start a phone call or meeting from a brief; returns the queued call."""
        return self._transport.start_call(brief)

    async def get_call(self, call_id):
        return self._transport.get_call(call_id)

    async def wait_for_call(self, call_id, timeout_seconds=60):
        """Wait until the call is terminal or the timeout (at most 300 s) passes."""
        import asyncio
        return await asyncio.to_thread(self._transport.wait_for_call, call_id, timeout_seconds)

    async def list_calls(self, limit=20):
        return self._transport.list_calls(limit)['calls']

    async def instruct_call(self, call_id, text):
        return self._transport.instruct_call(call_id, text)

    async def end_call(self, call_id):
        return self._transport.end_call(call_id)

    async def transfer_call(self, call_id):
        return self._transport.transfer_call(call_id)

    async def list_voices(self):
        return self._transport.list_voices()

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
