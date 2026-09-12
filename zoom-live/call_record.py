"""Durable local transcript and application-visible call context."""
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4


class CallRecord:
    def __init__(self, root):
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
        self.directory = Path(root) / (stamp + '-' + uuid4().hex[:8])
        self.directory.mkdir(parents=True, mode=0o700)
        self.last_speaker = None

    def append(self, filename, text):
        fd = os.open(self.directory / filename, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, 'a', encoding='utf-8') as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())

    def event(self, kind, **data):
        self.append('events.jsonl', json.dumps({
            'timestamp': datetime.now(timezone.utc).isoformat(), 'type': kind, **data
        }, ensure_ascii=False) + '\n')

    def transcript(self, speaker, text, muted):
        self.event('transcript', speaker=speaker, text=text, muted=muted)
        # Generated text does not prove playback: Zoom mute and interruptions can discard it.
        label = 'Meeting' if speaker == 'meeting' else 'Agent (generated, playback not guaranteed)'
        key = (speaker, muted)
        prefix = '' if key == self.last_speaker else '\n\n' + label + (' [muted]' if muted else '') + ': '
        self.append('transcript.txt', prefix + text)
        self.last_speaker = key
