"""Owner isolation, route by route, and the per-owner contract of the call hooks.

ROUTES names one check for every route the daemon serves. A route added without an entry
fails test_every_route_has_an_isolation_entry, so whether it is per owner is decided when it
is written. The checks act as another owner on owner A's calls, contacts, profile, do-not-call
list and meeting, and then prove A's things are exactly as they were.
"""
import asyncio
import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from aiohttp.test_utils import TestClient, TestServer

from api_tokens import ApiTokenStore
from call_hooks import CallRefused, DefaultCallHooks
from call_notify import WebhookNotifier, signature
from call_service import CallService
from call_store import CallStore, new_call_id
from meeting_line import meeting_environment
from phone_gateway import create_gateway_app
from runtime_daemon import create_app
from test_call_service import DAYTIME, ZOOM, FakeSummarizer, brief
from test_phone import ENV as PHONE_ENV
from test_phone import PUBLIC, FakeLive, FakeTwilioSocket, PhoneHarness, until
from test_runtime_daemon import FakeSupervisor
from test_schemas import context_payload
from twilio_client import compute_signature


A, B = 'acct_a', 'acct_b'
A_NUMBER = '+14155550101'      # called by owner A and saved in A's contacts
A_LIVE_NUMBER = '+14155550102'  # owner A's call in progress
A_BLOCKED = '+14155550177'     # on owner A's do-not-call list
LAUNCH = {'Authorization': 'Bearer launch-token'}
BAD_OWNERS = ('../x', '..', 'ACCT_A', 'Acct_a', 'a' * 65, '', '   ', 'acct a', '-a', '_a',
              'acct.a', 'acct/a', 'acct_a/../acct_b', '%2e%2e', 'acct_a;acct_b')


class AsOwner(dict):
    """A request as the daemon hands it to owner_for: an internal token naming the owner."""

    def __init__(self, owner):
        super().__init__(smitline_auth={'kind': 'api', 'token': {'id': 'tok-1', 'kind': 'internal'}})
        self.headers = {'X-Smitline-Owner': owner}


class GatedLine:
    """A phone line whose calls each wait for their own gate, so one call can stay live while
    another finishes, and which records everything asked of it."""
    channel = 'phone'

    def __init__(self):
        self.gates = {}
        self.contexts = {}
        self.instructions = []
        self.ended = []
        self.transfers = []
        self.downloads = []
        self.connect = True
        self.fail = None

    def gate(self, call_id):
        return self.gates.setdefault(call_id, asyncio.Event())

    def ready(self, hooks, owner, brief):
        hooks.credentials(owner, 'openai')

    async def start(self, ctx):
        self.contexts[ctx.call_id] = ctx
        if self.fail is not None:
            raise self.fail
        ctx.set_status('connecting')
        if self.connect:
            ctx.link(providerCallSid='CA' + ctx.call_id[-8:])
            ctx.set_status('in_progress')
            ctx.add_transcript('agent', "Hi, this is Robin's AI assistant.")
        await self.gate(ctx.call_id).wait()
        await ctx.finish('hangup', usage={'voiceSeconds': 30, 'phoneSeconds': 31})

    async def instruct(self, call_id, text, *, silent=False):
        self.instructions.append((call_id, text))
        return True

    async def end(self, call_id):
        self.ended.append(call_id)
        if not self.connect:
            return False
        if getattr(self, 'ends_when_asked', True):
            self.gate(call_id).set()
        return True

    async def transfer(self, call_id):
        self.transfers.append(call_id)
        return {'transferred': True}

    async def recording_audio(self, credentials, record, fmt):
        self.downloads.append(record['id'])
        return b'RIFF....WAVE', 'audio/wav'


class PerOwnerSettings(DefaultCallHooks):
    """Hosted-style hooks: each owner has its own settings over the installation's."""
    OVERRIDES = {}

    def settings(self, owner):
        return {**self.environ, **self.OVERRIDES.get(owner, {})}


class Counting(PerOwnerSettings):
    """Records every money-relevant hook call."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.usage = []
        self.started = []
        self.notified = []
        self.refuse_start = None

    def call_started(self, owner, record):
        self.started.append((owner, record['id']))
        if self.refuse_start is not None:
            raise self.refuse_start

    def record_usage(self, owner, call, usage):
        self.usage.append((owner, call['id'], call['status'], dict(usage or {})))

    async def notify(self, owner, call):
        self.notified.append((owner, call['id']))
        return None


class OwnersDaemon:
    """The daemon with the launch token (local), an internal gateway token, a standard token,
    and a token created for owner B."""

    def __init__(self, temp, hooks_class=PerOwnerSettings, environ=None):
        self.root = Path(temp)
        self.store = CallStore(self.root / 'calls')
        self.hooks = hooks_class(environ=dict(environ or {'OPENAI_API_KEY': 'sk-test'}),
                                 env_file=self.root / '.env', store=self.store, clock=DAYTIME)
        self.line = GatedLine()
        self.service = CallService(self.store, hooks=self.hooks, lines={'phone': self.line},
                                   summarizer_factory=lambda owner: FakeSummarizer(), environ={},
                                   shutdown_grace=1.0)
        self.tokens = ApiTokenStore(self.root / 'api-tokens.json')
        self.internal, _ = self.tokens.create('gateway', kind='internal')
        self.standard, _ = self.tokens.create('script')
        self.owned_b, _ = self.tokens.create('account b', owner=B)
        self.supervisor = FakeSupervisor()
        self.stops = []
        self.app = create_app(root=self.root / 'daemon', auth_token='launch-token',
                              call_service=self.service, api_tokens=self.tokens,
                              supervisor=self.supervisor, on_stop=lambda: self.stops.append(1),
                              sse_heartbeat_interval=0.05)
        self.client = TestClient(TestServer(self.app))

    def as_owner(self, owner):
        return {'Authorization': f'Bearer {self.internal}', 'X-Smitline-Owner': owner}

    def bearer(self, token, **headers):
        return {'Authorization': f'Bearer {token}', **headers}

    async def start(self):
        await self.client.start_server()
        return self

    async def close(self):
        await self.service.shutdown()
        await self.client.close()

    async def seed_owner_a(self):
        """Owner A's things: a finished call with a recording, a live call, a saved contact,
        a profile, a do-not-call entry, and a meeting."""
        a = self.as_owner(A)
        response = await self.client.post('/v1/calls', json=brief(to=A_NUMBER), headers=a)
        assert response.status == 201, await response.text()
        done = (await response.json())['id']
        self.line.gate(done).set()
        await self.service.wait(done, owner=A, timeout=5)
        self.service.attach_recording(done, sid='RE1', url='https://api.twilio.com/2010-04-01/'
                                      'Accounts/AC1/Recordings/RE1')
        live = (await (await self.client.post('/v1/calls', json=brief(to=A_LIVE_NUMBER),
                                              headers=a)).json())['id']
        await until(lambda: self.store.get(live)['status'] == 'in_progress')
        for method, path, body in (('PATCH', f'/v1/contacts/{A_NUMBER}', {'name': 'Sam', 'notes': 'A only'}),
                                   ('PATCH', '/v1/profile', {'about': 'Ana runs owner A.'}),
                                   ('PATCH', '/v1/do-not-call', {'add': [A_BLOCKED]})):
            response = await self.client.request(method, path, json=body, headers=a)
            assert response.status == 200, await response.text()
        meeting = await self.app.runtime_daemon.create_meeting(
            {'meetingUrl': ZOOM, 'context': context_payload()}, owner=A)
        return SimpleNamespace(done=done, live=live, meeting=meeting.id)

    def snapshot(self, seeded):
        """Everything of owner A's that another owner must never change."""
        owners = self.root / '.smitline' / 'owners' / A
        calls = {call_id: self.store.get(call_id) for call_id in (seeded.done, seeded.live)}
        return {
            'calls': {call_id: {key: record.get(key) for key in
                                ('owner', 'status', 'carryNote', 'endReason', 'brief', 'recording')}
                      for call_id, record in calls.items()},
            'files': {name: (owners / name).read_text() for name in
                      ('contacts.json', 'profile.json', 'do-not-call.json')},
            'meeting': self.app.runtime_daemon.meetings.get(seeded.meeting).session.to_dict(),
            'line': (list(self.line.instructions), list(self.line.ended),
                     list(self.line.transfers), list(self.line.downloads)),
            'supervisor': (list(self.supervisor.contexts), list(self.supervisor.cancelled)),
        }


