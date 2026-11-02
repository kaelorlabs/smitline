"""A goodbye ends a phone call, and a meeting once asked to leave, as soon as it has been heard."""
import asyncio
import base64
import struct
import tempfile
import unittest
from unittest.mock import patch

from meeting_lifecycle import asks_to_leave, only_closing, says_goodbye, voicemail_prompt
from test_phone import FakeLive, PhoneHarness, until


class WordsTests(unittest.TestCase):
    def test_goodbyes(self):
        for text in ('Sure thing. Talk soon, goodbye.', 'Thanks. Bye.', 'Okay, hanging up.',
                     'Take care!', 'Have a great evening.', 'bye-bye'):
            self.assertTrue(says_goodbye(text), text)
        for text in ('Hi, how can I help?', 'I will pass that on.', 'Goodbye is a song. Anyway, '
                     'the quote for the roof is four thousand dollars, and they can start Monday.'):
            self.assertFalse(says_goodbye(text), text)

    def test_what_people_say_back_to_a_goodbye(self):
        for text in ('', 'Bye', 'Okay, thanks, bye!', 'Yeah, you too.', 'Okay hang up', 'Cheers'):
            self.assertTrue(only_closing(text), text)
        for text in ('Wait, one more thing', 'Bye, and can you send the invoice?', 'Hello?',
                     'You there'):
            self.assertFalse(only_closing(text), text)

    def test_voicemail_prompts(self):
        for text in ("We've sent your call to our voice message system.",
                     "Robin isn't available right now. When you hear the beep, record your message.",
                     'Please leave a message after the tone.', 'At the tone, please record your message.',
                     'The mailbox is full and cannot accept any new messages.',
                     'You have reached the voicemail box of Robin Lee.'):
            self.assertTrue(voicemail_prompt(text), text)
        for text in ('Can I take a message?', 'I got your voicemail yesterday, thanks.',
                     'Sorry, she is not available right now.', 'I left it in your mailbox.',
                     'Beep me when you get here.', 'Let me record that for you.'):
            self.assertFalse(voicemail_prompt(text), text)

    def test_asked_to_leave(self):
        for text in ('Smitline, you can leave now.', 'Thanks, you can drop off now', 'Please leave',
                     'Smit line, go now', 'Okay Smitline, time to hang up',
                     # From the Zoom test on 2026-10-21: neither was taken as a request to leave.
                     'Mm. Okay, fine, I think it... looks overall fine. Um, hang up now, then',
                     '- Okay bye, hang up', 'Hang up.', 'You should leave now.', 'Smitline, end the call.'):
            self.assertTrue(asks_to_leave(text), text)
        for text in ('I have to leave early today.', 'Can you leave a note in the doc?',
                     'Priya is dropping off at five.', 'Smitline, what is the PR link?',
                     'I need to hang up in five minutes.', "Don't hang up yet.",
                     "We'll hang up after this.", 'Let me hang up and call back.'):
            self.assertFalse(asks_to_leave(text), text)


class LeaveJudgeTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_model_decides_and_failures_stay(self):
        from meeting_lifecycle import LEAVE_JUDGE_LINES, LeaveJudge
        sent = []

        def answer(body):
            async def post(payload):
                sent.append(payload)
                if isinstance(body, Exception):
                    raise body
                return {'output': [{'type': 'message', 'content': [
                    {'type': 'output_text', 'text': body}]}]}
            return post

        captions = [{'speaker': 'meeting', 'text': f'line {n}'} for n in range(40)]
        captions.append({'speaker': 'agent', 'text': 'Bye everyone!'})
        judge = LeaveJudge('key', post=answer('{"leave": true, "reason": "completed"}'))
        self.assertEqual(await judge.decide(captions, 'Smitline AI'), (True, 'completed'))
        request = sent[-1]
        self.assertEqual(request['text']['format']['name'], 'leave_decision')
        self.assertTrue(request['input'].endswith('Smitline AI: Bye everyone!'))
        self.assertNotIn('line 0', request['input'])  # only the last LEAVE_JUDGE_LINES lines
        self.assertEqual(request['input'].count('Participant: '), LEAVE_JUDGE_LINES - 1)
        for body in ('{"leave": "yes", "reason": "completed"}', 'not json', RuntimeError('down')):
            judge = LeaveJudge('key', post=answer(body))
            self.assertEqual(await judge.decide(captions, 'Smitline AI'), (False, 'completed' if '{' in str(body) else 'other'))
        judge = LeaveJudge('key', post=answer('{"leave": true, "reason": "bored"}'))
        self.assertEqual(await judge.decide(captions, 'Smitline AI'), (True, 'other'))


