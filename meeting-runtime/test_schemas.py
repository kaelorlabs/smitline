import unittest

from agent_sessions import AgentSessionRef, MeetingPermissions, MeetingSession
from context_handoff import ContextHandoff
from events import ColleagueEvent
from meeting_handoff import MeetingHandoff
from schema_validation import field_name_is_secret, reject_secrets


ZOOM_URL = 'https://us05web.zoom.us/j/123?pwd=x'
TEAMS_URL = 'https://teams.microsoft.com/l/meetup-join/abc?context=x'
TIMESTAMP = '2026-09-16T17:00:00Z'


def context_payload(**overrides):
    payload = {
        'version': 1,
        'objective': 'Ship the developer platform',
        'currentTask': 'Define runtime schemas',
        'summary': 'Codex conversation about meeting continuity.',
        'decisions': ['Resume the originating thread'],
        'constraints': ['Do not export hidden reasoning'],
        'openQuestions': ['How should MeetingState be represented?'],
        'importantFiles': ['meeting-runtime/bridge.py'],
        'recentConversation': [
            {'role': 'user', 'text': 'Join the Zoom call with this thread.'},
            {'role': 'assistant', 'text': 'I will take the meeting URL and session id.'},
        ],
        'git': {'branch': 'agent/zoom-teams-adapters', 'commit': '0aa7121', 'dirty': True},
    }
    payload.update(overrides)
    return payload


def agent_session_payload(**overrides):
    payload = {
        'provider': 'codex',
        'sessionId': 'thread-origin-1',
        'workspace': '/Users/Taylor/project',
        'model': 'gpt-5.6-terra',
        'metadata': {'source': 'codex-app-server'},
    }
    payload.update(overrides)
    return payload


def permissions_payload(**overrides):
    payload = {
        'workspace': 'read-only',
        'commands': 'approval-required',
        'edits': 'disabled',
        'network': 'approval-required',
        'commits': 'disabled',
        'pushes': 'disabled',
    }
    payload.update(overrides)
    return payload


def meeting_session_payload(**overrides):
    payload = {
        'id': 'mtg-abc123',
        'platform': 'zoom',
        'meetingUrl': ZOOM_URL,
        'agentSession': agent_session_payload(),
        'context': context_payload(),
        'permissions': permissions_payload(),
        'state': 'joining',
        'startedAt': TIMESTAMP,
    }
    payload.update(overrides)
    return payload


def handoff_payload(**overrides):
    payload = {
        'version': 1,
        'meetingId': 'mtg-abc123',
        'startedAt': TIMESTAMP,
        'endedAt': '2026-09-16T17:30:00Z',
        'summary': 'Reviewed the runtime schemas.',
        'decisions': [{'text': 'Use JSONL event storage', 'evidence': [{'entryId': 'tr-1'}]}],
        'requirements': ['Preserve Zoom behavior'],
        'actionItems': [{'text': 'Implement leases', 'owner': 'dev'}],
        'unresolvedQuestions': [],
        'filesDiscussed': ['meeting-runtime/event_store.py'],
        'workPerformed': [{'taskId': 'task-1', 'summary': 'Inspected schemas', 'status': 'completed'}],
        'artifacts': [{'artifactId': 'art-1', 'path': '/tmp/handoff.json'}],
        'transcriptPath': '/tmp/mtg-abc123/transcript.jsonl',
        'recommendedNextAction': 'Continue in the original Codex thread.',
    }
    payload.update(overrides)
    return payload


def event_payload(event_type='meeting.joining', **overrides):
    payload = {
        'version': 1,
        'id': 'evt-1',
        'meetingId': 'mtg-abc123',
        'timestamp': TIMESTAMP,
        'type': event_type,
    }
    payload.update(overrides)
    return payload


