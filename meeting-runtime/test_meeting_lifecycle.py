import unittest
from meeting_lifecycle import should_leave_alone

class AloneDepartureTests(unittest.TestCase):
    def test_leaves_on_first_confirmed_solo_observation(self):
        self.assertTrue(should_leave_alone(1))

    def test_other_participants_or_unknown_count_do_not_trigger_departure(self):
        for count in (2, 10, None, 0):
            with self.subTest(count=count):
                self.assertFalse(should_leave_alone(count))


class LeavingTests(unittest.TestCase):
    def test_end_reasons_name_why_the_agent_left(self):
        from meeting_lifecycle import AGENT_ENDINGS, leave_end_reason
        self.assertEqual(leave_end_reason('asked_to_leave'), 'left_asked_to_leave')
        self.assertEqual(leave_end_reason('completed'), 'left_completed')
        # A reason the model made up is filed as other, never passed on as is.
        self.assertEqual(leave_end_reason('bored'), 'left_other')
        self.assertEqual(leave_end_reason(None), 'left_other')
        self.assertEqual(AGENT_ENDINGS, {'left_completed', 'left_asked_to_leave',
                                         'left_not_productive', 'left_other', 'idle',
                                         'alone_in_meeting'})

    def test_idle_after_the_limit_and_never_when_off(self):
        from meeting_lifecycle import is_idle
        self.assertFalse(is_idle(100, 699, 600))
        self.assertTrue(is_idle(100, 700, 600))
        self.assertFalse(is_idle(100, 10 ** 6, 0))

    def test_idle_setting(self):
        from runtime_config import RuntimeConfig
        self.assertEqual(RuntimeConfig.from_environ({}).idle_leave_seconds, 600)
        self.assertEqual(RuntimeConfig.from_environ(
            {'SMITLINE_MEETING_IDLE_LEAVE_SECONDS': ' 90 '}).idle_leave_seconds, 90)
        self.assertEqual(RuntimeConfig.from_environ(
            {'SMITLINE_MEETING_IDLE_LEAVE_SECONDS': '0'}).idle_leave_seconds, 0)
        self.assertEqual(RuntimeConfig.from_environ(
            {'SMITLINE_MEETING_IDLE_LEAVE_SECONDS': ''}).idle_leave_seconds, 600)
        for bad in ('-5', '1.5', 'ten', '86401'):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                RuntimeConfig.from_environ({'SMITLINE_MEETING_IDLE_LEAVE_SECONDS': bad})

    def test_pool_meetings_receive_the_idle_setting(self):
        from meeting_line import POOL_MEETING_SETTINGS, POOL_OWNER_SETTINGS
        self.assertIn('SMITLINE_MEETING_IDLE_LEAVE_SECONDS', POOL_MEETING_SETTINGS)
        # Installation-wide: .env.meeting sets it for every owner's meetings.
        self.assertNotIn('SMITLINE_MEETING_IDLE_LEAVE_SECONDS', POOL_OWNER_SETTINGS)
