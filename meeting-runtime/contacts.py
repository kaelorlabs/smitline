"""Contacts: what the user saved about a number Smitline has called or been called from.

A contact exists for every number in the call store; this file only keeps what the user or
their agent added: a name, notes, and whether new calls to the number start with notes from
earlier calls (autoContext). Kept as JSON next to the owner's profile, like the do-not-call
list; without a path (tests, hooks that keep no files) it lives in memory.
"""
import json
import threading
from datetime import datetime, timezone
from pathlib import Path

from call_brief import normalize_phone
from schema_validation import reject_secrets


MAX_CONTACTS = 1000
TEXT_LIMITS = {'name': 80, 'notes': 600}
FIELDS = ('name', 'notes', 'autoContext')


class ContactsUnreadable(ValueError):
    """The contacts file exists but cannot be read; it is never silently replaced."""


def _text(value, name):
    if value is None:
        return ''
    if not isinstance(value, str):
        raise ValueError(f'{name} must be text')
    text = ' '.join(value.split())
    if len(text) > TEXT_LIMITS[name]:
        raise ValueError(f'{name} must be at most {TEXT_LIMITS[name]} characters')
    return text


class ContactBook:
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
        except (OSError, ValueError) as error:
            # Saving over a damaged file would lose every name and setting in it.
            raise ContactsUnreadable('The contacts file (.colleague/contacts.json) cannot be read; '
                                     'fix or remove it.') from error
        return [entry for entry in data.get('contacts') or [] if isinstance(entry, dict)]

    def get(self, number):
        number = normalize_phone(number, 'number')
        return next((dict(entry) for entry in self.entries() if entry.get('number') == number), None)

    def _save(self, entries):
        if self.path is None:
            self._memory = entries
            return
        from call_store import _atomic_write
        self.path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        _atomic_write(self.path, json.dumps({'version': self.VERSION, 'contacts': entries},
                                            ensure_ascii=False, indent=2))

    def update(self, number, changes, *, now=None):
        """Set name, notes, or autoContext for a number; '' or false clears. Returns the entry."""
        number = normalize_phone(number, 'number')
        if not isinstance(changes, dict) or not changes:
            raise ValueError('a contact update sets name, notes, autoContext, or several of them')
        unknown = set(changes) - set(FIELDS)
        if unknown:
            raise ValueError('unknown contact fields: ' + ', '.join(sorted(unknown)))
        reject_secrets(changes, 'contact')
        values = {}
        for key in ('name', 'notes'):
            if key in changes:
                values[key] = _text(changes[key], key)
        if 'autoContext' in changes:
            if not isinstance(changes['autoContext'], bool):
                raise ValueError('autoContext must be true or false')
            values['autoContext'] = changes['autoContext']
        moment = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        stamp = moment.isoformat(timespec='seconds').replace('+00:00', 'Z')
        with self._lock:
            entries = self.entries()
            current = next((entry for entry in entries if entry.get('number') == number), None)
            entry = dict(current or {'number': number})
            entry.update(values)
            # Keep only what says something; an entry with nothing left is dropped.
            entry = {key: value for key, value in entry.items()
                     if key != 'updatedAt' and value not in ('', False, None)}
            entry['number'] = number
            others = [item for item in entries if item.get('number') != number]
            if len(entry) > 1:
                if current is None and len(others) >= MAX_CONTACTS:
                    raise ValueError(f'at most {MAX_CONTACTS} contacts can be saved')
                entry['updatedAt'] = stamp
                others.append(entry)
            self._save(others)
            return entry

    def forget(self, number):
        """Drop what was saved for a number. Its call records stay. True if anything was saved."""
        number = normalize_phone(number, 'number')
        with self._lock:
            entries = self.entries()
            kept = [entry for entry in entries if entry.get('number') != number]
            if len(kept) == len(entries):
                return False
            self._save(kept)
            return True
