"""Host-side MeetingSupervisor over the existing Docker meeting-agent."""
from pathlib import Path
import asyncio
import json
import os

from runtime_state import (
    active_meeting_path, context_index_from_handoff, context_index_path,
    meeting_state_path, read_json, state_from_session, write_private_json,
)

try:
    from runtime_daemon import DaemonError
except ImportError:
    class DaemonError(Exception):
        def __init__(self, status, code, message):
            super().__init__(message)
            self.status = status
            self.code = code
            self.message = message


COMPOSE_FILE = 'compose.meeting.yaml'
SERVICE = 'meeting-agent'
HEALTH_URL = 'http://127.0.0.1:8094/health'
STAGE_TO_STATE = {
    'starting': 'joining',
    'joining': 'joining',
    'admitted': 'joining',
    'connecting_audio': 'joining',
    'waiting_for_admission': 'waiting_for_admission',
    'live': 'live',
}
TERMINAL_STAGES = frozenset({
    'meeting_ended', 'finished', 'authentication_required', 'needs_attention',
    'api_error', 'closed_without_final_usage',
})


class CommandResult:
    def __init__(self, returncode=0, stdout='', stderr=''):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class SubprocessCommandRunner:
    """Runs host commands. Tests inject a fake instead of this class."""

    def __init__(self, cwd):
        self.cwd = str(cwd)

    async def run(self, args, *, env=None):
        merged = os.environ.copy()
        if env:
            merged.update({key: str(value) for key, value in env.items() if value is not None})
        process = await asyncio.create_subprocess_exec(
            *args,
            cwd=self.cwd,
            env=merged,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        return CommandResult(
            process.returncode,
            stdout.decode('utf-8', errors='replace'),
            stderr.decode('utf-8', errors='replace'),
        )


class UrlHealthClient:
    """Reads meeting-agent /health. Tests inject a fake instead of this class."""

    def __init__(self, url=HEALTH_URL, timeout=0.7):
        self.url = url
        self.timeout = timeout

    async def fetch(self):
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._get)

    def _get(self):
        from urllib.error import URLError
        from urllib.request import Request, urlopen
        try:
            with urlopen(Request(self.url), timeout=self.timeout) as response:
                return json.loads(response.read().decode('utf-8'))
        except (URLError, TimeoutError, json.JSONDecodeError, OSError, ValueError):
            return None


class ComposeMeetingAgent:
    """Single-container meeting-agent capacity at the current fixed ports."""

    def __init__(self, runner):
        self.runner = runner

    async def up(self, env):
        result = await self.runner.run(
            ['docker', 'compose', '-f', COMPOSE_FILE, 'up', '-d', '--build', SERVICE],
            env=env,
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or 'failed to start meeting-agent')
        return result

    async def stop(self):
        return await self.runner.run(
            ['docker', 'compose', '-f', COMPOSE_FILE, 'stop', SERVICE],
            env=None,
        )

    async def inspect(self):
        result = await self.runner.run(
            ['docker', 'compose', '-f', COMPOSE_FILE, 'ps', '-a', '--format', 'json'],
            env=None,
        )
        if result.returncode != 0:
            return {'running': False, 'unknown': True}
        text = result.stdout.strip()
        if not text:
            return {'running': False, 'unknown': False}
        running = False
        try:
            chunks = [text] if text.startswith('{') and '\n{' not in text else text.splitlines()
            if text.startswith('['):
                rows = json.loads(text)
            else:
                rows = [json.loads(chunk) for chunk in chunks if chunk.strip()]
        except (json.JSONDecodeError, ValueError):
            return {'running': False, 'unknown': True}
        for row in rows:
            service = row.get('Service') or row.get('Name') or ''
            if SERVICE not in str(service) and 'meeting-agent' not in str(row.get('Name', '')):
                continue
            state = str(row.get('State') or row.get('Status') or '').lower()
            if 'running' in state or state == 'up':
                running = True
        return {'running': running, 'unknown': False}


