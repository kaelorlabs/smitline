"""Following a call live: wait_for_call with since, and a meeting's live captions."""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import call_service
from aiohttp.test_utils import TestClient, TestServer
from api_tokens import ApiTokenStore

from meeting_finalizer import archive_dir, meeting_transcript, meeting_transcript_pieces
from meeting_line import LiveTranscript, MeetingLine
from runtime_daemon import RuntimeDaemon, create_app
from test_call_service import ZOOM, FakeDaemon, FakeSession, ServiceHarness, brief


class Context:
    def __init__(self):
        self.lines = []

    def add_transcript(self, speaker, text):
        self.lines.append((speaker, text))


class LiveWaitTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.h = ServiceHarness(self.temp.name)
        self.h.line.transcript = []
        self.settle = patch.object(call_service, 'LIVE_SETTLE_SECONDS', 0.15)
        self.settle.start()

    async def asyncTearDown(self):
        self.settle.stop()
        await self.h.service.shutdown()
        self.temp.cleanup()

    async def started(self):
        record = await self.h.service.create(brief())
        for _ in range(100):
            if record['id'] in self.h.line.contexts and self.h.service.get(
                    record['id'], owner='local')['status'] == 'in_progress':
                return record['id'], self.h.line.contexts[record['id']]
            await asyncio.sleep(0.01)
        self.fail('the call did not start')

    async def test_follows_what_is_said_and_returns_early(self):
        call_id, ctx = await self.started()
        wait = self.h.service.wait
        first = await wait(call_id, owner='local', timeout=5, since='')
        self.assertEqual(first['status'], 'in_progress')
        self.assertEqual(first['live']['transcript'], [])
        cursor = first['live']['cursor']

        # Nothing new: it waits out the timeout and keeps the cursor.
        quiet = await wait(call_id, owner='local', timeout=0.3, since=cursor)
        self.assertEqual((quiet['live']['cursor'], quiet['live']['transcript']), (cursor, []))

        async def talk():
            await asyncio.sleep(0.05)
            ctx.add_transcript('other', 'Do you have Friday?')
            await asyncio.sleep(0.05)
            ctx.add_transcript('agent', 'Let me check.')
        talking = asyncio.create_task(talk())
        loop = asyncio.get_running_loop()
        began = loop.time()
        said = await wait(call_id, owner='local', timeout=30, since=cursor)
        await talking
        # Back within the settle time of the first line, not the 30 s timeout, with both lines.
        self.assertLess(loop.time() - began, 2)
        self.assertEqual([(line['speaker'], line['text']) for line in said['live']['transcript']],
                         [('other', 'Do you have Friday?'), ('agent', 'Let me check.')])
        self.assertTrue(all(line['at'] for line in said['live']['transcript']))
        self.assertGreater(int(said['live']['cursor']), int(cursor))

        # Lines are never given twice; the end comes with what was left.
        ctx.add_transcript('other', 'Great, bye.')
        self.h.line.proceed.set()
        done = await wait(call_id, owner='local', timeout=30, since=said['live']['cursor'])
        self.assertEqual(done['status'], 'completed')
        self.assertEqual([line['text'] for line in done['live']['transcript']], ['Great, bye.'])

    async def test_without_since_nothing_changes(self):
        call_id, ctx = await self.started()
        ctx.add_transcript('other', 'Hello?')
        record = await self.h.service.wait(call_id, owner='local', timeout=0.2)
        self.assertNotIn('live', record)
        self.assertEqual(record['status'], 'in_progress')

    async def test_a_finished_call_returns_at_once_with_its_lines(self):
        call_id, ctx = await self.started()
        ctx.add_transcript('other', 'Bye.')
        self.h.line.proceed.set()
        await self.h.service.wait(call_id, owner='local', timeout=5)
        done = await self.h.service.wait(call_id, owner='local', timeout=30, since='')
        self.assertEqual(done['status'], 'completed')
        self.assertIn('Bye.', [line['text'] for line in done['live']['transcript']])


class LiveWaitApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.h = ServiceHarness(self.temp.name)
        tokens = ApiTokenStore(Path(self.temp.name) / 'api-tokens.json')
        app = create_app(root=Path(self.temp.name) / 'daemon', auth_token='launch-token',
                         call_service=self.h.service, api_tokens=tokens)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()
        self.auth = {'Authorization': 'Bearer launch-token'}

    async def asyncTearDown(self):
        await self.client.close()
        await self.h.service.shutdown()
        self.temp.cleanup()

    async def test_since_on_the_wait_endpoint(self):
        call = await (await self.client.post('/v1/calls', json=brief(), headers=self.auth)).json()
        self.h.line.proceed.set()
        response = await self.client.get(f'/v1/calls/{call["id"]}/wait?timeout=5&since=', headers=self.auth)
        body = await response.json()
        self.assertEqual(response.status, 200)
        self.assertEqual(body['status'], 'completed')
        self.assertEqual([line['speaker'] for line in body['live']['transcript']], ['agent', 'other'])
        again = await (await self.client.get(
            f'/v1/calls/{call["id"]}/wait?timeout=1&since={body["live"]["cursor"]}',
            headers=self.auth)).json()
        self.assertEqual(again['live']['transcript'], [])
        plain = await (await self.client.get(f'/v1/calls/{call["id"]}/wait?timeout=1',
                                             headers=self.auth)).json()
        self.assertNotIn('live', plain)
        for bad in ('abc', '-1', '1.5', '9' * 13):
            with self.subTest(since=bad):
                response = await self.client.get(f'/v1/calls/{call["id"]}/wait?timeout=1&since={bad}',
                                                 headers=self.auth)
                self.assertEqual(response.status, 422)


