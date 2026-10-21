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
