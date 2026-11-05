"""Inside a meeting container: the daemon's way in (/control) and the voice's requests out.

The daemon reaches a running meeting through POST /control on the container's health server,
with the meeting's own token (SMITLINE_CONTROL_TOKEN): notes for the voice (request updates,
send_call_instruction), whether the agent that started the meeting is following it, and stop.
Requests the voice passes on leave through /health, numbered by seq, as captions do.
"""
import asyncio
import hmac
import time

from call_requests import (
    MAX_REQUESTS, NOTE_KINDS, TELL_MAX_WAIT, TELL_QUIET, ClaimWatch, RequestDesk, appends,
    pause_cue, told,
)

MAX_NOTE_TEXT = 4000
# Requests /health carries; the daemon reads it every second, so it never falls this far behind.
HEALTH_REQUESTS = 100
# A meeting on a machine of its own (SMITLINE_MEETING_LAUNCHER) is reached over a network rather
# than the daemon's loopback. There SMITLINE_HEALTH_TOKEN=1 makes /health need the meeting's token
# too, and the archive leaves through GET /archive, since the daemon cannot read that machine's
# disk. SMITLINE_ARCHIVE_GRACE keeps the server up that many seconds for the daemon to fetch it.
MAX_ARCHIVE_GRACE = 600
# An answer to tell (a note's `say`) is cued at the next pause: call_requests.TELL_QUIET,
# TELL_MAX_WAIT and pause_cue, shared with phone calls. In a quiet meeting an answer once sat
# unsaid until someone asked.


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
        # stop(): end the meeting as SIGTERM would; set by the bridge. A daemon on another
        # machine cannot signal the container, so it asks here (type stop) before it takes the
        # archive and deletes the machine.
        self.stop = None
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
                    cue = pause_cue(text)
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
        if payload.get('type') == 'stop':
            if self.stop is None:
                return 409, {'error': 'the meeting cannot be stopped from here'}
            self.stop()
            return 200, {'stopping': True}
        if payload.get('type') != 'note':
            return 422, {'error': 'type must be note, following or stop'}
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


def archive_tarball(directory):
    """A meeting archive's files as a gzipped tar: its top-level regular files only (none for a
    meeting that never had an archive)."""
    import io
    import tarfile
    from pathlib import Path
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode='w:gz') as tar:
        for path in sorted(Path(directory).iterdir()) if directory else ():
            if path.is_file() and not path.is_symlink():
                tar.add(path, arcname=path.name, recursive=False)
    return buffer.getvalue()


def archive_grace(environ):
    """Seconds the server stays up after the meeting for the daemon to fetch the archive."""
    text = str(environ.get('SMITLINE_ARCHIVE_GRACE') or '').strip()
    return min(int(text), MAX_ARCHIVE_GRACE) if text.isdigit() else 0


class ArchiveDoor:
    """GET /archive in the container: the meeting's archive once it is closed, for the daemon."""

    def __init__(self):
        self.directory = None
        self.closed = False
        self.fetched = asyncio.Event()

    def close(self, directory=None):
        """The meeting is over and its archive (None: it had none) is complete."""
        self.directory = directory
        self.closed = True

    async def wait_fetched(self, seconds):
        """Keep the server up until the daemon has the archive, at most `seconds`."""
        if seconds <= 0:
            return False
        try:
            await asyncio.wait_for(self.fetched.wait(), seconds)
            return True
        except asyncio.TimeoutError:
            return False


def health_route(control, public_state, *, token_required=False):
    """GET /health: the meeting's state for the daemon. On a network (token_required) only the
    daemon that started the meeting may read it: it carries live captions and requests."""
    from aiohttp import web

    async def status(request):
        if token_required and not control.authorized(request.headers.get('Authorization')):
            return web.json_response({'error': 'forbidden'}, status=403)
        return web.json_response(public_state(), headers={'Cache-Control': 'no-store'})
    return status


def add_routes(app, control, listening, archive=None):
    """/listen (live audio), /control and, given an ArchiveDoor, /archive on the container's
    health server: for the daemon that started the meeting alone, by its token."""
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

    if archive is not None:
        async def archive_files(request):
            if not control.authorized(request.headers.get('Authorization')):
                return web.json_response({'error': 'forbidden'}, status=403)
            if not archive.closed:
                # The meeting is still writing it; the daemon asks again.
                return web.json_response({'error': 'not_closed'}, status=409,
                                         headers={'Cache-Control': 'no-store'})
            body = await asyncio.to_thread(archive_tarball, archive.directory)
            archive.fetched.set()
            return web.Response(body=body, content_type='application/gzip',
                                headers={'Cache-Control': 'no-store'})
        app.router.add_get('/archive', archive_files)
