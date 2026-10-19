"""Host-side MeetingSupervisor over the Docker meeting-agent."""
from pathlib import Path
import asyncio
import json
import os

from meeting_finalizer import MeetingFinalizer, archive_dir, load_finalization
from runtime_state import (
    POOL_ACTIVE_DIR, active_meeting_path, control_root, ensure_private_dir, meeting_state_path,
    pool_claim_path, read_json, state_from_session, write_private_file, write_private_json,
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
IMAGE_COMPOSE_FILE = 'compose.meeting.image.yaml'
# Several meetings at once (SMITLINE_MAX_MEETINGS above 1): one compose project per meeting.
POOL_COMPOSE_FILE = 'compose.meeting.pool.yaml'
POOL_IMAGE_COMPOSE_FILE = 'compose.meeting.pool.image.yaml'
POOL_PROJECT_PREFIX = 'smitline-meeting-'
# Host ports the pool gives meetings, two each (noVNC, then /health), on 127.0.0.1.
DEFAULT_POOL_PORTS = (18100, 18399)
# One meeting container at a time uses the installation's signed-in browser profiles and sees
# the whole data volume, so it runs the local owner's meetings only.
MEETING_POOL_REQUIRED = ('Meetings for other owners need the meeting pool: set SMITLINE_MAX_MEETINGS '
                         'to 2 or more.')
SERVICE = 'meeting-agent'
CODE_ROOT = Path(__file__).resolve().parent.parent
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


def owner_env_file(values):
    """KEY="value" lines for compose's env_file. Inside double quotes compose reads a
    backslash before a backslash, a quote, or a dollar sign as that character, so a value
    arrives exactly as given (O'Neil, $5). Every key is written: a value with a control
    character, or none, is blank, so the shared .env read before this file never fills in
    for the owner."""
    lines = []
    for key, value in values.items():
        value = str(value or '')
        if any(ord(char) < 32 or ord(char) == 127 for char in value):
            value = ''
        quoted = value.replace('\\', '\\\\').replace('"', '\\"').replace('$', '\\$')
        lines.append(f'{key}="{quoted}"')
    return ''.join(line + '\n' for line in lines)


def participant_name(value):
    """A name the meeting container accepts (1 to 80 printable characters), else None."""
    name = str(value or '').strip()
    if not name or len(name) > 80 or any(ord(char) < 32 for char in name):
        return None
    return name


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
        try:
            process = await asyncio.create_subprocess_exec(
                *args,
                cwd=self.cwd,
                env=merged,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            # No Docker on this computer: phone calls still work, meetings report the failure.
            return CommandResult(127, '', str(exc))
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
    for key, lookup in (('SMITLINE_UID', 'getuid'), ('SMITLINE_GID', 'getgid')):
        value = str(env.get(key) or '').strip()
        if not value and hasattr(os, lookup):
            value = str(getattr(os, lookup)())
        if value:
            ids[key] = value
    return ids


def published_image(environ=None):
    env = os.environ if environ is None else environ
    return bool(str(env.get('SMITLINE_MEETING_IMAGE') or '').strip())


def compose_command(environ=None, *, project=None):
    """The docker compose command for the meeting container, and how `up` gets its image.

    The published image sets SMITLINE_MEETING_IMAGE: the meeting image is pulled and uses the
    shared data volume. From a checkout it is built, with the code mounted. With a project
    name, it is one meeting of several, in the pool's compose file.
    """
    image = published_image(environ)
    if project is not None:
        name = POOL_IMAGE_COMPOSE_FILE if image else POOL_COMPOSE_FILE
        compose = ['docker', 'compose', '-p', project,
                   '-f', str(CODE_ROOT / name) if image else name]
    elif image:
        compose = ['docker', 'compose', '-f', str(CODE_ROOT / IMAGE_COMPOSE_FILE)]
    else:
        compose = ['docker', 'compose', '-f', COMPOSE_FILE]
    return compose, (['--pull', 'missing'] if image else ['--build'])


class ComposeMeetingAgent:
    """One meeting-agent container: the single one at the fixed ports, or, with a project
    name, one meeting of the pool, whose compose file needs `env` (its ports and folders) for
    every command. A pool meeting's container is removed when it stops."""

    def __init__(self, runner, environ=None, *, project=None, env=None):
        self.runner = runner
        self.environ = environ
        self.project = project
        self.env = dict(env or {})

    def _command(self):
        return compose_command(self.environ, project=self.project)

    async def up(self, env):
        """Start the meeting container, pulling or building its image the first time."""
        compose, get_image = self._command()
        result = await self.runner.run(
            [*compose, 'up', '-d', *get_image, SERVICE],
            env={**host_user_env(self.environ), **self.env, **(env or {})},
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or 'failed to start meeting-agent')
        return result

    async def stop(self):
        compose, _ = self._command()
        if self.project is None:
            return await self.runner.run([*compose, 'stop', SERVICE], env=None)
        return await self.runner.run([*compose, 'down', '--remove-orphans'],
                                     env={**host_user_env(self.environ), **self.env})

    async def inspect(self):
        compose, _ = self._command()
        result = await self.runner.run([*compose, 'ps', '-a', '--format', 'json'],
                                       env={**host_user_env(self.environ), **self.env}
                                       if self.project else None)
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
    meeting-agent container (ports 6082/8094) is the capacity unit; MeetingPool
    runs several, each with a supervisor of its own.
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
        owner_environment=None,
        claim=None,
        mounts=(),
    ):
        self.project_root = Path(project_root).resolve()
        # claim: a pool meeting's own claim file and what it records (owner, project, ports),
        # removed when the meeting ends; otherwise the single .smitline/active-meeting.json.
        self._claim = claim
        # mounts: more folders the container mounts, made private before it starts.
        self._mounts = tuple(mounts)
        self.runtime_root = Path(runtime_root or (self.project_root / 'meeting-runtime')).resolve()
        self.runner = runner or SubprocessCommandRunner(self.project_root)
        self.launcher = launcher or ComposeMeetingAgent(self.runner)
        self.health = health or UrlHealthClient()
        self.poll_interval = poll_interval
        self.start_timeout = start_timeout
        self.stop_timeout = stop_timeout
        # owner_environment(owner): a pool meeting's whole environment besides HOME and its
        # runtime state (meeting_line.pool_meeting_environment), written to a private env file
        # that the pool's compose files read instead of .env. One meeting container at a time
        # runs only the local owner's meetings, which read .env as always.
        self.owner_environment = owner_environment
        self.daemon = None
        self._lock = asyncio.Lock()
        self._active_id = None
        self._active_session = None
        self._watch_task = None
        self._finalize_inflight = {}

    def bind_daemon(self, daemon):
        self.daemon = daemon
        return self

    @property
    def local_only(self):
        """One meeting container at a time (not one meeting of a pool): the installation's
        signed-in browser profiles and the whole data volume, so local meetings only."""
        return self._claim is None

    def capacity_problem(self, owner):
        """Why a meeting for `owner` cannot start now, as (status, code, message), else None.
        Asked when a meeting call is placed; start() still decides, for meetings that race."""
        if self.local_only and owner != 'local':
            return 409, 'meeting_pool_required', MEETING_POOL_REQUIRED
        if self._active_id is not None:
            return 409, 'capacity_exceeded', 'meeting agent is already running'
        return None

    def _container_env(self, session, owner_env=None):
        state_path = meeting_state_path(self.runtime_root, session.id)
        env = {
            'SMITLINE_RUNTIME_STATE': '/meeting-runtime/run/meetings/' + session.id + '/runtime.json',
            'MEETING_URL': session.meeting_url,
            'SMITLINE_HOST_RUNTIME_STATE': str(state_path),
        }
        if owner_env is not None:
            # A pool meeting's compose file reads only this file for the container's settings.
            env['SMITLINE_MEETING_ENV_FILE'] = str(owner_env)
        return env

    def _owner_values(self, session):
        """A pool meeting's environment (every owner's, the local one's too); nothing for one
        container at a time, whose local meetings read .env as always."""
        if self.owner_environment is None or self.local_only:
            return {}
        return dict(self.owner_environment(getattr(session, 'owner', 'local')) or {})

    def _write_owner_env(self, session, values=None):
        """The private env file of a pool meeting, or None."""
        body = owner_env_file(self._owner_values(session) if values is None else values)
        if not body:
            return None
        path = meeting_state_path(self.runtime_root, session.id).with_name('owner.env')
        write_private_file(path, body)
        return path

    def _claim_path(self):
        return self._claim['path'] if self._claim else active_meeting_path(self.project_root)

    def _claimed_id(self):
        return (read_json(self._claim_path()) or {}).get('meetingId')

    def _prepare_mounts(self):
        for path in [self.runtime_root / name for name in MOUNTED_DIRS] + list(self._mounts):
            try:
                ensure_private_dir(path)
            except PermissionError as error:
                raise RuntimeError(
                    f'{path} belongs to another user, so the meeting container cannot use it. '
                    f'Run: sudo chown -R "$(id -u):$(id -g)" {path}') from error

    def _write_session_files(self, session, camera_settings=None, owner_values=None):
        payload = state_from_session(session)
        # Another owner's meeting joins under that owner's SMITLINE_PARTICIPANT_NAME; the local
        # owner's keeps the name it always had.
        if getattr(session, 'owner', 'local') != 'local':
            name = participant_name((owner_values or {}).get('SMITLINE_PARTICIPANT_NAME'))
            if name:
                payload['participantName'] = name
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
        claim = {'meetingId': session.id}
        if self._claim:
            claim.update(self._claim.get('record') or {})
        write_private_json(self._claim_path(), claim)
        return payload

    async def start(self, session, camera_settings=None):
        if self.local_only and getattr(session, 'owner', 'local') != 'local':
            raise DaemonError(409, 'meeting_pool_required', MEETING_POOL_REQUIRED)
        async with self._lock:
            status = await self.launcher.inspect()
            if status.get('unknown'):
                raise DaemonError(503, 'retry_required', 'meeting-agent state is uncertain')
            if self._active_id == session.id and status.get('running'):
                return
            if status.get('running') or (self._active_id not in (None, session.id)):
                raise DaemonError(409, 'capacity_exceeded', 'meeting agent is already running')
            values = self._owner_values(session)
            self._write_session_files(session, camera_settings=camera_settings, owner_values=values)
            try:
                self._prepare_mounts()
                owner_env = self._write_owner_env(session, values)
                try:
                    await self.launcher.up(self._container_env(session, owner_env))
                finally:
                    # Compose copied it into the container; no key stays on disk.
                    if owner_env is not None:
                        owner_env.unlink(missing_ok=True)
                await self._wait_until_running()
                await self._wait_until_health()
            except Exception:
                await self._stop_container()
                self._active_id = None
                self._active_session = None
                if self._claim:
                    self._claim['path'].unlink(missing_ok=True)
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
        self._release_active_claim(meeting_id)

    async def reconcile(self, daemon=None):
        daemon = daemon or self.daemon
        if daemon is None:
            return
        status = await self.launcher.inspect()
        claimed = self._claimed_id()
        if status.get('unknown'):
            return
        if status.get('running'):
            if not claimed:
                return
            record = daemon.meetings.get(claimed)
            if record is None or record.session.state == 'ended':
                return
            self.adopt(record.session)
            return
        await self._finish_unfinished(daemon)

    def adopt(self, session):
        """Follow a meeting whose container is still running from before a restart."""
        self._active_id = session.id
        self._active_session = session
        if self._watch_task is None or self._watch_task.done():
            self._watch_task = asyncio.create_task(
                self._watch(session), name='meeting-agent-watch-' + session.id)

    async def _finish_unfinished(self, daemon, skip=()):
        """Write the handoff of every meeting no container is running any more."""
        for meeting_id in daemon.meetings.list_ids():
            if meeting_id in skip:
                continue
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
        if self._claimed_id() != meeting_id:
            return
        if self._claim:
            self._claim['path'].unlink(missing_ok=True)
        else:
            write_private_json(active_meeting_path(self.project_root), {})

    async def shutdown(self):
        await self._stop_watch()


def pool_settings(environ=None):
    """SMITLINE_MAX_MEETINGS, SMITLINE_MAX_MEETINGS_PER_OWNER, and SMITLINE_MEETING_PORTS."""
    env = os.environ if environ is None else environ

    def count(name, default):
        text = str(env.get(name) or '').strip()
        if not text:
            return default
        if not text.isdigit() or int(text) < 1:
            raise ValueError(f'{name} must be a whole number of meetings, 1 or more')
        return int(text)
    ports = str(env.get('SMITLINE_MEETING_PORTS') or '').strip()
    low, high = DEFAULT_POOL_PORTS
    if ports:
        first, _, last = ports.partition('-')
        if not (first.strip().isdigit() and last.strip().isdigit()):
            raise ValueError('SMITLINE_MEETING_PORTS must look like 18100-18399')
        low, high = int(first), int(last)
        if not 1024 <= low < high <= 65535:
            raise ValueError('SMITLINE_MEETING_PORTS must be a range of ports between 1024 and 65535')
    return {'max_meetings': count('SMITLINE_MAX_MEETINGS', 1),
            'per_owner': count('SMITLINE_MAX_MEETINGS_PER_OWNER', None),
            'ports': (low, high)}


def port_is_free(port):
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind(('127.0.0.1', port))
        except OSError:
            return False
    return True


class _NoContainer:
    """The launcher of a supervisor that only writes handoffs: there is never a container."""

    async def up(self, env):
        raise RuntimeError('no meeting container here')

    async def stop(self):
        return None

    async def inspect(self):
        return {'running': False, 'unknown': False}


class _NoHealth:
    async def fetch(self):
        return None


class MeetingPool:
    """Several meetings at once, for SMITLINE_MAX_MEETINGS above 1.

    Each meeting runs in its own compose project, smitline-meeting-<meeting id>, published on two
    ports from SMITLINE_MEETING_PORTS (noVNC, then /health), with only its own run and recording
    folders and its owner's browser profiles mounted: the local owner's signed-in profiles, or,
    for any other owner, a folder of its own under .smitline/owners/<owner>/, so it joins as a
    guest. Each meeting has a ProductionMeetingSupervisor of its own, which follows it to its
    handoff exactly as the single supervisor does; the pool only hands out capacity, ports, and
    claims (.smitline/active-meetings/<meeting id>.json). A meeting's container gets only the
    environment owner_environment(owner) gives it (meeting_line.pool_meeting_environment).
    """
    # Every owner's meetings may run here.
    local_only = False

    def __init__(
        self,
        project_root,
        *,
        runtime_root=None,
        runner=None,
        environ=None,
        max_meetings=2,
        per_owner=None,
        ports=DEFAULT_POOL_PORTS,
        poll_interval=1.0,
        start_timeout=30.0,
        stop_timeout=25.0,
        owner_environment=None,
        launcher_factory=None,
        health_factory=None,
        port_free=port_is_free,
    ):
        self.project_root = Path(project_root).resolve()
        self.runtime_root = Path(runtime_root or (self.project_root / 'meeting-runtime')).resolve()
        self.runner = runner or SubprocessCommandRunner(self.project_root)
        self.environ = environ
        self.max_meetings = max_meetings
        # A cap for each owner other than the local one; None: only the pool's size.
        self.per_owner = per_owner
        self.ports = tuple(ports)
        self.poll_interval = poll_interval
        self.start_timeout = start_timeout
        self.stop_timeout = stop_timeout
        self.owner_environment = owner_environment
        self._launcher_factory = launcher_factory or (
            lambda project, env: ComposeMeetingAgent(self.runner, self.environ, project=project, env=env))
        self._health_factory = health_factory or (
            lambda port: UrlHealthClient(f'http://127.0.0.1:{port}/health'))
        self._port_free = port_free
        self.daemon = None
        self._lock = asyncio.Lock()
        self._meetings = {}  # meeting id -> {'supervisor', 'owner', 'ports'}
        self._starting = set()
        # Writes the handoff of a meeting that has no container (canceled before it started,
        # or left behind by a restart).
        self._finisher = self._supervisor(launcher=_NoContainer(), health=_NoHealth())

    def _supervisor(self, **kwargs):
        supervisor = ProductionMeetingSupervisor(
            self.project_root, runtime_root=self.runtime_root, runner=self.runner,
            poll_interval=self.poll_interval, start_timeout=self.start_timeout,
            stop_timeout=self.stop_timeout,
            owner_environment=lambda owner: (self.owner_environment(owner)
                                             if self.owner_environment else {}),
            **kwargs)
        return supervisor.bind_daemon(self.daemon)

    def bind_daemon(self, daemon):
        self.daemon = daemon
        self._finisher.bind_daemon(daemon)
        for meeting in self._meetings.values():
            meeting['supervisor'].bind_daemon(daemon)
        return self

    @staticmethod
    def project(meeting_id):
        return POOL_PROJECT_PREFIX + meeting_id

    def profiles_dir(self, owner):
        """The browser profiles a meeting may use: the installation's signed-in ones for the
        local owner; for any other owner, its own (empty unless it signs in), so it joins as a
        guest and never sees another account's sign-in."""
        if owner == 'local':
            return self.runtime_root / 'profiles'
        return self.project_root / '.smitline' / 'owners' / owner / 'meeting-profiles'

    def _mount_env(self, meeting_id, owner, ports):
        """Ports and folders the pool's compose file needs, as the Docker host names them."""
        folders = {
            'SMITLINE_MEETING_RUN_DIR': meeting_state_path(self.runtime_root, meeting_id).parent,
            'SMITLINE_MEETING_RECORDINGS_DIR': self.runtime_root / 'recordings' / meeting_id,
            'SMITLINE_MEETING_PROFILES_DIR': self.profiles_dir(owner),
        }
        if published_image(self.environ):
            # The published image mounts these as subpaths of the data volume (SMITLINE_ROOT).
            try:
                folders = {key: path.relative_to(self.project_root) for key, path in folders.items()}
            except ValueError as error:
                raise RuntimeError('meeting folders must be inside SMITLINE_ROOT to run several '
                                   'meetings at once') from error
        env = {key: str(path) for key, path in folders.items()}
        env.update(SMITLINE_MEETING_ID=meeting_id,
                   SMITLINE_MEETING_VNC_PORT=str(ports[0]),
                   SMITLINE_MEETING_HEALTH_PORT=str(ports[1]))
        return env

    def _mount_dirs(self, meeting_id, owner):
        dirs = [self.runtime_root / 'recordings' / meeting_id, self.profiles_dir(owner)]
        if not published_image(self.environ):
            # The code mount is read-only, so the folders the pool hides need mount points.
            dirs += [self.project_root / 'meeting-runtime' / name
                     for name in ('run', 'recordings', 'context')]
        return dirs

    def _allocate(self):
        taken = {port for meeting in self._meetings.values() for port in meeting['ports']}
        low, high = self.ports
        for first in range(low, high, 2):
            pair = (first, first + 1)
            if any(port in taken for port in pair):
                continue
            if all(self._port_free(port) for port in pair):
                return pair
        raise DaemonError(503, 'no_free_ports',
                          f'no two free ports for a meeting in SMITLINE_MEETING_PORTS {low}-{high}')

    def _meeting(self, meeting_id, owner, ports):
        supervisor = self._supervisor(
            launcher=self._launcher_factory(self.project(meeting_id),
                                            self._mount_env(meeting_id, owner, ports)),
            health=self._health_factory(ports[1]),
            claim={'path': pool_claim_path(self.project_root, meeting_id),
                   'record': {'owner': owner, 'project': self.project(meeting_id),
                              'ports': {'vnc': ports[0], 'health': ports[1]}}},
            mounts=self._mount_dirs(meeting_id, owner),
        )
        meeting = {'supervisor': supervisor, 'owner': owner, 'ports': ports}
        self._meetings[meeting_id] = meeting
        return meeting

    def _prune(self):
        """Forget meetings that ended, freeing their capacity and ports."""
        for meeting_id, meeting in list(self._meetings.items()):
            if meeting_id not in self._starting and meeting['supervisor']._active_id is None:
                self._meetings.pop(meeting_id, None)

    def active_ids(self):
        self._prune()
        return tuple(self._meetings)

    def _count_problem(self, owner):
        """A full pool, or an owner at its cap, as (status, code, message); else None."""
        if len(self._meetings) >= self.max_meetings:
            return 409, 'capacity_exceeded', f'all {self.max_meetings} meeting agents are busy'
        if owner != 'local' and self.per_owner is not None and sum(
                1 for item in self._meetings.values() if item['owner'] == owner) >= self.per_owner:
            return 409, 'owner_capacity_exceeded', f'this owner already has {self.per_owner} meetings running'
        return None

    def capacity_problem(self, owner):
        """Why a meeting for `owner` cannot start now, as (status, code, message), else None.
        Asked when a meeting call is placed; start() decides again, for meetings that race."""
        self._prune()
        problem = self._count_problem(owner)
        if problem is None:
            try:
                self._allocate()
            except DaemonError as error:
                problem = error.status, error.code, error.message
        return problem

    async def start(self, session, camera_settings=None):
        owner = getattr(session, 'owner', 'local')
        async with self._lock:
            self._prune()
            meeting = self._meetings.get(session.id)
            if meeting is None:
                problem = self._count_problem(owner)
                if problem:
                    raise DaemonError(*problem)
                meeting = self._meeting(session.id, owner, self._allocate())
            self._starting.add(session.id)
        try:
            await meeting['supervisor'].start(session, camera_settings=camera_settings)
        finally:
            async with self._lock:
                self._starting.discard(session.id)
                self._prune()

    async def add_context(self, meeting_id, context):
        meeting = self._meetings.get(meeting_id)
        await (meeting['supervisor'] if meeting else self._finisher).add_context(meeting_id, context)

    async def cancel(self, meeting_id):
        meeting = self._meetings.get(meeting_id)
        try:
            await (meeting['supervisor'] if meeting else self._finisher).cancel(meeting_id)
        finally:
            async with self._lock:
                self._prune()

    async def _projects(self):
        """Every pool compose project on the Docker host, as {name: running}; None if unknown."""
        result = await self.runner.run(['docker', 'compose', 'ls', '-a', '--format', 'json'], env=None)
        if result.returncode != 0:
            return None
        try:
            rows = json.loads(result.stdout or '[]')
        except ValueError:
            return None
        return {str(row.get('Name')): str(row.get('Status') or '').lower().startswith('running')
                for row in rows if isinstance(row, dict)
                and str(row.get('Name') or '').startswith(POOL_PROJECT_PREFIX)}

    async def _remove_project(self, name):
        await self.runner.run(['docker', 'compose', '-p', name, 'down', '--remove-orphans'], env=None)

    async def reconcile(self, daemon=None):
        """After a restart: follow every pool meeting of this installation still running and
        remove its other pool containers, then write the handoff of meetings that no container
        is running any more. A pool container whose meeting this installation does not know
        (no meeting record, no claim) belongs to another installation on the same Docker host,
        such as a test copy or a hosted deployment, and is left alone."""
        daemon = daemon or self.daemon
        if daemon is None:
            return
        projects = await self._projects()
        if projects is None:
            return
        adopted = set()
        for name, running in sorted(projects.items()):
            meeting_id = name[len(POOL_PROJECT_PREFIX):]
            session = None
            claim = {}
            known = False
            try:
                claim = read_json(pool_claim_path(self.project_root, meeting_id)) or {}
                known = bool(claim)
                record = daemon.meetings.get(meeting_id)
                known = known or record is not None
                session = None if record is None or record.session.state == 'ended' else record.session
            except Exception:
                session = None
            if not known:
                continue
            ports = claim.get('ports') or {}
            if running and session is not None and claim.get('meetingId') == meeting_id \
                    and isinstance(ports.get('vnc'), int) and isinstance(ports.get('health'), int):
                async with self._lock:
                    meeting = self._meeting(meeting_id, session.owner, (ports['vnc'], ports['health']))
                meeting['supervisor'].adopt(session)
                adopted.add(meeting_id)
            else:
                await self._remove_project(name)
        claims = control_root(self.project_root) / POOL_ACTIVE_DIR
        for path in claims.glob('*.json') if claims.is_dir() else ():
            if path.stem not in adopted:
                path.unlink(missing_ok=True)
        await self._finisher._finish_unfinished(daemon, skip=adopted)

    async def shutdown(self):
        # Containers keep running; the next start follows them again (reconcile).
        for meeting in list(self._meetings.values()):
            await meeting['supervisor'].shutdown()
