"""Host-side MeetingSupervisor over the Docker meeting-agent."""
from pathlib import Path
import asyncio
import json
import os

from meeting_finalizer import MeetingFinalizer, archive_dir, load_finalization
from runtime_state import (
    active_meeting_path, ensure_private_dir, meeting_state_path, read_json, state_from_session,
    write_private_json,
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
# Bind-mount sources the container writes; Docker would create missing ones as root.
MOUNTED_DIRS = ('recordings', 'profiles')
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
COMPLETE_REASONS = frozenset({'finished', 'meeting_ended'})


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


def host_user_env(environ=None):
    """The uid and gid compose.meeting.yaml runs the container as: the host user's own.

    Runtime files are private to the host user, so the container must share its uid.
    Values already set (for example 0:0 for rootless Docker) are kept.
    """
    env = os.environ if environ is None else environ
    ids = {}
    for key, lookup in (('COLLEAGUE_UID', 'getuid'), ('COLLEAGUE_GID', 'getgid')):
        value = str(env.get(key) or '').strip()
        if not value and hasattr(os, lookup):
            value = str(getattr(os, lookup)())
        if value:
            ids[key] = value
    return ids


class ComposeMeetingAgent:
    """Single-container meeting-agent capacity at the current fixed ports."""

    def __init__(self, runner, environ=None):
        self.runner = runner
        self.environ = environ

    async def up(self, env):
        """Start the meeting container; the first start builds its image."""
        result = await self.runner.run(
            ['docker', 'compose', '-f', COMPOSE_FILE, 'up', '-d', '--build', SERVICE],
            env={**host_user_env(self.environ), **(env or {})},
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
    """Connects RuntimeDaemon meetings to the Docker meeting-agent.

    Command execution and health access are injectable. The supervisor never
    rewrites .env.meeting and never stores credentials in runtime state. One
    meeting-agent container (ports 6082/8094) is the capacity unit.
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
        self._active_session = None
        self._watch_task = None
        self._finalize_inflight = {}

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

    def _prepare_mounts(self):
        for name in MOUNTED_DIRS:
            path = self.runtime_root / name
            try:
                ensure_private_dir(path)
            except PermissionError as error:
                raise RuntimeError(
                    f'{path} belongs to another user, so the meeting container cannot use it. '
                    f'Run: sudo chown -R "$(id -u):$(id -g)" {path}') from error

    def _write_session_files(self, session, camera_settings=None):
        payload = state_from_session(session)
        if camera_settings:
            if 'cameraEnabled' in camera_settings:
                payload['cameraEnabled'] = bool(camera_settings['cameraEnabled'])
            if 'cameraDefaultOn' in camera_settings:
                payload['cameraDefaultOn'] = bool(camera_settings['cameraDefaultOn'])
            avatar = camera_settings.get('cameraAvatarDataUri')
            if avatar:
                payload['cameraAvatarDataUri'] = avatar
            payload.pop('cameraAvatarPath', None)
        write_private_json(meeting_state_path(self.runtime_root, session.id), payload)
        write_private_json(active_meeting_path(self.project_root), {'meetingId': session.id})
        return payload

    async def start(self, session, camera_settings=None):
        async with self._lock:
            status = await self.launcher.inspect()
            if status.get('unknown'):
                raise DaemonError(503, 'retry_required', 'meeting-agent state is uncertain')
            if self._active_id == session.id and status.get('running'):
                return
            if status.get('running') or (self._active_id not in (None, session.id)):
                raise DaemonError(409, 'capacity_exceeded', 'meeting agent is already running')
            self._write_session_files(session, camera_settings=camera_settings)
            try:
                self._prepare_mounts()
                await self.launcher.up(self._container_env(session))
                await self._wait_until_running()
                await self._wait_until_health()
            except Exception:
                await self._stop_container()
                self._active_id = None
                self._active_session = None
                raise
            self._active_id = session.id
            self._active_session = session
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

    async def cancel(self, meeting_id):
        async with self._lock:
            await self._cancel_locked(meeting_id)

    async def _cancel_locked(self, meeting_id):
        if self._active_id not in (None, meeting_id):
            await self._complete_handoff_for(meeting_id, 'cancelled', partial=True)
            return
        await self._stop_watch()
        await self._await_stop()
        await self._complete_handoff_for(meeting_id, 'cancelled', partial=True)
        if self._active_id == meeting_id:
            self._active_id = None
            self._active_session = None
        active = read_json(active_meeting_path(self.project_root)) or {}
        if active.get('meetingId') == meeting_id:
            write_private_json(active_meeting_path(self.project_root), {})

    async def reconcile(self, daemon=None):
        daemon = daemon or self.daemon
        if daemon is None:
            return
        status = await self.launcher.inspect()
        active = read_json(active_meeting_path(self.project_root)) or {}
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
            self._active_session = record.session
            if self._watch_task is None or self._watch_task.done():
                self._watch_task = asyncio.create_task(
                    self._watch(record.session), name='meeting-agent-watch-' + claimed)
            return
        for meeting_id in daemon.meetings.list_ids():
            try:
                record = daemon.meetings.get(meeting_id)
            except Exception:
                continue
            if record is None or record.handoff is not None:
                continue
            local = {}
            try:
                local = load_finalization(archive_dir(self.runtime_root, meeting_id))
            except Exception:
                local = {}
            pending = local.get('status') == 'local'
            # An ended meeting with no pending local handoff has nothing left to finish.
            if record.session.state == 'ended' and not pending:
                continue
            await self._complete_handoff(
                record.session,
                local.get('endReason') or 'runtime_unavailable_on_restart',
                partial=True,
            )

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
                    await self._complete_handoff(session, 'container_exited', partial=True)
                    return
                health = await self.health.fetch()
                if not health:
                    continue
                if await self._apply_health(session, health):
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
        if self.daemon is not None and hasattr(self.daemon, 'apply_presence'):
            from visual_presence import map_visual_state
            fields = {}
            for key in ('cameraEnabled', 'cameraState', 'visualState', 'degradedReason'):
                if key in health:
                    fields[key] = health[key]
            if 'visualState' not in fields:
                fields['visualState'] = map_visual_state(health)
            try:
                self.daemon.apply_presence(session.id, **fields)
            except Exception:
                pass
        if stage in TERMINAL_STAGES:
            await self._complete_handoff(
                session, stage, partial=stage not in COMPLETE_REASONS)
            return True
        return False

    def _handoff_ready(self, meeting_id):
        if self.daemon is None:
            return False
        try:
            record = self.daemon.meetings.get(meeting_id)
        except Exception:
            return False
        return record is not None and record.handoff is not None

    def _session_for(self, meeting_id):
        if self._active_session is not None and self._active_session.id == meeting_id:
            return self._active_session
        if self.daemon is None:
            return None
        try:
            record = self.daemon.meetings.get(meeting_id)
        except Exception:
            return None
        return None if record is None else record.session

    async def _complete_handoff_for(self, meeting_id, reason, *, partial):
        session = self._session_for(meeting_id)
        if session is None:
            return {'status': 'skipped'}
        return await self._complete_handoff(session, reason, partial=partial)

    async def _complete_handoff(self, session, reason, *, partial):
        meeting_id = session.id
        existing = self._finalize_inflight.get(meeting_id)
        if existing is not None:
            return await existing
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        self._finalize_inflight[meeting_id] = future
        try:
            result = await self._complete_handoff_body(session, reason, partial=partial)
            if not future.done():
                future.set_result(result)
            return result
        except asyncio.CancelledError:
            if not future.done():
                future.cancel()
            raise
        except Exception as error:
            if not future.done():
                future.set_exception(error)
            raise
        finally:
            if self._finalize_inflight.get(meeting_id) is future:
                self._finalize_inflight.pop(meeting_id, None)

    async def _complete_handoff_body(self, session, reason, *, partial):
        if self._handoff_ready(session.id):
            try:
                await self._stop_container()
            finally:
                self._release_active_claim(session.id)
            return {'status': 'ready', 'idempotent': True}
        finalizer = MeetingFinalizer(self.runtime_root, daemon=self.daemon)
        try:
            try:
                return await finalizer.complete(session, reason=reason, partial=partial)
            except DaemonError as error:
                return {'status': 'error', 'error': error.message}
        finally:
            try:
                await self._stop_container()
            finally:
                # A meeting whose handoff failed is not live and must never
                # keep the meeting-agent capacity.
                self._release_active_claim(session.id)

    def _release_active_claim(self, meeting_id):
        if self._active_id == meeting_id:
            self._active_id = None
            self._active_session = None
        active = read_json(active_meeting_path(self.project_root)) or {}
        if active.get('meetingId') == meeting_id:
            write_private_json(active_meeting_path(self.project_root), {})

    async def shutdown(self):
        await self._stop_watch()