# (method, path) of every route -> the check proving another owner gets nothing of A's.
ROUTES = {
    ('POST', '/v1/calls/check'): 'check_reads_own_lists',
    ('POST', '/v1/calls'): 'new_call_is_own',
    ('GET', '/v1/calls'): 'list_is_own',
    ('GET', '/v1/calls/{callId}'): 'call_hidden',
    ('GET', '/v1/calls/{callId}/wait'): 'wait_hidden',
    ('GET', '/v1/calls/{callId}/events'): 'events_hidden',
    ('POST', '/v1/calls/{callId}/instructions'): 'instruct_hidden',
    ('POST', '/v1/calls/{callId}/end'): 'end_hidden',
    ('POST', '/v1/calls/{callId}/transfer'): 'transfer_hidden',
    ('GET', '/v1/calls/{callId}/recording'): 'recording_hidden',
    ('GET', '/v1/calls/{callId}/listen'): 'listen_hidden',
    ('POST', '/v1/calls/{callId}/note'): 'note_hidden',
    ('GET', '/v1/contacts'): 'contacts_are_own',
    ('GET', '/v1/contacts/{number}'): 'contact_hidden',
    ('PATCH', '/v1/contacts/{number}'): 'contact_change_is_own',
    ('DELETE', '/v1/contacts/{number}'): 'contact_forget_is_own',
    ('GET', '/v1/voices'): 'voices_are_own',
    ('GET', '/v1/profile'): 'profile_is_own',
    ('PATCH', '/v1/profile'): 'profile_change_is_own',
    ('GET', '/v1/do-not-call'): 'do_not_call_is_own',
    ('PATCH', '/v1/do-not-call'): 'do_not_call_change_is_own',
    ('POST', '/v1/meetings'): 'meeting_create_local_only',
    ('GET', '/v1/meetings/{meetingId}'): 'meeting_hidden',
    ('POST', '/v1/meetings/{meetingId}/context'): 'meeting_context_hidden',
    ('POST', '/v1/meetings/{meetingId}/cancel'): 'meeting_cancel_hidden',
    ('GET', '/v1/meetings/{meetingId}/events'): 'meeting_events_hidden',
    ('GET', '/v1/meetings/{meetingId}/handoff'): 'meeting_handoff_hidden',
    ('POST', '/v1/daemon/stop'): 'stop_needs_the_launch_token',
    ('GET', '/v1/openapi.json'): 'openapi_has_no_owner_data',
}
# Routes that act for no owner, so the owner rules do not apply to them.
OWNERLESS = {('POST', '/v1/daemon/stop'), ('GET', '/v1/openapi.json')}
RAW_MEETINGS = {route for route in ROUTES if route[1].startswith('/v1/meetings')}


def request_for(route, seeded):
    """A request that would succeed for the owner of the things it names (valid body)."""
    method, path = route
    path = (path.replace('{callId}', seeded.live if method == 'POST' and 'note' not in path else seeded.done)
            .replace('{number}', A_NUMBER).replace('{meetingId}', seeded.meeting))
    bodies = {
        '/v1/calls/check': brief(to='+14155550150'),
        '/v1/calls': brief(to='+14155550150'),
        f'/v1/calls/{seeded.live}/instructions': {'text': 'Ask about parking.'},
        f'/v1/calls/{seeded.done}/note': {'text': 'Booked for 7pm.'},
        f'/v1/contacts/{A_NUMBER}': {'notes': 'changed'},
        '/v1/profile': {'about': 'changed'},
        '/v1/do-not-call': {'add': ['+14155550199']},
        '/v1/meetings': {'meetingUrl': ZOOM, 'context': context_payload()},
        f'/v1/meetings/{seeded.meeting}/context': context_payload(),
    }
    body = bodies.get(path, {} if method == 'POST' else None)
    if path.endswith('/wait'):
        path += '?timeout=0'
    if method == 'PATCH' and body is None:
        body = {}
    return method, path, body


