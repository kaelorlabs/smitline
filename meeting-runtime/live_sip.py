"""GPT-Live over SIP: call audio flows between the phone provider and OpenAI directly.

Smitline only steers the call: it creates or accepts the session with the
brief as instructions, then attaches a text "sideband" WebSocket for call
progress, transcripts, backend tool calls, and commands, and ends or
transfers the call through REST. See docs/phone.md and
https://developers.openai.com/api/docs/guides/voice-sip.

Two ways to connect a call:
- OpenAI dials out through the provider's SIP trunk (POST /v1/live/sessions with a
  SIP transport). No public URL is needed; OpenAI must enable outbound SIP for the
  organization first, or the request fails with 403 outbound_sip_not_enabled.
- The provider dials the person and bridges the call to OpenAI's SIP address;
  OpenAI announces it with a signed live.transport.incoming webhook, and the
  session is accepted with the same configuration.
"""
import asyncio
import base64
import hashlib
import hmac
import json
import time

from voice_core import LiveSession


API_BASE = 'https://api.openai.com/v1'
ATTACH_URL = 'wss://api.openai.com/v1/live/sessions/{session_id}/attach'
SIP_HOST = 'sip.api.openai.com'
WEBHOOK_TOLERANCE_SECONDS = 300
INCOMING_EVENTS = ('live.transport.incoming', 'live.call.incoming')  # the second is deprecated


class LiveSipError(RuntimeError):
    def __init__(self, status, code, message=''):
        super().__init__(message or code)
        self.status = status
        self.code = code
        self.message = message or code


def sip_session(config, *, accept=False):
    """A primary-WebSocket session config reshaped for SIP.

    SIP negotiates the codec, so audio.format is dropped; accept also needs type "live".
    """
    session = json.loads(json.dumps(config))
    audio = session.get('audio') or {}
    audio.pop('format', None)
    if audio:
        session['audio'] = audio
    else:
        session.pop('audio', None)
    if accept:
        session = {'type': 'live', **session}
    return session


def openai_sip_uri(project_id, headers=None):
    """The address a provider dials to hand a call to GPT-Live; headers travel as X-* SIP headers."""
    from urllib.parse import quote
    uri = f'sip:{project_id}@{SIP_HOST};transport=tls'
    if headers:
        uri += '?' + '&'.join(f'{quote(name)}={quote(str(value))}' for name, value in headers.items())
    return uri


def verify_webhook(raw_body, headers, secret, *, tolerance=WEBHOOK_TOLERANCE_SECONDS, now=None):
    """Check an OpenAI webhook (Standard Webhooks) and return its parsed JSON body.

    The signature is base64(HMAC-SHA256(key, f"{id}.{timestamp}.{body}")), where the key is
    the base64-decoded secret after "whsec_". The header may hold several "v1,<sig>" values.
    """
    lower = {str(name).lower(): value for name, value in headers.items()}
    webhook_id = lower.get('webhook-id')
    timestamp = lower.get('webhook-timestamp')
    signatures = lower.get('webhook-signature')
    if not (webhook_id and timestamp and signatures and secret):
        raise ValueError('missing webhook headers or secret')
    try:
        sent = int(timestamp)
    except ValueError as error:
        raise ValueError('bad webhook timestamp') from error
    current = int(time.time() if now is None else now)
    if abs(current - sent) > tolerance:
        raise ValueError('stale webhook timestamp')
    key = base64.b64decode(secret[6:]) if secret.startswith('whsec_') else secret.encode('utf-8')
    body = raw_body.decode('utf-8') if isinstance(raw_body, bytes) else raw_body
    expected = hmac.new(key, f'{webhook_id}.{timestamp}.{body}'.encode('utf-8'), hashlib.sha256).digest()
    for item in str(signatures).split():
        value = item.split(',', 1)[1] if item.startswith('v1,') else item
        try:
            if hmac.compare_digest(base64.b64decode(value), expected):
                return json.loads(body)
        except (ValueError, TypeError):
            continue
    raise ValueError('invalid webhook signature')


