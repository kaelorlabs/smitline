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
import socket
from urllib.parse import urlsplit


# api.trycloudflare.com appears in cloudflared's own error messages; it is never a tunnel.
QUICK_URL = re.compile(r'https://(?!api\.)[a-z0-9-]+\.trycloudflare\.com\b')
REGISTERED = 'Registered tunnel connection'
DEFAULT_IMAGE = 'cloudflare/cloudflared:2026.9.3'
# The first run may pull the Docker image; a fresh address can take a few seconds to resolve.
START_TIMEOUT = 90.0
PROBE_TIMEOUT = 45.0
# A quick tunnel that never becomes reachable is replaced by a fresh one this many times.
QUICK_TUNNEL_ATTEMPTS = 2
# Before reuse, a running quick tunnel must still answer within this long.
REUSE_PROBE_TIMEOUT = 6.0
# Public DNS over HTTPS (JSON form), asked in order: Cloudflare, then Google.
PUBLIC_DNS = ('https://1.1.1.1/dns-query', 'https://dns.google/resolve')


class TunnelError(RuntimeError):
    pass


def dns_answers(data):
    """IPv4 addresses from a DNS-over-HTTPS JSON answer."""
    answers = data.get('Answer') if isinstance(data, dict) else None
    return [item['data'] for item in answers or []
            if isinstance(item, dict) and item.get('type') == 1 and isinstance(item.get('data'), str)]


async def ask_dns(endpoint, host):
    """One DNS-over-HTTPS query, or [] when there is no answer or the server cannot be asked.

    Each query opens a fresh connection. A kept-alive one reaches the same server every
    time, which remembers "no such name" from before the tunnel existed; a fresh one is
    routed to whichever server is nearest, and finds the new name within seconds.
    """
    from aiohttp import ClientError, ClientSession, ClientTimeout, TCPConnector
    try:
        async with ClientSession(timeout=ClientTimeout(total=5), connector=TCPConnector(force_close=True)) as session:
            async with session.get(endpoint, params={'name': host, 'type': 'A'},
                                   headers={'Accept': 'application/dns-json'}) as response:
                if response.status != 200:
                    return []
                return dns_answers(await response.json(content_type=None))
    except (ClientError, OSError, asyncio.TimeoutError, ValueError):
        return []


async def public_addresses(host, ask=None):
    """The addresses public DNS gives for `host`: the first provider that knows it, or []."""
    ask = ask or ask_dns
    for endpoint in PUBLIC_DNS:
        addresses = await ask(endpoint, host)
        if addresses:
            return addresses
    return []


def _pinned_resolver(host, addresses):
    """An aiohttp resolver that answers `host` with the given addresses (TLS still checks `host`)."""
    from aiohttp.abc import AbstractResolver

    class Pinned(AbstractResolver):
        async def resolve(self, name, port=0, family=socket.AF_INET):
            return [{'hostname': host, 'host': address, 'port': port, 'family': socket.AF_INET,
                     'proto': 0, 'flags': socket.AI_NUMERICHOST} for address in addresses]

        async def close(self):
            pass

    return Pinned()


async def answers(url, addresses=None):
    """None when `url` answers 200, else why not (resolving its host normally or at `addresses`)."""
    from aiohttp import ClientError, ClientSession, ClientTimeout, TCPConnector
    connector = TCPConnector(resolver=_pinned_resolver(urlsplit(url).hostname, addresses)) if addresses else None
    try:
        async with ClientSession(timeout=ClientTimeout(total=5), connector=connector) as session:
            async with session.get(url) as response:
                return None if response.status == 200 else f'HTTP {response.status}'
    except (ClientError, OSError, asyncio.TimeoutError) as error:
        return type(error).__name__


async def reachable(url, timeout, problems=None):
    """Poll `url` until it answers 200: the tunnel's DNS name can lag its registration.

    Each round asks both this computer's DNS and public DNS. The phone provider resolves
    a new trycloudflare.com name within seconds, but a home router's resolver can take
    minutes and remembers the miss, which would fail a tunnel that works. The last reason
    it did not answer is appended to `problems`.
    """
    host = urlsplit(url).hostname
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    last = None
    while True:
        last = await answers(url)
        if last is None:
            return True
        addresses = await public_addresses(host)
        if addresses:
            through_public = await answers(url, addresses)
            if through_public is None:
                return True
            last = f'{through_public} through public DNS'
        if loop.time() + 1 >= deadline:
            if problems is not None and last:
                problems.append(last)
            return False
        await asyncio.sleep(1)


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
                 timeout=START_TIMEOUT, probe=None, probe_timeout=PROBE_TIMEOUT,
                 attempts=QUICK_TUNNEL_ATTEMPTS, log=print):
        self._environ = environ
        self.local_port = local_port
        self._spawn = spawn or asyncio.create_subprocess_exec
        self._which = which
        self.timeout = timeout
        self._probe = probe or reachable
        self.probe_timeout = probe_timeout
        self.attempts = attempts
        self._log = log
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
            return ['docker', 'run', '--rm', '--network', 'host', '--name', self.container(),
                    image, 'tunnel', '--no-autoupdate', '--url', target]
        return None

    def container(self):
        return f'colleague-tunnel-{self.local_port}'

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
                # A quick tunnel does not survive sleep or a network change, though cloudflared
                # keeps running and retrying: check it still answers before handing it out.
                problems = []
                if await self._probe(self._url + '/healthz', REUSE_PROBE_TIMEOUT, problems):
                    return self._url
                why = problems[-1] if problems else 'no reply'
                self._log(f'quick tunnel {self._url} stopped answering ({why}); starting a fresh one', flush=True)
                self._url = None
            for attempt in range(1, self.attempts + 1):
                url, problem = await self._start_locked()
                if url:
                    self._url = url
                    return url
                self._log(f'quick tunnel attempt {attempt} failed: {problem}', flush=True)
            raise TunnelError(f'The Cloudflare quick tunnel did not become reachable ({problem}); '
                              'check the network, or set COLLEAGUE_PUBLIC_URL.')

    async def _start_locked(self):
        """Start a fresh quick tunnel: (url, None) once reachable, else (None, why not)."""
        await self._stop_locked()
        command = self.command()
        if command is None:
            raise TunnelError('Set COLLEAGUE_PUBLIC_URL, or install cloudflared or Docker, so '
                              'Twilio can reach this computer.')
        if command[0] == 'docker':
            await self._remove_stale_container()
        self._process = await self._spawn(
            *command, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
        try:
            url = await asyncio.wait_for(self._read_url(), self.timeout)
        except asyncio.TimeoutError as error:
            await self._stop_locked()
            raise TunnelError('The Cloudflare quick tunnel did not start in time.') from error
        self._reader = asyncio.create_task(self._drain())
        problems = []
        if await self._probe(url + '/healthz', self.probe_timeout, problems):
            self._log(f'quick tunnel ready: {url}', flush=True)
            return url, None
        await self._stop_locked()
        return None, f'{url} did not answer: {problems[-1] if problems else "no reply"}'

    async def _remove_stale_container(self):
        """A daemon that was killed can leave its tunnel container running under our name."""
        try:
            process = await self._spawn('docker', 'rm', '-f', self.container(),
                                        stdout=asyncio.subprocess.DEVNULL,
                                        stderr=asyncio.subprocess.DEVNULL)
            await asyncio.wait_for(process.wait(), 15)
        except (OSError, asyncio.TimeoutError):
            pass

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
