"""A simulated dial-in, with no real call: a fake phone provider plays the call's <Play digits>
into a fake meeting bridge that asks for the meeting ID and passcode the way Zoom, Teams and Google
Meet do, then connects the media stream; a scripted voice stands in for GPT-Live.

The bridge only takes keys while it is listening: from a moment after it answers, and again a
moment after each part it has checked. Keys that arrive earlier are lost, as on a real bridge, so
the one-tap pauses decide whether a meeting is joined."""
import asyncio
import re
import tempfile
import unittest
from unittest import mock

import dial_in
from test_dial_in import FAST, MEET_INVITE, TEAMS_INVITE, ZOOM_INVITE, ZOOM_LINK, DialHarness, meeting_brief
from test_phone import FakeLive, FakeTwilioSocket, until


# A provider plays a key as a tone and a gap, and each w as half a second (both document w).
KEY_PLAY_SECONDS = 0.25


def key_timeline(digits):
    """[(seconds after the answer, key)] for a <Play digits> string."""
    timeline, now = [], 0.0
    for key in digits:
        if key == 'w':
            now += 0.5
        else:
            timeline.append((round(now, 3), key))
            now += KEY_PLAY_SECONDS
    return timeline


class FakeBridge:
    """A meeting's phone line. Each part is (prompt, the digits it expects, an optional prefix it
    skips, the sentence it says when they are wrong). It listens `ready` seconds after answering,
    and `check` seconds after each part it took, which it spends checking it."""

    def __init__(self, parts, *, ready=1.5, check=1.5, after=('You are now in the meeting.',)):
        self.parts = parts
        self.ready = ready
        self.check = check
        self.after = after
        self.lost = []  # keys that arrived while it was not listening
        self.heard = []  # (part, digits, when the last key came)

    def hear(self, timeline):
        """What the bridge says once the keys have played: ('joined', sentences),
        ('refused', sentence) or ('asking', prompt) when it is still waiting for a part."""
        listening_from = self.ready
        index, typed = 0, ''
        for when, key in timeline:
            if index >= len(self.parts):
                break
            prompt, expected, prefix, wrong = self.parts[index]
            if when < listening_from:
                self.lost.append((when, key))
                continue
            if key == prefix and not typed:
                continue
            if key != '#':
                typed += key
                continue
            self.heard.append((index, typed, when))
            if typed != expected:
                return 'refused', wrong
            index, typed = index + 1, ''
            listening_from = when + self.check
        if index < len(self.parts):
            return 'asking', self.parts[index][0]
        return 'joined', self.after


def zoom_bridge(passcode='123456'):
    return FakeBridge([
        ('Welcome to Zoom. Enter your meeting ID followed by pound.', '85951289482', None,
         'The meeting ID you entered is invalid. Please try again.'),
        ('Please enter the meeting passcode followed by pound.', passcode, '*',
         'The passcode you entered is incorrect.'),
    ], after=('Please wait, the meeting host will let you in soon.',))


def teams_bridge():
    return FakeBridge([
        ('Welcome to Microsoft Teams. Please enter your conference ID followed by the pound key.',
         '123456789', None, 'That conference ID is not valid.'),
    ], after=('Please wait, someone in the meeting should let you in soon.',))


def meet_bridge():
    return FakeBridge([('Welcome to Google Meet. Please enter the meeting PIN followed by pound.',
                        '123456789', None, 'That PIN is invalid.')])


class ScriptedVoice(FakeLive):
    """GPT-Live as the meeting voice: it says the hello when cued, and answers when addressed."""

    def __init__(self, key, config):
        super().__init__(key, config)
        self.spoken = []

    def say(self, text):
        self.spoken.append(text)
        self.push({'type': 'session.output_transcript.delta', 'delta': text})

    async def append(self, kind, content, delegation_id=None):
        await super().append(kind, content, delegation_id)
        if kind == 'session.commentary.append' and 'Introduce yourself' in content:
            self.say(re.search(r'"([^"]+)"', content)[1])
        return True


