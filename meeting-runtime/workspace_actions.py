"""Typed workspace action plans and results. Treat Codex output as untrusted."""
from approvals import sanitize_summary
from schema_validation import (
    omit_none, optional_bool, optional_field, optional_int, optional_string, reject_secrets,
    reject_unknown_fields, require_enum, require_field, require_id, require_mapping,
    require_meeting_id, require_string, require_version,
)


PLAN_VERSION = 1
PLAN_CATEGORIES = ('edits', 'commands', 'network')
FORBIDDEN_CATEGORIES = ('commits', 'pushes')
PLAN_STATUSES = (
    'planned', 'approved', 'running', 'completed', 'failed', 'cancelled', 'denied',
    'conflict', 'unsupported',
)
PLAN_FIELDS = (
    'version', 'id', 'meetingId', 'delegationId', 'summary', 'categories', 'files',
    'commands', 'verification', 'status',
)
FILE_SPEC_FIELDS = ('path', 'glob')
COMMAND_SPEC_FIELDS = ('argv', 'cwd', 'timeoutSeconds')
VERIFICATION_FIELDS = ('argv', 'cwd', 'summary')
RESULT_FIELDS = (
    'version', 'planId', 'meetingId', 'delegationId', 'status', 'summary',
    'changedFiles', 'commands', 'artifactIds', 'conflict',
)
CHANGED_FILE_FIELDS = ('path', 'before', 'after', 'bytes')
COMMAND_RESULT_FIELDS = ('name', 'argv0', 'exitCode', 'durationMs', 'artifactId', 'truncated')
MAX_FILES = 32
MAX_COMMANDS = 8
MAX_ARGV = 16
MAX_RELATIVE = 240
ALLOWED_ARGV0 = frozenset({
    'python', 'python3', 'pytest', 'node', 'npm', 'npx', 'go', 'cargo', 'ruff',
    'make', 'git',
})
FORBIDDEN_GIT = frozenset({
    'commit', 'push', 'merge', 'rebase', 'cherry-pick', 'reset', 'tag', 'stash',
    'submodule', 'filter-branch', 'update-ref',
})
SHELL_ARGV0 = frozenset({'bash', 'sh', 'zsh', 'fish', 'dash', 'csh', 'ksh'})


def relative_workspace_path(value, name='path'):
    text = require_string(value, name, max_length=MAX_RELATIVE)
    if text.startswith('/') or text.startswith('\\') or ':' in text[:2]:
        raise ValueError(f'{name} must be a workspace-relative path')
    parts = text.replace('\\', '/').split('/')
    if any(part in ('', '.', '..') for part in parts):
        raise ValueError(f'{name} must not contain parent or empty path segments')
    if text.startswith('.git/') or '/.git/' in text or text == '.git':
        raise ValueError(f'{name} cannot target .git')
    return '/'.join(parts)


def _file_spec(data):
    payload = require_mapping(data, 'files item')
    reject_unknown_fields(payload, FILE_SPEC_FIELDS, 'files item')
    path = optional_string(optional_field(payload, 'path'), 'files.path', max_length=MAX_RELATIVE)
    glob = optional_string(optional_field(payload, 'glob'), 'files.glob', max_length=MAX_RELATIVE)
    if bool(path) == bool(glob):
        raise ValueError('files item must have exactly one of path or glob')
    if path:
        return {'path': relative_workspace_path(path)}
    return {'glob': relative_workspace_path(glob, 'glob')}


def _argv(value, name):
    if not isinstance(value, list) or not value:
        raise ValueError(f'{name} must be a non-empty argv array')
    if len(value) > MAX_ARGV:
        raise ValueError(f'{name} exceeds {MAX_ARGV} items')
    items = [require_string(item, f'{name}[{index}]', max_length=128) for index, item in enumerate(value)]
    if '/' in items[0] or '\\' in items[0] or items[0] in ('.', '..'):
        raise ValueError(f'{name}[0] must be a command basename')
    if items[0] in SHELL_ARGV0:
        raise ValueError(f'{name}[0] cannot be a shell')
    if items[0] not in ALLOWED_ARGV0:
        raise ValueError(f'{name}[0] is not an allowlisted command')
    if items[0] == 'git' and len(items) > 1 and items[1] in FORBIDDEN_GIT:
        raise ValueError('git commit and push are disabled')
    return items


