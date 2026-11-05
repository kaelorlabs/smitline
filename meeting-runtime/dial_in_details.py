"""A meeting's dial-in details: reading them from an invitation or a one-tap number, the keys to
press and their timing, and what the meeting's phone line says before Smitline is in.

Pure functions, with no phone line behind them: call_brief.py reads a brief's dialIn with them
(the meeting container imports that module too), and dial_in.py joins by phone.
"""
import re
import unicodedata

from call_brief import emergency_number, normalize_phone
from meeting_urls import platform_for_url
from schema_validation import reject_unknown_fields, require_mapping, require_string

PLATFORMS = ('zoom', 'teams', 'meet')
DIAL_IN_FIELDS = ('number', 'meetingId', 'passcode', 'platform', 'keys', 'invite')
MAX_INVITE = 12000
# Keys in one-tap form: digits, * and #, and a comma for a two-second pause, as phone dialers
# take it. Each provider's <Play digits> pauses half a second per w.
PAUSE_SECONDS = 2.0
KEYS = re.compile(r'^[0-9*#,]{1,64}$')
MAX_PAUSES = 20
# Roughly how long a provider takes to play one key, tone and gap (only for time limits).
KEY_SECONDS = 0.3
MEETING_ID = re.compile(r'^\d{4,20}$')
PASSCODE = re.compile(r'^\d{1,20}$')
# How a platform's one-tap number keys a meeting in, for a brief without one: Zoom ends the meeting
# ID with # and, after a longer pause, enters the numeric phone passcode after *; Teams and Google
# Meet take the phone conference ID or PIN, then #. An unknown bridge gets the ID and the passcode.
ONE_TAP_KEYS = {
    'zoom': (',,{id}#', ',,,,*{passcode}#'),
    'teams': (',,{id}#', None),
    'meet': (',,{id}#', None),
    None: (',,{id}#', ',,,,{passcode}#'),
}
# Reading an invitation ---------------------------------------------------------------------------

# Format characters that invitations put around numbers (Google Meet wraps them in bidi marks).
_INVISIBLE = re.compile('[­​-‏‪-‮⁠-⁤﻿]')
_DASHES = re.compile('[‐-―−]')
# A number and the keys a phone dials after it, as invitations print them for one tap:
# +14388097799,,85951289482#,,,,*123456#   +1 323-849-4874,,123456789#   tel:+1-650-555-0123;123456789#
ONE_TAP = re.compile(r'(?<![\w+])(?:tel:)?(\+?[(\d][\d ().-]{5,22}\d)[ ]?([,;][\d*#,;]*[\d#])')
PHONE_ADDRESS = re.compile(r'(?:tel:)?(\+[(\d][\d ().-]{5,22}\d)[ ]?(?:([,;][\d*#,;]*[\d#]))?')
PLUS_NUMBER = re.compile(r'(?<![\w+])\+\d[\d ().-]{6,22}\d(?!\d)')
# A North American number written without +1 counts only on a line that says where it rings.
NANP_NUMBER = re.compile(r'(?<![\w+(])(?:1[ .-]?)?\(?[2-9]\d{2}\)?[ .-]?\d{3}[ .-]?\d{4}(?!\d)')
NORTH_AMERICA = re.compile(r'\b(?:US|USA|U\.S\.|United States|Canada|Toll[- ]?free)\b', re.I)
LABELED_ID = re.compile(
    r'\b(phone conference id|conference id|meeting id|webinar id|access code|meeting number|pin)\b'
    r'[ \t]*[:#.]?[ \t]*(\d[\d \t-]{2,30}\d)', re.I)
# Which labelled number is the one a phone enters. Teams invitations also print a "Meeting ID"
# and an app passcode for joining from the Teams app: on the phone only the conference ID counts.
ID_LABELS = {
    'teams': ('phone conference id', 'conference id'),
    'meet': ('pin', 'meeting id', 'access code'),
    'zoom': ('meeting id', 'webinar id'),
    None: ('phone conference id', 'conference id', 'pin', 'meeting id', 'webinar id', 'access code',
           'meeting number'),
}
PASSCODE_LABEL = re.compile(
    r'\b(?:numeric[ \t]+)?(?:meeting[ \t]+)?(?:passcode|pass code|password)\b[ \t]*[:#.]?[ \t]*([^\s,;]+)',
    re.I)
