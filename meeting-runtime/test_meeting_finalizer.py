import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from agent_sessions import MeetingSession
from call_record import CallRecord
from handoff_builder import build_meeting_handoff, handoff_id_for
from meeting_finalizer import MeetingFinalizer, archive_dir, load_finalization
from test_schemas import agent_session_payload, meeting_session_payload


def session(**overrides):
    return MeetingSession.from_dict(meeting_session_payload(**overrides))


class FakeRecord:
    def __init__(self, session, lease_token='lease-secret', handoff=None):
        self.session = session
        self.lease_token = lease_token
        self.handoff = handoff


class FakeMeetings:
    def __init__(self):
        self.records = {}

    def get(self, meeting_id):
        return self.records.get(meeting_id)


class FakeDaemon:
    def __init__(self):
        self.handoffs = []
        self.append_failures = []
        self.prepared = []
        self.meetings = FakeMeetings()

    def prepare_finalization(self, meeting_id):
        self.prepared.append(meeting_id)

    def public_approvals(self, meeting_id):
        return []

    def note_append_failure(self, meeting_id, handoff_id, reason):
        self.append_failures.append((meeting_id, handoff_id, reason))

    def store_handoff(self, handoff):
        self.handoffs.append(handoff)
        record = self.meetings.records.get(handoff.meeting_id)
        if record is None:
            record = FakeRecord(session(id=handoff.meeting_id), lease_token=None)
            self.meetings.records[handoff.meeting_id] = record
        record.handoff = handoff
        return handoff


class MeetingFinalizerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.runtime = Path(self.temporary.name) / 'meeting-runtime'
        self.runtime.mkdir()
        self.recordings = self.runtime / 'recordings'
        self.recordings.mkdir()
        self.daemon = FakeDaemon()
        self.appended = []

    async def asyncTearDown(self):
        self.temporary.cleanup()

    def _archive(self, meeting, *, with_work=True):
        archive = CallRecord(self.recordings, meeting_id=meeting.id)
        archive.transcript('meeting', 'What is the release risk?', False, start_ms=0, end_ms=1200)
        archive.transcript('agent', 'I will check the workspace.', False, start_ms=1300, end_ms=2400)
        if with_work:
            archive.event('delegation.started', delegationId='dlg-1')
            archive.event('delegation.completed', delegationId='dlg-1', message='Checked schemas')
            archive.event('plot', path='chart.png')
            (archive.directory / 'chart.png').write_bytes(b'\x89PNG')
        archive.close(end_reason='finished', stage='finished')
        return archive

    async def test_builder_is_deterministic_and_does_not_fabricate(self):
        meeting = session()
        archive = self._archive(meeting)
        newer = CallRecord(self.recordings)
        newer.transcript('meeting', 'newest directory must be ignored', False)
        handoff = build_meeting_handoff(archive, meeting, reason='finished', partial=False)
        self.assertEqual(handoff.handoff_id, handoff_id_for(meeting.id))
        self.assertEqual(handoff.decisions, ())
        self.assertEqual(handoff.action_items, ())
        self.assertEqual(handoff.requirements, ())
        self.assertEqual(handoff.unresolved_questions, (
            'How should MeetingState be represented?',
        ))
        self.assertIn('meeting-runtime/bridge.py', handoff.files_discussed)
        self.assertEqual(handoff.work_performed[0].task_id, 'dlg-1')
        self.assertEqual(handoff.artifacts[0].path, 'chart.png')
        self.assertEqual(handoff.archive_path, meeting.id)
        self.assertEqual(handoff.git.branch, 'agent/zoom-teams-adapters')
        self.assertNotEqual(newer.directory.name, meeting.id)
        self.assertEqual(archive_dir(self.runtime, meeting.id).name, meeting.id)

    async def test_normal_exact_append_then_store(self):
        meeting = session()
        self._archive(meeting)
        self.daemon.meetings.records[meeting.id] = FakeRecord(meeting)

        async def append(session, handoff):
            self.appended.append((session.id, handoff.handoff_id))
            return {'ok': True, 'idempotent': False}

        finalizer = MeetingFinalizer(self.runtime, daemon=self.daemon, append=append)
        first = await finalizer.complete(meeting, reason='finished', partial=False)
        second = await finalizer.complete(meeting, reason='finished', partial=False)
        self.assertEqual(first['status'], 'ready')
        self.assertEqual(second['status'], 'ready')
        self.assertTrue(second.get('idempotent'))
        self.assertEqual(self.appended, [(meeting.id, handoff_id_for(meeting.id))])
        self.assertEqual(len(self.daemon.handoffs), 1)
        self.assertEqual(load_finalization(archive_dir(self.runtime, meeting.id))['status'], 'ready')

    async def test_exact_append_failure_retry_and_context_skip(self):
        meeting = session()
        self._archive(meeting, with_work=False)
        self.daemon.meetings.records[meeting.id] = FakeRecord(meeting)
        calls = []

        async def fail(_session, _handoff):
            calls.append('fail')
            return {'error': 'codex unavailable'}

        failed = await MeetingFinalizer(
            self.runtime, daemon=self.daemon, append=fail).complete(
                meeting, reason='cancelled', partial=True)
        self.assertEqual(failed['status'], 'append_failed')
        self.assertEqual(self.daemon.handoffs, [])
        self.assertEqual(self.daemon.append_failures[-1][0], meeting.id)
        self.assertEqual(load_finalization(archive_dir(self.runtime, meeting.id))['status'],
                         'append_failed')

        async def succeed(_session, handoff):
            calls.append('ok')
            return {'ok': True, 'idempotent': True}

        ready = await MeetingFinalizer(
            self.runtime, daemon=self.daemon, append=succeed).complete(
                meeting, reason='cancelled', partial=True)
        self.assertEqual(ready['status'], 'ready')
        self.assertEqual(calls, ['fail', 'ok'])
        self.assertEqual(self.daemon.handoffs[-1].partial, True)

        context = session(
            id='mtg-context0000001',
            agentSession=agent_session_payload(
                sessionId='local-portal',
                metadata={'source': 'local-portal', 'continuity': 'context'},
            ),
        )
        self._archive(context, with_work=False)
        self.daemon.meetings.records[context.id] = FakeRecord(context)
        skipped = await MeetingFinalizer(
            self.runtime, daemon=self.daemon, append=fail).complete(
                context, reason='finished', partial=False)
        self.assertEqual(skipped['status'], 'ready')
        self.assertEqual(skipped['continuity'], 'context')
        self.assertEqual(calls, ['fail', 'ok'])

    async def test_corrupt_and_secret_handoff_are_rejected(self):
        meeting = session()
        directory = archive_dir(self.runtime, meeting.id)
        directory.mkdir(parents=True)
        (directory / 'handoff.json').write_text('{not-json', encoding='utf-8')
        with self.assertRaises(ValueError):
            MeetingFinalizer(self.runtime).persist_local(meeting, reason='ended')
        (directory / 'handoff.json').write_text(
            json.dumps({'version': 1, 'apiKey': 'sk-secret', 'meetingId': meeting.id}),
            encoding='utf-8')
        with self.assertRaises(ValueError):
            MeetingFinalizer(self.runtime).persist_local(meeting, reason='ended')


if __name__ == '__main__':
    unittest.main()
