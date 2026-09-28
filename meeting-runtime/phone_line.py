"""Phone calls: Twilio Media Streams on one side, a GPT-Live session on the other.

Twilio sends and receives G.711 mu-law at 8 kHz, which GPT-Live accepts
directly (audio/pcmu), so audio passes through without conversion. GPT-Live
owns turn-taking and barge-in. This module only paces output so that an
interruption leaves little buffered speech, watches for the end of the call,
and turns backend tool calls into telephony actions.
"""
import asyncio
import base64
import json
import secrets
import time

from call_brief import CallBrief, normalize_phone, validate_webhook_url
from call_hooks import CallRefused
from call_hooks import LineNotReady as NotReady
from phone_prompts import (
    delegation_config, disclosure_reminder, mentions_ai, opening_cue, voice_instructions,
)
from twilio_client import TwilioClient, dial_twiml, stream_twiml
from voice_core import (
    DEFAULT_VOICE, PCMU8, LiveSession, backend_usage_from, function_call_from, session_config,
)


OUTPUT_LEAD_SECONDS = 0.3
DRAIN_TIMEOUT = 8.0
OPENING_DELAY = 2.5
SILENCE_PROMPT_SECONDS = 40.0
SILENCE_END_SECONDS = 65.0
WRAP_UP_SECONDS = 60.0
RING_SECONDS = 30
# How long to wait for the media stream or a final status after dialing, before
# asking Twilio directly. Covers the ring time plus a lost callback.
CONNECT_TIMEOUT = RING_SECONDS + 45.0
LIVE_CONNECT_TIMEOUT = 20.0
# Extra time past maxMinutes before a connected call is ended regardless.
OVERRUN_SECONDS = 180.0
# Characters of the agent's first utterance to hear before judging the disclosure.
DISCLOSURE_WINDOW = 120
TWILIO_TERMINAL = {
    'completed': 'hangup', 'busy': 'busy', 'no-answer': 'no_answer', 'failed': 'error',
    'canceled': 'canceled',
}


class UtteranceJoiner:
    """Join GPT-Live transcript fragments into readable lines per speaker."""

    def __init__(self, gap_ms=1500):
        self.gap_ms = gap_ms
        self.speaker = None
        self.parts = []
        self.last_end = None

    def add(self, speaker, text, start_ms=None, end_ms=None):
        flushed = []
        gap = (start_ms is not None and self.last_end is not None
               and start_ms - self.last_end > self.gap_ms)
        if self.parts and (speaker != self.speaker or gap):
            flushed = self.flush()
        self.speaker = speaker
        self.parts.append(text)
        if end_ms is not None:
            self.last_end = end_ms
        return flushed

    def flush(self):
        text = ''.join(self.parts).strip()
        speaker = self.speaker
        self.parts = []
        return [(speaker, ' '.join(text.split()))] if text else []


class OutputPacer:
    """Send model audio to Twilio no more than a short lead ahead of playback."""

    def __init__(self, send, *, lead=OUTPUT_LEAD_SECONDS, clock=time.monotonic,
                 sleep=asyncio.sleep, bytes_per_second=8000):
        self._send = send
        self.lead = lead
        self.clock = clock
        self.sleep = sleep
        self.bytes_per_second = bytes_per_second
        self.queue = asyncio.Queue()
        self.play_until = 0.0
        self.sent_marks = 0
        self.played_marks = 0
        self._drained = asyncio.Event()
        self._drained.set()

    def offer(self, payload):
        self._drained.clear()
        self.queue.put_nowait(payload)

    def mark_played(self, name):
        try:
            self.played_marks = max(self.played_marks, int(str(name).rsplit('-', 1)[-1]))
        except ValueError:
            return
        if self.played_marks >= self.sent_marks and self.queue.empty():
            self._drained.set()

    async def drained(self, timeout=DRAIN_TIMEOUT):
        try:
            await asyncio.wait_for(self._drained.wait(), timeout)
            return True
        except asyncio.TimeoutError:
            return False

    async def run(self):
        while True:
            payload = await self.queue.get()
            seconds = len(base64.b64decode(payload)) / self.bytes_per_second
            now = self.clock()
            ahead = self.play_until - now
            if ahead > self.lead:
                await self.sleep(ahead - self.lead)
                now = self.clock()
            await self._send({'event': 'media', 'media': {'payload': payload}})
            self.sent_marks += 1
            await self._send({'event': 'mark', 'mark': {'name': f'out-{self.sent_marks}'}})
            self.play_until = max(now, self.play_until) + seconds


