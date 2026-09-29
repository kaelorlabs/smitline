"""Minimal Twilio Programmable Voice client and request-signature checks."""
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


def stream_twiml(stream_url, parameters, *, say=None, say_voice='Polly.Joanna'):
    """<Connect><Stream> keeps the call on our WebSocket; it ends when the socket closes."""
    parts = ['<?xml version="1.0" encoding="UTF-8"?><Response>']
    if say:
        parts.append(f'<Say voice={quoteattr(say_voice)}>{escape(say)}</Say>')
    parts.append(f'<Connect><Stream url={quoteattr(stream_url)}>')
    for name, value in parameters.items():
        parts.append(f'<Parameter name={quoteattr(name)} value={quoteattr(str(value))}/>')
    parts.append('</Stream></Connect></Response>')
    return ''.join(parts)


def dial_twiml(number, caller_id, *, timeout=30):
    return ('<?xml version="1.0" encoding="UTF-8"?><Response>'
            f'<Dial callerId={quoteattr(caller_id)} timeout="{int(timeout)}">{escape(number)}</Dial>'
            '</Response>')


def hangup_twiml():
    return '<?xml version="1.0" encoding="UTF-8"?><Response><Hangup/></Response>'


class TwilioClient:
    def __init__(self, account_sid, auth_token, *, request=None, base=API_BASE):
        if not account_sid or not auth_token:
            raise ValueError('Twilio account SID and auth token are required')
        self.account_sid = account_sid
        self.auth_token = auth_token
        self.base = base
        self._request = request or self._aiohttp_request

    async def _aiohttp_request(self, method, url, form=None):
        from aiohttp import BasicAuth, ClientSession, ClientTimeout
        auth = BasicAuth(self.account_sid, self.auth_token)
        headers = {'Content-Type': 'application/x-www-form-urlencoded'} if form else None
        async with ClientSession(timeout=ClientTimeout(total=20), auth=auth) as session:
            async with session.request(method, url, data=form, headers=headers) as response:
                body = await response.json(content_type=None)
                return response.status, body

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
                          record=False, timeout=30, time_limit=None):
        form = [('To', to), ('From', from_), ('Twiml', twiml), ('Timeout', str(int(timeout)))]
        if time_limit:
            form.append(('TimeLimit', str(int(time_limit))))
        if status_callback:
            form.append(('StatusCallback', status_callback))
            form.append(('StatusCallbackMethod', 'POST'))
            for event in ('initiated', 'ringing', 'answered', 'completed'):
                form.append(('StatusCallbackEvent', event))
        if amd_callback:
            form += [('MachineDetection', 'DetectMessageEnd'), ('AsyncAmd', 'true'),
                     ('AsyncAmdStatusCallback', amd_callback),
                     ('AsyncAmdStatusCallbackMethod', 'POST')]
        if record:
            form.append(('Record', 'true'))
        return await self._call('POST', 'Calls.json', urlencode(form))

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

    async def verified_caller_ids(self):
        body = await self._call('GET', 'OutgoingCallerIds.json?PageSize=50')
        return [item.get('phone_number') for item in body.get('outgoing_caller_ids') or ()
                if item.get('phone_number')]
