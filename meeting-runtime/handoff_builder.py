"""Deterministic MeetingHandoff extraction from a local meeting archive."""
from datetime import datetime, timezone
from pathlib import Path
import json

from call_record import CallRecord
from context_handoff import GitState
from meeting_handoff import (
    ActionItem, AgentWorkRecord, ArtifactReference, DecisionRecord, MeetingHandoff,
)
from schema_validation import reject_secrets, require_meeting_id
from startup_input import clip_tokens


MAX_SUMMARY = 4000
MAX_ITEM = 1000
STABLE_HANDOFF_PREFIX = 'hnd-'


def _now():
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _read_jsonl(path):
    if not path.is_file():
        return []
    rows = []
    for line in path.read_text(encoding='utf-8').splitlines():
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if isinstance(item, dict):
            reject_secrets(item, 'archive event')
            rows.append(item)
    return rows


def _clip(value, limit=MAX_ITEM):
    text = '' if value is None else str(value)
    return clip_tokens(text, limit) if text else ''


def _git_from_session(session):
    context = getattr(session, 'context', None)
    git = getattr(context, 'git', None) if context is not None else None
    if git is None and isinstance(context, dict):
        git = context.get('git')
    if git is None:
        return None
    if isinstance(git, GitState):
        return git
    try:
        return GitState.from_dict(git)
    except (TypeError, ValueError):
        return None


def _work_from_events(events):
    started = {}
    completed = {}
    for event in events:
        kind = event.get('type')
        result = event.get('result') if isinstance(event.get('result'), dict) else {}
        delegation_id = (
            event.get('delegationId') or event.get('delegation_id') or result.get('delegationId')
        )
        if kind == 'delegation.started' and isinstance(delegation_id, str):
            started[delegation_id] = event
        elif kind in ('delegation.completed', 'delegation.cancelled', 'workspace.action.completed',
                      'git.action.completed') and isinstance(delegation_id, str):
            completed[delegation_id] = event
    records = []
    for delegation_id, event in list(completed.items())[:50]:
        status = 'cancelled' if event.get('type') == 'delegation.cancelled' else 'completed'
        result = event.get('result') or {}
        summary = _clip(
            event.get('message') or event.get('summary') or result.get('summary')
            or ('Delegated turn ' + delegation_id))
        records.append(AgentWorkRecord(
            task_id=delegation_id[:128],
            summary=summary or ('Delegated turn ' + delegation_id),
            status=status,
            started_at=None,
            ended_at=None,
        ))
    for event in events:
        kind = event.get('type')
        if kind not in ('git.action.completed', 'git.action.failed', 'git.action.cancelled'):
            continue
        result = event.get('result') if isinstance(event.get('result'), dict) else {}
        operation_id = result.get('operationId') or event.get('operationId')
        if not isinstance(operation_id, str):
            continue
        if kind == 'git.action.completed':
            status = 'completed'
        elif kind == 'git.action.cancelled':
            status = 'cancelled'
        else:
            status = 'failed'
        records.append(AgentWorkRecord(
            task_id=operation_id[:128],
            summary=_clip(result.get('summary') or 'Recorded reviewed files'),
            status=status,
        ))
    return tuple(records[:50])


def _artifacts_from_events(events, directory):
    artifacts = []
    for event in events:
        if event.get('type') != 'plot':
            continue
        path = event.get('path') or event.get('file')
        if not isinstance(path, str) or not path:
            continue
        name = Path(path).name
        if not name or name in ('.', '..'):
            continue
        artifacts.append(ArtifactReference(
            artifact_id=('plot-' + str(len(artifacts) + 1)),
            path=str((directory / name).name),
        ))
    for event in events:
        if event.get('type') != 'artifact.created':
            continue
        artifact = event.get('artifact') or {}
        path = artifact.get('path')
        artifact_id = artifact.get('id')
        if not isinstance(path, str) or not artifact_id:
            continue
        artifacts.append(ArtifactReference(artifact_id=str(artifact_id)[:128], path=path[:4096]))
    for path in sorted(Path(directory).glob('*.png'))[:24]:
        if any(item.path == path.name for item in artifacts):
            continue
        artifacts.append(ArtifactReference(artifact_id='file-' + path.stem[:32], path=path.name))
    return tuple(artifacts[:32])


def _files_discussed(session, events):
    names = []
    context = getattr(session, 'context', None)
    important = getattr(context, 'important_files', None) if context is not None else None
    if important is None and isinstance(context, dict):
        important = context.get('importantFiles')
    for item in important or ():
        text = _clip(item, 256)
        if text:
            names.append(text)
    return tuple(names[:50])


def _questions(session):
    context = getattr(session, 'context', None)
    questions = getattr(context, 'open_questions', None) if context is not None else None
    if questions is None and isinstance(context, dict):
        questions = context.get('openQuestions')
    return tuple(_clip(item) for item in (questions or ()) if str(item).strip())[:32]


def _summary(session, events, reason, partial):
    context = getattr(session, 'context', None)
    objective = getattr(context, 'objective', None) if context is not None else None
    if objective is None and isinstance(context, dict):
        objective = context.get('objective')
    transcripts = [event for event in events if event.get('type') == 'transcript']
    parts = []
    if objective:
        parts.append(_clip(objective, 800))
    parts.append(
        'Meeting ended ({reason}) with {count} transcript entries{partial}.'.format(
            reason=_clip(reason or 'ended', 80),
            count=len(transcripts),
            partial=' (partial)' if partial else '',
        )
    )
    return _clip(' '.join(parts), MAX_SUMMARY)


def handoff_id_for(meeting_id):
    return STABLE_HANDOFF_PREFIX + require_meeting_id(meeting_id)


def build_meeting_handoff(archive, session, *, reason, partial=False, ended_at=None,
                          approvals=None):
    directory = Path(archive.directory) if isinstance(archive, CallRecord) else Path(archive)
    meeting_id = getattr(session, 'id', None) or getattr(archive, 'meeting_id', None)
    meeting_id = require_meeting_id(meeting_id)
    events = _read_jsonl(directory / 'events.jsonl')
    started_at = getattr(session, 'started_at', None) or _now()
    usage = {}
    usage_path = directory / 'usage.json'
    if usage_path.is_file():
        try:
            usage = json.loads(usage_path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            usage = {}
    ended = ended_at or usage.get('endedAt') or _now()
    next_action = (
        'Continue in the original Codex thread.'
        if getattr(getattr(session, 'agent_session', None), 'session_id', None) not in (
            None, 'local-portal')
        else 'Review the local meeting archive.'
    )
    if partial:
        next_action = 'Review the partial meeting archive, then continue the original work.'
    handoff = MeetingHandoff(
        version=1,
        meeting_id=meeting_id,
        started_at=started_at,
        ended_at=ended,
        summary=_summary(session, events, reason, partial),
        decisions=(),
        requirements=(),
        action_items=(),
        unresolved_questions=_questions(session),
        files_discussed=_files_discussed(session, events),
        work_performed=_work_from_events(events),
        artifacts=_artifacts_from_events(events, directory),
        transcript_path='transcript.txt',
        recommended_next_action=next_action,
        handoff_id=handoff_id_for(meeting_id),
        partial=bool(partial),
        end_reason=_clip(reason or 'ended', 256) or 'ended',
        archive_path=str(directory.name),
        git=_git_from_session(session),
        permissions=getattr(session, 'permissions', None),
        approvals=tuple(approvals or ()),
    )
    reject_secrets(handoff.to_dict(), 'meeting handoff')
    return handoff
