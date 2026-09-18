"""Ordered GPT-Live transcript fragments with lookup at a session offset."""
import re
from uuid import uuid4

from events import TranscriptEntry
from startup_input import clip_tokens, estimate_tokens


UTTERANCE_GAP_MS = 2000
REQUEST_TOKEN_LIMIT = 500
TRANSCRIPT_TOKEN_LIMIT = 1500
BACKCHANNELS = frozenset({
    'mmhmm', 'mhm', 'mm', 'uhhuh', 'uhuh', 'uh', 'um', 'yeah', 'yep', 'yup',
    'yes', 'ok', 'okay', 'alright', 'right', 'sure', 'gotit',
})


class TranscriptAssembler:
    """Retains raw deltas and selects the latest input utterance at an offset."""

    def __init__(self):
        self.fragments = []
        self._seen = set()
        self._turn_ids = []

    def add_delta(self, event):
        if not isinstance(event, dict):
            return None
        kind = event.get('type') or ''
        if kind not in ('session.input_transcript.delta', 'session.output_transcript.delta'):
            return None
        text = event.get('delta')
        if not isinstance(text, str) or text == '':
            return None
        identity = _event_identity(event)
        if identity in self._seen:
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
        self._seen.add(identity)
        self.fragments.append(entry)
        turn_id = event.get('item_id') or event.get('turn_id')
        self._turn_ids.append(turn_id if isinstance(turn_id, str) and turn_id.strip() else None)
        return entry

    def _visible(self, offset_ms, source=None):
        visible = []
        for index, entry in enumerate(self.fragments):
            if source and entry.source != source:
                continue
            if offset_ms is not None and entry.start_offset_ms is not None and entry.start_offset_ms > offset_ms:
                continue
            visible.append((index, entry))
        return visible

    def text_at_or_before(self, offset_ms, source='input'):
        return ''.join(entry.text for _index, entry in self._visible(offset_ms, source))

    def bounded_transcript(self, offset_ms):
        text = self.text_at_or_before(offset_ms, source=None)
        if not text:
            return ''
        if _all_missing_starts(entry for _index, entry in self._visible(offset_ms)):
            return clip_token_suffix(text, TRANSCRIPT_TOKEN_LIMIT)
        return clip_tokens(text, TRANSCRIPT_TOKEN_LIMIT)

    def relevant_request(self, offset_ms):
        groups = self._input_utterances(offset_ms)
        chosen = _latest_substantive(groups)
        if chosen is None:
            return None
        entries = [entry for _index, entry in chosen]
        text = join_fragments(entries).strip()
        if not text:
            return None
        if _all_missing_starts(entries) and estimate_tokens(text) > REQUEST_TOKEN_LIMIT:
            return clip_token_suffix(text, REQUEST_TOKEN_LIMIT) or None
        return clip_tokens(text, REQUEST_TOKEN_LIMIT) or None

    def _input_utterances(self, offset_ms):
        groups = []
        current = []
        last_input = None
        last_turn = None
        for index, entry in self._visible(offset_ms):
            if entry.source == 'output':
                if current:
                    groups.append(current)
                    current = []
                last_input = None
                last_turn = None
                continue
            if entry.source != 'input':
                continue
            turn_id = self._turn_ids[index]
            if current and last_input is not None and _starts_new_utterance(last_input, last_turn, entry, turn_id):
                groups.append(current)
                current = []
            current.append((index, entry))
            last_input = entry
            last_turn = turn_id
        if current:
            groups.append(current)
        return groups


def _starts_new_utterance(previous, previous_turn, current, current_turn):
    if previous_turn and current_turn and previous_turn != current_turn:
        return True
    gap = _timestamp_gap(previous, current)
    if gap is None:
        return _looks_complete(previous)
    return gap > UTTERANCE_GAP_MS


def _looks_complete(entry):
    text = (entry.text or '').rstrip()
    return bool(text) and text[-1] in '.!?'


def _timestamp_gap(previous, current):
    if current.start_offset_ms is None:
        return None
    origin = previous.end_offset_ms
    if origin is None:
        origin = previous.start_offset_ms
    if origin is None:
        return None
    return current.start_offset_ms - origin


def join_fragments(entries):
    parts = []
    for entry in entries:
        text = entry.text
        if not text:
            continue
        if (parts and not parts[-1][-1].isspace() and not text[0].isspace()
                and (parts[-1][-1].isalnum() or parts[-1][-1] in ',;:')
                and (text[0].isalnum() or text[0] in '\'"')):
            parts.append(' ')
        parts.append(text)
    return ''.join(parts)


def _latest_substantive(groups):
    last_nonempty = None
    last_substantive = None
    for group in groups:
        text = join_fragments(entry for _index, entry in group).strip()
        if not text:
            continue
        last_nonempty = group
        if not _is_backchannel(text):
            last_substantive = group
    return last_substantive or last_nonempty


def _is_backchannel(text):
    compact = re.sub(r'[^a-z]+', '', text.lower())
    return not compact or compact in BACKCHANNELS


def _all_missing_starts(entries):
    return all(entry.start_offset_ms is None for entry in entries)


def _event_identity(event):
    event_id = event.get('event_id')
    if isinstance(event_id, str) and event_id.strip():
        return ('id', event_id.strip())
    return (
        'body',
        event.get('type'),
        event.get('delta'),
        event.get('start_ms', event.get('offset_ms')),
        event.get('end_ms'),
        event.get('item_id') or event.get('turn_id'),
    )


def clip_token_suffix(text, limit):
    text = str(text or '')
    if estimate_tokens(text) <= limit:
        return text
    low, high = 0, len(text)
    while low < high:
        mid = (low + high) // 2
        if estimate_tokens(text[mid:]) <= limit:
            high = mid
        else:
            low = mid + 1
    return text[low:].lstrip()


def _optional_int(value):
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int) and value >= 0:
        return value
    if isinstance(value, float) and value >= 0 and value.is_integer():
        return int(value)
    return None
