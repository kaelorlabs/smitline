"""Durable local transcript and application-visible call context."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from uuid import uuid4

from schema_validation import reject_secrets, require_meeting_id, require_mapping


class CallRecord:
    def __init__(self, root, meeting_id=None):
        root = Path(root)
        if meeting_id:
            self.meeting_id = require_meeting_id(meeting_id)
            self.directory = root / self.meeting_id
        else:
            self.meeting_id = None
            stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
            self.directory = root / (stamp + '-' + uuid4().hex[:8])
        self.directory.mkdir(parents=True, exist_ok=True)
        os.chmod(self.directory, 0o700)
        self.last_speaker = None
        if self.meeting_id and not (self.directory / 'archive.json').is_file():
            self.write_json('archive.json', {
                'version': 1,
                'meetingId': self.meeting_id,
                'startedAt': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
            })

    def append(self, filename, text):
        path = self.directory / filename
        if '/' in filename or '\\' in filename or filename in ('.', '..'):
            raise ValueError('archive filename must not contain a path')
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, 'a', encoding='utf-8') as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())

    def write_json(self, filename, payload):
        require_mapping(payload, filename)
        reject_secrets(payload, filename)
        encoded = json.dumps(payload, ensure_ascii=False, indent=2) + '\n'
        path = self.directory / filename
        temporary = path.with_suffix(path.suffix + '.tmp')
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
        return path

    def read_json(self, filename):
        path = self.directory / filename
        if not path.is_file():
            return None
        payload = json.loads(path.read_text(encoding='utf-8'))
        require_mapping(payload, filename)
        reject_secrets(payload, filename)
        return payload

    def event(self, kind, **data):
        payload = {'timestamp': datetime.now(timezone.utc).isoformat(), 'type': kind, **data}
        reject_secrets(payload, 'archive event')
        self.append('events.jsonl', json.dumps(payload, ensure_ascii=False) + '\n')

    def transcript(self, speaker, text, muted, **extra):
        self.event('transcript', speaker=speaker, text=text, muted=muted, **extra)
        label = 'Meeting' if speaker == 'meeting' else 'Agent (generated, playback not guaranteed)'
        key = (speaker, muted)
        prefix = '' if key == self.last_speaker else '\n\n' + label + (' [muted]' if muted else '') + ': '
        self.append('transcript.txt', prefix + text)
        self.last_speaker = key

    def close(self, *, usage=None, end_reason=None, stage=None):
        payload = {
            'endedAt': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
        }
        if end_reason:
            payload['endReason'] = str(end_reason)[:256]
        if stage:
            payload['stage'] = str(stage)[:64]
        if isinstance(usage, dict):
            reject_secrets(usage, 'usage')
            payload['usage'] = usage
        self.write_json('usage.json', payload)
        self.event('archive_closed', **{key: value for key, value in payload.items()
                                        if key != 'usage'})
        return payload
