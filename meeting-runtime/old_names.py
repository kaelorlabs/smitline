"""Settings and files from before the product was called Smitline (it started as Colleague AI).

COLLEAGUE_* environment variables still work: each one sets the matching SMITLINE_* variable,
and wins over it, because an image default can set the SMITLINE_* name while the user's own
`docker run -e COLLEAGUE_...` cannot. migrate_data() renames the keys in .env files and moves
the private .colleague folder to .smitline, leaving a link so an older image still finds it.

Run as a script (python old_names.py ROOT...) it migrates each data root.
"""
import os
import re
import sys
import tempfile
from pathlib import Path

OLD_PREFIX = 'COLLEAGUE_'
NEW_PREFIX = 'SMITLINE_'
OLD_DIR = '.colleague'
NEW_DIR = '.smitline'
ENV_FILES = ('.env', '.env.meeting')
_ASSIGNMENT = re.compile(r'^(\s*(?:export\s+)?)([A-Za-z_][A-Za-z0-9_]*)(\s*=.*)$')


def adopt_old_settings(environ=None):
    """Copy COLLEAGUE_* variables to their SMITLINE_* names, in place."""
    environ = os.environ if environ is None else environ
    for key in [key for key in environ if key.startswith(OLD_PREFIX)]:
        environ[NEW_PREFIX + key[len(OLD_PREFIX):]] = environ[key]
    return environ


def renamed_env_text(text):
    """The .env text with COLLEAGUE_* keys renamed; a key already set under its new name is dropped."""
    lines = text.splitlines(keepends=True)
    keys = {match[2] for match in map(_ASSIGNMENT.match, (line.rstrip('\r\n') for line in lines)) if match}
    out = []
    for line in lines:
        match = _ASSIGNMENT.match(line.rstrip('\r\n'))
        if match and match[2].startswith(OLD_PREFIX):
            new_key = NEW_PREFIX + match[2][len(OLD_PREFIX):]
            if new_key in keys:
                continue
            line = match[1] + new_key + line[match.end(2):]
        out.append(line)
    return ''.join(out)


def _rewrite(path):
    try:
        text = path.read_text(encoding='utf-8')
    except (FileNotFoundError, IsADirectoryError, PermissionError):
        return False
    new = renamed_env_text(text)
    if new == text:
        return False
    mode = path.stat().st_mode & 0o777
    fd, tmp = tempfile.mkstemp(prefix=path.name + '.', suffix='.tmp', dir=path.parent)
    with os.fdopen(fd, 'w', encoding='utf-8') as handle:
        handle.write(new)
    os.chmod(tmp, mode or 0o600)
    os.replace(tmp, path)
    return True


def migrate_data(root):
    """Rename old keys in ROOT's .env files and move ROOT/.colleague to ROOT/.smitline."""
    root = Path(root)
    changed = [name for name in ENV_FILES if _rewrite(root / name)]
    old, new = root / OLD_DIR, root / NEW_DIR
    if old.is_dir() and not old.is_symlink() and not new.exists():
        old.rename(new)
        old.symlink_to(NEW_DIR)
        changed.append(OLD_DIR)
    return changed


if __name__ == '__main__':
    for arg in sys.argv[1:]:
        if Path(arg).is_dir():
            done = migrate_data(arg)
            if done:
                print(f'Renamed to Smitline names in {arg}: {", ".join(done)}', file=sys.stderr)