def _command_spec(data, name='commands item'):
    payload = require_mapping(data, name)
    reject_unknown_fields(payload, COMMAND_SPEC_FIELDS, name)
    cwd = optional_string(optional_field(payload, 'cwd'), f'{name}.cwd', max_length=MAX_RELATIVE)
    return omit_none({
        'argv': _argv(require_field(payload, 'argv', name), f'{name}.argv'),
        'cwd': None if cwd is None else relative_workspace_path(cwd, f'{name}.cwd'),
        'timeoutSeconds': optional_int(optional_field(payload, 'timeoutSeconds'),
                                       f'{name}.timeoutSeconds', min_value=1, max_value=120),
    })


def _verification(data):
    if data is None:
        return None
    payload = require_mapping(data, 'verification')
    reject_unknown_fields(payload, VERIFICATION_FIELDS, 'verification')
    argv = optional_field(payload, 'argv')
    return omit_none({
        'argv': None if argv is None else _argv(argv, 'verification.argv'),
        'cwd': None if optional_field(payload, 'cwd') is None else relative_workspace_path(
            payload['cwd'], 'verification.cwd'),
        'summary': optional_string(optional_field(payload, 'summary'), 'verification.summary',
                                   max_length=240),
    })


class WorkspaceActionPlan:
    def __init__(self, payload):
        self._payload = dict(payload)

    def to_dict(self):
        return omit_none(dict(self._payload))

    def public_dict(self):
        payload = self.to_dict()
        reject_secrets(payload, 'workspace plan')
        return payload

    @property
    def categories(self):
        return tuple(self._payload.get('categories') or ())

    @property
    def files(self):
        return tuple(self._payload.get('files') or ())

    @property
    def commands(self):
        return tuple(self._payload.get('commands') or ())

    @classmethod
    def from_dict(cls, data):
        raw = require_mapping(data, 'workspace plan')
        reject_unknown_fields(raw, PLAN_FIELDS, 'workspace plan')
        categories = require_field(raw, 'categories', 'workspace plan')
        if not isinstance(categories, list) or not categories:
            raise ValueError('workspace plan categories must be a non-empty array')
        if len(categories) > 4:
            raise ValueError('workspace plan categories exceed 4 items')
        parsed_categories = []
        for item in categories:
            if item in FORBIDDEN_CATEGORIES:
                raise ValueError('commits and pushes are not executable in this workspace action')
            parsed_categories.append(require_enum(item, 'categories item', PLAN_CATEGORIES))
        files = require_field(raw, 'files', 'workspace plan')
        if not isinstance(files, list):
            raise ValueError('workspace plan files must be an array')
        if len(files) > MAX_FILES:
            raise ValueError('workspace plan files exceed 32 items')
        parsed_files = [_file_spec(item) for item in files]
        commands = raw.get('commands') or []
        if not isinstance(commands, list):
            raise ValueError('workspace plan commands must be an array')
        if len(commands) > MAX_COMMANDS:
            raise ValueError('workspace plan commands exceed 8 items')
        parsed_commands = [_command_spec(item) for item in commands]
        status = 'planned' if optional_field(raw, 'status') is None else require_enum(
            raw['status'], 'status', PLAN_STATUSES)
        return cls({
            'version': require_version(raw.get('version', PLAN_VERSION)),
            'id': require_id(require_field(raw, 'id', 'workspace plan'), 'id'),
            'meetingId': require_meeting_id(require_field(raw, 'meetingId', 'workspace plan')),
            'delegationId': require_id(require_field(raw, 'delegationId', 'workspace plan'),
                                       'delegationId'),
            'summary': sanitize_summary(require_field(raw, 'summary', 'workspace plan')),
            'categories': list(dict.fromkeys(parsed_categories)),
            'files': parsed_files,
            'commands': parsed_commands,
            'verification': _verification(optional_field(raw, 'verification')),
            'status': status,
        })


