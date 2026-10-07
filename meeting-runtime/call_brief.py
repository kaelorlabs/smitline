"""Call briefs: what an agent asks Smitline to do on a phone call or in a meeting."""
from dataclasses import dataclass, field
import ipaddress
import re
from urllib.parse import urlsplit

from meeting_urls import platform_for_url
from schema_validation import (
    optional_field, reject_secrets, reject_unknown_fields, require_bool, require_enum,
    require_int, require_mapping, require_string, require_string_list,
)


CHANNELS = ('phone', 'meeting')
BRIEF_FIELDS = (
    'channel', 'to', 'onBehalfOf', 'objective', 'context', 'questions', 'tone', 'contact',
    'mayAgreeTo', 'mustNotShare', 'successCriteria', 'language', 'voice', 'maxMinutes',
    'rehearsal', 'afterHours', 'record', 'notify', 'camera', 'task', 'carryFrom',
)
NOTIFY_FIELDS = ('webhookUrl',)
# A task ties calls toward one goal together, such as "roof repair quotes"; the agent names it.
TASK_FIELDS = ('id', 'title')
TASK_ID = re.compile(r'^[a-z0-9][a-z0-9-]{0,63}$')
# Which earlier calls' notes a call starts with. See docs/calls.md#earlier-calls.
CARRY_FIELDS = ('task', 'contact', 'calls')
CALL_ID = re.compile(r'^call-[0-9a-f]{16}$')
MAX_CARRY_CALLS = 10
# A meeting's virtual camera, as the console's manual start sets it. No avatarPath: a brief
# never names a file on the computer running Smitline.
CAMERA_FIELDS = ('enabled', 'defaultOn', 'avatarDataUri')
E164 = re.compile(r'^\+[1-9][0-9]{7,14}$')
VOICE = re.compile(r'^[a-z][a-z0-9_-]{1,31}$')
LANGUAGE = re.compile(r'^[a-z]{2,3}(?:-[A-Za-z0-9]{2,8})?$')
DEFAULT_MAX_MINUTES = {'phone': 10, 'meeting': 120}
MAX_MINUTES = {'phone': 60, 'meeting': 240}
MAX_CONTEXT = 6000
# Short numbers that reach emergency services or crisis lines somewhere in the world.
# Smitline never dials them: a person in trouble must reach help directly.
EMERGENCY_NUMBERS = frozenset({
    '000', '08', '061', '100', '101', '102', '108', '110', '111', '112', '113', '115', '117',
    '118', '119', '122', '123', '125', '133', '150', '155', '190', '191', '192', '193', '197',
    '199', '911', '988', '999', '1122', '10111', '15', '17', '18',
})

QUESTIONS = {
    'channel': 'Should I place a phone call or join a video meeting?',
    'to': 'What number should I call, or what is the meeting link?',
    'objective': 'What should the call achieve?',
    'onBehalfOf': 'Whose behalf am I calling on? I say this name when the call starts.',
}


class BriefIncomplete(ValueError):
    """Required brief fields are missing; each comes with a question for the user."""

    def __init__(self, missing):
        self.missing = tuple(missing)
        super().__init__('brief is missing ' + ', '.join(self.missing))

    def to_dict(self):
        return {
            'missing': [{'field': name, 'question': QUESTIONS[name]} for name in self.missing],
        }


def emergency_number(value):
    """True for an emergency or crisis short number, written with or without a country code."""
    digits = re.sub(r'\D', '', str(value or ''))
    if not digits or len(digits) > 6:
        return False
    return any(digits[cut:] in EMERGENCY_NUMBERS for cut in range(0, min(4, len(digits) - 1)))


def normalize_phone(value, name='to'):
    text = require_string(value, name, max_length=32)
    compact = re.sub(r'[\s().-]', '', text)
    if not E164.fullmatch(compact):
        raise ValueError(f'{name} must be an E.164 phone number such as +14155550142')
    return compact


def validate_webhook_url(value, name='notify.webhookUrl'):
    text = require_string(value, name, max_length=2048)
    try:
        url = urlsplit(text)
    except ValueError as error:
        raise ValueError(f'{name} is not a valid URL') from error
    if url.username or url.password:
        raise ValueError(f'{name} must not contain credentials')
    host = (url.hostname or '').lower()
    if url.scheme == 'https' and host:
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            return text
        # An https IP literal must be public; local receivers use http on localhost.
        if address.is_private or address.is_loopback or address.is_link_local or \
                address.is_reserved or address.is_multicast or address.is_unspecified:
            raise ValueError(f'{name} must not point at a private or local IP address over https')
        return text
    if url.scheme == 'http' and host in ('127.0.0.1', 'localhost', '::1'):
        return text
    raise ValueError(f'{name} must use https, or http on localhost')


def _optional_text(data, key, max_length):
    value = optional_field(data, key)
    if value is None:
        return None
    return require_string(value, key, allow_newlines=True, max_length=max_length)