class IsolationTests(unittest.IsolatedAsyncioTestCase):
    """Another owner (B through the gateway's internal token) acting on owner A's things."""
    identity = 'internal'

    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        PerOwnerSettings.OVERRIDES = {A: {'SMITLINE_EXTRA_VOICES': 'aria', 'SMITLINE_VOICE': 'aria'}}
        self.d = await OwnersDaemon(self.temp.name).start()
        self.client = self.d.client
        self.seeded = await self.d.seed_owner_a()
        self.local = False
        if self.identity == 'internal':
            self.other = self.d.as_owner(B)
        elif self.identity == 'owned':
            self.other = self.d.bearer(self.d.owned_b)
        elif self.identity == 'launch':
            self.other, self.local = dict(LAUNCH), True
        else:
            self.other, self.local = self.d.bearer(self.d.standard), True
        self.other_owner = 'local' if self.local else B

    async def asyncTearDown(self):
        PerOwnerSettings.OVERRIDES = {}
        await self.d.close()
        self.temp.cleanup()

    async def call(self, method, path, body=None, headers=None):
        response = await self.client.request(method, path, json=body,
                                             headers=self.other if headers is None else headers)
        text = await response.text()
        try:
            payload = json.loads(text) if text else None
        except ValueError:
            payload = text
        return response.status, payload

    async def assert_not_found(self, method, path, body=None, code='not_found'):
        status, payload = await self.call(method, path, body)
        self.assertEqual(status, 404, (method, path, payload))
        self.assertEqual(payload['error']['code'], code, (method, path))
        return payload

    def test_every_route_has_an_isolation_entry(self):
        registered = {(route.method, route.resource.canonical)
                      for route in self.d.app.router.routes() if route.method != 'HEAD'}
        self.assertEqual(registered - set(ROUTES), set(),
                         'a route without an isolation entry: add it to ROUTES')
        self.assertEqual(set(ROUTES) - registered, set(), 'ROUTES names a route that is gone')

    async def test_another_owner_gets_nothing_of_owner_a(self):
        before = self.d.snapshot(self.seeded)
        for route, check in ROUTES.items():
            with self.subTest(route=route, identity=self.identity):
                await getattr(self, check)()
        # Whatever was asked, owner A's things are exactly as they were.
        after = self.d.snapshot(self.seeded)
        self.assertEqual(after, before)
        self.assertEqual(self.d.store.get(self.seeded.live)['status'], 'in_progress')

    # Calls ------------------------------------------------------------------------------

    async def check_reads_own_lists(self):
        # A's do-not-call list stops A's calls only; carrying A's calls is "no such call".
        status, payload = await self.call('POST', '/v1/calls/check', brief(to=A_BLOCKED))
        self.assertEqual((status, payload['ok'], payload['problems']), (200, True, []))
        status, payload = await self.call('POST', '/v1/calls/check',
                                          brief(carryFrom={'calls': [self.seeded.done]}))
        self.assertFalse(payload['ok'])
        self.assertIn('do not exist', ' '.join(payload['problems']))
        own = await (await self.client.post('/v1/calls/check', json=brief(to=A_BLOCKED),
                                            headers=self.d.as_owner(A))).json()
        self.assertFalse(own['ok'])

    async def new_call_is_own(self):
        status, payload = await self.call('POST', '/v1/calls', brief(to='+14155550150'))
        self.assertEqual(status, 201, payload)
        self.assertEqual(payload['owner'], self.other_owner)
        self.assertNotIn(payload['id'], [call['id'] for call in self.d.service.list(owner=A)])
        self.d.line.gate(payload['id']).set()
        await self.d.service.wait(payload['id'], owner=self.other_owner, timeout=5)
        # Notes of A's calls cannot be carried into another owner's call.
        status, payload = await self.call('POST', '/v1/calls',
                                          brief(to='+14155550151', carryFrom={'calls': [self.seeded.done]}))
        self.assertEqual((status, payload['error']['code']), (422, 'invalid_request'))

    async def list_is_own(self):
        for query in ('', '?limit=100', f'?contact=%2B{A_NUMBER[1:]}', '?channel=phone'):
            status, payload = await self.call('GET', '/v1/calls' + query)
            self.assertEqual(status, 200)
            ids = {call['id'] for call in payload['calls']}
            self.assertFalse(ids & {self.seeded.done, self.seeded.live}, query)
            self.assertTrue(all(call['owner'] == self.other_owner for call in payload['calls']))
            # Spend totals count only the owner's own finished calls, never owner A's.
            finished = [call for call in payload['calls']
                        if call['status'] in ('completed', 'failed', 'canceled') and call.get('cost')]
            self.assertEqual(sum(day['calls'] for day in payload['spend']['days']), len(finished), query)

    async def call_hidden(self):
        await self.assert_not_found('GET', f'/v1/calls/{self.seeded.done}')
        await self.assert_not_found('GET', f'/v1/calls/{self.seeded.live}')

    async def wait_hidden(self):
        await self.assert_not_found('GET', f'/v1/calls/{self.seeded.live}/wait?timeout=0')

    async def events_hidden(self):
        await self.assert_not_found('GET', f'/v1/calls/{self.seeded.live}/events')
        await self.assert_not_found('GET', f'/v1/calls/{self.seeded.live}/events?format=json&after=0')
        await self.assert_not_found('GET', f'/v1/calls/{self.seeded.done}/events?format=json')

    async def instruct_hidden(self):
        await self.assert_not_found('POST', f'/v1/calls/{self.seeded.live}/instructions',
                                    {'text': 'Hang up and call me instead.'})
        await self.assert_not_found('POST', f'/v1/calls/{self.seeded.live}/instructions',
                                    {'text': 'Psst.', 'silent': True})

    async def end_hidden(self):
        await self.assert_not_found('POST', f'/v1/calls/{self.seeded.live}/end', {})

    async def transfer_hidden(self):
        await self.assert_not_found('POST', f'/v1/calls/{self.seeded.live}/transfer', {})

    async def recording_hidden(self):
        for fmt in ('wav', 'mp3'):
            await self.assert_not_found('GET', f'/v1/calls/{self.seeded.done}/recording?format={fmt}')
        # The owner gets it, so the 404 above is the owner check and not a missing recording.
        response = await self.client.get(f'/v1/calls/{self.seeded.done}/recording',
                                         headers=self.d.as_owner(A))
        self.assertEqual(response.status, 200)
        self.d.line.downloads.pop()

    async def listen_hidden(self):
        await self.assert_not_found('GET', f'/v1/calls/{self.seeded.live}/listen')

    async def note_hidden(self):
        await self.assert_not_found('POST', f'/v1/calls/{self.seeded.done}/note', {'text': 'Mine now.'})

    # Contacts, profile, do-not-call ------------------------------------------------------

    async def contacts_are_own(self):
        status, payload = await self.call('GET', '/v1/contacts')
        self.assertEqual(status, 200)
        numbers = {contact['number'] for contact in payload['contacts']}
        self.assertFalse(numbers & {A_NUMBER, A_LIVE_NUMBER})

    async def contact_hidden(self):
        await self.assert_not_found('GET', f'/v1/contacts/{A_NUMBER}')
        await self.assert_not_found('GET', f'/v1/contacts/%2B{A_NUMBER[1:]}')

    async def contact_change_is_own(self):
        status, payload = await self.call('PATCH', f'/v1/contacts/{A_NUMBER}', {'name': 'Mallory'})
        self.assertEqual(status, 200)
        # The change is the other owner's own entry: none of A's calls or notes show up.
        self.assertEqual((payload['name'], payload['notes'], payload['calls'], payload['history']),
                         ('Mallory', '', 0, []))
        await self.call('DELETE', f'/v1/contacts/{A_NUMBER}')

    async def contact_forget_is_own(self):
        status, payload = await self.call('DELETE', f'/v1/contacts/{A_NUMBER}')
        self.assertEqual((status, payload), (200, {'forgotten': False}))

    async def voices_are_own(self):
        status, payload = await self.call('GET', '/v1/voices')
        self.assertEqual(status, 200)
        self.assertNotIn('aria', payload['voices'])
        self.assertNotEqual(payload['default'], 'aria')

    async def profile_is_own(self):
        status, payload = await self.call('GET', '/v1/profile')
        self.assertEqual(status, 200)
        self.assertNotIn('Ana', json.dumps(payload))

    async def profile_change_is_own(self):
        status, payload = await self.call('PATCH', '/v1/profile', {'about': 'Mallory.'})
        self.assertEqual((status, payload['about']), (200, 'Mallory.'))

    async def do_not_call_is_own(self):
        status, payload = await self.call('GET', '/v1/do-not-call')
        self.assertEqual(status, 200)
        self.assertNotIn(A_BLOCKED, json.dumps(payload))

    async def do_not_call_change_is_own(self):
        status, payload = await self.call('PATCH', '/v1/do-not-call', {'remove': [A_BLOCKED]})
        self.assertEqual(status, 200)
        status, payload = await self.call('PATCH', '/v1/do-not-call', {'add': ['+14155550188']})
        self.assertEqual(status, 200)
        await self.call('PATCH', '/v1/do-not-call', {'remove': ['+14155550188']})

    # Raw meetings (local owner only) -------------------------------------------------------

    async def assert_meeting_refused(self, method, path, body=None):
        status, payload = await self.call(method, path, body)
        if self.local:
            # The local owner may use the raw API, but owner A's meeting does not exist for it.
            self.assertEqual((status, payload['error']['message']), (404, 'meeting not found'), path)
        else:
            self.assertEqual((status, payload['error']['code']), (403, 'local_owner_only'), path)

    async def meeting_create_local_only(self):
        started = list(self.d.supervisor.started)
        status, payload = await self.call('POST', '/v1/meetings',
                                          {'meetingUrl': ZOOM, 'context': context_payload()})
        if self.local:
            self.assertEqual(status, 201)
            self.assertNotIn('owner', payload)
        else:
            self.assertEqual((status, payload['error']['code']), (403, 'local_owner_only'))
            self.assertEqual(self.d.supervisor.started, started)

    async def meeting_hidden(self):
        await self.assert_meeting_refused('GET', f'/v1/meetings/{self.seeded.meeting}')

    async def meeting_context_hidden(self):
        await self.assert_meeting_refused('POST', f'/v1/meetings/{self.seeded.meeting}/context',
                                          context_payload(objective='Leak the roadmap'))

    async def meeting_cancel_hidden(self):
        await self.assert_meeting_refused('POST', f'/v1/meetings/{self.seeded.meeting}/cancel', {})

    async def meeting_events_hidden(self):
        await self.assert_meeting_refused('GET', f'/v1/meetings/{self.seeded.meeting}/events')

    async def meeting_handoff_hidden(self):
        await self.assert_meeting_refused('GET', f'/v1/meetings/{self.seeded.meeting}/handoff')

    # Routes that act for no owner -----------------------------------------------------------

    async def stop_needs_the_launch_token(self):
        status, payload = await self.call('POST', '/v1/daemon/stop', {})
        if self.identity == 'launch':
            self.assertEqual(status, 202)
            self.d.stops.clear()
        else:
            # Only the per-launch token on this computer stops the daemon; no API token does.
            self.assertEqual((status, payload['error']['code']), (403, 'forbidden'))
            self.assertEqual(self.d.stops, [])

    async def openapi_has_no_owner_data(self):
        status, payload = await self.call('GET', '/v1/openapi.json')
        self.assertEqual(status, 200)
        text = json.dumps(payload)
        for secret in (self.seeded.done, self.seeded.live, A_NUMBER, 'Ana'):
            self.assertNotIn(secret, text)


class IsolationFromOwnedTokenTests(IsolationTests):
    """Owner B through an API token created for it."""
    identity = 'owned'


class IsolationFromLaunchTokenTests(IsolationTests):
    """The local owner, with the per-launch token (the console, the CLI, local MCP)."""
    identity = 'launch'


class IsolationFromStandardTokenTests(IsolationTests):
    """The local owner, with a server-mode API token created without an owner."""
    identity = 'standard'