def incoming_call(event):
    """(session_id, {header: value}) from a live.transport.incoming event, or None."""
    if event.get('type') not in INCOMING_EVENTS:
        return None
    data = event.get('data') or {}
    if data.get('type', 'sip') != 'sip' or not data.get('session_id'):
        return None
    headers = {}
    for item in data.get('sip_headers') or ():
        name = str(item.get('name') or '')
        if name and name not in headers:
            headers[name] = str(item.get('value') or '')
    return data['session_id'], headers


class LiveSipClient:
    """REST calls for SIP sessions. `request(method, url, body)` returns (status, json)."""

    def __init__(self, api_key, *, request=None, base=API_BASE):
        if not api_key:
            raise ValueError('an OpenAI API key is required')
        self.api_key = api_key
        self.base = base
        self._request = request or self._aiohttp_request

    async def _aiohttp_request(self, method, url, body=None):
        from aiohttp import ClientSession, ClientTimeout
        headers = {'Authorization': f'Bearer {self.api_key}'}
        if body is not None:
            headers['Content-Type'] = 'application/json'
        async with ClientSession(timeout=ClientTimeout(total=30)) as session:
            async with session.request(method, url, headers=headers,
                                       data=None if body is None else json.dumps(body)) as response:
                text = await response.text()
                try:
                    payload = json.loads(text) if text else {}
                except ValueError:
                    payload = {}
                return response.status, payload

    async def _call(self, method, path, body=None):
        status, payload = await self._request(method, f'{self.base}{path}', body)
        if status >= 400:
            error = (payload or {}).get('error') or {}
            raise LiveSipError(status, error.get('code') or str(status), error.get('message') or '')
        return payload or {}

    async def create_outbound(self, session, *, destination, trunk):
        """Have OpenAI dial `destination` (E.164) through the provider trunk; returns the session id.

        Each request places a new call, so it must never be retried automatically.
        """
        body = {'session': session, 'transport': {'type': 'sip', 'destination': destination,
                                                  'trunk': trunk}}
        payload = await self._call('POST', '/live/sessions', body)
        session_id = (payload.get('session') or {}).get('id')
        if not session_id:
            raise LiveSipError(502, 'no_session_id', 'OpenAI did not return a session id')
        return session_id

    async def accept(self, session_id, session):
        await self._call('POST', f'/live/sessions/{session_id}/accept', {'session': session})

    async def reject(self, session_id, status_code=486):
        await self._call('POST', f'/live/sessions/{session_id}/reject', {'status_code': status_code})

    async def hangup(self, session_id):
        """End the call; a session that is already gone counts as ended."""
        try:
            await self._call('POST', f'/live/sessions/{session_id}/hangup')
        except LiveSipError as error:
            if error.status != 404:
                raise

    async def refer(self, session_id, target_uri):
        await self._call('POST', f'/live/sessions/{session_id}/refer', {'target_uri': target_uri})


class LiveSideband(LiveSession):
    """The text side of a SIP session: events and commands, no audio injection.

    Attaching sends no session.start. Outbound calls replay the last few seconds of
    events on attach, so events are deduplicated by event_id.
    """

    def __init__(self, api_key, session_id, *, connect=None):
        super().__init__(api_key, {}, connect=connect,
                         url=ATTACH_URL.format(session_id=session_id))
        self.session_id = session_id
        self._seen = set()

    async def __aenter__(self):
        self._ws, self._closer = await self._connect(self.url, self.api_key)
        self.started.set()  # the session already exists; commands can go at once
        return self

    async def send_audio(self, audio):
        return False  # SIP carries the audio

    async def events(self):
        async for event in super().events():
            event_id = event.get('event_id')
            if event_id:
                if event_id in self._seen:
                    continue
                self._seen.add(event_id)
            yield event
