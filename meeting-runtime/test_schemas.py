import unittest

from agent_sessions import MeetingSession, upgrade_legacy_session
from context_handoff import ContextHandoff
from events import ColleagueEvent
from meeting_handoff import MeetingHandoff
from schema_validation import field_name_is_secret, reject_secrets


ZOOM_URL = 'https://us05web.zoom.us/j/123?pwd=x'
TEAMS_URL = 'https://teams.microsoft.com/l/meetup-join/abc?context=x'
MEET_URL = 'https://meet.google.com/aaa-bbbb-ccc'
TIMESTAMP = '2026-09-16T17:00:00Z'


def context_payload(**overrides):
    payload = {
        'version': 1,
        'objective': 'Ship the developer platform',
        'currentTask': 'Define runtime schemas',
        'summary': 'Agent conversation before the meeting.',
        'decisions': ['Report back after the meeting'],
        'constraints': ['Do not export hidden reasoning'],
        'openQuestions': ['How should MeetingState be represented?'],
        'importantFiles': ['meeting-runtime/bridge.py'],
        'recentConversation': [
            {'role': 'user', 'text': 'Join the Zoom call for me.'},
            {'role': 'assistant', 'text': 'I will take the meeting URL.'},
        ],
    }
    payload.update(overrides)
    return payload


