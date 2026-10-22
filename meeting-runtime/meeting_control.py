"""Inside a meeting container: the daemon's way in (/control) and the voice's requests out.

The daemon reaches a running meeting through POST /control on the container's health server,
with the meeting's own token (SMITLINE_CONTROL_TOKEN): notes for the voice (request updates,
send_call_instruction) and whether the agent that started the meeting is following it.
Requests the voice passes on leave through /health, numbered by seq, as captions do.
"""
import hmac

from call_requests import MAX_REQUESTS, NOTE_KINDS, ClaimWatch, RequestDesk, appends

MAX_NOTE_TEXT = 4000
# Requests /health carries; the daemon reads it every second, so it never falls this far behind.
HEALTH_REQUESTS = 100


class MeetingControl:
    def __init__(self, token, state, *, owner, can_do=None):
        self.token = token or ''
        self.state = state
        # send(kind, text): append to the GPT-Live session; set while the voice is live.
        self.send = None
        self.adapter = None
        # record(kind, **data): the meeting archive's event log, when there is one.
        self.record = None
        # Until the daemon says otherwise, nobody is known to be following the meeting.
        self.following = False
        self.desk = RequestDesk(owner, limit=MAX_REQUESTS['meeting']) if can_do else None
        # Requests the voice says it passed on without asking its backend (call_requests.py).
        self.claims = ClaimWatch(self.desk) if self.desk is not None else None

    def authorized(self, header):
        """Only the daemon that started this meeting holds its token."""
        if not self.token or not isinstance(header, str) or not header.startswith('Bearer '):
            return False
        return hmac.compare_digest(header[len('Bearer '):].encode(), self.token.encode())

    def ask(self, arguments, *, asked_by='', context=()):
        """An ask_agent call from the backend: the tool result; the message goes out on /health."""
        if self.claims is not None:
            self.claims.tool_called()
        message, output = self.desk.handle(arguments, asked_by=asked_by or '', context=context,
                                           following=self.following)
        if message is not None:
            self.state['requests'] = self.desk.messages[-HEALTH_REQUESTS:]
        return output

    def heard(self, speaker, delta):
        """What was said, for the safety net (speaker: 'agent', or anyone in the meeting)."""
        if self.claims is not None:
            self.claims.heard(speaker, delta)

    def claim_pending(self):
        return self.claims is not None and self.claims.claimed_at is not None

    def claims_due(self, *, asked_by=''):
        """A request the voice said it passed on, when its backend never did; out on /health."""
        if self.claims is None:
            return None
        message = self.claims.due(asked_by=asked_by, following=self.following)
        if message is not None:
            self.state['requests'] = self.desk.messages[-HEALTH_REQUESTS:]
        return message

    async def handle(self, payload):
        """One message from the daemon: (HTTP status, body)."""
        if not isinstance(payload, dict):
            return 422, {'error': 'a control message is an object'}
        if payload.get('type') == 'following':
            self.following = payload.get('value') is True
            return 200, {'following': self.following}
        if payload.get('type') != 'note':
            return 422, {'error': 'type must be note or following'}
        kind, text = payload.get('kind'), payload.get('text')
        if kind not in NOTE_KINDS or not isinstance(text, str) or not text.strip():
            return 422, {'error': 'a note has a kind and text'}
        note = {'kind': kind, 'text': text[:MAX_NOTE_TEXT], 'request': payload.get('request')}
        for name in ('details', 'chat'):
            if isinstance(payload.get(name), str) and payload[name].strip():
                note[name] = payload[name][:MAX_NOTE_TEXT]
        if self.send is None:
            return 409, {'error': 'the voice is not live'}
        chat_posted = False
        if note.get('chat') and self.adapter is not None and self.state.get('chatAvailable'):
            try:
                await self.adapter.send_chat_message(note['chat'])
                chat_posted = True
            except Exception as error:
                # The voice gets the details instead; say why, so the adapter can be fixed.
                if self.record is not None:
                    self.record('chat_failed', error=f'{type(error).__name__}: {str(error)[:200]}')
        delivered = True
        for append_kind, append_text in appends(note, chat_posted=chat_posted):
            delivered = bool(await self.send(append_kind, append_text)) and delivered
        if payload.get('closes') and self.desk is not None:
            self.desk.close(payload.get('request'))
        elif kind == 'instructions' and self.claims is not None and payload.get('request'):
            self.claims.awaiting = payload.get('request')  # their answer belongs to this request
        return 200, {'delivered': delivered, 'chat': chat_posted}


def add_routes(app, control, listening):
    """/listen (live audio) and /control on the container's health server: for the daemon that
    started the meeting alone, by its token."""
    from aiohttp import web
    from listening import ndjson

    async def listen(request):
        if not control.authorized(request.headers.get('Authorization')):
            return web.json_response({'error': 'forbidden'}, status=403)
        response = web.StreamResponse(headers={'Content-Type': 'application/x-ndjson',
                                               'Cache-Control': 'no-store'})
        await response.prepare(request)
        async for chunk in listening.chunks():
            await response.write(ndjson(chunk))
        return response

    async def control_message(request):
        if not control.authorized(request.headers.get('Authorization')):
            return web.json_response({'error': 'forbidden'}, status=403)
        try:
            payload = await request.json()
        except ValueError:
            payload = None
        status, body = await control.handle(payload)
        return web.json_response(body, status=status, headers={'Cache-Control': 'no-store'})

    app.router.add_get('/listen', listen)
    app.router.add_post('/control', control_message)