class LiveTranscriptTests(unittest.TestCase):
    def test_pieces_join_into_lines(self):
        ctx = Context()
        live = LiveTranscript(ctx, pause=2.5)
        live.add(0, 'meeting', ' Can you', 0)
        live.add(1, 'meeting', ' hear  me?', 0.5)
        live.add(2, 'agent', ' Yes,', 1)
        self.assertEqual(ctx.lines, [('meeting', 'Can you hear me?')])
        live.add(3, 'agent', ' loud and clear.', 1.2)
        live.tick(2)
        self.assertEqual(len(ctx.lines), 1)
        live.tick(3.8)  # a pause ends the line
        self.assertEqual(ctx.lines[-1], ('agent', 'Yes, loud and clear.'))
        live.add(3, 'agent', ' repeated', 4)  # an old piece again
        live.add(4, 'nobody', '  ', 4)
        live.flush()
        self.assertEqual(len(ctx.lines), 2)
        self.assertEqual(live.seq, 4)


class DaemonCaptionTests(unittest.TestCase):
    def test_keeps_new_valid_captions_in_order(self):
        with tempfile.TemporaryDirectory() as temp:
            daemon = RuntimeDaemon(Path(temp) / 'daemon')
            daemon.note_captions('mtg-a', [
                {'seq': 0, 'speaker': 'meeting', 'text': 'Hi'},
                {'seq': 1, 'speaker': 'agent', 'text': ' Hello'},
                {'seq': 1, 'speaker': 'agent', 'text': 'dup'},
                {'seq': True, 'speaker': 'agent', 'text': 'bool'},
                {'seq': 2, 'speaker': 'host', 'text': 'bad speaker'},
                {'seq': 3, 'speaker': 'meeting', 'text': 5},
                'junk',
            ])
            daemon.note_captions('mtg-a', 'not a list')
            # /health keeps the last 200, so the same captions come again: only new ones count.
            daemon.note_captions('mtg-a', [{'seq': 1, 'speaker': 'agent', 'text': ' Hello'},
                                           {'seq': 4, 'speaker': 'meeting', 'text': 'Bye'}])
            self.assertEqual([c['seq'] for c in daemon.captions_after('mtg-a', -1)], [0, 1, 4])
            self.assertEqual(daemon.captions_after('mtg-a', 1), [{'seq': 4, 'speaker': 'meeting', 'text': 'Bye'}])
            self.assertEqual(daemon.captions_after('mtg-other', -1), [])
            daemon.forget_captions('mtg-a')
            self.assertEqual(daemon.captions_after('mtg-a', -1), [])

    def test_memory_is_bounded(self):
        import runtime_daemon
        with tempfile.TemporaryDirectory() as temp, \
                patch.object(runtime_daemon, 'MAX_LIVE_CAPTIONS', 3), \
                patch.object(runtime_daemon, 'MAX_LIVE_MEETINGS', 2):
            daemon = RuntimeDaemon(Path(temp) / 'daemon')
            daemon.note_captions('mtg-a', [{'seq': n, 'speaker': 'meeting', 'text': str(n)} for n in range(10)])
            self.assertEqual([c['seq'] for c in daemon.captions_after('mtg-a', -1)], [7, 8, 9])
            daemon.note_captions('mtg-b', [{'seq': 0, 'speaker': 'meeting', 'text': 'b'}])
            daemon.note_captions('mtg-c', [{'seq': 0, 'speaker': 'meeting', 'text': 'c'}])
            self.assertEqual(daemon.captions_after('mtg-a', -1), [])
            self.assertEqual(len(daemon.captions_after('mtg-c', -1)), 1)


