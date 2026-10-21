import unittest
from meeting_lifecycle import ALONE_LEAVE_SECONDS, AloneWatch, should_leave_alone

class AloneDepartureTests(unittest.TestCase):
    def test_leaves_on_first_confirmed_solo_observation(self):
        self.assertTrue(should_leave_alone(1))

    def test_other_participants_or_unknown_count_do_not_trigger_departure(self):
        for count in (2, 10, None, 0):
            with self.subTest(count=count):
                self.assertFalse(should_leave_alone(count))


class AloneWatchTests(unittest.TestCase):
    def test_leaves_after_being_alone_for_a_while(self):
        now = [100.0]
        watch = AloneWatch(clock=lambda: now[0])
        self.assertFalse(watch.update(1))
        now[0] += ALONE_LEAVE_SECONDS - 1
        self.assertFalse(watch.update(1))
        now[0] += 1
        self.assertTrue(watch.update(1))

    def test_someone_joining_resets_it_and_an_unknown_count_does_not(self):
        now = [0.0]
        watch = AloneWatch(clock=lambda: now[0])
        watch.update(1)
        now[0] += 10
        self.assertFalse(watch.update(None))  # unknown: no reset
        now[0] += 5
        self.assertTrue(watch.update(1))
        watch.update(3)  # someone is back
        now[0] += 20
        self.assertFalse(watch.update(1))
