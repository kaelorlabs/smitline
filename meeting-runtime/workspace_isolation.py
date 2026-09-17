"""Isolated git worktree/copy for approved workspace mutations."""
import hashlib
import os
import shutil
import stat
import subprocess
from pathlib import Path

from workspace_actions import relative_workspace_path


DENY_NAMES = frozenset({
    '.env', '.env.local', '.env.meeting', '.colleague', '.git', 'id_rsa', 'id_ed25519',
    'credentials', 'credentials.json', 'daemon.auth',
})
DENY_SUFFIXES = ('.pem', '.key', '.p12', '.pfx')
DENY_PARTS = ('.git', '.colleague', 'profiles', 'recordings')
MAX_SEEDED_FILES = 400
MAX_FILE_BYTES = 1_000_000
MAX_PATCH_BYTES = 1_000_000


class IsolationError(ValueError):
    pass


def _run_git(workspace, args, check=True):
    try:
        completed = subprocess.run(
            ['git', '-C', str(workspace), *args],
            check=False, capture_output=True, text=True,
        )
    except FileNotFoundError as error:
        raise IsolationError('git is not available') from error
    if check and completed.returncode != 0:
        raise IsolationError(completed.stderr.strip() or 'git command failed')
    return completed


def is_git_workspace(workspace):
    root = Path(workspace)
    if not root.is_dir():
        return False
    try:
        result = _run_git(root, ['rev-parse', '--is-inside-work-tree'], check=False)
    except IsolationError:
        return False
    return result.returncode == 0 and result.stdout.strip() == 'true'


def denied_relative(path):
    text = str(path).replace('\\', '/')
    name = Path(text).name.lower()
    if name in DENY_NAMES or name.startswith('.env.'):
        return True
    if any(name.endswith(suffix) for suffix in DENY_SUFFIXES):
        return True
    parts = text.split('/')
    return any(part in DENY_PARTS for part in parts)


def contained_path(workspace, relative):
    root = Path(workspace).resolve()
    rel = relative_workspace_path(relative)
    raw = root / rel
    if raw.is_symlink():
        raise IsolationError('path must not be a symlink')
    for parent in raw.parents:
        if str(parent).startswith(str(root)) and parent.is_symlink():
            raise IsolationError('path must not be a symlink')
    candidate = raw.resolve()
    try:
        candidate.relative_to(root)
    except ValueError as error:
        raise IsolationError('path escapes the workspace') from error
    if denied_relative(rel):
        raise IsolationError('path is denied')
    return candidate


def file_digest(path):
    location = Path(path)
    if not location.is_file() or location.is_symlink():
        return None
    data = location.read_bytes()
    return hashlib.sha256(data).hexdigest()


def preimage_map(workspace, relatives):
    images = {}
    for relative in relatives:
        path = contained_path(workspace, relative)
        images[relative] = file_digest(path)
    return images


def _copy_file(source, dest, limit=MAX_FILE_BYTES):
    dest.parent.mkdir(parents=True, exist_ok=True)
    if source.is_symlink() or stat.S_ISLNK(source.lstat().st_mode):
        raise IsolationError('refused to copy a symlink')
    size = source.stat().st_size
    if size > limit:
        raise IsolationError('seeded file exceeds size limit')
    shutil.copyfile(source, dest)
    os.chmod(dest, 0o600)


def create_isolated_workspace(workspace, destination):
    source = Path(workspace).resolve()
    dest = Path(destination).resolve()
    if dest.exists():
        raise IsolationError('isolated workspace already exists')
    if not is_git_workspace(source):
        raise IsolationError('mutations require a Git workspace')
    dest.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(dest.parent, 0o700)
    _run_git(source, ['worktree', 'add', '--detach', str(dest), 'HEAD'])
    os.chmod(dest, 0o700)
    listed = _run_git(source, ['ls-files', '--others', '--exclude-standard'])
    extras = [line.strip() for line in listed.stdout.splitlines() if line.strip()]
    if len(extras) > MAX_SEEDED_FILES:
        extras = extras[:MAX_SEEDED_FILES]
    for relative in extras:
        if denied_relative(relative):
            continue
        try:
            src = contained_path(source, relative)
        except (IsolationError, ValueError):
            continue
        if not src.is_file():
            continue
        _copy_file(src, dest / relative)
    return dest


def remove_isolated_workspace(workspace, destination):
    dest = Path(destination)
    source = Path(workspace)
    if dest.exists():
        _run_git(source, ['worktree', 'remove', '--force', str(dest)], check=False)
        shutil.rmtree(dest, ignore_errors=True)


def staged_patch(workspace, relatives):
    root = Path(workspace).resolve()
    for relative in relatives:
        if denied_relative(relative):
            continue
        path = contained_path(root, relative)
        if path.exists():
            _run_git(root, ['add', '--', relative], check=False)
    completed = _run_git(root, ['diff', '--cached', '--binary'], check=False)
    text = completed.stdout or ''
    if len(text.encode('utf-8')) > MAX_PATCH_BYTES:
        raise IsolationError('patch exceeds size limit')
    return text


def apply_patch(workspace, patch, preimages):
    root = Path(workspace).resolve()
    for relative, digest in preimages.items():
        path = contained_path(root, relative)
        current = file_digest(path)
        if current != digest:
            raise IsolationError('working tree changed underfoot')
    if not patch.strip():
        return []
    completed = subprocess.run(
        ['git', '-C', str(root), 'apply', '--whitespace=nowarn'],
        input=patch, check=False, capture_output=True, text=True,
    )
    if completed.returncode != 0:
        raise IsolationError('patch does not apply cleanly')
    changed = []
    for relative in preimages:
        path = contained_path(root, relative)
        changed.append({
            'path': relative,
            'before': preimages[relative],
            'after': file_digest(path),
            'bytes': path.stat().st_size if path.is_file() else 0,
        })
    return changed