class OwnerHeaderTests(unittest.IsolatedAsyncioTestCase):
    """Who may name the owner, on every route that acts for one."""
    require_owner = False

    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        env = {'OPENAI_API_KEY': 'sk-test'}
        self.d = await OwnersDaemon(self.temp.name, environ=env).start()
        self.seeded = await self.d.seed_owner_a()
        if self.require_owner:
            # From now on the deployment serves accounts only.
            self.d.hooks._environ['SMITLINE_REQUIRE_OWNER'] = '1'
        self.client = self.d.client

    async def asyncTearDown(self):
        await self.d.close()
        self.temp.cleanup()

    async def send(self, route, headers):
        method, path, body = request_for(route, self.seeded)
        response = await self.client.request(method, path, json=body, headers=headers)
        text = await response.text()
        return response.status, (json.loads(text) if text.startswith('{') else {})

    def owner_routes(self):
        return [route for route in ROUTES if route not in OWNERLESS]

    def expected(self, route, status, code):
        # With SMITLINE_REQUIRE_OWNER=1 the raw meeting API is closed before anything else.
        if self.require_owner and route in RAW_MEETINGS:
            return 403, 'local_owner_only'
        return status, code

    async def test_only_an_internal_token_may_name_the_owner(self):
        refused = {
            'launch': dict(LAUNCH, **{'X-Smitline-Owner': A}),
            'standard': self.d.bearer(self.d.standard, **{'X-Smitline-Owner': A}),
            # A token created for owner B neither acts as A nor is ignored: it is refused.
            'owned': self.d.bearer(self.d.owned_b, **{'X-Smitline-Owner': A}),
            'owned, naming itself': self.d.bearer(self.d.owned_b, **{'X-Smitline-Owner': B}),
        }
        for route in self.owner_routes():
            for name, headers in refused.items():
                with self.subTest(route=route, credential=name):
                    status, payload = await self.send(route, headers)
                    self.assertEqual((status, payload['error']['code']),
                                     self.expected(route, 403, 'owner_header_forbidden'))

    async def test_an_owner_id_is_checked_before_anything_is_read(self):
        for route in self.owner_routes():
            for bad in BAD_OWNERS:
                with self.subTest(route=route, owner=bad):
                    status, payload = await self.send(route, self.d.as_owner(bad))
                    self.assertEqual((status, payload['error']['code']),
                                     self.expected(route, 400, 'invalid_owner'))
        # Nothing was written for an owner that is not one.
        owners = self.d.root / '.smitline' / 'owners'
        self.assertEqual(sorted(path.name for path in owners.iterdir()), [A])

    async def test_a_token_bound_to_an_owner_with_the_internal_kind_still_acts_as_it(self):
        # api_tokens.py create --kind internal --owner acct_b: "always acts as that owner".
        token, entry = self.d.tokens.create('odd', kind='internal', owner=B)
        self.assertEqual((entry['kind'], entry['owner']), ('internal', B))
        named_a = self.d.bearer(token, **{'X-Smitline-Owner': A})
        response = await self.client.get(f'/v1/calls/{self.seeded.done}', headers=named_a)
        self.assertEqual(response.status, 403)
        self.assertEqual((await response.json())['error']['code'], 'owner_header_forbidden')
        response = await self.client.get('/v1/calls', headers=self.d.bearer(token, **{'X-Smitline-Owner': B}))
        self.assertEqual(response.status, 200)
        response = await self.client.get('/v1/calls', headers=self.d.bearer(token))
        self.assertEqual(response.status, 200)

    async def test_ownerless_routes(self):
        # Stopping needs the launch token; the header changes nothing for it.
        for headers in (self.d.as_owner(A), self.d.bearer(self.d.owned_b), self.d.bearer(self.d.standard)):
            response = await self.client.post('/v1/daemon/stop', json={}, headers=headers)
            self.assertEqual(response.status, 403)
        response = await self.client.post('/v1/daemon/stop', json={}, headers=LAUNCH)
        self.assertEqual(response.status, 202)
        # The OpenAPI document needs a credential, not an owner.
        self.assertEqual((await self.client.get('/v1/openapi.json')).status, 401)
        self.assertEqual((await self.client.get('/v1/openapi.json', headers=self.d.as_owner(A))).status, 200)


class RequireOwnerEverywhereTests(OwnerHeaderTests):
    """SMITLINE_REQUIRE_OWNER=1: nothing acts as the local owner, on any route."""
    require_owner = True

    async def test_no_route_acts_as_the_local_owner(self):
        local = {
            'launch': dict(LAUNCH),
            'standard': self.d.bearer(self.d.standard),
            'internal without a header': self.d.bearer(self.d.internal),
            'internal naming local': self.d.as_owner('local'),
        }
        started = list(self.d.supervisor.started)
        for route in self.owner_routes():
            for name, headers in local.items():
                with self.subTest(route=route, credential=name):
                    status, payload = await self.send(route, headers)
                    if route in RAW_MEETINGS:
                        self.assertEqual((status, payload['error']['code']), (403, 'local_owner_only'))
                    else:
                        self.assertEqual((status, payload['error']['code']), (401, 'owner_required'))
        self.assertEqual(self.d.supervisor.started, started)

    async def test_an_owner_still_works_and_raw_meetings_stay_closed(self):
        for route in self.owner_routes():
            with self.subTest(route=route):
                status, payload = await self.send(route, self.d.bearer(self.d.owned_b))
                if route in RAW_MEETINGS:
                    self.assertEqual((status, payload['error']['code']), (403, 'local_owner_only'))
                else:
                    self.assertNotIn(status, (400, 401, 403), (route, payload))


