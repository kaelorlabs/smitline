"""Screen-share settings, status, and observation schemas."""
from dataclasses import dataclass

from schema_validation import (
    omit_none, optional_bool, optional_field, reject_secrets, reject_unknown_fields, require_bool,
    require_enum, require_field, require_id, require_int, require_mapping,
    require_meeting_id, require_string, require_timestamp,
)


SETTINGS_FIELDS = (
    'enabled', 'captureIntervalMs', 'minChange', 'maxFrames', 'maxBytes', 'retentionSeconds',
    'settleTicks',
)
STATUS_FIELDS = (
    'enabled', 'available', 'active', 'paused', 'capturing', 'lastObservationAt',
    'degradedReason', 'captureIntervalMs', 'retention', 'analyzerAvailable',
)
OBSERVATION_FIELDS = (
    'id', 'meetingId', 'timestamp', 'summary', 'frameArtifactId', 'confidence', 'visibleText',
    'reused',
)
DEGRADED_REASONS = (
    'disabled', 'unavailable', 'selector_ambiguous', 'paused', 'analyzer_unavailable',
    'share_stopped', 'cancelled', 'ended', 'oversized', 'backpressure',
)
PRIVATE_SNIPPET_TOKENS = (
    'password', 'api key', 'secret', 'credential', 'bearer ', 'sk-', 'transcript',
)
DEFAULT_INTERVAL_MS = 4000
MIN_INTERVAL_MS = 2000
MAX_INTERVAL_MS = 15000
# minChange is compared with sqrt(changed-tile fraction); see visual_diff.
DEFAULT_MIN_CHANGE = 0.08
DEFAULT_SETTLE_TICKS = 1
MAX_SETTLE_TICKS = 5
DEFAULT_MAX_FRAMES = 20
MAX_FRAMES = 50
DEFAULT_MAX_BYTES = 8_000_000
MAX_BYTES = 12_000_000
DEFAULT_RETENTION_SECONDS = 3600
MAX_RETENTION_SECONDS = 6 * 3600
MAX_VISIBLE_TEXT = 3
MAX_SNIPPET = 80


def _number(value, name, *, min_value, max_value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f'{name} must be a number')
    number = float(value)
    if number < min_value or number > max_value:
        raise ValueError(f'{name} is out of bounds')
    return number


def _sanitize_snippet(value):
    text = require_string(value, 'visibleText item', max_length=MAX_SNIPPET, allow_newlines=False)
    lowered = text.lower()
    if any(token in lowered for token in PRIVATE_SNIPPET_TOKENS):
        return None
    if '/' in text or '\\' in text:
        return None
    reject_secrets({'text': text}, 'visibleText')
    return text


def default_settings():
    return {
        'enabled': False,
        'captureIntervalMs': DEFAULT_INTERVAL_MS,
        'minChange': DEFAULT_MIN_CHANGE,
        'maxFrames': DEFAULT_MAX_FRAMES,
        'maxBytes': DEFAULT_MAX_BYTES,
        'retentionSeconds': DEFAULT_RETENTION_SECONDS,
        'settleTicks': DEFAULT_SETTLE_TICKS,
    }


def parse_screen_share_settings(payload=None):
    if payload is None:
        return default_settings()
    data = require_mapping(payload, 'screenShare')
    reject_unknown_fields(data, SETTINGS_FIELDS, 'screenShare')
    settings = default_settings()
    if optional_field(data, 'enabled') is not None:
        settings['enabled'] = require_bool(data['enabled'], 'screenShare.enabled')
    if optional_field(data, 'captureIntervalMs') is not None:
        settings['captureIntervalMs'] = require_int(
            data['captureIntervalMs'], 'screenShare.captureIntervalMs',
            min_value=MIN_INTERVAL_MS, max_value=MAX_INTERVAL_MS)
    if optional_field(data, 'minChange') is not None:
        settings['minChange'] = _number(
            data['minChange'], 'screenShare.minChange', min_value=0.0, max_value=1.0)
    if optional_field(data, 'maxFrames') is not None:
        settings['maxFrames'] = require_int(
            data['maxFrames'], 'screenShare.maxFrames', min_value=1, max_value=MAX_FRAMES)
    if optional_field(data, 'maxBytes') is not None:
        settings['maxBytes'] = require_int(
            data['maxBytes'], 'screenShare.maxBytes', min_value=50_000, max_value=MAX_BYTES)
    if optional_field(data, 'retentionSeconds') is not None:
        settings['retentionSeconds'] = require_int(
            data['retentionSeconds'], 'screenShare.retentionSeconds',
            min_value=30, max_value=MAX_RETENTION_SECONDS)
    if optional_field(data, 'settleTicks') is not None:
        settings['settleTicks'] = require_int(
            data['settleTicks'], 'screenShare.settleTicks', min_value=0, max_value=MAX_SETTLE_TICKS)
    return settings


