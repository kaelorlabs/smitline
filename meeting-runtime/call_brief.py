"""Call briefs: what an agent asks Smitline to do on a phone call or in a meeting."""
from dataclasses import dataclass, field
import ipaddress
import re
import unicodedata
from urllib.parse import urlsplit

from meeting_urls import platform_for_url
from schema_validation import (
    optional_field, reject_secrets, reject_unknown_fields, require_bool, require_enum,
    require_int, require_mapping, require_string, require_string_list,
)


CHANNELS = ('phone', 'meeting')
# A meeting brief's `to` for a meeting Smitline starts itself: a Daily room of its own, made when
# the call is placed (CallService.create), whose link the call then carries as `to`.
NEW_ROOM = 'new'
BRIEF_FIELDS = (
    'channel', 'to', 'onBehalfOf', 'objective', 'context', 'questions', 'tone', 'contact',
    'mayAgreeTo', 'mustNotShare', 'successCriteria', 'language', 'voice', 'maxMinutes',
    'rehearsal', 'afterHours', 'record', 'notify', 'camera', 'task', 'carryFrom', 'canDo',
    'forRequest', 'dialIn',
)
# A meeting's `to` that is neither a link, "new", nor its dial-in number.
MEETING_TO_ERROR = ('to must be a Zoom, Teams, Google Meet, or Daily meeting link, "new", or the '
                    "meeting's dial-in number in E.164 form, such as +14155550142 (dialIn then "
                    'gives the meeting ID).')
NOTIFY_FIELDS = ('webhookUrl',)
# A task ties calls toward one goal together, such as "roof repair quotes"; the agent names it.
TASK_FIELDS = ('id', 'title')
TASK_ID = re.compile(r'^[a-z0-9][a-z0-9-]{0,63}$')
# Which earlier calls' notes a call starts with. See docs/calls.md#earlier-calls.
CARRY_FIELDS = ('task', 'contact', 'calls')
CALL_ID = re.compile(r'^call-[0-9a-f]{16}$')
MAX_CARRY_CALLS = 10
# The request on another call that this call serves: {'callId', 'requestId'}. The call then
# reports its progress and outcome to that request by itself.
FOR_REQUEST_FIELDS = ('callId', 'requestId')
REQUEST_ID = re.compile(r'^r[1-9][0-9]{0,2}$')
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
MAX_ON_BEHALF_OF = 120
# A call is always for a person, but agents sometimes put themselves in onBehalfOf ("ChatGPT",
# "Claude's assistant"). A value names an AI only when it is made of these words alone, so a
# person who shares a word with a product stays a person ("Alexa Smith", "Claude Monet").
AI_NAMES = frozenset((
    'chatgpt', 'openai', 'gpt', 'claude', 'anthropic', 'gemini', 'bard', 'copilot', 'grok', 'xai',
    'cursor', 'perplexity', 'siri', 'alexa', 'cortana', 'codex', 'deepseek', 'llama', 'mistral',
    'smitline',
))
AI_WORDS = frozenset(('ai', 'assistant', 'agent', 'bot', 'chatbot', 'robot', 'llm', 'model', 'app'))
# Words that may stand around those in a product's name without making it a person's: articles,
# makers and editions ("the assistant", "Google Gemini", "Claude Code", "GPT-4o mini").
AI_NAME_EXTRAS = frozenset((
    'the', 'a', 'an', 'my', 'your', 'our', 'this', 'by', 'from', 'of', 'and', 'user',
    'google', 'microsoft', 'github', 'meta', 'apple', 'amazon',
    'pro', 'plus', 'mini', 'turbo', 'ultra', 'flash', 'opus', 'sonnet', 'haiku', 'code', 'chat',
    'desktop', 'mobile', 'web', 'com', 'voice', 'virtual', 'personal', 'digital',
))
# Versions in a product's name: 4, 4o, 3.5 (as 3 and 5), o3, v2.
AI_VERSION = re.compile(r'^(?:v?\d{1,4}[a-z]?|[a-z]\d{1,2})$')
NOT_A_PERSON = ('onBehalfOf must name the person the call is for, such as the user, not an AI or '
                'app. Ask the user for their name and put it in onBehalfOf.')


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


