"""Leaving a meeting cleanly: out of the meeting platform first, and only then GPT-Live, the
archive and the container; the daemon waits for the bot to be out; and an answer to "you can
leave" in any language leaves as quickly as an English goodbye.

On 2026-11-05 a Zoom bot's tile stayed in the meeting for four minutes after it left: the leave
came last, never answered Zoom's confirmation, and the browser was killed instead. A goodbye in
Hindi after "you can hang up" waited 27 seconds for the backend."""
import asyncio
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import adapters_base
from adapters_base import leave_meeting
from meeting_supervisor import ProductionMeetingSupervisor, out_of_the_meeting
from test_meeting_pool import owned
from test_meeting_supervisor import FakeDaemon, FakeHealth, FakeLauncher, session
from test_participation import Adapter, FakeLiveSocket, IdleSpeaker, Mic, RecordingArchive
from test_remote_meetings import RemoteCase


async def until(check, seconds=3.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + seconds
    while not check():
        if loop.time() > deadline:
            raise AssertionError('timed out waiting')
        await asyncio.sleep(0.01)


class Platform(Adapter):
    """A meeting platform that notes, when it is left, what GPT-Live had been sent by then."""

    def __init__(self, socket=None, *, fail=None, hang=False):
        super().__init__()
        self.socket, self.fail, self.hang = socket, fail, hang
        self.leaves = []  # for each leave: the types of the events sent to GPT-Live before it
        self.mutes_after_leaving = 0

    async def leave(self):
        self.leaves.append([event['type'] for event in self.socket.sent] if self.socket else [])
        if self.hang:
            await asyncio.Event().wait()
        if self.fail:
            raise RuntimeError(self.fail)

    async def mute(self):
        if self.leaves:
            self.mutes_after_leaving += 1
        await super().mute()


class LeaveMeetingTests(unittest.IsolatedAsyncioTestCase):
    async def test_it_leaves_once_and_says_so(self):
        platform, state, events = Platform(), {}, []

        def record(kind, **data):
            events.append((kind, data))
        first, second = await asyncio.gather(leave_meeting(platform, state, record=record),
                                             leave_meeting(platform, state, record=record))
        self.assertEqual((first, second), (True, True))
        self.assertTrue(await leave_meeting(platform, state, record=record))
        self.assertEqual(len(platform.leaves), 1)  # the platform is never asked twice
        self.assertEqual(events, [('left', {})])
        self.assertEqual(state['leaveState'], 'left')
        self.assertTrue(platform.left_meeting)

    async def test_a_leave_that_fails_is_reported_and_unloads_the_page(self):
        class Page:
            def __init__(self):
                self.urls = []

            async def goto(self, url, **kwargs):
                self.urls.append(url)
        page, state, events = Page(), {}, []
        platform = Platform(fail='Zoom still showed the meeting after Leave and Leave Meeting')
        self.assertFalse(await leave_meeting(
            platform, state, page=page, record=lambda kind, **data: events.append((kind, data))))
        reason = 'RuntimeError: Zoom still showed the meeting after Leave and Leave Meeting'
        self.assertEqual(events, [('leave_failed', {'reason': reason})])
        self.assertEqual((state['leaveState'], state['leaveError']), ('leave_failed', reason))
        self.assertEqual(page.urls, ['about:blank'])
        self.assertFalse(getattr(platform, 'left_meeting', False))
        self.assertFalse(await leave_meeting(platform, state, page=page))  # the same answer
        self.assertEqual((len(platform.leaves), page.urls), (1, ['about:blank']))

    async def test_a_platform_that_never_answers_is_given_a_few_seconds(self):
        state = {}
        loop = asyncio.get_running_loop()
        began = loop.time()
        with patch.object(adapters_base, 'LEAVE_TIMEOUT', 0.2):
            self.assertFalse(await leave_meeting(Platform(hang=True), state))
        self.assertLess(loop.time() - began, 1.0)
        self.assertEqual(state['leaveState'], 'leave_failed')
        self.assertIn('within 0.2 seconds', state['leaveError'])


def leave_call(reason):
    return {'type': 'response.event', 'delegation_id': 'dlg-1', 'event': {
        'type': 'response.output_item.done', 'item': {
            'type': 'function_call', 'call_id': 'fc-1', 'name': 'leave_meeting',
            'arguments': json.dumps({'reason': reason})}}}


class VoiceRun:
    """bridge.run_voice with a fake GPT-Live, a fake platform and the archive's events in order,
    stages among them."""

    def __init__(self, test, platform=None, judge=(False, 'other')):
        import bridge
        self.bridge = bridge
        self.socket = FakeLiveSocket(bridge)
        self.platform = platform or Platform()
        self.platform.socket = self.socket
        self.record = RecordingArchive()
        self.state = copy.deepcopy(bridge.state)
        self.stop = asyncio.Event()
        self.judged = []
        run = self

        class Judge:
            def __init__(self, *_args, **_kwargs):
                pass

            async def decide(self, captions, name):
                run.judged.append([line['text'] for line in captions])
                return judge
        self.Judge = Judge

    def stage(self, value):
        self.state['stage'] = value
        self.record.event('stage', stage=value)

    def kinds(self):
        return [(kind if kind != 'stage' else 'stage:' + data['stage']) for kind, data in self.record.events]

    def sent(self, kind):
        return any(event['type'] == kind for event in self.socket.sent)

    async def __call__(self, script, *, close=True):
        import meeting_lifecycle
        from runtime_config import RuntimeConfig
        bridge = self.bridge
        with patch.object(bridge, 'ClientSession', self.socket.client), \
                patch.object(bridge, 'record', self.record), \
                patch.object(bridge, 'state', self.state), \
                patch.object(bridge, 'stage', self.stage), \
                patch.object(bridge, 'stop', self.stop), \
                patch.object(bridge, 'LeaveJudge', self.Judge), \
                patch.object(bridge, 'LEAVE_SPEECH_TIMEOUT', 1.0), \
                patch.object(meeting_lifecycle, 'LEAVE_QUIET_SECONDS', 0.3):
            task = asyncio.create_task(bridge.run_voice(
                IdleSpeaker(), Mic(), None, 'test-only',
                RuntimeConfig.from_environ({'SMITLINE_MEETING_INTRO': '0'}), self.platform))
            try:
                await self.socket.events.put({'type': 'session.started'})
                await script(self)
                if close:
                    await until(lambda: self.sent('session.close'), 10)
                    await self.socket.events.put({'type': 'session.closed', 'usage': {'seconds': 9}})
                await asyncio.wait_for(task, 5)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        return self


class TeardownOrderTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_agent_leaves_the_meeting_before_gpt_live_closes(self):
        async def script(run):
            await run.socket.events.put(leave_call('completed'))
        run = await VoiceRun(self)(script)
        self.assertEqual(len(run.platform.leaves), 1)
        self.assertNotIn('session.close', run.platform.leaves[0])
        kinds = run.kinds()
        # Out of the meeting first; only then is it marked ended, and GPT-Live closed after that.
        self.assertLess(kinds.index('agent_left'), kinds.index('left'))
        self.assertLess(kinds.index('left'), kinds.index('stage:meeting_ended'))
        # input_audio, what came in from the meeting, is written whenever the audio reader stops;
        # this test's fake meeting never hands it another chunk, so it stops last of all.
        ending = [kind for kind in kinds if kind != 'input_audio']
        self.assertEqual(ending[-2:], ['session_closed', 'stage:finished'])
        self.assertEqual(run.state['leaveState'], 'left')
        self.assertEqual(run.state['endReason'], 'left_completed')
        # Nothing is left to mute once it is out (Zoom took three seconds to find nothing).
        self.assertEqual(run.platform.mutes_after_leaving, 0)

    async def test_being_stopped_leaves_the_meeting_before_gpt_live_closes(self):
        # end_call, maxMinutes, SIGTERM and /control stop all set the bridge's stop.
        async def script(run):
            await asyncio.sleep(0.1)
            run.stop.set()
        run = await VoiceRun(self)(script)
        self.assertEqual(len(run.platform.leaves), 1)
        self.assertNotIn('session.close', run.platform.leaves[0])
        self.assertNotIn('stage:meeting_ended', run.kinds())
        self.assertLess(run.kinds().index('left'), run.kinds().index('stage:finished'))

    async def test_gpt_live_ending_the_session_still_leaves_the_meeting_first(self):
        async def script(run):
            await run.socket.events.put({'type': 'session.closed', 'usage': {'seconds': 3}})
        run = await VoiceRun(self)(script, close=False)
        self.assertEqual(len(run.platform.leaves), 1)
        kinds = run.kinds()
        self.assertLess(kinds.index('left'), kinds.index('stage:finished'))

    async def test_a_platform_that_will_not_let_it_out_is_reported_and_the_meeting_still_ends(self):
        async def script(run):
            await run.socket.events.put(leave_call('asked_to_leave'))
        run = await VoiceRun(self, Platform(fail='Zoom still showed the meeting'))(script)
        self.assertIn(('leave_failed', {'reason': 'RuntimeError: Zoom still showed the meeting'}),
                      run.record.events)
        self.assertEqual(run.state['leaveState'], 'leave_failed')
        kinds = run.kinds()
        self.assertLess(kinds.index('leave_failed'), kinds.index('stage:meeting_ended'))
        self.assertTrue(run.state['finalized'])

    async def test_a_platform_that_never_answers_does_not_hold_up_the_end(self):
        async def script(run):
            await run.socket.events.put(leave_call('completed'))
        with patch.object(adapters_base, 'LEAVE_TIMEOUT', 0.3):
            run = await VoiceRun(self, Platform(hang=True))(script)
        self.assertEqual(run.state['leaveState'], 'leave_failed')
        self.assertTrue(run.state['finalized'])

    async def test_nothing_more_the_voice_says_goes_out_once_it_leaves(self):
        import base64
        import struct
        speech = base64.b64encode(struct.pack('<480h', *([900] * 480))).decode()

        async def script(run):
            await run.socket.events.put(leave_call('completed'))
            await until(lambda: run.platform.leaves, 5)
            before = run.state['generated_audio_bytes']
            await run.socket.events.put({'type': 'session.output_audio.delta', 'delta': speech})
            await asyncio.sleep(0.1)
            self.assertEqual(run.state['generated_audio_bytes'], before)
        await VoiceRun(self)(script)


class AnyLanguageLeaveTests(unittest.IsolatedAsyncioTestCase):
    """Asked to leave in so many words, its answer is its goodbye in whatever language it speaks:
    the leave check reads it, with no word lists, once it has been heard and a moment has passed."""

    ANSWERS = {
        'Spanish': 'Perfecto, ya me voy. ¡Hasta luego a todos!',
        'Hindi': 'ठीक है, मैं अब चलती हूँ। सबको नमस्ते!',
        'German': 'Alles klar, ich verabschiede mich. Bis bald!',
    }

    async def asked_then_answered(self, answer, judge):
        async def script(run):
            await run.socket.events.put({'type': 'session.input_transcript.delta',
                                         'delta': 'Great, thanks. You can hang up now.'})
            await asyncio.sleep(0.1)
            await run.socket.events.put({'type': 'session.output_transcript.delta', 'delta': answer})
            run.answered_at = asyncio.get_running_loop().time()
            if not judge[0]:
                await asyncio.sleep(1.5)
                self.assertFalse(run.stop.is_set())
                run.stop.set()
        return await VoiceRun(self, judge=judge)(script)

    async def test_an_answer_in_any_language_leaves_without_the_backend(self):
        for language, answer in self.ANSWERS.items():
            with self.subTest(language):
                loop = asyncio.get_running_loop()
                run = await self.asked_then_answered(answer, (True, 'asked_to_leave'))
                self.assertEqual(run.state['endReason'], 'left_asked_to_leave')
                kinds = run.kinds()
                self.assertIn('goodbye_leave', kinds)  # its own leave, not the backend's
                self.assertIn(('agent_left', {'reason': 'asked_to_leave'}), run.record.events)
                self.assertEqual(len(run.judged), 1)
                self.assertEqual(run.judged[0][-1], answer)
                self.assertLess(kinds.index('left'), kinds.index('stage:meeting_ended'))
                # The quiet moment and the check, then out: not the backend's 27 seconds.
                self.assertLess(loop.time() - run.answered_at, 4.0)

    async def test_the_leave_check_can_keep_it_after_such_an_answer(self):
        run = await self.asked_then_answered(self.ANSWERS['Spanish'], (False, 'other'))
        self.assertNotIn('goodbye_leave', run.kinds())
        self.assertEqual(len(run.judged), 1)  # once for this answer, not every moment

    async def test_without_an_ask_words_it_cannot_read_never_leave(self):
        async def script(run):
            await run.socket.events.put({'type': 'session.input_transcript.delta',
                                         'delta': 'What did we decide about the launch?'})
            await asyncio.sleep(0.1)
            await run.socket.events.put({'type': 'session.output_transcript.delta',
                                         'delta': self.ANSWERS['German']})
            await asyncio.sleep(1.5)
            run.stop.set()
        run = await VoiceRun(self, judge=(True, 'completed'))(script)
        self.assertNotIn('goodbye_leave', run.kinds())
        self.assertEqual(run.judged, [])


class SupervisorWaitsForTheBotTests(unittest.IsolatedAsyncioTestCase):
    """The daemon writes the handoff and stops the container once the bot is out of the meeting."""

    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        (root / 'meeting-runtime').mkdir()
        self.launcher, self.health, self.daemon = FakeLauncher(), FakeHealth(), FakeDaemon()
        self.supervisor = ProductionMeetingSupervisor(
            root, runtime_root=root / 'meeting-runtime', launcher=self.launcher,
            health=self.health, poll_interval=0.02, start_timeout=0.4, stop_timeout=0.4,
            leave_wait=5.0).bind_daemon(self.daemon)
        await self.supervisor.start(session())

    async def asyncTearDown(self):
        await self.supervisor.shutdown()
        self.temporary.cleanup()

    ENDED = {'stage': 'meeting_ended', 'endReason': 'left_asked_to_leave',
             'admissionState': 'admitted', 'leaveState': 'leaving'}

    async def test_it_waits_for_the_bot_to_leave_before_it_stops_the_container(self):
        self.health.payload = dict(self.ENDED)
        await asyncio.sleep(0.3)
        self.assertEqual(self.daemon.handoffs, [])
        self.assertEqual((self.launcher.stops, self.launcher.running), (0, True))
        self.health.payload = dict(self.ENDED, leaveState='left')
        await until(lambda: self.daemon.handoffs)
        handoff = self.daemon.handoffs[-1]
        self.assertEqual((handoff.end_reason, handoff.partial), ('left_asked_to_leave', False))
        await until(lambda: not self.launcher.running)

    async def test_a_leave_that_failed_does_not_hold_it_up(self):
        self.health.payload = dict(self.ENDED, leaveState='leave_failed')
        await until(lambda: self.daemon.handoffs, 1.0)

    async def test_a_bot_that_never_says_it_left_is_stopped_after_a_moment(self):
        self.supervisor.leave_wait = 0.3
        self.health.payload = {'stage': 'finished', 'admissionState': 'admitted'}  # an older bot
        loop = asyncio.get_running_loop()
        began = loop.time()
        await until(lambda: self.daemon.handoffs)
        self.assertGreaterEqual(loop.time() - began, 0.3)
        self.assertEqual(self.daemon.handoffs[-1].end_reason, 'finished')

    async def test_a_bot_that_exits_while_it_leaves_keeps_the_end_it_reported(self):
        self.health.payload = dict(self.ENDED)
        await asyncio.sleep(0.1)
        self.launcher.running = False
        await until(lambda: self.daemon.handoffs)
        handoff = self.daemon.handoffs[-1]
        self.assertEqual((handoff.end_reason, handoff.partial), ('left_asked_to_leave', False))

    def test_what_counts_as_out_of_the_meeting(self):
        self.assertTrue(out_of_the_meeting({'admissionState': 'admitted', 'leaveState': 'left'}))
        self.assertTrue(out_of_the_meeting({'admissionState': 'admitted', 'leaveState': 'leave_failed'}))
        self.assertTrue(out_of_the_meeting({'admissionState': 'admitted', 'archive': {'closed': True}}))
        self.assertTrue(out_of_the_meeting({'admissionState': 'waiting_for_admission'}))
        self.assertTrue(out_of_the_meeting({'stage': 'authentication_required'}))
        self.assertFalse(out_of_the_meeting({'admissionState': 'admitted'}))
        self.assertFalse(out_of_the_meeting({'admissionState': 'admitted', 'leaveState': 'leaving',
                                             'archive': {'closed': False}}))


class RemoteMachineWaitsForTheBotTests(RemoteCase):
    async def test_its_machine_is_deleted_only_once_the_bot_left(self):
        meeting = owned('mtg-remote0000001', 'acct_a')
        machine = await self.started(meeting)
        machine.state.update(stage='finished', endReason='left_completed',
                             admissionState='admitted', leaveState='leaving')
        machine.close_archive(self.disk)
        await asyncio.sleep(0.3)
        self.assertIsNone(self.handoff(meeting.id))
        self.assertEqual(machine.stops, 0)
        self.assertEqual(machine.answered('/archive'), [])
        machine.state['leaveState'] = 'left'
        await until(lambda: self.handoff(meeting.id) is not None)
        handoff = self.handoff(meeting.id)
        self.assertEqual((handoff.partial, handoff.end_reason), (False, 'left_completed'))
        await until(lambda: machine.stops >= 1)


if __name__ == '__main__':
    unittest.main()
