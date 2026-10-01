"""Minimal Twilio Programmable Voice client and request-signature checks.

SignalWire's Compatibility API uses the same REST shapes, the same HMAC-SHA1
webhook signatures (keyed with its signing key), and the same media stream.
"""
import base64
import hashlib
import hmac
from urllib.parse import urlencode
from xml.sax.saxutils import escape, quoteattr


API_BASE = 'https://api.twilio.com/2010-04-01'


class TwilioError(Exception):
    def __init__(self, status, code, message):
        super().__init__(f'Twilio error {code}: {message}')
        self.status = status
        self.code = code
        self.message = message


def compute_signature(auth_token, url, params=None):
    """X-Twilio-Signature: base64 HMAC-SHA1 over the URL plus sorted POST parameters."""
    payload = url
    for key in sorted(params or {}):
        payload += key + str(params[key])
    digest = hmac.new(auth_token.encode('utf-8'), payload.encode('utf-8'), hashlib.sha1).digest()
    return base64.b64encode(digest).decode('ascii')


def valid_signature(auth_token, url, params, provided):
    if not provided or not auth_token:
        return False
    expected = compute_signature(auth_token, url, params)
    return hmac.compare_digest(expected, provided)


def stream_twiml(stream_url, parameters, *, say=None, say_voice='Polly.Joanna', realtime=False):
    """<Connect><Stream> keeps the call on our WebSocket; it ends when the socket closes.

    realtime (SignalWire only): its player manages packet delays and bursts itself instead
    of playing buffered audio with delay.
    """
    parts = ['<?xml version="1.0" encoding="UTF-8"?><Response>']
    if say:
        parts.append(f'<Say voice={quoteattr(say_voice)}>{escape(say)}</Say>')
    extra = ' realtime="true"' if realtime else ''
    parts.append(f'<Connect><Stream url={quoteattr(stream_url)}{extra}>')
    for name, value in parameters.items():
        parts.append(f'<Parameter name={quoteattr(name)} value={quoteattr(str(value))}/>')
    parts.append('</Stream></Connect></Response>')
    return ''.join(parts)


def dial_twiml(number, caller_id, *, timeout=30, fallback=None, say_voice='Polly.Joanna'):
    """Connect the caller to `number`; `fallback` is said if nobody answers."""
    after = f'<Say voice={quoteattr(say_voice)}>{escape(fallback)}</Say>' if fallback else ''
    return ('<?xml version="1.0" encoding="UTF-8"?><Response>'
            f'<Dial callerId={quoteattr(caller_id)} timeout="{int(timeout)}">{escape(number)}</Dial>'
            f'{after}</Response>')


def sip_dial_twiml(sip_uri, *, timeout=30):
    """Bridge the answered call to a SIP address (used to hand calls to GPT-Live).

    TLS is the default for <Sip>. G.711 comes first: the phone leg is 8 kHz anyway, and it
    avoids transcoding; Opus stays available because OpenAI prefers it.
    """
    return ('<?xml version="1.0" encoding="UTF-8"?><Response>'
            f'<Dial answerOnBridge="true" timeout="{int(timeout)}"><Sip codecs="PCMU,PCMA,OPUS">{escape(sip_uri)}</Sip></Dial>'
            '</Response>')


def hangup_twiml():
    return '<?xml version="1.0" encoding="UTF-8"?><Response><Hangup/></Response>'


def client_for(creds, **kwargs):
    """A REST client for phone credentials from the call hooks: Twilio or SignalWire."""
    return TwilioClient(creds['accountSid'], creds['authToken'],
                        base=creds.get('apiBase') or API_BASE,
                        flavor=creds.get('provider') or 'twilio', **kwargs)


def signing_keys(creds):
    """Secrets a webhook signature may be keyed with: SignalWire's signing key, then the token."""
    return [key for key in (creds.get('signingKey'), creds.get('authToken')) if key]


