"""Waiting on hold with the voice switched off (phone_hold.py), and ringing the owner's phone
(phone_owner.py). The listener here is a fake: hold_listener.py has its own tests."""
import asyncio
import base64
import json
import tempfile
import time
import unittest
from unittest import mock

import phone_hold
from call_brief import CallBrief, DEFAULT_MAX_HOLD_MINUTES
from call_costs import call_cost
from call_result import result_without_conversation, transcript_text
from call_service import CallError
from dial_in import DialInSession
from phone_hold import HoldBook, resume_input
from phone_line import PhoneSession, SipPhoneSession
from phone_prompts import (
    OWNER_DECLINED, WAIT_ON_HOLD_TOOL, back_from_hold, backend_instructions, delegation_config,
    voice_instructions, waited,
)
from startup_input import MESSAGE_LIMIT
from test_phone import PUBLIC, FakeLive, FakeTwilioSocket, PhoneHarness, brief, until


OWNER_PHONE = '+14155550199'
SPEECH = base64.b64encode(b'\x2a' * 1600).decode()  # 0.2 s of loud model speech


def tool_call(name, call_id='fc-hold', arguments=None):
    return {'type': 'response.event', 'delegation_id': 'dlg-1',
            'event': {'type': 'response.output_item.done',
                      'item': {'type': 'function_call', 'call_id': call_id, 'name': name,
                               'arguments': json.dumps(arguments or {})}}}


def payload(index):
    return base64.b64encode(bytes([index % 256]) * 160).decode()


def parsed(**overrides):
    return CallBrief.from_dict(brief(**overrides))


class Wake:
    def __init__(self, onset_ms_ago, speech_ms=800):
        self.onset_ms_ago = onset_ms_ago
        self.speech_ms = speech_ms
        self.scores = {}


class FakeListener:
    """Wakes once, on the `wake_on`-th frame after listen(), when a wake is set."""
    name = 'fake'

    def __init__(self):
        self.listening = False
        self.heard = 0       # frames since listen()
        self.frames = 0      # frames in all
        self.wake = None
        self.wake_on = 1
        self.remembered = 0
        self.stats = {'wakes': 0, 'skippedRepeats': 0, 'skippedMusic': 0}

    def listen(self):
        self.listening = True
        self.heard = 0

    def feed(self, frame):
        assert isinstance(frame, bytes) and len(frame) == 160
        self.frames += 1
        if not self.listening:
            return None
        self.heard += 1
        if self.wake is not None and self.heard >= self.wake_on:
            wake, self.wake = self.wake, None
            self.listening = False
            self.stats['wakes'] += 1
            return wake
        return None

    def remember_recording(self):
        self.remembered += 1


class BriefTests(unittest.TestCase):
    def test_max_hold_minutes(self):
        self.assertIsNone(parsed().max_hold_minutes)
        self.assertEqual(parsed().hold_minutes, DEFAULT_MAX_HOLD_MINUTES)
        self.assertNotIn('maxHoldMinutes', parsed().to_dict())
        held = parsed(maxHoldMinutes=120)
        self.assertEqual((held.max_hold_minutes, held.hold_minutes), (120, 120))
        self.assertEqual(CallBrief.from_dict(held.to_dict()), held)
        for bad in (0, 181, '30', True):
            with self.assertRaises(ValueError):
                parsed(maxHoldMinutes=bad)
        with self.assertRaisesRegex(ValueError, 'only for phone calls'):
            CallBrief.from_dict({'channel': 'meeting', 'to': 'https://zoom.us/j/123456789',
                                 'onBehalfOf': 'Robin', 'objective': 'Notes', 'maxHoldMinutes': 30})
        meeting = CallBrief.from_dict({'channel': 'meeting', 'to': 'https://zoom.us/j/123456789',
                                       'onBehalfOf': 'Robin', 'objective': 'Notes'})
        self.assertEqual(meeting.hold_minutes, 0)

    def test_connect_owner(self):
        self.assertFalse(parsed().connect_owner)
        on = parsed(connectOwner=True)
        self.assertTrue(on.connect_owner)
        self.assertIs(on.to_dict()['connectOwner'], True)
        self.assertEqual(CallBrief.from_dict(on.to_dict()), on)
        with self.assertRaises(ValueError):
            parsed(connectOwner='yes')
        with self.assertRaisesRegex(ValueError, 'only for phone calls'):
            CallBrief.from_dict({'channel': 'meeting', 'to': 'https://zoom.us/j/123456789',
                                 'onBehalfOf': 'Robin', 'objective': 'Notes', 'connectOwner': True})


