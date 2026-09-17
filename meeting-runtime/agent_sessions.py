"""Versioned coding-agent session, permission, and meeting session records."""
from dataclasses import dataclass
from types import MappingProxyType

from context_handoff import ContextHandoff
from meeting_urls import platform_for_url
from schema_validation import (
    omit_none, optional_bool, optional_field, reject_unknown_fields,
    require_enum, require_field, require_id, require_mapping, require_meeting_id,
    require_string, require_timestamp, require_workspace,
)


AGENT_PROVIDERS = ('codex', 'cursor', 'claude-code', 'generic')
MEETING_PLATFORMS = ('zoom', 'teams')
MEETING_STATES = ('joining', 'waiting_for_admission', 'live', 'ended')
WORKSPACE_PERMISSIONS = ('none', 'read-only', 'workspace-write')
COMMAND_PERMISSIONS = ('disabled', 'approval-required', 'allowed')
COMMIT_PERMISSIONS = ('disabled', 'approval-required')
AGENT_SESSION_FIELDS = ('provider', 'sessionId', 'workspace', 'model', 'metadata')
PERMISSION_FIELDS = ('workspace', 'commands', 'edits', 'network', 'commits', 'pushes')
MEETING_SESSION_FIELDS = (
    'id', 'platform', 'meetingUrl', 'agentSession', 'context', 'permissions', 'state', 'startedAt',
    'cameraEnabled', 'cameraState', 'visualState', 'degradedReason',
)
CAMERA_STATES = ('off', 'starting', 'on', 'blocked', 'degraded')
VISUAL_STATES = (
    'joining', 'listening', 'working', 'speaking', 'finalizing', 'needs_attention', 'ended',
)
DEGRADED_REASONS = ('platform_blocked', 'unsupported', 'unconfirmed', 'policy')


def _optional_enum(value, name, allowed):
    if value is None:
        return None
    return require_enum(value, name, allowed)


def _string_metadata(value):
    if value is None:
        return None
    payload = require_mapping(value, 'metadata')
    if len(payload) > 32:
        raise ValueError('metadata exceeds 32 keys')
    if not payload:
        return None
    items = []
    for key, item in payload.items():
        require_string(key, 'metadata key', max_length=64)
        require_string(item, f'metadata.{key}', max_length=1024)
        items.append((key, item))
    return MappingProxyType(dict(items))


@dataclass(frozen=True)
class AgentSessionRef:
    provider: str
    session_id: str
    workspace: str
    model: str = None
    metadata: MappingProxyType = None

    def to_dict(self):
        return omit_none({
            'provider': self.provider,
            'sessionId': self.session_id,
            'workspace': self.workspace,
            'model': self.model,
            'metadata': None if self.metadata is None else dict(self.metadata),
        })

    @classmethod
    def from_dict(cls, data):
        payload = require_mapping(data, 'agentSession')
        reject_unknown_fields(payload, AGENT_SESSION_FIELDS, 'agentSession')
        model = optional_field(payload, 'model')
        return cls(
            provider=require_enum(require_field(payload, 'provider', 'agentSession'),
                                  'provider', AGENT_PROVIDERS),
            session_id=require_id(require_field(payload, 'sessionId', 'agentSession'),
                                  'sessionId', max_length=256),
            workspace=require_workspace(require_field(payload, 'workspace', 'agentSession')),
            model=None if model is None else require_string(model, 'model', max_length=128),
            metadata=_string_metadata(optional_field(payload, 'metadata')),
        )


@dataclass(frozen=True)
class MeetingPermissions:
    workspace: str
    commands: str
    edits: str
    network: str
    commits: str
    pushes: str

    def to_dict(self):
        return {
            'workspace': self.workspace,
            'commands': self.commands,
            'edits': self.edits,
            'network': self.network,
            'commits': self.commits,
            'pushes': self.pushes,
        }

    @classmethod
    def from_dict(cls, data):
        payload = require_mapping(data, 'permissions')
        reject_unknown_fields(payload, PERMISSION_FIELDS, 'permissions')
        return cls(
            workspace=require_enum(require_field(payload, 'workspace', 'permissions'),
                                   'permissions.workspace', WORKSPACE_PERMISSIONS),
            commands=require_enum(require_field(payload, 'commands', 'permissions'),
                                  'permissions.commands', COMMAND_PERMISSIONS),
            edits=require_enum(require_field(payload, 'edits', 'permissions'),
                               'permissions.edits', COMMAND_PERMISSIONS),
            network=require_enum(require_field(payload, 'network', 'permissions'),
                                 'permissions.network', COMMAND_PERMISSIONS),
            commits=require_enum(require_field(payload, 'commits', 'permissions'),
                                 'permissions.commits', COMMIT_PERMISSIONS),
            pushes=require_enum(require_field(payload, 'pushes', 'permissions'),
                                'permissions.pushes', COMMIT_PERMISSIONS),
        )


@dataclass(frozen=True)
class MeetingSession:
    id: str
    platform: str
    meeting_url: str
    agent_session: AgentSessionRef
    context: ContextHandoff
    permissions: MeetingPermissions
    state: str
    started_at: str
    camera_enabled: bool = None
    camera_state: str = None
    visual_state: str = None
    degraded_reason: str = None

    def to_dict(self):
        return omit_none({
            'id': self.id,
            'platform': self.platform,
            'meetingUrl': self.meeting_url,
            'agentSession': self.agent_session.to_dict(),
            'context': self.context.to_dict(),
            'permissions': self.permissions.to_dict(),
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
        return cls(
            id=require_meeting_id(require_field(payload, 'id', 'meetingSession'), 'id'),
            platform=platform,
            meeting_url=meeting_url,
            agent_session=AgentSessionRef.from_dict(require_field(payload, 'agentSession',
                                                                 'meetingSession')),
            context=ContextHandoff.from_dict(require_field(payload, 'context', 'meetingSession')),
            permissions=MeetingPermissions.from_dict(require_field(payload, 'permissions',
                                                                  'meetingSession')),
            state=require_enum(require_field(payload, 'state', 'meetingSession'),
                               'state', MEETING_STATES),
            started_at=require_timestamp(require_field(payload, 'startedAt', 'meetingSession'),
                                         'startedAt'),
            camera_enabled=optional_bool(optional_field(payload, 'cameraEnabled'), 'cameraEnabled'),
            camera_state=_optional_enum(optional_field(payload, 'cameraState'), 'cameraState',
                                        CAMERA_STATES),
            visual_state=_optional_enum(optional_field(payload, 'visualState'), 'visualState',
                                        VISUAL_STATES),
            degraded_reason=_optional_enum(optional_field(payload, 'degradedReason'),
                                           'degradedReason', DEGRADED_REASONS),
        )