ZOOM_ID = re.compile(r'zoom\.us/(?:j|w|s|wc(?:/join)?)/(\d{9,11})(?!\d)', re.I)
PLATFORM_HINTS = (
    ('teams', re.compile(r'teams\.microsoft\.com|teams\.live\.com|microsoft teams|phone conference id', re.I)),
    ('meet', re.compile(r'meet\.google\.com|tel\.meet/|google meet', re.I)),
    ('zoom', re.compile(r'zoom\.us|\bzoom\b', re.I)),
)


def clean_text(text):
    """An invitation as plain text: Unicode compatibility forms, no invisible marks, ASCII dashes,
    and the keys of a tel: link (%23 is #) unescaped."""
    text = unicodedata.normalize('NFKC', str(text or ''))
    text = _DASHES.sub('-', _INVISIBLE.sub('', text))
    for escaped, plain in (('%23', '#'), ('%2C', ','), ('%2c', ','), ('%2B', '+'), ('%2b', '+')):
        text = text.replace(escaped, plain)
    return text


def _e164(raw):
    """A printed number as E.164, or None. Without + only a North American number counts."""
    digits = re.sub(r'\D', '', raw)
    if raw.strip().startswith('+'):
        candidate = '+' + digits
    elif len(digits) == 10 and digits[0] in '23456789':
        candidate = '+1' + digits
    elif len(digits) == 11 and digits[0] == '1':
        candidate = '+' + digits
    else:
        return None
    try:
        return normalize_phone(candidate)
    except ValueError:
        return None


def _keys(raw):
    """One-tap keys with ; (a dialer's wait) read as a pause."""
    return raw.replace(';', ',') if raw else None


def phone_address(value):
    """(E.164 number, its one-tap keys or None) when `value` is a dial-in number, a one-tap number
    or a tel: link, such as +14388097799,,85951289482#,,,,*123456#; None for anything else."""
    match = PHONE_ADDRESS.fullmatch(clean_text(value).strip())
    if not match:
        return None
    number = _e164(match[1])
    return (number, _keys(match[2])) if number else None


def invite_platform(text):
    """zoom, teams or meet: the platform the invitation names first; None when it names none."""
    found = [(match.start(), name) for name, pattern in PLATFORM_HINTS
             if (match := pattern.search(text))]
    return min(found)[1] if found else None


def parse_invite(text, platform=None):
    """What an invitation says about joining by phone: its numbers in order, each with the keys of
    its one-tap line (or None); the meeting ID or PIN; the numeric phone passcode; the platform."""
    text = clean_text(text)[:MAX_INVITE]
    platform = platform or invite_platform(text)
    numbers = []

    def add(number, keys):
        if number and all(number != have for have, _ in numbers):
            numbers.append((number, keys))
    for match in ONE_TAP.finditer(text):
        add(_e164(match[1]), _keys(match[2]))
    for line in text.splitlines():
        for match in PLUS_NUMBER.finditer(line):
            add(_e164(match[0]), None)
        if NORTH_AMERICA.search(line):
            for match in NANP_NUMBER.finditer(line):
                add(_e164(match[0]), None)
    labeled = {}
    for match in LABELED_ID.finditer(text):
        labeled.setdefault(' '.join(match[1].lower().split()), re.sub(r'\D', '', match[2]))
    meeting_id = next((labeled[label] for label in ID_LABELS.get(platform, ID_LABELS[None])
                       if label in labeled), None)
    if meeting_id is None and platform in ('zoom', None):
        zoom = ZOOM_ID.search(text)
        meeting_id = zoom[1] if zoom else None
    passcode = None
    if platform not in ('teams', 'meet'):  # their dial-ins take none; Teams' passcode is the app's
        for match in PASSCODE_LABEL.finditer(text):
            value = match[1].rstrip('#.')
            if value.isdigit():
                passcode = value  # the last numeric one: Zoom repeats the phone passcode last
    return {'numbers': numbers, 'meetingId': meeting_id, 'passcode': passcode, 'platform': platform}


def split_keys(keys):
    """(meeting ID, passcode) from one-tap keys: the first group of digits, and the last one when
    there are more (Zoom's *123456#, or the older ,,,,0#,,123456#)."""
    groups = [group for group in re.split(r',+', keys or '') if re.search(r'\d', group)]
    meeting_id = re.sub(r'\D', '', groups[0]) if groups else ''
    passcode = re.sub(r'\D', '', groups[-1]) if len(groups) > 1 else ''
    return meeting_id or None, passcode or None