class ArchivePieceTests(unittest.TestCase):
    def test_piece_i_is_caption_i_and_the_transcript_is_unchanged(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = archive_dir(Path(temp), 'mtg-pieces00001')
            directory.mkdir(parents=True)
            rows = [{'type': 'started'},
                    {'type': 'transcript', 'speaker': 'meeting', 'text': ' Hi'},
                    {'type': 'transcript', 'speaker': 'meeting', 'text': ' '},
                    {'type': 'transcript', 'speaker': 'meeting', 'text': ' there'},
                    {'type': 'transcript', 'speaker': 'agent', 'text': ' Hello'}]
            (directory / 'events.jsonl').write_text(
                '\n'.join(json.dumps(row) for row in rows) + '\nnot json\n', encoding='utf-8')
            pieces = meeting_transcript_pieces(Path(temp), 'mtg-pieces00001')
            self.assertEqual([p['text'] for p in pieces], [' Hi', ' ', ' there', ' Hello'])
            self.assertEqual(meeting_transcript(Path(temp), 'mtg-pieces00001'),
                             [{'speaker': 'meeting', 'text': 'Hi there'},
                              {'speaker': 'agent', 'text': 'Hello'}])
            self.assertEqual(meeting_transcript_pieces(Path(temp), 'mtg-missing00001'), [])


class BridgeCaptionTests(unittest.TestCase):
    def test_every_caption_is_numbered_like_the_archive(self):
        import bridge

        class Archive:
            def __init__(self):
                self.count = 0

            def transcript(self, *args, **kwargs):
                self.count += 1
        archive, state = Archive(), {}
        transcript = bridge.MeetingTranscript(archive, state)
        for index, (kind, delta) in enumerate([('session.input_transcript.delta', 'Hi'),
                                               ('session.output_transcript.delta', 'Hello'),
                                               ('session.input_transcript.delta', 'Bye')]):
            transcript.note({'type': kind, 'delta': delta, 'start_ms': index * 100,
                             'end_ms': index * 100 + 50, 'event_id': f'e{index}'})
        self.assertEqual([c['seq'] for c in state['captions']], list(range(archive.count)))
        self.assertEqual([c['speaker'] for c in state['captions']], ['meeting', 'agent', 'meeting'])


class MeetingFollowedLiveTests(unittest.IsolatedAsyncioTestCase):
    async def test_lines_arrive_during_the_meeting_and_never_twice(self):
        pieces = [{'speaker': 'meeting', 'text': ' What is'}, {'speaker': 'meeting', 'text': ' the Q3 number?'},
                  {'speaker': 'agent', 'text': ' It was'}, {'speaker': 'agent', 'text': ' 800,000.'},
                  {'speaker': 'meeting', 'text': ' Thanks,'}, {'speaker': 'meeting', 'text': ' bye.'}]
        heard = []
        seen_live = []

        class Live(FakeDaemon):
            def __init__(self):
                super().__init__()
                self.states = ['joining', 'live', 'live', 'live', 'live', 'ended']
                self.forgotten = []

            async def get_meeting(self, meeting_id, owner=None):
                # Each look at the meeting finds two more caption pieces; the last two only in
                # the archive (the meeting ended before /health was read again).
                if len(heard) < 4:
                    heard.extend(range(len(heard), len(heard) + 2))
                return await super().get_meeting(meeting_id, owner)

            def captions_after(self, meeting_id, seq):
                return [dict(pieces[n], seq=n) for n in heard if n > seq]

            def forget_captions(self, meeting_id):
                self.forgotten.append(meeting_id)

        with tempfile.TemporaryDirectory() as temp:
            daemon = Live()
            clock = [0.0]

            async def sleepy(_seconds):
                clock[0] += 3  # longer than a pause, so each line is out by the next look
                await asyncio.sleep(0)
            line = MeetingLine(daemon, sleep=sleepy, monotonic=lambda: clock[0],
                               transcript=lambda _id: self.fail('not used when followed live'),
                               pieces=lambda _id: pieces)
            h = ServiceHarness(temp, lines={'meeting': line})
            record = await h.service.create(brief(channel='meeting', to=ZOOM))
            original = h.service._finalize

            async def finalize(context, *args, **kwargs):
                seen_live.extend(e['data']['text'] for e in h.service.events(record['id'])
                                 if e['type'] == 'call.transcript')
                return await original(context, *args, **kwargs)
            h.service._finalize = finalize
            done = await h.service.wait(record['id'], timeout=5, owner='local')
            texts = [e['data']['text'] for e in h.service.events(record['id'])
                     if e['type'] == 'call.transcript']
            self.assertEqual(texts, ['What is the Q3 number?', 'It was 800,000.', 'Thanks, bye.'])
            self.assertEqual(seen_live, texts)
            self.assertEqual(done['status'], 'completed')
            self.assertEqual(daemon.forgotten, ['mtg-1'])
            await h.service.shutdown()


class SupervisorCaptionTests(unittest.IsolatedAsyncioTestCase):
    async def test_health_captions_reach_the_daemon(self):
        from test_meeting_supervisor import SupervisorTests
        case = SupervisorTests('test_finished_without_handoff_still_builds_handoff')
        await case.asyncSetUp()
        try:
            noted = []
            case.daemon.note_captions = lambda meeting_id, captions: noted.append((meeting_id, captions))
            from test_meeting_supervisor import session
            meeting = session()
            await case.supervisor.start(meeting)
            captions = [{'seq': 0, 'speaker': 'meeting', 'text': 'Hi'}]
            case.health.payload = {'stage': 'live', 'captions': captions}
            await asyncio.sleep(0.08)
            self.assertIn((meeting.id, captions), noted)
        finally:
            await case.asyncTearDown()
