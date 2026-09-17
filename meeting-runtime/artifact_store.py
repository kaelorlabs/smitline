"""Host-only immutable artifact store with path containment."""
import json
import os
import secrets
from pathlib import Path

from schema_validation import omit_none, reject_secrets, require_id, require_meeting_id, require_string


ARTIFACT_KINDS = (
    'plan', 'patch', 'manifest', 'command-log', 'workspace-result', 'file',
    'git-commit', 'git-push',
)
SAFE_MEDIA = {
    'plan': 'application/json',
    'patch': 'text/plain',
    'manifest': 'application/json',
    'command-log': 'text/plain',
    'workspace-result': 'application/json',
    'file': 'application/octet-stream',
    'git-commit': 'application/json',
    'git-push': 'application/json',
}
MAX_BODY = 1_000_000


class ArtifactStore:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        os.chmod(self.root, 0o700)

    def _meeting_dir(self, meeting_id, create=False):
        meeting_id = require_meeting_id(meeting_id)
        directory = self.root / meeting_id
        if create:
            directory.mkdir(parents=True, exist_ok=True)
            os.chmod(directory, 0o700)
        return directory

    def _artifact_dir(self, meeting_id, artifact_id, create=False):
        artifact_id = require_id(artifact_id, 'artifactId')
        directory = self._meeting_dir(meeting_id, create=create) / artifact_id
        if '..' in artifact_id or '/' in artifact_id:
            raise ValueError('artifactId must not contain a path')
        if create:
            directory.mkdir(parents=True, exist_ok=True)
            os.chmod(directory, 0o700)
        return directory.resolve().relative_to(self.root.resolve()), directory

    def put(self, meeting_id, *, kind, body, description=None, media_type=None, artifact_id=None):
        if kind not in ARTIFACT_KINDS:
            raise ValueError('artifact kind is invalid')
        if isinstance(body, (dict, list)):
            reject_secrets(body, 'artifact')
            payload = json.dumps(body, indent=2, sort_keys=True)
            data = payload.encode('utf-8')
            media = media_type or 'application/json'
        elif isinstance(body, str):
            data = body.encode('utf-8')
            media = media_type or SAFE_MEDIA[kind]
        elif isinstance(body, bytes):
            data = body
            media = media_type or SAFE_MEDIA[kind]
        else:
            raise ValueError('artifact body is invalid')
        if len(data) > MAX_BODY:
            raise ValueError('artifact exceeds size limit')
        artifact_id = artifact_id or ('art-' + secrets.token_hex(6))
        _rel, directory = self._artifact_dir(meeting_id, artifact_id, create=True)
        meta = omit_none({
            'id': artifact_id,
            'meetingId': meeting_id,
            'kind': kind,
            'path': f'{meeting_id}/{artifact_id}',
            'mediaType': media,
            'bytes': len(data),
            'description': None if description is None else require_string(
                description, 'description', max_length=240, allow_newlines=False),
        })
        reject_secrets(meta, 'artifact meta')
        body_path = directory / 'body'
        meta_path = directory / 'meta.json'
        body_path.write_bytes(data)
        os.chmod(body_path, 0o600)
        meta_path.write_text(json.dumps(meta) + '\n', encoding='utf-8')
        os.chmod(meta_path, 0o600)
        return meta

    def list(self, meeting_id):
        directory = self._meeting_dir(meeting_id, create=False)
        if not directory.is_dir():
            return []
        items = []
        for child in sorted(directory.iterdir()):
            meta = child / 'meta.json'
            if not meta.is_file() or child.is_symlink() or meta.is_symlink():
                continue
            try:
                payload = json.loads(meta.read_text(encoding='utf-8'))
                reject_secrets(payload, 'artifact meta')
            except (OSError, ValueError):
                continue
            items.append(payload)
        return items

    def get(self, meeting_id, artifact_id):
        _rel, directory = self._artifact_dir(meeting_id, artifact_id, create=False)
        meta_path = directory / 'meta.json'
        if not directory.is_dir() or not meta_path.is_file() or directory.is_symlink():
            raise FileNotFoundError('artifact not found')
        payload = json.loads(meta_path.read_text(encoding='utf-8'))
        reject_secrets(payload, 'artifact meta')
        return payload

    def read_body(self, meeting_id, artifact_id):
        meta = self.get(meeting_id, artifact_id)
        _rel, directory = self._artifact_dir(meeting_id, artifact_id, create=False)
        body = directory / 'body'
        if body.is_symlink() or not body.is_file():
            raise FileNotFoundError('artifact body not found')
        data = body.read_bytes()
        if len(data) > MAX_BODY:
            raise ValueError('artifact exceeds size limit')
        return meta, data