class WorkspaceActionResult:
    def __init__(self, payload):
        self._payload = dict(payload)

    def to_dict(self):
        return omit_none(dict(self._payload))

    def public_dict(self):
        payload = self.to_dict()
        reject_secrets(payload, 'workspace result')
        return payload

    @classmethod
    def from_dict(cls, data):
        raw = require_mapping(data, 'workspace result')
        reject_unknown_fields(raw, RESULT_FIELDS, 'workspace result')
        changed = raw.get('changedFiles') or []
        if not isinstance(changed, list) or len(changed) > MAX_FILES:
            raise ValueError('changedFiles must be an array of at most 32 items')
        files = []
        for item in changed:
            payload = require_mapping(item, 'changedFiles item')
            reject_unknown_fields(payload, CHANGED_FILE_FIELDS, 'changedFiles item')
            files.append(omit_none({
                'path': relative_workspace_path(require_field(payload, 'path', 'changedFiles item')),
                'before': optional_string(optional_field(payload, 'before'), 'before', max_length=64),
                'after': optional_string(optional_field(payload, 'after'), 'after', max_length=64),
                'bytes': optional_int(optional_field(payload, 'bytes'), 'bytes', min_value=0,
                                      max_value=2_000_000),
            }))
        commands = raw.get('commands') or []
        if not isinstance(commands, list) or len(commands) > MAX_COMMANDS:
            raise ValueError('result commands must be an array of at most 8 items')
        parsed_commands = []
        for item in commands:
            payload = require_mapping(item, 'result commands item')
            reject_unknown_fields(payload, COMMAND_RESULT_FIELDS, 'result commands item')
            parsed_commands.append(omit_none({
                'name': require_string(require_field(payload, 'name', 'result commands item'),
                                       'name', max_length=64),
                'argv0': require_string(require_field(payload, 'argv0', 'result commands item'),
                                        'argv0', max_length=64),
                'exitCode': optional_int(optional_field(payload, 'exitCode'), 'exitCode',
                                         min_value=-1, max_value=255),
                'durationMs': optional_int(optional_field(payload, 'durationMs'), 'durationMs',
                                           min_value=0, max_value=600_000),
                'artifactId': optional_string(optional_field(payload, 'artifactId'), 'artifactId',
                                              max_length=128),
                'truncated': optional_bool(optional_field(payload, 'truncated'), 'truncated'),
            }))
        artifact_ids = raw.get('artifactIds') or []
        if not isinstance(artifact_ids, list) or len(artifact_ids) > 16:
            raise ValueError('artifactIds must be an array of at most 16 items')
        return cls({
            'version': require_version(raw.get('version', PLAN_VERSION)),
            'planId': require_id(require_field(raw, 'planId', 'workspace result'), 'planId'),
            'meetingId': require_meeting_id(require_field(raw, 'meetingId', 'workspace result')),
            'delegationId': require_id(require_field(raw, 'delegationId', 'workspace result'),
                                       'delegationId'),
            'status': require_enum(require_field(raw, 'status', 'workspace result'), 'status',
                                   PLAN_STATUSES),
            'summary': sanitize_summary(require_field(raw, 'summary', 'workspace result')),
            'changedFiles': files,
            'commands': parsed_commands,
            'artifactIds': [require_id(item, 'artifactId') for item in artifact_ids],
            'conflict': bool(raw.get('conflict')),
        })


def plan_needs_mutation(plan):
    categories = plan.categories if hasattr(plan, 'categories') else tuple(plan.get('categories') or ())
    return any(item in ('edits', 'commands') for item in categories)


def build_plan(*, plan_id, meeting_id, delegation_id, summary, categories, files=None,
               commands=None, verification=None, status='planned'):
    return WorkspaceActionPlan.from_dict({
        'version': PLAN_VERSION,
        'id': plan_id,
        'meetingId': meeting_id,
        'delegationId': delegation_id,
        'summary': summary,
        'categories': list(categories),
        'files': list(files or []),
        'commands': list(commands or []),
        'verification': verification,
        'status': status,
    })


def build_result(*, plan_id, meeting_id, delegation_id, status, summary, changed_files=None,
                 commands=None, artifact_ids=None, conflict=False):
    return WorkspaceActionResult.from_dict({
        'version': PLAN_VERSION,
        'planId': plan_id,
        'meetingId': meeting_id,
        'delegationId': delegation_id,
        'status': status,
        'summary': summary,
        'changedFiles': list(changed_files or []),
        'commands': list(commands or []),
        'artifactIds': list(artifact_ids or []),
        'conflict': bool(conflict),
    })
