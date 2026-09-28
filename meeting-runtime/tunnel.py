"""A public HTTPS address for Twilio: configured, or a Cloudflare quick tunnel.

Twilio must reach the phone gateway to deliver audio and call status. Server
installs set COLLEAGUE_PUBLIC_URL behind their own TLS proxy. Laptop installs
get a temporary trycloudflare.com address from `cloudflared`, run from the
binary when installed or from the official Docker image otherwise. Only the
phone gateway is exposed; the daemon API stays on loopback.
"""
import asyncio
import re
import shutil
from urllib.parse import urlsplit


# api.trycloudflare.com appears in cloudflared's own error messages; it is never a tunnel.
QUICK_URL = re.compile(r'https://(?!api\.)[a-z0-9-]+\.trycloudflare\.com\b')
REGISTERED = 'Registered tunnel connection'
DEFAULT_IMAGE = 'cloudflare/cloudflared:latest'
START_TIMEOUT = 40.0


class TunnelError(RuntimeError):
    pass


def configured_url(environ):
    value = (environ.get('COLLEAGUE_PUBLIC_URL') or '').strip().rstrip('/')
    if not value:
        return None
    url = urlsplit(value)
    if url.scheme != 'https' or not url.hostname or url.path not in ('', '/'):
        raise TunnelError('COLLEAGUE_PUBLIC_URL must be an https origin such as https://calls.example.com')
    return value


class PublicUrl:
    def __init__(self, environ, local_port, *, spawn=None, which=shutil.which,
                 timeout=START_TIMEOUT):
        self._environ = environ
        self.local_port = local_port
        self._spawn = spawn or asyncio.create_subprocess_exec
        self._which = which
        self.timeout = timeout
        self._lock = asyncio.Lock()
        self._process = None
        self._url = None
        self._reader = None

    def command(self):
        env = self._environ()
        target = f'http://127.0.0.1:{self.local_port}'
        binary = env.get('COLLEAGUE_CLOUDFLARED') or self._which('cloudflared')
        if binary:
            return [binary, 'tunnel', '--no-autoupdate', '--url', target]
        if self._which('docker'):
            image = env.get('COLLEAGUE_CLOUDFLARED_IMAGE') or DEFAULT_IMAGE
            return ['docker', 'run', '--rm', '--network', 'host', image,
                    'tunnel', '--no-autoupdate', '--url', target]
        return None

    def available(self):
        try:
            if configured_url(self._environ()):
                return True
        except TunnelError:
            return False
        return self.command() is not None

    def describe(self):
        env = self._environ()
        if configured_url(env):
            return 'configured'
        return 'quick_tunnel' if self.command() else 'unavailable'

    def current(self):
        """The address Twilio uses right now, without starting anything; None if there is none."""
        try:
            url = configured_url(self._environ())
        except TunnelError:
            return None
        if url:
            return url
        if self._url and self._process is not None and self._process.returncode is None:
            return self._url
        return None

    async def get(self):
        url = configured_url(self._environ())
        if url:
            return url
        async with self._lock:
            if self._url and self._process is not None and self._process.returncode is None:
                return self._url
            await self._stop_locked()
            command = self.command()
            if command is None:
                raise TunnelError('Set COLLEAGUE_PUBLIC_URL, or install cloudflared or Docker, so '
                                  'Twilio can reach this computer.')
            self._process = await self._spawn(
                *command, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
            try:
                self._url = await asyncio.wait_for(self._read_url(), self.timeout)
            except asyncio.TimeoutError as error:
                await self._stop_locked()
                raise TunnelError('The Cloudflare quick tunnel did not start in time.') from error
            self._reader = asyncio.create_task(self._drain())
            return self._url

    async def _read_url(self):
        """Return the tunnel address once cloudflared reports a registered connection."""
        url = None
        while True:
            line = await self._process.stderr.readline()
            if not line:
                raise TunnelError('cloudflared exited before the tunnel was ready.')
            text = line.decode('utf-8', 'replace')
            if 'failed' in text.lower() and 'trycloudflare' in text and url is None:
                raise TunnelError('cloudflared could not create a quick tunnel; check the '
                                  'network, or set COLLEAGUE_PUBLIC_URL.')
            match = QUICK_URL.search(text)
            if match and url is None:
                url = match.group(0)
            if url and REGISTERED in text:
                return url

    async def _drain(self):
        try:
            while await self._process.stderr.readline():
                pass
        except Exception:
            pass

    async def _stop_locked(self):
        process, self._process = self._process, None
        self._url = None
        if self._reader is not None:
            self._reader.cancel()
            self._reader = None
        if process is not None and process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), 5)
            except asyncio.TimeoutError:
                process.kill()

    async def close(self):
        async with self._lock:
            await self._stop_locked()