class PromptTests(unittest.TestCase):
    def test_the_hold_tool_and_rules_come_with_calls_that_can_hold(self):
        call = parsed()
        tools = [tool['name'] for tool in delegation_config(call, hold=True)['responses']['tools']]
        self.assertIn('wait_on_hold', tools)
        self.assertNotIn('wait_on_hold', [tool['name'] for tool in
                                          delegation_config(call)['responses']['tools']])
        self.assertTrue(WAIT_ON_HOLD_TOOL['strict'])
        self.assertIn('Hold: when the call is put on hold', voice_instructions(call, hold=True))
        self.assertNotIn('Hold:', voice_instructions(call))
        self.assertIn('call wait_on_hold when', backend_instructions(call, hold=True))
        self.assertNotIn('wait_on_hold', backend_instructions(call))

    def test_no_standing_rule_to_greet_whoever_picks_up(self):
        # The 2026-11-02 lesson (test_phone): a standing "greet them when someone picks up" made the
        # voice start over after a quiet "hello?". Phone menus are on for every call now, so their
        # rule speaks only of the person after a menu, and the hold rule of no greeting at all.
        text = voice_instructions(parsed(), hold=True).lower()
        self.assertNotIn('greet them', text)
        self.assertNotIn('pick up', text)
        self.assertIn('a menu never hears your opening', text)

    def test_time_on_hold_in_words(self):
        self.assertEqual(waited(10), 'a few seconds')
        self.assertEqual(waited(70), 'about a minute')
        self.assertEqual(waited(25 * 60 + 10), 'about 25 minutes')
        self.assertEqual(waited(150 * 60), 'about 2.5 hours')

    def test_the_woken_voice_knows_it_was_on_hold_and_both_ways_on(self):
        note = back_from_hold(parsed(), 12 * 60)
        self.assertIn('on hold about 12 minutes ago', note)
        self.assertIn('If a person is talking to you, answer them right away', note)
        self.assertIn('the person you spoke with before the hold, carry on', note)
        self.assertIn('ask your backend to wait on hold again', note)


class ResumeInputTests(unittest.TestCase):
    def test_the_latest_of_the_call_that_fits_then_why_it_woke(self):
        seed = [{'type': 'message', 'role': 'developer',
                 'content': [{'type': 'input_text', 'text': 'Notes about Robin.'}]}]
        transcript = [{'speaker': 'note', 'text': 'On hold.'}]
        transcript += [{'speaker': 'other' if index % 2 else 'agent', 'text': f'line {index}'}
                       for index in range(400)]
        messages = resume_input(seed, transcript, 'You were on hold.')
        self.assertLessEqual(len(messages), MESSAGE_LIMIT)
        self.assertEqual(messages[0], seed[0])
        self.assertEqual(messages[-1]['role'], 'developer')
        self.assertEqual(messages[-1]['content'][0]['text'], 'You were on hold.')
        self.assertEqual(messages[-2]['content'][0]['text'], 'line 399')
        self.assertEqual(messages[-2]['role'], 'user')
        self.assertEqual(messages[-3]['role'], 'assistant')
        self.assertEqual(messages[-3]['content'][0]['type'], 'output_text')
        self.assertNotIn('On hold.', json.dumps(messages))  # notes are not something said

    def test_a_long_line_does_not_crowd_out_the_note(self):
        transcript = [{'speaker': 'other', 'text': 'x' * 100000}]
        messages = resume_input(None, transcript, 'Back.')
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]['content'][0]['text'], 'Back.')