def _ai_word(word):
    """A product's name or an AI word, also with a version stuck to it (gpt4o, claude3)."""
    if word in AI_NAMES or word in AI_WORDS:
        return True
    stem = re.fullmatch(r'([a-z]+)\d[a-z0-9]*', word)
    return bool(stem) and (stem[1] in AI_NAMES or stem[1] in AI_WORDS)


def _only_ai(text):
    """Do these words, together, name an AI: one product or AI word, and nothing else that could
    be part of a person's name? A.I. counts as AI; a possessive 's is dropped."""
    text = re.sub(r"'s\b", '', re.sub(r'\ba\.\s?i\b\.?', 'ai', text))
    words = re.findall(r'\w+', text)
    return any(_ai_word(word) for word in words) and all(
        _ai_word(word) or word in AI_NAME_EXTRAS or AI_VERSION.fullmatch(word) for word in words)


def names_an_ai(value):
    """True when onBehalfOf names an AI, a model, an app or a bot instead of a person: "ChatGPT",
    "Claude AI", "the assistant", "GPT-4o", or anyone's assistant ("Claude's assistant",
    "Robin's AI"). Only the whole value counts: "Alexa Smith" and "Claude Monet" are people."""
    if not isinstance(value, str):
        return False
    text = unicodedata.normalize('NFKC', value).casefold().replace('’', "'")
    _, possessive, owned = text.rpartition("'s ")
    return bool(possessive and _only_ai(owned)) or _only_ai(text)


def person_on_behalf_of(value, owner_name):
    """onBehalfOf as a call uses it. A person's name stays. The name of an AI, a model, an app or
    a bot becomes the owner's name (SMITLINE_OWNER_NAME), unless it is the owner's own name.
    Raises ValueError (NOT_A_PERSON) when there is no owner name to use instead."""
    if not isinstance(value, str) or len(value) > MAX_ON_BEHALF_OF or not names_an_ai(value):
        return value
    name = ' '.join(str(owner_name or '').split())
    if name.casefold() == ' '.join(value.split()).casefold():
        return value
    if not name:
        raise ValueError(NOT_A_PERSON)
    return name


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
    # What the calling agent will do for the people on the call when they ask ("look things up
    # in the repo and edit docs"). It limits tasks; questions the voice cannot answer go to the agent
    # too. Requests are off without it. See call_requests.py.
    can_do: str = None
    # {'callId', 'requestId'}: a request on another call this call serves; see FOR_REQUEST_FIELDS.
    for_request: dict = field(default=None, compare=False)
    # Meetings only: the phone way in, {'number', 'meetingId', 'keys', 'passcode'?, 'platform'?}
    # (dial_in_details.resolve). A meeting whose `to` is this number joins by phone; one with a
    # link can (dial_in.py).
    dial_in: dict = field(default=None, compare=False)

    @property
    def joins_by_phone(self):
        """A meeting brief with no link: its `to` is the meeting's dial-in number."""
        return self.channel == 'meeting' and self.to.startswith('+')

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
        if self.channel != 'meeting':
            return 'phone'
        if self.joins_by_phone:
            return (self.dial_in or {}).get('platform')  # zoom, teams, meet, or None: unknown
        return 'daily' if self.to == NEW_ROOM else platform_for_url(self.to)

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
            'canDo': self.can_do,
            'forRequest': self.for_request,
            'dialIn': dict(self.dial_in) if self.dial_in else None,
        }
        return {key: value for key, value in data.items() if value is not None}

    @classmethod
    def from_dict(cls, payload, *, environ=None):
        data = require_mapping(payload, 'brief')
        reject_unknown_fields(data, BRIEF_FIELDS, 'brief')
        reject_secrets(data, 'brief')
        missing = [name for name in ('channel', 'to', 'objective', 'onBehalfOf')
                   if not isinstance(data.get(name), str) or not data.get(name).strip()]
        if 'to' in missing and data.get('channel') == 'meeting' and isinstance(data.get('dialIn'), dict):
            missing.remove('to')  # a meeting joined by its dial-in alone: dialIn has the number
        if missing:
            raise BriefIncomplete(missing)
        channel = require_enum(data['channel'], 'channel', CHANNELS)
        dial_in = None
        if channel == 'phone':
            if optional_field(data, 'dialIn') is not None:
                raise ValueError('dialIn is only for meetings')
            if emergency_number(data['to']):
                raise ValueError('Smitline never calls emergency or crisis numbers. If someone '
                                 'needs help, call the number yourself now.')
            to = normalize_phone(data['to'])
        else:
            to, dial_in = _meeting_to(optional_field(data, 'to'), optional_field(data, 'dialIn'),
                                      environ)
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
        for_request = optional_field(data, 'forRequest')
        if for_request is not None:
            for_request = require_mapping(for_request, 'forRequest')
            reject_unknown_fields(for_request, FOR_REQUEST_FIELDS, 'forRequest')
            served_call = require_string(for_request.get('callId'), 'forRequest.callId', max_length=21)
            served_request = require_string(for_request.get('requestId'), 'forRequest.requestId',
                                            max_length=4)
            if not CALL_ID.fullmatch(served_call):
                raise ValueError('forRequest.callId must be a call id such as call-0123456789abcdef')
            if not REQUEST_ID.fullmatch(served_request):
                raise ValueError('forRequest.requestId must be a request id such as r1')
            for_request = {'callId': served_call, 'requestId': served_request}
        tone = _optional_text(data, 'tone', 200)
        can_do = optional_field(data, 'canDo')
        if can_do is not None:
            from call_requests import MAX_CAN_DO
            can_do = ' '.join(require_string(can_do, 'canDo', allow_newlines=True,
                                             max_length=MAX_CAN_DO).split()) or None
        return cls(
            channel=channel,
            to=to,
            on_behalf_of=require_string(data['onBehalfOf'], 'onBehalfOf',
                                        max_length=MAX_ON_BEHALF_OF),
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
            can_do=can_do,
            for_request=for_request,
            dial_in=dial_in,
        )