class SettingsStayWithTheirOwnerTests(unittest.IsolatedAsyncioTestCase):
    """settings(owner) decides the brief defaults, caller ID, rehearsal phone, calling hours,
    allow-list, repeat limits, recording, voice and owner phone of that owner's calls only."""

    LOCAL_ENV = {**PHONE_ENV, 'SMITLINE_OWNER_NAME': 'Robin', 'SMITLINE_OWNER_PHONE': '+14155550111',
                 'SMITLINE_VOICE': 'quartz'}
    A_SETTINGS = {'SMITLINE_OWNER_NAME': 'Ana', 'SMITLINE_OWNER_PHONE': '+14155550122',
                  'SMITLINE_CALLER_ID': '+15005550007', 'SMITLINE_VOICE': 'vesper',
                  'SMITLINE_CALLING_HOURS': 'off', 'SMITLINE_ALLOWED_CALLING_CODES': '1',
                  'SMITLINE_MAX_CALLS_PER_NUMBER': '1', 'SMITLINE_RECORD_CALLS': '1',
                  'SMITLINE_NOTIFY_WEBHOOK': 'https://a.example.com/smitline'}

    async def asyncSetUp(self):
        FakeLive.instances = []
        self.temp = tempfile.TemporaryDirectory()
        PerOwnerSettings.OVERRIDES = {A: self.A_SETTINGS}
        self.h = PhoneHarness(self.temp.name, env=self.LOCAL_ENV)
        self.h.hooks = self.h.service.hooks = PerOwnerSettings(
            environ=self.h.env, store=self.h.store, clock=DAYTIME)

    async def asyncTearDown(self):
        PerOwnerSettings.OVERRIDES = {}
        await self.h.service.shutdown()
        self.temp.cleanup()

    async def dial(self, owner, **overrides):
        request = None if owner == 'local' else AsOwner(owner)
        record = await self.h.service.create(brief(**overrides), request=request)

        def placed():
            return next((created for created in self.h.twilio.created
                         if created.get('status_callback', '').endswith(record['id'])), None)
        await until(lambda: self.h.line.session(record['id']) is not None and placed() is not None)
        return record, self.h.line.session(record['id']), placed()

    async def checked(self, owner, payload):
        request = None if owner == 'local' else AsOwner(owner)
        return await self.h.service.check(payload, request=request)

    async def test_brief_defaults_and_rehearsals(self):
        payload = {'channel': 'phone', 'to': '+14155550142', 'objective': 'Book a table'}
        self.assertEqual((await self.checked(A, payload))['brief']['onBehalfOf'], 'Ana')
        self.assertEqual((await self.checked('local', payload))['brief']['onBehalfOf'], 'Robin')
        rehearsal = {'channel': 'phone', 'objective': 'Practice', 'rehearsal': True}
        self.assertEqual((await self.checked(A, rehearsal))['brief']['to'], '+14155550122')
        self.assertEqual((await self.checked('local', rehearsal))['brief']['to'], '+14155550111')
        # A rehearsal rings only its own owner's phone, never another owner's.
        crossed = await self.checked(A, dict(rehearsal, to='+14155550111'))
        self.assertFalse(crossed['ok'])
        self.assertIn('SMITLINE_OWNER_PHONE', crossed['problems'][0])
        self.assertTrue((await self.checked('local', dict(rehearsal, to='+14155550111')))['ok'])

    async def test_each_owner_calls_with_its_own_number_voice_recording_and_phone(self):
        mine_record, mine, created_mine = await self.dial('local')
        theirs_record, theirs, created_theirs = await self.dial(A, to='+14155550143')
        self.assertEqual((created_mine['from_'], created_theirs['from_']), ('+15005550006', '+15005550007'))
        self.assertEqual((created_mine['record'], created_theirs['record']), (False, True))
        self.assertEqual((mine.owner_phone, theirs.owner_phone), ('+14155550111', '+14155550122'))
        self.assertEqual(self.h.hooks.credentials(A, 'twilio')['fromNumber'], '+15005550007')
        self.assertEqual(self.h.hooks.credentials('local', 'twilio')['fromNumber'], '+15005550006')
        self.assertEqual(self.h.hooks.credentials(A, 'twilio')['twilioNumber'], '+15005550006')
        for record, session, voice in ((mine_record, mine, 'quartz'), (theirs_record, theirs, 'vesper')):
            ws = FakeTwilioSocket()
            self.assertIs(self.h.line.claim(record['id'], session.token), session)
            task = asyncio.create_task(session.run(ws, {'streamSid': 'MZ1', 'callSid': 'CA123'}))
            await until(lambda: FakeLive.instances and FakeLive.instances[-1] is session.live)
            self.assertEqual(session.live.config['audio']['output'], {'voice': voice})
            ws.push({'event': 'stop'})
            await asyncio.wait_for(task, 3)

    async def test_guardrails_read_the_owner_settings(self):
        # 3 AM in San Francisco: outside the local owner's calling hours; owner A turned them off.
        night = lambda: datetime(2026, 9, 30, 10, 0, tzinfo=timezone.utc)
        self.h.hooks.clock = night
        mine = await self.checked('local', brief(to='+14155550150'))
        self.assertIn('calls people between', ' '.join(mine['problems']))
        self.assertTrue((await self.checked(A, brief(to='+14155550150')))['ok'])
        self.h.hooks.clock = DAYTIME
        # Owner A may call only +1 numbers; the local owner has no allow-list.
        uk = '+442079460958'
        self.assertIn('SMITLINE_ALLOWED_CALLING_CODES', ' '.join((await self.checked(A, brief(to=uk)))['problems']))
        self.assertNotIn('SMITLINE_ALLOWED_CALLING_CODES',
                         ' '.join((await self.checked('local', brief(to=uk)))['problems']))
        # Owner A allows one call a day per number, counting only its own calls.
        await self.dial('local', to='+14155550160')
        self.assertTrue((await self.checked(A, brief(to='+14155550160')))['ok'])
        await self.dial(A, to='+14155550160')
        refused = await self.checked(A, brief(to='+14155550160'))
        self.assertIn('SMITLINE_MAX_CALLS_PER_NUMBER', ' '.join(refused['problems']))
        self.assertTrue((await self.checked('local', brief(to='+14155550160')))['ok'])

    async def test_incoming_calls_and_meetings_use_the_called_owner_settings(self):
        from phone_line import inbound_brief
        theirs = inbound_brief(self.h.hooks.settings(A), '+14155550142')
        mine = inbound_brief(self.h.hooks.settings('local'), '+14155550142')
        self.assertEqual((theirs.on_behalf_of, theirs.webhook_url), ('Ana', 'https://a.example.com/smitline'))
        self.assertEqual((mine.on_behalf_of, mine.webhook_url), ('Robin', None))
        env = meeting_environment(self.h.hooks, A)
        self.assertEqual((env['SMITLINE_VOICE'], env['SMITLINE_OWNER_NAME']), ('vesper', 'Ana'))
        self.assertNotIn('Robin', json.dumps(env))