class PacerSpeechTests(unittest.IsolatedAsyncioTestCase):
    async def test_streamed_silence_is_not_speech(self):
        from phone_line import OutputPacer
        sent = []

        async def send(message):
            sent.append(message)
        pacer = OutputPacer(send)
        runner = asyncio.create_task(pacer.run())
        try:
            self.assertFalse(pacer.speech_pending())
            pacer.offer(base64.b64encode(b'\x2a' * 1600).decode())  # 0.2 s of speech
            self.assertTrue(pacer.speech_pending())
            # GPT-Live keeps streaming silence after it: the queue never empties for long.
            for _ in range(15):
                pacer.offer(base64.b64encode(b'\xff' * 160).decode())
                await asyncio.sleep(0.02)
            self.assertTrue(await pacer.speech_finished(timeout=2))
            self.assertFalse(pacer.speech_pending())
        finally:
            runner.cancel()
            await asyncio.gather(runner, return_exceptions=True)

    async def test_speech_is_still_playing_after_it_was_all_sent(self):
        from phone_line import OutputPacer
        now = [0.0]

        async def sleep(seconds):
            now[0] += seconds
        sent = []

        async def send(message):
            sent.append(message)
        pacer = OutputPacer(send, clock=lambda: now[0], sleep=sleep, cushion=0)
        runner = asyncio.create_task(pacer.run())
        try:
            pacer.offer(base64.b64encode(bytes([0x2a]) * 8000).decode())  # one second of speech
            await until(lambda: not pacer.queue and pacer._next is None)
            # All of it went out, up to the lead ahead of playback: the provider still plays it.
            self.assertTrue(pacer.speech_pending())
            self.assertGreater(pacer.speech_until, now[0] + 0.3)
            now[0] = pacer.speech_until + 0.01
            self.assertFalse(pacer.speech_pending())
        finally:
            runner.cancel()
            await asyncio.gather(runner, return_exceptions=True)


class PhoneGoodbyeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        FakeLive.instances = []
        self.temp = tempfile.TemporaryDirectory()
        self.h = PhoneHarness(self.temp.name)

    async def asyncTearDown(self):
        await self.h.service.shutdown()
        self.temp.cleanup()

    async def connected(self):
        record, session = await self.h.dial()
        ws, live, task = await self.h.connect(record, session)
        live.push({'type': 'session.started', 'session': {}})
        return record, session, ws, live, task

    def say(self, live, text, *, audio_bytes=1600):
        live.push({'type': 'session.output_transcript.delta', 'delta': text})
        live.push({'type': 'session.output_audio.delta',
                   'delta': base64.b64encode(b'\x2a' * audio_bytes).decode()})

    async def test_a_goodbye_hangs_up_once_heard_without_the_backend(self):
        record, session, ws, live, task = await self.connected()
        self.say(live, "I'm Robin's AI assistant, by the way. Great, talk soon. Bye!")
        await until(lambda: ws.closed, 5)
        events = [e['type'] for e in self.h.service.events(record['id'])]
        self.assertIn('call.goodbye_hangup', events)
        self.assertNotIn('call.end_requested', events)  # no end_call from the backend
        await asyncio.wait_for(task, 3)

    async def test_their_bye_back_still_hangs_up(self):
        record, session, ws, live, task = await self.connected()
        self.say(live, "I'm Robin's AI assistant. Thanks, bye!")
        live.push({'type': 'session.input_transcript.delta', 'delta': 'Okay, bye!'})
        await until(lambda: ws.closed, 5)
        await asyncio.wait_for(task, 3)

    async def test_carrying_on_after_the_goodbye_keeps_the_call(self):
        record, session, ws, live, task = await self.connected()
        self.say(live, "I'm Robin's AI assistant. Okay, bye!")
        live.push({'type': 'session.input_transcript.delta', 'delta': 'Wait, one more thing'})
        await asyncio.sleep(3)
        self.assertFalse(ws.closed)
        self.assertNotIn('call.goodbye_hangup',
                         [e['type'] for e in self.h.service.events(record['id'])])
        # A later goodbye, with nothing after it, ends it.
        self.say(live, 'Sure, I will tell Robin. Bye now!')
        await until(lambda: ws.closed, 5)
        await asyncio.wait_for(task, 3)

    async def test_it_never_hangs_up_on_someone_still_talking(self):
        record, session, ws, live, task = await self.connected()
        self.say(live, "I'm Robin's AI assistant. Okay, bye!")
        # They start talking at once; no transcript of it has arrived yet, only their voice.
        loud = base64.b64encode(bytes([0x2a]) * 160).decode()
        quiet = base64.b64encode(bytes([0xff]) * 160).decode()
        for _ in range(150):  # three seconds
            ws.push({'event': 'media', 'media': {'track': 'inbound', 'payload': loud}})
            await asyncio.sleep(0.02)
        self.assertFalse(ws.closed)
        self.assertNotIn('call.goodbye_hangup',
                         [e['type'] for e in self.h.service.events(record['id'])])
        # They finish with a goodbye of their own, then quiet: now it hangs up.
        live.push({'type': 'session.input_transcript.delta', 'delta': 'Okay, thanks, bye!'})
        for _ in range(40):
            ws.push({'event': 'media', 'media': {'track': 'inbound', 'payload': quiet}})
            await asyncio.sleep(0.02)
        await until(lambda: ws.closed, 6)
        await asyncio.wait_for(task, 3)

    async def test_ending_at_a_voicemail_hangs_up_without_a_message(self):
        record, session, ws, live, task = await self.connected()
        live.push({'type': 'session.input_transcript.delta', 'delta': 'Hello,'})
        self.say(live, 'Hey, hi!')  # it talks over the greeting before it knows
        # The words arrive in pieces, none of them a voicemail prompt on its own.
        for piece in ("Robin isn't available right now. Please record your", ' message after the', ' tone.'):
            live.push({'type': 'session.input_transcript.delta', 'delta': piece})
        await until(lambda: session.voicemail_heard)
        await self.h.service.end(record['id'], owner='local')
        await until(lambda: ws.closed, 3)
        # No goodbye cue and no disclosure reminder: nothing more is said onto the recording.
        sent = ' '.join(str(content) for _kind, content in live.appends)
        self.assertNotIn('end the call now', sent)
        self.assertNotIn('Before the call ends', sent)
        events = [e['type'] for e in self.h.service.events(record['id'])]
        self.assertIn('call.voicemail_hangup', events)
        await asyncio.wait_for(task, 3)
        done = await self.h.service.wait(record['id'], timeout=5, owner='local')
        self.assertEqual(done['endReason'], 'hangup')

    async def test_ending_with_a_person_still_says_goodbye(self):
        record, session, ws, live, task = await self.connected()
        live.push({'type': 'session.input_transcript.delta',
                   'delta': "Sorry, she's not available. Can I take a message? I did get your voicemail."})
        await asyncio.sleep(0.2)
        await self.h.service.end(record['id'], owner='local')
        await until(lambda: any(kind == 'session.commentary.append' for kind, _ in live.appends), 3)
        self.assertFalse(session.voicemail_heard)
        self.assertFalse(ws.closed)
        self.say(live, "I'm Robin's AI assistant, by the way. Thanks so much, goodbye!")
        await until(lambda: ws.closed, 5)
        await asyncio.wait_for(task, 3)

    async def test_it_waits_for_the_goodbye_to_be_heard(self):
        record, session, ws, live, task = await self.connected()
        # Three seconds of goodbye: the line stays up until it has played.
        self.say(live, "I'm Robin's AI assistant. Talk soon, goodbye.", audio_bytes=24000)
        await asyncio.sleep(1.5)
        self.assertFalse(ws.closed)
        await until(lambda: ws.closed, 6)
        await asyncio.wait_for(task, 3)

    # At a voicemail ---------------------------------------------------------

    def hear(self, live, text):
        live.push({'type': 'session.input_transcript.delta', 'delta': text})

    def voicemail_greeting(self, live, prompt=' Please leave a message after the tone.'):
        # The greeting on call-91d75999d4bb843b; the prompt arrives as its own piece.
        self.hear(live, 'Call is not available.')
        self.hear(live, prompt)

    async def message_heard(self, session):
        """The agent's message (its first speech) has played to the end."""
        await until(lambda: session.pacer.speech_until > 0 and not session.pacer.speech_pending())

    async def voicemail_hangups(self, record):
        done = await self.h.service.wait(record['id'], timeout=5, owner='local')
        events = self.h.service.events(record['id'])
        return [e['data'] for e in events if e['type'] == 'call.voicemail_hangup'], done

    async def test_a_voicemail_message_without_a_goodbye_hangs_up_once_heard(self):
        # call-91d75999d4bb843b, 2026-11-02: after the beep the agent said one sentence with no
        # goodbye and never ended the call; the line stayed open, and billed, until the mailbox
        # cut it off two minutes later.
        from phone_line import VOICEMAIL_QUIET_SECONDS
        record, session, ws, live, task = await self.connected()
        self.h.line.on_amd(record['id'], 'machine_end_beep')
        self.voicemail_greeting(live)
        await until(lambda: session.voicemail_heard)
        self.say(live, "Hi, I'm calling on behalf of Robin about your question on connecting a "
                       'custom MCP server to your account.', audio_bytes=8000)  # one second
        await self.message_heard(session)
        loop = asyncio.get_running_loop()
        heard = loop.time()
        await asyncio.sleep(VOICEMAIL_QUIET_SECONDS - 1)
        self.assertFalse(ws.closed)  # it waits for a few seconds of quiet first
        await until(lambda: ws.closed, 3)
        self.assertGreaterEqual(loop.time() - heard, VOICEMAIL_QUIET_SECONDS - 0.1)
        await asyncio.wait_for(task, 3)
        hangups, done = await self.voicemail_hangups(record)
        self.assertEqual(hangups, [{'reason': 'message_left'}])
        # Ended as the model's end_call with voicemail_left ends it, without that call.
        self.assertEqual((done['endReason'], done['result']['outcome']), ('voicemail', 'voicemail'))
        self.assertNotIn('call.end_requested', [e['type'] for e in self.h.service.events(record['id'])])
        self.assertIn(('CA123', {'status': 'completed'}), self.h.twilio.updates)
        # Nothing more was asked for on the recording: no goodbye cue, no disclosure reminder.
        sent = ' '.join(str(content) for _kind, content in live.appends)
        self.assertNotIn('end the call now', sent)
        self.assertNotIn('Before the call ends', sent)

    async def test_the_voicemail_time_cap_cuts_in_while_the_agent_talks(self):
        self.h.env['SMITLINE_VOICEMAIL_MAX_SECONDS'] = '1.5'
        record, session, ws, live, task = await self.connected()
        self.voicemail_greeting(live)
        await until(lambda: session.voicemail_heard)
        loop = asyncio.get_running_loop()
        heard = loop.time()
        # It talks on into the recording: ten seconds of speech, far past the cap.
        self.say(live, 'Hi, this is about the roof, and the invoice, and also the gutters...',
                 audio_bytes=80000)
        await until(lambda: ws.closed, 4)
        self.assertGreaterEqual(loop.time() - heard, 1.4)
        self.assertLess(loop.time() - heard, 3.0)
        self.assertTrue(session.pacer.speech_pending())  # cut off, not waited for
        await asyncio.wait_for(task, 3)
        hangups, done = await self.voicemail_hangups(record)
        self.assertEqual(hangups, [{'reason': 'time_cap'}])
        self.assertEqual(done['endReason'], 'voicemail')

    async def test_the_voicemail_time_cap_hangs_up_when_the_agent_never_speaks(self):
        self.h.env['SMITLINE_VOICEMAIL_MAX_SECONDS'] = '1'
        record, session, ws, live, task = await self.connected()
        self.voicemail_greeting(live)
        await until(lambda: session.voicemail_heard)
        await until(lambda: ws.closed, 3)
        await asyncio.wait_for(task, 3)
        hangups, done = await self.voicemail_hangups(record)
        self.assertEqual(hangups, [{'reason': 'time_cap'}])
        self.assertEqual(done['endReason'], 'voicemail')

    def test_the_voicemail_time_cap_setting(self):
        from phone_line import VOICEMAIL_MAX_SECONDS, voicemail_max_seconds
        self.assertEqual(VOICEMAIL_MAX_SECONDS, 60.0)
        self.assertEqual(voicemail_max_seconds({}), 60.0)
        self.assertEqual(voicemail_max_seconds({'SMITLINE_VOICEMAIL_MAX_SECONDS': ' 90 '}), 90.0)
        self.assertEqual(voicemail_max_seconds({'SMITLINE_VOICEMAIL_MAX_SECONDS': '0'}), 0.0)  # off
        for bad in ('soon', '-5', 'nan', 'inf'):
            self.assertEqual(voicemail_max_seconds({'SMITLINE_VOICEMAIL_MAX_SECONDS': bad}), 60.0, bad)

    async def test_no_voicemail_hangup_before_the_agent_speaks_after_the_beep(self):
        with patch('phone_line.VOICEMAIL_QUIET_SECONDS', 0.5):
            record, session, ws, live, task = await self.connected()
            self.hear(live, 'Hi, you have reached Robin.')
            self.say(live, 'Hey, hi!')  # it talks over the greeting before it knows
            self.hear(live, ' Please record your message after the beep.')
            await until(lambda: session.voicemail_heard)
            # The prompt, the beep, then quiet: nothing it said is on the recording yet.
            await asyncio.sleep(2.0)
            self.assertFalse(ws.closed)
            self.assertNotIn('call.voicemail_hangup',
                             [e['type'] for e in self.h.service.events(record['id'])])
            # Its message after the beep, then quiet: now it hangs up.
            self.say(live, "Hi, it's Robin's AI assistant, calling about Friday. Robin will "
                           'follow up by text.')
            await until(lambda: ws.closed, 3)
            await asyncio.wait_for(task, 3)
        hangups, done = await self.voicemail_hangups(record)
        self.assertEqual(hangups, [{'reason': 'message_left'}])
        self.assertEqual(done['endReason'], 'voicemail')

    async def test_thanks_or_bye_after_the_message_is_still_a_voicemail(self):
        with patch('phone_line.VOICEMAIL_QUIET_SECONDS', 0.5):
            record, session, ws, live, task = await self.connected()
            self.voicemail_greeting(live)
            await until(lambda: session.voicemail_heard)
            self.say(live, "Hi, it's Robin's AI assistant, calling about Friday.")
            await self.message_heard(session)
            self.hear(live, 'Thank you. Goodbye.')
            await until(lambda: ws.closed, 3)
            self.assertTrue(session.voicemail_heard)
            await asyncio.wait_for(task, 3)
        hangups, done = await self.voicemail_hangups(record)
        self.assertEqual(hangups, [{'reason': 'message_left'}])

    async def test_a_person_after_the_voicemail_prompt_is_still_a_person(self):
        self.h.env['SMITLINE_VOICEMAIL_MAX_SECONDS'] = '2'
        with patch('phone_line.VOICEMAIL_QUIET_SECONDS', 0.5):
            record, session, ws, live, task = await self.connected()
            self.voicemail_greeting(live)
            await until(lambda: session.voicemail_heard)
            self.say(live, "Hi, I'm calling on behalf of Robin about Friday's booking.")
            await self.message_heard(session)
            # Someone picks up after all.
            self.hear(live, "Hello? Sorry, I'm here. Who is this?")
            await until(lambda: not session.voicemail_heard)
            # Past both the quiet window and the cap: a person is never hung up on as a voicemail.
            await asyncio.sleep(2.5)
            self.assertFalse(ws.closed)
            # Ending the call now says goodbye to them, as to anyone.
            await self.h.service.end(record['id'], owner='local')
            await until(lambda: any('end the call now' in str(text) for _kind, text in live.appends))
            self.assertFalse(ws.closed)
            self.say(live, "I'm Robin's AI assistant, by the way. Thanks, goodbye!")
            await until(lambda: ws.closed, 5)
            await asyncio.wait_for(task, 3)
        hangups, done = await self.voicemail_hangups(record)
        self.assertEqual(hangups, [])
        self.assertEqual(done['endReason'], 'hangup')

    async def test_a_call_screener_is_a_person_until_it_offers_voicemail(self):
        # A screener asking for a name at the tone, which then hands over to voicemail, like the
        # phone's call screening in the live test on 2026-10-23.
        with patch('phone_line.VOICEMAIL_QUIET_SECONDS', 0.5):
            record, session, ws, live, task = await self.connected()
            self.hear(live, 'Please say your name after the tone.')
            await until(lambda: session.voicemail_heard)
            self.say(live, "Hi, I'm calling on behalf of Robin about Friday's booking.")
            await self.message_heard(session)
            self.hear(live, 'Thanks. Please stay on the line.')
            await until(lambda: not session.voicemail_heard)
            await asyncio.sleep(1.5)  # three times the quiet window: it waits for the person
            self.assertFalse(ws.closed)
            self.hear(live, "I'm sorry, this person is not available. Please leave your message "
                            'after the tone.')
            await until(lambda: session.voicemail_heard)
            self.say(live, "Hi, it's Robin's AI assistant. Robin will text you about Friday.")
            await until(lambda: ws.closed, 3)
            await asyncio.wait_for(task, 3)
        events = [e['type'] for e in self.h.service.events(record['id'])]
        self.assertEqual(events.count('call.voicemail_heard'), 2)
        hangups, done = await self.voicemail_hangups(record)
        self.assertEqual(hangups, [{'reason': 'message_left'}])
        self.assertEqual(done['endReason'], 'voicemail')


