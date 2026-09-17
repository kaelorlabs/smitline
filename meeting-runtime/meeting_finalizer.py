"""Persist local meeting handoffs and complete exact Codex append before lease release."""
from pathlib import Path
import json

from call_record import CallRecord
from handoff_builder import build_meeting_handoff, handoff_id_for
from schema_validation import reject_secrets, require_meeting_id
from session_continuity import EXACT, continuity_mode


FINALIZATION_NAME = 'finalization.json'
HANDOFF_NAME = 'handoff.json'


def recordings_root(runtime_root):
    return Path(runtime_root) / 'recordings'


def archive_dir(runtime_root, meeting_id):
    return recordings_root(runtime_root) / require_meeting_id(meeting_id)


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
    def __init__(self, runtime_root, *, daemon=None, append=None):
        self.runtime_root = Path(runtime_root)
        self.daemon = daemon
        self.append = append

    def directory(self, meeting_id):
        return archive_dir(self.runtime_root, meeting_id)

    def persist_local(self, session, *, reason, partial=False):
        meeting_id = require_meeting_id(session.id)
        directory = self.directory(meeting_id)
        directory.mkdir(parents=True, exist_ok=True)
        existing = _read_json(directory / HANDOFF_NAME)
        if existing and existing.get('handoffId') == handoff_id_for(meeting_id):
            from meeting_handoff import MeetingHandoff
            handoff = MeetingHandoff.from_dict(existing)
        else:
            archive = CallRecord(self.runtime_root / 'recordings', meeting_id=meeting_id)
            approvals = []
            if self.daemon is not None:
                try:
                    approvals = self.daemon.public_approvals(session.id)
                except Exception:
                    approvals = []
            handoff = build_meeting_handoff(
                archive, session, reason=reason, partial=partial, approvals=approvals)
            _write_json(directory / HANDOFF_NAME, handoff.to_dict())
        status = load_finalization(directory)
        if status.get('status') not in ('appended', 'ready'):
            _write_json(directory / FINALIZATION_NAME, {
                'status': 'local',
                'handoffId': handoff.handoff_id,
                'partial': bool(handoff.partial),
                'endReason': handoff.end_reason,
                'meetingId': meeting_id,
            })
        return handoff

    def _continuity(self, session):
        return continuity_mode(session.agent_session)

    async def complete(self, session, *, reason, partial=False):
        meeting_id = require_meeting_id(session.id)
        directory = self.directory(meeting_id)
        status = load_finalization(directory)
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
        continuity = self._continuity(session)
        if continuity == EXACT and self.daemon is not None:
            try:
                self.daemon.prepare_finalization(meeting_id)
            except Exception:
                pass
        if continuity == EXACT:
            if status.get('status') not in ('appended', 'ready'):
                if self.append is None:
                    result = {'error': 'exact append is unavailable'}
                else:
                    result = await self.append(session, handoff)
                if not isinstance(result, dict) or result.get('error'):
                    error = 'exact append failed'
                    if isinstance(result, dict) and result.get('error'):
                        error = str(result.get('error'))[:240]
                    _write_json(directory / FINALIZATION_NAME, {
                        'status': 'append_failed',
                        'handoffId': handoff.handoff_id,
                        'partial': bool(handoff.partial),
                        'endReason': reason,
                        'meetingId': meeting_id,
                        'error': error,
                    })
                    if self.daemon is not None:
                        self.daemon.note_append_failure(
                            meeting_id, handoff.handoff_id, error)
                    return {'status': 'append_failed', 'handoff': handoff, 'error': error}
            _write_json(directory / FINALIZATION_NAME, {
                'status': 'appended',
                'handoffId': handoff.handoff_id,
                'partial': bool(handoff.partial),
                'endReason': reason,
                'meetingId': meeting_id,
            })
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
        return {'status': 'ready', 'handoff': handoff, 'continuity': continuity}
