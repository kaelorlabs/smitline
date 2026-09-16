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
CONTROL_DIR = '.colleague'
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


def context_index_path(root, meeting_id=None):
    if meeting_id is None:
        return Path(root) / 'context' / 'index.json'
    return run_root(root) / MEETINGS_DIR / require_meeting_id(meeting_id) / 'context.json'


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


def context_index_from_handoff(context):
    payload = context.to_dict() if hasattr(context, 'to_dict') else dict(context)
    reject_secrets(payload, 'context handoff')
    sources = []
    for name in ('objective', 'currentTask', 'summary'):
        text = payload.get(name)
        if isinstance(text, str) and text.strip():
            sources.append({'name': name, 'text': text})
    for label, values in (
        ('decisions', payload.get('decisions') or ()),
        ('constraints', payload.get('constraints') or ()),
        ('openQuestions', payload.get('openQuestions') or ()),
        ('importantFiles', payload.get('importantFiles') or ()),
    ):
        if not values:
            continue
        text = '\n'.join(str(item) for item in values)
        if text.strip():
            sources.append({'name': label, 'text': text})
    turns = payload.get('recentConversation') or ()
    if turns:
        lines = []
        for turn in turns:
            if isinstance(turn, dict):
                lines.append(f"{turn.get('role', 'unknown')}: {turn.get('text', '')}")
            else:
                lines.append(str(turn))
        sources.append({'name': 'recentConversation', 'text': '\n'.join(lines)})
    git = payload.get('git')
    if isinstance(git, dict) and git:
        sources.append({'name': 'git', 'text': json.dumps(git, ensure_ascii=False)})
    return {'version': 1, 'sources': sources}


def state_from_session(session):
    agent = session.agent_session
    context = session.context.to_dict() if hasattr(session.context, 'to_dict') else session.context
    permissions = (session.permissions.to_dict()
                   if hasattr(session.permissions, 'to_dict') else session.permissions)
    return {
        'version': STATE_VERSION,
        'meetingId': session.id,
        'platform': session.platform,
        'meetingUrl': session.meeting_url,
        'participantName': 'Colleague AI',
        'workspace': agent.workspace,
        'provider': agent.provider,
        'sessionId': agent.session_id,
        'defaultCodexModel': agent.model or 'gpt-5.6-terra',
        'webSearchEnabled': True,
        'codexEnabled': True,
        'chartsEnabled': False,
        'meetingInstructions': '',
        'context': context,
        'permissions': permissions,
    }


def environ_from_state(payload):
    if not payload:
        return {}
    flags = {True: '1', False: '0'}
    mapping = {}
    if payload.get('participantName'):
        mapping['COLLEAGUE_PARTICIPANT_NAME'] = str(payload['participantName'])
    if payload.get('defaultCodexModel'):
        mapping['COLLEAGUE_CODEX_MODEL'] = str(payload['defaultCodexModel'])
    if 'webSearchEnabled' in payload:
        mapping['COLLEAGUE_ENABLE_WEB_SEARCH'] = flags[bool(payload['webSearchEnabled'])]
    if 'codexEnabled' in payload:
        mapping['COLLEAGUE_ENABLE_CODEX'] = flags[bool(payload['codexEnabled'])]
    if 'chartsEnabled' in payload:
        mapping['COLLEAGUE_ENABLE_CHARTS'] = flags[bool(payload['chartsEnabled'])]
    if payload.get('meetingInstructions') is not None:
        mapping['COLLEAGUE_MEETING_INSTRUCTIONS'] = str(payload['meetingInstructions'])
    if payload.get('meetingUrl'):
        mapping['MEETING_URL'] = str(payload['meetingUrl'])
    return mapping