def _error_code(error):
    return getattr(error, 'code', None) or type(error).__name__


class PhoneSession:
    """One phone call bridged to GPT-Live."""

    def __init__(self, line, ctx, *, api_key, twilio, from_number, token, inbound=False,
                 recording=False, owner_phone=None):
        self.line = line
        self.ctx = ctx
        self.brief = ctx.brief
        self.api_key = api_key
        self.twilio = twilio
        self.from_number = from_number
        self.token = token
        self.inbound = inbound
        self.recording = recording
        self.owner_phone = owner_phone
        self.call_sid = None
        self.stream_sid = None
        self.ws = None
        self.live = None
        self.pacer = None
        self.joiner = UtteranceJoiner()
        self.connected = asyncio.Event()
        self.finished = asyncio.Event()
        self.canceled = False
        self.end_reason = None
        self.error_detail = None
        self.phone_seconds = None
        self.backend_tokens = {'input': 0, 'output': 0}
        self.disclosure_checked = False
        self.heard_other = asyncio.Event()
        self.last_activity = None
        self.started_at = None
        self._hanging_up = False
        self._closing_stream = False
        self._tasks = set()
        self._hangups = set()
        self._pending_hangup = None

    # Configuration -------------------------------------------------------

    def config(self):
        env = self.line.environ()
        return session_config(
            instructions=voice_instructions(self.brief, inbound=self.inbound,
                                            recording=self.recording),
            audio_format=PCMU8,
            voice=self.brief.voice or env.get('COLLEAGUE_VOICE') or DEFAULT_VOICE,
            delegation=delegation_config(
                self.brief, model=env.get('COLLEAGUE_PHONE_BACKEND_MODEL'),
                web_search=env.get('COLLEAGUE_PHONE_WEB_SEARCH') == '1', inbound=self.inbound),
        )

    # Bookkeeping ---------------------------------------------------------

    def _spawn(self, coroutine):
        task = asyncio.create_task(coroutine)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    def _start_hangup(self, reason):
        task = asyncio.create_task(self.hangup(reason))
        self._hangups.add(task)
        task.add_done_callback(self._hangups.discard)
        return task

    def fail(self, detail):
        """Record why the call broke; the result says so instead of 'nobody spoke'."""
        if self.end_reason in (None, 'hangup', 'remote_hangup'):
            self.end_reason = 'error'
        if self.error_detail is None:
            self.error_detail = detail
            self.ctx.event('call.error', detail=detail)

    # Twilio side ---------------------------------------------------------

    async def _twilio_send(self, message):
        if self.ws is None or self._closing_stream:
            return
        message = dict(message, streamSid=self.stream_sid)
        try:
            await self.ws.send_str(json.dumps(message))
        except Exception:
            pass

    async def run(self, ws, start):
        """Bridge until the call ends; `start` is Twilio's start event."""
        self.ws = ws
        self.stream_sid = start.get('streamSid')
        self.call_sid = self.call_sid or start.get('callSid')
        self.started_at = time.monotonic()
        self.last_activity = self.started_at
        self.connected.set()
        self.ctx.set_status('in_progress')
        self.pacer = OutputPacer(self._twilio_send)
        manager = self.line.live_factory(self.api_key, self.config())
        try:
            try:
                live = await asyncio.wait_for(manager.__aenter__(), LIVE_CONNECT_TIMEOUT)
            except Exception as error:
                self.fail(f'The voice session could not start ({_error_code(error)}).')
                await self.hangup('error')
                return
            try:
                await self._bridge(live)
            finally:
                await manager.__aexit__(None, None, None)
        finally:
            for speaker, text in self.joiner.flush():
                self.ctx.add_transcript(speaker, text)
                if speaker == 'agent':
                    self._check_disclosure(text, final=True)
            for task in list(self._tasks):
                task.cancel()
            if self._hangups:
                await asyncio.wait(list(self._hangups), timeout=5)
            self.finished.set()

    async def _bridge(self, live):
        self.live = live
        reader = asyncio.create_task(self._read_live(), name='live-events')
        ending = [
            asyncio.create_task(self._read_twilio(), name='twilio-in'),
            asyncio.create_task(self._watch(), name='call-watchdog'),
            reader,
        ]
        helpers = [
            asyncio.create_task(self.pacer.run(), name='twilio-out'),
            asyncio.create_task(self._open(), name='call-opening'),
        ]
        done, _pending = await asyncio.wait(ending, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            if not task.cancelled() and task.exception():
                self.fail(f'The call broke ({_error_code(task.exception())}).')
        if not reader.done():
            # Close gracefully so session.closed reports final usage.
            await live.close()
            try:
                await asyncio.wait_for(asyncio.shield(reader), 5.0)
            except (asyncio.TimeoutError, Exception):
                pass
        for task in ending + helpers:
            task.cancel()
        await asyncio.gather(*ending, *helpers, return_exceptions=True)
        await self._close_stream()
        if self.error_detail and self.call_sid and not self._hanging_up:
            # The voice side failed while the caller was still on the line.
            await self.hangup('error')

    async def _read_twilio(self):
        from aiohttp import WSMsgType
        async for message in self.ws:
            if message.type != WSMsgType.TEXT:
                if message.type in (WSMsgType.CLOSE, WSMsgType.CLOSING, WSMsgType.CLOSED,
                                    WSMsgType.ERROR):
                    break
                continue
            try:
                data = json.loads(message.data)
            except ValueError:
                continue
            event = data.get('event')
            if event == 'media':
                media = data.get('media') or {}
                if media.get('track', 'inbound') == 'inbound' and media.get('payload'):
                    await self.live.send_audio(media['payload'])
            elif event == 'mark':
                self.pacer.mark_played((data.get('mark') or {}).get('name'))
            elif event == 'stop':
                break
        if self.end_reason is None:
            self.end_reason = 'remote_hangup'

    # GPT-Live side -------------------------------------------------------

    async def _read_live(self):
        async for event in self.live.events():
            kind = event.get('type')
            if kind == 'session.output_audio.delta':
                self.last_activity = time.monotonic()
                self.pacer.offer(event['delta'])
            elif kind in ('session.input_transcript.delta', 'session.output_transcript.delta'):
                self._transcript(event)
            elif kind == 'response.event':
                usage = backend_usage_from(event)
                if usage:
                    self.backend_tokens['input'] += usage['input']
                    self.backend_tokens['output'] += usage['output']
                call = function_call_from(event)
                if call and call['name'] == 'end_call':
                    # Hanging up needs no tool result or further backend turn.
                    reason = call['arguments'].get('reason')
                    self.ctx.event('call.end_requested', reason=reason or 'completed')
                    self._start_hangup('voicemail' if reason == 'voicemail_left' else 'hangup')
            elif kind == 'error':
                error = event.get('error') or {}
                self.ctx.event('call.voice_error', code=error.get('code') or 'live_error')
            elif kind == 'session.closed':
                if event.get('reason') in ('content', 'connection_lost', 'expired'):
                    self.fail(f'The voice session closed ({event.get("reason")}).')
                elif self.end_reason is None:
                    self.end_reason = 'hangup'
                return
        if self.end_reason is None:
            self.ctx.event('call.voice_error', code='connection_lost')
            self.fail('The voice connection was lost during the call.')

    def _transcript(self, event):
        self.last_activity = time.monotonic()
        source = 'other' if 'input_transcript' in event['type'] else 'agent'
        if source == 'other' and (event.get('delta') or '').strip():
            self.heard_other.set()
        for speaker, text in self.joiner.add(source, event.get('delta') or '',
                                             event.get('start_ms'), event.get('end_ms')):
            self.ctx.add_transcript(speaker, text)
            if speaker == 'agent':
                self._check_disclosure(text, final=True)
        if source == 'agent' and self.joiner.speaker == 'agent':
            self._check_disclosure(''.join(self.joiner.parts))

    def _check_disclosure(self, text, *, final=False):
        """Decide once, early in the agent's first utterance, whether it disclosed being an AI.

        GPT-Live cannot be forced to say a fixed sentence, so the opening is checked
        as it is spoken; a miss triggers an immediate instruction to disclose.
        """
        if self.disclosure_checked or self.inbound:
            return
        verified = mentions_ai(text)
        if not verified and not final and len(text.strip()) < DISCLOSURE_WINDOW:
            return
        self.disclosure_checked = True
        self.ctx.event('call.disclosure', verified=verified)
        if not verified and self.live is not None:
            self._spawn(self.live.append(
                'session.instructions.append', disclosure_reminder(self.brief)))

    async def _open(self):
        """Prompt the opening once the other side speaks, or after a short pause."""
        try:
            await asyncio.wait_for(self.heard_other.wait(), OPENING_DELAY)
        except asyncio.TimeoutError:
            pass
        await self.live.started.wait()
        await self.live.append('session.commentary.append',
                               opening_cue(self.brief, inbound=self.inbound))
        await asyncio.Event().wait()

    async def _watch(self):
        limit = self.brief.max_minutes * 60
        warned = prompted = False
        while True:
            await asyncio.sleep(1)
            now = time.monotonic()
            elapsed = now - self.started_at
            if not warned and elapsed >= max(limit - WRAP_UP_SECONDS, limit * 0.8):
                warned = True
                await self.live.append('session.commentary.append',
                                       'Time is almost up. Wrap up politely and say goodbye.')
            if elapsed >= limit:
                await self.hangup('max_duration')
                return
            quiet = now - self.last_activity
            if not prompted and quiet >= SILENCE_PROMPT_SECONDS:
                prompted = True
                await self.live.append('session.commentary.append',
                                       'Nobody has spoken for a while. Ask if they are still there.')
            elif quiet < SILENCE_PROMPT_SECONDS:
                prompted = False
            if quiet >= SILENCE_END_SECONDS:
                await self.hangup('hangup')
                return

    # Actions -------------------------------------------------------------

    async def hangup(self, reason):
        if self._hanging_up:
            return
        self._hanging_up = True
        if self.end_reason is None:
            self.end_reason = reason
        if self.pacer is not None:
            await self.pacer.drained()
        await self._close_stream()
        if self.call_sid:
            try:
                await self.twilio.update_call(self.call_sid, status='completed')
            except Exception:
                pass

    async def _close_stream(self):
        self._closing_stream = True
        if self.ws is not None:
            try:
                await self.ws.close()
            except Exception:
                pass

    async def voicemail(self):
        if self.end_reason is None:
            self.end_reason = 'voicemail'
        self.ctx.event('call.voicemail')
        if self.live is not None:
            await self.live.append(
                'session.commentary.append',
                'You reached voicemail and the beep has sounded. Leave a short message now: the '
                'disclosure, who it is for, and why you called, without private details. Then '
                'end the call.')

    async def instruct(self, text):
        if self.live is None:
            return False
        return await self.live.append('session.instructions.append', text)

    async def wrap_up(self):
        if self.live is None:
            return False
        await self.live.append('session.commentary.append',
                               'You need to end the call now. Thank them and say goodbye.')
        if self._pending_hangup is None:
            self._pending_hangup = self._spawn(self._delayed_hangup('hangup', 12.0))
        return True

    async def _delayed_hangup(self, reason, seconds):
        await asyncio.sleep(seconds)
        await self.hangup(reason)

    async def transfer(self):
        if not self.owner_phone:
            raise NotReady('Set COLLEAGUE_OWNER_PHONE to transfer calls to your phone.')
        if self._pending_hangup is not None:
            self._pending_hangup.cancel()
            self._pending_hangup = None
        if self.live is not None:
            await self.live.append(
                'session.commentary.append',
                f'Tell them briefly that you are connecting them with {self.brief.on_behalf_of} now.')
        if self.pacer is not None:
            await asyncio.sleep(1.0)
            await self.pacer.drained(6.0)
        await self.twilio.update_call(self.call_sid,
                                      twiml=dial_twiml(self.owner_phone, self.from_number))
        self.end_reason = 'transferred'
        self.ctx.event('call.transferred')
        return {'transferred': True}


class PhoneLine:
    channel = 'phone'

    def __init__(self, *, public_url, environ, twilio_factory=None, live_factory=None,
                 status_grace=5.0, public_available=None, connect_timeout=CONNECT_TIMEOUT):
        """public_url: async callable returning the https base URL Twilio can reach."""
        self._public_url = public_url
        self._public_available = public_available
        self.environ = environ
        self.twilio_factory = twilio_factory or (lambda creds: TwilioClient(
            creds['accountSid'], creds['authToken']))
        self.live_factory = live_factory or (lambda key, config: LiveSession(key, config))
        self.status_grace = status_grace
        self.connect_timeout = connect_timeout
        self.gateway_ready = True
        self.sessions = {}

    def ready(self, hooks, owner, brief):
        hooks.credentials(owner, 'openai')
        hooks.credentials(owner, 'twilio')
        if not self.gateway_ready:
            raise CallRefused('gateway_unavailable', 'The phone gateway could not start; see the '
                              'daemon log. Set COLLEAGUE_GATEWAY_PORT to a free port.')
        if self._public_available is not None and not self._public_available():
            raise CallRefused('no_public_url', 'Twilio cannot reach this computer. Set '
                              'COLLEAGUE_PUBLIC_URL, or install cloudflared or Docker for a '
                              'quick tunnel.')

    def _recording(self):
        return self.environ().get('COLLEAGUE_RECORD_CALLS') == '1'

    def session(self, call_id):
        return self.sessions.get(call_id)

    def claim(self, call_id, token):
        """Match a Twilio stream to the session waiting for it."""
        session = self.sessions.get(call_id)
        if session is None or not secrets.compare_digest(session.token, token or ''):
            return None
        if session.connected.is_set() or session.canceled or session.finished.is_set():
            return None
        return session

    async def start(self, ctx):
        openai = ctx.credentials('openai')
        twilio_creds = ctx.credentials('twilio')
        env = self.environ()
        twilio = self.twilio_factory(twilio_creds)
        session = PhoneSession(
            self, ctx, api_key=openai['apiKey'], twilio=twilio,
            from_number=env.get('COLLEAGUE_CALLER_ID') or twilio_creds['fromNumber'],
            token=secrets.token_urlsafe(24), recording=self._recording(),
            owner_phone=env.get('COLLEAGUE_OWNER_PHONE'))
        self.sessions[ctx.call_id] = session
        try:
            ctx.set_status('connecting')
            base = (await self._public_url()).rstrip('/')
            if session.canceled:
                return await self._finish(ctx, session)
            stream_url = 'wss://' + base.split('://', 1)[1] + '/twilio/media'
            twiml = stream_twiml(stream_url, {'callId': ctx.call_id, 'token': session.token})
            created = await twilio.create_call(
                to=ctx.brief.to, from_=session.from_number, twiml=twiml,
                status_callback=f'{base}/twilio/status/{ctx.call_id}',
                amd_callback=f'{base}/twilio/amd/{ctx.call_id}',
                record=session.recording, timeout=RING_SECONDS,
                time_limit=ctx.brief.max_minutes * 60 + 90)
            session.call_sid = created.get('sid')
            ctx.link(providerCallSid=session.call_sid, fromNumber=session.from_number)
            if session.canceled:
                await self._cancel_remote(session)
                return await self._finish(ctx, session)
            await self._await_call(ctx, session)
            return await self._finish(ctx, session)
        finally:
            self.sessions.pop(ctx.call_id, None)

    async def _await_call(self, ctx, session):
        """Wait for the call to end, but never forever."""
        waits = [asyncio.create_task(session.connected.wait()),
                 asyncio.create_task(session.finished.wait())]
        done, pending = await asyncio.wait(waits, timeout=self.connect_timeout,
                                           return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        if not done:
            await self._resolve_silent_call(session)
            return
        limit = ctx.brief.max_minutes * 60 + OVERRUN_SECONDS
        try:
            await asyncio.wait_for(session.finished.wait(), limit)
        except asyncio.TimeoutError:
            session.fail('The call ran past its time limit and was ended.')
            await session.hangup('error')
            try:
                await asyncio.wait_for(session.finished.wait(), 15)
            except asyncio.TimeoutError:
                session.finished.set()

    async def _resolve_silent_call(self, session):
        """No stream and no final status: ask Twilio, then end the call either way."""
        status = None
        try:
            status = (await session.twilio.get_call(session.call_sid)).get('status')
        except Exception:
            pass
        if status in TWILIO_TERMINAL and status != 'failed':
            session.end_reason = session.end_reason or TWILIO_TERMINAL[status]
        elif status == 'in-progress':
            session.fail('The call was answered, but Twilio could not reach the phone gateway.')
            await self._hang_up_remote(session)
        else:
            session.fail('Twilio sent no call status; check that the public address works.')
            await self._cancel_remote(session)
        session.finished.set()

    async def _cancel_remote(self, session):
        if not session.call_sid:
            return
        try:
            await session.twilio.update_call(session.call_sid, status='canceled')
        except Exception:
            await self._hang_up_remote(session)

    async def _hang_up_remote(self, session):
        if not session.call_sid:
            return
        try:
            await session.twilio.update_call(session.call_sid, status='completed')
        except Exception:
            pass

    async def _finish(self, ctx, session):
        if session.phone_seconds is None and session.connected.is_set():
            try:
                await asyncio.wait_for(self._await_duration(session), self.status_grace)
            except asyncio.TimeoutError:
                pass
        reason = session.end_reason or ('canceled' if session.canceled else 'hangup')
        error = session.error_detail if reason == 'error' else None
        await ctx.finish(reason, usage=self._usage(session), error=error)

    async def start_inbound(self, ctx, *, call_sid, token):
        openai = ctx.credentials('openai')
        twilio_creds = ctx.credentials('twilio')
        env = self.environ()
        session = PhoneSession(
            self, ctx, api_key=openai['apiKey'], twilio=self.twilio_factory(twilio_creds),
            from_number=twilio_creds['fromNumber'], token=token, inbound=True,
            recording=self._recording(), owner_phone=env.get('COLLEAGUE_OWNER_PHONE'))
        session.call_sid = call_sid
        self.sessions[ctx.call_id] = session
        try:
            ctx.set_status('connecting')
            ctx.link(providerCallSid=call_sid)
            try:
                await asyncio.wait_for(session.connected.wait(), 30)
            except asyncio.TimeoutError:
                session.fail('The incoming call ended before its audio connected.')
                session.finished.set()
            if not session.finished.is_set():
                await self._await_call(ctx, session)
            await self._finish(ctx, session)
        finally:
            self.sessions.pop(ctx.call_id, None)

    async def _await_duration(self, session):
        while session.phone_seconds is None:
            await asyncio.sleep(0.2)

    @staticmethod
    def _usage(session):
        usage = {'voiceSeconds': int(getattr(session.live, 'usage_seconds', 0) or 0)}
        if session.phone_seconds is not None:
            usage['phoneSeconds'] = session.phone_seconds
        if any(session.backend_tokens.values()):
            usage['backendTokens'] = dict(session.backend_tokens)
        return usage

    # Twilio callbacks ----------------------------------------------------

    def on_status(self, call_id, params):
        session = self.sessions.get(call_id)
        if session is None:
            return False
        status = params.get('CallStatus')
        if params.get('CallDuration'):
            try:
                session.phone_seconds = int(params['CallDuration'])
            except ValueError:
                pass
        if status == 'ringing':
            session.ctx.set_status('ringing')
        elif status in TWILIO_TERMINAL:
            if session.phone_seconds is None:
                session.phone_seconds = 0
            if not session.connected.is_set():
                if status == 'failed':
                    session.fail('Twilio could not place the call.')
                else:
                    session.end_reason = session.end_reason or TWILIO_TERMINAL[status]
                session.finished.set()
        return True

    def on_amd(self, call_id, answered_by):
        session = self.sessions.get(call_id)
        if session is None:
            return False
        session.ctx.event('call.answered_by', answeredBy=answered_by or 'unknown')
        if (answered_by or '').startswith('machine') or answered_by == 'fax':
            session._spawn(session.voicemail())
        return True

    # Line interface --------------------------------------------------------

    async def instruct(self, call_id, text):
        session = self.sessions.get(call_id)
        return bool(session and await session.instruct(text))

    async def end(self, call_id):
        session = self.sessions.get(call_id)
        if session is None:
            return False
        if session.connected.is_set():
            await session.wrap_up()
            return True
        # Not connected yet: start() checks this flag after each step, so a call
        # still being set up is never dialed, and one already dialed is canceled.
        session.canceled = True
        session.end_reason = session.end_reason or 'canceled'
        await self._cancel_remote(session)
        session.finished.set()
        return True

    async def transfer(self, call_id):
        session = self.sessions.get(call_id)
        if session is None or not session.connected.is_set():
            raise NotReady('The call is not connected.')
        return await session.transfer()


def inbound_brief(environ, caller):
    """The brief for a call someone placed to us. Withheld numbers are still answered."""
    context = None
    try:
        to = normalize_phone(caller or '')
    except ValueError:
        to = environ.get('TWILIO_FROM_NUMBER') or '+10000000000'
        context = 'The caller withheld their number. Ask for a callback number.'
    notify = None
    try:
        if environ.get('COLLEAGUE_NOTIFY_WEBHOOK'):
            notify = {'webhookUrl': validate_webhook_url(environ['COLLEAGUE_NOTIFY_WEBHOOK'])}
    except ValueError:
        notify = None
    payload = {
        'channel': 'phone',
        'to': to,
        'onBehalfOf': environ.get('COLLEAGUE_OWNER_NAME') or 'the owner',
        'objective': 'Answer an incoming call and take a clear message with a callback number.',
    }
    if context:
        payload['context'] = context
    if notify:
        payload['notify'] = notify
    return CallBrief.from_dict(payload)
