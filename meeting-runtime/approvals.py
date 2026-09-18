"""Versioned approval requests, decisions, and fail-closed sanitization."""
from datetime import datetime, timedelta, timezone
from types import MappingProxyType
from urllib.parse import urlparse

from agent_sessions import PERMISSION_FIELDS
from schema_validation import (
    field_name_is_secret, omit_none, optional_field, optional_string, reject_secrets,
    reject_unknown_fields, require_enum, require_field, require_id, require_mapping,
    require_meeting_id, require_string, require_timestamp,
)


APPROVAL_VERSION = 1
APPROVAL_CATEGORIES = PERMISSION_FIELDS
APPROVAL_STATUSES = ('pending', 'approved', 'denied', 'expired', 'cancelled')
DECISION_VALUES = ('approved', 'denied')
APPROVAL_FIELDS = (
    'version', 'id', 'meetingId', 'delegationId', 'permission', 'category', 'summary',
    'scope', 'status', 'createdAt', 'expiresAt', 'resolvedAt', 'decision', 'consumed',
)
DECISION_FIELDS = ('approvalId', 'decision', 'decidedAt')
DEFAULT_TTL_SECONDS = 900
PRIVATE_SUMMARY_TOKENS = (
    'transcript', 'password', 'api key', 'secret', 'bearer', 'cookie', 'private key',
    'authorization', 'diff --git', '@@ ',
)
UNSAFE_SUMMARY = (
    'rm -', 'git push', 'git commit', 'sudo ', 'curl http', 'wget http', 'drop table',
)


def _utcnow():
    return datetime.now(timezone.utc)


def _iso(moment):
    return moment.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def parse_timestamp(value, name='timestamp'):
    text = require_timestamp(value, name)
    candidate = text[:-1] + '+00:00' if text.endswith('Z') else text
    parsed = datetime.fromisoformat(candidate)
    if parsed.tzinfo is None:
        raise ValueError(f'{name} must include a timezone')
    return parsed.astimezone(timezone.utc)


def sanitize_summary(value, workspace=None):
    text = require_string(value, 'summary', max_length=240, allow_newlines=False)
    lowered = text.lower()
    if any(token in lowered for token in PRIVATE_SUMMARY_TOKENS):
        raise ValueError('approval summary cannot include private or command text')
    if any(token in lowered for token in UNSAFE_SUMMARY):
        raise ValueError('approval summary cannot include raw actions')
    if workspace:
        root = str(workspace).rstrip('/')
        if root and root.lower() not in lowered and ('/' in text or '\\' in text):
            if '..' in text or text.startswith('/') or ':\\' in text:
                raise ValueError('approval summary cannot include paths outside the workspace')
    return text


def sanitize_scope(value, workspace=None):
    if value is None:
        return MappingProxyType({})
    payload = require_mapping(value, 'scope')
    if len(payload) > 8:
        raise ValueError('approval scope exceeds 8 keys')
    items = []
    for key, item in payload.items():
        require_string(key, 'scope key', max_length=32)
        if field_name_is_secret(key):
            raise ValueError('approval scope cannot include secret fields')
        text = require_string(item, f'scope.{key}', max_length=128)
        if '..' in text:
            raise ValueError('approval scope cannot include parent paths')
        if key in ('host', 'url') and text:
            parsed = urlparse(text if '://' in text else 'https://' + text)
            if parsed.username or parsed.password:
                raise ValueError('approval scope cannot include credentials')
        if workspace and key == 'path':
            root = str(workspace)
            if not (text == root or text.startswith(root.rstrip('/') + '/')):
                raise ValueError('approval scope path must stay inside the workspace')
        items.append((key, text))
    reject_secrets(dict(items), 'scope')
    return MappingProxyType(dict(items))


def public_approval(record):
    payload = dict(record)
    payload.pop('consumed', None)
    return omit_none(payload)


