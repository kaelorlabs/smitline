"""Extension points around calls: owner, credentials, pre-call policy, usage, notification.

The defaults serve one local owner and read provider credentials from the
environment. A managed deployment supplies its own hooks through
COLLEAGUE_CALL_HOOKS=module:factory without changing the call service.
"""
from datetime import datetime, timezone
import importlib
import inspect
import os


class CallRefused(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


class LineNotReady(Exception):
    """A line cannot perform the requested action right now; the message says why."""


PHONE_AUDIO_MODES = ('relay', 'sip', 'sip-webhook')


def phone_audio(env):
    """How call audio travels: 'relay' (through this computer), 'sip' (OpenAI dials out
    through the provider's SIP trunk), or 'sip-webhook' (the provider dials and hands the
    call to OpenAI, which announces it with a webhook)."""
    mode = str(env.get('COLLEAGUE_PHONE_AUDIO') or '').strip().lower()
    return mode if mode in PHONE_AUDIO_MODES else 'relay'


def phone_provider(env):
    """'twilio' or 'signalwire': COLLEAGUE_PHONE_PROVIDER, else whichever account is set up."""
    chosen = str(env.get('COLLEAGUE_PHONE_PROVIDER') or '').strip().lower()
    if chosen in ('twilio', 'signalwire'):
        return chosen
    if _usable(env.get('SIGNALWIRE_PROJECT_ID')) and not _usable(env.get('TWILIO_ACCOUNT_SID')):
        return 'signalwire'
    return 'twilio'


def signalwire_space(value):
    """The Space host from 'example', 'example.signalwire.com', or its https URL; '' if unusable."""
    text = str(value or '').strip().lower()
    for prefix in ('https://', 'http://'):
        if text.startswith(prefix):
            text = text[len(prefix):]
    text = text.split('/', 1)[0]
    if not text or not all(ch.isalnum() or ch in '.-' for ch in text) or text.startswith(('.', '-')):
        return ''
    return text if '.' in text else f'{text}.signalwire.com'


def _sip_credentials(env):
    mode = phone_audio(env)
    values = {'mode': mode}
    if mode == 'sip':
        names = ('COLLEAGUE_SIP_TRUNK_URL', 'COLLEAGUE_SIP_USERNAME', 'COLLEAGUE_SIP_PASSWORD')
        keys = ('trunkUrl', 'username', 'password')
    elif mode == 'sip-webhook':
        names = ('OPENAI_PROJECT_ID', 'OPENAI_WEBHOOK_SECRET')
        keys = ('projectId', 'webhookSecret')
    else:
        return values
    for name, key in zip(names, keys):
        values[key] = str(env.get(name) or '').strip()
    missing = [name for name, key in zip(names, keys) if not _usable(values[key])]
    if missing:
        raise MissingCredentials('sip', missing)
    return values


def _signalwire_credentials(env):
    """SignalWire's Compatibility API speaks Twilio's REST, webhook, and media-stream formats."""
    space = signalwire_space(env.get('SIGNALWIRE_SPACE'))
    number = _usable(env.get('SIGNALWIRE_FROM_NUMBER'))
    caller_id = _usable(env.get('COLLEAGUE_CALLER_ID'))
    values = {
        'apiBase': f'https://{space}/api/laml/2010-04-01' if space else '',
        'accountSid': str(env.get('SIGNALWIRE_PROJECT_ID') or '').strip(),
        'authToken': str(env.get('SIGNALWIRE_API_TOKEN') or '').strip(),
        'fromNumber': caller_id or number,
    }
    names = ('SIGNALWIRE_SPACE', 'SIGNALWIRE_PROJECT_ID', 'SIGNALWIRE_API_TOKEN',
             'SIGNALWIRE_FROM_NUMBER or COLLEAGUE_CALLER_ID')
    missing = [name for name, value in zip(names, values.values()) if not _usable(value)]
    if missing:
        raise MissingCredentials('signalwire', missing)
    values.update(provider='signalwire', twilioNumber=number,
                  signingKey=_usable(env.get('SIGNALWIRE_SIGNING_KEY')))
    return values


class MissingCredentials(Exception):
    def __init__(self, provider, missing):
        self.provider = provider
        self.missing = tuple(missing)
        super().__init__(f'{provider} is not configured: set {", ".join(self.missing)}')


def _country_prefixes(value):
    """Parse '1, 44' or '+1876'; unreadable entries fail closed instead of allowing everything."""
    prefixes = []
    for item in (value or '').split(','):
        item = item.strip().lstrip('+')
        if not item:
            continue
        if not item.isdigit() or len(item) > 7:
            raise CallRefused('invalid_allow_list',
                              f'COLLEAGUE_ALLOWED_CALLING_CODES has an entry that is not a calling '
                              f'code: {item[:20]!r}. Use digits such as 1,44 or 1415.')
        prefixes.append('+' + item)
    return tuple(prefixes)


def _usable(value):
    value = str(value or '').strip()
    return '' if value.startswith('replace_with') else value


def read_env_file(path):
    """Parse KEY=VALUE lines from an ignored .env file; never logs values."""
    values = {}
    if not path:
        return values
    try:
        with open(path, encoding='utf-8') as handle:
            lines = handle.read().splitlines()
    except (FileNotFoundError, NotADirectoryError, PermissionError):
        return values
    for line in lines:
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, _, value = line.partition('=')
        key = key.strip()
        if key.startswith('export '):
            key = key[len('export '):].strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in '"\'':
            value = value[1:-1]
        values[key] = value
    return values


class DefaultCallHooks:
    def __init__(self, *, environ=None, env_file=None, store=None, notifier=None, clock=None):
        from pathlib import Path
        from call_policy import DoNotCallList
        self._environ = os.environ if environ is None else environ
        self.env_file = env_file
        self.store = store
        self.notifier = notifier
        self.clock = clock
        self.do_not_call_list = DoNotCallList(
            Path(env_file).parent / '.colleague' / 'do-not-call.json' if env_file else None)

    def now(self):
        return self.clock() if self.clock else datetime.now(timezone.utc)

    @property
    def environ(self):
        """Process environment overlaid with the project's .env, read fresh on each use."""
        merged = dict(read_env_file(self.env_file))
        merged.update({key: value for key, value in self._environ.items() if value})
        return merged

    def owner_for(self, request):
        return 'local'

    @property
    def profile_path(self):
        """The owner's profile sits next to .env, in the private .colleague folder."""
        from pathlib import Path
        return Path(self.env_file).parent / '.colleague' / 'profile.json' if self.env_file else None

    def profile(self, owner):
        from briefing import PROFILE_VERSION, load_profile
        path = self.profile_path
        return load_profile(path) if path else {'version': PROFILE_VERSION}

    def save_profile(self, owner, profile):
        from briefing import save_profile
        if self.profile_path is None:
            raise ValueError('no place to keep the profile')
        return save_profile(self.profile_path, profile)

    def credentials(self, owner, provider):
        env = self.environ
        if provider == 'openai':
            names = ('OPENAI_API_KEY',)
            values = {'apiKey': env.get('OPENAI_API_KEY', '').strip()}
        elif provider == 'sip':
            return _sip_credentials(env)
        elif provider == 'twilio' and phone_provider(env) == 'signalwire':
            return _signalwire_credentials(env)
        elif provider == 'twilio':
            # Outgoing calls need a number to show: a Twilio number, or the owner's
            # own mobile verified in Twilio. Incoming calls need the Twilio number.
            twilio_number = _usable(env.get('TWILIO_FROM_NUMBER'))
            caller_id = _usable(env.get('COLLEAGUE_CALLER_ID'))
            names = ('TWILIO_ACCOUNT_SID', 'TWILIO_AUTH_TOKEN', 'TWILIO_FROM_NUMBER')
            values = {
                'accountSid': env.get('TWILIO_ACCOUNT_SID', '').strip(),
                'authToken': env.get('TWILIO_AUTH_TOKEN', '').strip(),
                'fromNumber': caller_id or twilio_number,
            }
        else:
            raise MissingCredentials(provider, ())
        missing = [name for name, value in zip(names, values.values()) if not _usable(value)]
        if missing:
            if provider == 'twilio' and missing[-1] == 'TWILIO_FROM_NUMBER':
                missing[-1] = 'TWILIO_FROM_NUMBER or COLLEAGUE_CALLER_ID'
            raise MissingCredentials(provider, missing)
        if provider == 'twilio':
            values['twilioNumber'] = twilio_number
        return values

    def brief_defaults(self, owner, payload):
        """Fill what setup already knows, so agents need not ask the user again."""
        env = self.environ
        defaults = {}
        name = _usable(env.get('COLLEAGUE_OWNER_NAME'))
        if name:
            defaults['onBehalfOf'] = name
        if payload.get('rehearsal') is True:
            phone = _usable(env.get('COLLEAGUE_OWNER_PHONE'))
            if phone:
                defaults['to'] = phone
        return defaults

    def precheck(self, owner, brief):
        if brief.channel != 'phone':
            return
        env = self.environ
        if brief.rehearsal:
            # A rehearsal tells the voice the other side is the owner practicing;
            # it must never ring anyone else.
            phone = _usable(env.get('COLLEAGUE_OWNER_PHONE'))
            if not phone:
                raise CallRefused('owner_phone_missing', 'Rehearsals call your own phone. Set it '
                                  'first: smitline setup set COLLEAGUE_OWNER_PHONE +1...')
            if brief.to != phone:
                raise CallRefused('rehearsal_owner_only',
                                  'A rehearsal can only call your own phone (COLLEAGUE_OWNER_PHONE).')
            return
        allowed = _country_prefixes(env.get('COLLEAGUE_ALLOWED_CALLING_CODES'))
        if allowed and not brief.to.startswith(allowed):
            raise CallRefused(
                'destination_not_allowed',
                'That country is not in COLLEAGUE_ALLOWED_CALLING_CODES for this installation.')
        from call_policy import (
            PolicyProblem, check_cost, check_do_not_call, check_hours, check_repeats,
        )
        own_number = brief.to == _usable(env.get('COLLEAGUE_OWNER_PHONE'))
        now = self.now()
        try:
            check_cost(brief.to, env)
            check_do_not_call(brief.to, self.do_not_call_list.entries())
            if not (own_number or brief.after_hours):
                check_hours(brief.to, now, env)
            if self.store is not None:
                check_repeats(brief.to, self.store.list(limit=None), now, env,
                              own_number=own_number)
        except PolicyProblem as problem:
            raise CallRefused(problem.code, problem.message) from problem

    def do_not_call(self, owner):
        return {'numbers': self.do_not_call_list.entries()}

    def update_do_not_call(self, owner, update):
        from call_policy import PolicyProblem
        try:
            return {'numbers': self.do_not_call_list.update(update, now=self.now())}
        except PolicyProblem as problem:
            raise ValueError(problem.message) from problem

    def record_usage(self, owner, call, usage):
        if self.store is None:
            return
        self.store.append_usage({
            'owner': owner,
            'callId': call['id'],
            'channel': call['channel'],
            'endedAt': call.get('endedAt'),
            'usage': usage,
        })

    async def notify(self, owner, call):
        if self.notifier is None:
            return None
        return await self.notifier.deliver(call)


def load_hooks(spec, **kwargs):
    """Build hooks from 'package.module:factory'; the factory receives the defaults' kwargs."""
    if not spec:
        return DefaultCallHooks(**kwargs)
    module_name, _, attribute = spec.partition(':')
    if not module_name or not attribute:
        raise ValueError('COLLEAGUE_CALL_HOOKS must look like module:factory')
    factory = getattr(importlib.import_module(module_name), attribute)
    hooks = factory(**kwargs)
    for name in ('owner_for', 'credentials', 'precheck', 'record_usage', 'notify'):
        if not callable(getattr(hooks, name, None)):
            raise ValueError(f'call hooks are missing {name}()')
    return hooks


async def maybe_await(value):
    if inspect.isawaitable(value):
        return await value
    return value
