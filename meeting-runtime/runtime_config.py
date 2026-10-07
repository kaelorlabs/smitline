"""Validated runtime configuration for a Smitline meeting participant."""
from dataclasses import dataclass
import logging
import os

from meeting_intro import owner_name
from runtime_state import environ_from_state, read_json


logger = logging.getLogger('colleague.meeting')


def _voice(env):
    """COLLEAGUE_VOICE when it names a GPT-Live voice; anything else keeps the default."""
    name = (env.get('COLLEAGUE_VOICE') or '').strip()
    if not name:
        return ''
    from call_brief import available_voices
    if name in available_voices(env):
        return name
    logger.warning('Ignoring COLLEAGUE_VOICE=%r: not a GPT-Live voice', name[:40])
    return ''


def _boolean(value, default=False):
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in ('1', 'true', 'yes', 'on'):
        return True
    if normalized in ('0', 'false', 'no', 'off'):
        return False
    raise ValueError(f'Expected a boolean value, received {value!r}')


@dataclass(frozen=True)
class RuntimeConfig:
    participant_name: str
    meeting_instructions: str
    camera_enabled: bool = True
    camera_default_on: bool = True
    camera_logo_data_uri: str = ''
    voice: str = ''
    meeting_intro: bool = True
    owner_name: str = ''
    # Responses delegation: the backend model GPT-Live hands hard questions to.
    backend_model: str = ''
    web_search: bool = False
    # 'still' (default): one picture per state, almost no CPU. 'animated': the 30 fps canvas.
    camera_style: str = 'still'

    @classmethod
    def from_environ(cls, environ=None):
        env = dict(os.environ if environ is None else environ)
        state_path = (env.get('COLLEAGUE_RUNTIME_STATE') or '').strip()
        state = {}
        if state_path:
            state = read_json(state_path) or {}
            overlay = environ_from_state(state)
            env.update(overlay)
        name = env.get('COLLEAGUE_PARTICIPANT_NAME', 'Smitline').strip()
        if not name or len(name) > 80 or any(ord(char) < 32 for char in name):
            raise ValueError('COLLEAGUE_PARTICIPANT_NAME must contain 1–80 printable characters')

        from phone_prompts import DEFAULT_BACKEND_MODEL
        backend_model = (env.get('COLLEAGUE_MEETING_BACKEND_MODEL') or '').strip()
        if len(backend_model) > 128 or any(ord(char) < 33 for char in backend_model):
            raise ValueError('COLLEAGUE_MEETING_BACKEND_MODEL must be a model name')
        web_search = (env.get('COLLEAGUE_MEETING_WEB_SEARCH') or '').strip() == '1'
        meeting_instructions = env.get('COLLEAGUE_MEETING_INSTRUCTIONS', '').strip()
        if len(meeting_instructions) > 2000 or any(ord(char) < 32 for char in meeting_instructions):
            raise ValueError('COLLEAGUE_MEETING_INSTRUCTIONS must contain at most 2000 printable characters')
        camera_style = (env.get('COLLEAGUE_MEETING_CAMERA_STYLE') or 'still').strip().lower()
        if camera_style not in ('still', 'animated'):
            raise ValueError("COLLEAGUE_MEETING_CAMERA_STYLE must be 'still' or 'animated'")
        camera_enabled = True
        camera_default_on = True
        camera_logo = ''
        if 'cameraEnabled' in state:
            if not isinstance(state['cameraEnabled'], bool):
                raise ValueError('cameraEnabled must be a boolean')
            camera_enabled = state['cameraEnabled']
        if 'cameraDefaultOn' in state:
            if not isinstance(state['cameraDefaultOn'], bool):
                raise ValueError('cameraDefaultOn must be a boolean')
            camera_default_on = state['cameraDefaultOn']
        raw_avatar = state.get('cameraAvatarDataUri')
        if raw_avatar:
            try:
                from visual_presence import parse_avatar_data_uri
                camera_logo = parse_avatar_data_uri(raw_avatar)
            except ValueError:
                camera_logo = ''
        return cls(name, meeting_instructions,
                   camera_enabled, camera_default_on, camera_logo,
                   voice=_voice(env),
                   meeting_intro=_boolean(env.get('COLLEAGUE_MEETING_INTRO'), True),
                   owner_name=owner_name(state, env),
                   backend_model=backend_model or DEFAULT_BACKEND_MODEL,
                   web_search=web_search,
                   camera_style=camera_style)


def meeting_state_from_environ(environ=None):
    env = dict(os.environ if environ is None else environ)
    state_path = (env.get('COLLEAGUE_RUNTIME_STATE') or '').strip()
    if not state_path:
        return {}
    return read_json(state_path) or {}


def resolve_meeting_url(environ=None):
    env = dict(os.environ if environ is None else environ)
    state_path = (env.get('COLLEAGUE_RUNTIME_STATE') or '').strip()
    if state_path:
        overlay = environ_from_state(read_json(state_path) or {})
        if overlay.get('MEETING_URL'):
            return overlay['MEETING_URL']
    url = env.get('MEETING_URL')
    if not url:
        raise KeyError('MEETING_URL')
    return url