class FakeProvider:
    """The phone provider: dials, plays the TwiML's <Play digits> into the bridge, then connects
    the media stream to the phone line, as Twilio and SignalWire do."""

    def __init__(self, harness, bridge, flavor='signalwire'):
        self.h = harness
        self.bridge = bridge
        self.flavor = flavor
        self.created = []
        self.updates = []
        self.ws = None
        self.session = None
        self.connected = asyncio.Event()

    async def create_call(self, **kwargs):
        self.created.append(kwargs)
        twiml = kwargs['twiml']
        # The keys play first, then the stream starts: <Play> comes before <Connect>.
        self.digits = re.search(r'<Play digits="([0-9*#w]+)"/>', twiml)[1]
        assert twiml.index('<Play') < twiml.index('<Connect>'), twiml
        self.call_id = re.search(r'name="callId" value="([^"]+)"', twiml)[1]
        self.token = re.search(r'name="token" value="([^"]+)"', twiml)[1]
        self.outcome = self.bridge.hear(key_timeline(self.digits))
        asyncio.get_running_loop().call_soon(lambda: asyncio.ensure_future(self._answer()))
        return {'sid': 'CA-sim'}

    async def _answer(self):
        self.ws = FakeTwilioSocket()
        self.session = self.h.phone.claim(self.call_id, self.token)
        assert self.session is not None
        self.task = asyncio.ensure_future(self.session.run(self.ws, {'streamSid': 'MZ-sim', 'callSid': 'CA-sim'}))
        await until(lambda: self.session.live is not None)
        self.voice = self.session.live
        self.voice.push({'type': 'session.started', 'session': {'id': 'sess-sim'}})
        kind, said = self.outcome
        for sentence in ([said] if isinstance(said, str) else said):
            self.bridge_says(sentence)
        self.connected.set()

    def bridge_says(self, sentence):
        self.voice.push({'type': 'session.input_transcript.delta', 'delta': sentence + ' '})

    async def update_call(self, call_sid, **kwargs):
        self.updates.append((call_sid, kwargs))
        if kwargs.get('status') in ('completed', 'canceled') and self.ws is not None:
            await self.ws.close()
            self.h.phone.on_status(self.call_id, {'CallStatus': 'completed', 'CallDuration': '95'})
        return {}

    async def get_call(self, call_sid):
        return {'sid': call_sid, 'status': 'completed', 'price': '0.0420', 'price_unit': 'USD'}


class SimulatedDialInTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        FakeLive.instances = []
        self.temp = tempfile.TemporaryDirectory()
        self.h = DialHarness(self.temp.name, env={'SMITLINE_MEETING_DIAL_IN_FIRST': 'all'})
        self.h.phone.live_factory = ScriptedVoice
        self.patches = [mock.patch.object(dial_in, name, value) for name, value in FAST.items()]
        for patch in self.patches:
            patch.start()

    async def asyncTearDown(self):
        for patch in self.patches:
            patch.stop()
        await self.h.service.shutdown()
        self.temp.cleanup()

    def provider(self, bridge, flavor='signalwire'):
        provider = FakeProvider(self.h, bridge, flavor)
        self.h.phone.twilio_factory = lambda creds: provider
        return provider

    async def test_zoom_by_phone_from_its_invitation_end_to_end(self):
        for flavor in ('signalwire', 'twilio'):
            with self.subTest(flavor=flavor):
                bridge = zoom_bridge()
                provider = self.provider(bridge, flavor)
                record = await self.h.service.create(meeting_brief(
                    to=ZOOM_LINK, dialIn={'invite': ZOOM_INVITE}, canDo='look up the launch date'))
                await asyncio.wait_for(provider.connected.wait(), 5)
                # The bridge took the meeting ID and the passcode, each after it asked for it.
                self.assertEqual(bridge.lost, [])
                self.assertEqual([(part, digits) for part, digits, _ in bridge.heard],
                                 [(0, '85951289482'), (1, '123456')])
                id_done = bridge.heard[0][2]
                passcode_keys = [when for when, key in key_timeline(provider.digits) if key == '*']
                self.assertGreaterEqual(passcode_keys[0], id_done + bridge.check)
                # Its waiting room first: not meeting time, and no hello into it.
                await until(lambda: self.h.status(record) == 'waiting')
                voice = provider.voice
                await asyncio.sleep(0.4)
                self.assertEqual(voice.spoken, [])
                # The host lets it in; people talk; at the first pause it says who it is.
                provider.bridge_says('Hi everyone, thanks for joining.')
                await until(lambda: self.h.status(record) == 'in_progress')
                await until(lambda: voice.spoken, timeout=5)
                self.assertEqual(voice.spoken[0], "Hey, this is Raj's AI assistant. I'm here to answer "
                                 "any questions or do any work. I'll be here taking notes, so let me "
                                 "know if you need anything.")
                await until(lambda: provider.session.disclosed.is_set())
                # Asked to go, it says goodbye and hangs up once that has been heard.
                provider.bridge_says('Great, thanks Smitline, you can drop off now.')
                await asyncio.sleep(0.1)
                voice.say('Sure, bye everyone!')
                await until(lambda: provider.ws.closed, timeout=5)
                done = await self.h.service.wait(record['id'], timeout=5, owner='local')
                self.assertEqual((done['channel'], done['status'], done['endReason']),
                                 ('meeting', 'completed', 'meeting_ended'))
                self.assertEqual(self.h.events(record, 'call.agent_left'), [{'reason': 'left_asked_to_leave'}])
                self.assertEqual(done['usage']['phoneSeconds'], 95)
                self.assertEqual(done['line']['joinedBy'], 'phone')
                lines = [(entry['speaker'], entry['text']) for entry in done['result']['transcript']]
                self.assertIn(('meeting', 'Great, thanks Smitline, you can drop off now.'), lines)

    async def test_teams_lobby_then_in(self):
        provider = self.provider(teams_bridge())
        record = await self.h.service.create(meeting_brief(dialIn={'invite': TEAMS_INVITE}))
        await asyncio.wait_for(provider.connected.wait(), 5)
        self.assertEqual(provider.bridge.heard[0][:2], (0, '123456789'))
        self.assertEqual(provider.created[0]['to'], '+13238494874')
        await until(lambda: self.h.status(record) == 'waiting')
        provider.bridge_says("You're now in the meeting.")
        await until(lambda: self.h.status(record) == 'in_progress')
        await until(lambda: provider.voice.spoken, timeout=5)
        provider.ws.push({'event': 'stop'})  # the organizer ends the meeting
        done = await self.h.service.wait(record['id'], timeout=5, owner='local')
        self.assertEqual(done['endReason'], 'meeting_ended')

    async def test_meet_pin_from_an_invitation_with_bidi_marks(self):
        provider = self.provider(meet_bridge())
        record = await self.h.service.create(meeting_brief(
            to='https://meet.google.com/abc-defg-hij', dialIn={'invite': MEET_INVITE}))
        await asyncio.wait_for(provider.connected.wait(), 5)
        self.assertEqual(provider.bridge.heard[0][:2], (0, '123456789'))
        await until(lambda: self.h.status(record) == 'in_progress')
        await self.h.service.end(record['id'], owner='local')
        done = await self.h.service.wait(record['id'], timeout=5, owner='local')
        self.assertEqual(done['endReason'], 'canceled')

    async def test_a_wrong_passcode_is_refused_and_the_call_ends(self):
        provider = self.provider(zoom_bridge(passcode='654321'))
        record = await self.h.service.create(meeting_brief(to='+14388097799,,85951289482#,,,,*123456#'))
        await asyncio.wait_for(provider.connected.wait(), 5)
        await until(lambda: provider.ws.closed, timeout=5)
        done = await self.h.service.wait(record['id'], timeout=5, owner='local')
        self.assertEqual(done['endReason'], 'dial_in_refused')
        self.assertEqual(self.h.events(record, 'call.dial_in_refused'),
                         [{'said': 'The passcode you entered is incorrect.'}])
        self.assertEqual(done['usage']['voiceSeconds'], 0)
        self.assertEqual(provider.voice.spoken, [])  # it never spoke into the bridge

    async def test_the_platform_pauses_decide_whether_the_keys_are_heard(self):
        # Keys sent the moment the call is answered, with no pauses, reach the bridge before it
        # listens: it loses the start of the meeting ID and refuses the rest.
        bridge = zoom_bridge()
        provider = self.provider(bridge)
        record = await self.h.service.create(meeting_brief(
            to=ZOOM_LINK, dialIn={'number': '+14388097799', 'keys': '85951289482#*123456#'}))
        await asyncio.wait_for(provider.connected.wait(), 5)
        self.assertTrue(bridge.lost)
        self.assertEqual(provider.outcome[0], 'refused')
        done = await self.h.service.wait(record['id'], timeout=5, owner='local')
        self.assertEqual(done['endReason'], 'dial_in_refused')
        # Zoom's own one-tap timing, which Smitline uses by default, gets every key in.
        bridge = zoom_bridge()
        provider = self.provider(bridge)
        record = await self.h.service.create(meeting_brief(
            to=ZOOM_LINK, dialIn={'number': '+14388097799', 'meetingId': '85951289482',
                                  'passcode': '123456'}))
        await asyncio.wait_for(provider.connected.wait(), 5)
        self.assertEqual((bridge.lost, provider.outcome[0]), ([], 'joined'))
        await self.h.service.end(record['id'], owner='local')
        await self.h.service.wait(record['id'], timeout=5, owner='local')


if __name__ == '__main__':
    unittest.main()
