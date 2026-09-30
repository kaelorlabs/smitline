"""Validated ColleagueEvent envelope and event vocabulary."""
from dataclasses import dataclass
from types import MappingProxyType

from agent_sessions import CAMERA_STATES, DEGRADED_REASONS, VISUAL_STATES
from meeting_handoff import MeetingHandoff
from schema_validation import (
    omit_none, optional_bool, optional_field, optional_int, optional_string,
    reject_unknown_fields, require_bool, require_enum, require_field, require_id, require_mapping,
    require_meeting_id, require_string, require_timestamp, require_version,
)


EVENT_VERSION = 1
EVENT_TYPES = (
    'meeting.joining',
    'meeting.waiting_for_admission',
    'meeting.live',
    'meeting.ended',
    'transcript.delta',
    'handoff.ready',
    'presence.updated',
)
PAYLOAD_FIELDS = {
    'meeting.joining': (),
    'meeting.waiting_for_admission': (),
    'meeting.live': (),
    'meeting.ended': ('reason',),
    'transcript.delta': ('entry',),
    'handoff.ready': ('handoff',),
    'presence.updated': ('cameraEnabled', 'cameraState', 'visualState', 'degradedReason'),
}
ENVELOPE_FIELDS = ('version', 'id', 'meetingId', 'timestamp', 'type')
TRANSCRIPT_SOURCES = ('input', 'output', 'platform')
TRANSCRIPT_ENTRY_FIELDS = (
    'id', 'text', 'source', 'speaker', 'startOffsetMs', 'endOffsetMs', 'muted',
)


@dataclass(frozen=True)
class TranscriptEntry:
    id: str
    text: str
    source: str
    speaker: str = None
    start_offset_ms: int = None
    end_offset_ms: int = None
    muted: bool = None

    def to_dict(self):
        return omit_none({
            'id': self.id,
            'text': self.text,
            'source': self.source,
            'speaker': self.speaker,
            'startOffsetMs': self.start_offset_ms,
            'endOffsetMs': self.end_offset_ms,
            'muted': self.muted,
        })

    @classmethod
    def from_dict(cls, data):
        payload = require_mapping(data, 'entry')
        reject_unknown_fields(payload, TRANSCRIPT_ENTRY_FIELDS, 'entry')
        return cls(
            id=require_id(require_field(payload, 'id', 'entry'), 'entry.id'),
            text=require_string(require_field(payload, 'text', 'entry'), 'entry.text',
                                allow_empty=True, allow_newlines=True),
            source=require_enum(require_field(payload, 'source', 'entry'), 'entry.source',
                                TRANSCRIPT_SOURCES),
            speaker=optional_string(optional_field(payload, 'speaker'), 'entry.speaker',
                                    max_length=256),
            start_offset_ms=optional_int(optional_field(payload, 'startOffsetMs'),
                                         'entry.startOffsetMs', min_value=0),
            end_offset_ms=optional_int(optional_field(payload, 'endOffsetMs'),
                                       'entry.endOffsetMs', min_value=0),
            muted=optional_bool(optional_field(payload, 'muted'), 'entry.muted'),
        )


def _payload_from_dict(event_type, payload):
    if event_type == 'meeting.ended':
        return {'reason': require_string(require_field(payload, 'reason', 'event'), 'reason')}
    if event_type == 'transcript.delta':
        return {'entry': TranscriptEntry.from_dict(require_field(payload, 'entry', 'event'))}
    if event_type == 'handoff.ready':
        return {'handoff': MeetingHandoff.from_dict(require_field(payload, 'handoff', 'event'))}
    if event_type == 'presence.updated':
        return omit_none({
            'cameraEnabled': require_bool(require_field(payload, 'cameraEnabled', 'event'),
                                          'cameraEnabled'),
            'cameraState': require_enum(require_field(payload, 'cameraState', 'event'),
                                        'cameraState', CAMERA_STATES),
            'visualState': require_enum(require_field(payload, 'visualState', 'event'),
                                        'visualState', VISUAL_STATES),
            'degradedReason': None if optional_field(payload, 'degradedReason') is None else (
                require_enum(payload['degradedReason'], 'degradedReason', DEGRADED_REASONS)
            ),
        })
    return {}


def _serialize_payload(fields):
    serialized = {}
    for key, value in fields.items():
        serialized[key] = value.to_dict() if hasattr(value, 'to_dict') else value
    return serialized


@dataclass(frozen=True)
class ColleagueEvent:
    version: int
    id: str
    meeting_id: str
    timestamp: str
    type: str
    payload: dict

    def to_dict(self):
        envelope = {
            'version': self.version,
            'id': self.id,
            'meetingId': self.meeting_id,
            'timestamp': self.timestamp,
            'type': self.type,
        }
        envelope.update(_serialize_payload(self.payload))
        return envelope

    @classmethod
    def from_dict(cls, data):
        raw = require_mapping(data, 'event')
        event_type = require_enum(require_field(raw, 'type', 'event'), 'type', EVENT_TYPES)
        allowed = set(ENVELOPE_FIELDS) | set(PAYLOAD_FIELDS[event_type])
        reject_unknown_fields(raw, allowed, 'event')
        payload = _payload_from_dict(event_type, raw)
        return cls(
            version=require_version(require_field(raw, 'version', 'event')),
            id=require_id(require_field(raw, 'id', 'event'), 'id'),
            meeting_id=require_meeting_id(require_field(raw, 'meetingId', 'event')),
            timestamp=require_timestamp(require_field(raw, 'timestamp', 'event'), 'timestamp'),
            type=event_type,
            payload=MappingProxyType(payload),
        )
