"""The short AI disclosure a meeting session speaks once it is admitted and live."""
import asyncio
import re
import time

from voice_core import append_event


FALLBACK_OWNER = 'the person who invited me'
# The name the disclosure asks people to use.
CALL_NAME = 'Smitline'
# How speech recognition often hears "Smitline".
CALL_NAME_HEARD_AS = ('smit line', 'Smith line', 'Smithline', 'Smitlin')
# meeting_line.context_from_brief writes this task for meetings started through /v1/calls.
CALL_TASK = re.compile(r'^Take part in this meeting on behalf of (.+?)\.?$')
MAX_NAME = 120
# The intro waits for a pause: this long with nobody heard speaking. If the meeting never
# pauses, it is said anyway after INTRO_MAX_WAIT, since the disclosure must be made.
INTRO_QUIET_SECONDS = 2.0
INTRO_MAX_WAIT = 45.0
TITLES = ('dr', 'mr', 'mrs', 'ms', 'prof')


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
    return _clean_name((environ or {}).get('SMITLINE_OWNER_NAME'))


def spoken_name(owner):
    """What people call the owner: the first name, or the full name after a title."""
    words = str(owner or '').split()
    if not words:
        return ''
    return ' '.join(words) if words[0].rstrip('.').casefold() in TITLES else words[0]


def intro_line(owner):
    if not owner:
        return (f"Hi everyone, I'm an AI assistant for {FALLBACK_OWNER}. "
                f"Just say '{CALL_NAME}' if you need me.")
    return f"Hi everyone, {spoken_name(owner)}'s AI assistant here. Just say '{CALL_NAME}' if you need me."


def addressing_instructions(participant_name=''):
    """Which names address the assistant: its participant name, and Smitline as it is often heard.
    GPT-Live hears the meeting, so this is how it recognizes them."""
    names = [f'"{CALL_NAME}"']
    name = ' '.join(str(participant_name or '').split())
    if name and name.casefold() != CALL_NAME.casefold():
        names.insert(0, f'"{name}"')
    heard = ', '.join(f'"{variant}"' for variant in CALL_NAME_HEARD_AS)
    return ('\nYour name: people address you as ' + ' or '.join(names) + '. You may hear '
            f'{CALL_NAME} as {heard}, or similar; treat those as your name.')


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


async def introduce(send, runtime, participation, ready, stop, poll=0.2, heard_at=None,
                    clock=time.monotonic, quiet=None, max_wait=None):
    """Send the cue once the session is live, the meeting microphone can carry speech, and
    nobody is talking: heard_at() is when someone in the meeting was last heard (None: not yet).
    Joining in the middle of a sentence and talking over it is rude; after max_wait it is said
    at the next chance anyway, since the disclosure must be made."""
    event = intro_event(runtime)
    if event is None:
        return False
    await ready.wait()
    while not participation.platform_ready:
        if stop.is_set():
            return False
        await asyncio.sleep(poll)
    quiet = INTRO_QUIET_SECONDS if quiet is None else quiet
    max_wait = INTRO_MAX_WAIT if max_wait is None else max_wait
    started = clock()
    while heard_at is not None and clock() - started < max_wait:
        if stop.is_set():
            return False
        last = heard_at()
        if clock() - max(started, last or started) >= quiet:
            break
        await asyncio.sleep(poll)
    await send(event)
    return True
