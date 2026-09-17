"""Hosted/remote runtime foundation (v1). Local loopback remains the supported mode.

This module is a control-plane and runner-transport abstraction plus an in-memory
mock remote plane. It is not production hosting. Credentials, browser profiles,
workspace bytes, transcripts, screenshots, and provider session handles stay on
the local runner. FakeTlsSession is a test double for authenticated encrypted
transport; it is not a TLS cipher and must not be used on a public network.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import hashlib
import hmac
import json
import os
import secrets
import threading

from agent_sessions import MeetingPermissions
from schema_validation import omit_none, reject_secrets


PROTOCOL_VERSION = 1
PAIRING_TTL_SECONDS = 120
HEARTBEAT_TIMEOUT_SECONDS = 30
RATE_LIMIT = 20
RATE_WINDOW_SECONDS = 60
CODE_ALPHABET = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789'

MESSAGE_KINDS = (
    'runner.register',
    'runner.capabilities',
    'job.assign',
    'job.cancel',
    'runner.heartbeat',
    'event.upload',
    'runner.reconnect',
    'handoff.final',
    'audit.record',
)

JOB_BINDING_FIELDS = (
    'userId', 'deviceId', 'provider', 'sessionId', 'workspaceIdentity',
    'permissions', 'meetingId',
)
FORBIDDEN_REMOTE_OPS = frozenset({
    'shell',
    'filesystem_path',
    'credential_retrieve',
    'browser_profile_download',
    'transcript_dump',
    'screenshot_dump',
})
REMOTE_SAFE_EVENT_TYPES = frozenset({
    'meeting.joining',
    'meeting.waiting_for_admission',
    'meeting.live',
    'meeting.ended',
    'agent_session.locked',
    'agent_session.released',
    'delegation.started',
    'delegation.completed',
    'delegation.cancelled',
    'approval.required',
    'approval.approved',
    'approval.denied',
    'approval.expired',
    'approval.cancelled',
    'artifact.created',
    'workspace.action.planned',
    'workspace.action.started',
    'workspace.action.completed',
    'workspace.action.failed',
    'workspace.action.cancelled',
    'git.action.requested',
    'git.action.completed',
    'git.action.failed',
    'git.action.cancelled',
    'screen_share.started',
    'screen_share.stopped',
    'screen_share.failed',
    'handoff.ready',
    'handoff.append_failed',
    'presence.updated',
})
STRIP_EVENT_KEYS = frozenset({
    'entry', 'text', 'transcript', 'summary', 'bytes', 'png', 'image', 'content',
    'path', 'paths', 'argv', 'command', 'cwd', 'workspace', 'visibleText',
    'observation', 'frame', 'cookie', 'authorization',
})
WORKSPACE_RANK = {'none': 0, 'read-only': 1, 'workspace-write': 2}
COMMAND_RANK = {'disabled': 0, 'approval-required': 1, 'allowed': 2}
COMMIT_RANK = {'disabled': 0, 'approval-required': 1}

_CONTROL_CAPABILITIES = {
    'browserProfiles': False,
    'microphone': False,
    'codingAgentCli': False,
    'workspaceBytes': False,
    'localGit': False,
    'artifactBytes': False,
    'providerSessions': False,
    'tenantMetadata': True,
    'lifecycleState': True,
    'encryptedRouting': True,
    'retentionRecords': True,
}
_RUNNER_CAPABILITIES = {
    'browserProfiles': True,
    'microphone': True,
    'codingAgentCli': True,
    'workspaceBytes': True,
    'localGit': True,
    'artifactBytes': True,
    'providerSessions': True,
    'loopbackDaemon': True,
}


class HostedRuntimeError(Exception):
    def __init__(self, code, message, status=400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


def _utcnow():
    return datetime.now(timezone.utc)


def _iso(moment):
    return moment.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _hash(value):
    return hashlib.sha256(str(value).encode('utf-8')).hexdigest()


def _new_id(prefix):
    return prefix + secrets.token_hex(8)


def _random_code(length=8):
    return ''.join(secrets.choice(CODE_ALPHABET) for _ in range(length))


def workspace_identity_from_path(path):
    resolved = str(Path(path).resolve())
    return 'ws-' + hashlib.sha256(resolved.encode('utf-8')).hexdigest()[:16]


def redact_error(exc):
    text = str(exc)
    text = text.replace('\\', '/')
    # Never echo paths, cookies, or key-shaped tokens from remote errors.
    if '/' in text or 'sk-' in text or 'Bearer' in text:
        return 'request failed'
    return text[:180]


class ControlPlane:
    """Versioned hosted control plane. Local loopback remains the default production path."""

    protocol_version = PROTOCOL_VERSION


class RunnerTransport:
    """Versioned runner envelope. Loopback identity is the default; FakeTlsSession is a test double."""

    protocol_version = PROTOCOL_VERSION

    def seal(self, payload):
        if not isinstance(payload, dict):
            raise HostedRuntimeError('invalid_request', 'transport payload must be an object', 422)
        body = dict(payload)
        body.setdefault('protocolVersion', PROTOCOL_VERSION)
        if body['protocolVersion'] != PROTOCOL_VERSION:
            raise HostedRuntimeError('unsupported_version', 'unsupported hosted protocol version', 422)
        return body

    def open(self, envelope):
        if not isinstance(envelope, dict):
            raise HostedRuntimeError('invalid_request', 'transport envelope must be an object', 422)
        version = envelope.get('protocolVersion', PROTOCOL_VERSION)
        if version != PROTOCOL_VERSION:
            raise HostedRuntimeError('unsupported_version', 'unsupported hosted protocol version', 422)
        return envelope

    def rotate(self, new_key, new_key_id=None):
        return 'loopback'


def narrow_permissions(local, remote):
    if not isinstance(local, MeetingPermissions) or not isinstance(remote, MeetingPermissions):
        raise HostedRuntimeError('job_binding_incomplete', 'permissions binding is required', 422)
    fields = (
        ('workspace', WORKSPACE_RANK),
        ('commands', COMMAND_RANK),
        ('edits', COMMAND_RANK),
        ('network', COMMAND_RANK),
        ('commits', COMMIT_RANK),
        ('pushes', COMMIT_RANK),
    )
    narrowed = {}
    for name, ranks in fields:
        local_value = getattr(local, name)
        remote_value = getattr(remote, name)
        if remote_value not in ranks or local_value not in ranks:
            raise HostedRuntimeError('job_binding_incomplete', f'permissions.{name} is invalid', 422)
        if ranks[remote_value] > ranks[local_value]:
            raise HostedRuntimeError(
                'permission_escalation',
                'hosted requests may only narrow local permissions',
                403,
            )
        narrowed[name] = remote_value if ranks[remote_value] <= ranks[local_value] else local_value
    return MeetingPermissions(**narrowed)


def filter_remote_event(event):
    if not isinstance(event, dict):
        return None
    event_type = event.get('type')
    if event_type not in REMOTE_SAFE_EVENT_TYPES:
        return None
    public = {}
    for key, value in event.items():
        if field_is_unsafe(key):
            continue
        if key in STRIP_EVENT_KEYS:
            continue
        public[key] = _strip_nested(value)
    if event_type == 'artifact.created':
        artifact = event.get('artifact') if isinstance(event.get('artifact'), dict) else {}
        public['artifact'] = omit_none({
            'id': artifact.get('id'),
            'type': artifact.get('type'),
            'size': artifact.get('size'),
            'createdAt': artifact.get('createdAt'),
        })
    if event_type == 'handoff.ready':
        public.pop('summary', None)
        public.pop('transcriptPath', None)
        public.pop('archivePath', None)
    try:
        reject_secrets(public, 'remote event')
    except ValueError:
        return None
    return public


def field_is_unsafe(name):
    from schema_validation import field_name_is_secret
    return field_name_is_secret(name)


def _strip_nested(value):
    if isinstance(value, dict):
        return {
            key: _strip_nested(item)
            for key, item in value.items()
            if key not in STRIP_EVENT_KEYS and not field_is_unsafe(key)
        }
    if isinstance(value, list):
        return [_strip_nested(item) for item in value]
    if isinstance(value, str) and ('/' in value or value.startswith('-')):
        # Raw filesystem paths and command fragments stay local.
        if value.startswith('/') or value.startswith('-') or '/Users/' in value or '/home/' in value:
            return None
    return value


class FakeTlsSession(RunnerTransport):
    """Authenticated encrypted envelope for tests. Not production TLS."""

    def __init__(self, key, key_id='k1'):
        if not key:
            raise ValueError('transport key is required')
        self._keys = {key_id: bytes(key) if isinstance(key, (bytes, bytearray)) else str(key).encode()}
        self._current = key_id
        self._previous = None
        self._seq = 0
        self._seen = set()
        self._lock = threading.Lock()

    @property
    def key_id(self):
        return self._current

    def rotate(self, new_key, new_key_id=None):
        with self._lock:
            new_id = new_key_id or ('k' + secrets.token_hex(2))
            material = bytes(new_key) if isinstance(new_key, (bytes, bytearray)) else str(new_key).encode()
            self._previous = self._current
            self._keys[new_id] = material
            self._current = new_id
            return new_id

    def seal(self, payload):
        if not isinstance(payload, dict):
            raise HostedRuntimeError('invalid_request', 'transport payload must be an object', 422)
        if payload.get('protocolVersion', PROTOCOL_VERSION) != PROTOCOL_VERSION:
            raise HostedRuntimeError('unsupported_version', 'unsupported hosted protocol version', 422)
        body = json.dumps(payload, separators=(',', ':'), sort_keys=True).encode('utf-8')
        with self._lock:
            self._seq += 1
            seq = self._seq
            key_id = self._current
            key = self._keys[key_id]
        nonce = secrets.token_bytes(16)
        ciphertext = _xor_keystream(body, key, nonce, seq)
        mac = _mac(key, key_id, seq, nonce, ciphertext)
        return {
            'protocolVersion': PROTOCOL_VERSION,
            'keyId': key_id,
            'seq': seq,
            'nonce': nonce.hex(),
            'ciphertext': ciphertext.hex(),
            'mac': mac,
        }

    def open(self, envelope):
        if not isinstance(envelope, dict):
            raise HostedRuntimeError('invalid_request', 'transport envelope must be an object', 422)
        if envelope.get('protocolVersion') != PROTOCOL_VERSION:
            raise HostedRuntimeError('unsupported_version', 'unsupported hosted protocol version', 422)
        key_id = envelope.get('keyId')
        seq = envelope.get('seq')
        nonce_hex = envelope.get('nonce')
        cipher_hex = envelope.get('ciphertext')
        mac = envelope.get('mac')
        if not isinstance(seq, int) or seq < 1 or not nonce_hex or not cipher_hex or not mac:
            raise HostedRuntimeError('invalid_request', 'transport envelope is incomplete', 422)
        with self._lock:
            key = self._keys.get(key_id)
            if key is None:
                raise HostedRuntimeError('unknown_key', 'transport key is unknown or rotated away', 401)
            seen_key = (key_id, seq, nonce_hex)
            if seen_key in self._seen:
                raise HostedRuntimeError('replayed_nonce', 'transport nonce or sequence was reused', 409)
            nonce = bytes.fromhex(nonce_hex)
            ciphertext = bytes.fromhex(cipher_hex)
            expected = _mac(key, key_id, seq, nonce, ciphertext)
            if not hmac.compare_digest(expected, str(mac)):
                raise HostedRuntimeError('auth_failed', 'transport authentication failed', 401)
            self._seen.add(seen_key)
        plain = _xor_keystream(ciphertext, key, nonce, seq)
        payload = json.loads(plain.decode('utf-8'))
        if payload.get('protocolVersion', PROTOCOL_VERSION) != PROTOCOL_VERSION:
            raise HostedRuntimeError('unsupported_version', 'unsupported hosted protocol version', 422)
        return payload


def _xor_keystream(data, key, nonce, seq):
    material = key + nonce + seq.to_bytes(8, 'big')
    out = bytearray()
    counter = 0
    while len(out) < len(data):
        block = hmac.new(key, material + counter.to_bytes(4, 'big'), hashlib.sha256).digest()
        out.extend(block)
        counter += 1
    return bytes(a ^ b for a, b in zip(data, out[:len(data)]))


def _mac(key, key_id, seq, nonce, ciphertext):
    msg = f'{key_id}|{seq}|'.encode() + nonce + ciphertext
    return hmac.new(key, msg, hashlib.sha256).hexdigest()


class InMemoryRemoteControlPlane(ControlPlane):
    """Mock remote control plane. Stores only tenant/device metadata and safe state."""

    def __init__(self, *, clock=None, pairing_ttl=PAIRING_TTL_SECONDS,
                 heartbeat_timeout=HEARTBEAT_TIMEOUT_SECONDS,
                 rate_limit=RATE_LIMIT, rate_window=RATE_WINDOW_SECONDS):
        self._clock = clock or _utcnow
        self._pairing_ttl = pairing_ttl
        self._heartbeat_timeout = heartbeat_timeout
        self._rate_limit = rate_limit
        self._rate_window = rate_window
        self._lock = threading.Lock()
        self.tenants = {}
        self.users = {}
        self.devices = {}
        self.pairings = {}
        self.runners = {}
        self.jobs = {}
        self.events = {}
        self.meetings = {}
        self.handoffs = {}
        self.routing = {}
        self.retention = {}
        self.audit = []
        self._rate = {}

    def _now(self):
        moment = self._clock()
        if moment.tzinfo is None:
            raise ValueError('clock must return a timezone-aware datetime')
        return moment.astimezone(timezone.utc)

    def _audit(self, kind, **fields):
        record = omit_none({'type': kind, 'at': _iso(self._now()), **fields})
        reject_secrets(record, 'audit')
        self.audit.append(record)
        return record

    def start_pairing(self, *, tenant_id='ten-local', user_id='usr-local'):
        if not tenant_id or not user_id:
            raise HostedRuntimeError('job_binding_incomplete', 'tenant and user are required', 422)
        pairing_id = _new_id('pair-')
        code = _random_code()
        expires = self._now() + timedelta(seconds=self._pairing_ttl)
        self.tenants.setdefault(tenant_id, {'id': tenant_id})
        self.users.setdefault(user_id, {'id': user_id, 'tenantId': tenant_id})
        self.pairings[pairing_id] = {
            'id': pairing_id,
            'tenantId': tenant_id,
            'userId': user_id,
            'codeHash': _hash(code),
            'expiresAt': expires,
            'used': False,
        }
        self._audit('pairing.started', pairingId=pairing_id, tenantId=tenant_id, userId=user_id)
        return {
            'pairingId': pairing_id,
            'pairingCode': code,
            'expiresAt': _iso(expires),
            'tenantId': tenant_id,
            'userId': user_id,
        }

    def complete_pairing(self, pairing_id, pairing_code):
        record = self.pairings.get(pairing_id)
        if record is None:
            raise HostedRuntimeError('not_found', 'pairing is not available', 404)
        if record['used']:
            self._audit('pairing.failed', pairingId=pairing_id, reason='replay')
            raise HostedRuntimeError('pairing_replay', 'pairing code was already used', 409)
        if self._now() >= record['expiresAt']:
            self._audit('pairing.failed', pairingId=pairing_id, reason='expired')
            raise HostedRuntimeError('pairing_expired', 'pairing code has expired', 410)
        if _hash(pairing_code) != record['codeHash']:
            self._audit('pairing.failed', pairingId=pairing_id, reason='mismatch')
            raise HostedRuntimeError('pairing_mismatch', 'pairing code is invalid', 401)
        record['used'] = True
        device_id = _new_id('dev-')
        enrollment = secrets.token_urlsafe(32)
        self.devices[device_id] = {
            'id': device_id,
            'tenantId': record['tenantId'],
            'userId': record['userId'],
            'enrollmentHash': _hash(enrollment),
            'revoked': False,
            'keyId': 'k1',
            'createdAt': _iso(self._now()),
        }
        self.routing[device_id] = secrets.token_hex(16)
        self._audit('pairing.completed', pairingId=pairing_id, deviceId=device_id, tenantId=record['tenantId'])
        return {
            'deviceId': device_id,
            'deviceEnrollment': enrollment,
            'tenantId': record['tenantId'],
            'userId': record['userId'],
        }

    def revoke_device(self, device_id):
        device = self.devices.get(device_id)
        if device is None:
            raise HostedRuntimeError('not_found', 'device is not paired', 404)
        device['revoked'] = True
        self.runners.pop(device_id, None)
        self._audit('device.revoked', deviceId=device_id, tenantId=device['tenantId'])
        return {'paired': False, 'deviceId': device_id}

    def authenticate_device(self, device_id, enrollment):
        device = self.devices.get(device_id)
        if device is None:
            raise HostedRuntimeError('not_found', 'device is not paired', 404)
        if device['revoked']:
            raise HostedRuntimeError('device_revoked', 'device pairing was revoked', 403)
        if _hash(enrollment) != device['enrollmentHash']:
            raise HostedRuntimeError('auth_failed', 'device enrollment is invalid', 401)
        return device

    def register_runner(self, device_id, capabilities=None):
        device = self._require_device(device_id)
        self.runners[device_id] = {
            'deviceId': device_id,
            'tenantId': device['tenantId'],
            'online': True,
            'capabilities': dict(capabilities or _RUNNER_CAPABILITIES),
            'lastHeartbeat': self._now(),
            'cursor': '',
        }
        self._audit('runner.registered', deviceId=device_id, tenantId=device['tenantId'])
        return {'registered': True, 'protocolVersion': PROTOCOL_VERSION}

    def heartbeat(self, device_id):
        runner = self._require_runner(device_id)
        runner['lastHeartbeat'] = self._now()
        runner['online'] = True
        return {'ok': True, 'at': _iso(runner['lastHeartbeat'])}

    def runner_status(self, device_id):
        runner = self.runners.get(device_id)
        device = self.devices.get(device_id)
        if device is None:
            return {
                'mode': 'loopback',
                'paired': False,
                'controlPlane': 'local',
                'protocolVersion': PROTOCOL_VERSION,
            }
        online = False
        if runner is not None:
            age = (self._now() - runner['lastHeartbeat']).total_seconds()
            online = (not device['revoked']) and age <= self._heartbeat_timeout
            runner['online'] = online
            if not online:
                self._audit('runner.heartbeat_lost', deviceId=device_id, tenantId=device['tenantId'])
        return omit_none({
            'mode': 'loopback',
            'paired': not device['revoked'],
            'controlPlane': 'mock-remote',
            'protocolVersion': PROTOCOL_VERSION,
            'deviceId': device_id,
            'tenantId': device['tenantId'],
            'userId': device['userId'],
            'runnerOnline': online,
            'revoked': device['revoked'],
        })

    def assign_job(self, job):
        self._require_bindings(job)
        device = self._require_device(job['deviceId'])
        if device['tenantId'] != job.get('tenantId', device['tenantId']):
            raise HostedRuntimeError('tenant_isolation', 'job tenant does not match the device', 403)
        if job.get('tenantId') and job['tenantId'] != device['tenantId']:
            raise HostedRuntimeError('tenant_isolation', 'job tenant does not match the device', 403)
        assignment_id = job.get('assignmentId') or _new_id('asg-')
        existing = self.jobs.get(assignment_id)
        if existing is not None:
            return dict(existing)
        self._rate_check(device['tenantId'], device['id'])
        if job.get('op') in FORBIDDEN_REMOTE_OPS:
            self._audit('job.rejected', assignmentId=assignment_id, reason='forbidden', deviceId=device['id'])
            raise HostedRuntimeError('forbidden_remote_op', 'operation is not allowed over remote transport', 403)
        stored = {
            'assignmentId': assignment_id,
            'status': 'assigned',
            'tenantId': device['tenantId'],
            'userId': job['userId'],
            'deviceId': job['deviceId'],
            'provider': job['provider'],
            'sessionId': job['sessionId'],
            'workspaceIdentity': job['workspaceIdentity'],
            'permissions': job['permissions'],
            'meetingId': job['meetingId'],
            'op': job.get('op', 'assign_meeting'),
        }
        self.jobs[assignment_id] = stored
        self.meetings[job['meetingId']] = {
            'id': job['meetingId'],
            'tenantId': device['tenantId'],
            'state': 'joining',
        }
        self._audit('job.assigned', assignmentId=assignment_id, meetingId=job['meetingId'], tenantId=device['tenantId'])
        return dict(stored)

    def cancel_job(self, assignment_id):
        job = self.jobs.get(assignment_id)
        if job is None:
            raise HostedRuntimeError('not_found', 'assignment is not available', 404)
        job['status'] = 'cancelled'
        self._audit('job.cancelled', assignmentId=assignment_id, tenantId=job['tenantId'])
        return dict(job)

    def upload_events(self, device_id, events, *, cursor=''):
        device = self._require_device(device_id)
        runner = self.runners.setdefault(device_id, {
            'deviceId': device_id, 'tenantId': device['tenantId'], 'online': True,
            'capabilities': dict(_RUNNER_CAPABILITIES), 'lastHeartbeat': self._now(), 'cursor': '',
        })
        accepted = []
        last = cursor or runner.get('cursor') or ''
        for event in events or []:
            filtered = filter_remote_event(event)
            if filtered is None:
                continue
            event_id = filtered.get('id')
            if event_id and event_id == last:
                continue
            bucket = self.events.setdefault(device['tenantId'], [])
            if event_id and any(item.get('id') == event_id for item in bucket):
                last = event_id
                continue
            bucket.append(filtered)
            accepted.append(filtered)
            if event_id:
                last = event_id
        runner['cursor'] = last
        return {'accepted': len(accepted), 'cursor': last}

    def reconnect(self, device_id, cursor=''):
        device = self._require_device(device_id)
        runner = self.runners.get(device_id)
        if runner is None:
            self.register_runner(device_id)
            runner = self.runners[device_id]
        runner['online'] = True
        runner['lastHeartbeat'] = self._now()
        stored = self.events.get(device['tenantId'], [])
        replay = []
        seen = not cursor
        for event in stored:
            event_id = event.get('id') or ''
            if not seen:
                if event_id == cursor:
                    seen = True
                continue
            replay.append(event)
        runner['cursor'] = cursor or runner.get('cursor') or ''
        self._audit('runner.reconnected', deviceId=device_id, tenantId=device['tenantId'])
        return {'replay': replay, 'cursor': runner['cursor'], 'online': True}

    def complete_handoff(self, assignment_id, handoff_id):
        job = self.jobs.get(assignment_id)
        if job is None:
            raise HostedRuntimeError('not_found', 'assignment is not available', 404)
        existing = self.handoffs.get(assignment_id)
        if existing is not None:
            return dict(existing)
        record = {
            'assignmentId': assignment_id,
            'handoffId': handoff_id,
            'meetingId': job['meetingId'],
            'tenantId': job['tenantId'],
            'status': 'appended',
        }
        self.handoffs[assignment_id] = record
        meeting = self.meetings.get(job['meetingId'])
        if meeting is not None:
            meeting['state'] = 'ended'
        job['status'] = 'completed'
        self._audit('handoff.appended', assignmentId=assignment_id, handoffId=handoff_id, tenantId=job['tenantId'])
        return dict(record)

    def capabilities(self):
        return {
            'protocolVersion': PROTOCOL_VERSION,
            'controlPlane': dict(_CONTROL_CAPABILITIES),
            'runner': dict(_RUNNER_CAPABILITIES),
            'messageKinds': list(MESSAGE_KINDS),
        }

    def _require_device(self, device_id):
        device = self.devices.get(device_id)
        if device is None:
            raise HostedRuntimeError('not_found', 'device is not paired', 404)
        if device['revoked']:
            raise HostedRuntimeError('device_revoked', 'device pairing was revoked', 403)
        return device

    def _require_runner(self, device_id):
        self._require_device(device_id)
        runner = self.runners.get(device_id)
        if runner is None:
            raise HostedRuntimeError('not_found', 'runner is not registered', 404)
        return runner

    def _require_bindings(self, job):
        if not isinstance(job, dict):
            raise HostedRuntimeError('job_binding_incomplete', 'job must be an object', 422)
        missing = [field for field in JOB_BINDING_FIELDS if not job.get(field)]
        if missing:
            raise HostedRuntimeError(
                'job_binding_incomplete',
                'remote jobs require user, device, session, workspace identity, permissions, and meeting',
                422,
            )
        identity = job['workspaceIdentity']
        if isinstance(identity, str) and (identity.startswith('/') or '\\' in identity or '..' in identity):
            raise HostedRuntimeError('forbidden_remote_op', 'filesystem path retrieval is forbidden', 403)

    def _rate_check(self, tenant_id, device_id):
        now = self._now()
        key = (tenant_id, device_id)
        stamps = [item for item in self._rate.get(key, []) if (now - item).total_seconds() < self._rate_window]
        if len(stamps) >= self._rate_limit:
            self._audit('rate_limited', tenantId=tenant_id, deviceId=device_id)
            raise HostedRuntimeError('rate_limited', 'too many remote jobs', 429)
        stamps.append(now)
        self._rate[key] = stamps


class LocalRunner:
    """Final enforcement point. Hosted requests may only narrow local permissions."""

    def __init__(self, plane, *, local_permissions, transport=None, device_id=None, enrollment=None):
        self.plane = plane
        self.local_permissions = local_permissions
        self.transport = transport if transport is not None else RunnerTransport()
        self.device_id = device_id
        self._enrollment = enrollment
        self._seen_assignments = {}
        self._handoffs = {}
        self._cursor = ''

    def accept_job(self, job):
        if job.get('op') in FORBIDDEN_REMOTE_OPS:
            raise HostedRuntimeError('forbidden_remote_op', 'operation is not allowed over remote transport', 403)
        missing = [field for field in JOB_BINDING_FIELDS if not job.get(field)]
        if missing:
            raise HostedRuntimeError('job_binding_incomplete', 'job binding is incomplete', 422)
        remote = job['permissions']
        if isinstance(remote, dict):
            remote = MeetingPermissions.from_dict(remote)
        narrowed = narrow_permissions(self.local_permissions, remote)
        assignment_id = job.get('assignmentId') or _new_id('asg-')
        existing = self._seen_assignments.get(assignment_id)
        if existing is not None:
            return dict(existing)
        accepted = {
            'assignmentId': assignment_id,
            'meetingId': job['meetingId'],
            'permissions': narrowed.to_dict(),
            'status': 'accepted',
            'workspaceIdentity': job['workspaceIdentity'],
        }
        self._seen_assignments[assignment_id] = accepted
        return dict(accepted)

    def upload_filtered_events(self, events):
        filtered = [item for item in (filter_remote_event(event) for event in events) if item]
        result = self.plane.upload_events(self.device_id, filtered, cursor=self._cursor)
        self._cursor = result['cursor']
        return result

    def reconnect(self):
        result = self.plane.reconnect(self.device_id, self._cursor)
        self._cursor = result.get('cursor') or self._cursor
        return result

    def append_handoff(self, assignment_id, handoff_id):
        existing = self._handoffs.get(assignment_id)
        if existing is not None:
            return dict(existing)
        record = self.plane.complete_handoff(assignment_id, handoff_id)
        self._handoffs[assignment_id] = record
        return dict(record)

    def send(self, payload):
        return self.transport.seal({**payload, 'protocolVersion': PROTOCOL_VERSION})

    def receive(self, envelope):
        return self.transport.open(envelope)


class HostedRuntime:
    """Daemon-facing facade. Default mode is local loopback with optional mock pairing."""

    def __init__(self, root, *, clock=None, plane=None, **limits):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.chmod(self.root, 0o700)
        except OSError:
            pass
        self._clock = clock or _utcnow
        self.plane = plane or InMemoryRemoteControlPlane(clock=self._clock, **{
            key: value for key, value in limits.items()
            if key in {'pairing_ttl', 'heartbeat_timeout', 'rate_limit', 'rate_window'}
        })
        self._state_path = self.root / 'runner-state.json'
        self._lock = threading.Lock()
        self._revealed_code = None
        self._load()

    def _now(self):
        moment = self._clock()
        if moment.tzinfo is None:
            raise ValueError('clock must return a timezone-aware datetime')
        return moment.astimezone(timezone.utc)

    def status(self):
        with self._lock:
            device_id = self._state.get('deviceId')
            if not device_id:
                payload = {
                    'mode': 'loopback',
                    'paired': False,
                    'controlPlane': 'local',
                    'protocolVersion': PROTOCOL_VERSION,
                    'capabilities': self.plane.capabilities(),
                }
                reject_secrets(payload, 'runner status')
                return payload
            payload = self.plane.runner_status(device_id)
            payload['capabilities'] = self.plane.capabilities()
            reject_secrets(payload, 'runner status')
            return payload

    def start_pair(self, *, tenant_id='ten-local', user_id='usr-local'):
        with self._lock:
            started = self.plane.start_pairing(tenant_id=tenant_id, user_id=user_id)
            self._revealed_code = started['pairingCode']
            self._state['pendingPairingId'] = started['pairingId']
            self._save()
            return started

    def complete_pair(self, pairing_id, pairing_code):
        with self._lock:
            completed = self.plane.complete_pairing(pairing_id, pairing_code)
            self.plane.register_runner(completed['deviceId'])
            self._state = {
                'deviceId': completed['deviceId'],
                'tenantId': completed['tenantId'],
                'userId': completed['userId'],
                'enrollmentHash': _hash(completed['deviceEnrollment']),
            }
            self._revealed_code = None
            self._save()
            return completed

    def unpair(self):
        with self._lock:
            device_id = self._state.get('deviceId')
            if device_id:
                self.plane.revoke_device(device_id)
            self._state = {}
            self._revealed_code = None
            if self._state_path.exists():
                self._state_path.unlink()
            return {'paired': False, 'mode': 'loopback', 'controlPlane': 'local'}

    def _load(self):
        self._state = {}
        if not self._state_path.is_file():
            return
        try:
            payload = json.loads(self._state_path.read_text())
        except (OSError, ValueError):
            return
        if isinstance(payload, dict) and 'deviceEnrollment' not in payload and 'pairingCode' not in payload:
            self._state = {
                'deviceId': payload.get('deviceId'),
                'tenantId': payload.get('tenantId'),
                'userId': payload.get('userId'),
                'enrollmentHash': payload.get('enrollmentHash'),
            }

    def _save(self):
        stored = omit_none({
            'deviceId': self._state.get('deviceId'),
            'tenantId': self._state.get('tenantId'),
            'userId': self._state.get('userId'),
            'enrollmentHash': self._state.get('enrollmentHash'),
        })
        reject_secrets(stored, 'runner state')
        temporary = self._state_path.with_suffix('.tmp')
        temporary.write_text(json.dumps(stored) + '\n')
        os.chmod(temporary, 0o600)
        temporary.replace(self._state_path)
        os.chmod(self._state_path, 0o600)
