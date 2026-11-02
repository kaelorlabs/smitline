"""Extension points around calls: owner, credentials, pre-call policy, usage, notification.

The defaults serve one local owner and read provider credentials from the
environment. A managed deployment supplies its own hooks through
SMITLINE_CALL_HOOKS=module:factory without changing the call service.
"""
from datetime import datetime, timezone
import importlib
import inspect
import os
import re


LOCAL_OWNER = 'local'
OWNER_ID = re.compile(r'^[a-z0-9][a-z0-9_-]{0,63}$')
# Only an internal API token (a hosted gateway) may name the owner per request.
OWNER_HEADER = 'X-Smitline-Owner'


class CallRefused(Exception):
    """A call or request is refused; the daemon answers `status` with code, message, and details."""

    def __init__(self, code, message, status=403, **details):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.details = details


def valid_owner(value):
    return isinstance(value, str) and bool(OWNER_ID.fullmatch(value))


def owner_required(env):
    """SMITLINE_REQUIRE_OWNER=1 (a hosted deployment): nothing may act as the local owner."""
    return str(env.get('SMITLINE_REQUIRE_OWNER') or '').strip() == '1'


def owner_settings(hooks, owner):
    """One owner's settings: hooks.settings(owner), else the installation's environment."""
    getter = getattr(hooks, 'settings', None)
    if callable(getter):
        return getter(owner)
    return getattr(hooks, 'environ', os.environ)


class LineNotReady(Exception):
    """A line cannot perform the requested action right now; the message says why."""


PHONE_AUDIO_MODES = ('relay', 'sip', 'sip-webhook')


def phone_audio(env):
    """How call audio travels: 'relay' (through this computer), 'sip' (OpenAI dials out
    through the provider's SIP trunk), or 'sip-webhook' (the provider dials and hands the
    call to OpenAI, which announces it with a webhook)."""
    mode = str(env.get('SMITLINE_PHONE_AUDIO') or '').strip().lower()
    return mode if mode in PHONE_AUDIO_MODES else 'relay'


