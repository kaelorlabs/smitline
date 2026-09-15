"""Leave immediately when the platform confirms the agent is alone."""
def should_leave_alone(participant_count):
    return participant_count == 1