def keys_for(platform, meeting_id, passcode=None):
    """The keys a platform's own one-tap number would dial for this meeting ID and passcode."""
    first, then = ONE_TAP_KEYS.get(platform, ONE_TAP_KEYS[None])
    keys = first.format(id=meeting_id)
    if passcode and then:
        keys += then.format(passcode=passcode)
    return keys


def _calling_codes(settings):
    """SMITLINE_ALLOWED_CALLING_CODES as prefixes; entries that are not codes are skipped here,
    since the precheck refuses such a list anyway (call_hooks._country_prefixes)."""
    codes = []
    for item in str(settings.get('SMITLINE_ALLOWED_CALLING_CODES') or '').split(','):
        item = item.strip().lstrip('+')
        if item.isdigit() and len(item) <= 7:
            codes.append('+' + item)
    return tuple(codes)


def _allowed(number, settings):
    """Whether the installation may call the number at all: its calling codes, and not premium."""
    from call_policy import PolicyProblem, check_cost
    settings = settings or {}
    codes = _calling_codes(settings)
    if codes and not number.startswith(codes):
        return False
    try:
        check_cost(number, settings)
    except PolicyProblem:
        return False
    return not emergency_number(number)


def pick_number(numbers, meeting_id=None, settings=None):
    """The number to dial among an invitation's: the first one the installation may call,
    preferring a one-tap line for this meeting ID. The first one when none may be called, so the
    refusal names it."""
    if not numbers:
        return None, None

    def rank(item):
        number, keys = item
        mine = bool(keys) and bool(meeting_id) and meeting_id in re.sub(r'\D', '', keys)
        return (not _allowed(number, settings), not mine, not keys)
    return sorted(numbers, key=rank)[0]


def _digits(value, name, pattern, example):
    if isinstance(value, int) and not isinstance(value, bool):
        value = str(value)  # agents sometimes send a meeting ID as a number
    text = re.sub(r'[\s.-]', '', require_string(value, name, max_length=40)).rstrip('#')
    if not pattern.fullmatch(text):
        raise ValueError(f'{name} must be digits, such as {example}')
    return text


def resolve(data, *, to=None, settings=None):
    """The dial-in a meeting brief joins by, from its dialIn (`data`, or None) and its `to` (a
    meeting link, or the dial-in number itself): {number, meetingId, keys, passcode?, platform?}.
    Fields the brief gives win over what the invitation text says. Raises ValueError."""
    data = require_mapping(data, 'dialIn') if data is not None else {}
    reject_unknown_fields(data, DIAL_IN_FIELDS, 'dialIn')
    number = keys = link_platform = zoom_id = None
    address = phone_address(to) if to else None
    if address:
        number, keys = address
    elif to:
        link_platform = platform_for_url(to)
        if link_platform == 'daily':
            raise ValueError('A Daily room has no dial-in number: leave dialIn out.')
        zoom = ZOOM_ID.search(to)
        zoom_id = zoom[1] if zoom else None
    platform = data.get('platform')
    if platform is not None and platform not in PLATFORMS:
        raise ValueError('dialIn.platform must be one of: ' + ', '.join(PLATFORMS))
    platform = platform or link_platform
    invite = None
    if data.get('invite') is not None:
        text = require_string(data['invite'], 'dialIn.invite', allow_newlines=True,
                              max_length=MAX_INVITE)
        invite = parse_invite(text, platform)
        platform = platform or invite['platform']
    if number is None and data.get('number') is not None:
        address = phone_address(require_string(data['number'], 'dialIn.number', max_length=120))
        if address is None:
            raise ValueError("dialIn.number must be the meeting's dial-in number in E.164 form, "
                             'such as +14155550142, optionally followed by its keys as in a one-tap '
                             'number: +14155550142,,123456789#')
        number, keys = address
    meeting_id = (_digits(data['meetingId'], 'dialIn.meetingId', MEETING_ID, '859 5128 9482')
                  if data.get('meetingId') is not None else None)
    passcode = (_digits(data['passcode'], 'dialIn.passcode', PASSCODE, '123456')
                if data.get('passcode') is not None else None)
    if passcode and platform in ('teams', 'meet'):
        raise ValueError('Teams and Google Meet dial-ins take no passcode: give the phone '
                         'conference ID or the PIN as dialIn.meetingId.')
    known_id = meeting_id or (invite or {}).get('meetingId') or zoom_id
    if number is None and invite:
        number, keys = pick_number(invite['numbers'], known_id, settings)
    if number is None:
        raise ValueError("dialIn needs the meeting's dial-in number: dialIn.number, the invitation "
                         'text in dialIn.invite, or the number as to.')
    if emergency_number(number):
        raise ValueError('Smitline never calls emergency or crisis numbers.')
    if data.get('keys') is not None:
        keys = require_string(data['keys'], 'dialIn.keys', max_length=64)
    if keys is not None:
        keys = keys.replace(' ', '')
        if not KEYS.fullmatch(keys) or not re.search(r'\d', keys) or keys.count(',') > MAX_PAUSES:
            raise ValueError('dialIn.keys must be digits, * and #, with a comma for each two-second '
                             f'pause (at most {MAX_PAUSES}), as in a one-tap number: ,,123456789#')
        from_keys, passcode_in_keys = split_keys(keys)
        if meeting_id and meeting_id not in re.sub(r'\D', '', keys):
            raise ValueError('dialIn.meetingId is not in the keys of the one-tap number.')
        meeting_id = meeting_id or from_keys
        passcode = passcode or passcode_in_keys
    else:
        meeting_id = known_id
        passcode = passcode or (invite or {}).get('passcode')
        if not meeting_id:
            raise ValueError('dialIn needs the meeting ID to key in: dialIn.meetingId (for Teams the '
                             'phone conference ID, for Google Meet the PIN), the invitation text in '
                             'dialIn.invite, or a one-tap number.')
        keys = keys_for(platform, meeting_id, passcode)
    dial = {'number': number, 'meetingId': meeting_id, 'keys': keys}
    if passcode:
        dial['passcode'] = passcode
    if platform:
        dial['platform'] = platform
    return dial


