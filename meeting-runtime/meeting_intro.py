"""Who the meeting voice is: its name, the caption on its camera tile, and the optional hello.

Meetings join quietly. The bot's name ("Smitline AI") and the caption on its tile ("Arun's AI
assistant") say it is an AI for as long as it is there, also to people who join later; a spoken
hello is opt-in (SMITLINE_MEETING_INTRO=1). Phone calls, which have no screen, say it aloud.
"""
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


def assistant_of(owner):
    """ "Arun's AI assistant", or "an AI assistant for the person who invited me". """
    return f"{spoken_name(owner)}'s AI assistant" if owner else f'an AI assistant for {FALLBACK_OWNER}'


def intro_line(owner):
    if not owner:
        return f"Hi everyone, I'm {assistant_of(owner)}."
    return f"Hi everyone, {assistant_of(owner)} here."


MAX_CAPTION = 40


def camera_caption(owner):
    """The one line the camera tile shows, under the mark: never task text."""
    caption = f"{spoken_name(owner)}'s AI assistant" if owner else 'AI assistant'
    return caption if len(caption) <= MAX_CAPTION else 'AI assistant'


# Platform labels a participant list puts after a name: "Arun Raj (Host, me)".
_LABELS = re.compile(r'\s*\((?:host|co-host|me|you|guest|organizer|presenter)\b[^)]*\)\s*$', re.I)
MAX_PARTICIPANTS = 30


def others_in(names, own_name):
    """The meeting's participants without the bot itself: tidied, in order, without repeats."""
    own = ' '.join(str(own_name or '').split()).casefold()
    out = []
    for name in names or ():
        name = _LABELS.sub('', ' '.join(str(name or '').split())).strip()
        if not name or len(name) > MAX_NAME or name in out:
            continue
        if own and (name.casefold() == own or name.casefold().startswith(own + ' ')):
            continue
        out.append(name)
    return out[:MAX_PARTICIPANTS]


def participants_note(names, owner, own_name):
    """Quiet context: who is in the meeting, and which of them is probably the owner."""
    first = spoken_name(owner).casefold() if owner else ''
    listed = []
    for name in names:
        words = name.casefold().replace(',', ' ').split()
        if first and first in words:
            listed.append(f'{name} (probably {spoken_name(owner)}, the person you work for)')
        else:
            listed.append(name)
    return ('People in the meeting now: ' + ', '.join(listed) + f'. You are listed as {own_name}. '
            'Use their names when it helps. Do not announce this list.')


def identity_instructions(owner):
    """Who the voice is, for when someone greets it or asks; it does not announce itself."""
    who = assistant_of(owner).replace('invited me', 'invited you')
    return (f'\nWho you are: {who}. Your name and your camera tile show the meeting that you are '
            'an AI. Do not introduce yourself unprompted; if someone greets you or asks who you '
            'are, say so in a few natural words.')


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
