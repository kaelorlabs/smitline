import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from approvals import (
    ApprovalRecord, build_approval, event_request_payload, expire_if_needed, public_approval,
    sanitize_scope, sanitize_summary,
)
from events import ColleagueEvent
from test_schemas import TIMESTAMP, event_payload


class ApprovalSchemaTests(unittest.TestCase):
    def test_build_and_public_record_omit_consumed_and_secrets(self):
        record = build_approval(
            approval_id='appr-abc123',
            meeting_id='mtg-abc123',
            category='commands',
            summary='Run a workspace lookup',
            created_at=TIMESTAMP,
            scope={'host': 'workspace'},
            workspace='/Users/Taylor/project',
        )
        public = record.public_dict()
        self.assertEqual(public['status'], 'pending')
        self.assertNotIn('consumed', public)
        dumped = str(public)
        self.assertNotIn('password', dumped)
        self.assertNotIn('transcript', dumped)
        request = event_request_payload(record)
        parsed = ColleagueEvent.from_dict(event_payload(
            'approval.required', id='evt-appr', request=request))
        self.assertEqual(parsed.payload['request'].id, 'appr-abc123')

    def test_summary_and_scope_reject_private_and_raw_actions(self):
        with self.assertRaises(ValueError):
            sanitize_summary('Include the transcript text')
        with self.assertRaises(ValueError):
            sanitize_summary('git commit -am x')
        with self.assertRaises(ValueError):
            sanitize_scope({'token': 'secret'})
        with self.assertRaises(ValueError):
            sanitize_scope({'path': '/etc/passwd'}, workspace='/Users/Taylor/project')
        with self.assertRaises(ValueError):
            sanitize_scope({'url': 'https://user:pass@example.com'})

    def test_expire_pending_after_ttl(self):
        record = build_approval(
            approval_id='appr-ttl1',
            meeting_id='mtg-abc123',
            category='network',
            summary='Allow a web search',
            created_at=TIMESTAMP,
            ttl_seconds=1,
        )
        now = datetime(2026, 9, 16, 17, 0, 2, tzinfo=timezone.utc)
        expired = expire_if_needed(record, now=now)
        self.assertEqual(expired['status'], 'expired')
        still = expire_if_needed(expired, now=now + timedelta(seconds=10))
        self.assertEqual(still['status'], 'expired')


class ApprovalStoreTests(unittest.TestCase):
    def test_repository_persists_approvals_without_secrets(self):
        from agent_sessions import MeetingSession
        from meeting_repository import MeetingRepository
        from test_schemas import context_payload, meeting_session_payload

        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        store = MeetingRepository(temporary.name)
        self.addCleanup(store.close)
        session = MeetingSession.from_dict(meeting_session_payload(id='mtg-apprstore'))
        store.put(session, 'lease-secret-value')
        built = build_approval(
            approval_id='appr-store1',
            meeting_id='mtg-apprstore',
            category='edits',
            summary='Edit a workspace file',
            created_at=TIMESTAMP,
            workspace=session.agent_session.workspace,
        )

        def mutate(records):
            records.append(built.to_dict())
            return records

        stored = store.update_approvals(
            'mtg-apprstore', mutate, workspace=session.agent_session.workspace)
        path = Path(store.root) / 'mtg-apprstore' / 'approvals.json'
        self.assertTrue(path.is_file())
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        text = path.read_text(encoding='utf-8')
        self.assertNotIn('lease-secret-value', text)
        self.assertEqual(stored[0]['id'], 'appr-store1')
        listed = store.list_approvals(
            'mtg-apprstore', workspace=session.agent_session.workspace)
        self.assertEqual(listed[0]['summary'], 'Edit a workspace file')
        self.assertIn('consumed', listed[0])
        self.assertEqual(public_approval(listed[0]).get('consumed'), None)


if __name__ == '__main__':
    unittest.main()
