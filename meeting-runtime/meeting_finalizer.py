"""Persist local meeting handoffs and hand them to the daemon once a meeting ends."""
from pathlib import Path
import json

from call_record import CallRecord
from handoff_builder import build_meeting_handoff, handoff_id_for
from schema_validation import reject_secrets, require_meeting_id


FINALIZATION_NAME = 'finalization.json'
HANDOFF_NAME = 'handoff.json'


def recordings_root(runtime_root):
    return Path(runtime_root) / 'recordings'


def archive_dir(runtime_root, meeting_id):
    return recordings_root(runtime_root) / require_meeting_id(meeting_id)


def meeting_transcript(runtime_root, meeting_id):
    """A meeting's transcript from its archive, as call transcript entries.

    The bridge logs GPT-Live's transcript in pieces (a word, a comma); pieces in a row from one
    side join into one line. The assistant is 'agent'; everyone in the meeting is 'meeting'.
    """
    try:
        raw = (archive_dir(runtime_root, meeting_id) / 'events.jsonl').read_text(encoding='utf-8')
    except (OSError, ValueError):
        return []
    lines = []
    for row in raw.splitlines():
        try:
            event = json.loads(row)
        except ValueError:
            continue
        if not isinstance(event, dict) or event.get('type') != 'transcript':
            continue
        text = str(event.get('text') or '')
        if not text.strip():
            continue
        speaker = 'agent' if event.get('speaker') == 'agent' else 'meeting'
        if lines and lines[-1]['speaker'] == speaker:
            lines[-1]['text'] += text
        else:
            lines.append({'speaker': speaker, 'text': text})
    return [{'speaker': line['speaker'], 'text': ' '.join(line['text'].split())} for line in lines]


def _write_json(path, payload):
    reject_secrets(payload, path.name)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    temporary.replace(path)
    path.chmod(0o600)
    return payload


def _read_json(path):
    if not path.is_file():
        return None
    payload = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(payload, dict):
        raise ValueError('archive json must be an object')
    reject_secrets(payload, path.name)
    return payload


def load_finalization(directory):
    return _read_json(Path(directory) / FINALIZATION_NAME) or {}


class MeetingFinalizer:
    def __init__(self, runtime_root, *, daemon=None):
        self.runtime_root = Path(runtime_root)
        self.daemon = daemon

    def directory(self, meeting_id):
        return archive_dir(self.runtime_root, meeting_id)

    def persist_local(self, session, *, reason, partial=False):
        meeting_id = require_meeting_id(session.id)
        directory = self.directory(meeting_id)
        directory.mkdir(parents=True, exist_ok=True)
        existing = _read_json(directory / HANDOFF_NAME)
        if existing and existing.get('handoffId') == handoff_id_for(meeting_id):
            from meeting_handoff import MeetingHandoff, upgrade_legacy_handoff
            handoff = MeetingHandoff.from_dict(upgrade_legacy_handoff(existing))
        else:
            archive = CallRecord(self.runtime_root / 'recordings', meeting_id=meeting_id)
            handoff = build_meeting_handoff(archive, session, reason=reason, partial=partial)
            _write_json(directory / HANDOFF_NAME, handoff.to_dict())
        status = load_finalization(directory)
        if status.get('status') != 'ready':
            _write_json(directory / FINALIZATION_NAME, {
                'status': 'local',
                'handoffId': handoff.handoff_id,
                'partial': bool(handoff.partial),
                'endReason': handoff.end_reason,
                'meetingId': meeting_id,
            })
        return handoff

    async def complete(self, session, *, reason, partial=False):
        meeting_id = require_meeting_id(session.id)
        directory = self.directory(meeting_id)
        if self.daemon is not None:
            try:
                record = self.daemon.meetings.get(meeting_id)
            except Exception:
                record = None
            if record is not None and record.handoff is not None:
                _write_json(directory / FINALIZATION_NAME, {
                    'status': 'ready',
                    'handoffId': getattr(record.handoff, 'handoff_id', None) or handoff_id_for(meeting_id),
                    'partial': bool(getattr(record.handoff, 'partial', False)),
                    'endReason': reason,
                    'meetingId': meeting_id,
                })
                return {'status': 'ready', 'handoff': record.handoff, 'idempotent': True}
        handoff = self.persist_local(session, reason=reason, partial=partial)
        if self.daemon is not None:
            stored = self.daemon.store_handoff(handoff)
            handoff = stored
        _write_json(directory / FINALIZATION_NAME, {
            'status': 'ready',
            'handoffId': handoff.handoff_id,
            'partial': bool(handoff.partial),
            'endReason': reason,
            'meetingId': meeting_id,
        })
        return {'status': 'ready', 'handoff': handoff}
