"""Provider-isolated job files for Cursor/Claude host workers.

Codex keeps `jobs/` at the runtime root. Cursor and Claude Code use
`jobs/<provider>/` so heartbeats, locks, and session files cannot collide.
"""
import json
import os
import time
import uuid
from pathlib import Path

from runtime_state import ensure_private_dir
from session_continuity import CONTEXT, EXACT, continuity_from_payload


CLI_PROVIDERS = ('cursor', 'claude-code')


def jobs_root(path=None):
    return Path(path or os.environ.get('CODEX_JOBS_DIR', '/meeting-runtime/jobs'))


def provider_jobs_dir(provider_id, root=None, create=True):
    base = jobs_root(root)
    if provider_id in (None, 'codex', 'generic'):
        path = base
    elif provider_id not in CLI_PROVIDERS:
        raise ValueError('unknown coding agent provider')
    else:
        path = base / provider_id
    return ensure_private_dir(path) if create else Path(path)


def worker_lock_path(jobs_dir):
    return Path(jobs_dir) / 'worker.lock'


def heartbeat_path(jobs_dir):
    return Path(jobs_dir) / 'heartbeat'


def rewrite_private(path, body, *, mode=0o600):
    path = Path(path)
    ensure_private_dir(path.parent)
    tmp = path.with_name(path.name + '.tmp')
    tmp.unlink(missing_ok=True)
    payload = body if isinstance(body, (bytes, bytearray)) else str(body).encode('utf-8')
    tmp.write_bytes(payload)
    os.chmod(tmp, mode)
    os.replace(tmp, path)
    os.chmod(path, mode)
    return path


def write_heartbeat(jobs_dir):
    return rewrite_private(heartbeat_path(jobs_dir), str(time.time()))


