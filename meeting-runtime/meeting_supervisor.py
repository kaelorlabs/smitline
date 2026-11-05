"""Host-side MeetingSupervisor over the Docker meeting-agent."""
from pathlib import Path
import asyncio
import json
import os
import secrets

from meeting_finalizer import MeetingFinalizer, archive_dir, load_finalization
from meeting_lifecycle import AGENT_ENDINGS
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


async def post_control(url, message, headers, timeout=5.0):
    """POST one control message to a meeting container; its JSON answer when it took it."""
    from aiohttp import ClientError, ClientSession, ClientTimeout
    try:
        async with ClientSession(timeout=ClientTimeout(total=timeout)) as session:
            async with session.post(url, json=message, headers=headers) as response:
                if response.status != 200:
                    return None
                body = await response.json()
                return body if isinstance(body, dict) else None
    except (ClientError, asyncio.TimeoutError, ValueError):
        return None


class UrlHealthClient:
    """Reads meeting-agent /health. Tests inject a fake instead of this class.

    `url` may be a function, for a meeting on a machine of its own whose address is known only
    once it runs; `headers` too, for the meeting's token there (SMITLINE_HEALTH_TOKEN)."""

    def __init__(self, url=HEALTH_URL, timeout=0.7, headers=None):
        self._url = url
        self.timeout = timeout
        self._headers = headers

    @property
    def url(self):
        return self._url() if callable(self._url) else self._url

    @url.setter
    def url(self, value):
        self._url = value

    def headers(self):
        return dict((self._headers() if callable(self._headers) else self._headers) or {})

    async def fetch(self):
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._get)

    def _get(self):
        from urllib.error import URLError
        from urllib.request import Request, urlopen
        url = self.url
        if not url:
            return None
        try:
            with urlopen(Request(url, headers=self.headers()), timeout=self.timeout) as response:
                return json.loads(response.read().decode('utf-8'))
        except (URLError, TimeoutError, json.JSONDecodeError, OSError, ValueError):
            return None


# A meeting on a machine of its own (SMITLINE_MEETING_LAUNCHER): a machine takes a minute or two
# to boot (about 80 s on a Google VM, docs/meetings.md), and its archive comes back over HTTP.
REMOTE_START_TIMEOUT = 300.0
# How long the daemon asks for the archive once the meeting ended, and how long the meeting's
# server waits for it (SMITLINE_ARCHIVE_GRACE).
ARCHIVE_TIMEOUT = 90.0
ARCHIVE_GRACE = 120
MAX_ARCHIVE_BYTES = 256 * 1024 * 1024


def unpack_archive(data, directory):
    """Write a meeting archive's files (a gzipped tar from GET /archive) into `directory`, private
    to this user. Only plain top-level files, within MAX_ARCHIVE_BYTES; anything else is skipped."""
    import io
    import tarfile
    directory = Path(directory)
    ensure_private_dir(directory)
    written, total = [], 0
    with tarfile.open(fileobj=io.BytesIO(data), mode='r:gz') as tar:
        for member in tar:  # one header at a time, so the cap stops reading too
            name = member.name
            if (not member.isfile() or not name or '/' in name or '\\' in name
                    or name in ('.', '..') or name.startswith('.')):
                continue
            total += member.size
            if total > MAX_ARCHIVE_BYTES:
                break
            source = tar.extractfile(member)
            if source is None:
                continue
            write_private_file(directory / name, source.read())
            written.append(name)
    return written


def fetch_archive(url, headers, timeout=10.0):
    """GET /archive once: ('ready', bytes), ('not_closed', None) or ('unreachable', None)."""
    from urllib.error import HTTPError, URLError
    from urllib.request import Request, urlopen
    try:
        with urlopen(Request(url, headers=headers), timeout=timeout) as response:
            return 'ready', response.read(MAX_ARCHIVE_BYTES + 1)
    except HTTPError as error:
        return ('not_closed' if error.code == 409 else 'unreachable'), None
    except (URLError, TimeoutError, OSError, ValueError):
        return 'unreachable', None


def remote_token_path(project_root, meeting_id):
    """A remote meeting's token, in a private file beside its claim: a restarted daemon needs it
    to control, hear and finish the meeting, and claims are JSON, which never holds a secret."""
    return pool_claim_path(project_root, meeting_id).with_suffix('.token')


