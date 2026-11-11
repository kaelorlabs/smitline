"""Phone menus: the voice presses keys ("press 2 for billing") on phone calls, unless the brief
says phoneMenus: false."""
import base64
import json
import math
import tempfile
import unittest

from call_brief import CallBrief
from call_recorder import ULAW
from phone_keys import FREQUENCIES, key_audio, key_payload, menu_keys, shown_keys
from phone_line import SILENT_FRAME_DB
from phone_prompts import (
    PRESS_KEYS_TOOL, backend_instructions, delegation_config, machine_hint, opening_cue,
    voice_instructions,
)
from barge_in import frame_level_db
from test_phone import (
    SIP_ENV, FakeLive, FakeSideband, FakeSipClient, PhoneHarness, brief, until,
)


def press(keys, call_id='fc-keys'):
    return {'type': 'response.event', 'delegation_id': 'dlg-1',
            'event': {'type': 'response.output_item.done',
                      'item': {'type': 'function_call', 'call_id': call_id, 'name': 'press_keys',
                               'arguments': json.dumps({'keys': keys})}}}


def goertzel(samples, frequency, rate=8000):
    k = 2 * math.cos(2 * math.pi * frequency / rate)
    a = b = 0.0
    for sample in samples:
        a, b = sample + k * a - b, a
    return a * a + b * b - k * a * b


def parsed(**overrides):
    return CallBrief.from_dict(brief(**overrides))


class BriefTests(unittest.TestCase):
    def test_on_for_phone_calls_unless_the_brief_turns_it_off(self):
        # Any business may answer with a menu: a call is ready for one without being told.
        self.assertTrue(parsed().phone_menus)
        self.assertNotIn('phoneMenus', parsed().to_dict())
        self.assertTrue(parsed(phoneMenus=True).phone_menus)
        self.assertNotIn('phoneMenus', parsed(phoneMenus=True).to_dict())
        off = parsed(phoneMenus=False)
        self.assertFalse(off.phone_menus)
        self.assertIs(off.to_dict()['phoneMenus'], False)
        for value in (parsed(), off):
            self.assertEqual(CallBrief.from_dict(value.to_dict()), value)
        meeting = CallBrief.from_dict({'channel': 'meeting', 'to': 'https://zoom.us/j/123456789',
                                       'onBehalfOf': 'Robin', 'objective': 'Take notes'})
        self.assertFalse(meeting.phone_menus)
        self.assertNotIn('phoneMenus', meeting.to_dict())

    def test_fails_closed(self):
        with self.assertRaises(ValueError):
            parsed(phoneMenus='yes')
        with self.assertRaisesRegex(ValueError, 'only for phone calls'):
            CallBrief.from_dict({'channel': 'meeting', 'to': 'https://zoom.us/j/123456789',
                                 'onBehalfOf': 'Robin', 'objective': 'Take notes',
                                 'phoneMenus': True})


class PromptTests(unittest.TestCase):
    def tools(self, brief_, **kwargs):
        return [tool['name'] for tool in delegation_config(brief_, **kwargs)['responses']['tools']]

    def test_the_key_tool_comes_only_with_the_brief(self):
        self.assertEqual(self.tools(parsed(phoneMenus=False)), ['end_call'])
        self.assertEqual(self.tools(parsed()), ['end_call', 'press_keys'])
        self.assertEqual(self.tools(parsed(phoneMenus=True)), ['end_call', 'press_keys'])
        self.assertEqual(self.tools(parsed(), inbound=True), ['end_call'])
        self.assertTrue(PRESS_KEYS_TOOL['strict'])

    def test_turned_off_the_voice_gives_up_at_a_menu_as_before(self):
        off = parsed(phoneMenus=False)
        text = voice_instructions(off)
        self.assertIn('automated menu will not let you through. Say that', text)
        self.assertNotIn('Phone menus:', text)
        self.assertNotIn('Phone menus:', backend_instructions(off))
        self.assertNotIn('phone menu', opening_cue(off))
        self.assertIn('a voicemail system or an automated call screener', machine_hint(off))

    def test_with_it_the_voice_works_the_menu(self):
        on = parsed(phoneMenus=True)
        text = voice_instructions(on)
        self.assertIn('Phone menus: an automated phone menu is a recording', text)
        # On a live call the voice handed nothing to its backend until it was told plainly.
        self.assertIn('You cannot press keys yourself', text)
        self.assertIn('only your backend can', text)
        self.assertIn('Say nothing to a menu: do not greet it', text)
        self.assertIn('Never key in a card number, a PIN, or a password', text)
        self.assertIn('still will not let you through after you have tried its options', text)
        backend = backend_instructions(on)
        self.assertIn('call press_keys when the voice assistant asks you', backend)
        self.assertIn('counts as agreeing', backend)
        self.assertIn('do not say hello: listen to its options', opening_cue(on))
        self.assertIn('an automated phone menu', machine_hint(on))


    def test_no_example_keys_steer_the_model(self):
        # Menus differ from call to call: the key comes from what the menu says, and a number from
        # the brief. An example key or number in these texts could be copied instead.
        import re
        on = parsed(phoneMenus=True)
        texts = [PRESS_KEYS_TOOL['description'], backend_instructions(on), voice_instructions(on)]
        for text in texts:
            self.assertFalse(re.search(r'press \d', text), text)
            self.assertEqual(re.findall(r'\d{2,}', text.replace('0-9', '')), [], text)