class UsageIsRecordedOnceTests(unittest.IsolatedAsyncioTestCase):
    """record_usage(owner, call, usage) runs exactly once for every call that started, with
    the call's owner; a call refused before it starts records nothing."""

    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.d = await OwnersDaemon(self.temp.name, hooks_class=Counting).start()
        self.client = self.d.client

    async def asyncTearDown(self):
        await self.d.close()
        self.temp.cleanup()

    async def start(self, owner, **overrides):
        headers = LAUNCH if owner == 'local' else self.d.as_owner(owner)
        response = await self.client.post('/v1/calls', json=brief(**overrides), headers=headers)
        return response.status, await response.json()

    def usage_for(self, call_id):
        return [entry for entry in self.d.hooks.usage if entry[1] == call_id]

    async def test_finished_failed_and_canceled_calls(self):
        _, mine = await self.start('local')
        _, theirs = await self.start(A, to='+14155550150')
        for call in (mine, theirs):
            self.d.line.gate(call['id']).set()
            await self.d.service.wait(call['id'], owner=call['owner'], timeout=5)
        self.assertEqual(self.usage_for(mine['id']),
                         [('local', mine['id'], 'completed', {'voiceSeconds': 30, 'phoneSeconds': 31,
                                                             'summaryTokens': {'input': 10, 'output': 5}})])
        self.assertEqual([entry[0] for entry in self.usage_for(theirs['id'])], [A])
        # A line that fails, and a call ended before it connects, are recorded once too.
        self.d.line.fail = RuntimeError('dial failed')
        _, failed = await self.start(A, to='+14155550151')
        await self.d.service.wait(failed['id'], owner=A, timeout=5)
        self.d.line.fail, self.d.line.connect = None, False
        _, canceled = await self.start(A, to='+14155550152')
        await until(lambda: self.d.store.get(canceled['id'])['status'] == 'connecting')
        await self.client.post(f'/v1/calls/{canceled["id"]}/end', json={}, headers=self.d.as_owner(A))
        await self.d.service.wait(canceled['id'], owner=A, timeout=5)
        self.assertEqual([(entry[0], entry[2]) for entry in self.usage_for(failed['id'])], [(A, 'failed')])
        self.assertEqual([(entry[0], entry[2]) for entry in self.usage_for(canceled['id'])], [(A, 'canceled')])
        self.assertEqual(len(self.d.hooks.usage), 4)

    async def test_a_refused_start_records_nothing_and_starts_nothing(self):
        self.d.hooks.refuse_start = CallRefused('insufficient_credit', 'Top up to call.', status=402,
                                                balance='0.40', topUpUrl='https://example.com/top-up')
        status, payload = await self.start(A)
        self.assertEqual(status, 402)
        self.assertEqual(payload['error'], {'code': 'insufficient_credit', 'message': 'Top up to call.',
                                            'balance': '0.40', 'topUpUrl': 'https://example.com/top-up'})
        [record] = self.d.store.list(limit=None)
        self.assertEqual((record['owner'], record['status'], record['endReason']), (A, 'failed', 'refused'))
        self.assertEqual(self.d.hooks.started, [(A, record['id'])])
        self.assertEqual((self.d.hooks.usage, self.d.hooks.notified, self.d.line.contexts), ([], [], {}))
        statuses = [event['data'] for event in self.d.store.events(record['id']) if event['type'] == 'call.status']
        self.assertEqual(statuses, [{'status': 'failed', 'endReason': 'refused', 'detail': 'insufficient_credit'}])
        # Checking a brief never places a hold.
        await self.client.post('/v1/calls/check', json=brief(), headers=self.d.as_owner(A))
        self.assertEqual(len(self.d.hooks.started), 1)

    async def test_a_failing_start_hook_fails_closed(self):
        self.d.hooks.refuse_start = ConnectionError('ledger down')
        status, payload = await self.start(A)
        self.assertEqual((status, payload['error']['code']), (503, 'call_start_failed'))
        self.assertNotIn('ledger', json.dumps(payload))
        [record] = self.d.store.list(limit=None)
        self.assertEqual((record['status'], record['endReason']), ('failed', 'refused'))
        self.assertEqual((self.d.hooks.usage, self.d.line.contexts), ([], {}))

    async def test_a_daemon_stop_lets_calls_end_and_records_what_they_used(self):
        _, mine = await self.start('local')
        _, theirs = await self.start(A, to='+14155550150')
        await until(lambda: all(self.d.store.get(call['id'])['status'] == 'in_progress'
                                for call in (mine, theirs)))
        await self.d.service.shutdown()
        # Each line was asked to end its call and had time to report what it used.
        for call in (mine, theirs):
            self.assertEqual(self.d.store.get(call['id'])['status'], 'completed')
            [(owner, _call_id, status, usage)] = self.usage_for(call['id'])
            self.assertEqual((owner, status, usage['phoneSeconds']), (call['owner'], 'completed', 31))
        await self.d.service.shutdown()
        self.assertEqual(len(self.d.hooks.usage), 2)

    async def test_a_call_that_does_not_end_within_the_grace_is_closed_and_recorded_once(self):
        self.d.line.ends_when_asked = False
        self.d.service.shutdown_grace = 0.3
        _, call = await self.start(A)
        await until(lambda: self.d.store.get(call['id'])['status'] == 'in_progress')
        loop = asyncio.get_running_loop()
        started = loop.time()
        await self.d.service.shutdown()
        self.assertGreaterEqual(loop.time() - started, 0.3)
        self.assertLess(loop.time() - started, 3)
        self.assertEqual(self.d.store.get(call['id'])['status'], 'failed')
        self.assertEqual([(entry[0], entry[2], entry[3]) for entry in self.usage_for(call['id'])],
                         [(A, 'failed', {})])
        await self.d.service.shutdown()
        self.assertEqual(len(self.usage_for(call['id'])), 1)

    def test_the_grace_is_five_seconds_unless_set(self):
        from call_service import SHUTDOWN_GRACE
        from daemon_main import shutdown_grace
        self.assertEqual(SHUTDOWN_GRACE, 5.0)
        self.assertEqual(shutdown_grace({}), 5.0)
        self.assertEqual(shutdown_grace({'SMITLINE_SHUTDOWN_GRACE': '8'}), 8.0)
        self.assertEqual(shutdown_grace({'SMITLINE_SHUTDOWN_GRACE': '0'}), 0.0)
        for bad in ('-1', 'soon', 'nan', 'inf'):
            self.assertEqual(shutdown_grace({'SMITLINE_SHUTDOWN_GRACE': bad}), 5.0, bad)

    async def test_a_call_refused_at_start_does_not_count_toward_repeat_limits(self):
        self.d.hooks._environ['SMITLINE_MAX_CALLS_PER_NUMBER'] = '1'
        self.d.hooks.refuse_start = CallRefused('insufficient_credit', 'Top up to call.', status=402)
        status, _ = await self.start(A)
        self.assertEqual(status, 402)
        # Topped up, the same call goes ahead: the refused one never rang anyone.
        self.d.hooks.refuse_start = None
        status, payload = await self.start(A)
        self.assertEqual(status, 201, payload)
        # The one that was placed counts.
        status, payload = await self.start(A)
        self.assertEqual((status, payload['error']['code']), (403, 'too_many_calls_to_number'))

    async def test_a_stop_while_the_result_is_written_keeps_the_line_usage(self):
        gate = asyncio.Event()

        class Slow(FakeSummarizer):
            async def summarize(self, brief, transcript, *, duration_seconds=0):
                await gate.wait()
        self.d.service._summarizer_factory = lambda owner: Slow()
        self.d.service.shutdown_grace = 0.3
        _, call = await self.start(A)
        self.d.line.gate(call['id']).set()
        await until(lambda: self.d.store.get(call['id'])['status'] == 'summarizing')
        await self.d.service.shutdown()
        record = self.d.store.get(call['id'])
        self.assertEqual(record['status'], 'failed')
        self.assertEqual(record['usage'], {'voiceSeconds': 30, 'phoneSeconds': 31})
        self.assertEqual(self.usage_for(call['id']),
                         [(A, call['id'], 'failed', {'voiceSeconds': 30, 'phoneSeconds': 31})])

    async def test_a_restart_settles_calls_a_stopped_daemon_left_open(self):
        now = self.d.store.now()
        left = {'version': 1, 'id': new_call_id(), 'owner': A, 'channel': 'phone',
                'direction': 'outbound', 'status': 'in_progress', 'brief': brief(),
                'createdAt': now, 'updatedAt': now, 'line': {}, 'result': None, 'usage': {}}
        self.d.store.create(left)
        closed = self.d.service.reconcile()
        self.assertEqual(closed, [left['id']])
        await until(lambda: self.usage_for(left['id']))
        self.assertEqual([(entry[0], entry[2]) for entry in self.usage_for(left['id'])], [(A, 'failed')])
        self.assertEqual(self.d.service.reconcile(), [])
        await asyncio.sleep(0.05)
        self.assertEqual(len(self.usage_for(left['id'])), 1)


class UsageFailureAndStartupTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_failing_record_usage_never_breaks_the_call_or_its_webhook(self):
        with tempfile.TemporaryDirectory() as temp:
            d = await OwnersDaemon(temp, hooks_class=Counting).start()
            try:
                def broken(owner, call, usage):
                    raise ConnectionError('ledger down')
                d.hooks.record_usage = broken
                response = await d.client.post('/v1/calls', json=brief(), headers=d.as_owner(A))
                call = await response.json()
                d.line.gate(call['id']).set()
                done = await d.service.wait(call['id'], owner=A, timeout=5)
                self.assertEqual(done['status'], 'completed')
                await until(lambda: d.hooks.notified)
                events = [event['type'] for event in d.store.events(call['id'])]
                self.assertIn('call.usage_failed', events)
                self.assertEqual(d.hooks.notified, [(A, call['id'])])
            finally:
                await d.close()

    def test_calls_left_open_before_the_event_loop_runs_are_recorded_at_shutdown(self):
        with tempfile.TemporaryDirectory() as temp:
            store = CallStore(Path(temp) / 'calls')
            hooks = Counting(environ={'OPENAI_API_KEY': 'sk-test'}, store=store, clock=DAYTIME)
            service = CallService(store, hooks=hooks, lines={'phone': GatedLine()},
                                  summarizer_factory=lambda owner: FakeSummarizer(), environ={})
            now = store.now()
            left = store.create({'version': 1, 'id': new_call_id(), 'owner': A, 'channel': 'phone',
                                 'direction': 'outbound', 'status': 'ringing', 'brief': brief(),
                                 'createdAt': now, 'updatedAt': now, 'line': {}, 'result': None,
                                 'usage': {}})
            self.assertEqual(service.reconcile(), [left['id']])
            self.assertEqual(hooks.usage, [])
            asyncio.run(service.shutdown())
            self.assertEqual([(owner, call_id) for owner, call_id, _, _ in hooks.usage], [(A, left['id'])])

    def test_listing_calls_needs_an_owner(self):
        with tempfile.TemporaryDirectory() as temp:
            store = CallStore(Path(temp) / 'calls')
            service = CallService(store, hooks=DefaultCallHooks(environ={}), lines={}, environ={})
            with self.assertRaises(ValueError):
                service.list(owner=None)
            with self.assertRaises(TypeError):
                service.get('call-0000000000000000')

    def test_the_token_cli_refuses_an_invalid_owner_or_kind(self):
        import contextlib
        import io
        from api_tokens import main as tokens_main
        with tempfile.TemporaryDirectory() as temp:
            for args in (['--owner', '../escape'], ['--owner', 'Acct'], ['--owner', 'a' * 65]):
                errors = io.StringIO()
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(errors):
                    code = tokens_main(['--root', temp, 'create', '--name', 'x', *args])
                self.assertEqual(code, 2, args)
                self.assertIn('owner must be', errors.getvalue())
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                tokens_main(['--root', temp, 'create', '--name', 'x', '--kind', 'root'])
            self.assertEqual(list(Path(temp).rglob('api-tokens.json')), [])


