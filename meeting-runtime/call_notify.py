"""Signed webhook delivery for finished calls."""
import asyncio
import hashlib
import hmac
import ipaddress
import json
import os
import secrets
from pathlib import Path
from urllib.parse import urlsplit

from call_brief import validate_webhook_url


RETRY_DELAYS = (1.0, 4.0, 10.0)


def load_or_create_secret(path):
    path = Path(path)
    try:
        value = path.read_text(encoding='utf-8').strip()
        if value:
            return value
    except FileNotFoundError:
        pass
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    value = secrets.token_hex(32)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, (value + '\n').encode('utf-8'))
    finally:
        os.close(fd)
    return value


async def resolves_publicly(url):
    """False when an https hostname resolves to a private, loopback or link-local address.

    Checked at delivery time so a webhook cannot be aimed at the daemon's own
    network through DNS. http://localhost receivers are allowed by validation.
    """
    parts = urlsplit(url)
    host = parts.hostname or ''
    if parts.scheme != 'https':
        return True
    try:
        ipaddress.ip_address(host)
        return True  # IP literals were checked by validate_webhook_url
    except ValueError:
        pass
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, parts.port or 443)
    except OSError:
        return True  # unresolvable: delivery fails as unreachable
    for info in infos:
        address = ipaddress.ip_address(info[4][0].split('%', 1)[0])
        if (address.is_private or address.is_loopback or address.is_link_local
                or address.is_reserved or address.is_multicast or address.is_unspecified):
            return False
    return True


def signature(secret, body):
    digest = hmac.new(secret.encode('utf-8'), body, hashlib.sha256).hexdigest()
    return 'sha256=' + digest


class WebhookNotifier:
    def __init__(self, secret, *, post=None, sleep=asyncio.sleep, delays=RETRY_DELAYS,
                 on_attempt=None, allow_private=False, resolve=resolves_publicly):
        self.secret = secret
        self._post = post or self._aiohttp_post
        self.allow_private = allow_private
        self._resolve = resolve
        self._sleep = sleep
        self.delays = tuple(delays)
        self.on_attempt = on_attempt

    @staticmethod
    async def _aiohttp_post(url, body, headers):
        from aiohttp import ClientSession, ClientTimeout
        async with ClientSession(timeout=ClientTimeout(total=15)) as session:
            async with session.post(url, data=body, headers=headers,
                                    allow_redirects=False) as response:
                return response.status

    def payload(self, call):
        return {
            'type': 'call.' + call['status'],
            'call': call,
        }

    async def deliver(self, call):
        url = ((call.get('brief') or {}).get('notify') or {}).get('webhookUrl')
        if not url:
            return None
        validate_webhook_url(url)
        if not self.allow_private and not await self._resolve(url):
            return {'delivered': False, 'attempts': 0, 'error': 'rejected'}
        body = json.dumps(self.payload(call), ensure_ascii=False).encode('utf-8')
        headers = {
            'Content-Type': 'application/json',
            'User-Agent': 'colleague-ai-webhook/1',
            'X-Colleague-Event': 'call.' + call['status'],
            'X-Colleague-Signature': signature(self.secret, body),
        }
        # Outcomes are coarse on purpose: the result is readable by API callers, so
        # it must not become a probe of the receiver's exact responses.
        attempts = (0.0,) + self.delays
        last = None
        for attempt, delay in enumerate(attempts, start=1):
            if delay:
                await self._sleep(delay)
            try:
                status = await self._post(url, body, headers)
            except Exception:
                last = {'delivered': False, 'attempts': attempt, 'error': 'unreachable'}
            else:
                if 200 <= status < 300:
                    return {'delivered': True, 'attempts': attempt}
                if 400 <= status < 500 and status not in (408, 429):
                    return {'delivered': False, 'attempts': attempt, 'error': 'rejected'}
                last = {'delivered': False, 'attempts': attempt, 'error': 'failed'}
            if self.on_attempt:
                self.on_attempt(last)
        return last