def read_token(path):
    try:
        return Path(path).read_text(encoding='utf-8').strip() or None
    except (OSError, UnicodeDecodeError):
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
        archive_timeout=ARCHIVE_TIMEOUT,
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
        # A launcher plugin's machine (RemoteMeetingPool) runs the meeting where the daemon cannot
        # read its disk: it gets a start package, and the archive comes back over HTTP.
        self.remote = bool(getattr(self.launcher, 'remote', False))
        self.health = health or UrlHealthClient()
        self.poll_interval = poll_interval
        self.start_timeout = start_timeout
        self.stop_timeout = stop_timeout
        self.archive_timeout = archive_timeout
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
        # The running meeting's own token for /control and /listen, kept in memory only: a
        # meeting adopted after a restart has none, so it runs on without either. A remote
        # meeting's is also kept in a private file beside its claim, so a restarted daemon has it.
        self._control_token = None
        # While a remote meeting starts: its id, and an event cancel() sets to stop the start.
        self._start_cancel = None
        self._starting_id = None
        # The remote meeting whose archive is already here (_fetch_archive).
        self._archive_taken = None

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
        if self._control_token:
            env['SMITLINE_CONTROL_TOKEN'] = self._control_token
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
        if self.remote and self._control_token:
            # A restarted daemon reads it back to control, hear and finish the meeting (adopt).
            write_private_file(self._claim_path().with_suffix('.token'), self._control_token + '\n')
        return payload

    def _drop_claim(self):
        """Remove a pool meeting's claim, and a remote meeting's token beside it."""
        self._claim['path'].unlink(missing_ok=True)
        if self.remote:
            self._claim['path'].with_suffix('.token').unlink(missing_ok=True)

    def _start_package(self, session, payload, values):
        """What a remote meeting's machine needs to run it, handed to the launcher's up(): the
        runtime state the container reads, its whole environment (the owner's settings and
        provider key, as a pool meeting's env file would hold them) and the meeting's token.
        No setting or key of it is written to disk here."""
        environment = dict(values or {})
        environment.update({
            'MEETING_URL': session.meeting_url,
            'SMITLINE_RUNTIME_STATE': '/meeting-runtime/run/meetings/' + session.id + '/runtime.json',
            'SMITLINE_CONTROL_TOKEN': self._control_token,
            # Its port is reachable on a network: /health takes the token too.
            'SMITLINE_HEALTH_TOKEN': '1',
            'SMITLINE_ARCHIVE_GRACE': str(ARCHIVE_GRACE),
        })
        return {
            'meetingId': session.id,
            'owner': getattr(session, 'owner', 'local'),
            'meetingUrl': session.meeting_url,
            'runtimeState': payload,
            'environment': environment,
            'controlToken': self._control_token,
        }

    async def start(self, session, camera_settings=None):
        if self.local_only and getattr(session, 'owner', 'local') != 'local':
            raise DaemonError(409, 'meeting_pool_required', MEETING_POOL_REQUIRED)
        if not self.remote:
            return await self._start(session, camera_settings)
        # A machine takes a minute or more to start. cancel() sets this, so ending the meeting
        # meanwhile stops the start at once instead of waiting behind it.
        self._start_cancel = asyncio.Event()
        self._starting_id = session.id
        try:
            return await self._start(session, camera_settings)
        finally:
            self._start_cancel = None
            self._starting_id = None

    async def _start(self, session, camera_settings):
        async with self._lock:
            status = await self.launcher.inspect()
            if status.get('unknown'):
                raise DaemonError(503, 'retry_required', 'meeting-agent state is uncertain')
            if self._active_id == session.id and status.get('running'):
                return
            if status.get('running') or (self._active_id not in (None, session.id)):
                raise DaemonError(409, 'capacity_exceeded', 'meeting agent is already running')
            values = self._owner_values(session)
            self._control_token = secrets.token_urlsafe(32)
            payload = self._write_session_files(session, camera_settings=camera_settings,
                                                owner_values=values)
            try:
                if self.remote:
                    # The machine gets everything by value: no setting or key is written here.
                    self._check_start_canceled()
                    await self._unless_canceled(
                        self.launcher.up(self._start_package(session, payload, values)))
                else:
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
                self._control_token = None
                if self._claim:
                    self._drop_claim()
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

    async def cancel(self, meeting_id, reason='cancelled'):
        # A remote machine takes minutes to start; ending the meeting meanwhile stops the start
        # at once instead of waiting behind it.
        if self._start_cancel is not None:
            self._start_cancel.set()
        async with self._lock:
            await self._cancel_locked(meeting_id, reason)

    async def _cancel_locked(self, meeting_id, reason='cancelled'):
        if self._active_id not in (None, meeting_id):
            await self._complete_handoff_for(meeting_id, reason, partial=True)
            return
        await self._stop_watch()
        if self.remote and self._active_id == meeting_id:
            # The daemon cannot signal a container on another machine: the meeting is asked to
            # stop, as SIGTERM would, and its archive comes back before the machine is deleted.
            if await self.control(meeting_id, {'type': 'stop'}) is not None:
                await self._fetch_archive(meeting_id, timeout=self.stop_timeout)
        await self._await_stop()
        await self._complete_handoff_for(meeting_id, reason, partial=True)
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

    def adopt(self, session, control_token=None):
        """Follow a meeting whose container is still running from before a restart; with its
        token (kept beside a remote meeting's claim), control, hearing and its archive too."""
        self._active_id = session.id
        self._active_session = session
        if control_token:
            self._control_token = control_token
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

    def _check_start_canceled(self):
        """A remote start stops once its meeting was ended: by cancel(), or by a handoff written
        meanwhile (the meeting was ended before its start began)."""
        if self._start_cancel is None:
            return
        if self._start_cancel.is_set() or self._handoff_ready(self._starting_id):
            raise DaemonError(409, 'canceled', 'the meeting was ended while it was starting')

    async def _unless_canceled(self, awaitable):
        """Await a remote start's step, unless cancel() comes first: the step is then
        interrupted and the start ends as canceled."""
        task = asyncio.ensure_future(awaitable)
        waiter = asyncio.ensure_future(self._start_cancel.wait())
        try:
            await asyncio.wait({task, waiter}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            waiter.cancel()
            if not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
        if task.cancelled():
            self._check_start_canceled()
        return task.result()

    def _inspect_every(self):
        """Seconds between asking the launcher while starting or stopping: a container answers
        at once; a remote machine's state is a cloud API call."""
        return self.poll_interval if self.remote else min(self.poll_interval, 0.05)

    async def _wait_until_running(self):
        deadline = asyncio.get_running_loop().time() + self.start_timeout
        while asyncio.get_running_loop().time() < deadline:
            self._check_start_canceled()
            status = await self.launcher.inspect()
            if status.get('unknown'):
                raise DaemonError(503, 'retry_required', 'meeting-agent state is uncertain')
            if status.get('running'):
                return
            await asyncio.sleep(self._inspect_every())
        raise RuntimeError('meeting-agent did not become running')

    async def _wait_until_health(self):
        deadline = asyncio.get_running_loop().time() + self.start_timeout
        while asyncio.get_running_loop().time() < deadline:
            self._check_start_canceled()
            health = await self.health.fetch()
            if health:
                return health
            status = await self.launcher.inspect()
            if status.get('unknown'):
                raise DaemonError(503, 'retry_required', 'meeting-agent state is uncertain')
            if not status.get('running'):
                raise RuntimeError('meeting-agent exited during startup')
            await asyncio.sleep(self._inspect_every())
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
            await asyncio.sleep(self._inspect_every())
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
                # A remote machine's state is a cloud API call, so it is asked only when the
                # meeting does not answer; a container's is asked first, as always.
                health = await self.health.fetch() if self.remote else None
                if not health:
                    status = await self.launcher.inspect()
                    if status.get('unknown'):
                        continue
                    if not status.get('running'):
                        await self._complete_handoff(session, 'container_exited', partial=True)
                        return
                    if not self.remote:
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
        note_captions = getattr(self.daemon, 'note_captions', None)
        if note_captions is not None and 'captions' in health:
            try:
                note_captions(session.id, health['captions'])
            except Exception:
                pass
        note_requests = getattr(self.daemon, 'note_requests', None)
        if note_requests is not None and 'requests' in health:
            try:
                note_requests(session.id, health['requests'])
            except Exception:
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
            reason = stage
            if stage in COMPLETE_REASONS and health.get('endReason') in AGENT_ENDINGS:
                reason = health['endReason']  # the agent left: why, not just that it ended
            await self._complete_handoff(
                session, reason, partial=stage not in COMPLETE_REASONS)
            return True
        return False

    def _container_url(self, meeting_id, path):
        url = getattr(self.health, 'url', None)
        if self._active_id != meeting_id or not isinstance(url, str) or not url.endswith('/health'):
            return None
        return url[:-len('/health')] + path

    def listen_url(self, meeting_id):
        """The running meeting container's /listen, or None."""
        return self._container_url(meeting_id, '/listen')

    def control_headers(self, meeting_id):
        """What /listen and /control need from the daemon: the meeting's token."""
        if self._active_id != meeting_id or not self._control_token:
            return None
        return {'Authorization': 'Bearer ' + self._control_token}

    async def control(self, meeting_id, message):
        """Send the running meeting a control message (meeting_control.py); its answer, or None
        when it is not running, has no token, or did not take it."""
        url = self._container_url(meeting_id, '/control')
        headers = self.control_headers(meeting_id)
        if url is None or headers is None:
            return None
        return await post_control(url, message, headers)

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
        if self.remote:
            # Its disk is on another machine: bring the archive here before the handoff.
            await self._fetch_archive(session.id)
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

    async def _fetch_archive(self, meeting_id, timeout=None):
        """A remote meeting's archive into recordings/<meeting id>, from its GET /archive, before
        the handoff is written. The meeting may still be closing it (409): ask again until
        archive_timeout, unless the machine is gone. False when it never came; the handoff is
        then written from what the daemon has (a partial one)."""
        if self._archive_taken == meeting_id:
            return True
        url = self._container_url(meeting_id, '/archive')
        token = self._control_token
        if url is None or not token:
            return False
        headers = {'Authorization': 'Bearer ' + token}
        loop = asyncio.get_running_loop()
        deadline = loop.time() + (self.archive_timeout if timeout is None else timeout)
        while True:
            outcome, data = await loop.run_in_executor(None, fetch_archive, url, headers)
            if outcome == 'ready' and data and len(data) <= MAX_ARCHIVE_BYTES:
                try:
                    await loop.run_in_executor(
                        None, unpack_archive, data, archive_dir(self.runtime_root, meeting_id))
                except Exception:
                    return False
                self._archive_taken = meeting_id
                return True
            if outcome == 'ready' or loop.time() >= deadline:
                return False
            if outcome == 'unreachable':
                status = await self.launcher.inspect()
                if not status.get('running'):
                    return False
            await asyncio.sleep(self.poll_interval)

    def _release_active_claim(self, meeting_id):
        if self._active_id == meeting_id:
            self._active_id = None
            self._active_session = None
            self._control_token = None
        if self._claimed_id() != meeting_id:
            return
        if self._claim:
            self._drop_claim()
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

    def control_headers(self, meeting_id):
        entry = self._meetings.get(meeting_id)
        return entry['supervisor'].control_headers(meeting_id) if entry else None

    async def control(self, meeting_id, message):
        entry = self._meetings.get(meeting_id)
        return await entry['supervisor'].control(meeting_id, message) if entry else None

    def listen_url(self, meeting_id):
        entry = self._meetings.get(meeting_id)
        return entry['supervisor'].listen_url(meeting_id) if entry else None

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

    async def cancel(self, meeting_id, reason='cancelled'):
        meeting = self._meetings.get(meeting_id)
        try:
            await (meeting['supervisor'] if meeting else self._finisher).cancel(meeting_id, reason)
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


LAUNCHER_PARTS = ('machine', 'running', 'remove')


def load_launcher(spec, **kwargs):
    """A meeting launcher plugin from 'package.module:create' (SMITLINE_MEETING_LAUNCHER), built
    as create(environ=..., project_root=..., runtime_root=...). See RemoteMeetingPool for what
    it must provide."""
    import importlib
    module_name, _, attribute = str(spec or '').strip().partition(':')
    if not module_name or not attribute:
        raise ValueError('SMITLINE_MEETING_LAUNCHER must look like module:create')
    factory = getattr(importlib.import_module(module_name), attribute)
    provider = factory(**kwargs)
    for name in LAUNCHER_PARTS:
        if not callable(getattr(provider, name, None)):
            raise ValueError(f'the meeting launcher is missing {name}()')
    return provider


class RemoteMeetingPool(MeetingPool):
    """Meetings on machines of their own, from a launcher plugin (SMITLINE_MEETING_LAUNCHER), such
    as a cloud VM for each meeting. The plugin's create(environ=, project_root=, runtime_root=)
    returns a provider with:

    - machine(meeting_id, owner): the machine for one meeting, at once (no I/O). After a restart
      it is asked again for a machine that still runs, and then stands for that one. It has:
        - remote = True;
        - async up(package): create the machine and have it run the meeting. It may return once
          the machine is asked for; the daemon then waits for inspect() and /health. The package
          (ProductionMeetingSupervisor._start_package) holds the meeting id, its runtime state
          (the container reads it at SMITLINE_RUNTIME_STATE), the container's whole environment
          (the owner's settings and provider key, SMITLINE_CONTROL_TOKEN) and the meeting's
          token: hand it to the machine over a private channel, never in instance metadata. A
          meeting ended while it starts interrupts up() (the task is cancelled), then stop();
        - async stop(): delete the machine, also one that never finished starting; a machine
          never serves a second meeting;
        - async inspect(): {'running': bool, 'unknown': bool}. running is False once stop() was
          asked or the meeting exited, even while the cloud is still deleting the machine. It is
          asked about once a second while the machine starts or stops, and when the meeting
          stops answering /health: keep it cheap (a cache) if the cloud's API has quotas;
        - health_url: http://<address>:8094/health once the machine has an address, else None.
          A plain attribute that up() or inspect() sets.
    - async running(): {meeting_id: running} for every machine of this installation (by label),
      so a restarted daemon adopts the meetings still going;
    - async remove(meeting_id): delete that meeting's machine;
    - start_timeout (optional): seconds a machine may take to answer /health (default 300).

    The daemon follows each meeting as it follows a pool container, at the machine's address:
    /health every second and /control and /listen, all with the meeting's token, and the archive
    from GET /archive before the handoff is written. Starting runs in the background
    (starts_in_background), so the meeting is joining meanwhile and ending it never waits behind
    the start. The token is kept in a private file beside the meeting's claim, so a restarted
    daemon can still control, hear and finish the meetings it adopts. No ports, folders or
    containers here.
    """
    starts_in_background = True

    def __init__(self, project_root, *, launcher, ports=None, start_timeout=None,
                 archive_timeout=ARCHIVE_TIMEOUT, port_free=None, launcher_factory=None,
                 health_factory=None, **kwargs):
        self.provider = launcher
        self.archive_timeout = archive_timeout
        timeout = start_timeout or getattr(launcher, 'start_timeout', None) or REMOTE_START_TIMEOUT
        super().__init__(project_root, start_timeout=float(timeout),
                         port_free=lambda _port: True, **kwargs)

    def _allocate(self):
        return (None, None)  # a machine of its own: no host ports to hand out

    def _meeting(self, meeting_id, owner, ports):
        machine = self.provider.machine(meeting_id, owner)
        holder = {}

        def token_header():
            token = holder['supervisor']._control_token
            return {'Authorization': 'Bearer ' + token} if token else {}
        supervisor = self._supervisor(
            launcher=machine,
            health=UrlHealthClient(lambda: getattr(machine, 'health_url', None), timeout=2.0,
                                   headers=token_header),
            claim={'path': pool_claim_path(self.project_root, meeting_id),
                   'record': {'owner': owner, 'project': self.project(meeting_id), 'remote': True}},
            archive_timeout=self.archive_timeout,
        )
        holder['supervisor'] = supervisor
        meeting = {'supervisor': supervisor, 'owner': owner, 'ports': (None, None)}
        self._meetings[meeting_id] = meeting
        return meeting

    async def reconcile(self, daemon=None):
        """After a restart: adopt every meeting of this installation whose machine still runs,
        with the token kept beside its claim, and delete the machines of meetings that ended. A machine
        this installation knows nothing about (no claim, no meeting record) is left alone. Then
        write the handoff of meetings no machine runs any more."""
        daemon = daemon or self.daemon
        if daemon is None:
            return
        try:
            machines = await self.provider.running()
        except Exception:
            return
        adopted = set()
        for meeting_id, running in sorted((machines or {}).items()):
            session, claim, known = None, {}, False
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
            if running and session is not None and claim.get('meetingId') == meeting_id:
                async with self._lock:
                    meeting = self._meeting(meeting_id, session.owner, (None, None))
                token = read_token(remote_token_path(self.project_root, meeting_id))
                meeting['supervisor'].adopt(session, control_token=token)
                adopted.add(meeting_id)
            else:
                try:
                    await self.provider.remove(meeting_id)
                except Exception:
                    pass
        claims = control_root(self.project_root) / POOL_ACTIVE_DIR
        for path in claims.iterdir() if claims.is_dir() else ():
            if path.suffix in ('.json', '.token') and path.stem not in adopted:
                path.unlink(missing_ok=True)
        await self._finisher._finish_unfinished(daemon, skip=adopted)
