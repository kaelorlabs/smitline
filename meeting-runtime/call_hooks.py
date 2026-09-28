"""Extension points around calls: owner, credentials, pre-call policy, usage, notification.

The defaults serve one local owner and read provider credentials from the
environment. A managed deployment supplies its own hooks through
COLLEAGUE_CALL_HOOKS=module:factory without changing the call service.
"""
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
    def __init__(self, *, environ=None, env_file=None, store=None, notifier=None):
        self._environ = os.environ if environ is None else environ
        self.env_file = env_file
        self.store = store
        self.notifier = notifier

    @property
    def environ(self):
        """Process environment overlaid with the project's .env, read fresh on each use."""
        merged = dict(read_env_file(self.env_file))
        merged.update({key: value for key, value in self._environ.items() if value})
        return merged

    def owner_for(self, request):
        return 'local'

    def credentials(self, owner, provider):
        env = self.environ
        if provider == 'openai':
            names = ('OPENAI_API_KEY',)
            values = {'apiKey': env.get('OPENAI_API_KEY', '').strip()}
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
                                  'first: colleague setup set COLLEAGUE_OWNER_PHONE +1...')
            if brief.to != phone:
                raise CallRefused('rehearsal_owner_only',
                                  'A rehearsal can only call your own phone (COLLEAGUE_OWNER_PHONE).')
            return
        allowed = _country_prefixes(env.get('COLLEAGUE_ALLOWED_CALLING_CODES'))
        if allowed and not brief.to.startswith(allowed):
            raise CallRefused(
                'destination_not_allowed',
                'That country is not in COLLEAGUE_ALLOWED_CALLING_CODES for this installation.')

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
