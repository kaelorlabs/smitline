"""Leave when the platform confirms the agent has been alone for a moment."""
import time

# Seconds every reading must say the agent is the only one in the meeting before it leaves:
# a participants list that is re-rendering can be wrong once.
ALONE_LEAVE_SECONDS = 15.0


def should_leave_alone(participant_count):
    return participant_count == 1


class AloneWatch:
    """Says when to leave: the agent has been alone for ALONE_LEAVE_SECONDS.

    A count of others resets it; an unknown count (None) neither resets nor advances it.
    """

    def __init__(self, seconds=ALONE_LEAVE_SECONDS, clock=time.monotonic):
        self.seconds = seconds
        self.clock = clock
        self.since = None

    def update(self, participant_count):
        if participant_count is None:
            return False
        if not should_leave_alone(participant_count):
            self.since = None
            return False
        now = self.clock()
        if self.since is None:
            self.since = now
        return now - self.since >= self.seconds