def _meeting_to(to, dial_in, environ):
    """A meeting brief's `to` and the dial-in it may join by (dial_in.resolve; None without one).
    `to` is the meeting's link, "new", or its dial-in number, which a one-tap number gives with
    its keys; without `to` the dial-in's number is it."""
    if isinstance(to, str) and to.strip().casefold() == NEW_ROOM:
        if dial_in is not None:
            raise ValueError('dialIn is for joining a meeting that already exists; leave it out '
                             'with to "new".')
        return NEW_ROOM, None
    import dial_in_details as dialing
    by_phone = False
    if isinstance(to, str) and not to.strip() and dial_in is not None:
        to = None
    if to is not None:
        to = require_string(to, 'to', max_length=2048)
        by_phone = dialing.phone_address(to) is not None
        if not by_phone:
            try:
                platform_for_url(to)
            except ValueError:
                raise ValueError(MEETING_TO_ERROR) from None
            if dial_in is None:
                return to, None
    dial = dialing.resolve(dial_in, to=to, settings=environ)
    return (dial['number'] if to is None or by_phone else to), dial


def available_voices(environ=None):
    """Documented GPT-Live voices plus any listed in SMITLINE_EXTRA_VOICES."""
    import os
    from voice_core import GPT_LIVE_VOICES
    extra = (environ if environ is not None else os.environ).get('SMITLINE_EXTRA_VOICES', '')
    names = [name.strip() for name in extra.split(',') if VOICE.fullmatch(name.strip() or '-')]
    return tuple(dict.fromkeys(GPT_LIVE_VOICES + tuple(names)))


def default_voice(environ=None):
    """SMITLINE_VOICE when it names a known voice; otherwise GPT-Live's default."""
    import os
    from voice_core import DEFAULT_VOICE
    env = environ if environ is not None else os.environ
    voice = str(env.get('SMITLINE_VOICE') or '').strip()
    return voice if voice in available_voices(env) else DEFAULT_VOICE


def opening_line(brief, name=None):
    """How a phone call opens: a normal hello naming who the call is for. The reason follows."""
    hello = f'Hey {name}' if name else 'Hi'
    return f"{hello}, I'm calling on behalf of {brief.on_behalf_of} about"


def disclosure_line(brief):
    """The AI disclosure, said in the agent's second turn, before it asks for anything."""
    return f"I'm {brief.on_behalf_of}'s AI assistant"