class ProductionMeetingSupervisor:
    """Connects RuntimeDaemon meetings to the existing Joinly meeting-agent.

    Command execution and health access are injectable. The supervisor never
    rewrites .env.meeting and never stores provider credentials in runtime
    state. One meeting-agent container (ports 6082/8094) is the capacity unit.
    """

    def __init__(
        self,
        project_root,
        *,
        runtime_root=None,
        launcher=None,
        health=None,
        runner=None,
        poll_interval=1.0,
        start_timeout=30.0,
        stop_timeout=25.0,
    ):
        self.project_root = Path(project_root).resolve()
        self.runtime_root = Path(runtime_root or (self.project_root / 'meeting-runtime')).resolve()
        self.runner = runner or SubprocessCommandRunner(self.project_root)
        self.launcher = launcher or ComposeMeetingAgent(self.runner)
        self.health = health or UrlHealthClient()
        self.poll_interval = poll_interval
        self.start_timeout = start_timeout
        self.stop_timeout = stop_timeout
        self.daemon = None
        self._lock = asyncio.Lock()
        self._active_id = None
        self._watch_task = None

    def bind_daemon(self, daemon):
        self.daemon = daemon
        return self

    def _container_env(self, session):
        state_path = meeting_state_path(self.runtime_root, session.id)
        return {
            'COLLEAGUE_RUNTIME_STATE': '/meeting-runtime/run/meetings/' + session.id + '/runtime.json',
            'MEETING_URL': session.meeting_url,
            'COLLEAGUE_HOST_RUNTIME_STATE': str(state_path),
        }

    def _write_session_files(self, session):
        payload = state_from_session(session)
        write_private_json(meeting_state_path(self.runtime_root, session.id), payload)
        index = context_index_from_handoff(session.context)
        write_private_json(context_index_path(self.runtime_root, session.id), index)
        write_private_json(context_index_path(self.runtime_root), index)
        write_private_json(active_meeting_path(self.runtime_root), {'meetingId': session.id})
        return payload

    async def start(self, session):
        async with self._lock:
            status = await self.launcher.inspect()
            if status.get('unknown'):
                raise DaemonError(503, 'retry_required', 'meeting-agent state is uncertain')
            if self._active_id == session.id and status.get('running'):
                return
            if status.get('running') or (self._active_id not in (None, session.id)):
                raise DaemonError(409, 'capacity_exceeded', 'meeting agent is already running')
            self._write_session_files(session)
            try:
                await self.launcher.up(self._container_env(session))
                await self._wait_until_running()
                await self._wait_until_health()
            except Exception:
                await self._stop_container()
                self._active_id = None
                raise
            self._active_id = session.id
            self._watch_task = asyncio.create_task(
                self._watch(session), name='meeting-agent-watch-' + session.id)

    async def add_context(self, meeting_id, context):
        async with self._lock:
            if self._active_id not in (None, meeting_id):
                raise DaemonError(409, 'conflict', 'context does not match the active meeting')
            path = meeting_state_path(self.runtime_root, meeting_id)
            payload = read_json(path) or {'version': 1, 'meetingId': meeting_id}
            payload['context'] = context.to_dict() if hasattr(context, 'to_dict') else context
            write_private_json(path, payload)
            index = context_index_from_handoff(context)
            write_private_json(context_index_path(self.runtime_root, meeting_id), index)
            write_private_json(context_index_path(self.runtime_root), index)

    async def cancel(self, meeting_id):
        async with self._lock:
            await self._cancel_locked(meeting_id)

    async def _cancel_locked(self, meeting_id):
        if self._active_id not in (None, meeting_id):
            active = read_json(active_meeting_path(self.runtime_root)) or {}
            if active.get('meetingId') not in (None, meeting_id):
                return
        await self._stop_watch()
        await self._await_stop()
        if self._active_id == meeting_id:
            self._active_id = None
        active = read_json(active_meeting_path(self.runtime_root)) or {}
        if active.get('meetingId') == meeting_id:
            write_private_json(active_meeting_path(self.runtime_root), {})

    async def reconcile(self, daemon=None):
        daemon = daemon or self.daemon
        if daemon is None:
            return
        status = await self.launcher.inspect()
        active = read_json(active_meeting_path(self.runtime_root)) or {}
        claimed = active.get('meetingId')
        if status.get('unknown'):
            return
        if status.get('running'):
            if not claimed:
                return
            record = daemon.meetings.get(claimed)
            if record is None or record.session.state == 'ended':
                return
            self._active_id = claimed
            if self._watch_task is None or self._watch_task.done():
                self._watch_task = asyncio.create_task(
                    self._watch(record.session), name='meeting-agent-watch-' + claimed)
            return
        for meeting_id in daemon.meetings.list_ids():
            record = daemon.meetings.get(meeting_id)
            if record is None or record.session.state == 'ended':
                continue
            if record.lease_token is None and daemon.leases.get(
                    record.session.agent_session.provider,
                    record.session.agent_session.session_id) is None:
                continue
            daemon.record_finalization_failure(meeting_id, 'runtime_unavailable_on_restart')

    async def _wait_until_running(self):
        deadline = asyncio.get_running_loop().time() + self.start_timeout
        while asyncio.get_running_loop().time() < deadline:
            status = await self.launcher.inspect()
            if status.get('unknown'):
                raise DaemonError(503, 'retry_required', 'meeting-agent state is uncertain')
            if status.get('running'):
                return
            await asyncio.sleep(min(self.poll_interval, 0.05))
        raise RuntimeError('meeting-agent did not become running')

    async def _wait_until_health(self):
        deadline = asyncio.get_running_loop().time() + self.start_timeout
        while asyncio.get_running_loop().time() < deadline:
            health = await self.health.fetch()
            if health:
                return health
            status = await self.launcher.inspect()
            if status.get('unknown'):
                raise DaemonError(503, 'retry_required', 'meeting-agent state is uncertain')
            if not status.get('running'):
                raise RuntimeError('meeting-agent exited during startup')
            await asyncio.sleep(min(self.poll_interval, 0.05))
        raise RuntimeError('meeting-agent health was not reachable')

    async def _stop_container(self):
        try:
            await self.launcher.stop()
        except Exception:
            pass

    async def _await_stop(self):
        await self._stop_container()
        deadline = asyncio.get_running_loop().time() + self.stop_timeout
        while asyncio.get_running_loop().time() < deadline:
            status = await self.launcher.inspect()
            if status.get('unknown'):
                return
            if not status.get('running'):
                return
            await asyncio.sleep(min(self.poll_interval, 0.05))
        raise DaemonError(503, 'retry_required', 'meeting-agent did not stop')

    async def _stop_watch(self):
        task = self._watch_task
        self._watch_task = None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass

    async def _watch(self, session):
        try:
            while True:
                await asyncio.sleep(self.poll_interval)
                status = await self.launcher.inspect()
                if status.get('unknown'):
                    continue
                if not status.get('running'):
                    await self._finalize_without_handoff(session, 'container_exited')
                    return
                health = await self.health.fetch()
                if not health:
                    continue
                terminal = await self._apply_health(session, health)
                self._heartbeat(session)
                if terminal:
                    return
        except asyncio.CancelledError:
            raise

    async def _apply_health(self, session, health):
        stage = health.get('stage')
        mapped = STAGE_TO_STATE.get(stage)
        if mapped and self.daemon is not None:
            try:
                self.daemon.transition(session.id, mapped)
            except DaemonError:
                pass
        if stage in TERMINAL_STAGES:
            await self._finalize_without_handoff(session, 'runtime_ended_before_handoff')
            return True
        return False

    def _heartbeat(self, session):
        if self.daemon is None:
            return
        agent = session.agent_session
        try:
            self.daemon.heartbeat_lease(agent.provider, agent.session_id)
        except DaemonError:
            pass

    def _handoff_ready(self, meeting_id):
        if self.daemon is None:
            return False
        try:
            record = self.daemon.meetings.get(meeting_id)
        except Exception:
            return False
        return record is not None and record.handoff is not None

    async def _finalize_without_handoff(self, session, reason):
        if self._handoff_ready(session.id):
            self._active_id = None
            return
        if self.daemon is not None:
            try:
                self.daemon.record_finalization_failure(session.id, reason)
            except DaemonError:
                pass
        self._active_id = None
        await self._stop_container()
