"""Versioned meeting session records."""
from dataclasses import dataclass
import re

from context_handoff import ContextHandoff
from meeting_urls import platform_for_url
from schema_validation import (
    omit_none, optional_bool, optional_field, reject_unknown_fields,
    require_enum, require_field, require_mapping, require_meeting_id,
    require_string, require_timestamp,
)


MEETING_PLATFORMS = ('zoom', 'teams', 'meet')
MEETING_STATES = ('joining', 'waiting_for_admission', 'live', 'ended')
MEETING_SESSION_FIELDS = (
    'id', 'platform', 'meetingUrl', 'context', 'onBehalfOf', 'voice', 'state', 'startedAt',
    'cameraEnabled', 'cameraState', 'visualState', 'degradedReason',
)
# Fields older snapshots stored for coding-agent sessions; ignored when read back.
LEGACY_SESSION_FIELDS = ('agentSession', 'permissions')
CAMERA_STATES = ('off', 'starting', 'on', 'blocked', 'degraded')
VISUAL_STATES = (
    'joining', 'listening', 'working', 'speaking', 'finalizing', 'needs_attention', 'ended',
)
DEGRADED_REASONS = ('platform_blocked', 'unsupported', 'unconfirmed', 'policy')
MAX_ON_BEHALF_OF = 120
VOICE_NAME = re.compile(r'^[a-z][a-z0-9_-]{1,31}$')


def _optional_enum(value, name, allowed):
    if value is None:
        return None
    return require_enum(value, name, allowed)


def optional_on_behalf_of(value):
    """The owner's name spoken in the meeting's AI disclosure, or None."""
    if value is None:
        return None
    text = ' '.join(require_string(value, 'onBehalfOf', max_length=MAX_ON_BEHALF_OF).split())
    return text or None


def optional_voice(value, environ=None):
    """A GPT-Live voice name (see /v1/voices), or None for the default voice."""
    if value is None:
        return None
    from call_brief import available_voices
    name = require_string(value, 'voice', max_length=32)
    voices = available_voices(environ)
    if not VOICE_NAME.fullmatch(name) or name not in voices:
        raise ValueError('voice must be one of: ' + ', '.join(voices))
    return name


def upgrade_legacy_session(payload):
    """Map a snapshot written before coding-agent sessions were removed onto today's fields."""
    payload = dict(payload)
    agent = payload.pop('agentSession', None)
    payload.pop('permissions', None)
    context = payload.get('context')
    if isinstance(context, dict) and 'git' in context:
        payload['context'] = {key: value for key, value in context.items() if key != 'git'}
    metadata = agent.get('metadata') if isinstance(agent, dict) else None
    if isinstance(metadata, dict):
        if metadata.get('onBehalfOf') and 'onBehalfOf' not in payload:
            payload['onBehalfOf'] = metadata['onBehalfOf']
        if metadata.get('voice') and 'voice' not in payload:
            payload['voice'] = metadata['voice']
    return payload


@dataclass(frozen=True)
class MeetingSession:
    id: str
    platform: str
    meeting_url: str
    context: ContextHandoff
    state: str
    started_at: str
    on_behalf_of: str = None
    voice: str = None
    camera_enabled: bool = None
    camera_state: str = None
    visual_state: str = None
    degraded_reason: str = None

    def to_dict(self):
        return omit_none({
            'id': self.id,
            'platform': self.platform,
            'meetingUrl': self.meeting_url,
            'context': self.context.to_dict(),
            'onBehalfOf': self.on_behalf_of,
            'voice': self.voice,
            'state': self.state,
            'startedAt': self.started_at,
            'cameraEnabled': self.camera_enabled,
            'cameraState': self.camera_state,
            'visualState': self.visual_state,
            'degradedReason': self.degraded_reason,
        })

    @classmethod
    def from_dict(cls, data):
        payload = require_mapping(data, 'meetingSession')
        reject_unknown_fields(payload, MEETING_SESSION_FIELDS, 'meetingSession')
        platform = require_enum(require_field(payload, 'platform', 'meetingSession'),
                                'platform', MEETING_PLATFORMS)
        meeting_url = require_string(require_field(payload, 'meetingUrl', 'meetingSession'),
                                     'meetingUrl', max_length=2048)
        detected = platform_for_url(meeting_url)
        if detected != platform:
            raise ValueError('meetingUrl does not match platform')
        voice = optional_field(payload, 'voice')
        return cls(
            id=require_meeting_id(require_field(payload, 'id', 'meetingSession'), 'id'),
            platform=platform,
            meeting_url=meeting_url,
            context=ContextHandoff.from_dict(require_field(payload, 'context', 'meetingSession')),
            state=require_enum(require_field(payload, 'state', 'meetingSession'),
                               'state', MEETING_STATES),
            started_at=require_timestamp(require_field(payload, 'startedAt', 'meetingSession'),
                                         'startedAt'),
            on_behalf_of=optional_on_behalf_of(optional_field(payload, 'onBehalfOf')),
            # Stored voices were validated at create time; later reads keep them as written.
            voice=None if voice is None else require_string(voice, 'voice', max_length=32),
            camera_enabled=optional_bool(optional_field(payload, 'cameraEnabled'), 'cameraEnabled'),
            camera_state=_optional_enum(optional_field(payload, 'cameraState'), 'cameraState',
                                        CAMERA_STATES),
            visual_state=_optional_enum(optional_field(payload, 'visualState'), 'visualState',
                                        VISUAL_STATES),
            degraded_reason=_optional_enum(optional_field(payload, 'degradedReason'),
                                           'degradedReason', DEGRADED_REASONS),
        )
