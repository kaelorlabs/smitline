"""When the meeting agent leaves: alone, idle, or by its own choice (the leave_meeting tool),
and when a goodbye ends a call or meeting by itself."""
import re

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


# A goodbye ends a phone call, and a meeting once someone asked the agent to leave, when it has
# been heard and nobody says anything else for a moment. The backend's end_call or
# leave_meeting also ends it, but the voice does not always ask its backend, and waiting for it
# left people holding a silent line.
GOODBYE = re.compile(
    r"\b(?:bye|goodbye|good-bye|bye-bye|hanging up|talk (?:to you )?(?:soon|later)|take care|"
    r"have a (?:good|great|nice|lovely) (?:one|day|night|evening|weekend|afternoon))\b", re.I)
# What people say back to a goodbye: thanks, okay, bye. Anything else carries the call on.
CLOSING_WORDS = (
    r"(?:ok(?:ay)?|yeah|yes|yep|sure|alright|all right|thanks|thank you|cheers|great|cool|"
    r"perfect|bye|bye-bye|goodbye|good-bye|see you|see ya|talk (?:to you )?(?:soon|later)|"
    r"take care|you too|same to you|hang up|you can hang up)")
CLOSING_ONLY = re.compile(rf"^\W*(?:{CLOSING_WORDS}\W*)+$", re.I)
# A participant asking the meeting agent to go: "you can leave now", "Smitline, drop off".
LEAVE_ASK = re.compile(
    r"\b(?:you (?:can|may|could)|please|go ahead and|feel free to|time to)\b[^.?!]{0,40}?"
    r"\b(?:leave|drop(?: off| out)?|go now|hang up|disconnect|log off|sign off|exit)\b"
    r"|\bsmit\w*\b[^.?!]{0,40}?\b(?:leave|drop(?: off| out)?|go now|hang up|disconnect|exit)\b",
    re.I)
# Quiet after the goodbye has been heard before a call hangs up, or a meeting is left.
GOODBYE_QUIET_SECONDS = 1.5
LEAVE_QUIET_SECONDS = 2.0
# How long a request to leave counts, waiting for the agent's goodbye.
LEAVE_ASK_SECONDS = 120.0


def says_goodbye(text):
    """What was said ends with a goodbye ("Talk soon, bye!")."""
    return bool(GOODBYE.search(' '.join(str(text or '').split())[-80:]))


def only_closing(text):
    """Nothing, or only thanks and goodbyes: nothing that carries the conversation on."""
    text = ' '.join(str(text or '').split())
    return not text or bool(CLOSING_ONLY.match(text))


def asks_to_leave(text):
    return bool(LEAVE_ASK.search(str(text or '')))


def should_leave_alone(participant_count):
    return participant_count == 1


def leave_end_reason(reason):
    return f'left_{reason if reason in LEAVE_REASONS else "other"}'


def is_idle(last_speech, now, limit):
    """Nobody, the agent included, has said anything for `limit` seconds (0 turns it off)."""
    return bool(limit) and now - last_speech >= limit
