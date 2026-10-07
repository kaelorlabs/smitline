"""Per-meeting runtime state files used by the host supervisor and container."""
from pathlib import Path
import json
import os
import stat

from schema_validation import reject_secrets, require_mapping, require_meeting_id


STATE_VERSION = 1
ACTIVE_NAME = 'active-meeting.json'
TOKEN_NAME = 'daemon.auth'
MEETINGS_DIR = 'meetings'
CONTROL_DIR = '.smitline'
DAEMON_DATA_NAME = 'daemon-data'
PORTAL_ACTIVE_NAME = 'portal-active.json'


def ensure_private_dir(path):
    directory = Path(path)
    directory.mkdir(parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    return directory


def write_private_file(path, body, *, mode=0o600):
    path = Path(path)
    ensure_private_dir(path.parent)
    payload = body if isinstance(body, (bytes, bytearray)) else str(body).encode('utf-8')
    tmp = path.with_name(path.name + '.tmp')
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    fd = os.open(tmp, flags, mode)
    try:
        os.write(fd, payload)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)
    os.chmod(path, mode)
    return path


def write_private_json(path, payload, *, mode=0o600):
    require_mapping(payload, 'runtime state')
    reject_secrets(payload, 'runtime state')
    encoded = json.dumps(payload, ensure_ascii=False, indent=2) + '\n'
    return write_private_file(path, encoded, mode=mode)


def read_json(path):
    path = Path(path)
    if not path.is_file():
        return None
    payload = json.loads(path.read_text(encoding='utf-8'))
    require_mapping(payload, 'runtime state')
    reject_secrets(payload, 'runtime state')
    return payload


def run_root(root):
    return ensure_private_dir(Path(root) / 'run')


def control_root(project_root):
    return ensure_private_dir(Path(project_root) / CONTROL_DIR)


def meeting_state_path(root, meeting_id):
    meeting_id = require_meeting_id(meeting_id)
    return run_root(root) / MEETINGS_DIR / meeting_id / 'runtime.json'


def active_meeting_path(project_root):
    return control_root(project_root) / ACTIVE_NAME


def daemon_token_path(project_root):
    return control_root(project_root) / TOKEN_NAME


def daemon_data_path(project_root):
    return control_root(project_root) / DAEMON_DATA_NAME


def portal_active_path(project_root):
    return control_root(project_root) / PORTAL_ACTIVE_NAME


def file_mode(path):
    return stat.S_IMODE(Path(path).stat().st_mode)


def state_from_session(session):
    context = session.context.to_dict() if hasattr(session.context, 'to_dict') else session.context
    payload = {
        'version': STATE_VERSION,
        'meetingId': session.id,
        'platform': session.platform,
        'meetingUrl': session.meeting_url,
        'participantName': 'Smitline',
        'meetingInstructions': '',
        'context': context,
    }
    if session.on_behalf_of:
        # Named in the meeting's opening AI disclosure (meeting_intro.owner_name).
        payload['onBehalfOf'] = session.on_behalf_of
    if session.voice:
        payload['voice'] = session.voice
    if session.camera_enabled is not None:
        payload['cameraEnabled'] = bool(session.camera_enabled)
    return payload


def environ_from_state(payload):
    if not payload:
        return {}
    mapping = {}
    if payload.get('participantName'):
        mapping['SMITLINE_PARTICIPANT_NAME'] = str(payload['participantName'])
    if payload.get('meetingInstructions') is not None:
        mapping['SMITLINE_MEETING_INSTRUCTIONS'] = str(payload['meetingInstructions'])
    if payload.get('meetingUrl'):
        mapping['MEETING_URL'] = str(payload['meetingUrl'])
    if payload.get('voice'):
        # Validated by runtime_config; an unknown name falls back to the default voice.
        mapping['SMITLINE_VOICE'] = str(payload['voice'])
    return mapping