class MeetingGoodbyeTests(unittest.IsolatedAsyncioTestCase):
    decision = (False, 'other')

    def fake_judge(self):
        """The leave check, answering self.decision and noting what it was shown."""
        test = self
        test.judged = []

        class FakeJudge:
            def __init__(self, *_args, **_kwargs):
                pass

            async def decide(self, captions, name):
                test.judged.append((list(captions), name))
                return test.decision
        return FakeJudge

    async def run_meeting(self, script):
        import copy
        import bridge
        import meeting_lifecycle
        from runtime_config import RuntimeConfig
        from test_participation import Adapter, FakeLiveSocket, IdleSpeaker, Mic, RecordingArchive
        socket = FakeLiveSocket(bridge)
        record = RecordingArchive()
        state = copy.deepcopy(bridge.state)
        stop = asyncio.Event()
        with patch.object(bridge, 'LeaveJudge', self.fake_judge()), \
                patch.object(bridge, 'ClientSession', socket.client), \
                patch.object(bridge, 'record', record), \
                patch.object(bridge, 'state', state), \
                patch.object(bridge, 'stop', stop), \
                patch.object(bridge, 'LEAVE_SPEECH_TIMEOUT', 1.0), \
                patch.object(meeting_lifecycle, 'LEAVE_QUIET_SECONDS', 0.3):
            task = asyncio.create_task(bridge.run_voice(
                IdleSpeaker(), Mic(), None, 'test-only',
                RuntimeConfig.from_environ({'SMITLINE_MEETING_INTRO': '0'}), Adapter()))
            try:
                await socket.events.put({'type': 'session.started'})
                await script(socket, stop)
                await until(lambda: any(e['type'] == 'session.close' for e in socket.sent), 10)
                await socket.events.put({'type': 'session.closed', 'usage': {'seconds': 9}})
                await asyncio.wait_for(task, 3)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        return state, record

    async def test_asked_to_leave_and_said_goodbye_it_leaves(self):
        async def script(socket, _stop):
            await socket.events.put({'type': 'session.input_transcript.delta',
                                     'delta': 'Thanks Smitline, you can leave now.'})
            await asyncio.sleep(.1)
            await socket.events.put({'type': 'session.output_transcript.delta',
                                     'delta': 'Okay, goodbye everyone!'})
            # No leave_meeting from the backend.
        state, record = await self.run_meeting(script)
        self.assertEqual(state['endReason'], 'left_asked_to_leave')
        self.assertIn('goodbye_leave', [kind for kind, _ in record.events])

    async def test_a_bare_hang_up_and_a_goodbye_leaves(self):
        async def script(socket, _stop):
            await socket.events.put({'type': 'session.input_transcript.delta',
                                     'delta': 'Okay, fine, it looks overall fine. Um, hang up now, then'})
            await asyncio.sleep(.1)
            await socket.events.put({'type': 'session.output_transcript.delta',
                                     'delta': 'Alright, bye.'})
            # No leave_meeting from the backend.
        state, record = await self.run_meeting(script)
        self.assertEqual(state['endReason'], 'left_asked_to_leave')
        self.assertIn('goodbye_leave', [kind for kind, _ in record.events])

    async def test_it_leaves_by_its_own_judgment(self):
        self.decision = (True, 'completed')

        async def script(socket, _stop):
            await socket.events.put({'type': 'session.input_transcript.delta',
                                     'delta': "That's everything for today, thanks Smitline."})
            await asyncio.sleep(.1)
            await socket.events.put({'type': 'session.output_transcript.delta',
                                     'delta': 'Glad I could help. Bye everyone!'})
        state, record = await self.run_meeting(script)
        self.assertEqual(state['endReason'], 'left_completed')
        kinds = [kind for kind, _ in record.events]
        self.assertIn('leave_judged', kinds)
        self.assertIn('goodbye_leave', kinds)
        self.assertEqual(len(self.judged), 1)

    async def test_its_judgment_can_keep_it_in_the_meeting(self):
        async def script(socket, stop):
            await socket.events.put({'type': 'session.input_transcript.delta',
                                     'delta': 'Thanks for that. Over to Priya now.'})
            await asyncio.sleep(.1)
            await socket.events.put({'type': 'session.output_transcript.delta',
                                     'delta': 'Sure. Bye for now, Priya, talk soon!'})
            await asyncio.sleep(1.5)
            self.assertFalse(stop.is_set())
            stop.set()
        state, record = await self.run_meeting(script)
        self.assertNotIn('goodbye_leave', [kind for kind, _ in record.events])
        self.assertEqual(len(self.judged), 1)  # asked once for this goodbye, not every moment

    async def test_a_goodbye_to_someone_else_does_not_leave(self):
        async def script(socket, stop):
            await socket.events.put({'type': 'session.input_transcript.delta',
                                     'delta': 'I have to leave early today, bye all.'})
            await asyncio.sleep(.1)
            await socket.events.put({'type': 'session.output_transcript.delta',
                                     'delta': 'Bye, Priya!'})
            await asyncio.sleep(1.5)
            self.assertFalse(stop.is_set())
            stop.set()
        state, record = await self.run_meeting(script)
        self.assertNotIn('goodbye_leave', [kind for kind, _ in record.events])
        self.assertNotEqual(state.get('endReason'), 'left_asked_to_leave')

    async def test_carrying_on_after_the_goodbye_keeps_it_in_the_meeting(self):
        async def script(socket, stop):
            await socket.events.put({'type': 'session.input_transcript.delta',
                                     'delta': 'Smitline, you can drop off now.'})
            await asyncio.sleep(.1)
            await socket.events.put({'type': 'session.output_transcript.delta',
                                     'delta': 'Sure, bye!'})
            await socket.events.put({'type': 'session.input_transcript.delta',
                                     'delta': 'Actually wait, what was the PR number?'})
            await asyncio.sleep(1.5)
            self.assertFalse(stop.is_set())
            stop.set()
        state, record = await self.run_meeting(script)
        self.assertNotIn('goodbye_leave', [kind for kind, _ in record.events])


if __name__ == '__main__':
    unittest.main()
