"""The meeting view: the meeting browser's screen in noVNC, for whoever holds this start's token.

x11vnc serves the virtual display inside the container only (127.0.0.1:5900, joinly's
VirtualDisplay). websockify publishes it on port 6080 as a WebSocket, beside noVNC's static page,
and opens the display only to a WebSocket that carries this start's token
(/websockify?token=...). Whoever starts the container makes the token and passes it as
SMITLINE_VIEWER_TOKEN: the daemon for a meeting, start-meeting-agent.sh for a sign-in or a meeting
started by hand. They keep it in a private file: .smitline/meeting-view-token, where the console
finds it for Open meeting view, or for a meeting of the pool .smitline/active-meetings/<id>.view.
A container started without a usable token, such as a meeting on a machine of its own, makes up
one nobody is told, so its view stays closed.
"""
import asyncio
import contextlib
import os
import re
import secrets
import shutil
import tempfile

VIEW_PORT = 6080
VNC_ADDRESS = '127.0.0.1:5900'
# What secrets.token_urlsafe makes; the console and start-meeting-agent.sh check the same.
TOKEN = re.compile(r'[A-Za-z0-9_-]{32,128}')


def viewer_token(environ=None):
    """This start's token, SMITLINE_VIEWER_TOKEN; when it is missing or not a token, one nobody
    is told, so the view stays closed."""
    env = os.environ if environ is None else environ
    token = str(env.get('SMITLINE_VIEWER_TOKEN') or '').strip()
    return token if TOKEN.fullmatch(token) else secrets.token_urlsafe(32)


def viewer_command(token_file):
    """websockify serving noVNC on VIEW_PORT, and the display only for the token in token_file."""
    return ['/usr/bin/websockify', '--web=/usr/share/novnc',
            '--token-plugin=ReadOnlyTokenFile', '--token-source=' + str(token_file),
            str(VIEW_PORT)]


def write_token_file(directory, token):
    """websockify's token file (token: host:port) in a private folder; its path."""
    path = os.path.join(directory, 'view-token')
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as stream:
        stream.write(f'{token}: {VNC_ADDRESS}\n')
    return path


class MeetingView:
    """The running viewer: websockify and its private folder, until stop()."""

    def __init__(self, process, directory):
        self.process = process
        self.directory = directory

    async def stop(self):
        if self.process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                self.process.terminate()
        await self.process.wait()
        shutil.rmtree(self.directory, ignore_errors=True)


async def start_meeting_view(env, environ=None, spawn=None):
    """Start the meeting view for this start's token. env is the viewer process's environment,
    which needs no token: websockify reads it from its file."""
    token = viewer_token(environ)
    directory = tempfile.mkdtemp(prefix='smitline-view-')
    try:
        command = viewer_command(write_token_file(directory, token))
        process = await (spawn or asyncio.create_subprocess_exec)(
            *command, env=env, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
    except BaseException:
        shutil.rmtree(directory, ignore_errors=True)
        raise
    return MeetingView(process, directory)
