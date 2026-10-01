"""Guardrails for outbound phone calls.

Before Smitline dials, it checks that the number is not premium-rate or satellite, that the
person has not asked not to be called again, that it is daytime where they are, and that the
number has not been called too often already. Emergency numbers are refused earlier, when the
brief is read (call_brief.emergency_number). Rehearsals ring the owner and skip these checks.
"""
from datetime import datetime, time, timedelta, timezone
import json
import math
from pathlib import Path
import re
import threading
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import phonenumbers
from phonenumbers import timezone as number_timezones

from call_brief import normalize_phone


# Inmarsat, satellite services, international networks, and international premium rate:
# a minute can cost dollars, and fraud schemes are paid through such numbers.
HIGH_COST_CODES = frozenset({870, 881, 882, 883, 979})
DEFAULT_HOURS = (time(8), time(21))
DEFAULT_MAX_PER_NUMBER = 5
DEFAULT_MAX_PER_HOUR = 20
HOURS = re.compile(r'^(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})$')


class PolicyProblem(Exception):
    """A call the policy refuses, with a code agents can act on."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


def _parse(number):
    try:
        return phonenumbers.parse(number)
    except phonenumbers.NumberParseException:
        return None


def check_cost(number, env):
    """Refuse premium-rate and satellite numbers unless COLLEAGUE_ALLOW_PREMIUM_NUMBERS=1."""
    if str(env.get('COLLEAGUE_ALLOW_PREMIUM_NUMBERS') or '').strip() == '1':
        return
    parsed = _parse(number)
    if parsed is None:
        return
    if parsed.country_code in HIGH_COST_CODES:
        kind = 'a satellite or international-network number'
    elif phonenumbers.number_type(parsed) == phonenumbers.PhoneNumberType.PREMIUM_RATE:
        kind = 'a premium-rate number'
    else:
        return
    raise PolicyProblem('high_cost_number',
                        f'{number} is {kind}, which can cost several dollars a minute. To call such '
                        'numbers anyway, set COLLEAGUE_ALLOW_PREMIUM_NUMBERS=1.')


def calling_hours(env):
    """(start, end) from COLLEAGUE_CALLING_HOURS such as 08:00-21:00; None when it is off."""
    raw = str(env.get('COLLEAGUE_CALLING_HOURS') or '').strip().lower()
    if not raw:
        return DEFAULT_HOURS
    if raw == 'off':
        return None
    match = HOURS.fullmatch(raw)
    if match:
        start_h, start_m, end_h, end_m = map(int, match.groups())
        if start_h < 24 and end_h < 24 and start_m < 60 and end_m < 60 and \
                (start_h, start_m) < (end_h, end_m):
            return time(start_h, start_m), time(end_h, end_m)
    raise PolicyProblem('invalid_calling_hours',
                        'COLLEAGUE_CALLING_HOURS must look like 08:00-21:00, or be off.')


def _clock(moment):
    return moment.strftime('%I:%M %p').lstrip('0')


def recipient_zones(number):
    """Time zones the number may ring in; empty when the number does not tell."""
    parsed = _parse(number)
    if parsed is None:
        return ()
    zones = []
    for name in number_timezones.time_zones_for_number(parsed):
        try:
            zones.append(ZoneInfo(name))
        except (ZoneInfoNotFoundError, ValueError):
            continue
    return tuple(zones)


def check_hours(number, now, env):
    """Refuse a call when it is outside calling hours in every zone the number may ring in.

    A number with one zone (most landlines, North American mobiles) is checked exactly. For
    one that spans zones, such as a Russian or Australian mobile, the call goes ahead while it
    is daytime in any of them.
    """
    hours = calling_hours(env)
    zones = recipient_zones(number)
    if hours is None or not zones:
        return
    start, end = hours
    local = [now.astimezone(zone) for zone in zones]
    if any(start <= moment.time() < end for moment in local):
        return
    if len(local) == 1:
        where = f'It is {_clock(local[0])} for {number} ({zones[0].key}).'
    else:
        where = f'It is outside those hours in every time zone {number} may ring in.'
    raise PolicyProblem('outside_calling_hours',
                        f'Smitline calls people between {_clock(start)} and {_clock(end)} their '
                        f'time. {where} Try again later, or set afterHours to true if the user '
                        'confirms this person expects a call now.')


def _limit(env, name, default):
    raw = str(env.get(name) or '').strip()
    if not raw:
        return default
    if not raw.isdigit() or len(raw) > 4:
        raise PolicyProblem('invalid_call_limit',
                            f'{name} must be a whole number; 0 turns the limit off.')
    return int(raw)


def _created(record):
    try:
        return datetime.fromisoformat(str(record.get('createdAt')).replace('Z', '+00:00'))
    except ValueError:
        return None


def _in_hours(delta):
    hours = max(1, math.ceil(delta.total_seconds() / 3600))
    return 'in about an hour' if hours == 1 else f'in about {hours} hours'


def check_repeats(number, records, now, env, *, own_number=False):
    """Limit calls to one number per 24 hours, and outbound calls per hour.

    `records` are earlier calls; rehearsals and incoming calls do not count. The owner's own
    phone has no per-number limit.
    """
    per_number = _limit(env, 'COLLEAGUE_MAX_CALLS_PER_NUMBER', DEFAULT_MAX_PER_NUMBER)
    per_hour = _limit(env, 'COLLEAGUE_MAX_CALLS_PER_HOUR', DEFAULT_MAX_PER_HOUR)
    day_ago, hour_ago = now - timedelta(days=1), now - timedelta(hours=1)
    to_number, last_hour = [], []
    for record in records:
        brief = record.get('brief') or {}
        if record.get('channel') != 'phone' or record.get('direction') != 'outbound' or \
                brief.get('rehearsal'):
            continue
        created = _created(record)
        if created is None:
            continue
        if created > hour_ago:
            last_hour.append(created)
        if brief.get('to') == number and created > day_ago:
            to_number.append(created)
    if per_number and not own_number and len(to_number) >= per_number:
        raise PolicyProblem('too_many_calls_to_number',
                            f'Smitline has called {number} {len(to_number)} times in the last 24 '
                            'hours, the most COLLEAGUE_MAX_CALLS_PER_NUMBER allows. It can call '
                            f'again {_in_hours(min(to_number) + timedelta(days=1) - now)}.')
    if per_hour and len(last_hour) >= per_hour:
        raise PolicyProblem('too_many_calls',
                            f'Smitline has placed {len(last_hour)} calls in the last hour, the most '
                            'COLLEAGUE_MAX_CALLS_PER_HOUR allows. It can call again '
                            f'{_in_hours(min(last_hour) + timedelta(hours=1) - now)}.')


class DoNotCallList:
    """People who asked not to be called again, kept as JSON next to the owner's profile.

    Without a path (tests, hooks that keep no files) the list lives in memory.
    """

    VERSION = 1

    def __init__(self, path=None):
        self.path = Path(path) if path else None
        self._memory = []
        self._lock = threading.Lock()

    def entries(self):
        if self.path is None:
            return [dict(entry) for entry in self._memory]
        try:
            data = json.loads(self.path.read_text(encoding='utf-8'))
        except FileNotFoundError:
            return []
        except ValueError:
            # A damaged list must not quietly allow calls to the people on it.
            raise PolicyProblem('do_not_call_unreadable',
                                'The do-not-call list (.colleague/do-not-call.json) cannot be '
                                'read; fix or remove it.')
        return [entry for entry in data.get('numbers') or [] if isinstance(entry, dict)]

    def _save(self, entries):
        if self.path is None:
            self._memory = entries
            return
        from call_store import _atomic_write
        self.path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        _atomic_write(self.path, json.dumps({'version': self.VERSION, 'numbers': entries},
                                            ensure_ascii=False, indent=2))

    def update(self, update, *, now=None):
        """Apply {'add': [number or {number, reason, callId}], 'remove': [number]}; returns the list."""
        if not isinstance(update, dict) or not update or set(update) - {'add', 'remove'}:
            raise ValueError('a do-not-call update has add, remove, or both')
        adds, removes = update.get('add') or [], update.get('remove') or []
        if not isinstance(adds, list) or not isinstance(removes, list) or \
                len(adds) + len(removes) > 100:
            raise ValueError('add and remove must be lists of at most 100 numbers')
        moment = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        stamp = moment.isoformat(timespec='seconds').replace('+00:00', 'Z')
        with self._lock:
            entries = self.entries()
            gone = {normalize_phone(value, 'remove') for value in removes}
            entries = [entry for entry in entries if entry.get('number') not in gone]
            for item in adds:
                item = {'number': item} if isinstance(item, str) else item
                if not isinstance(item, dict) or set(item) - {'number', 'reason', 'callId'}:
                    raise ValueError('each added entry is a number or {number, reason, callId}')
                number = normalize_phone(item.get('number'), 'add.number')
                if any(entry.get('number') == number for entry in entries):
                    continue
                entry = {'number': number, 'addedAt': stamp}
                reason = str(item.get('reason') or '').strip()
                if reason:
                    entry['reason'] = reason[:300]
                if item.get('callId'):
                    entry['callId'] = str(item['callId'])[:40]
                entries.append(entry)
            self._save(entries)
            return entries


def check_do_not_call(number, entries):
    entry = next((item for item in entries if item.get('number') == number), None)
    if entry is None:
        return
    since = str(entry.get('addedAt') or '')[:10]
    raise PolicyProblem('do_not_call',
                        f'{number} is on the do-not-call list' + (f' since {since}' if since else '') +
                        '. Call only if they have since agreed to calls, after removing the number '
                        f'with: smitline do-not-call remove {number}')
