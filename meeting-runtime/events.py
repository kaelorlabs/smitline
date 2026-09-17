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
    'agent_session.locked',
    'agent_session.released',
    'transcript.delta',
    'delegation.started',
    'delegation.progress',
    'delegation.completed',
    'delegation.cancelled',
    'approval.required',
    'approval.approved',
    'approval.denied',
    'approval.expired',
    'approval.cancelled',
    'artifact.created',
    'workspace.action.planned',
    'workspace.action.started',
    'workspace.action.completed',
    'workspace.action.failed',
    'workspace.action.cancelled',
    'handoff.ready',
    'handoff.append_failed',
    'presence.updated',
)
PAYLOAD_FIELDS = {
    'meeting.joining': (),
    'meeting.waiting_for_admission': (),
    'meeting.live': (),
    'meeting.ended': ('reason',),
    'agent_session.locked': ('sessionId',),
    'agent_session.released': ('sessionId',),
    'transcript.delta': ('entry',),
    'delegation.started': ('delegationId',),
    'delegation.progress': ('delegationId', 'message'),
    'delegation.completed': ('delegationId',),
    'delegation.cancelled': ('delegationId', 'reason'),
    'approval.required': ('request',),
    'approval.approved': ('decision',),
    'approval.denied': ('decision',),
    'approval.expired': ('approvalId',),
    'approval.cancelled': ('approvalId', 'reason'),
    'artifact.created': ('artifact',),
    'workspace.action.planned': ('plan',),
    'workspace.action.started': ('planId',),
    'workspace.action.completed': ('result',),
    'workspace.action.failed': ('result',),
    'workspace.action.cancelled': ('planId', 'reason'),
    'handoff.ready': ('handoff',),
    'handoff.append_failed': ('reason', 'handoffId', 'retryable'),
    'presence.updated': ('cameraEnabled', 'cameraState', 'visualState', 'degradedReason'),
}
ENVELOPE_FIELDS = ('version', 'id', 'meetingId', 'timestamp', 'type')
TRANSCRIPT_SOURCES = ('input', 'output', 'platform')
APPROVAL_PERMISSIONS = ('workspace', 'commands', 'edits', 'network', 'commits', 'pushes')
TRANSCRIPT_ENTRY_FIELDS = (
    'id', 'text', 'source', 'speaker', 'startOffsetMs', 'endOffsetMs', 'muted', 'delegationId',
)
APPROVAL_FIELDS = (
    'id', 'permission', 'summary', 'createdAt', 'action', 'category', 'scope',
    'delegationId', 'expiresAt', 'status',
)
APPROVAL_STATUSES = ('pending', 'approved', 'denied', 'expired', 'cancelled')
ARTIFACT_FIELDS = ('id', 'kind', 'path', 'createdAt', 'mediaType', 'description')