def play_digits(keys):
    """One-tap keys as <Play digits> takes them: each comma two seconds of w (half a second)."""
    return keys.replace(',', 'w' * int(PAUSE_SECONDS / 0.5))


def keys_seconds(keys):
    """About how long the provider takes to play the keys."""
    return keys.count(',') * PAUSE_SECONDS + sum(char != ',' for char in keys) * KEY_SECONDS


# What the bridge says before Smitline is in --------------------------------------------------------

# Only what a bridge says: the first sentence heard may be a person in the meeting, and "that's
# wrong" from them is not a refusal.
_KEYED = r"(?:meeting|conference|webinar|participant)?\s*(?:id|pin|passcode|password|code|number)"
REFUSED = re.compile(
    rf"\b{_KEYED}\b[^.?!]{{0,40}}\b(?:is invalid|is not valid|isn't valid|is incorrect|is not correct|"
    r"is wrong|was not recogni[sz]ed|wasn't recogni[sz]ed|not accepted)\b"
    rf"|\b(?:invalid|incorrect|wrong)\s+{_KEYED}\b"
    r"|\b(?:this|the) (?:meeting|conference|webinar|call) (?:has (?:already )?ended|is over|"
    r"no longer exists|does not exist|doesn't exist|was not found|cannot be found|could not be found)\b",
    re.I)
WAITING = re.compile(
    r"\b(?:waiting room|lobby|let you in|lets you in|admit(?:s|ted)? you|be admitted|host has not|"
    r"host hasn't|organi[sz]er has not|has not (?:yet )?(?:started|begun)|hasn't (?:started|begun)|"
    r"not started yet|wait for the (?:host|organi[sz]er)|until the (?:host|organi[sz]er)|"
    r"when the (?:host|organi[sz]er)|(?:please|continue to) (?:wait|hold))\b", re.I)
PROMPT = re.compile(
    r"\b(?:enter|press|key in|dial)\b[^.?!]{0,40}\b(?:id|pin|passcode|password|code|number|pound|hash|star)\b",
    re.I)


def bridge_says(sentence):
    """What a sentence the meeting's phone line said before Smitline was in means: refused (the ID
    or passcode was not accepted, or the meeting is over), waiting (to be let in), prompt (it asks
    for keys: not a person), or None (anything else, such as people talking: Smitline is in)."""
    text = ' '.join(str(sentence or '').split())
    if REFUSED.search(text):
        return 'refused'
    if WAITING.search(text):
        return 'waiting'
    if PROMPT.search(text):
        return 'prompt'
    return None