class OneContactBookTests(unittest.IsolatedAsyncioTestCase):
    """Hooks written before owners keep one contact book, as an attribute: it is the local
    owner's, and no other owner reads or changes it."""

    async def test_a_single_contact_book_is_the_local_owner_only(self):
        from contacts import ContactBook

        class Older(DefaultCallHooks):
            def __init__(self, **kwargs):
                super().__init__(**kwargs)
                self.contact_book = ContactBook(Path(kwargs['env_file']).parent / 'book.json')
        with tempfile.TemporaryDirectory() as temp:
            d = await OwnersDaemon(temp, hooks_class=Older).start()
            try:
                response = await d.client.patch('/v1/contacts/+14155550101', headers=LAUNCH,
                                                json={'name': 'Sam', 'notes': 'Local only'})
                self.assertEqual(response.status, 200)
                theirs = await (await d.client.get('/v1/contacts', headers=d.as_owner(A))).json()
                self.assertEqual(theirs, {'contacts': []})
                response = await d.client.get('/v1/contacts/+14155550101', headers=d.as_owner(A))
                self.assertEqual(response.status, 404)
                response = await d.client.patch('/v1/contacts/+14155550101', headers=d.as_owner(A),
                                                json={'notes': 'Overwritten by A'})
                self.assertEqual((response.status, (await response.json())['error']['code']), (501, 'unsupported'))
                response = await d.client.delete('/v1/contacts/+14155550101', headers=d.as_owner(A))
                self.assertEqual(response.status, 501)
                mine = await (await d.client.get('/v1/contacts/+14155550101', headers=LAUNCH)).json()
                self.assertEqual((mine['name'], mine['notes']), ('Sam', 'Local only'))
                # Nor does the local owner's switch decide what an owner's calls start with.
                d.hooks.contact_book.update('+14155550101', {'autoContext': True})
                earlier = await (await d.client.post('/v1/calls', json=brief(to='+14155550101'),
                                                     headers=d.as_owner(A))).json()
                d.line.gate(earlier['id']).set()
                await d.service.wait(earlier['id'], owner=A, timeout=5)
                checked = await (await d.client.post('/v1/calls/check', json=brief(to='+14155550101'),
                                                     headers=d.as_owner(A))).json()
                self.assertEqual(checked['carried'], 0)
                mine = await (await d.client.post('/v1/calls/check', json=brief(to='+14155550101'),
                                                  headers=LAUNCH)).json()
                self.assertEqual(mine['carried'], 0)
            finally:
                await d.close()


class TwoNumbers(DefaultCallHooks):
    """Each owner has its own phone credentials and number, and a wallet that may refuse."""
    AUTH = {'local': 'tw-secret', A: 'tw-acct-a'}
    NUMBERS = {'+15005550006': 'local', '+15005550007': A}
    refuse = None

    def credentials(self, owner, provider):
        values = super().credentials(owner, provider)
        return dict(values, authToken=self.AUTH[owner]) if provider == 'twilio' else values

    def owner_for_number(self, number):
        self.asked = getattr(self, 'asked', []) + [number]
        return self.NUMBERS.get(number)

    def call_started(self, owner, record):
        if self.refuse is not None:
            raise self.refuse


class GatewayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        FakeLive.instances = []
        self.temp = tempfile.TemporaryDirectory()
        self.h = PhoneHarness(self.temp.name, env={**PHONE_ENV, 'SMITLINE_ACCEPT_INBOUND': '1'})
        self.client = None
        await self.use_hooks(TwoNumbers(environ=self.h.env, store=self.h.store, clock=DAYTIME))

    async def use_hooks(self, hooks):
        self.h.hooks = self.h.service.hooks = hooks
        if self.client is not None:
            await self.client.close()
        app = create_gateway_app(self.h.line, self.h.service, current_url=lambda: PUBLIC)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        await self.h.service.shutdown()
        self.temp.cleanup()

    async def post_signed(self, path, params, token):
        signed = compute_signature(token, PUBLIC + path, params)
        response = await self.client.post(path, data=params, headers={'X-Twilio-Signature': signed})
        return response.status, await response.text()

    async def test_an_incoming_call_the_wallet_refuses_is_turned_away(self):
        self.h.hooks.refuse = CallRefused('insufficient_credit', 'Top up.', status=402)
        status, twiml = await self.post_signed('/twilio/inbound', {
            'From': '+14155550142', 'To': '+15005550007', 'CallSid': 'CA9'}, 'tw-acct-a')
        self.assertEqual(status, 200)
        self.assertIn('<Reject', twiml)
        [record] = self.h.store.list(limit=None)
        self.assertEqual((record['owner'], record['direction'], record['status'], record['endReason']),
                         (A, 'inbound', 'failed', 'refused'))
        self.assertIsNone(self.h.line.session(record['id']))
        self.assertEqual(FakeLive.instances, [])

    async def test_a_number_nobody_owns_or_none_at_all(self):
        for to in ('+15005550099', '', 'not a number'):
            params = {'From': '+14155550142', 'CallSid': 'CA9'}
            if to:
                params['To'] = to
            status, twiml = await self.post_signed('/twilio/inbound', params, 'tw-secret')
            self.assertIn('<Reject', twiml, to)
        self.assertEqual(self.h.hooks.asked, ['+15005550099', None, None])
        self.assertEqual(self.h.store.list(limit=None), [])

    async def test_callbacks_for_unknown_calls_are_checked_with_the_installation_keys(self):
        path = f'/twilio/status/{new_call_id()}'
        self.assertEqual((await self.post_signed(path, {'CallStatus': 'ringing'}, 'tw-acct-a'))[0], 403)
        self.assertEqual((await self.post_signed(path, {'CallStatus': 'ringing'}, 'tw-secret'))[0], 204)
        self.assertEqual((await self.post_signed('/twilio/status/not-a-call', {}, 'tw-acct-a'))[0], 403)

    async def test_require_owner_turns_away_incoming_calls_with_the_default_hooks(self):
        self.h.env['SMITLINE_REQUIRE_OWNER'] = '1'
        await self.use_hooks(DefaultCallHooks(environ=self.h.env, store=self.h.store, clock=DAYTIME))
        status, twiml = await self.post_signed('/twilio/inbound', {
            'From': '+14155550142', 'To': '+15005550006', 'CallSid': 'CA9'}, 'tw-secret')
        self.assertIn('<Reject', twiml)
        self.assertEqual(self.h.store.list(limit=None), [])

    async def test_require_owner_turns_away_incoming_calls_with_hooks_written_before_owners(self):
        class Older:
            """Hooks with only the five required methods, written before owners existed."""

            def __init__(self, inner):
                self.inner = inner
                self.environ = inner.environ

            def __getattr__(self, name):
                if name in ('owner_for_number', 'settings', 'call_started'):
                    raise AttributeError(name)
                return getattr(self.inner, name)
        self.h.env['SMITLINE_REQUIRE_OWNER'] = '1'
        await self.use_hooks(Older(DefaultCallHooks(environ=self.h.env, store=self.h.store, clock=DAYTIME)))
        status, twiml = await self.post_signed('/twilio/inbound', {
            'From': '+14155550142', 'To': '+15005550006', 'CallSid': 'CA9'}, 'tw-secret')
        self.assertIn('<Reject', twiml)
        self.assertEqual(self.h.store.list(limit=None), [])
        # Without SMITLINE_REQUIRE_OWNER they still answer as the local owner, as before.
        self.h.env.pop('SMITLINE_REQUIRE_OWNER')
        await self.use_hooks(Older(DefaultCallHooks(environ=self.h.env, store=self.h.store, clock=DAYTIME)))
        status, twiml = await self.post_signed('/twilio/inbound', {
            'From': '+14155550142', 'To': '+15005550006', 'CallSid': 'CA9'}, 'tw-secret')
        self.assertIn('<Stream', twiml)
        self.assertEqual([record['owner'] for record in self.h.store.list(limit=None)], ['local'])


