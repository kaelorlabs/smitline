"""Validated runtime configuration for a Colleague AI meeting participant."""
from dataclasses import dataclass
import logging
import os
from pathlib import Path

from codex_tool import CODEX_MODELS
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
    default_codex_model: str
    web_search_enabled: bool
    codex_enabled: bool
    charts_enabled: bool
    workspace: str
    meeting_instructions: str
    camera_enabled: bool = True
    camera_default_on: bool = True
    camera_logo_data_uri: str = ''
    screen_share_enabled: bool = False
    screen_share_settings: dict = None
    voice: str = ''
    meeting_intro: bool = True
    owner_name: str = ''

    @classmethod
    def from_environ(cls, environ=None):
        env = dict(os.environ if environ is None else environ)
        state_path = (env.get('COLLEAGUE_RUNTIME_STATE') or '').strip()
        state = {}
        if state_path:
            state = read_json(state_path) or {}
            overlay = environ_from_state(state)
            env.update(overlay)
        name = env.get('COLLEAGUE_PARTICIPANT_NAME', 'Colleague AI').strip()
        if not name or len(name) > 80 or any(ord(char) < 32 for char in name):
            raise ValueError('COLLEAGUE_PARTICIPANT_NAME must contain 1–80 printable characters')

        model = env.get('COLLEAGUE_CODEX_MODEL', 'gpt-5.6-terra').strip()
        if model not in CODEX_MODELS:
            raise ValueError(f'COLLEAGUE_CODEX_MODEL must be one of: {", ".join(CODEX_MODELS)}')

        web_search = _boolean(env.get('COLLEAGUE_ENABLE_WEB_SEARCH'), True)
        codex = _boolean(env.get('COLLEAGUE_ENABLE_CODEX'), True)
        charts = _boolean(env.get('COLLEAGUE_ENABLE_CHARTS'), False)
        if charts and not codex:
            raise ValueError('COLLEAGUE_ENABLE_CHARTS requires COLLEAGUE_ENABLE_CODEX=1')
        workspace = env.get('COLLEAGUE_WORKSPACE', '').strip()
        if workspace and not Path(workspace).is_absolute():
            raise ValueError('COLLEAGUE_WORKSPACE must be an absolute path')
        meeting_instructions = env.get('COLLEAGUE_MEETING_INSTRUCTIONS', '').strip()
        if len(meeting_instructions) > 2000 or any(ord(char) < 32 for char in meeting_instructions):
            raise ValueError('COLLEAGUE_MEETING_INSTRUCTIONS must contain at most 2000 printable characters')
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
        # Never interpret a host filesystem path inside the meeting container.
        # Keep the complete default settings shape even when capture is disabled.
        # bridge.py publishes retention details for every meeting state.
        from screen_share import parse_screen_share_settings
        screen_share = parse_screen_share_settings({'enabled': False})
        raw_share = state.get('screenShare')
        if isinstance(raw_share, dict):
            screen_share = parse_screen_share_settings(raw_share)
        elif state.get('screenShareEnabled') is True:
            screen_share = parse_screen_share_settings({'enabled': True})
        return cls(name, model, web_search, codex, charts, workspace, meeting_instructions,
                   camera_enabled, camera_default_on, camera_logo,
                   screen_share['enabled'], screen_share,
                   voice=_voice(env),
                   meeting_intro=_boolean(env.get('COLLEAGUE_MEETING_INTRO'), True),
                   owner_name=owner_name(state, env))


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
