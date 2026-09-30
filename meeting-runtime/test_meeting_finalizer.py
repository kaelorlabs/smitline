import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from agent_sessions import MeetingSession
from call_record import CallRecord
from handoff_builder import build_meeting_handoff, handoff_id_for
from meeting_finalizer import MeetingFinalizer, archive_dir, load_finalization
from test_schemas import meeting_session_payload


def session(**overrides):
    return MeetingSession.from_dict(meeting_session_payload(**overrides))


class FakeRecord:
    def __init__(self, session, handoff=None):
        self.session = session
        self.handoff = handoff


class FakeMeetings:
    def __init__(self):
        self.records = {}

    def get(self, meeting_id):
        return self.records.get(meeting_id)


class FakeDaemon:
    def __init__(self):
        self.handoffs = []
        self.meetings = FakeMeetings()

    def store_handoff(self, handoff):
        self.handoffs.append(handoff)
        record = self.meetings.records.get(handoff.meeting_id)
        if record is None:
            record = FakeRecord(session(id=handoff.meeting_id))
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

    async def asyncTearDown(self):
        self.temporary.cleanup()

    def _archive(self, meeting):
        archive = CallRecord(self.recordings, meeting_id=meeting.id)
        archive.transcript('meeting', 'What is the release risk?', False, start_ms=0, end_ms=1200)
        archive.transcript('agent', 'The main risk is the late API change.', False,
                           start_ms=1300, end_ms=2400)
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
        self.assertEqual(handoff.work_performed, ())
        self.assertEqual(handoff.artifacts, ())
        self.assertIn('2 transcript entries', handoff.summary)
        self.assertEqual(handoff.archive_path, meeting.id)
        payload = handoff.to_dict()
        for removed in ('git', 'permissions', 'approvals'):
            self.assertNotIn(removed, payload)
        self.assertNotEqual(newer.directory.name, meeting.id)
        self.assertEqual(archive_dir(self.runtime, meeting.id).name, meeting.id)

    async def test_handoff_is_stored_once(self):
        meeting = session()
        self._archive(meeting)
        self.daemon.meetings.records[meeting.id] = FakeRecord(meeting)
        finalizer = MeetingFinalizer(self.runtime, daemon=self.daemon)
        first = await finalizer.complete(meeting, reason='finished', partial=False)
        second = await finalizer.complete(meeting, reason='finished', partial=False)
        self.assertEqual(first['status'], 'ready')
        self.assertEqual(second['status'], 'ready')
        self.assertTrue(second.get('idempotent'))
        self.assertEqual(len(self.daemon.handoffs), 1)
        self.assertEqual(load_finalization(archive_dir(self.runtime, meeting.id))['status'], 'ready')

    async def test_partial_handoff_without_a_daemon_stays_local(self):
        meeting = session()
        self._archive(meeting)
        result = await MeetingFinalizer(self.runtime).complete(
            meeting, reason='cancelled', partial=True)
        self.assertEqual(result['status'], 'ready')
        self.assertTrue(result['handoff'].partial)
        stored = json.loads((archive_dir(self.runtime, meeting.id) / 'handoff.json').read_text())
        self.assertEqual(stored['endReason'], 'cancelled')

    async def test_a_handoff_written_before_the_upgrade_still_loads(self):
        meeting = session()
        directory = archive_dir(self.runtime, meeting.id)
        directory.mkdir(parents=True)
        legacy = build_meeting_handoff(
            self._archive(meeting), meeting, reason='finished').to_dict()
        legacy.update(permissions={'workspace': 'none'}, approvals=[], git={'branch': 'main'})
        (directory / 'handoff.json').write_text(json.dumps(legacy), encoding='utf-8')
        handoff = MeetingFinalizer(self.runtime).persist_local(meeting, reason='finished')
        self.assertEqual(handoff.handoff_id, handoff_id_for(meeting.id))

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