class HoldBookTests(unittest.TestCase):
    def test_counts_every_hold_and_the_one_going_on(self):
        now = [100.0]
        book = HoldBook(clock=lambda: now[0])
        self.assertFalse(book.on_hold)
        book.start('listener')
        now[0] = 160.0
        self.assertEqual(book.current(), 60.0)
        self.assertEqual(book.end(), 60.0)
        book.start('listener')
        now[0] = 190.0
        self.assertEqual((book.times, book.seconds()), (2, 90.0))
        self.assertTrue(book.on_hold)


class SessionKindTests(unittest.TestCase):
    def test_only_relayed_outgoing_calls_hold(self):
        self.assertTrue(PhoneSession.hold_possible)
        self.assertFalse(SipPhoneSession.hold_possible)
        self.assertFalse(DialInSession.hold_possible)
        self.assertFalse(SipPhoneSession.relays_audio)


class HoldTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        FakeLive.instances = []
        self.temp = tempfile.TemporaryDirectory()
        self.h = PhoneHarness(self.temp.name)
        self.listener = FakeListener()
        self.h.line.hold_listener_factory = lambda environ: self.listener

    async def asyncTearDown(self):
        await self.h.service.shutdown()
        self.temp.cleanup()

    async def call(self, **overrides):
        record, session = await self.h.dial(**overrides)
        ws, live, task = await self.h.connect(record, session)
        live.push({'type': 'session.started', 'session': {}})
        return record, session, ws, live, task

    async def hold(self, session, live, call_id='fc-hold'):
        live.push(tool_call('wait_on_hold', call_id))
        await until(lambda: session.asleep and session.live is None)

    def frames(self, ws, payloads):
        for item in payloads:
            ws.push({'event': 'media', 'media': {'track': 'inbound', 'payload': item}})

    async def wake(self, session, ws, frames=30, onset_ms_ago=200):
        """Someone talks on the line: the listener wakes on the last of `frames` frames."""
        count = len(FakeLive.instances)
        self.listener.wake, self.listener.wake_on = Wake(onset_ms_ago), frames
        payloads = [payload(index) for index in range(frames)]
        self.frames(ws, payloads)
        await until(lambda: len(FakeLive.instances) == count + 1 and not session.asleep)
        return FakeLive.instances[-1], payloads

    def events(self, record, kind):
        return [e['data'] for e in self.h.service.events(record['id']) if e['type'] == kind]

    async def end(self, ws, task, record):
        ws.push({'event': 'stop'})
        await asyncio.wait_for(task, 3)
        return await self.h.service.wait(record['id'], timeout=5, owner='local')

    async def test_a_call_that_can_hold_has_the_tool_and_the_rule(self):
        record, session, ws, live, task = await self.call()
        tools = [tool['name'] for tool in live.config['delegation']['responses']['tools']]
        self.assertIn('wait_on_hold', tools)
        self.assertIn('Hold: when the call is put on hold', live.config['instructions'])
        await self.end(ws, task, record)

    async def test_on_hold_the_voice_is_switched_off_and_the_line_kept(self):
        record, session, ws, live, task = await self.call()
        await self.hold(session, live)
        self.assertTrue(live.close_requested)
        self.assertTrue(self.listener.listening)
        stored = self.h.store.get(record['id'])['hold']
        self.assertEqual((stored['onHold'], stored['times'], stored['detector']), (True, 1, 'fake'))
        self.assertIn('since', stored)
        self.assertIn({'speaker': 'note', 'text': 'On hold.'}, session.ctx.transcript)
        self.assertEqual(self.events(record, 'call.on_hold'), [{'times': 1, 'detector': 'fake'}])
        before = len(live.audio)
        self.frames(ws, [payload(index) for index in range(10)])
        await until(lambda: self.listener.frames >= 10)
        self.assertEqual(len(live.audio), before)  # nothing goes to the closed voice
        self.assertEqual(len(FakeLive.instances), 1)
        self.assertEqual(session.voice_seconds(), 33)  # the closed session's seconds count
        # Closing the voice on purpose is not the call ending, whatever reason GPT-Live gives.
        self.assertIsNone(session.end_reason)
        self.assertIsNone(session.error_detail)
        done = await self.end(ws, task, record)
        self.assertEqual(done['endReason'], 'remote_hangup')
        self.assertEqual(done['usage']['voiceSeconds'], 33)
        self.assertIn('holdSeconds', done['usage'])
        self.assertFalse(done['hold']['onHold'])

    async def test_someone_talking_wakes_the_voice_with_their_first_words(self):
        record, session, ws, live, task = await self.call()
        live.push({'type': 'session.input_transcript.delta', 'delta': 'Please hold.',
                   'start_ms': 0, 'end_ms': 500})
        await until(lambda: session._other_turn)
        await self.hold(session, live)
        woken, payloads = await self.wake(session, ws, frames=30, onset_ms_ago=200)
        # From 300 ms before they started (200 ms ago): the last 25 frames, then the line live.
        self.assertEqual(woken.audio, payloads[5:])
        later = [payload(100 + index) for index in range(5)]
        self.frames(ws, later)
        await until(lambda: len(woken.audio) == 30)
        self.assertEqual(woken.audio[25:], later)
        # It starts with the call so far and why it woke; no opening cue.
        start = woken.config['input']
        self.assertIn('You put this call on hold', start[-1]['content'][0]['text'])
        self.assertIn({'type': 'message', 'role': 'user',
                       'content': [{'type': 'input_text', 'text': 'Please hold.'}]}, start)
        self.assertEqual(woken.appends, [])
        self.assertEqual(len(self.events(record, 'call.hold_wake')), 1)
        # The voice answers: someone is there, and the hold is over.
        woken.push({'type': 'session.output_audio.delta', 'delta': SPEECH})
        await until(lambda: not session.holdbook.on_hold)
        self.assertEqual(self.events(record, 'call.hold_ended')[0]['by'], 'person')
        self.assertIn({'speaker': 'note', 'text': 'Someone picked up after a few seconds on hold.'},
                      session.ctx.transcript)
        stored = self.h.store.get(record['id'])['hold']
        self.assertEqual((stored['onHold'], stored['wakes']), (False, 1))
        self.assertNotIn('since', stored)
        done = await self.end(ws, task, record)
        self.assertEqual(done['usage']['voiceSeconds'], 66)  # both sessions' seconds (33 each)

    async def test_a_recording_sends_it_back_to_waiting_and_is_remembered(self):
        record, session, ws, live, task = await self.call()
        await self.hold(session, live)
        woken, _payloads = await self.wake(session, ws)
        woken.push(tool_call('wait_on_hold', 'fc-hold-2'))
        await until(lambda: woken.close_requested and session.asleep and session.live is None)
        self.assertEqual(self.listener.remembered, 1)
        self.assertTrue(self.listener.listening)
        self.assertEqual(self.events(record, 'call.hold_rewait'), [{'why': 'recording'}])
        self.assertTrue(session.holdbook.on_hold)
        self.assertEqual((session.holdbook.times, session.holdbook.wakes), (1, 1))
        # And it wakes again for the next one.
        again, _payloads = await self.wake(session, ws, frames=5)
        self.assertEqual(len(FakeLive.instances), 3)
        self.assertEqual(session.holdbook.wakes, 2)
        await self.end(ws, task, record)

    async def test_a_woken_voice_that_hears_only_music_goes_back_to_waiting(self):
        record, session, ws, live, task = await self.call()
        await self.hold(session, live)
        with mock.patch.object(phone_hold, 'WAKE_GUARD_SECONDS', 0.3), \
                mock.patch.object(phone_hold, 'WAKE_QUIET_SECONDS', 0.2), \
                mock.patch.object(phone_hold, 'WAKE_GUARD_POLL', 0.05):
            woken, _payloads = await self.wake(session, ws)
            await until(lambda: woken.close_requested and session.asleep)
        self.assertEqual(self.events(record, 'call.hold_rewait'), [{'why': 'quiet'}])
        self.assertEqual(self.listener.remembered, 0)  # music is not remembered as a recording
        self.assertTrue(session.holdbook.on_hold)
        await self.end(ws, task, record)

    async def test_a_hold_nobody_answers_ends_at_max_hold_minutes(self):
        record, session, ws, live, task = await self.call(maxHoldMinutes=1)
        await self.hold(session, live)
        session.holdbook.since -= 61  # a minute on hold already
        await asyncio.wait_for(task, 5)
        done = await self.h.service.wait(record['id'], timeout=5, owner='local')
        self.assertEqual(done['endReason'], 'hold_timeout')
        self.assertEqual(done['result']['outcome'], 'not_reached')
        self.assertEqual(done['result']['summary'],
                         'Nobody picked up after about a minute on hold; Smitline hung up.')
        self.assertTrue(done['hold']['timedOut'])
        self.assertIn(('CA123', {'status': 'completed'}), self.h.twilio.updates)

    async def test_time_on_hold_does_not_count_toward_max_minutes(self):
        record, session, ws, live, task = await self.call()
        session.started_at = time.monotonic() - 700
        session.holdbook.ended_seconds = 600
        self.assertAlmostEqual(session.talk_seconds(), 100, delta=1)
        await self.end(ws, task, record)

    async def test_while_on_hold_there_are_no_prompts_or_quiet_hang_ups(self):
        record, session, ws, live, task = await self.call()
        await self.hold(session, live)
        session.last_activity -= 120  # long quiet: would prompt and hang up on a live call
        session._unanswered = 5
        await asyncio.sleep(1.3)
        self.assertFalse(session._hanging_up)
        self.assertTrue(session.holdbook.on_hold)
        await self.end(ws, task, record)

    async def test_notes_for_the_voice_wait_until_it_wakes(self):
        record, session, ws, live, task = await self.call()
        await self.hold(session, live)
        self.assertTrue(await self.h.line.instruct(record['id'], 'Ask for the reference number.'))
        woken, _payloads = await self.wake(session, ws)
        await until(lambda: any('reference number' in text for _kind, text in woken.appends))
        self.assertEqual(session._held_notes, [])
        await self.end(ws, task, record)

    async def test_ending_on_hold_hangs_up_without_a_goodbye(self):
        record, session, ws, live, task = await self.call()
        await self.hold(session, live)
        await self.h.service.end(record['id'], owner='local')
        await asyncio.wait_for(task, 3)
        self.assertIn(('CA123', {'status': 'completed'}), self.h.twilio.updates)
        self.assertEqual(len(FakeLive.instances), 1)

    async def test_without_a_listener_the_voice_stays_on_and_waits(self):
        def unavailable(environ):
            raise RuntimeError('model files are missing')
        self.h.line.hold_listener_factory = unavailable
        record, session, ws, live, task = await self.call()
        live.push(tool_call('wait_on_hold'))
        await until(lambda: live.outputs)
        output = json.loads(live.outputs[0][1])
        self.assertEqual((output['ok'], output['waiting']), (True, True))
        self.assertFalse(live.close_requested)
        self.assertFalse(session.asleep)
        self.assertIsNone(self.h.store.get(record['id'])['hold']['detector'])
        self.assertEqual(self.events(record, 'call.hold_listener_unavailable'),
                         [{'reason': 'model files are missing'}])
        # What it says right away ("sure, I'll hold") does not end the hold.
        live.push({'type': 'session.output_audio.delta', 'delta': SPEECH})
        await asyncio.sleep(0.1)
        self.assertTrue(session.holdbook.on_hold)
        with mock.patch.object(phone_hold, 'VOICE_HOLD_SETTLE', 0):
            live.push({'type': 'session.output_audio.delta', 'delta': SPEECH})
            await until(lambda: not session.holdbook.on_hold)
        await self.end(ws, task, record)

    async def test_the_agent_hears_at_once_when_the_call_goes_on_hold(self):
        record, session, ws, live, task = await self.call()
        cursor = self.h.service.events(record['id'])[-1]['id']
        waiter = asyncio.create_task(self.h.service.wait(record['id'], owner='local', timeout=30,
                                                         since=cursor))
        await asyncio.sleep(0.05)
        live.push(tool_call('wait_on_hold'))
        found = await asyncio.wait_for(waiter, 3)
        self.assertTrue(found['hold']['onHold'])
        self.assertIn({'speaker': 'note', 'text': 'On hold.'},
                      [{key: line[key] for key in ('speaker', 'text')}
                       for line in found['live']['transcript']])
        await self.end(ws, task, record)


class OwnerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        FakeLive.instances = []
        self.temp = tempfile.TemporaryDirectory()
        self.h = PhoneHarness(self.temp.name)
        self.h.env['SMITLINE_OWNER_PHONE'] = OWNER_PHONE
        self.listener = FakeListener()
        self.h.line.hold_listener_factory = lambda environ: self.listener

    async def asyncTearDown(self):
        await self.h.service.shutdown()
        self.temp.cleanup()

    async def call(self, **overrides):
        record, session = await self.h.dial(**overrides)
        ws, live, task = await self.h.connect(record, session)
        live.push({'type': 'session.started', 'session': {}})
        return record, session, ws, live, task

    def events(self, record, kind):
        return [e['data'] for e in self.h.service.events(record['id']) if e['type'] == kind]

    def owner_calls(self):
        return [call for call in self.h.twilio.created if call['to'] == OWNER_PHONE]

    async def test_connect_owner_rings_them_once_someone_picks_up(self):
        record, session, ws, live, task = await self.call(connectOwner=True)
        live.push(tool_call('wait_on_hold'))
        await until(lambda: session.asleep and session.live is None)
        self.assertEqual(self.owner_calls(), [])  # not while on hold
        self.listener.wake, self.listener.wake_on = Wake(100), 3
        for index in range(3):
            ws.push({'event': 'media', 'media': {'track': 'inbound', 'payload': payload(index)}})
        await until(lambda: len(FakeLive.instances) == 2 and not session.asleep)
        woken = FakeLive.instances[-1]
        woken.push({'type': 'session.output_audio.delta', 'delta': SPEECH})
        await until(lambda: self.owner_calls())
        call = self.owner_calls()[0]
        self.assertEqual(call['from_'], '+15005550006')
        self.assertIn('Hi Robin, it is Smitline.', call['twiml'])
        self.assertIn('Book a table for 4 at 7pm', call['twiml'])
        self.assertTrue(call['status_callback'].endswith(f'/twilio/owner-status/{record["id"]}'))
        self.assertEqual(self.events(record, 'call.owner_ringing'), [{'why': 'pickup'}])
        await until(lambda: any('getting Robin on the line' in text for _kind, text in woken.appends))
        ws.push({'event': 'stop'})
        await asyncio.wait_for(task, 3)

    async def test_pressing_1_connects_them_and_the_voice_comes_back_when_they_leave(self):
        record, session, ws, live, task = await self.call()
        self.assertEqual(await self.h.service.transfer(record['id'], owner='local'),
                         {'transferred': False, 'ringing': True})
        twiml = self.h.line.owner_answer(record['id'], {'Digits': '1'}, PUBLIC)
        self.assertIn('<Stream', twiml)
        self.assertIn('<Parameter name="leg" value="owner"/>', twiml)
        self.assertIn(session.owner_token, twiml)
        self.assertNotIn(session.token, twiml)  # never the call's own token
        self.assertIsNone(self.h.line.claim_owner(record['id'], session.token))
        self.assertIs(self.h.line.claim_owner(record['id'], session.owner_token), session)
        owner_ws = FakeTwilioSocket()
        owner_task = asyncio.create_task(session.run_owner_leg(owner_ws, {'streamSid': 'MZ9'}))
        await until(lambda: session._owner_on_line() and session.live is None)
        self.assertTrue(live.close_requested)  # the voice is off while they talk
        self.assertEqual(self.events(record, 'call.owner_connected'), [{}])
        # The person, to the owner.
        ws.push({'event': 'media', 'media': {'track': 'inbound', 'payload': 'cGVyc29u'}})
        await until(lambda: any(m.get('media', {}).get('payload') == 'cGVyc29u' for m in owner_ws.sent))
        self.assertEqual(owner_ws.sent[-1]['streamSid'], 'MZ9')
        # The owner, to the person.
        owner_ws.push({'event': 'media', 'media': {'track': 'inbound', 'payload': 'b3duZXI='}})
        await until(lambda: any(m.get('event') == 'media' and m['media']['payload'] == 'b3duZXI='
                                for m in ws.sent))
        # They hang up: the voice comes back to see the person off.
        owner_ws.push({'event': 'stop'})
        await asyncio.wait_for(owner_task, 3)
        await until(lambda: len(FakeLive.instances) == 2 and not session.asleep)
        self.assertIn('took over this call',
                      FakeLive.instances[-1].config['input'][-1]['content'][0]['text'])
        self.assertIn({'speaker': 'note', 'text': 'Robin left the call.'}, session.ctx.transcript)
        ws.push({'event': 'stop'})
        await asyncio.wait_for(task, 3)
        done = await self.h.service.wait(record['id'], timeout=5, owner='local')
        self.assertGreaterEqual(done['usage']['ownerTalkedSeconds'], 1)

    async def test_any_other_key_and_the_voice_carries_on(self):
        record, session, ws, live, task = await self.call()
        await self.h.service.transfer(record['id'], owner='local')
        twiml = self.h.line.owner_answer(record['id'], {'Digits': '2'}, PUBLIC)
        self.assertIn(OWNER_DECLINED, twiml)
        self.assertNotIn('<Stream', twiml)
        await until(lambda: self.events(record, 'call.owner_unavailable'))
        self.assertEqual(self.events(record, 'call.owner_unavailable'), [{'reason': 'declined'}])
        await until(lambda: any("can't come to the phone" in text for _kind, text in live.appends))
        self.assertIsNone(self.h.line.claim_owner(record['id'], session.owner_token))
        # Their phone not answering, as the provider reports it, is the same.
        await self.h.service.transfer(record['id'], owner='local')
        self.h.line.on_owner_status(record['id'], {'CallStatus': 'no-answer'})
        await until(lambda: len(self.events(record, 'call.owner_unavailable')) == 2)
        self.assertEqual(self.events(record, 'call.owner_unavailable')[1], {'reason': 'no_answer'})
        ws.push({'event': 'stop'})
        await asyncio.wait_for(task, 3)

    async def test_connect_owner_needs_their_phone(self):
        del self.h.env['SMITLINE_OWNER_PHONE']
        with self.assertRaises(CallError) as caught:
            await self.h.service.create(brief(connectOwner=True))
        self.assertEqual(caught.exception.code, 'owner_phone_missing')


