"""Shared process-boundary validation for versioned runtime schemas."""
from datetime import datetime
from pathlib import Path
import re


SCHEMA_VERSION = 1
MEETING_ID_PATTERN = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$')
SECRET_FIELD_SUFFIXES = (
    'apikey', 'token', 'secret', 'password', 'passwd', 'cookie', 'cookies',
    'authorization', 'credential', 'credentials', 'privatekey', 'bearer',
)


def normalized_field_name(name):
    return ''.join(character for character in name.lower() if character.isalnum())


def field_name_is_secret(name):
    if not isinstance(name, str):
        return False
    normalized = normalized_field_name(name)
    if not normalized:
        return False
    return any(normalized == suffix or normalized.endswith(suffix) for suffix in SECRET_FIELD_SUFFIXES)


def reject_secrets(value, location='payload'):
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError(f'{location} keys must be strings')
            if field_name_is_secret(key):
                raise ValueError(f'Refused to serialize secret field {key!r}')
            reject_secrets(item, f'{location}.{key}')
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            reject_secrets(item, f'{location}[{index}]')


def require_mapping(value, name):
    if not isinstance(value, dict):
        raise ValueError(f'{name} must be an object')
    reject_secrets(value, name)
    return value


def reject_unknown_fields(data, allowed, name):
    extra = set(data) - set(allowed)
    if extra:
        raise ValueError(f'{name} has unknown fields: {", ".join(sorted(extra))}')


def require_field(data, key, name):
    if key not in data:
        raise ValueError(f'{name} is missing {key}')
    return data[key]


def optional_field(data, key):
    return data[key] if key in data else None


def require_string(value, name, *, allow_empty=False, allow_newlines=False, max_length=8000):
    if not isinstance(value, str):
        raise ValueError(f'{name} must be a string')
    allowed_controls = '\n\t' if allow_newlines else ''
    if any(ord(character) < 32 and character not in allowed_controls for character in value):
        raise ValueError(f'{name} must be printable')
    if len(value) > max_length:
        raise ValueError(f'{name} exceeds {max_length} characters')
    if not allow_empty and not value.strip():
        raise ValueError(f'{name} must be non-empty')
    return value


def require_id(value, name, max_length=128):
    text = require_string(value, name, max_length=max_length)
    if any(ord(character) < 33 or ord(character) > 126 for character in text):
        raise ValueError(f'{name} must be visible ASCII without spaces')
    if '/' in text or '\\' in text or text in ('.', '..'):
        raise ValueError(f'{name} must not contain a path')
    return text


def require_meeting_id(value, name='meetingId'):
    text = require_id(value, name)
    if not MEETING_ID_PATTERN.fullmatch(text):
        raise ValueError(f'{name} must be a filesystem-safe identifier')
    return text


def require_enum(value, name, allowed):
    if value not in allowed:
        raise ValueError(f'{name} must be one of: {", ".join(allowed)}')
    return value


def require_bool(value, name):
    if not isinstance(value, bool):
        raise ValueError(f'{name} must be a boolean')
    return value


def require_int(value, name, *, min_value=None, max_value=None):
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f'{name} must be an integer')
    if min_value is not None and value < min_value:
        raise ValueError(f'{name} must be >= {min_value}')
    if max_value is not None and value > max_value:
        raise ValueError(f'{name} must be <= {max_value}')
    return value


def require_version(value, name='version'):
    version = require_int(value, name, min_value=SCHEMA_VERSION, max_value=SCHEMA_VERSION)
    return version


def require_timestamp(value, name):
    text = require_string(value, name, max_length=64)
    candidate = text[:-1] + '+00:00' if text.endswith('Z') else text
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as error:
        raise ValueError(f'{name} must be an ISO-8601 timestamp') from error
    if parsed.tzinfo is None:
        raise ValueError(f'{name} must include a timezone')
    return text


def require_workspace(value, name='workspace'):
    text = require_string(value, name, max_length=4096)
    if not Path(text).is_absolute():
        raise ValueError(f'{name} must be an absolute path')
    return text


def require_string_list(value, name, *, max_items=200, item_max_length=8000, allow_empty_items=False):
    if not isinstance(value, list):
        raise ValueError(f'{name} must be an array')
    if len(value) > max_items:
        raise ValueError(f'{name} exceeds {max_items} items')
    return tuple(require_string(item, f'{name}[{index}]', allow_empty=allow_empty_items,
                                max_length=item_max_length)
                 for index, item in enumerate(value))


def require_object_list(value, name, parser, *, max_items=200):
    if not isinstance(value, list):
        raise ValueError(f'{name} must be an array')
    if len(value) > max_items:
        raise ValueError(f'{name} exceeds {max_items} items')
    return tuple(parser(item) for item in value)


def optional_string(value, name, **kwargs):
    if value is None:
        return None
    return require_string(value, name, **kwargs)


def optional_bool(value, name):
    if value is None:
        return None
    return require_bool(value, name)


def optional_int(value, name, **kwargs):
    if value is None:
        return None
    return require_int(value, name, **kwargs)


def omit_none(data):
    return {key: value for key, value in data.items() if value is not None}