class ApprovalRecord:
    def __init__(self, payload):
        self._payload = dict(payload)

    def to_dict(self):
        return omit_none(dict(self._payload))

    def public_dict(self):
        return public_approval(self.to_dict())

    @classmethod
    def from_dict(cls, data, *, workspace=None):
        raw = require_mapping(data, 'approval')
        reject_unknown_fields(raw, APPROVAL_FIELDS, 'approval')
        reject_secrets(raw, 'approval')
        if 'version' in raw and raw['version'] != APPROVAL_VERSION:
            raise ValueError('approval version must be 1')
        category = require_enum(
            require_field(raw, 'category', 'approval') if 'category' in raw
            else require_field(raw, 'permission', 'approval'),
            'category', APPROVAL_CATEGORIES)
        created = require_timestamp(require_field(raw, 'createdAt', 'approval'), 'createdAt')
        expires = require_timestamp(require_field(raw, 'expiresAt', 'approval'), 'expiresAt')
        if parse_timestamp(expires, 'expiresAt') <= parse_timestamp(created, 'createdAt'):
            raise ValueError('approval expiresAt must be after createdAt')
        status = require_enum(require_field(raw, 'status', 'approval'), 'status', APPROVAL_STATUSES)
        consumed = raw.get('consumed', False)
        if consumed not in (True, False):
            raise ValueError('consumed must be a boolean')
        record = {
            'version': APPROVAL_VERSION,
            'id': require_id(require_field(raw, 'id', 'approval'), 'id'),
            'meetingId': require_meeting_id(require_field(raw, 'meetingId', 'approval')),
            'delegationId': optional_string(optional_field(raw, 'delegationId'), 'delegationId',
                                            max_length=128),
            'permission': category,
            'category': category,
            'summary': sanitize_summary(require_field(raw, 'summary', 'approval'), workspace),
            'scope': dict(sanitize_scope(optional_field(raw, 'scope'), workspace)),
            'status': status,
            'createdAt': created,
            'expiresAt': expires,
            'resolvedAt': optional_string(optional_field(raw, 'resolvedAt'), 'resolvedAt',
                                          max_length=32),
            'decision': None if optional_field(raw, 'decision') is None else require_enum(
                raw['decision'], 'decision', DECISION_VALUES),
            'consumed': consumed,
        }
        if record['resolvedAt']:
            require_timestamp(record['resolvedAt'], 'resolvedAt')
        return cls(record)


class ApprovalDecision:
    def __init__(self, approval_id, decision, decided_at):
        self.approval_id = approval_id
        self.decision = decision
        self.decided_at = decided_at

    def to_dict(self):
        return {
            'approvalId': self.approval_id,
            'decision': self.decision,
            'decidedAt': self.decided_at,
        }

    @classmethod
    def from_dict(cls, data):
        raw = require_mapping(data, 'decision')
        reject_unknown_fields(raw, DECISION_FIELDS, 'decision')
        return cls(
            approval_id=require_id(require_field(raw, 'approvalId', 'decision'), 'approvalId'),
            decision=require_enum(require_field(raw, 'decision', 'decision'), 'decision',
                                  DECISION_VALUES),
            decided_at=require_timestamp(require_field(raw, 'decidedAt', 'decision'), 'decidedAt'),
        )


def build_approval(*, approval_id, meeting_id, category, summary, created_at=None,
                   ttl_seconds=DEFAULT_TTL_SECONDS, delegation_id=None, scope=None,
                   workspace=None):
    created = created_at or _iso(_utcnow())
    expires = _iso(parse_timestamp(created, 'createdAt') + timedelta(seconds=int(ttl_seconds)))
    return ApprovalRecord.from_dict({
        'version': APPROVAL_VERSION,
        'id': approval_id,
        'meetingId': meeting_id,
        'delegationId': delegation_id,
        'permission': category,
        'category': category,
        'summary': summary,
        'scope': scope or {},
        'status': 'pending',
        'createdAt': created,
        'expiresAt': expires,
        'consumed': False,
    }, workspace=workspace)


def expire_if_needed(record, now=None):
    payload = record.to_dict() if hasattr(record, 'to_dict') else dict(record)
    if payload.get('status') != 'pending':
        return payload
    current = now or _utcnow()
    if current >= parse_timestamp(payload['expiresAt'], 'expiresAt'):
        payload['status'] = 'expired'
        payload['resolvedAt'] = _iso(current)
        payload['decision'] = None
    return payload


def event_request_payload(record):
    payload = record.public_dict() if hasattr(record, 'public_dict') else public_approval(record)
    return omit_none({
        'id': payload['id'],
        'permission': payload.get('category') or payload.get('permission'),
        'category': payload.get('category') or payload.get('permission'),
        'summary': payload.get('summary'),
        'createdAt': payload.get('createdAt'),
        'expiresAt': payload.get('expiresAt'),
        'status': payload.get('status'),
        'scope': payload.get('scope') or None,
        'delegationId': payload.get('delegationId'),
        'action': payload.get('category') or payload.get('permission'),
    })


def event_decision_payload(record, decided_at=None):
    payload = record.to_dict() if hasattr(record, 'to_dict') else dict(record)
    return {
        'approvalId': payload['id'],
        'decision': payload.get('decision') or payload.get('status'),
        'decidedAt': decided_at or payload.get('resolvedAt'),
    }
