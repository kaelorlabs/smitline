"""Bounded local job client for the host Codex CLI worker."""
import asyncio
import hashlib
import json
import os
import time
import uuid
from pathlib import Path


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
        'Ask a read-only Codex agent to analyze code, solve a technical problem, '
        'or produce an implementation plan. Choose gpt-6-astra for the hardest work, '
        'gpt-5.6-sol for strong general work, gpt-5.6-terra for balanced everyday work, '
        'gpt-5.6-luna for fast inexpensive work, or gpt-5.5 for compatibility.'
    ),
    'strict': True,
    'parameters': {
        'type': 'object',
        'properties': {
            'task': {
                'type': 'string',
                'description': 'A self-contained technical task. Include relevant meeting context but never credentials.',
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
    def __init__(self, jobs=None, timeout=180, session_key=None):
        self.jobs = Path(jobs or os.environ.get('CODEX_JOBS_DIR', '/zoom-live/jobs'))
        self.timeout = timeout
        meeting = os.environ.get('ZOOM_MEETING_URL', 'local-colleague-ai')
        self.session_key = session_key or hashlib.sha256(meeting.encode()).hexdigest()[:24]

    def worker_connected(self):
        try:
            timestamp = float((self.jobs / 'heartbeat').read_text())
            return time.time() - timestamp < 5
        except (OSError, ValueError):
            return False

    async def run(self, task, model):
        if not isinstance(task, str) or not task.strip() or len(task) > 6000:
            return {'error': 'task must contain 1–6000 characters'}
        if model not in CODEX_MODELS:
            return {'error': 'unsupported Codex model', 'available_models': list(CODEX_MODELS)}
        if not self.worker_connected():
            return {'error': 'Codex worker is not connected. Start the Zoom agent with start-zoom-live.sh.'}

        job_id = uuid.uuid4().hex
        request = self.jobs / f'{job_id}.request.json'
        response = self.jobs / f'{job_id}.response.json'
        temporary = self.jobs / f'{job_id}.request.tmp'
        self.jobs.mkdir(parents=True, exist_ok=True)
        temporary.write_text(json.dumps({'task': task.strip(), 'model': model,
                                         'session_key': self.session_key}))
        os.chmod(temporary, 0o600)
        os.replace(temporary, request)

        deadline = asyncio.get_running_loop().time() + self.timeout
        try:
            while asyncio.get_running_loop().time() < deadline:
                if response.exists():
                    try:
                        result = json.loads(response.read_text())
                    except (OSError, ValueError):
                        return {'error': 'Codex worker returned an invalid response'}
                    if not isinstance(result, dict):
                        return {'error': 'Codex worker returned an invalid response'}
                    return result
                await asyncio.sleep(0.25)
            return {'error': 'Codex agent timed out'}
        finally:
            request.unlink(missing_ok=True)
            response.unlink(missing_ok=True)
