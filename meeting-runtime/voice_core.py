"""Transport-neutral GPT-Live session: connect, start, stream audio, append context, close.

A "line" (phone, meeting browser) owns the audio device on one side; this
module owns the GPT-Live WebSocket on the other. Event names follow the
GPT-Live WebSocket reference.
"""
import asyncio
import base64
import json
from uuid import uuid4

from startup_input import clip_tokens


LIVE_URL = 'wss://api.openai.com/v1/live/sessions'
LIVE_MODEL = 'gpt-live-1'
PCM24 = {'type': 'audio/pcm', 'rate': 24000}
PCMU8 = {'type': 'audio/pcmu', 'rate': 8000}
DEFAULT_VOICE = 'marin'
# GPT-Live output voices as documented for gpt-live-1. The text-to-speech API
# uses a different list, so these are not interchangeable.
GPT_LIVE_VOICES = (
    'marin', 'vesper', 'quartz', 'ripple', 'willow', 'stone', 'gleam', 'meridian', 'bossa',
    'tempo', 'beacon', 'delta', 'cinder',
)
APPEND_KINDS = ('session.thinking.append', 'session.commentary.append',
                'session.instructions.append')


class LiveSessionError(RuntimeError):
    def __init__(self, code, message=''):
        super().__init__(message or code)
        self.code = code


def append_event(kind, content, delegation_id=None):
    """Context appends take plain text up to 500 tokens and a delegation id or null."""
    if kind not in APPEND_KINDS:
        raise ValueError(f'unsupported append kind {kind}')
    text = clip_tokens(str(content or '').strip(), 500)
    if not text:
        return None
    return {
        'type': kind,
        'event_id': kind.split('.')[1] + '-' + uuid4().hex[:12],
        'delegation_id': delegation_id,
        'content': text,
    }


def session_config(*, instructions, audio_format=PCM24, voice=None, delegation=None,
                   seed_input=None):
    config = {
        'model': LIVE_MODEL,
        'store': False,
        'instructions': instructions,
        'audio': {'format': dict(audio_format)},
        'delegation': delegation or {'type': 'client'},
    }
    if voice:
        config['audio']['output'] = {'voice': voice}
    if seed_input:
        config['input'] = seed_input
    return config


async def _aiohttp_connect(url, api_key):
    from aiohttp import ClientSession, ClientTimeout
    session = ClientSession(timeout=ClientTimeout(total=None, sock_connect=30))
    try:
        ws = await session.ws_connect(url, headers={'Authorization': f'Bearer {api_key}'},
                                      max_msg_size=8 * 1024 * 1024)
    except Exception:
        await session.close()
        raise
    return ws, session.close


class LiveSession:
    """One GPT-Live WebSocket session.

    Use as an async context manager; iterate events() for server events.
    Audio passes through base64-encoded in the session's configured format.
    """

    def __init__(self, api_key, config, *, connect=None, url=LIVE_URL):
        self.api_key = api_key
        self.config = config
        self.url = url
        self._connect = connect or _aiohttp_connect
        self._ws = None
        self._closer = None
        self.started = asyncio.Event()
        self.closed = asyncio.Event()
        self.session_id = None
        self.usage_seconds = 0
        self.close_reason = None
        self._send_lock = asyncio.Lock()

    async def __aenter__(self):
        self._ws, self._closer = await self._connect(self.url, self.api_key)
        await self.send({'type': 'session.start', 'session': self.config})
        return self

    async def __aexit__(self, *exc):
        await self.shutdown()

    async def send(self, payload):
        if self._ws is None or self.closed.is_set():
            return False
        async with self._send_lock:
            await self._ws.send_json(payload)
        return True

    async def send_audio(self, audio):
        """Accept raw bytes or an already base64-encoded string (Twilio passes base64)."""
        if not self.started.is_set():
            return False
        encoded = audio if isinstance(audio, str) else base64.b64encode(audio).decode('ascii')
        if not encoded:
            return False
        return await self.send({'type': 'session.input_audio.append', 'audio': encoded})

    async def append(self, kind, content, delegation_id=None):
        payload = append_event(kind, content, delegation_id)
        if payload is None:
            return False
        return await self.send(payload)

    async def submit_function_output(self, call_id, output, *, delegation_id=None):
        """Responses delegation: return a backend tool result and continue the response."""
        item = {'type': 'response.item.create', 'event_id': 'tool-' + uuid4().hex[:12],
                'item': {'type': 'function_call_output', 'call_id': call_id,
                         'output': output if isinstance(output, str) else json.dumps(output)}}
        more = {'type': 'response.create', 'event_id': 'continue-' + uuid4().hex[:12]}
        if delegation_id:
            item['delegation_id'] = delegation_id
            more['delegation_id'] = delegation_id
        await self.send(item)
        await self.send(more)

    async def close(self):
        """Begin graceful shutdown; session.closed arrives through events()."""
        await self.send({'type': 'session.close'})

    async def shutdown(self):
        self.closed.set()
        ws, self._ws = self._ws, None
        if ws is not None:
            try:
                await ws.close()
            except Exception:
                pass
        if self._closer is not None:
            closer, self._closer = self._closer, None
            try:
                await closer()
            except Exception:
                pass

    async def events(self):
        """Yield parsed server events; track startup, usage, and close."""
        from aiohttp import WSMsgType
        ws = self._ws
        if ws is None:
            return
        async for message in ws:
            if message.type != WSMsgType.TEXT:
                if message.type in (WSMsgType.CLOSED, WSMsgType.ERROR):
                    break
                continue
            try:
                event = json.loads(message.data)
            except ValueError:
                continue
            kind = event.get('type')
            if kind == 'session.started':
                self.session_id = (event.get('session') or {}).get('id')
                self.started.set()
            elif kind == 'session.usage.updated':
                self.usage_seconds = (event.get('usage') or {}).get('seconds', self.usage_seconds)
            elif kind == 'session.closed':
                self.usage_seconds = (event.get('usage') or {}).get('seconds', self.usage_seconds)
                self.close_reason = event.get('reason')
                yield event
                self.closed.set()
                return
            elif kind == 'error' and not self.started.is_set():
                error = event.get('error') or {}
                raise LiveSessionError(error.get('code') or 'live_error', error.get('message') or '')
            yield event
        self.closed.set()


def function_call_from(event):
    """Extract a completed backend function call from a response.event envelope."""
    if event.get('type') != 'response.event':
        return None
    inner = event.get('event') or {}
    item = inner.get('item') or {}
    if inner.get('type') != 'response.output_item.done' or item.get('type') != 'function_call':
        return None
    try:
        arguments = json.loads(item.get('arguments') or '{}')
    except ValueError:
        arguments = {}
    return {
        'delegation_id': event.get('delegation_id'),
        'call_id': item.get('call_id'),
        'name': item.get('name'),
        'arguments': arguments if isinstance(arguments, dict) else {},
    }


def backend_usage_from(event):
    """Backend token usage arrives in nested response.completed events."""
    if event.get('type') != 'response.event':
        return None
    inner = event.get('event') or {}
    if inner.get('type') != 'response.completed':
        return None
    response = inner.get('response') or {}
    usage = response.get('usage') or {}
    details = usage.get('input_tokens_details') or {}
    searches = sum(1 for item in response.get('output') or ()
                   if isinstance(item, dict) and item.get('type') == 'web_search_call')
    return {'input': int(usage.get('input_tokens') or 0),
            'cached': int(details.get('cached_tokens') or 0),
            'output': int(usage.get('output_tokens') or 0),
            'webSearches': searches}
