"""Version 1 structured meeting handoff returned to the originating conversation."""
from dataclasses import dataclass

from agent_sessions import MeetingPermissions
from context_handoff import GitState
from schema_validation import (
    omit_none, optional_bool, optional_field, optional_int, optional_string, reject_unknown_fields,
    require_enum, require_field, require_id, require_mapping, require_meeting_id,
    require_object_list, require_string, require_string_list, require_timestamp,
    require_version,
)


WORK_STATUSES = ('completed', 'failed', 'cancelled')
TRANSCRIPT_REFERENCE_FIELDS = ('entryId', 'startOffsetMs', 'endOffsetMs')
DECISION_FIELDS = ('text', 'evidence')
ACTION_ITEM_FIELDS = ('text', 'owner', 'due')
WORK_RECORD_FIELDS = ('taskId', 'summary', 'status', 'startedAt', 'endedAt')
ARTIFACT_REFERENCE_FIELDS = ('artifactId', 'path')
HANDOFF_FIELDS = (
    'version', 'meetingId', 'startedAt', 'endedAt', 'summary', 'decisions', 'requirements',
    'actionItems', 'unresolvedQuestions', 'filesDiscussed', 'workPerformed', 'artifacts',
    'transcriptPath', 'recommendedNextAction', 'handoffId', 'partial', 'endReason',
    'archivePath', 'git', 'permissions', 'approvals',
)


def _approval_item(data):
    from approvals import ApprovalRecord, public_approval
    return public_approval(ApprovalRecord.from_dict(data).to_dict())


@dataclass(frozen=True)
class TranscriptReference:
    entry_id: str
    start_offset_ms: int = None
    end_offset_ms: int = None

    def to_dict(self):
        return omit_none({
            'entryId': self.entry_id,
            'startOffsetMs': self.start_offset_ms,
            'endOffsetMs': self.end_offset_ms,
        })

    @classmethod
    def from_dict(cls, data):
        payload = require_mapping(data, 'evidence item')
        reject_unknown_fields(payload, TRANSCRIPT_REFERENCE_FIELDS, 'evidence item')
        return cls(
            entry_id=require_id(require_field(payload, 'entryId', 'evidence item'), 'entryId'),
            start_offset_ms=optional_int(optional_field(payload, 'startOffsetMs'),
                                         'startOffsetMs', min_value=0),
            end_offset_ms=optional_int(optional_field(payload, 'endOffsetMs'),
                                       'endOffsetMs', min_value=0),
        )


@dataclass(frozen=True)
class DecisionRecord:
    text: str
    evidence: tuple = ()

    def to_dict(self):
        payload = {'text': self.text}
        if self.evidence:
            payload['evidence'] = [item.to_dict() for item in self.evidence]
        return payload

    @classmethod
    def from_dict(cls, data):
        payload = require_mapping(data, 'decisions item')
        reject_unknown_fields(payload, DECISION_FIELDS, 'decisions item')
        evidence = optional_field(payload, 'evidence')
        return cls(
            text=require_string(require_field(payload, 'text', 'decisions item'),
                                'decisions item.text', allow_newlines=True),
            evidence=() if evidence is None else require_object_list(
                evidence, 'evidence', TranscriptReference.from_dict),
        )


@dataclass(frozen=True)
class ActionItem:
    text: str
    owner: str = None
    due: str = None

    def to_dict(self):
        return omit_none({'text': self.text, 'owner': self.owner, 'due': self.due})

    @classmethod
    def from_dict(cls, data):
        payload = require_mapping(data, 'actionItems item')
        reject_unknown_fields(payload, ACTION_ITEM_FIELDS, 'actionItems item')
        return cls(
            text=require_string(require_field(payload, 'text', 'actionItems item'),
                                'actionItems item.text', allow_newlines=True),
            owner=optional_string(optional_field(payload, 'owner'), 'owner', max_length=256),
            due=optional_string(optional_field(payload, 'due'), 'due', max_length=64),
        )


@dataclass(frozen=True)
class AgentWorkRecord:
    task_id: str
    summary: str
    status: str
    started_at: str = None
    ended_at: str = None

    def to_dict(self):
        return omit_none({
            'taskId': self.task_id,
            'summary': self.summary,
            'status': self.status,
            'startedAt': self.started_at,
            'endedAt': self.ended_at,
        })

    @classmethod
    def from_dict(cls, data):
        payload = require_mapping(data, 'workPerformed item')
        reject_unknown_fields(payload, WORK_RECORD_FIELDS, 'workPerformed item')
        started_at = optional_field(payload, 'startedAt')
        ended_at = optional_field(payload, 'endedAt')
        return cls(
            task_id=require_id(require_field(payload, 'taskId', 'workPerformed item'), 'taskId'),
            summary=require_string(require_field(payload, 'summary', 'workPerformed item'),
                                   'workPerformed item.summary', allow_empty=True, allow_newlines=True),
            status=require_enum(require_field(payload, 'status', 'workPerformed item'),
                                'status', WORK_STATUSES),
            started_at=None if started_at is None else require_timestamp(started_at, 'startedAt'),
            ended_at=None if ended_at is None else require_timestamp(ended_at, 'endedAt'),
        )