class KeyTests(unittest.TestCase):
    def test_keys_are_digits_star_hash_and_pauses(self):
        self.assertEqual(menu_keys('2'), '2')
        self.assertEqual(menu_keys(' 408-555 (1212)# '), '4085551212#')
        self.assertEqual(menu_keys('1W2w'), '1w2w')
        for bad in ('', 'ww', '12a', 'A', '1' * 33, 12, None):
            with self.assertRaises(ValueError):
                menu_keys(bad)

    def test_long_entries_show_only_their_length(self):
        self.assertEqual(shown_keys('2'), '2')
        self.assertEqual(shown_keys('1w0#'), '10#')
        self.assertEqual(shown_keys('w4085551212#'), '11 keys')

    def test_each_tone_is_its_two_frequencies(self):
        bands = (697, 770, 852, 941, 1209, 1336, 1477, 1633)
        for key in '0123456789*#':
            audio = key_audio(key)
            self.assertEqual(len(audio), 1600)  # 100 ms of tone, 100 ms of quiet
            samples = [ULAW[byte] for byte in audio[:800]]
            power = {band: goertzel(samples, band) for band in bands}
            self.assertEqual(sorted(sorted(power, key=power.get)[-2:]), list(FREQUENCIES[key]))
            self.assertEqual(set(audio[800:]), {0xFF})


class RelayedKeyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        FakeLive.instances = []
        self.temp = tempfile.TemporaryDirectory()
        self.h = PhoneHarness(self.temp.name)

    async def asyncTearDown(self):
        await self.h.service.shutdown()
        self.temp.cleanup()

    async def call(self, **overrides):
        record, session = await self.h.dial(phoneMenus=True, **overrides)
        ws, live, task = await self.h.connect(record, session)
        live.push({'type': 'session.started', 'session': {}})
        return record, session, ws, live, task

    def events(self, record, kind):
        return [e['data'] for e in self.h.service.events(record['id']) if e['type'] == kind]

    async def test_signalwire_presses_keys_on_the_stream(self):
        self.h.twilio.flavor = 'signalwire'
        record, session, ws, live, task = await self.call()
        live.push(press('1w2'))
        await until(lambda: live.outputs, 4)
        self.assertEqual(live.outputs, [('fc-keys', json.dumps({'ok': True, 'pressed': '12'}))])
        sent = [m for m in ws.sent if m['event'] in ('clear', 'dtmf')]
        self.assertEqual(sent, [{'event': 'clear', 'streamSid': 'MZ1'},
                                {'event': 'dtmf', 'dtmf': {'digit': '1'}, 'streamSid': 'MZ1'},
                                {'event': 'dtmf', 'dtmf': {'digit': '2'}, 'streamSid': 'MZ1'}])
        self.assertEqual(self.events(record, 'call.keys_pressed'), [{'keys': '12'}])
        ws.push({'event': 'stop'})
        await task

    async def test_an_account_number_is_not_kept_in_the_record(self):
        self.h.twilio.flavor = 'signalwire'
        record, session, ws, live, task = await self.call()
        live.push(press('4085551212#'))
        await until(lambda: live.outputs, 6)
        self.assertEqual(json.loads(live.outputs[0][1]), {'ok': True, 'pressed': '11 keys'})
        self.assertEqual(self.events(record, 'call.keys_pressed'), [{'keys': '11 keys'}])
        self.assertNotIn('4085551212', json.dumps(self.h.service.events(record['id'])))
        ws.push({'event': 'stop'})
        await task

    async def test_twilio_plays_the_tones_and_nothing_talks_over_them(self):
        record, session, ws, live, task = await self.call()
        live.push(press('11'))
        await until(lambda: session.keying)
        # The voice starting to talk, and the menu talking on, change nothing while keys play.
        live.push({'type': 'session.output_audio.delta',
                   'delta': base64.b64encode(b'\x2a' * 1600).decode()})
        loud = base64.b64encode(b'\x9a' * 160).decode()
        for _ in range(20):
            ws.push({'event': 'media', 'media': {'track': 'inbound', 'payload': loud}})
        await until(lambda: live.outputs, 6)
        self.assertEqual(json.loads(live.outputs[0][1]), {'ok': True, 'pressed': '11'})
        self.assertEqual([m['event'] for m in ws.sent].count('clear'), 1)
        frames = [base64.b64decode(m['media']['payload']) for m in ws.sent if m['event'] == 'media']
        heard = b''.join(f for f in frames if frame_level_db(f) >= SILENT_FRAME_DB)
        tone = base64.b64decode(key_payload('1'))[:800]
        self.assertEqual(heard, tone + tone)  # two separate presses of 1, and nothing else
        self.assertEqual(self.events(record, 'call.keys_pressed'), [{'keys': '11'}])
        ws.push({'event': 'stop'})
        await task

    async def test_keys_that_are_not_keys_are_refused(self):
        self.h.twilio.flavor = 'signalwire'
        record, session, ws, live, task = await self.call()
        live.push(press('one two'))
        await until(lambda: live.outputs)
        result = json.loads(live.outputs[0][1])
        self.assertFalse(result['ok'])
        self.assertIn('only digits', result['error'])
        self.assertFalse([m for m in ws.sent if m['event'] == 'dtmf'])
        self.assertEqual(self.events(record, 'call.keys_pressed'), [])
        ws.push({'event': 'stop'})
        await task

    async def test_with_phone_menus_off_nothing_is_pressed(self):
        self.h.twilio.flavor = 'signalwire'
        record, session = await self.h.dial(phoneMenus=False)
        ws, live, task = await self.h.connect(record, session)
        live.push({'type': 'session.started', 'session': {}})
        live.push(press('2'))
        live.push({'type': 'session.input_transcript.delta', 'delta': 'Hello?'})
        await until(lambda: session._other_turn)
        self.assertFalse([m for m in ws.sent if m['event'] == 'dtmf'])
        self.assertEqual(live.outputs, [])
        ws.push({'event': 'stop'})
        await task


class SidebandWithSend(FakeSideband):
    def __init__(self, key, session_id):
        super().__init__(key, session_id)
        self.sent = []

    async def send(self, payload):
        self.sent.append(payload)
        return True


class SipKeyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        FakeLive.instances = []
        FakeSideband.instances = []
        self.temp = tempfile.TemporaryDirectory()
        self.h = PhoneHarness(self.temp.name, env=dict(SIP_ENV))
        self.h.line.sip_client_factory = lambda key: FakeSipClient()
        self.h.line.sideband_factory = SidebandWithSend

    async def asyncTearDown(self):
        await self.h.service.shutdown()
        self.temp.cleanup()

    async def test_openai_sends_the_keys_on_the_sip_call(self):
        record = await self.h.service.create(brief(phoneMenus=True))
        await until(lambda: FakeSideband.instances)
        side = FakeSideband.instances[-1]
        side.push({'type': 'transport.answered', 'event_id': 'e2'})
        await until(lambda: self.h.store.get(record['id'])['status'] == 'in_progress')
        side.push(press('3#'))
        await until(lambda: side.outputs, 4)
        self.assertEqual(json.loads(side.outputs[0][1]), {'ok': True, 'pressed': '3#'})
        self.assertEqual([p for p in side.sent if p['type'] == 'transport.dtmf.send'],
                         [{'type': 'transport.dtmf.send', 'event': '3'},
                          {'type': 'transport.dtmf.send', 'event': '#'}])


if __name__ == '__main__':
    unittest.main()
