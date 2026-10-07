"""Smitline visual presence: states, avatar validation, and camera policy."""
from pathlib import Path
import base64
import binascii
import json
import re
from urllib.parse import quote, unquote

from schema_validation import (
    omit_none, optional_field, reject_unknown_fields, require_bool, require_mapping,
    require_string,
)


VISUAL_STATES = (
    'joining', 'listening', 'working', 'speaking', 'finalizing', 'needs_attention', 'ended',
)
CAMERA_STATES = ('off', 'starting', 'on', 'blocked', 'degraded')
DEGRADED_REASONS = ('platform_blocked', 'unsupported', 'unconfirmed', 'policy')
CAMERA_CREATE_FIELDS = ('enabled', 'defaultOn', 'avatarDataUri', 'avatarPath')
AVATAR_MAX_BYTES = 80 * 1024
AVATAR_MEDIA_TYPES = {
    'image/png': ('.png',),
    'image/jpeg': ('.jpg', '.jpeg'),
    'image/webp': ('.webp',),
    'image/svg+xml': ('.svg',),
}
_SVG_UNSAFE = re.compile(
    r'<script|javascript:|on\w+\s*=|<foreignobject', re.IGNORECASE)
JOINING_STAGES = frozenset({
    'starting', 'opening_meeting', 'joining', 'waiting_for_admission', 'admitted',
    'connecting_audio',
})
ATTENTION_STAGES = frozenset({
    'needs_attention', 'authentication_required', 'api_error',
})
ENDED_STAGES = frozenset({
    'meeting_ended', 'finished', 'closed_without_final_usage', 'ended',
})
PRIVATE_RENDER_TOKENS = (
    'transcript', 'prompt', 'filename', 'tool result', 'customer', 'password',
    'api key', 'secret', 'checking project', 'preparing handoff',
)

SMITLINE_MARK_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 128 128" role="img" '
    'aria-label="Smitline">'
    '<rect width="128" height="128" rx="28" fill="#30323b"/>'
    '<rect x="34" y="44" width="12" height="40" rx="4" fill="#fff"/>'
    '<rect x="58" y="28" width="12" height="72" rx="4" fill="#fff"/>'
    '<rect x="82" y="44" width="12" height="40" rx="4" fill="#fff"/>'
    '</svg>'
)
DEFAULT_AVATAR_DATA_URI = 'data:image/svg+xml,' + quote(SMITLINE_MARK_SVG, safe='')


def default_avatar_data_uri():
    return DEFAULT_AVATAR_DATA_URI


def map_visual_state(state=None, **overrides):
    payload = dict(state or {})
    payload.update(overrides)
    stage = str(payload.get('stage') or 'joining')
    floor = str(payload.get('floorState') or 'listening')
    backend = str(payload.get('backend_status') or 'idle')
    if payload.get('finalizing') or stage == 'finalizing':
        if stage in ATTENTION_STAGES:
            return 'needs_attention'
        if stage in ENDED_STAGES:
            return 'ended'
        return 'finalizing'
    if stage in ENDED_STAGES:
        return 'ended'
    if stage in ATTENTION_STAGES:
        return 'needs_attention'
    if floor == 'speaking':
        return 'speaking'
    if backend == 'working':
        return 'working'
    if stage in JOINING_STAGES:
        return 'joining'
    if stage == 'live':
        return 'listening'
    return 'joining'


def rendered_state_is_private(text):
    lowered = str(text or '').lower()
    return any(token in lowered for token in PRIVATE_RENDER_TOKENS)


def _sniff_media_type(data):
    if data.startswith(b'\x89PNG\r\n\x1a\n'):
        return 'image/png'
    if data.startswith(b'\xff\xd8\xff'):
        return 'image/jpeg'
    if len(data) >= 12 and data.startswith(b'RIFF') and data[8:12] == b'WEBP':
        return 'image/webp'
    head = data.lstrip()[:200].lower()
    if head.startswith(b'<svg') or (head.startswith(b'<?xml') and b'<svg' in data[:800].lower()):
        return 'image/svg+xml'
    return None


def _reject_unsafe_svg(data):
    text = data.decode('utf-8', errors='replace')
    if _SVG_UNSAFE.search(text):
        raise ValueError('avatar SVG contains disallowed script or event handlers')
    return text


def encode_avatar_bytes(data, media_type=None):
    if not isinstance(data, (bytes, bytearray)):
        raise ValueError('avatar must be bytes')
    payload = bytes(data)
    if not payload:
        raise ValueError('avatar is empty')
    if len(payload) > AVATAR_MAX_BYTES:
        raise ValueError(f'avatar exceeds {AVATAR_MAX_BYTES} bytes')
    sniffed = _sniff_media_type(payload)
    if media_type:
        media_type = require_string(media_type, 'avatar media type', max_length=64)
        if media_type not in AVATAR_MEDIA_TYPES:
            raise ValueError('avatar media type is not allowed')
        if sniffed and sniffed != media_type:
            raise ValueError('avatar bytes do not match the declared media type')
    else:
        media_type = sniffed
    if media_type not in AVATAR_MEDIA_TYPES:
        raise ValueError('avatar must be a PNG, JPEG, WebP, or SVG image')
    if media_type == 'image/svg+xml':
        _reject_unsafe_svg(payload)
        return 'data:image/svg+xml,' + quote(payload.decode('utf-8'), safe='')
    encoded = base64.b64encode(payload).decode('ascii')
    return f'data:{media_type};base64,{encoded}'