@dataclass(frozen=True)
class TranscriptEntry:
    id: str
    text: str
    source: str
    speaker: str = None
    start_offset_ms: int = None
    end_offset_ms: int = None
    muted: bool = None
    delegation_id: str = None

    def to_dict(self):
        return omit_none({
            'id': self.id,
            'text': self.text,
            'source': self.source,
            'speaker': self.speaker,
            'startOffsetMs': self.start_offset_ms,
            'endOffsetMs': self.end_offset_ms,
            'muted': self.muted,
            'delegationId': self.delegation_id,
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
            delegation_id=(
                None if optional_field(payload, 'delegationId') is None
                else require_id(payload['delegationId'], 'entry.delegationId')
            ),
        )


@dataclass(frozen=True)
class ApprovalRequest:
    id: str
    permission: str
    summary: str
    created_at: str
    action: str = None
    category: str = None
    scope: dict = None
    delegation_id: str = None
    expires_at: str = None
    status: str = None

    def to_dict(self):
        return omit_none({
            'id': self.id,
            'permission': self.permission,
            'summary': self.summary,
            'createdAt': self.created_at,
            'action': self.action,
            'category': self.category or self.permission,
            'scope': None if not self.scope else dict(self.scope),
            'delegationId': self.delegation_id,
            'expiresAt': self.expires_at,
            'status': self.status,
        })

    @classmethod
    def from_dict(cls, data):
        payload = require_mapping(data, 'request')
        reject_unknown_fields(payload, APPROVAL_FIELDS, 'request')
        permission = require_enum(require_field(payload, 'permission', 'request'),
                                  'request.permission', APPROVAL_PERMISSIONS)
        category = permission if optional_field(payload, 'category') is None else require_enum(
            payload['category'], 'request.category', APPROVAL_PERMISSIONS)
        status = None if optional_field(payload, 'status') is None else require_enum(
            payload['status'], 'request.status', APPROVAL_STATUSES)
        scope = optional_field(payload, 'scope')
        if scope is not None and not isinstance(scope, dict):
            raise ValueError('request.scope must be an object')
        lowered = require_string(require_field(payload, 'summary', 'request'),
                                 'request.summary', max_length=240).lower()
        if any(token in lowered for token in (
                'password', 'api key', 'bearer', 'transcript', 'diff --git')):
            raise ValueError('approval summary cannot include private or command text')
        return cls(
            id=require_id(require_field(payload, 'id', 'request'), 'request.id'),
            permission=permission,
            summary=require_string(payload['summary'], 'request.summary', max_length=240),
            created_at=require_timestamp(require_field(payload, 'createdAt', 'request'),
                                         'request.createdAt'),
            action=optional_string(optional_field(payload, 'action'), 'request.action',
                                   max_length=64),
            category=category,
            scope=None if scope is None else dict(scope),
            delegation_id=(
                None if optional_field(payload, 'delegationId') is None
                else require_id(payload['delegationId'], 'request.delegationId')
            ),
            expires_at=None if optional_field(payload, 'expiresAt') is None else require_timestamp(
                payload['expiresAt'], 'request.expiresAt'),
            status=status,
        )


@dataclass(frozen=True)
class ApprovalDecision:
    approval_id: str
    decision: str
    decided_at: str

    def to_dict(self):
        return {
            'approvalId': self.approval_id,
            'decision': self.decision,
            'decidedAt': self.decided_at,
        }

    @classmethod
    def from_dict(cls, data):
        payload = require_mapping(data, 'decision')
        reject_unknown_fields(payload, ('approvalId', 'decision', 'decidedAt'), 'decision')
        return cls(
            approval_id=require_id(require_field(payload, 'approvalId', 'decision'), 'approvalId'),
            decision=require_enum(require_field(payload, 'decision', 'decision'), 'decision',
                                  ('approved', 'denied')),
            decided_at=require_timestamp(require_field(payload, 'decidedAt', 'decision'),
                                         'decidedAt'),
        )


@dataclass(frozen=True)
class Artifact:
    id: str
    kind: str
    path: str
    created_at: str
    media_type: str = None
    description: str = None

    def to_dict(self):
        return omit_none({
            'id': self.id,
            'kind': self.kind,
            'path': self.path,
            'createdAt': self.created_at,
            'mediaType': self.media_type,
            'description': self.description,
        })

    @classmethod
    def from_dict(cls, data):
        payload = require_mapping(data, 'artifact')
        reject_unknown_fields(payload, ARTIFACT_FIELDS, 'artifact')
        return cls(
            id=require_id(require_field(payload, 'id', 'artifact'), 'artifact.id'),
            kind=require_string(require_field(payload, 'kind', 'artifact'), 'artifact.kind',
                                max_length=64),
            path=require_string(require_field(payload, 'path', 'artifact'), 'artifact.path',
                                max_length=4096),
            created_at=require_timestamp(require_field(payload, 'createdAt', 'artifact'),
                                         'artifact.createdAt'),
            media_type=optional_string(optional_field(payload, 'mediaType'), 'artifact.mediaType',
                                       max_length=128),
            description=optional_string(optional_field(payload, 'description'),
                                        'artifact.description', allow_newlines=True),
        )


def _payload_from_dict(event_type, payload):
    if event_type == 'meeting.ended':
        return {'reason': require_string(require_field(payload, 'reason', 'event'), 'reason')}
    if event_type in ('agent_session.locked', 'agent_session.released'):
        return {'sessionId': require_id(require_field(payload, 'sessionId', 'event'),
                                        'sessionId', max_length=256)}
    if event_type == 'transcript.delta':
        return {'entry': TranscriptEntry.from_dict(require_field(payload, 'entry', 'event'))}
    if event_type in ('delegation.started', 'delegation.completed'):
        return {'delegationId': require_id(require_field(payload, 'delegationId', 'event'),
                                           'delegationId')}
    if event_type == 'delegation.progress':
        return {
            'delegationId': require_id(require_field(payload, 'delegationId', 'event'),
                                       'delegationId'),
            'message': require_string(require_field(payload, 'message', 'event'), 'message',
                                      allow_newlines=True),
        }
    if event_type == 'delegation.cancelled':
        return {
            'delegationId': require_id(require_field(payload, 'delegationId', 'event'),
                                       'delegationId'),
            'reason': require_string(require_field(payload, 'reason', 'event'), 'reason'),
        }
    if event_type == 'approval.required':
        return {'request': ApprovalRequest.from_dict(require_field(payload, 'request', 'event'))}
    if event_type in ('approval.approved', 'approval.denied'):
        return {'decision': ApprovalDecision.from_dict(require_field(payload, 'decision', 'event'))}
    if event_type == 'approval.expired':
        return {'approvalId': require_id(require_field(payload, 'approvalId', 'event'), 'approvalId')}
    if event_type == 'approval.cancelled':
        return {
            'approvalId': require_id(require_field(payload, 'approvalId', 'event'), 'approvalId'),
            'reason': require_string(require_field(payload, 'reason', 'event'), 'reason',
                                     max_length=64),
        }
    if event_type == 'artifact.created':
        return {'artifact': Artifact.from_dict(require_field(payload, 'artifact', 'event'))}
    if event_type == 'workspace.action.planned':
        from workspace_actions import WorkspaceActionPlan
        return {'plan': WorkspaceActionPlan.from_dict(require_field(payload, 'plan', 'event'))}
    if event_type == 'workspace.action.started':
        return {'planId': require_id(require_field(payload, 'planId', 'event'), 'planId')}
    if event_type in ('workspace.action.completed', 'workspace.action.failed'):
        from workspace_actions import WorkspaceActionResult
        return {'result': WorkspaceActionResult.from_dict(require_field(payload, 'result', 'event'))}
    if event_type == 'workspace.action.cancelled':
        return {
            'planId': require_id(require_field(payload, 'planId', 'event'), 'planId'),
            'reason': require_string(require_field(payload, 'reason', 'event'), 'reason',
                                     max_length=64),
        }
    if event_type == 'handoff.ready':
        return {'handoff': MeetingHandoff.from_dict(require_field(payload, 'handoff', 'event'))}
    if event_type == 'handoff.append_failed':
        return {
            'reason': require_string(require_field(payload, 'reason', 'event'), 'reason'),
            'handoffId': require_id(require_field(payload, 'handoffId', 'event'), 'handoffId',
                                    max_length=256),
            'retryable': require_bool(require_field(payload, 'retryable', 'event'), 'retryable'),
        }
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