def _optional_list(data, key):
    value = optional_field(data, key)
    if value is None:
        return ()
    return require_string_list(value, key, max_items=20, item_max_length=300)


@dataclass(frozen=True)
class CallBrief:
    channel: str
    to: str
    on_behalf_of: str
    objective: str
    context: str = None
    may_agree_to: tuple = ()
    must_not_share: tuple = ()
    success_criteria: str = None
    questions: tuple = ()
    tone: str = None
    contact: dict = field(default=None, compare=False)
    language: str = None
    voice: str = None
    max_minutes: int = None
    rehearsal: bool = False
    after_hours: bool = False
    record: bool = False
    webhook_url: str = None
    # Meetings only: {'enabled', 'defaultOn', 'avatarDataUri'}, as the meeting daemon takes it.
    camera: dict = field(default=None, compare=False)
    # {'id', 'title'}: the task this call serves.
    task: dict = field(default=None, compare=False)
    # {'task', 'contact', 'calls'}: which earlier calls' notes to start with.
    carry_from: dict = field(default=None, compare=False)
    # The earlier calls' notes this call starts with, resolved by the call service; never stored
    # in the brief (the call record keeps them as `carried`).
    carried: tuple = field(default=(), compare=False)

    @property
    def session_context(self):
        """Level-2 context as a structured object, whether the brief gave text or an object,
        with the notes of earlier calls this call starts with."""
        from briefing import parse_session_context
        context = parse_session_context(self.context)
        if self.carried:
            context = dict(context, earlierCalls=list(self.carried))
        return context

    @property
    def platform(self):
        return platform_for_url(self.to) if self.channel == 'meeting' else 'phone'

    def to_dict(self):
        data = {
            'channel': self.channel,
            'to': self.to,
            'onBehalfOf': self.on_behalf_of,
            'objective': self.objective,
            'context': self.context,
            'questions': list(self.questions) or None,
            'tone': self.tone,
            'contact': self.contact,
            'mayAgreeTo': list(self.may_agree_to),
            'mustNotShare': list(self.must_not_share),
            'successCriteria': self.success_criteria,
            'language': self.language,
            'voice': self.voice,
            'maxMinutes': self.max_minutes,
            'rehearsal': self.rehearsal,
            'afterHours': True if self.after_hours else None,
            'record': True if self.record else None,
            'notify': {'webhookUrl': self.webhook_url} if self.webhook_url else None,
            # The avatar image stays out of the stored brief, which every call listing returns.
            'camera': ({'enabled': self.camera['enabled'], 'defaultOn': self.camera['defaultOn']}
                       if self.camera else None),
            'task': self.task,
            'carryFrom': self.carry_from,
        }
        return {key: value for key, value in data.items() if value is not None}

    @classmethod
    def from_dict(cls, payload, *, environ=None):
        data = require_mapping(payload, 'brief')
        reject_unknown_fields(data, BRIEF_FIELDS, 'brief')
        reject_secrets(data, 'brief')
        missing = [name for name in ('channel', 'to', 'objective', 'onBehalfOf')
                   if not isinstance(data.get(name), str) or not data.get(name).strip()]
        if missing:
            raise BriefIncomplete(missing)
        channel = require_enum(data['channel'], 'channel', CHANNELS)
        if channel == 'phone':
            if emergency_number(data['to']):
                raise ValueError('Smitline never calls emergency or crisis numbers. If someone '
                                 'needs help, call the number yourself now.')
            to = normalize_phone(data['to'])
        else:
            to = require_string(data['to'], 'to', max_length=2048)
            platform_for_url(to)
        language = optional_field(data, 'language')
        if language is not None and not LANGUAGE.fullmatch(require_string(language, 'language', max_length=16)):
            raise ValueError('language must be a language tag such as en or pt-BR')
        voice = optional_field(data, 'voice')
        if voice is not None:
            voice = require_string(voice, 'voice', max_length=32)
            voices = available_voices(environ)
            if not VOICE.fullmatch(voice) or voice not in voices:
                raise ValueError('voice must be one of: ' + ', '.join(voices))
        max_minutes = optional_field(data, 'maxMinutes')
        max_minutes = (DEFAULT_MAX_MINUTES[channel] if max_minutes is None else
                       require_int(max_minutes, 'maxMinutes', min_value=1,
                                   max_value=MAX_MINUTES[channel]))
        rehearsal = optional_field(data, 'rehearsal')
        rehearsal = False if rehearsal is None else require_bool(rehearsal, 'rehearsal')
        if rehearsal and channel != 'phone':
            raise ValueError('rehearsal is only supported for phone calls')
        after_hours = optional_field(data, 'afterHours')
        after_hours = False if after_hours is None else require_bool(after_hours, 'afterHours')
        if after_hours and channel != 'phone':
            raise ValueError('afterHours is only for phone calls')
        record = optional_field(data, 'record')
        record = False if record is None else require_bool(record, 'record')
        if record and channel != 'phone':
            raise ValueError('record is only for phone calls')
        notify = optional_field(data, 'notify')
        webhook_url = None
        if notify is not None:
            notify = require_mapping(notify, 'notify')
            reject_unknown_fields(notify, NOTIFY_FIELDS, 'notify')
            if optional_field(notify, 'webhookUrl') is not None:
                webhook_url = validate_webhook_url(notify['webhookUrl'])
        context = optional_field(data, 'context')
        if isinstance(context, dict):
            from briefing import parse_session_context
            context = parse_session_context(context) or None
        else:
            context = _optional_text(data, 'context', MAX_CONTEXT)
        contact = optional_field(data, 'contact')
        if contact is not None:
            from briefing import _person
            contact = _person(require_mapping(contact, 'contact'))
        camera = optional_field(data, 'camera')
        if camera is not None:
            if channel != 'meeting':
                raise ValueError('camera is only for meetings')
            camera = require_mapping(camera, 'camera')
            reject_unknown_fields(camera, CAMERA_FIELDS, 'camera')
            from visual_presence import parse_camera_settings
            camera = parse_camera_settings(camera)
        task = optional_field(data, 'task')
        if task is not None:
            task = require_mapping(task, 'task')
            reject_unknown_fields(task, TASK_FIELDS, 'task')
            task_id = require_string(task.get('id'), 'task.id', max_length=64)
            if not TASK_ID.fullmatch(task_id):
                raise ValueError('task.id must be lowercase letters, digits, and dashes, such as roof-quotes-oct')
            task = {'id': task_id}
            if optional_field(data['task'], 'title') is not None:
                task['title'] = ' '.join(require_string(data['task']['title'], 'task.title', max_length=120).split())
        carry_from = optional_field(data, 'carryFrom')
        if carry_from is not None:
            carry_from = require_mapping(carry_from, 'carryFrom')
            reject_unknown_fields(carry_from, CARRY_FIELDS, 'carryFrom')
            parsed = {}
            for key in ('task', 'contact'):
                if optional_field(carry_from, key) is not None:
                    parsed[key] = require_bool(carry_from[key], f'carryFrom.{key}')
            if parsed.get('task') and task is None:
                raise ValueError('carryFrom.task needs the brief\'s task')
            if parsed.get('contact') and channel != 'phone':
                raise ValueError('carryFrom.contact is only for phone calls')
            if optional_field(carry_from, 'calls') is not None:
                calls = require_string_list(carry_from['calls'], 'carryFrom.calls',
                                            max_items=MAX_CARRY_CALLS, item_max_length=21)
                if not all(CALL_ID.fullmatch(call_id) for call_id in calls):
                    raise ValueError('carryFrom.calls must be call ids such as call-0123456789abcdef')
                parsed['calls'] = list(dict.fromkeys(calls))
            carry_from = parsed or None
        tone = _optional_text(data, 'tone', 200)
        return cls(
            channel=channel,
            to=to,
            on_behalf_of=require_string(data['onBehalfOf'], 'onBehalfOf', max_length=120),
            objective=require_string(data['objective'], 'objective', allow_newlines=True,
                                     max_length=1000),
            context=context,
            questions=_optional_list(data, 'questions'),
            tone=tone,
            contact=contact,
            may_agree_to=_optional_list(data, 'mayAgreeTo'),
            must_not_share=_optional_list(data, 'mustNotShare'),
            success_criteria=_optional_text(data, 'successCriteria', 500),
            language=language,
            voice=voice,
            max_minutes=max_minutes,
            rehearsal=rehearsal,
            after_hours=after_hours,
            record=record,
            webhook_url=webhook_url,
            camera=camera,
            task=task,
            carry_from=carry_from,
        )