def parse_avatar_data_uri(value):
    text = require_string(value, 'avatarDataUri', max_length=AVATAR_MAX_BYTES * 2 + 64)
    if not text.startswith('data:'):
        raise ValueError('avatarDataUri must be a data URI')
    header, _, body = text.partition(',')
    media = header[5:]
    if media.endswith(';base64'):
        media_type = media[:-7]
        try:
            data = base64.b64decode(body, validate=True)
        except (binascii.Error, ValueError) as error:
            raise ValueError('avatarDataUri is not valid base64') from error
        return encode_avatar_bytes(data, media_type)
    media_type = media or 'image/svg+xml'
    data = unquote(body).encode('utf-8')
    return encode_avatar_bytes(data, media_type)


def load_avatar_from_path(path):
    location = Path(path)
    if not location.is_absolute():
        raise ValueError('avatar path must be absolute')
    if location.is_symlink():
        raise ValueError('avatar path must not be a symlink')
    if not location.is_file():
        raise ValueError('avatar path must be a local image file')
    suffix = location.suffix.lower()
    allowed_suffixes = {ext for values in AVATAR_MEDIA_TYPES.values() for ext in values}
    if suffix not in allowed_suffixes:
        raise ValueError('avatar path must be a PNG, JPEG, WebP, or SVG image')
    return encode_avatar_bytes(location.read_bytes())


def parse_camera_settings(payload=None):
    if payload is None:
        return {
            'enabled': True,
            'defaultOn': True,
            'avatarDataUri': None,
        }
    data = require_mapping(payload, 'camera')
    reject_unknown_fields(data, CAMERA_CREATE_FIELDS, 'camera')
    enabled = True if optional_field(data, 'enabled') is None else require_bool(
        data['enabled'], 'camera.enabled')
    default_on = True if optional_field(data, 'defaultOn') is None else require_bool(
        data['defaultOn'], 'camera.defaultOn')
    avatar = None
    raw_uri = optional_field(data, 'avatarDataUri')
    raw_path = optional_field(data, 'avatarPath')
    if raw_uri is not None and raw_path is not None:
        raise ValueError('camera may include avatarDataUri or avatarPath, not both')
    if raw_uri is not None:
        avatar = parse_avatar_data_uri(raw_uri)
    elif raw_path is not None:
        avatar = load_avatar_from_path(require_string(raw_path, 'camera.avatarPath', max_length=4096))
    return {
        'enabled': enabled,
        'defaultOn': default_on,
        'avatarDataUri': avatar,
    }


def camera_state_payload(*, enabled=True, default_on=True, avatar_data_uri=None):
    return omit_none({
        'cameraEnabled': bool(enabled),
        'cameraDefaultOn': bool(default_on),
        'cameraAvatarDataUri': avatar_data_uri,
    })


def presence_public_fields(state):
    payload = {
        'cameraEnabled': bool(state.get('cameraEnabled', True)),
        'cameraState': state.get('cameraState') or ('off' if not state.get('cameraEnabled', True) else 'starting'),
        'visualState': state.get('visualState') or map_visual_state(state),
        'degradedReason': state.get('degradedReason'),
    }
    if payload['cameraState'] not in CAMERA_STATES:
        payload['cameraState'] = 'degraded'
        payload['degradedReason'] = payload['degradedReason'] or 'unconfirmed'
    if payload['visualState'] not in VISUAL_STATES:
        payload['visualState'] = 'joining'
    if payload['degradedReason'] not in DEGRADED_REASONS:
        payload['degradedReason'] = None
    if rendered_state_is_private(json.dumps(payload)):
        raise ValueError('visual presence payload contained private text')
    return omit_none(payload)


async def apply_platform_camera(adapter, *, enabled, default_on, state):
    """Turn the platform camera on after admission. Never raises; degrades to audio-only."""
    state['cameraEnabled'] = bool(enabled)
    state.pop('degradedReason', None)
    if not enabled:
        state['cameraState'] = 'off'
        return 'off'
    if not default_on:
        state['cameraState'] = 'off'
        return 'off'
    capabilities = getattr(adapter, 'capabilities', None)
    if capabilities is not None and hasattr(capabilities, 'camera') and not capabilities.camera:
        state['cameraState'] = 'degraded'
        state['degradedReason'] = 'unsupported'
        return 'degraded'
    state['cameraState'] = 'starting'
    try:
        await adapter.enable_camera()
        actual = await adapter.get_camera_state()
    except Exception:
        state['cameraState'] = 'degraded'
        state['degradedReason'] = 'platform_blocked'
        return 'degraded'
    if actual in ('on', 'open'):
        state['cameraState'] = 'on'
        return 'on'
    if actual == 'blocked':
        state['cameraState'] = 'blocked'
        state['degradedReason'] = 'platform_blocked'
        return 'blocked'
    if actual == 'off':
        state['cameraState'] = 'degraded'
        state['degradedReason'] = 'platform_blocked'
        return 'degraded'
    state['cameraState'] = 'degraded'
    state['degradedReason'] = 'unconfirmed'
    return 'degraded'


class Presence:
    """Deterministic visual-state mapper driven by lifecycle, work, and playback."""

    def __init__(self, state, camera_feed=None):
        self.state = state
        self.camera_feed = camera_feed
        self._last_visual = None
        state.setdefault('visualState', 'joining')
        state.setdefault('cameraEnabled', True)
        state.setdefault('cameraState', 'off' if not state.get('cameraEnabled', True) else 'starting')

    def sync(self):
        visual = map_visual_state(self.state)
        self.state['visualState'] = visual
        feed = self.camera_feed
        if visual != self._last_visual and feed is not None:
            self._last_visual = visual
            setter = getattr(feed, 'set_visual_state', None)
            if setter is not None:
                setter(visual)
        else:
            self._last_visual = visual
        return visual