class TwilioClient:
    def __init__(self, account_sid, auth_token, *, request=None, base=API_BASE, flavor='twilio',
                 download=None):
        if not account_sid or not auth_token:
            raise ValueError('Twilio account SID and auth token are required')
        self.account_sid = account_sid
        self.auth_token = auth_token
        self.base = base
        # SignalWire documents no TimeLimit or AsyncAmdStatusCallbackMethod; the line
        # enforces the time limit itself, and AMD callbacks default to POST.
        self.flavor = flavor
        self._request = request or self._aiohttp_request
        self._download = download or self._aiohttp_download

    async def _aiohttp_request(self, method, url, form=None):
        from aiohttp import BasicAuth, ClientSession, ClientTimeout
        auth = BasicAuth(self.account_sid, self.auth_token)
        headers = {'Content-Type': 'application/x-www-form-urlencoded'} if form else None
        async with ClientSession(timeout=ClientTimeout(total=20), auth=auth) as session:
            async with session.request(method, url, data=form, headers=headers) as response:
                body = await response.json(content_type=None)
                return response.status, body

    async def _aiohttp_download(self, url):
        from aiohttp import BasicAuth, ClientSession, ClientTimeout
        auth = BasicAuth(self.account_sid, self.auth_token)
        async with ClientSession(timeout=ClientTimeout(total=120), auth=auth) as session:
            async with session.get(url) as response:
                body = await response.read()
                return response.status, body, response.headers.get('Content-Type') or ''

    async def download(self, url):
        """GET a media file, such as a recording, with the account credentials: (bytes, type)."""
        status, body, content_type = await self._download(url)
        if status >= 400:
            raise TwilioError(status, status, 'the recording could not be downloaded')
        return body, content_type

    def _url(self, path):
        return f'{self.base}/Accounts/{self.account_sid}/{path}'

    async def _call(self, method, path, form=None):
        status, body = await self._request(method, self._url(path), form)
        if status >= 400:
            body = body or {}
            raise TwilioError(status, body.get('code') or status,
                              body.get('message') or 'request failed')
        return body or {}

    async def create_call(self, *, to, from_, twiml, status_callback=None, amd_callback=None,
                          record=False, timeout=30, time_limit=None, recording_callback=None):
        form = [('To', to), ('From', from_), ('Twiml', twiml), ('Timeout', str(int(timeout)))]
        if time_limit and self.flavor == 'twilio':
            form.append(('TimeLimit', str(int(time_limit))))
        if status_callback:
            form.append(('StatusCallback', status_callback))
            form.append(('StatusCallbackMethod', 'POST'))
            for event in ('initiated', 'ringing', 'answered', 'completed'):
                form.append(('StatusCallbackEvent', event))
        if amd_callback:
            form += [('MachineDetection', 'DetectMessageEnd'), ('AsyncAmd', 'true'),
                     ('AsyncAmdStatusCallback', amd_callback)]
            if self.flavor == 'twilio':
                form.append(('AsyncAmdStatusCallbackMethod', 'POST'))
        if record:
            form += [('Record', 'true'), ('RecordingChannels', 'dual')]
            if recording_callback:
                form += [('RecordingStatusCallback', recording_callback),
                         ('RecordingStatusCallbackMethod', 'POST'),
                         ('RecordingStatusCallbackEvent', 'completed')]
        return await self._call('POST', 'Calls.json', urlencode(form))

    async def get_call(self, call_sid):
        return await self._call('GET', f'Calls/{call_sid}.json')

    async def update_call(self, call_sid, *, status=None, twiml=None):
        form = []
        if status:
            form.append(('Status', status))
        if twiml:
            form.append(('Twiml', twiml))
        return await self._call('POST', f'Calls/{call_sid}.json', urlencode(form))

    async def account(self):
        status, body = await self._request('GET', f'{self.base}/Accounts/{self.account_sid}.json', None)
        if status >= 400:
            body = body or {}
            raise TwilioError(status, body.get('code') or status, body.get('message') or 'request failed')
        return body

    async def incoming_numbers(self):
        body = await self._call('GET', 'IncomingPhoneNumbers.json?PageSize=50')
        return [item.get('phone_number') for item in body.get('incoming_phone_numbers') or ()
                if item.get('phone_number')]

    async def set_incoming_voice_url(self, number, voice_url):
        """Point one of the account's numbers at our inbound webhook (HTTP POST)."""
        from urllib.parse import quote
        body = await self._call('GET', f'IncomingPhoneNumbers.json?PhoneNumber={quote(number)}')
        matches = body.get('incoming_phone_numbers') or []
        if not matches:
            raise TwilioError(404, 'number_not_found', f'{number} is not a number in this Twilio account')
        form = urlencode([('VoiceUrl', voice_url), ('VoiceMethod', 'POST')])
        return await self._call('POST', f'IncomingPhoneNumbers/{matches[0]["sid"]}.json', form)

    async def verified_caller_ids(self):
        body = await self._call('GET', 'OutgoingCallerIds.json?PageSize=50')
        return [item.get('phone_number') for item in body.get('outgoing_caller_ids') or ()
                if item.get('phone_number')]