class RecordTests(unittest.TestCase):
    def test_the_owners_phone_is_a_second_call_in_the_cost(self):
        record = {'channel': 'phone', 'status': 'completed',
                  'usage': {'phoneSeconds': 120, 'phoneProvider': 'twilio', 'ownerLegSeconds': 90}}
        phone = next(item for item in call_cost(record, {})['items'] if item['kind'] == 'phone')
        self.assertEqual(phone['ownerLegSeconds'], 90)
        self.assertEqual(phone['amount'], 0.056)  # 2 + 2 started minutes at $0.014

    def test_notes_are_marked_and_a_hold_nobody_answered_needs_no_summary(self):
        entries = [{'speaker': 'other', 'text': 'Please hold.'},
                   {'speaker': 'note', 'text': 'On hold.'},
                   {'speaker': 'note', 'text': 'Nobody picked up after about an hour on hold; Smitline hung up.'}]
        self.assertIn('Note: On hold.', transcript_text(entries))
        result = result_without_conversation('hold_timeout', transcript=entries)
        self.assertEqual(result['outcome'], 'not_reached')
        self.assertEqual(result['summary'], entries[-1]['text'])
        # Once the assistant talked with someone, the summary model judges it.
        talked = entries + [{'speaker': 'agent', 'text': 'Hi, I am calling about a bill.'}]
        self.assertIsNone(result_without_conversation('hold_timeout', transcript=talked))


if __name__ == '__main__':
    unittest.main()
