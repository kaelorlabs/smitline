"""Deterministic MeetingHandoff extraction from a local meeting archive."""
from datetime import datetime, timezone
from pathlib import Path
import json

from call_record import CallRecord
from meeting_handoff import MeetingHandoff
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


def _files_discussed(session):
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


def build_meeting_handoff(archive, session, *, reason, partial=False, ended_at=None):
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
    next_action = 'Review the meeting transcript and follow up on open questions.'
    if partial:
        next_action = 'Review the partial meeting transcript; the meeting did not finish normally.'
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
        files_discussed=_files_discussed(session),
        work_performed=(),
        artifacts=(),
        transcript_path='transcript.txt',
        recommended_next_action=next_action,
        handoff_id=handoff_id_for(meeting_id),
        partial=bool(partial),
        end_reason=_clip(reason or 'ended', 256) or 'ended',
        archive_path=str(directory.name),
    )
    reject_secrets(handoff.to_dict(), 'meeting handoff')
    return handoff
