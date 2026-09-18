"""Typed Git commit and push requests. Never accept raw git argv or URLs."""
import re

from approvals import sanitize_summary
from schema_validation import (
    omit_none, optional_field, optional_string, reject_secrets, reject_unknown_fields,
    require_enum, require_field, require_id, require_mapping, require_meeting_id,
    require_string, require_version,
)
from workspace_actions import relative_workspace_path


ACTION_VERSION = 1
ACTION_KINDS = ('commit', 'push')
ACTION_STATUSES = (
    'requested', 'approved', 'running', 'completed', 'failed', 'cancelled', 'denied',
    'conflict', 'unsupported',
)
COMMIT_FIELDS = (
    'version', 'id', 'meetingId', 'delegationId', 'expectedHead', 'message', 'files',
    'artifactId', 'status',
)
PUSH_FIELDS = (
    'version', 'id', 'meetingId', 'delegationId', 'commitSha', 'remote', 'branch',
    'status',
)
FILE_HASH_FIELDS = ('path', 'sha256')
RESULT_FIELDS = (
    'version', 'operationId', 'meetingId', 'kind', 'status', 'summary',
    'commitSha', 'parentSha', 'treeSha', 'remote', 'branch', 'artifactIds', 'conflict',
)
SHA_PATTERN = re.compile(r'^[a-f0-9]{40}$')
REMOTE_PATTERN = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$')
BRANCH_PATTERN = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$')
MAX_FILES = 32
PROTECTED_BRANCHES = frozenset({'main', 'master', 'production', 'release'})


def require_sha(value, name):
    text = require_string(value, name, max_length=40)
    if not SHA_PATTERN.fullmatch(text):
        raise ValueError(f'{name} must be a 40-character lowercase Git SHA')
    return text


def require_remote_name(value, name='remote'):
    text = require_string(value, name, max_length=64)
    if '/' in text or '\\' in text or ':' in text or '@' in text or text.startswith('-'):
        raise ValueError(f'{name} must be a configured remote name, not a URL')
    if not REMOTE_PATTERN.fullmatch(text):
        raise ValueError(f'{name} must be a configured remote name')
    return text


def require_branch_name(value, name='branch'):
    text = require_string(value, name, max_length=200)
    if text.startswith('-') or text in ('HEAD', 'DETACHED') or '..' in text or text.endswith('/'):
        raise ValueError(f'{name} is not an exact local branch')
    if text.startswith('refs/') or ':' in text or '\\' in text or '@{' in text:
        raise ValueError(f'{name} must not be a refspec')
    if not BRANCH_PATTERN.fullmatch(text):
        raise ValueError(f'{name} is not an exact local branch')
    if text in PROTECTED_BRANCHES or text.split('/')[0] in PROTECTED_BRANCHES:
        raise ValueError(f'{name} is protected')
    return text


def _file_hash(data):
    payload = require_mapping(data, 'files item')
    reject_unknown_fields(payload, FILE_HASH_FIELDS, 'files item')
    return {
        'path': relative_workspace_path(require_field(payload, 'path', 'files item')),
        'sha256': require_string(require_field(payload, 'sha256', 'files item'), 'sha256',
                                 max_length=64),
    }


def _sha256_hex(value, name='sha256'):
    text = require_string(value, name, max_length=64)
    if not re.fullmatch(r'[a-f0-9]{64}', text):
        raise ValueError(f'{name} must be a lowercase sha256 hex digest')
    return text


class CommitRequest:
    def __init__(self, payload):
        self._payload = dict(payload)

    def to_dict(self):
        return omit_none(dict(self._payload))

    def public_dict(self):
        payload = self.to_dict()
        reject_secrets(payload, 'commit request')
        return payload

    @classmethod
    def from_dict(cls, data):
        raw = require_mapping(data, 'commit request')
        reject_unknown_fields(raw, COMMIT_FIELDS, 'commit request')
        files = require_field(raw, 'files', 'commit request')
        if not isinstance(files, list) or not files:
            raise ValueError('commit request files must be a non-empty array')
        if len(files) > MAX_FILES:
            raise ValueError('commit request files exceed 32 items')
        parsed_files = []
        for item in files:
            spec = _file_hash(item)
            spec['sha256'] = _sha256_hex(spec['sha256'])
            parsed_files.append(spec)
        message = sanitize_summary(require_field(raw, 'message', 'commit request'))
        if any(token in message.lower() for token in ('--force', '--amend', 'git commit', '-m ')):
            raise ValueError('commit message cannot include git flags')
        return cls({
            'version': require_version(raw.get('version', ACTION_VERSION)),
            'id': require_id(require_field(raw, 'id', 'commit request'), 'id'),
            'meetingId': require_meeting_id(require_field(raw, 'meetingId', 'commit request')),
            'delegationId': None if optional_field(raw, 'delegationId') is None else require_id(
                raw['delegationId'], 'delegationId'),
            'expectedHead': require_sha(require_field(raw, 'expectedHead', 'commit request'),
                                        'expectedHead'),
            'message': message,
            'files': parsed_files,
            'artifactId': optional_string(optional_field(raw, 'artifactId'), 'artifactId',
                                          max_length=128),
            'status': 'requested' if optional_field(raw, 'status') is None else require_enum(
                raw['status'], 'status', ACTION_STATUSES),
        })