def meeting_session_payload(**overrides):
    payload = {
        'id': 'mtg-abc123',
        'platform': 'zoom',
        'meetingUrl': ZOOM_URL,
        'context': context_payload(),
        'onBehalfOf': 'Ada Lovelace',
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
        'actionItems': [{'text': 'Send the meeting notes', 'owner': 'dev'}],
        'unresolvedQuestions': [],
        'filesDiscussed': ['meeting-runtime/event_store.py'],
        'workPerformed': [{'taskId': 'task-1', 'summary': 'Inspected schemas', 'status': 'completed'}],
        'artifacts': [{'artifactId': 'art-1', 'path': '/tmp/handoff.json'}],
        'transcriptPath': '/tmp/mtg-abc123/transcript.jsonl',
        'recommendedNextAction': 'Review the meeting transcript and follow up on open questions.',
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
        with self.assertRaises(ValueError):
            ContextHandoff.from_dict(context_payload(git={'branch': 'main'}))
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

    def test_meeting_session_validates_platform_url_and_state(self):
        parsed = self.assert_round_trip(MeetingSession, meeting_session_payload())
        self.assertEqual(parsed.state, 'joining')
        self.assertEqual(parsed.on_behalf_of, 'Ada Lovelace')
        voiced = self.assert_round_trip(MeetingSession, meeting_session_payload(voice='cinder'))
        self.assertEqual(voiced.voice, 'cinder')
        for legacy in ('agentSession', 'permissions'):
            with self.subTest(legacy=legacy), self.assertRaises(ValueError):
                MeetingSession.from_dict(meeting_session_payload(**{legacy: {}}))
        teams = self.assert_round_trip(MeetingSession, meeting_session_payload(
            platform='teams', meetingUrl=TEAMS_URL, state='waiting_for_admission'))
        self.assertEqual(teams.platform, 'teams')
        meet = self.assert_round_trip(MeetingSession, meeting_session_payload(
            platform='meet', meetingUrl=MEET_URL, state='waiting_for_admission'))
        self.assertEqual(meet.platform, 'meet')
        with self.assertRaises(ValueError):
            MeetingSession.from_dict(meeting_session_payload(meetingUrl=TEAMS_URL))
        with self.assertRaises(ValueError):
            MeetingSession.from_dict(meeting_session_payload(state='cancelled'))
        with self.assertRaises(ValueError):
            MeetingSession.from_dict(meeting_session_payload(id='../etc'))
        with self.assertRaises(ValueError):
            MeetingSession.from_dict(meeting_session_payload(startedAt='2026-09-16T17:00:00'))
        camera = self.assert_round_trip(MeetingSession, meeting_session_payload(
            cameraEnabled=False, cameraState='off', visualState='ended'))
        self.assertFalse(camera.camera_enabled)
        self.assertEqual(camera.camera_state, 'off')
        with self.assertRaises(ValueError):
            MeetingSession.from_dict(meeting_session_payload(cameraState='streaming'))
        with self.assertRaises(ValueError):
            MeetingSession.from_dict(meeting_session_payload(degradedReason='customer transcript'))
        degraded = self.assert_round_trip(MeetingSession, meeting_session_payload(
            cameraEnabled=True, cameraState='degraded', visualState='listening',
            degradedReason='platform_blocked'))
        self.assertEqual(degraded.degraded_reason, 'platform_blocked')

    def test_snapshots_from_coding_agent_sessions_still_load(self):
        legacy = meeting_session_payload(
            agentSession={'provider': 'generic', 'sessionId': 'call-1', 'workspace': '/tmp/w',
                          'metadata': {'onBehalfOf': 'Grace Hopper', 'voice': 'marin'}},
            permissions={'workspace': 'none'},
            context=dict(context_payload(), git={'branch': 'main'}))
        del legacy['onBehalfOf']
        session = MeetingSession.from_dict(upgrade_legacy_session(legacy))
        self.assertEqual(session.on_behalf_of, 'Grace Hopper')
        self.assertEqual(session.voice, 'marin')
        self.assertNotIn('git', session.context.to_dict())

    def test_handoff_and_event_vocabulary_round_trip(self):
        parsed = self.assert_round_trip(MeetingHandoff, handoff_payload())
        extra = self.assert_round_trip(MeetingHandoff, handoff_payload(
            handoffId='hnd-mtg-abc123', partial=True, endReason='cancelled',
            archivePath='mtg-abc123',
        ))
        self.assertTrue(extra.partial)
        self.assertEqual(extra.handoff_id, 'hnd-mtg-abc123')
        with self.assertRaises(ValueError):
            MeetingHandoff.from_dict(handoff_payload(git={'branch': 'main'}))
        samples = [
            event_payload('meeting.joining'),
            event_payload('meeting.waiting_for_admission', id='evt-2'),
            event_payload('meeting.live', id='evt-3'),
            event_payload('meeting.ended', id='evt-4', reason='host_ended'),
            event_payload('transcript.delta', id='evt-7', entry={
                'id': 'tr-1', 'text': 'What did we decide last week?', 'source': 'input',
                'speaker': 'Ada', 'startOffsetMs': 1200, 'muted': False,
            }),
            event_payload('handoff.ready', id='evt-14', handoff=handoff_payload()),
            event_payload('presence.updated', id='evt-16', cameraEnabled=True,
                          cameraState='on', visualState='listening'),
        ]
        for sample in samples:
            with self.subTest(sample['type']):
                parsed = self.assert_round_trip(ColleagueEvent, sample)
                self.assertEqual(parsed.type, sample['type'])
                self.assertEqual(parsed.meeting_id, 'mtg-abc123')

    def test_event_envelope_rejects_unknown_types_fields_and_missing_payload(self):
        with self.assertRaises(ValueError):
            ColleagueEvent.from_dict(event_payload('meeting.thinking'))
        for removed in ('agent_session.locked', 'approval.required', 'git.action.requested',
                        'screen_share.started', 'delegation.started'):
            with self.subTest(removed=removed), self.assertRaises(ValueError):
                ColleagueEvent.from_dict(event_payload(removed))
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
            ColleagueEvent.from_dict(event_payload('meeting.ended', reason='done',
                                                   accessToken='secret-token'))
        with self.assertRaises(ValueError):
            reject_secrets({'nested': {'cookie': 'abc'}})
        with self.assertRaises(ValueError):
            reject_secrets({'nested': {'ssh_key': 'abc'}})
        reject_secrets({'nested': {'monkey': 'ok', 'keyboard': 'ok', 'turnkey': 'ok'}})


if __name__ == '__main__':
    unittest.main()
