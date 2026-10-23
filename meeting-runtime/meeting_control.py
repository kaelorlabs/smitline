"""Inside a meeting container: the daemon's way in (/control) and the voice's requests out.

The daemon reaches a running meeting through POST /control on the container's health server,
with the meeting's own token (SMITLINE_CONTROL_TOKEN): notes for the voice (request updates,
send_call_instruction) and whether the agent that started the meeting is following it.
Requests the voice passes on leave through /health, numbered by seq, as captions do.
"""
import asyncio
import hmac
import time

from call_requests import (
    MAX_REQUESTS, NOTE_KINDS, WHEN_TO_TELL, ClaimWatch, RequestDesk, appends, told,
)

MAX_NOTE_TEXT = 4000
# Requests /health carries; the daemon reads it every second, so it never falls this far behind.
HEALTH_REQUESTS = 100
# GPT-Live speaks when prompted: an instructions note alone waits until someone talks to it, so
# in a quiet meeting an answer sat unsaid until someone asked. An answer to tell (a note's `say`)
# is cued once nobody, the voice included, has been heard for TELL_QUIET seconds: at once when
# the room is quiet, at a real gap when people are talking. Without one in TELL_MAX_WAIT, the
# note's own rule stands (first thing the next time it speaks).
TELL_QUIET = 2.5
TELL_MAX_WAIT = 120.0


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
        # quiet(): seconds since anyone, the voice included, was last heard; set by the bridge.
        self.quiet = None
        self.clock = time.monotonic
        self.tell_quiet = TELL_QUIET
        self.tell_max_wait = TELL_MAX_WAIT
        self._said = {}  # request id -> what the voice has said since its answer came
        self._telling = set()

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
        if speaker == 'agent':
            for request_id in self._said:
                self._said[request_id] += delta

    async def tell_at_pause(self, request_id, say, text, poll=0.2):
        """Cue the voice to say an answer at the next pause, unless it has said it by then."""
        started = self.clock()
        self._said.setdefault(request_id, '')
        try:
            while self.clock() - started < self.tell_max_wait:
                if told(say, self._said.get(request_id, '')):
                    return False
                if self.send is None:
                    return False
                if self.quiet() >= self.tell_quiet:
                    cue = (f'Now is a pause: tell them this now, in a sentence or two and in your '
                           f'own words, unless you have just said it. {text.replace(WHEN_TO_TELL, "").strip()} '
                           'Then go back to listening.')
                    sent = bool(await self.send('commentary', cue))
                    if self.record is not None:
                        self.record('answer_cued', request=request_id, sent=sent)
                    return sent
                await asyncio.sleep(poll)
            return False
        finally:
            self._said.pop(request_id, None)
            self._telling.discard(request_id)

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
        say, request_id = payload.get('say'), payload.get('request')
        if (delivered and isinstance(say, str) and say.strip() and isinstance(request_id, str)
                and self.quiet is not None and request_id not in self._telling):
            self._telling.add(request_id)
            self._said[request_id] = ''  # from now on, what the voice says counts
            asyncio.get_running_loop().create_task(
                self.tell_at_pause(request_id, say[:MAX_NOTE_TEXT], note['text']))
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
