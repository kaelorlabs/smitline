"""Fail-closed meeting permission policy: defaults, origin ceilings, and narrowing."""
from agent_sessions import MeetingPermissions
from session_continuity import CONTEXT, continuity_mode


WORKSPACE_RANK = {'none': 0, 'read-only': 1, 'workspace-write': 2}
COMMAND_RANK = {'disabled': 0, 'approval-required': 1, 'allowed': 2}
COMMIT_RANK = {'disabled': 0, 'approval-required': 1}
PERMISSION_AXES = ('workspace', 'commands', 'edits', 'network', 'commits', 'pushes')

DEFAULT_PERMISSIONS = MeetingPermissions(
    workspace='read-only',
    commands='disabled',
    edits='disabled',
    network='disabled',
    commits='disabled',
    pushes='disabled',
)
PORTAL_CEILING = MeetingPermissions(
    workspace='read-only',
    commands='approval-required',
    edits='approval-required',
    network='allowed',
    commits='disabled',
    pushes='disabled',
)
EXACT_CEILING = MeetingPermissions(
    workspace='workspace-write',
    commands='approval-required',
    edits='approval-required',
    network='allowed',
    commits='approval-required',
    pushes='approval-required',
)


def _as_permissions(value):
    if value is None:
        return DEFAULT_PERMISSIONS
    if isinstance(value, MeetingPermissions):
        return value
    return MeetingPermissions.from_dict(value)


def origin_ceiling(agent_session):
    mode = continuity_mode(agent_session)
    return PORTAL_CEILING if mode == CONTEXT else EXACT_CEILING


def _rank(axis, value):
    table = WORKSPACE_RANK if axis == 'workspace' else (
        COMMIT_RANK if axis in ('commits', 'pushes') else COMMAND_RANK)
    if value not in table:
        raise ValueError(f'permissions.{axis} is invalid')
    return table[value]


def validate_permission_combination(permissions):
    policy = _as_permissions(permissions)
    if policy.workspace == 'none':
        if policy.commands == 'allowed':
            raise ValueError('commands cannot be allowed when workspace is none')
        if policy.edits != 'disabled' or policy.commits != 'disabled' or policy.pushes != 'disabled':
            raise ValueError('workspace none cannot authorize edits, commits, or pushes')
    if policy.workspace == 'read-only' and policy.edits == 'allowed':
        raise ValueError('edits cannot be allowed without workspace-write')
    if policy.workspace != 'workspace-write':
        if policy.commits != 'disabled' or policy.pushes != 'disabled':
            raise ValueError('commits and pushes require workspace-write')
    if policy.pushes == 'approval-required' and policy.commits != 'approval-required':
        raise ValueError('pushes require commits to be approval-required')
    return policy


def is_narrower_or_equal(requested, origin):
    asked = _as_permissions(requested)
    ceiling = _as_permissions(origin)
    for axis in PERMISSION_AXES:
        if _rank(axis, getattr(asked, axis)) > _rank(axis, getattr(ceiling, axis)):
            return False
    return True


def bound_requested_permissions(requested, agent_session):
    asked = validate_permission_combination(
        DEFAULT_PERMISSIONS if requested is None else requested)
    ceiling = origin_ceiling(agent_session)
    if not is_narrower_or_equal(asked, ceiling):
        raise ValueError('requested permissions escalate the originating session authorization')
    return asked


def permission_mode(permissions, category, fallback='disabled'):
    policy = _as_permissions(permissions)
    return getattr(policy, category, fallback) if category in PERMISSION_AXES else fallback
