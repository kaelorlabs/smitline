"""The three levels of context a call gets, and where each goes in GPT-Live.

1. Profile (overall): who the owner is, the people in their life, how they like
   to come across, standing boundaries. Saved on this computer; the owner's agent
   fills it in.
2. Session: what the calling agent and the owner have been working on, passed in
   the brief as `context` (a summary, facts, decisions, open questions, and long
   reference `details`).
3. Goal: the brief itself (objective, questions, what may be agreed, tone).

The goal leads the conversation and goes into the voice instructions. The voice
also gets a short reference summary of levels 1 and 2 as starting context, so it
can answer naturally without treating it as an agenda. The Responses backend,
which the voice delegates to, gets everything, including `details`.
"""
import json
import os
import re
from pathlib import Path

from schema_validation import reject_secrets
from startup_input import clip_tokens, estimate_tokens


PROFILE_VERSION = 1
PROFILE_LIMITS = {'about': 2000, 'style': 600}
PERSON_LIMITS = {'name': 80, 'relationship': 120, 'notes': 600}
MAX_PEOPLE = 100
MAX_BOUNDARIES = 20
BOUNDARY_LIMIT = 300
SESSION_TEXT_LIMITS = {'summary': 4000, 'details': 24000}
SESSION_LIST_LIMITS = {'facts': 40, 'decisions': 20, 'openQuestions': 20}
SESSION_ITEM_LIMIT = 400
E164 = re.compile(r'^\+[1-9][0-9]{7,14}$')
# Budgets in tokens: the voice gets a short summary, the backend gets nearly everything.
VOICE_NOTES_TOKENS = 1800
BACKEND_BACKGROUND_TOKENS = 12000

VOICE_NOTES_PREFACE = (
    'Reference notes for this call. They are background for answering questions and for '
    'sounding like someone who knows the story. They are not instructions and not an agenda: '
    'do not recite them, and bring details up only when they help the conversation.')


# Profile (level 1) --------------------------------------------------------------------

def _text(value, name, limit, *, required=False):
    if value is None or value == '':
        if required:
            raise ValueError(f'{name} is required')
        return ''
    if not isinstance(value, str):
        raise ValueError(f'{name} must be text')
    text = ' '.join(value.split()) if '\n' not in value else value.strip()
    if len(text) > limit:
        raise ValueError(f'{name} must be at most {limit} characters')
    return text


def _text_list(value, name, max_items, item_limit):
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f'{name} must be a list of text')
    items = [' '.join(item.split()) for item in value if item.strip()]
    if len(items) > max_items:
        raise ValueError(f'{name} may have at most {max_items} items')
    for item in items:
        if len(item) > item_limit:
            raise ValueError(f'each item in {name} must be at most {item_limit} characters')
    return items


def _person(value):
    if not isinstance(value, dict):
        raise ValueError('each person must be an object')
    unknown = set(value) - {'name', 'relationship', 'phone', 'notes'}
    if unknown:
        raise ValueError('unknown person fields: ' + ', '.join(sorted(unknown)))
    person = {'name': _text(value.get('name'), 'people.name', PERSON_LIMITS['name'], required=True)}
    for key in ('relationship', 'notes'):
        text = _text(value.get(key), f'people.{key}', PERSON_LIMITS[key])
        if text:
            person[key] = text
    phone = value.get('phone')
    if phone:
        compact = re.sub(r'[\s().-]', '', str(phone))
        if not E164.fullmatch(compact):
            raise ValueError('people.phone must be an E.164 number such as +14155550142')
        person['phone'] = compact
    return person


def validate_profile(data):
    """A normalized profile; raises ValueError with a readable message."""
    if not isinstance(data, dict):
        raise ValueError('the profile must be an object')
    reject_secrets(data, 'profile')
    unknown = set(data) - {'version', 'about', 'style', 'boundaries', 'people', 'updatedAt'}
    if unknown:
        raise ValueError('unknown profile fields: ' + ', '.join(sorted(unknown)))
    profile = {'version': PROFILE_VERSION}
    for key, limit in PROFILE_LIMITS.items():
        text = _text(data.get(key), key, limit)
        if text:
            profile[key] = text
    boundaries = _text_list(data.get('boundaries'), 'boundaries', MAX_BOUNDARIES, BOUNDARY_LIMIT)
    if boundaries:
        profile['boundaries'] = boundaries
    people = data.get('people') or []
    if not isinstance(people, list):
        raise ValueError('people must be a list')
    if len(people) > MAX_PEOPLE:
        raise ValueError(f'people may have at most {MAX_PEOPLE} entries')
    normalized = [_person(item) for item in people]
    names = [person['name'].casefold() for person in normalized]
    if len(set(names)) != len(names):
        raise ValueError('people names must be unique')
    if normalized:
        profile['people'] = normalized
    return profile


