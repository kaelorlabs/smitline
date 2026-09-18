"""Bounded local job client for the host Codex CLI worker."""
import asyncio
import json
import os
import time
import uuid
from pathlib import Path

from session_continuity import CONTEXT, EXACT, continuity_from_payload


CODEX_MODELS = (
    'gpt-6-astra',
    'gpt-5.6-sol',
    'gpt-5.6-terra',
    'gpt-5.6-luna',
    'gpt-5.5',
)

CODEX_TOOL = {
    'type': 'function',
    'name': 'run_codex',
    'description': (
        'Ask a read-only Codex agent to inspect the configured workspace and supplied meeting context, analyze code or documents, '
        'query structured data, perform calculations, solve technical problems, or develop a plan. '
        'Choose gpt-6-astra for the hardest work, '
        'gpt-5.6-sol for strong general work, gpt-5.6-terra for balanced everyday work, '
        'gpt-5.6-luna for fast inexpensive work, or gpt-5.5 for compatibility.'
    ),
    'strict': True,
    'parameters': {
        'type': 'object',
        'properties': {
            'task': {
                'type': 'string',
                'description': 'A self-contained task with relevant meeting context and desired output, but never credentials.',
            },
            'model': {
                'type': 'string',
                'enum': list(CODEX_MODELS),
                'description': 'The Codex model to use for this run.',
            },
        },
        'required': ['task', 'model'],
        'additionalProperties': False,
    },
}


class CodexJobClient:
    def __init__(self, jobs=None, timeout=180, session_id=None, continuity=None, workspace=None,
                 authorize_model=False, meeting_id=None, source=None):
        self.jobs = Path(jobs or os.environ.get('CODEX_JOBS_DIR', '/meeting-runtime/jobs'))
        self.timeout = timeout
        self.session_id = session_id
        self.continuity = continuity or CONTEXT
        self.workspace = workspace
        self.authorize_model = authorize_model
        self.meeting_id = meeting_id
        self.source = source

    def worker_connected(self):
        try:
            timestamp = float((self.jobs / 'heartbeat').read_text())
            return time.time() - timestamp < 5
        except (OSError, ValueError):
            return False

    def _fields(self, **overrides):
        payload = {
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
            'task': task,
            'model': model,
            'continuity': merged['continuity'],
            'authorize_model': bool(merged.get('authorize_model')),
        }
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
        request = self.jobs / f'{job_id}.request.json'
        temporary = self.jobs / f'{job_id}.request.tmp'
        self.jobs.mkdir(parents=True, exist_ok=True)
        temporary.write_text(json.dumps(payload))
        os.chmod(temporary, 0o600)
        os.replace(temporary, request)
        return request

    def _signal_cancel(self, job_id):
        path = self.jobs / f'{job_id}.cancel'
        path.write_text('1')
        os.chmod(path, 0o600)
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

    def _cleanup(self, job_id):
        for suffix in ('.request.json', '.response.json', '.cancel', '.progress.json',
                       '.answer.txt', '.request.tmp'):
            (self.jobs / (job_id + suffix)).unlink(missing_ok=True)

    async def _await_response(self, job_id, cancel=None, on_progress=None):
        request = self.jobs / f'{job_id}.request.json'
        response = self.jobs / f'{job_id}.response.json'
        deadline = asyncio.get_running_loop().time() + self.timeout
        last_progress = None
        try:
            while asyncio.get_running_loop().time() < deadline:
                if cancel is not None and cancel.is_set():
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
                        return {'error': 'Codex worker returned an invalid response'}
                    if not isinstance(result, dict):
                        return {'error': 'Codex worker returned an invalid response'}
                    return result
                await asyncio.sleep(0.25)
            self._signal_cancel(job_id)
            return {'error': 'Codex agent timed out'}
        finally:
            request.unlink(missing_ok=True)
            response.unlink(missing_ok=True)
            (self.jobs / f'{job_id}.cancel').unlink(missing_ok=True)
            (self.jobs / f'{job_id}.progress.json').unlink(missing_ok=True)

    async def run(self, task, model, cancel=None, on_progress=None, **fields):
        if not isinstance(task, str) or not task.strip() or len(task) > 6000:
            return {'error': 'task must contain 1–6000 characters'}
        if model not in CODEX_MODELS:
            return {'error': 'unsupported Codex model', 'available_models': list(CODEX_MODELS)}
        if not self.worker_connected():
            return {'error': 'Codex worker is not connected. Start the meeting agent with start-meeting-agent.sh.'}
        job_id = uuid.uuid4().hex
        self._write_request(job_id, self._payload('run', task.strip(), model, **fields))
        return await self._await_response(job_id, cancel=cancel, on_progress=on_progress)

    async def append_handoff(self, handoff, cancel=None, **fields):
        if not self.worker_connected():
            return {'error': 'Codex worker is not connected. Start the meeting agent with start-meeting-agent.sh.'}
        payload = handoff.to_dict() if hasattr(handoff, 'to_dict') else dict(handoff or {})
        meeting_id = fields.get('meeting_id') or payload.get('meetingId') or self.meeting_id
        handoff_id = fields.get('handoff_id') or payload.get('handoffId') or meeting_id
        extra = {'handoff': payload, 'handoff_id': handoff_id, 'meeting_id': meeting_id}
        job_id = uuid.uuid4().hex
        model = fields.get('model') or 'gpt-5.6-terra'
        self._write_request(
            job_id,
            self._payload('append_handoff', 'append meeting handoff', model, extra=extra, **fields),
        )
        return await self._await_response(job_id, cancel=cancel)

    async def validate_session(self, **fields):
        if not self.worker_connected():
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
            return {'ok': True, 'continuity': mode, 'session_id': merged.get('session_id')}
        job_id = uuid.uuid4().hex
        self._write_request(job_id, self._payload('validate_session', 'validate', 'gpt-5.6-terra', **fields))
        return await self._await_response(job_id)