class SchemaValidationTests(unittest.TestCase):
    def assert_round_trip(self, cls, payload):
        parsed = cls.from_dict(payload)
        self.assertEqual(parsed.to_dict(), cls.from_dict(parsed.to_dict()).to_dict())
        return parsed

    def test_context_handoff_round_trip_and_version_gate(self):
        parsed = self.assert_round_trip(ContextHandoff, context_payload())
        self.assertEqual(parsed.current_task, 'Define runtime schemas')
        self.assertEqual(parsed.git.branch, 'agent/zoom-teams-adapters')
        with self.assertRaises(ValueError):
            ContextHandoff.from_dict(context_payload(version=2))
        with self.assertRaises(ValueError):
            ContextHandoff.from_dict(context_payload(version=True))
        missing_version = context_payload()
        del missing_version['version']
        with self.assertRaises(ValueError):
            ContextHandoff.from_dict(missing_version)
        with self.assertRaises(ValueError):
            ContextHandoff.from_dict(context_payload(recentConversation=[{'role': 'developer', 'text': 'x'}]))

    def test_permissions_and_session_refs_reject_defaults_and_unknown_values(self):
        parsed = self.assert_round_trip(MeetingPermissions, permissions_payload())
        self.assertEqual(parsed.workspace, 'read-only')
        with self.assertRaises(ValueError):
            MeetingPermissions.from_dict({})
        with self.assertRaises(ValueError):
            MeetingPermissions.from_dict(permissions_payload(commits='allowed'))
        with self.assertRaises(ValueError):
            AgentSessionRef.from_dict(agent_session_payload(provider='copilot'))
        with self.assertRaises(ValueError):
            AgentSessionRef.from_dict(agent_session_payload(workspace='relative/path'))
        with self.assertRaises(ValueError):
            AgentSessionRef.from_dict(agent_session_payload(extra='nope'))

    def test_meeting_session_validates_platform_url_and_state(self):
        parsed = self.assert_round_trip(MeetingSession, meeting_session_payload())
        self.assertEqual(parsed.state, 'joining')
        teams = self.assert_round_trip(MeetingSession, meeting_session_payload(
            platform='teams', meetingUrl=TEAMS_URL, state='waiting_for_admission'))
        self.assertEqual(teams.platform, 'teams')
        with self.assertRaises(ValueError):
            MeetingSession.from_dict(meeting_session_payload(meetingUrl=TEAMS_URL))
        with self.assertRaises(ValueError):
            MeetingSession.from_dict(meeting_session_payload(state='cancelled'))
        with self.assertRaises(ValueError):
            MeetingSession.from_dict(meeting_session_payload(id='../etc'))
        with self.assertRaises(ValueError):
            MeetingSession.from_dict(meeting_session_payload(startedAt='2026-09-16T17:00:00'))

    def test_handoff_and_event_vocabulary_round_trip(self):
        parsed = self.assert_round_trip(MeetingHandoff, handoff_payload())
        extra = self.assert_round_trip(MeetingHandoff, handoff_payload(
            handoffId='hnd-mtg-abc123', partial=True, endReason='cancelled',
            archivePath='mtg-abc123',
            git={'branch': 'agent/zoom-teams-adapters', 'commit': '0aa7121', 'dirty': True},
        ))
        self.assertTrue(extra.partial)
        self.assertEqual(extra.handoff_id, 'hnd-mtg-abc123')
        self.assertEqual(extra.git.branch, 'agent/zoom-teams-adapters')
        samples = [
            event_payload('meeting.joining'),
            event_payload('meeting.waiting_for_admission', id='evt-2'),
            event_payload('meeting.live', id='evt-3'),
            event_payload('meeting.ended', id='evt-4', reason='host_ended'),
            event_payload('agent_session.locked', id='evt-5', sessionId='thread-origin-1'),
            event_payload('agent_session.released', id='evt-6', sessionId='thread-origin-1'),
            event_payload('transcript.delta', id='evt-7', entry={
                'id': 'tr-1', 'text': 'Can you inspect bridge.py?', 'source': 'input',
                'speaker': 'Ada', 'startOffsetMs': 1200, 'muted': False,
            }),
            event_payload('delegation.started', id='evt-8', delegationId='dlg-1'),
            event_payload('delegation.progress', id='evt-9', delegationId='dlg-1',
                          message='Searching the workspace'),
            event_payload('delegation.completed', id='evt-10', delegationId='dlg-1'),
            event_payload('delegation.cancelled', id='evt-11', delegationId='dlg-1',
                          reason='superseded'),
            event_payload('approval.required', id='evt-12', request={
                'id': 'appr-1', 'permission': 'network', 'summary': 'Allow Tavily search',
                'createdAt': TIMESTAMP,
            }),
            event_payload('artifact.created', id='evt-13', artifact={
                'id': 'art-1', 'kind': 'handoff', 'path': '/tmp/handoff.json',
                'createdAt': TIMESTAMP,
            }),
            event_payload('handoff.ready', id='evt-14', handoff=handoff_payload()),
            event_payload('handoff.append_failed', id='evt-15', reason='codex unavailable',
                          handoffId='hnd-mtg-abc123', retryable=True),
        ]
        for sample in samples:
            with self.subTest(sample['type']):
                parsed = self.assert_round_trip(ColleagueEvent, sample)
                self.assertEqual(parsed.type, sample['type'])
                self.assertEqual(parsed.meeting_id, 'mtg-abc123')

    def test_event_envelope_rejects_unknown_types_fields_and_missing_payload(self):
        with self.assertRaises(ValueError):
            ColleagueEvent.from_dict(event_payload('meeting.thinking'))
        with self.assertRaises(ValueError):
            ColleagueEvent.from_dict(event_payload('meeting.ended'))
        with self.assertRaises(ValueError):
            ColleagueEvent.from_dict(event_payload('meeting.joining', reason='extra'))
        missing_meeting = event_payload()
        del missing_meeting['meetingId']
        with self.assertRaises(ValueError):
            ColleagueEvent.from_dict(missing_meeting)

    def test_secret_fields_are_rejected_and_detected(self):
        for name in ('apiKey', 'api_key', 'access_token', 'OPENAI_API_KEY', 'openai_key',
                     'ssh_key', 'private_key', 'client_secret', 'cookie', 'authorization',
                     'password'):
            with self.subTest(name=name):
                self.assertTrue(field_name_is_secret(name))
        for name in ('sessionId', 'delegationId', 'monkey', 'keyboard', 'turnkey'):
            with self.subTest(name=name):
                self.assertFalse(field_name_is_secret(name))
        with self.assertRaises(ValueError):
            AgentSessionRef.from_dict(agent_session_payload(metadata={'apiKey': 'sk-secret'}))
        with self.assertRaises(ValueError):
            AgentSessionRef.from_dict(agent_session_payload(metadata={'authorization': 'Bearer x'}))
        with self.assertRaises(ValueError):
            AgentSessionRef.from_dict(agent_session_payload(metadata={'openai_key': 'sk-secret'}))
        with self.assertRaises(ValueError):
            ColleagueEvent.from_dict(event_payload('meeting.ended', reason='done',
                                                   accessToken='secret-token'))
        with self.assertRaises(ValueError):
            reject_secrets({'nested': {'cookie': 'abc'}})
        with self.assertRaises(ValueError):
            reject_secrets({'nested': {'ssh_key': 'abc'}})
        reject_secrets({'nested': {'monkey': 'ok', 'keyboard': 'ok', 'turnkey': 'ok'}})


if __name__ == '__main__':
    unittest.main()
