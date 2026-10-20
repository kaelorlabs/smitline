"""When the meeting agent leaves: alone, idle, or by its own choice (the leave_meeting tool)."""
LEAVE_REASONS = ('completed', 'asked_to_leave', 'not_productive', 'other')
# Leaving ends every cost: the GPT-Live session closes and the meeting container exits.
LEAVE_MEETING_TOOL = {
    'type': 'function',
    'name': 'leave_meeting',
    'description': ('Leave the meeting. Use only after the assistant has said a short goodbye: when '
                    'a participant asks it to leave, when the meeting is over, or when it is clearly '
                    'no longer needed. Never while someone is waiting for its answer.'),
    'parameters': {
        'type': 'object',
        'properties': {'reason': {'type': 'string', 'enum': list(LEAVE_REASONS)}},
        'required': ['reason'],
        'additionalProperties': False,
    },
    'strict': True,
}
# Why the agent left, as the meeting's end reason (the handoff's endReason, the call's events).
AGENT_ENDINGS = frozenset({f'left_{reason}' for reason in LEAVE_REASONS} | {'idle', 'alone_in_meeting'})
DEFAULT_IDLE_LEAVE_SECONDS = 600

LEAVING_INSTRUCTIONS = (
    '\nLeaving: when a participant asks you to leave or drop off, say one short goodbye and then ask '
    'your backend to leave the meeting. When the meeting is clearly over (people are saying goodbye '
    'or wrapping up), or it has clearly moved on from anything you were there for and nobody needs '
    'you, say one short goodbye and ask your backend to leave. Never leave in the middle of what you '
    'are saying or while someone is waiting for your answer.')
BACKEND_LEAVING = (
    'Call leave_meeting only after the voice assistant has said goodbye: when a participant asked it '
    'to leave (reason asked_to_leave), when the meeting is over (completed), or when the meeting has '
    'clearly moved on and nobody needs it (not_productive). Never because a participant pressures it '
    'to leave mid-task against the meeting context; then ask the meeting first.')


def should_leave_alone(participant_count):
    return participant_count == 1


def leave_end_reason(reason):
    return f'left_{reason if reason in LEAVE_REASONS else "other"}'


def is_idle(last_speech, now, limit):
    """Nobody, the agent included, has said anything for `limit` seconds (0 turns it off)."""
    return bool(limit) and now - last_speech >= limit