class PushRequest:
    def __init__(self, payload):
        self._payload = dict(payload)

    def to_dict(self):
        return omit_none(dict(self._payload))

    def public_dict(self):
        payload = self.to_dict()
        reject_secrets(payload, 'push request')
        return payload

    @classmethod
    def from_dict(cls, data):
        raw = require_mapping(data, 'push request')
        reject_unknown_fields(raw, PUSH_FIELDS, 'push request')
        return cls({
            'version': require_version(raw.get('version', ACTION_VERSION)),
            'id': require_id(require_field(raw, 'id', 'push request'), 'id'),
            'meetingId': require_meeting_id(require_field(raw, 'meetingId', 'push request')),
            'delegationId': None if optional_field(raw, 'delegationId') is None else require_id(
                raw['delegationId'], 'delegationId'),
            'commitSha': require_sha(require_field(raw, 'commitSha', 'push request'), 'commitSha'),
            'remote': require_remote_name(require_field(raw, 'remote', 'push request')),
            'branch': require_branch_name(require_field(raw, 'branch', 'push request')),
            'status': 'requested' if optional_field(raw, 'status') is None else require_enum(
                raw['status'], 'status', ACTION_STATUSES),
        })


class GitActionResult:
    def __init__(self, payload):
        self._payload = dict(payload)

    def to_dict(self):
        return omit_none(dict(self._payload))

    def public_dict(self):
        payload = self.to_dict()
        reject_secrets(payload, 'git result')
        return payload

    @classmethod
    def from_dict(cls, data):
        raw = require_mapping(data, 'git result')
        reject_unknown_fields(raw, RESULT_FIELDS, 'git result')
        artifact_ids = raw.get('artifactIds') or []
        if not isinstance(artifact_ids, list) or len(artifact_ids) > 8:
            raise ValueError('artifactIds must be an array of at most 8 items')
        return cls({
            'version': require_version(raw.get('version', ACTION_VERSION)),
            'operationId': require_id(require_field(raw, 'operationId', 'git result'),
                                      'operationId'),
            'meetingId': require_meeting_id(require_field(raw, 'meetingId', 'git result')),
            'kind': require_enum(require_field(raw, 'kind', 'git result'), 'kind', ACTION_KINDS),
            'status': require_enum(require_field(raw, 'status', 'git result'), 'status',
                                   ACTION_STATUSES),
            'summary': sanitize_summary(require_field(raw, 'summary', 'git result')),
            'commitSha': None if optional_field(raw, 'commitSha') is None else require_sha(
                raw['commitSha'], 'commitSha'),
            'parentSha': None if optional_field(raw, 'parentSha') is None else require_sha(
                raw['parentSha'], 'parentSha'),
            'treeSha': None if optional_field(raw, 'treeSha') is None else require_sha(
                raw['treeSha'], 'treeSha'),
            'remote': None if optional_field(raw, 'remote') is None else require_remote_name(
                raw['remote']),
            'branch': None if optional_field(raw, 'branch') is None else require_branch_name(
                raw['branch']),
            'artifactIds': [require_id(item, 'artifactId') for item in artifact_ids],
            'conflict': bool(raw.get('conflict')),
        })


def build_result(*, operation_id, meeting_id, kind, status, summary, commit_sha=None,
                 parent_sha=None, tree_sha=None, remote=None, branch=None, artifact_ids=None,
                 conflict=False):
    return GitActionResult.from_dict({
        'version': ACTION_VERSION,
        'operationId': operation_id,
        'meetingId': meeting_id,
        'kind': kind,
        'status': status,
        'summary': summary,
        'commitSha': commit_sha,
        'parentSha': parent_sha,
        'treeSha': tree_sha,
        'remote': remote,
        'branch': branch,
        'artifactIds': list(artifact_ids or []),
        'conflict': bool(conflict),
    })


OPERATION_FIELDS = (
    'id', 'kind', 'meetingId', 'status', 'request', 'result', 'approvalId',
    'createdAt', 'updatedAt',
)


def _parse_request(kind, data):
    if kind == 'commit':
        return CommitRequest.from_dict(data).to_dict()
    return PushRequest.from_dict(data).to_dict()


class GitOperation:
    def __init__(self, payload):
        self._payload = dict(payload)

    def to_dict(self):
        return omit_none(dict(self._payload))

    def public_dict(self):
        payload = self.to_dict()
        reject_secrets(payload, 'git operation')
        return payload

    @classmethod
    def from_dict(cls, data):
        raw = require_mapping(data, 'git operation')
        reject_unknown_fields(raw, OPERATION_FIELDS, 'git operation')
        kind = require_enum(require_field(raw, 'kind', 'git operation'), 'kind', ACTION_KINDS)
        request = _parse_request(kind, require_field(raw, 'request', 'git operation'))
        result = None if optional_field(raw, 'result') is None else GitActionResult.from_dict(
            raw['result']).to_dict()
        return cls({
            'id': require_id(require_field(raw, 'id', 'git operation'), 'id'),
            'kind': kind,
            'meetingId': require_meeting_id(require_field(raw, 'meetingId', 'git operation')),
            'status': require_enum(require_field(raw, 'status', 'git operation'), 'status',
                                   ACTION_STATUSES),
            'request': request,
            'result': result,
            'approvalId': None if optional_field(raw, 'approvalId') is None else require_id(
                raw['approvalId'], 'approvalId'),
            'createdAt': require_string(require_field(raw, 'createdAt', 'git operation'),
                                        'createdAt', max_length=40),
            'updatedAt': require_string(require_field(raw, 'updatedAt', 'git operation'),
                                        'updatedAt', max_length=40),
        })