class WebhookSecretTests(unittest.IsolatedAsyncioTestCase):
    async def test_each_owner_webhook_verifies_only_with_its_own_secret(self):
        with tempfile.TemporaryDirectory() as temp:
            posted = {}

            async def post(url, body, headers):
                posted[url] = (body, headers['X-Smitline-Signature'])
                return 200
            store = CallStore(Path(temp) / 'calls')
            notifier = WebhookNotifier('installation-secret', post=post, allow_private=True)
            hooks = DefaultCallHooks(environ={'OPENAI_API_KEY': 'sk-test'}, env_file=Path(temp) / '.env',
                                     store=store, notifier=notifier, clock=DAYTIME)
            line = GatedLine()
            service = CallService(store, hooks=hooks, lines={'phone': line},
                                  summarizer_factory=lambda owner: FakeSummarizer(), environ={})
            calls = {}
            for owner, request in (('local', None), (A, AsOwner(A)), (B, AsOwner(B))):
                record = await service.create(brief(to=f'+1415555016{len(calls)}',
                                                    notify={'webhookUrl': f'https://example.com/{owner}'}),
                                              request=request)
                line.gate(record['id']).set()
                await service.wait(record['id'], owner=owner, timeout=5)
                await until(lambda: f'https://example.com/{owner}' in posted)
                calls[owner] = record['id']
            secrets_by_owner = {'local': 'installation-secret', A: hooks.webhook_secret(A),
                                B: hooks.webhook_secret(B)}
            self.assertEqual(len(set(secrets_by_owner.values())), 3)
            for owner, (body, signed) in ((owner, posted[f'https://example.com/{owner}']) for owner in calls):
                for other, secret in secrets_by_owner.items():
                    with self.subTest(webhook=owner, secret=other):
                        self.assertEqual(signed == signature(secret, body), owner == other)
                self.assertEqual(json.loads(body)['call']['owner'], owner)
            # The secrets are kept per owner, private, and the local one stays where it was.
            for owner in (A, B):
                path = Path(temp) / '.smitline' / 'owners' / owner / 'webhook.secret'
                self.assertEqual(path.read_text().strip(), secrets_by_owner[owner])
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
            self.assertFalse((Path(temp) / '.smitline' / 'webhook.secret').exists())
            await service.shutdown()


def parse_env_file(text):
    """KEY=value lines as compose reads them: single quotes literal, double quotes with
    backslash escapes for backslash, quote and dollar."""
    values = {}
    for line in text.splitlines():
        key, _, raw = line.partition('=')
        if raw[:1] == "'" and raw[-1:] == "'":
            values[key] = raw[1:-1]
        elif raw[:1] == '"' and raw[-1:] == '"':
            body, out, index = raw[1:-1], [], 0
            while index < len(body):
                if body[index] == '\\' and index + 1 < len(body) and body[index + 1] in '\\"$':
                    index += 1
                out.append(body[index])
                index += 1
            values[key] = ''.join(out)
        else:
            values[key] = raw
    return values


class MeetingSettingsTests(unittest.IsolatedAsyncioTestCase):
    """Another owner's meeting gets that owner's settings, and never the installation's .env
    values for the ones that owner left unset."""

    class OwnerSetsNothing(DefaultCallHooks):
        def settings(self, owner):
            from meeting_line import MEETING_SETTINGS
            env = self.environ
            return env if owner == 'local' else {key: value for key, value in env.items()
                                                 if key not in MEETING_SETTINGS}

    async def start_meeting(self, hooks, owner):
        from agent_sessions import MeetingSession
        from meeting_supervisor import ProductionMeetingSupervisor
        from test_meeting_supervisor import FakeDaemon, FakeHealth, FakeLauncher, session
        seen = []

        class Launcher(FakeLauncher):
            async def up(self, env):
                path = env.get('SMITLINE_MEETING_ENV_FILE')
                seen.append(parse_env_file(Path(path).read_text()) if path else None)
                await super().up(env)
        with tempfile.TemporaryDirectory() as temp:
            from runtime_state import pool_claim_path
            runtime = Path(temp) / 'meeting-runtime'
            runtime.mkdir()
            meeting = MeetingSession.from_dict(dict(session().to_dict(), owner=owner))
            # Another owner's meeting runs in the meeting pool; the local owner's here runs in
            # the one container at a time, which reads .env as before.
            claim = None if owner == 'local' else {'path': pool_claim_path(temp, meeting.id),
                                                   'record': {'owner': owner}}
            supervisor = ProductionMeetingSupervisor(
                temp, runtime_root=runtime, launcher=Launcher(), health=FakeHealth(),
                poll_interval=0.02, start_timeout=0.4, stop_timeout=0.4, claim=claim,
                owner_environment=lambda name: meeting_environment(hooks, name)).bind_daemon(FakeDaemon())
            await supervisor.start(meeting)
            await supervisor.cancel(meeting.id)
            await supervisor.shutdown()
        return seen[0]

    async def test_unset_owner_settings_are_blank_not_the_installation_values(self):
        installation = {'OPENAI_API_KEY': 'sk-shared', 'SMITLINE_VOICE': 'quartz',
                        'SMITLINE_OWNER_NAME': 'Robin', 'SMITLINE_PARTICIPANT_NAME': "Robin's notetaker"}
        hooks = self.OwnerSetsNothing(environ=installation)
        self.assertEqual(meeting_environment(hooks, A), {
            'OPENAI_API_KEY': 'sk-shared', 'SMITLINE_VOICE': '', 'SMITLINE_PARTICIPANT_NAME': '',
            'SMITLINE_OWNER_NAME': ''})
        # The file compose reads after .env names every one of them, so .env's values lose.
        self.assertEqual(await self.start_meeting(hooks, A), {
            'OPENAI_API_KEY': 'sk-shared', 'SMITLINE_VOICE': '', 'SMITLINE_PARTICIPANT_NAME': '',
            'SMITLINE_OWNER_NAME': ''})
        # The local owner's meeting reads .env exactly as before: no file at all.
        self.assertIsNone(await self.start_meeting(hooks, 'local'))

    async def test_a_value_compose_cannot_carry_is_blank_rather_than_the_installation_value(self):
        class Odd(DefaultCallHooks):
            def settings(self, owner):
                return dict(self.environ, SMITLINE_OWNER_NAME='Ana\nSMITLINE_VOICE=quartz')
        hooks = Odd(environ={'OPENAI_API_KEY': 'sk-shared', 'SMITLINE_OWNER_NAME': 'Robin'})
        values = await self.start_meeting(hooks, A)
        self.assertEqual(values['SMITLINE_OWNER_NAME'], '')
        self.assertNotIn('quartz', json.dumps(values))


class OwnerStorageTests(unittest.IsolatedAsyncioTestCase):
    async def test_owner_files_stay_in_the_owner_folder(self):
        with tempfile.TemporaryDirectory() as temp:
            d = await OwnersDaemon(temp).start()
            try:
                for owner, headers in (('local', LAUNCH), (A, d.as_owner(A))):
                    for method, path, body in (('PATCH', '/v1/contacts/+14155550101', {'name': 'Sam'}),
                                               ('PATCH', '/v1/profile', {'about': owner}),
                                               ('PATCH', '/v1/do-not-call', {'add': ['+14155550177']})):
                        response = await d.client.request(method, path, json=body, headers=headers)
                        self.assertEqual(response.status, 200, await response.text())
                base = Path(temp) / '.smitline'
                files = sorted(str(path.relative_to(base)) for path in base.rglob('*') if path.is_file())
                # The local owner's files are where they were before owners existed.
                self.assertEqual(files, ['contacts.json', 'do-not-call.json',
                                         'owners/acct_a/contacts.json', 'owners/acct_a/do-not-call.json',
                                         'owners/acct_a/profile.json', 'profile.json'])
                for name in ('contacts.json', 'do-not-call.json', 'profile.json'):
                    self.assertEqual((base / 'owners' / A / name).stat().st_mode & 0o777, 0o600, name)
                self.assertEqual((base / 'owners' / A).stat().st_mode & 0o777, 0o700)
                # An owner id read back from disk is checked again before it names a folder.
                with self.assertRaises(ValueError):
                    d.hooks.storage_root('../../etc')
                tampered = d.store.create({
                    'version': 1, 'id': new_call_id(), 'owner': '../../escape', 'channel': 'phone',
                    'direction': 'outbound', 'status': 'completed', 'brief': brief(),
                    'createdAt': d.store.now(), 'updatedAt': d.store.now(), 'line': {},
                    'result': {'doNotCall': True}, 'usage': {}})
                d.service._honor_do_not_call(tampered, {'doNotCall': True})
                self.assertEqual([event['type'] for event in d.store.events(tampered['id'])],
                                 ['call.do_not_call_failed'])
                self.assertFalse((Path(temp) / 'escape').exists())
                self.assertFalse((base / 'escape').exists())
                self.assertEqual(sorted(str(path.relative_to(base)) for path in base.rglob('*')
                                        if path.is_file()), files)
            finally:
                await d.close()


if __name__ == '__main__':
    unittest.main()