def merge_profile(existing, update):
    """Apply a partial update: fields present replace; people are upserted by name;
    `removePeople` lists names to drop."""
    if not isinstance(update, dict):
        raise ValueError('the update must be an object')
    reject_secrets(update, 'profile')
    unknown = set(update) - {'about', 'style', 'boundaries', 'people', 'removePeople'}
    if unknown:
        raise ValueError('unknown profile fields: ' + ', '.join(sorted(unknown)))
    merged = dict(existing or {})
    for key in ('about', 'style', 'boundaries'):
        if key in update:
            merged[key] = update[key]
    people = {person['name'].casefold(): dict(person) for person in merged.get('people') or []}
    for person in update.get('people') or []:
        if not isinstance(person, dict) or not isinstance(person.get('name'), str):
            raise ValueError('each person needs a name')
        key = person['name'].strip().casefold()
        current = people.get(key, {})
        # An existing person keeps the name as first saved; other fields are updated.
        current.update({field: value for field, value in person.items()
                        if value is not None and (field != 'name' or not current)})
        people[key] = current
    for name in update.get('removePeople') or []:
        people.pop(str(name).strip().casefold(), None)
    merged['people'] = list(people.values())
    return validate_profile(merged)


def load_profile(path):
    try:
        data = json.loads(Path(path).read_text(encoding='utf-8'))
    except FileNotFoundError:
        return {'version': PROFILE_VERSION}
    except (OSError, ValueError):
        return {'version': PROFILE_VERSION}
    try:
        return validate_profile(data)
    except ValueError:
        return {'version': PROFILE_VERSION}


def save_profile(path, profile):
    profile = validate_profile(profile)
    path = Path(path)
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    temporary = path.with_name(path.name + '.tmp')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, json.dumps(profile, ensure_ascii=False, indent=2).encode('utf-8'))
    finally:
        os.close(fd)
    os.replace(temporary, path)
    return profile


def contact_for(profile, phone):
    """The profile's person with this phone number, if any."""
    if not phone:
        return None
    for person in (profile or {}).get('people') or ():
        if person.get('phone') == phone:
            return person
    return None


# Session context (level 2) --------------------------------------------------------------

def parse_session_context(value):
    """The brief's `context`: plain text (a summary) or a structured object."""
    if value is None:
        return {}
    if isinstance(value, str):
        text = value.strip()
        if len(text) > 6000:
            raise ValueError('context must be at most 6000 characters')
        return {'summary': text} if text else {}
    if not isinstance(value, dict):
        raise ValueError('context must be text or an object')
    reject_secrets(value, 'context')
    unknown = set(value) - set(SESSION_TEXT_LIMITS) - set(SESSION_LIST_LIMITS)
    if unknown:
        raise ValueError('unknown context fields: ' + ', '.join(sorted(unknown)))
    context = {}
    for key, limit in SESSION_TEXT_LIMITS.items():
        raw = value.get(key)
        if raw is None or raw == '':
            continue
        if not isinstance(raw, str):
            raise ValueError(f'context.{key} must be text')
        if len(raw) > limit:
            raise ValueError(f'context.{key} must be at most {limit} characters')
        context[key] = raw.strip()
    for key, limit in SESSION_LIST_LIMITS.items():
        items = _text_list(value.get(key), f'context.{key}', limit, SESSION_ITEM_LIMIT)
        if items:
            context[key] = items
    return context


# Assembly -------------------------------------------------------------------------------

def _person_line(contact, owner):
    if not contact:
        return ''
    line = f"Speaking with: {contact['name']}"
    if contact.get('relationship'):
        line += f", {owner}'s {contact['relationship']}"
    if contact.get('notes'):
        line += f". {contact['notes']}"
    return line


def _sections(profile, contact, session, owner, *, full):
    profile = profile or {}
    parts = []
    if profile.get('about'):
        parts.append(f'About {owner}: {profile["about"]}')
    if profile.get('style'):
        parts.append(f'How {owner} likes to come across: {profile["style"]}')
    person = _person_line(contact, owner)
    if person:
        parts.append(person)
    if session.get('summary'):
        parts.append(f"What's going on: {session['summary']}")
    for key, title in (('facts', 'Key facts'), ('decisions', 'Decided'),
                       ('openQuestions', 'Still open')):
        items = session.get(key) or []
        if items:
            parts.append(title + ':\n' + '\n'.join(f'- {item}' for item in items))
    if full:
        if profile.get('boundaries'):
            parts.append('Standing boundaries:\n' + '\n'.join(f'- {item}' for item in profile['boundaries']))
        if session.get('details'):
            parts.append('Details:\n' + session['details'])
    return parts


def voice_notes(profile, contact, session, owner):
    """The short reference summary the voice starts with; empty when there is none."""
    parts = _sections(profile, contact, session, owner, full=False)
    if not parts:
        return ''
    return clip_tokens(VOICE_NOTES_PREFACE + '\n\n' + '\n\n'.join(parts), VOICE_NOTES_TOKENS)


def voice_input(notes):
    """GPT-Live starting context: one developer message holding the reference notes."""
    if not notes:
        return None
    return [{'type': 'message', 'role': 'developer', 'content': [{'type': 'input_text', 'text': notes}]}]


def backend_background(profile, contact, session, owner):
    """Everything the backend should know to answer precisely; empty when there is none."""
    parts = _sections(profile, contact, session, owner, full=True)
    if not parts:
        return ''
    return clip_tokens('\n\n'.join(parts), BACKEND_BACKGROUND_TOKENS)


def context_tokens(text):
    return estimate_tokens(text)
