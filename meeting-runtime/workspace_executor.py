"""Fail-closed workspace executor: plan, approve, isolate, apply, verify."""
import inspect
import json
import re
import secrets
from datetime import datetime, timezone
from pathlib import Path

from artifact_store import ArtifactStore
from command_runner import run_command, validate_command
from permissions import permission_mode
from workspace_actions import WorkspaceActionPlan, build_result
from workspace_isolation import (
    IsolationError, apply_patch, contained_path, create_isolated_workspace,
    is_git_workspace, preimage_map, remove_isolated_workspace, staged_patch,
)


PLAN_JSON = re.compile(r'\{.*\}', re.DOTALL)


async def _maybe_await(value):
    if inspect.isawaitable(value):
        return await value
    return value


def extract_plan_payload(text):
    if not isinstance(text, str) or not text.strip():
        raise ValueError('plan text is empty')
    try:
        payload = json.loads(text)
    except ValueError:
        match = PLAN_JSON.search(text)
        if not match:
            raise ValueError('plan is not machine-readable JSON')
        payload = json.loads(match.group(0))
    if not isinstance(payload, dict):
        raise ValueError('plan must be an object')
    return payload


class WorkspaceExecutor:
    def __init__(self, *, artifacts, approval_gate=None, mutate=None, emit=None, clock=None):
        self.artifacts = artifacts if isinstance(artifacts, ArtifactStore) else ArtifactStore(artifacts)
        self.approval_gate = approval_gate
        self.mutate = mutate
        self.emit = emit or (lambda *_args, **_kwargs: None)
        self.clock = clock

    def _id(self, prefix):
        return prefix + secrets.token_hex(6)

    def _emit(self, meeting_id, event_type, **payload):
        self.emit(meeting_id, event_type, **payload)

    async def authorize(self, permissions, category, summary, scope, request):
        mode = permission_mode(permissions, category)
        if category in ('commits', 'pushes') or mode == 'disabled':
            return {'status': 'denied', 'error': 'approval_denied', 'mode': mode or 'disabled'}
        if mode == 'allowed':
            return {'status': 'approved', 'mode': 'allowed', 'source': 'policy'}
        gate = getattr(request, 'approval_gate', None) or self.approval_gate
        if gate is None:
            return {'status': 'denied', 'error': 'approval_denied', 'mode': mode}
        result = await _maybe_await(gate({
            'category': category,
            'summary': summary,
            'scope': scope or {},
            'delegationId': getattr(request, 'delegation_id', None),
            'meetingId': getattr(request, 'meeting_id', None),
        }))
        if not isinstance(result, dict):
            return {'status': 'denied', 'error': 'approval_denied'}
        status = result.get('status') or result.get('decision')
        if status == 'approved':
            return result
        if status == 'expired':
            return {'status': 'expired', 'error': 'approval_expired'}
        return {'status': 'denied', 'error': 'approval_denied'}

    async def execute(self, request, plan, cancel=None):
        if not isinstance(plan, WorkspaceActionPlan):
            plan = WorkspaceActionPlan.from_dict(plan)
        meeting_id = plan.to_dict()['meetingId']
        workspace = Path(request.workspace).resolve()
        artifact_ids = []
        plan_meta = self.artifacts.put(
            meeting_id, kind='plan', body=plan.public_dict(),
            description='Workspace action plan', artifact_id=plan.to_dict()['id'])
        artifact_ids.append(plan_meta['id'])
        self._emit(meeting_id, 'artifact.created', artifact={
            'id': plan_meta['id'], 'kind': 'plan', 'path': plan_meta['path'],
            'createdAt': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
            'mediaType': plan_meta.get('mediaType'), 'description': 'Workspace action plan',
        })
        self._emit(meeting_id, 'workspace.action.planned', plan=plan.public_dict())
        if cancel is not None and cancel.is_set():
            return self._finish(plan, 'cancelled', 'Workspace action was cancelled',
                                artifact_ids, meeting_id)
        for category in plan.categories:
            if category in ('commits', 'pushes'):
                return self._finish(plan, 'unsupported', 'Commits and pushes are disabled',
                                    artifact_ids, meeting_id)
            decision = await self.authorize(
                request.permissions, category, plan.to_dict()['summary'],
                {'host': 'workspace'}, request)
            if decision.get('status') != 'approved':
                status = 'denied' if decision.get('error') == 'approval_denied' else (
                    'cancelled' if decision.get('error') == 'approval_cancelled' else 'failed')
                if decision.get('error') == 'approval_expired':
                    status = 'failed'
                return self._finish(plan, status, 'Workspace action was not approved',
                                    artifact_ids, meeting_id)
        if 'edits' in plan.categories and not is_git_workspace(workspace):
            return self._finish(plan, 'unsupported', 'Mutations require a Git workspace',
                                artifact_ids, meeting_id)
        if 'edits' in plan.categories and self.mutate is None:
            return self._finish(plan, 'unsupported', 'Workspace edits require a host executor',
                                artifact_ids, meeting_id)
        relatives = []
        for spec in plan.files:
            if spec.get('path'):
                relatives.append(spec['path'])
            elif spec.get('glob'):
                relatives.extend(self._expand_glob(workspace, spec['glob']))
        if len(relatives) > 32:
            relatives = relatives[:32]
        isolated = None
        try:
            if 'edits' in plan.categories:
                isolated = create_isolated_workspace(
                    workspace, Path(self.artifacts.root).parent / 'worktrees' / meeting_id / plan.to_dict()['id'])
                if request.on_progress:
                    request.on_progress('updating workspace')
                self._emit(meeting_id, 'workspace.action.started', planId=plan.to_dict()['id'])
                if self.mutate is not None:
                    await _maybe_await(self.mutate(isolated, plan, request, cancel))
                preimages = preimage_map(workspace, relatives)
                patch = staged_patch(isolated, relatives)
                patch_meta = self.artifacts.put(
                    meeting_id, kind='patch', body=patch or '',
                    description='Proposed workspace patch')
                artifact_ids.append(patch_meta['id'])
                self._emit(meeting_id, 'artifact.created', artifact={
                    'id': patch_meta['id'], 'kind': 'patch', 'path': patch_meta['path'],
                    'createdAt': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
                    'mediaType': patch_meta.get('mediaType'), 'description': 'Proposed workspace patch',
                })
                if cancel is not None and cancel.is_set():
                    return self._finish(plan, 'cancelled', 'Workspace action was cancelled',
                                        artifact_ids, meeting_id)
                try:
                    changed = apply_patch(workspace, patch, preimages) if patch.strip() else []
                except IsolationError as error:
                    if 'underfoot' in str(error) or 'does not apply' in str(error):
                        return self._finish(
                            plan, 'conflict', 'Local edits conflicted with the proposed patch',
                            artifact_ids, meeting_id, conflict=True)
                    raise
            else:
                changed = []
                self._emit(meeting_id, 'workspace.action.started', planId=plan.to_dict()['id'])
            command_results = []
            network_allowed = permission_mode(request.permissions, 'network') == 'allowed'
            if 'network' in plan.categories:
                network_allowed = True
            specs = list(plan.commands)
            verification = plan.to_dict().get('verification') or {}
            if verification.get('argv'):
                specs.append({'argv': verification['argv'], 'cwd': verification.get('cwd')})
            for spec in specs:
                try:
                    validate_command(spec['argv'], network_allowed=network_allowed,
                                     workspace=isolated or workspace)
                except IsolationError as error:
                    if 'commit' in str(error) or 'push' in str(error):
                        return self._finish(plan, 'unsupported', 'Commits and pushes are disabled',
                                            artifact_ids, meeting_id)
                    raise
                cwd_root = isolated or workspace
                result = run_command(
                    spec['argv'], cwd=spec.get('cwd'), workspace=cwd_root,
                    timeout=spec.get('timeoutSeconds') or 60,
                    network_allowed=network_allowed, cancel=cancel)
                log_meta = self.artifacts.put(
                    meeting_id, kind='command-log',
                    body=result.get('output') or '',
                    description='Command output')
                artifact_ids.append(log_meta['id'])
                command_results.append({
                    'name': result['name'],
                    'argv0': result['argv0'],
                    'exitCode': result['exitCode'],
                    'durationMs': result['durationMs'],
                    'artifactId': log_meta['id'],
                    'truncated': result.get('truncated'),
                })
            manifest = self.artifacts.put(
                meeting_id, kind='manifest',
                body={'changedFiles': changed, 'commands': command_results},
                description='Changed files and command results')
            artifact_ids.append(manifest['id'])
            summary = 'Updated workspace files' if changed else 'Ran workspace commands'
            if any(item.get('exitCode') not in (0, None) for item in command_results):
                summary = 'Workspace changes are ready; verification reported a failure'
            return self._finish(
                plan, 'completed', summary, artifact_ids, meeting_id,
                changed_files=changed, commands=command_results)
        except IsolationError as error:
            message = str(error)[:240] or 'Workspace action failed'
            status = 'cancelled' if 'cancel' in message else 'failed'
            return self._finish(plan, status, message, artifact_ids, meeting_id)
        finally:
            if isolated is not None:
                remove_isolated_workspace(workspace, isolated)

    def _finish(self, plan, status, summary, artifact_ids, meeting_id, *, changed_files=None,
                commands=None, conflict=False):
        payload = plan.to_dict()
        result = build_result(
            plan_id=payload['id'], meeting_id=meeting_id, delegation_id=payload['delegationId'],
            status=status, summary=summary, changed_files=changed_files, commands=commands,
            artifact_ids=artifact_ids, conflict=conflict)
        public = result.public_dict()
        self.artifacts.put(
            meeting_id, kind='workspace-result', body=public, description='Workspace action result')
        event = {
            'completed': 'workspace.action.completed',
            'failed': 'workspace.action.failed',
            'cancelled': 'workspace.action.cancelled',
            'denied': 'workspace.action.failed',
            'conflict': 'workspace.action.failed',
            'unsupported': 'workspace.action.failed',
        }.get(status, 'workspace.action.failed')
        extra = {'result': public} if event != 'workspace.action.cancelled' else {
            'planId': payload['id'], 'reason': summary[:64],
        }
        self._emit(meeting_id, event, **extra)
        return public

    def _expand_glob(self, workspace, pattern):
        root = Path(workspace).resolve()
        matches = []
        for path in sorted(root.glob(pattern)):
            if not path.is_file() or path.is_symlink():
                continue
            try:
                relative = path.resolve().relative_to(root).as_posix()
                contained_path(root, relative)
            except (IsolationError, ValueError):
                continue
            matches.append(relative)
            if len(matches) >= 32:
                break
        return matches
