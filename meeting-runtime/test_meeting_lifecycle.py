import unittest
from meeting_lifecycle import should_leave_alone

class AloneDepartureTests(unittest.TestCase):
    def test_leaves_on_first_confirmed_solo_observation(self):
        self.assertTrue(should_leave_alone(1))

    def test_other_participants_or_unknown_count_do_not_trigger_departure(self):
        for count in (2, 10, None, 0):
            with self.subTest(count=count):
                self.assertFalse(should_leave_alone(count))
