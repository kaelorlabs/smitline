"""Ringing the owner's own phone and connecting them with the person on the call.

It happens when a person picks up after a hold and the brief says connectOwner, or when the
agent asks (transfer_call_to_me). The owner's phone rings with a short whisper: who has a person
on the line and why, "press 1 to talk to them now". Meanwhile the voice keeps the person company.
Pressing 1 connects the owner's leg to Smitline's media gateway, which passes the audio between
the two calls; the voice is switched off while they talk (it costs nothing and hears nothing of
their conversation). When the owner hangs up and the person is still there, the voice comes back
to see them off. If the owner does not answer, declines, or the call to them fails, the voice
carries on with the call itself.

Only calls whose audio passes through Smitline; direct SIP calls are still handed over blind.
"""
import asyncio
import json
import secrets
import time

from phone_prompts import (
    OWNER_DECLINED, owner_ringing_cue, owner_unavailable_cue, owner_whisper,
)
from twilio_client import gather_twiml, hangup_twiml, say_twiml, stream_twiml


# The owner's phone rings this long; the whisper is said twice and waits for a key after it.
OWNER_RING_SECONDS = 25
# From ringing to pressing 1, at most (the ring, the whisper twice, the wait for a key).
OWNER_JOIN_TIMEOUT = 75.0
# The owner talks with the person for at most this long.
OWNER_MAX_MINUTES = 60
TERMINAL = frozenset({'completed', 'busy', 'no-answer', 'failed', 'canceled'})


def _error_code(error):
    return getattr(error, 'code', None) or type(error).__name__


