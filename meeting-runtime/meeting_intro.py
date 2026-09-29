"""The short AI disclosure a meeting session speaks once it is admitted and live."""
import asyncio
import re

from voice_core import append_event


FALLBACK_OWNER = 'the person who invited me'
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
    return f"Hi everyone, I'm {who}. I'll mostly listen; say 'Colleague' if you need me."


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