def phone_provider(env):
    """'twilio' or 'signalwire': SMITLINE_PHONE_PROVIDER, else whichever account is set up."""
    chosen = str(env.get('SMITLINE_PHONE_PROVIDER') or '').strip().lower()
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
        names = ('SMITLINE_SIP_TRUNK_URL', 'SMITLINE_SIP_USERNAME', 'SMITLINE_SIP_PASSWORD')
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
    caller_id = _usable(env.get('SMITLINE_CALLER_ID'))
    values = {
        'apiBase': f'https://{space}/api/laml/2010-04-01' if space else '',
        'accountSid': str(env.get('SIGNALWIRE_PROJECT_ID') or '').strip(),
        'authToken': str(env.get('SIGNALWIRE_API_TOKEN') or '').strip(),
        'fromNumber': caller_id or number,
    }
    names = ('SIGNALWIRE_SPACE', 'SIGNALWIRE_PROJECT_ID', 'SIGNALWIRE_API_TOKEN',
             'SIGNALWIRE_FROM_NUMBER or SMITLINE_CALLER_ID')
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
                              f'SMITLINE_ALLOWED_CALLING_CODES has an entry that is not a calling '
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
        self._environ = os.environ if environ is None else environ
        self.env_file = env_file
        self.store = store
        self.notifier = notifier
        self.clock = clock
        # Per owner, made on first use; the local owner's files are the ones it always had.
        self._contact_books = {}
        self._do_not_call_lists = {}
        self._webhook_secrets = {}

    def now(self):
        return self.clock() if self.clock else datetime.now(timezone.utc)

    @property
    def environ(self):
        """Process environment overlaid with the project's .env, read fresh on each use."""
        merged = dict(read_env_file(self.env_file))
        merged.update({key: value for key, value in self._environ.items() if value})
        return merged

    def settings(self, owner):
        """What one owner's calls read (name, phone, voice, guardrails); here, the environment."""
        return self.environ

    def owner_for(self, request):
        """The owner a request acts for, from the credential the daemon verified.

        An internal API token names the owner with X-Smitline-Owner; a token created with
        an owner always acts as it, whatever its kind; everything else is the local owner.
        """
        auth = request.get('smitline_auth') if hasattr(request, 'get') else None
        token = (auth or {}).get('token') or {}
        bound = token.get('owner')
        named = (getattr(request, 'headers', None) or {}).get(OWNER_HEADER)
        if named is not None:
            if token.get('kind') != 'internal':
                raise CallRefused('owner_header_forbidden', f'Only an internal API token may '
                                  f'send {OWNER_HEADER}.', status=403)
            owner = named.strip()
            if bound and owner != bound:
                raise CallRefused('owner_header_forbidden', 'This API token acts for its own '
                                  'owner only.', status=403)
        else:
            owner = bound or LOCAL_OWNER
        if not valid_owner(owner):
            raise CallRefused('invalid_owner', 'An owner id is lowercase letters, digits, _ and -, '
                              'at most 64 characters.', status=400)
        if owner == LOCAL_OWNER and owner_required(self.environ):
            raise CallRefused('owner_required', 'This deployment serves accounts: authenticate '
                              'as one.', status=401)
        return owner

    def storage_root(self, owner):
        """Where an owner's files live: .smitline next to .env for the local owner, and
        .smitline/owners/<owner> for any other. None when the hooks keep no files."""
        from pathlib import Path
        if not self.env_file:
            return None
        base = Path(self.env_file).parent / '.smitline'
        if owner == LOCAL_OWNER:
            return base
        if not valid_owner(owner):
            raise ValueError('invalid owner id')
        return base / 'owners' / owner

    def _owner_file(self, owner, name):
        root = self.storage_root(owner)
        return root / name if root is not None else None

    def contact_book(self, owner):
        from contacts import ContactBook
        if owner not in self._contact_books:
            self._contact_books[owner] = ContactBook(self._owner_file(owner, 'contacts.json'))
        return self._contact_books[owner]

    def do_not_call_list(self, owner):
        from call_policy import DoNotCallList
        if owner not in self._do_not_call_lists:
            self._do_not_call_lists[owner] = DoNotCallList(self._owner_file(owner, 'do-not-call.json'))
        return self._do_not_call_lists[owner]

    def profile_path_for(self, owner):
        """The owner's profile sits next to .env, in the private .smitline folder."""
        return self._owner_file(owner, 'profile.json')

    @property
    def profile_path(self):
        return self.profile_path_for(LOCAL_OWNER)

    def profile(self, owner):
        from briefing import PROFILE_VERSION, load_profile
        path = self.profile_path_for(owner)
        return load_profile(path) if path else {'version': PROFILE_VERSION}

    def save_profile(self, owner, profile):
        from briefing import save_profile
        path = self.profile_path_for(owner)
        if path is None:
            raise ValueError('no place to keep the profile')
        return save_profile(path, profile)

    def webhook_secret(self, owner):
        """The key that signs an owner's webhooks: the installation's secret for the local
        owner, a secret file of its own for any other. None when there is none to use."""
        if owner == LOCAL_OWNER or self.storage_root(owner) is None:
            return getattr(self.notifier, 'secret', None)
        if owner not in self._webhook_secrets:
            from call_notify import load_or_create_secret
            self._webhook_secrets[owner] = load_or_create_secret(
                self._owner_file(owner, 'webhook.secret'))
        return self._webhook_secrets[owner]

    def owner_for_number(self, number):
        """The owner an incoming call to `number` belongs to; None turns the call away."""
        if owner_required(self.environ):
            return None
        return LOCAL_OWNER if self.environ.get('SMITLINE_ACCEPT_INBOUND') == '1' else None

    def credentials(self, owner, provider):
        # Provider keys are the installation's; the number calls show is the owner's.
        env = dict(self.environ)
        env['SMITLINE_CALLER_ID'] = self.settings(owner).get('SMITLINE_CALLER_ID') or ''
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
            caller_id = _usable(env.get('SMITLINE_CALLER_ID'))
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
                missing[-1] = 'TWILIO_FROM_NUMBER or SMITLINE_CALLER_ID'
            raise MissingCredentials(provider, missing)
        if provider == 'twilio':
            values['twilioNumber'] = twilio_number
        return values

    def brief_defaults(self, owner, payload):
        """Fill what setup already knows, so agents need not ask the user again."""
        env = self.settings(owner)
        defaults = {}
        name = _usable(env.get('SMITLINE_OWNER_NAME'))
        if name:
            defaults['onBehalfOf'] = name
        if payload.get('rehearsal') is True:
            phone = _usable(env.get('SMITLINE_OWNER_PHONE'))
            if phone:
                defaults['to'] = phone
        return defaults

    def brief_overrides(self, owner, payload):
        """Fields that replace what the agent sent, for a service that fixes who calls are for
        (a hosted one: {'onBehalfOf': the account holder's name}). Nothing here."""
        return {}

    def precheck(self, owner, brief):
        if brief.channel != 'phone':
            return
        env = self.settings(owner)
        if brief.rehearsal:
            # A rehearsal tells the voice the other side is the owner practicing;
            # it must never ring anyone else.
            phone = _usable(env.get('SMITLINE_OWNER_PHONE'))
            if not phone:
                raise CallRefused('owner_phone_missing', 'Rehearsals call your own phone. Set it '
                                  'first: smitline setup set SMITLINE_OWNER_PHONE +1...')
            if brief.to != phone:
                raise CallRefused('rehearsal_owner_only',
                                  'A rehearsal can only call your own phone (SMITLINE_OWNER_PHONE).')
            return
        allowed = _country_prefixes(env.get('SMITLINE_ALLOWED_CALLING_CODES'))
        if allowed and not brief.to.startswith(allowed):
            raise CallRefused(
                'destination_not_allowed',
                'That country is not in SMITLINE_ALLOWED_CALLING_CODES for this installation.')
        from call_policy import (
            PolicyProblem, check_cost, check_do_not_call, check_hours, check_repeats,
        )
        own_number = brief.to == _usable(env.get('SMITLINE_OWNER_PHONE'))
        now = self.now()
        try:
            check_cost(brief.to, env)
            check_do_not_call(brief.to, self.do_not_call_list(owner).entries())
            if not (own_number or brief.after_hours):
                check_hours(brief.to, now, env)
            if self.store is not None:
                # Only this owner's calls count toward its limits.
                check_repeats(brief.to, self.store.list(owner=owner, limit=None), now, env,
                              own_number=own_number)
        except PolicyProblem as problem:
            raise CallRefused(problem.code, problem.message) from problem

    def call_started(self, owner, record):
        """A call was recorded and its line is about to start. Raise CallRefused to stop it."""
        return None

    def do_not_call(self, owner):
        return {'numbers': self.do_not_call_list(owner).entries()}

    def update_do_not_call(self, owner, update):
        from call_policy import PolicyProblem
        try:
            return {'numbers': self.do_not_call_list(owner).update(update, now=self.now())}
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
        if owner == LOCAL_OWNER:
            return await self.notifier.deliver(call)
        return await self.notifier.deliver(call, secret=self.webhook_secret(owner))


def load_hooks(spec, **kwargs):
    """Build hooks from 'package.module:factory'; the factory receives the defaults' kwargs."""
    if not spec:
        return DefaultCallHooks(**kwargs)
    module_name, _, attribute = spec.partition(':')
    if not module_name or not attribute:
        raise ValueError('SMITLINE_CALL_HOOKS must look like module:factory')
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
