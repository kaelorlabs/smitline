"""The short AI disclosure a meeting session speaks once it is admitted and live."""
import asyncio
import re

from voice_core import append_event


FALLBACK_OWNER = 'the person who invited me'
# The name the disclosure asks people to use.
CALL_NAME = 'Smitline'
# How speech recognition often hears "Smitline".
CALL_NAME_HEARD_AS = ('smit line', 'Smith line', 'Smithline', 'Smitlin')
# The product's earlier name; people who learned it can still call on the assistant with it.
FORMER_CALL_NAME = 'Colleague'
# meeting_line.context_from_brief writes this task for meetings started through /v1/calls.
CALL_TASK = re.compile(r'^Take part in this meeting on behalf of (.+?)\.?$')
MAX_NAME = 120


def _clean_name(value):
    name = ' '.join(str(value or '').split())
    if not name or len(name) > MAX_NAME:
        return ''
    return name


def owner_name(state=None, environ=None):
    """Whose behalf the meeting agent acts on: explicit field, call task, then the owner setting."""
    state = state if isinstance(state, dict) else {}
    explicit = _clean_name(state.get('onBehalfOf'))
    if explicit:
        return explicit
    context = state.get('context') if isinstance(state.get('context'), dict) else {}
    match = CALL_TASK.match(str(context.get('currentTask') or '').strip())
    if match and _clean_name(match[1]):
        return _clean_name(match[1])
    return _clean_name((environ or {}).get('COLLEAGUE_OWNER_NAME'))


def intro_line(owner):
    who = f"{owner}'s AI assistant" if owner else f'an AI assistant for {FALLBACK_OWNER}'
    return f"Hi everyone, I'm {who}. I'll mostly listen; say '{CALL_NAME}' if you need me."


def addressing_instructions(participant_name=''):
    """Which names address the assistant: its participant name, Smitline as it is often heard,
    and the former name Colleague. GPT-Live hears the meeting, so this is how it recognizes them."""
    names = [f'"{CALL_NAME}"']
    name = ' '.join(str(participant_name or '').split())
    if name and name.casefold() != CALL_NAME.casefold():
        names.insert(0, f'"{name}"')
    heard = ', '.join(f'"{variant}"' for variant in CALL_NAME_HEARD_AS)
    return ('\nYour name: people address you as ' + ' or '.join(names) + '. You may hear '
            f'{CALL_NAME} as {heard}, or similar; treat those as your name. "{FORMER_CALL_NAME}" '
            'also addresses you when someone uses it as a name to call on you, such as '
            f'"{FORMER_CALL_NAME}, what do you think?", but not when they talk about a colleague of theirs.')


def intro_instructions(owner):
    return ('\nOpening disclosure: when the application cues you right after you join, say this '
            f'once, briefly and naturally: "{intro_line(owner)}" Do not repeat it unless asked; '
            'afterward follow the participation policy and keep listening silently.')


def intro_event(runtime):
    """The commentary cue that makes GPT-Live speak the disclosure, or None when disabled."""
    if not runtime.meeting_intro:
        return None
    return append_event(
        'session.commentary.append',
        f'You were just admitted to the meeting. Introduce yourself now, once: '
        f'"{intro_line(runtime.owner_name)}" Then stay silent and keep listening.')


async def introduce(send, runtime, participation, ready, stop, poll=0.2):
    """Send the cue once the session is live and the meeting microphone can carry speech."""
    event = intro_event(runtime)
    if event is None:
        return False
    await ready.wait()
    while not participation.platform_ready:
        if stop.is_set():
            return False
        await asyncio.sleep(poll)
    await send(event)
    return True