def available_voices(environ=None):
    """Documented GPT-Live voices plus any listed in COLLEAGUE_EXTRA_VOICES."""
    import os
    from voice_core import GPT_LIVE_VOICES
    extra = (environ if environ is not None else os.environ).get('COLLEAGUE_EXTRA_VOICES', '')
    names = [name.strip() for name in extra.split(',') if VOICE.fullmatch(name.strip() or '-')]
    return tuple(dict.fromkeys(GPT_LIVE_VOICES + tuple(names)))


def default_voice(environ=None):
    """COLLEAGUE_VOICE when it names a known voice; otherwise GPT-Live's default."""
    import os
    from voice_core import DEFAULT_VOICE
    env = environ if environ is not None else os.environ
    voice = str(env.get('COLLEAGUE_VOICE') or '').strip()
    return voice if voice in available_voices(env) else DEFAULT_VOICE


def opening_line(brief, name=None):
    """How a phone call opens: a normal hello naming who the call is for. The reason follows."""
    hello = f'Hey {name}' if name else 'Hi'
    return f"{hello}, I'm calling on behalf of {brief.on_behalf_of} about"


def disclosure_line(brief):
    """The AI disclosure, said in the agent's second turn, before it asks for anything."""
    return f"I'm {brief.on_behalf_of}'s AI assistant"