class ProviderJobClient:
    """File-bus client used inside the meeting container for CLI providers."""

    def __init__(self, provider_id, jobs=None, timeout=180, session_id=None, continuity=None,
                 workspace=None, authorize_model=False, meeting_id=None, source=None):
        if provider_id not in CLI_PROVIDERS:
            raise ValueError('unknown coding agent provider')
        self.provider_id = provider_id
        self.jobs = Path(jobs) if jobs is not None else provider_jobs_dir(provider_id, create=False)
        self.timeout = timeout
        self.session_id = session_id
        self.continuity = continuity or CONTEXT
        self.workspace = workspace
        self.authorize_model = authorize_model
        self.meeting_id = meeting_id
        self.source = source

    def worker_connected(self):
        try:
            timestamp = float(heartbeat_path(self.jobs).read_text())
            return time.time() - timestamp < 5
        except (OSError, ValueError):
            return False

    def _fields(self, **overrides):
        payload = {
            'provider': self.provider_id,
            'session_id': self.session_id,
            'continuity': self.continuity,
            'workspace': self.workspace,
            'authorize_model': self.authorize_model,
            'meeting_id': self.meeting_id,
            'source': self.source,
        }
        for key, value in overrides.items():
            if key == 'authorize_model' or value is not None:
                payload[key] = value
        if payload.get('continuity') not in (EXACT, CONTEXT):
            payload['continuity'] = continuity_from_payload({
                'sessionId': payload.get('session_id'),
                'continuity': payload.get('continuity'),
                'metadata': {'source': payload.get('source') or ''},
            })
        return payload

    def _payload(self, op, task, model, extra=None, **fields):
        merged = self._fields(**fields)
        payload = {
            'op': op,
            'provider': self.provider_id,
            'task': task,
            'continuity': merged['continuity'],
            'authorize_model': bool(merged.get('authorize_model')),
        }
        if model:
            payload['model'] = model
        if merged.get('session_id'):
            payload['session_id'] = merged['session_id']
        if merged.get('workspace'):
            payload['workspace'] = merged['workspace']
        if merged.get('meeting_id'):
            payload['meeting_id'] = merged['meeting_id']
        if merged.get('source'):
            payload['source'] = merged['source']
        if extra:
            payload.update(extra)
        return payload

    def _write_request(self, job_id, payload):
        ensure_private_dir(self.jobs)
        request = self.jobs / f'{job_id}.request.json'
        rewrite_private(request, json.dumps(payload))
        return request

    def _signal_cancel(self, job_id):
        path = self.jobs / f'{job_id}.cancel'
        rewrite_private(path, '1')
        return path

    def _read_progress(self, job_id):
        path = self.jobs / f'{job_id}.progress.json'
        if not path.is_file():
            return None
        try:
            payload = json.loads(path.read_text())
        except (OSError, ValueError):
            return None
        if isinstance(payload, dict) and isinstance(payload.get('message'), str):
            return payload['message']
        return None

    async def _await_response(self, job_id, cancel=None, on_progress=None):
        import asyncio
        response = self.jobs / f'{job_id}.response.json'
        deadline = asyncio.get_running_loop().time() + self.timeout
        last_progress = None
        try:
            while asyncio.get_running_loop().time() < deadline:
                if cancel is not None and getattr(cancel, 'is_set', lambda: False)():
                    self._signal_cancel(job_id)
                    return {'error': 'cancelled'}
                message = self._read_progress(job_id)
                if message and message != last_progress and on_progress is not None:
                    last_progress = message
                    on_progress(message)
                if response.exists():
                    try:
                        result = json.loads(response.read_text())
                    except (OSError, ValueError):
                        return {'error': 'malformed_output', 'message': 'invalid worker response'}
                    if not isinstance(result, dict):
                        return {'error': 'malformed_output', 'message': 'invalid worker response'}
                    return result
                await asyncio.sleep(0.25)
            self._signal_cancel(job_id)
            return {'error': 'timeout'}
        finally:
            for suffix in ('.request.json', '.response.json', '.cancel', '.progress.json',
                           '.request.tmp'):
                (self.jobs / (job_id + suffix)).unlink(missing_ok=True)

    async def run(self, task, model=None, cancel=None, on_progress=None, **fields):
        if not isinstance(task, str) or not task.strip() or len(task) > 6000:
            return {'error': 'malformed_output', 'message': 'task must contain 1–6000 characters'}
        if not self.worker_connected():
            return {'error': 'missing_binary', 'message': 'coding agent worker is not connected'}
        job_id = uuid.uuid4().hex
        self._write_request(job_id, self._payload('run', task.strip(), model, **fields))
        return await self._await_response(job_id, cancel=cancel, on_progress=on_progress)

    async def append_handoff(self, handoff, cancel=None, **fields):
        if not self.worker_connected():
            return {'error': 'missing_binary', 'message': 'coding agent worker is not connected'}
        payload = handoff.to_dict() if hasattr(handoff, 'to_dict') else dict(handoff or {})
        meeting_id = fields.get('meeting_id') or payload.get('meetingId') or self.meeting_id
        extra = {'handoff': payload, 'handoff_id': fields.get('handoff_id') or meeting_id,
                 'meeting_id': meeting_id}
        job_id = uuid.uuid4().hex
        self._write_request(
            job_id,
            self._payload('append_handoff', 'append meeting handoff', fields.get('model'),
                          extra=extra, **fields),
        )
        return await self._await_response(job_id, cancel=cancel)

    async def validate_session(self, **fields):
        merged = self._fields(**fields)
        from session_continuity import validate_agent_session
        try:
            mode = validate_agent_session({
                'sessionId': merged.get('session_id'),
                'metadata': {
                    'continuity': merged.get('continuity') or '',
                    'source': merged.get('source') or '',
                },
            })
        except ValueError as error:
            return {'ok': False, 'error': str(error)}
        if not self.worker_connected():
            return {'ok': True, 'continuity': mode, 'session_id': merged.get('session_id')}
        job_id = uuid.uuid4().hex
        self._write_request(job_id, self._payload('validate_session', 'validate', None, **fields))
        return await self._await_response(job_id)