def public_status(payload=None):
    data = dict(payload or {})
    retention = data.get('retention') if isinstance(data.get('retention'), dict) else {}
    status = {
        'enabled': bool(data.get('enabled')),
        'available': bool(data.get('available')),
        'active': bool(data.get('active')),
        'paused': bool(data.get('paused')),
        'capturing': bool(data.get('capturing')),
        'lastObservationAt': data.get('lastObservationAt'),
        'degradedReason': data.get('degradedReason'),
        'captureIntervalMs': int(data.get('captureIntervalMs') or DEFAULT_INTERVAL_MS),
        'analyzerAvailable': bool(data.get('analyzerAvailable')),
        'retention': {
            'maxFrames': int(retention.get('maxFrames') or data.get('maxFrames') or DEFAULT_MAX_FRAMES),
            'maxBytes': int(retention.get('maxBytes') or data.get('maxBytes') or DEFAULT_MAX_BYTES),
            'retentionSeconds': int(
                retention.get('retentionSeconds') or data.get('retentionSeconds')
                or DEFAULT_RETENTION_SECONDS),
        },
    }
    if status['lastObservationAt'] is not None:
        require_timestamp(status['lastObservationAt'], 'lastObservationAt')
    if status['degradedReason'] is not None:
        status['degradedReason'] = require_enum(
            status['degradedReason'], 'degradedReason', DEGRADED_REASONS)
    reject_secrets(status, 'screenShare status')
    return omit_none(status)


@dataclass(frozen=True)
class VisualObservation:
    id: str
    meeting_id: str
    timestamp: str
    summary: str
    frame_artifact_id: str
    confidence: float
    visible_text: tuple = ()
    reused: bool = False

    def to_dict(self):
        return omit_none({
            'id': self.id,
            'meetingId': self.meeting_id,
            'timestamp': self.timestamp,
            'summary': self.summary,
            'frameArtifactId': self.frame_artifact_id,
            'confidence': self.confidence,
            'visibleText': list(self.visible_text) or None,
            'reused': True if self.reused else None,
        })

    @classmethod
    def from_dict(cls, data):
        payload = require_mapping(data, 'observation')
        reject_unknown_fields(payload, OBSERVATION_FIELDS, 'observation')
        snippets = optional_field(payload, 'visibleText')
        visible = []
        if snippets is not None:
            if not isinstance(snippets, list) or len(snippets) > MAX_VISIBLE_TEXT:
                raise ValueError('visibleText is invalid')
            for item in snippets:
                cleaned = _sanitize_snippet(item)
                if cleaned:
                    visible.append(cleaned)
        summary = require_string(require_field(payload, 'summary', 'observation'), 'summary',
                                 max_length=240, allow_newlines=False)
        lowered = summary.lower()
        if any(token in lowered for token in PRIVATE_SNIPPET_TOKENS):
            raise ValueError('observation summary cannot include private text')
        reject_secrets(payload, 'observation')
        confidence = _number(require_field(payload, 'confidence', 'observation'), 'confidence',
                             min_value=0.0, max_value=1.0)
        return cls(
            id=require_id(require_field(payload, 'id', 'observation'), 'observation.id'),
            meeting_id=require_meeting_id(require_field(payload, 'meetingId', 'observation')),
            timestamp=require_timestamp(require_field(payload, 'timestamp', 'observation'),
                                        'timestamp'),
            summary=summary,
            frame_artifact_id=require_id(
                require_field(payload, 'frameArtifactId', 'observation'), 'frameArtifactId'),
            confidence=confidence,
            visible_text=tuple(visible),
            reused=bool(optional_bool(optional_field(payload, 'reused'), 'observation.reused')),
        )


def observation_payload(meeting_id, *, observation_id, timestamp, summary, frame_artifact_id,
                        confidence, visible_text=None):
    return VisualObservation.from_dict({
        'id': observation_id,
        'meetingId': meeting_id,
        'timestamp': timestamp,
        'summary': summary,
        'frameArtifactId': frame_artifact_id,
        'confidence': confidence,
        'visibleText': list(visible_text or ()),
    }).to_dict()
