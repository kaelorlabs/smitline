"""Host worker for Cursor and Claude Code jobs. Codex stays on codex_worker.py."""
import asyncio
import fcntl
import json
import os
import threading
import time
from pathlib import Path

from provider_jobs import CLI_PROVIDERS, provider_jobs_dir, rewrite_private, worker_lock_path, write_heartbeat
from providers.base import ProviderRequest
from providers.registry import ProviderRegistry
from runtime_state import ensure_private_dir
from session_continuity import CONTEXT


def _provider_id():
    value = (os.environ.get('COLLEAGUE_PROVIDER') or '').strip()
    if value not in CLI_PROVIDERS:
        raise SystemExit('COLLEAGUE_PROVIDER must be cursor or claude-code')
    return value


def _jobs_dir(provider_id):
    configured = os.environ.get('PROVIDER_JOBS_DIR')
    return provider_jobs_dir(provider_id, configured) if not configured else ensure_private_dir(configured)


def _make_provider(provider_id):
    registry = ProviderRegistry()
    adapter = registry.get(provider_id)
    if adapter is None:
        raise SystemExit('unknown coding agent provider')
    adapter.client = None
    binary = adapter._resolve_binary()
    if not binary:
        raise SystemExit(provider_id + ' CLI not found')
    adapter.command = binary
    adapter._binary = binary
    return adapter


def _request(data, job_id):
    return ProviderRequest(
        delegation_id=job_id,
        request_text=data.get('task') or '',
        workspace=data.get('workspace') or os.environ.get('COLLEAGUE_WORKSPACE'),
        provider=data.get('provider'),
        session_id=data.get('session_id'),
        continuity=data.get('continuity') or CONTEXT,
        authorize_model=bool(data.get('authorize_model')),
        meeting_id=data.get('meeting_id'),
        model=data.get('model'),
        source=data.get('source'),
    )


def _cancel_watch(cancel_path, event):
    path = Path(cancel_path) if cancel_path else None
    while not event.is_set():
        if path is not None and path.is_file():
            event.set()
            return
        time.sleep(0.05)


def handle_job(provider, data, job_id, cancel_path=None, jobs_dir=None):
    from codex_worker import exclusive_session_lock
    op = data.get('op') or 'run'
    incoming = data.get('provider')
    if incoming and incoming != provider.provider_id:
        return {'error': 'unknown_provider', 'message': 'job provider does not match this worker'}
    cancel = threading.Event()
    watcher = threading.Thread(
        target=_cancel_watch, args=(cancel_path, cancel), daemon=True)
    watcher.start()
    lock_key = provider.provider_id + ':' + str(data.get('session_id') or job_id)
    try:
        with exclusive_session_lock(Path(jobs_dir or '.'), lock_key):
            if op == 'validate_session':
                return asyncio.run(provider.validate_session({
                    'sessionId': data.get('session_id'),
                    'metadata': {
                        'continuity': data.get('continuity') or '',
                        'source': data.get('source') or '',
                    },
                }))
            request = _request(data, job_id)
            if op == 'append_handoff':
                return asyncio.run(provider.append_handoff(request, data.get('handoff') or {}, cancel))
            if op not in ('run', None):
                return {'error': 'unsupported', 'message': 'unsupported provider job op'}
            return asyncio.run(provider.execute_prompt(request, data.get('task') or '', cancel))
    except BlockingIOError:
        return {'error': 'conflict', 'message': 'originating session is already in use'}
    finally:
        cancel.set()


def _write_response(jobs, job_id, result):
    rewrite_private(jobs / (job_id + '.response.json'), json.dumps(
        result if isinstance(result, dict) else {'error': 'malformed_output'}))


def main():
    from codex_worker import BridgeLiveness, bridge_reachable
    provider_id = _provider_id()
    jobs = _jobs_dir(provider_id)
    provider = _make_provider(provider_id)
    lock = worker_lock_path(jobs).open('w')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit('Another ' + provider_id + ' worker is already running.')
    lock.write(str(os.getpid()))
    lock.flush()
    print('Meeting ' + provider_id + ' worker ready', flush=True)
    liveness = BridgeLiveness()
    next_liveness_check = 0
    while True:
        now = time.monotonic()
        if now >= next_liveness_check:
            if not liveness.update(bridge_reachable(), now):
                print('Meeting runtime stopped; ' + provider_id + ' worker exiting', flush=True)
                return
            next_liveness_check = now + 2
        write_heartbeat(jobs)
        for request in sorted(jobs.glob('*.request.json')):
            job_id = request.name.replace('.request.json', '')
            response = jobs / (job_id + '.response.json')
            if response.exists():
                continue
            cancel_path = jobs / (job_id + '.cancel')
            try:
                data = json.loads(request.read_text())
                if not isinstance(data, dict):
                    raise ValueError('Invalid job payload')
                result = handle_job(provider, data, job_id, cancel_path=cancel_path, jobs_dir=jobs)
            except Exception:
                result = {'error': 'malformed_output', 'message': provider_id + ' worker failed'}
            write_heartbeat(jobs)
            _write_response(jobs, job_id, result)
        time.sleep(0.25)


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print('Meeting provider worker stopped', flush=True)
