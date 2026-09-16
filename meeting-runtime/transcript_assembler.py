"""Ordered GPT-Live transcript fragments with lookup at a session offset."""
from uuid import uuid4

from events import TranscriptEntry


class TranscriptAssembler:
    """Retains raw deltas and concatenates text at/before a timeline offset."""

    def __init__(self):
        self.fragments = []

    def add_delta(self, event):
        if not isinstance(event, dict):
            return None
        kind = event.get('type') or ''
        if kind not in ('session.input_transcript.delta', 'session.output_transcript.delta'):
            return None
        text = event.get('delta')
        if not isinstance(text, str) or text == '':
            return None
        source = 'input' if 'input_transcript' in kind else 'output'
        speaker = 'meeting' if source == 'input' else 'agent'
        start_ms = event.get('start_ms', event.get('offset_ms'))
        end_ms = event.get('end_ms')
        event_id = event.get('event_id')
        if not isinstance(event_id, str) or not event_id.strip():
            event_id = 'tr-' + uuid4().hex[:12]
        entry = TranscriptEntry(
            id=event_id,
            text=text,
            source=source,
            speaker=speaker,
            start_offset_ms=_optional_int(start_ms),
            end_offset_ms=_optional_int(end_ms),
        )
        self.fragments.append(entry)
        return entry

    def text_at_or_before(self, offset_ms, source='input'):
        parts = []
        for entry in self.fragments:
            if source and entry.source != source:
                continue
            if offset_ms is None or entry.start_offset_ms is None or entry.start_offset_ms <= offset_ms:
                parts.append(entry.text)
        return ''.join(parts)

    def relevant_request(self, offset_ms):
        text = self.text_at_or_before(offset_ms, source='input').strip()
        return text or None


def _optional_int(value):
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int) and value >= 0:
        return value
    if isinstance(value, float) and value >= 0 and value.is_integer():
        return int(value)
    return None