class OwnerLegMixin:
    """The owner's call for PhoneSession (relayed audio): needs its line, ctx, brief, twilio,
    owner_phone and from_number, and the hold mixin's _go_to_sleep and _wake_owner_left."""

    def _init_owner(self):
        self.owner_leg = None  # {'state': ringing|answering|connected|left|unavailable|done, ...}
        self.owner_token = secrets.token_urlsafe(24)

    def _owner_on_line(self):
        leg = self.owner_leg
        return bool(leg and leg.get('state') == 'connected' and leg.get('ws') is not None)

    def owner_seconds(self, now=None):
        """How long the owner has talked with them so far."""
        leg = self.owner_leg or {}
        seconds = leg.get('talkedSeconds') or 0
        if leg.get('state') == 'connected' and leg.get('connectedAt'):
            seconds += (now if now is not None else time.monotonic()) - leg['connectedAt']
        return seconds

    async def _to_owner(self, payload):
        leg = self.owner_leg
        try:
            await leg['ws'].send_str(json.dumps({'event': 'media', 'streamSid': leg.get('streamSid'),
                                                 'media': {'payload': payload}}))
        except Exception:
            pass

    async def ring_owner(self, why):
        """Ring the owner's phone ('pickup': connectOwner after a hold; 'transfer': the agent
        asked). Pressing 1 connects them (owner_answer_twiml, run_owner_leg)."""
        leg = self.owner_leg
        if leg is not None and leg.get('state') in ('ringing', 'answering', 'connected'):
            return False
        if not self.owner_phone or self._hanging_up or self.finished.is_set():
            return False
        self.owner_leg = leg = {'state': 'ringing', 'why': why, 'sid': None, 'ws': None,
                                'streamSid': None, 'connectedAt': None, 'seconds': None,
                                'talkedSeconds': leg.get('talkedSeconds', 0) if leg else 0}
        who = self.brief.on_behalf_of
        try:
            base = (await self.line._public_url()).rstrip('/')
            created = await self.twilio.create_call(
                to=self.owner_phone, from_=self.from_number,
                twiml=gather_twiml(f'{base}/twilio/owner/{self.ctx.call_id}',
                                   owner_whisper(self.brief), after=OWNER_DECLINED),
                status_callback=f'{base}/twilio/owner-status/{self.ctx.call_id}',
                timeout=OWNER_RING_SECONDS, time_limit=OWNER_MAX_MINUTES * 60 + 120)
        except Exception as error:
            self.ctx.event('call.owner_call_failed', code=_error_code(error))
            await self._owner_unavailable('error')
            if why == 'transfer':
                raise  # the agent asked: it hears why (call_service maps provider errors)
            return False
        leg['sid'] = created.get('sid')
        self.ctx.link(ownerCallSid=leg['sid'])
        self.ctx.event('call.owner_ringing', why=why)
        self._write_note(f"Ringing {who}'s phone.")
        if self.live is not None and not self.asleep:
            await self.live.append('session.commentary.append', owner_ringing_cue(self.brief))
        self._spawn(self._owner_join_deadline(leg))
        return True

    async def _owner_join_deadline(self, leg):
        await asyncio.sleep(OWNER_JOIN_TIMEOUT)
        if self.owner_leg is leg and leg.get('state') in ('ringing', 'answering'):
            await self._hang_up_owner_call()
            await self._owner_unavailable('no_answer')

    def owner_answer_twiml(self, params, base):
        """The owner's phone after the whisper: 1 joins them to the call, anything else declines."""
        leg = self.owner_leg
        if not leg or leg.get('state') != 'ringing' or self.finished.is_set() or self._hanging_up:
            return hangup_twiml()
        if str(params.get('Digits') or '').strip() != '1':
            self._spawn(self._owner_unavailable('declined'))
            return say_twiml(OWNER_DECLINED)
        leg['state'] = 'answering'
        stream_url = 'wss://' + base.split('://', 1)[1] + '/twilio/media'
        return stream_twiml(stream_url, {'callId': self.ctx.call_id, 'token': self.owner_token,
                                         'leg': 'owner'})

    def on_owner_status(self, params):
        leg = self.owner_leg
        if not leg:
            return
        if params.get('CallDuration'):
            try:
                leg['seconds'] = int(params['CallDuration'])
            except ValueError:
                pass
        status = params.get('CallStatus')
        if status in TERMINAL and leg.get('state') in ('ringing', 'answering'):
            self._spawn(self._owner_unavailable(status.replace('-', '_')))

    async def _owner_unavailable(self, why):
        leg = self.owner_leg
        if leg is None:
            self.owner_leg = leg = {'state': 'unavailable'}
        elif leg.get('state') in ('unavailable', 'done', 'left', 'connected'):
            return
        leg['state'] = 'unavailable'
        self.ctx.event('call.owner_unavailable', reason=why)
        self._write_note(f"{self.brief.on_behalf_of} couldn't come to the phone.")
        if (self.live is not None and not self.asleep and not self.finished.is_set()
                and not self._hanging_up):
            await self.live.append('session.commentary.append', owner_unavailable_cue(self.brief))

    async def run_owner_leg(self, ws, start):
        """The owner pressed 1: pass the audio between them and the person until one hangs up."""
        from aiohttp import WSMsgType
        leg = self.owner_leg
        if not leg or leg.get('state') != 'answering' or self._hanging_up or self.finished.is_set():
            await ws.close()
            return
        leg.update(state='connected', ws=ws, streamSid=start.get('streamSid'),
                   connectedAt=time.monotonic())
        who = self.brief.on_behalf_of
        if self.holdbook.on_hold:
            self._end_hold('owner')  # they wait in the queue themselves now
        await self._go_to_sleep('owner')
        self.ctx.event('call.owner_connected')
        self._write_note(f'{who} joined the call.')
        try:
            async for message in ws:
                if message.type != WSMsgType.TEXT:
                    if message.type in (WSMsgType.CLOSE, WSMsgType.CLOSING, WSMsgType.CLOSED,
                                        WSMsgType.ERROR):
                        break
                    continue
                try:
                    data = json.loads(message.data)
                except ValueError:
                    continue
                if data.get('event') == 'media':
                    media = data.get('media') or {}
                    payload = media.get('payload')
                    if media.get('track', 'inbound') == 'inbound' and payload:
                        await self._twilio_send({'event': 'media', 'media': {'payload': payload}})
                elif data.get('event') == 'stop':
                    break
        finally:
            ended = time.monotonic()
            leg['talkedSeconds'] = (leg.get('talkedSeconds') or 0) + max(1, int(ended - leg['connectedAt']))
            leg.update(state='left', ws=None, connectedAt=None)
            self.last_activity = ended
            # The owner hung up while the other person may still be there: not the call ending.
            if self.stream_ended_at is None and not self._hanging_up:
                self.ctx.event('call.owner_left')
                self._write_note(f'{who} left the call.')
                self._wake_owner_left()

    async def _hang_up_owner_call(self):
        leg = self.owner_leg
        if not leg or not leg.get('sid'):
            return
        for status in ('completed', 'canceled'):
            try:
                await self.twilio.update_call(leg['sid'], status=status)
                return
            except Exception:
                continue

    async def _end_owner_leg(self):
        """The call is over: the owner's call ends too."""
        leg = self.owner_leg
        if not leg or leg.get('state') in ('done', 'unavailable', 'left'):
            return
        ws, state = leg.get('ws'), leg.get('state')
        leg['state'] = 'done'
        if ws is not None:
            try:
                await ws.close()
            except Exception:
                pass
        if state in ('ringing', 'answering', 'connected'):
            await self._hang_up_owner_call()

    def owner_usage(self):
        """The owner's call for the usage: seconds the provider billed, and seconds they talked."""
        leg = self.owner_leg or {}
        usage = {}
        if leg.get('seconds'):
            usage['ownerLegSeconds'] = leg['seconds']
        talked = int(self.owner_seconds())
        if talked:
            usage['ownerTalkedSeconds'] = talked
        return usage