@dataclass(frozen=True)
class ArtifactReference:
    artifact_id: str
    path: str = None

    def to_dict(self):
        return omit_none({'artifactId': self.artifact_id, 'path': self.path})

    @classmethod
    def from_dict(cls, data):
        payload = require_mapping(data, 'artifacts item')
        reject_unknown_fields(payload, ARTIFACT_REFERENCE_FIELDS, 'artifacts item')
        return cls(
            artifact_id=require_id(require_field(payload, 'artifactId', 'artifacts item'),
                                   'artifactId'),
            path=optional_string(optional_field(payload, 'path'), 'path', max_length=4096),
        )


@dataclass(frozen=True)
class MeetingHandoff:
    version: int
    meeting_id: str
    started_at: str
    ended_at: str
    summary: str
    decisions: tuple
    requirements: tuple
    action_items: tuple
    unresolved_questions: tuple
    files_discussed: tuple
    work_performed: tuple
    artifacts: tuple
    transcript_path: str
    recommended_next_action: str
    handoff_id: str = None
    partial: bool = None
    end_reason: str = None
    archive_path: str = None
    git: GitState = None
    permissions: MeetingPermissions = None
    approvals: tuple = ()

    def to_dict(self):
        return omit_none({
            'version': self.version,
            'meetingId': self.meeting_id,
            'startedAt': self.started_at,
            'endedAt': self.ended_at,
            'summary': self.summary,
            'decisions': [item.to_dict() for item in self.decisions],
            'requirements': list(self.requirements),
            'actionItems': [item.to_dict() for item in self.action_items],
            'unresolvedQuestions': list(self.unresolved_questions),
            'filesDiscussed': list(self.files_discussed),
            'workPerformed': [item.to_dict() for item in self.work_performed],
            'artifacts': [item.to_dict() for item in self.artifacts],
            'transcriptPath': self.transcript_path,
            'recommendedNextAction': self.recommended_next_action,
            'handoffId': self.handoff_id,
            'partial': self.partial,
            'endReason': self.end_reason,
            'archivePath': self.archive_path,
            'git': None if self.git is None else self.git.to_dict(),
            'permissions': None if self.permissions is None else self.permissions.to_dict(),
            'approvals': None if not self.approvals else [dict(item) for item in self.approvals],
        })

    @classmethod
    def from_dict(cls, data):
        payload = require_mapping(data, 'handoff')
        reject_unknown_fields(payload, HANDOFF_FIELDS, 'handoff')
        return cls(
            version=require_version(require_field(payload, 'version', 'handoff')),
            meeting_id=require_meeting_id(require_field(payload, 'meetingId', 'handoff')),
            started_at=require_timestamp(require_field(payload, 'startedAt', 'handoff'), 'startedAt'),
            ended_at=require_timestamp(require_field(payload, 'endedAt', 'handoff'), 'endedAt'),
            summary=require_string(require_field(payload, 'summary', 'handoff'), 'summary',
                                   allow_empty=True, allow_newlines=True),
            decisions=require_object_list(require_field(payload, 'decisions', 'handoff'),
                                          'decisions', DecisionRecord.from_dict),
            requirements=require_string_list(require_field(payload, 'requirements', 'handoff'),
                                             'requirements'),
            action_items=require_object_list(require_field(payload, 'actionItems', 'handoff'),
                                             'actionItems', ActionItem.from_dict),
            unresolved_questions=require_string_list(
                require_field(payload, 'unresolvedQuestions', 'handoff'), 'unresolvedQuestions'),
            files_discussed=require_string_list(require_field(payload, 'filesDiscussed', 'handoff'),
                                                'filesDiscussed'),
            work_performed=require_object_list(require_field(payload, 'workPerformed', 'handoff'),
                                               'workPerformed', AgentWorkRecord.from_dict),
            artifacts=require_object_list(require_field(payload, 'artifacts', 'handoff'),
                                          'artifacts', ArtifactReference.from_dict),
            transcript_path=require_string(require_field(payload, 'transcriptPath', 'handoff'),
                                           'transcriptPath', max_length=4096),
            recommended_next_action=require_string(
                require_field(payload, 'recommendedNextAction', 'handoff'),
                'recommendedNextAction', allow_empty=True, allow_newlines=True),
            handoff_id=optional_string(optional_field(payload, 'handoffId'), 'handoffId',
                                       max_length=256),
            partial=optional_bool(optional_field(payload, 'partial'), 'partial'),
            end_reason=optional_string(optional_field(payload, 'endReason'), 'endReason',
                                       max_length=256),
            archive_path=optional_string(optional_field(payload, 'archivePath'), 'archivePath',
                                         max_length=4096),
            git=None if optional_field(payload, 'git') is None else GitState.from_dict(
                payload['git']),
            permissions=None if optional_field(payload, 'permissions') is None else (
                MeetingPermissions.from_dict(payload['permissions'])),
            approvals=() if optional_field(payload, 'approvals') is None else require_object_list(
                payload['approvals'], 'approvals', _approval_item),
        )
