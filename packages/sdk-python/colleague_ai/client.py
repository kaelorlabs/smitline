"""Local Smitline Python SDK: phone calls and meetings through the runtime daemon."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import quote

SCHEMA_VERSION = 1
SDK_VERSION = '0.1.1'

def redact(value: Any) -> str:
    import re
    text = '' if value is None else str(value)
    text = re.sub(r'Bearer\s+\S+', 'Bearer [redacted]', text, flags=re.I)
    text = re.sub(r'\bsk-[A-Za-z0-9_-]{8,}\b', '[redacted]', text)
    text = re.sub(r'[A-Za-z0-9+/_-]{40,}', '[redacted]', text)
    return text


class ColleagueError(Exception):
    def __init__(self, message, *, code='runtime', status=None, archive_path=None):
        super().__init__(redact(message))
        self.code = code
        self.status = status
        self.archive_path = archive_path


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



def _map_http_error(status, payload, fallback):
    mapped = _map_http_error_class(status, payload, fallback)
    error = (payload or {}).get('error') or {}
    mapped.details = {key: value for key, value in error.items() if key not in ('code', 'message')}
    return mapped


def _map_http_error_class(status, payload, fallback):
    error = (payload or {}).get('error') or {}
    code = error.get('code') or fallback
    message = redact(error.get('message') or fallback)
    if status in {400, 422}:
        return ValidationError(message, status=status, code=code)
    if status == 503 or code in {'supervisor_unavailable', 'daemon_unavailable'}:
        return StartupError(message, status=status, code=code)
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

    def _http(self, method, path, body=None, headers=None, retried=False, timeout=30):
        self._ensure_daemon()
        if not self._token:
            self._token = self._read_auth()
        if self._request:
            try:
                return self._request(method, path, body=body, token=self._token)
            except ColleagueError as error:
                if error.status == 401 and not retried:
                    self._token = self._read_auth()
                    if self._token:
                        return self._http(method, path, body=body, headers=headers, retried=True,
                                          timeout=timeout)
                raise
        payload = None if body is None else json.dumps(body).encode('utf-8')
        request_headers = {
            'Authorization': f'Bearer {self._token}',
            **(headers or {}),
        }
        if payload is not None:
            request_headers['Content-Type'] = 'application/json'
        req = urllib.request.Request(
            f'http://{self.host}:{self.port}{path}',
            data=payload,
            method=method,
            headers=request_headers,
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                raw_body = response.read()
                status = response.status
        except urllib.error.HTTPError as error:
            raw_body = error.read()
            status = error.code
        except OSError as error:
            raise StartupError(redact(error), code='daemon_unavailable') from error
        if status == 401 and not retried:
            self._token = self._read_auth()
            if self._token:
                return self._http(method, path, body=body, headers=headers, retried=True, timeout=timeout)
        if status >= 400:
            try:
                parsed = json.loads(raw_body.decode('utf-8') or '{}')
            except json.JSONDecodeError:
                parsed = {'error': {'message': raw_body[:200].decode('utf-8', 'replace')}}
            raise _map_http_error(status, parsed, f'{method} {path} failed')
        try:
            return json.loads(raw_body.decode('utf-8') or '{}')
        except json.JSONDecodeError:
            return {'error': {'message': raw_body[:200].decode('utf-8', 'replace')}}

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

    def instruct_call(self, call_id, text, silent=False):
        body = {'text': text, 'silent': True} if silent else {'text': text}
        return self._http('POST', f'/v1/calls/{quote(call_id)}/instructions', body)

    def get_profile(self):
        return self._http('GET', '/v1/profile')

    def update_profile(self, update):
        return self._http('PATCH', '/v1/profile', update)

    def get_do_not_call(self):
        return self._http('GET', '/v1/do-not-call')

    def update_do_not_call(self, update):
        return self._http('PATCH', '/v1/do-not-call', update)

    def end_call(self, call_id):
        return self._http('POST', f'/v1/calls/{quote(call_id)}/end', {})

    def transfer_call(self, call_id):
        return self._http('POST', f'/v1/calls/{quote(call_id)}/transfer', {})

    def list_voices(self):
        return self._http('GET', '/v1/voices')


def create_loopback_transport(**options):
    return LoopbackTransport(**options)


class Colleague:
    def __init__(self, *, transport=None, **options):
        self._transport = transport or create_loopback_transport(**options)

    async def check_call(self, brief):
        """Validate a call brief and report missing configuration without dialing."""
        return self._transport.check_call(brief)

    async def start_call(self, brief):
        """Place a phone call (channel 'phone', ``to`` an E.164 number) or join a meeting
        (channel 'meeting', ``to`` the Zoom, Teams, or Google Meet invite URL); returns the queued call."""
        return self._transport.start_call(brief)

    async def get_call(self, call_id):
        return self._transport.get_call(call_id)

    async def wait_for_call(self, call_id, timeout_seconds=60):
        """Wait until the call is terminal or the timeout (at most 300 s) passes."""
        import asyncio
        return await asyncio.to_thread(self._transport.wait_for_call, call_id, timeout_seconds)

    async def list_calls(self, limit=20):
        return self._transport.list_calls(limit)['calls']

    async def instruct_call(self, call_id, text, silent=False):
        return self._transport.instruct_call(call_id, text, silent)

    async def get_profile(self):
        return self._transport.get_profile()

    async def update_profile(self, update):
        return self._transport.update_profile(update)

    async def get_do_not_call(self):
        """Numbers Smitline refuses to call because the person asked not to be called again."""
        return self._transport.get_do_not_call()

    async def update_do_not_call(self, update):
        """Add numbers ({'add': [...]}) or remove them ({'remove': [...]})."""
        return self._transport.update_do_not_call(update)

    async def end_call(self, call_id):
        return self._transport.end_call(call_id)

    async def transfer_call(self, call_id):
        return self._transport.transfer_call(call_id)

    async def list_voices(self):
        return self._transport.list_voices()
