"""Owner scoping: one deployment serving many owners, and the local owner exactly as before."""
import asyncio
import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path

from aiohttp.test_utils import TestClient, TestServer

from agent_sessions import MeetingSession
from api_tokens import ApiTokenStore, main as tokens_main
from call_brief import CallBrief
from call_hooks import CallRefused, DefaultCallHooks
from call_notify import WebhookNotifier, signature
from call_service import CallService
from call_store import CallStore, new_call_id
from meeting_line import meeting_environment
from meeting_repository import MeetingRepository
from meeting_supervisor import ProductionMeetingSupervisor
from phone_gateway import create_gateway_app
from runtime_config import RuntimeConfig
from runtime_daemon import DaemonError, create_app
from runtime_state import meeting_state_path, pool_claim_path, read_json
from test_briefing import PROFILE
from test_call_core import at, phone_brief, placed_at
from test_call_service import DAYTIME, ZOOM, FakeLine, FakeSummarizer, RecordingNotifier, brief
from test_meeting_supervisor import FakeDaemon as FakeSupervisorDaemon
from test_meeting_supervisor import FakeHealth, FakeLauncher, session
from test_phone import PUBLIC, PhoneHarness
from test_runtime_daemon import FakeSupervisor
from test_schemas import context_payload
from twilio_client import compute_signature


LOCAL = {'Authorization': 'Bearer launch-token'}


class OwnedCalls:
    """Earlier calls, by owner."""

    def __init__(self, records):
        self.records = records

    def list(self, *, owner=None, limit=20):
        return [record for record in self.records if owner is None or record['owner'] == owner][:limit]


class TwoOwners:
    """A daemon with the launch token (local), an internal gateway token, and owned tokens."""

    def __init__(self, temp, hooks_class=DefaultCallHooks, environ=None):
        self.root = Path(temp)
        self.store = CallStore(self.root / 'calls')
        self.notifier = RecordingNotifier()
        self.hooks = hooks_class(environ=environ or {'OPENAI_API_KEY': 'sk-test'},
                                 env_file=self.root / '.env', store=self.store,
                                 notifier=self.notifier, clock=DAYTIME)
        self.line = FakeLine()
        self.service = CallService(self.store, hooks=self.hooks, lines={'phone': self.line},
                                   summarizer_factory=lambda owner: FakeSummarizer(), environ={},
                                   shutdown_grace=0.2)
        self.tokens = ApiTokenStore(self.root / 'api-tokens.json')
        self.internal, _ = self.tokens.create('gateway', kind='internal')
        self.standard, _ = self.tokens.create('script')
        self.owned, _ = self.tokens.create('account b', owner='acct_b')
        self.supervisor = FakeSupervisor()
        self.app = create_app(root=self.root / 'daemon', auth_token='launch-token',
                              call_service=self.service, api_tokens=self.tokens,
                              supervisor=self.supervisor)
        self.client = TestClient(TestServer(self.app))

    def as_owner(self, owner):
        return {'Authorization': f'Bearer {self.internal}', 'X-Smitline-Owner': owner}

    async def start(self):
        await self.client.start_server()
        return self

    async def close(self):
        await self.client.close()


class OwnerApiTests(unittest.IsolatedAsyncioTestCase):
    hooks_class = DefaultCallHooks

    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.d = await TwoOwners(self.temp.name, self.hooks_class).start()
        self.client = self.d.client
        self.a = self.d.as_owner('acct_a')

    async def asyncTearDown(self):
        await self.d.close()
        self.temp.cleanup()

    async def post_call(self, headers, **overrides):
        return await self.client.post('/v1/calls', json=brief(**overrides), headers=headers)

    async def test_calls_are_isolated_between_owners(self):
        response = await self.post_call(self.a)
        self.assertEqual(response.status, 201)
        theirs = await response.json()
        self.assertEqual(theirs['owner'], 'acct_a')
        mine = await (await self.post_call(LOCAL, to='+14155550199')).json()
        self.assertEqual(mine['owner'], 'local')

        listed = await (await self.client.get('/v1/calls', headers=LOCAL)).json()
        self.assertEqual([call['id'] for call in listed['calls']], [mine['id']])
        listed = await (await self.client.get('/v1/calls', headers=self.a)).json()
        self.assertEqual([call['id'] for call in listed['calls']], [theirs['id']])
        other = self.d.as_owner('acct_c')
        self.assertEqual((await (await self.client.get('/v1/calls', headers=other)).json())['calls'], [])

        # Another owner's call is not found, whatever is asked of it.
        for method, path in (('GET', ''), ('GET', '/wait?timeout=0'), ('GET', '/events?format=json'),
                             ('POST', '/end'), ('POST', '/transfer'), ('GET', '/recording')):
            response = await self.client.request(method, f'/v1/calls/{theirs["id"]}{path}', headers=LOCAL,
                                                 json={} if method == 'POST' else None)
            self.assertEqual(response.status, 404, path)
        response = await self.client.post(f'/v1/calls/{theirs["id"]}/instructions', headers=LOCAL,
                                          json={'text': 'Ask about parking.'})
        self.assertEqual(response.status, 404)
        self.assertEqual(self.d.line.instructions, [])

        response = await self.client.post(f'/v1/calls/{theirs["id"]}/end', headers=self.a, json={})
        self.assertEqual(response.status, 200)
        done = await (await self.client.get(f'/v1/calls/{theirs["id"]}/wait?timeout=5', headers=self.a)).json()
        self.assertEqual(done['status'], 'completed')
        self.assertEqual(self.d.service.active_count(owner='acct_a'), 0)

    async def test_who_may_name_the_owner(self):
        for headers in ({'Authorization': f'Bearer {self.d.standard}', 'X-Smitline-Owner': 'acct_a'},
                        dict(LOCAL, **{'X-Smitline-Owner': 'acct_a'})):
            response = await self.client.get('/v1/calls', headers=headers)
            self.assertEqual(response.status, 403)
            self.assertEqual((await response.json())['error']['code'], 'owner_header_forbidden')
        for bad in ('Acct_A', '', '../x', 'a' * 65, '-a'):
            response = await self.client.get('/v1/calls', headers=self.d.as_owner(bad))
            self.assertEqual(response.status, 400, bad)
            self.assertEqual((await response.json())['error']['code'], 'invalid_owner')
        # A token created for an owner always acts as it.
        owned = {'Authorization': f'Bearer {self.d.owned}'}
        call = await (await self.post_call(owned)).json()
        self.assertEqual(call['owner'], 'acct_b')
        self.assertEqual((await self.client.get(f'/v1/calls/{call["id"]}', headers=LOCAL)).status, 404)
        self.assertEqual((await self.client.get(f'/v1/calls/{call["id"]}', headers=owned)).status, 200)
        # A standard token without an owner is the local owner, as before.
        standard = {'Authorization': f'Bearer {self.d.standard}'}
        mine = await (await self.post_call(LOCAL, to='+14155550199')).json()
        self.assertEqual((await self.client.get(f'/v1/calls/{mine["id"]}', headers=standard)).status, 200)

    async def test_contacts_do_not_call_and_profile_are_per_owner(self):
        root = self.d.root / '.smitline'
        response = await self.client.patch('/v1/contacts/+14155550199', headers=self.a,
                                           json={'name': 'Sam', 'notes': 'Plumber'})
        self.assertEqual(response.status, 200)
        self.assertTrue((root / 'owners' / 'acct_a' / 'contacts.json').is_file())
        self.assertFalse((root / 'contacts.json').exists())
        local = await (await self.client.get('/v1/contacts', headers=LOCAL)).json()
        self.assertEqual(local['contacts'], [])
        self.assertEqual((await self.client.get('/v1/contacts/+14155550199', headers=LOCAL)).status, 404)

        response = await self.client.patch('/v1/do-not-call', headers=self.a,
                                           json={'add': ['+14155550142']})
        self.assertEqual(response.status, 200)
        self.assertTrue((root / 'owners' / 'acct_a' / 'do-not-call.json').is_file())
        self.assertEqual(await (await self.client.get('/v1/do-not-call', headers=LOCAL)).json(),
                         {'numbers': []})
        refused = await self.post_call(self.a)
        self.assertEqual(refused.status, 403)
        self.assertEqual((await refused.json())['error']['code'], 'do_not_call')
        self.assertEqual((await self.post_call(LOCAL)).status, 201)

        response = await self.client.patch('/v1/profile', headers=self.a, json=PROFILE)
        self.assertEqual(response.status, 200)
        saved = json.loads((root / 'owners' / 'acct_a' / 'profile.json').read_text())
        self.assertEqual(saved['people'][0]['name'], 'Sam')
        self.assertEqual(await (await self.client.get('/v1/profile', headers=LOCAL)).json(), {'version': 1})
        await self.client.patch('/v1/profile', headers=LOCAL, json={'about': 'Robin.'})
        self.assertEqual(json.loads((root / 'profile.json').read_text())['about'], 'Robin.')
        theirs = await (await self.client.get('/v1/profile', headers=self.a)).json()
        self.assertEqual(theirs['about'], PROFILE['about'])

    async def test_meeting_sessions_belong_to_an_owner(self):
        daemon = self.d.app.runtime_daemon
        mine = await daemon.create_meeting({'meetingUrl': ZOOM, 'context': context_payload()})
        theirs = await daemon.create_meeting({'meetingUrl': ZOOM, 'context': context_payload()},
                                             owner='acct_a')
        self.assertEqual((mine.owner, theirs.owner), ('local', 'acct_a'))
        response = await self.client.get(f'/v1/meetings/{mine.id}', headers=LOCAL)
        self.assertEqual(response.status, 200)
        self.assertNotIn('owner', await response.json())
        for path in ('', '/handoff', '/events'):
            response = await self.client.get(f'/v1/meetings/{theirs.id}{path}', headers=LOCAL)
            self.assertEqual(response.status, 404, path)
        response = await self.client.post(f'/v1/meetings/{theirs.id}/cancel', headers=LOCAL)
        self.assertEqual(response.status, 404)
        self.assertEqual(self.d.supervisor.cancelled, [])
        # The raw meeting API acts for the local owner only.
        response = await self.client.get(f'/v1/meetings/{mine.id}', headers=self.a)
        self.assertEqual(response.status, 403)
        self.assertEqual((await response.json())['error']['code'], 'local_owner_only')
        response = await self.client.post('/v1/meetings', headers=self.a, json={'meetingUrl': ZOOM})
        self.assertEqual(response.status, 403)
        with self.assertRaises(DaemonError):
            await daemon.get_meeting(theirs.id, owner='acct_b')
        self.assertEqual((await daemon.get_meeting(theirs.id, owner='acct_a')).id, theirs.id)
        # The owner is kept with the snapshot; the local owner's snapshot looks as before.
        stored = MeetingRepository(self.d.root / 'daemon' / 'meetings')
        self.assertEqual(stored.get(theirs.id).session.owner, 'acct_a')
        self.assertNotIn('owner', stored.get(mine.id).session.to_dict())
        self.assertEqual(self.d.supervisor.sessions[1].owner, 'acct_a')

    async def test_voices_follow_the_owner_settings(self):
        class Voices(DefaultCallHooks):
            def settings(self, owner):
                env = super().settings(owner)
                return dict(env, SMITLINE_EXTRA_VOICES='aria', SMITLINE_VOICE='aria') if owner == 'acct_a' else env
        self.d.service.hooks = Voices(environ={'OPENAI_API_KEY': 'k'}, store=self.d.store, clock=DAYTIME)
        theirs = await (await self.client.get('/v1/voices', headers=self.a)).json()
        self.assertEqual(theirs['default'], 'aria')
        self.assertIn('aria', theirs['voices'])
        mine = await (await self.client.get('/v1/voices', headers=LOCAL)).json()
        self.assertNotIn('aria', mine['voices'])
        # A brief may use a voice only its owner has.
        self.assertEqual((await self.post_call(self.a, voice='aria')).status, 201)
        self.assertEqual((await self.post_call(LOCAL, voice='aria', to='+14155550199')).status, 422)


class Wallet(DefaultCallHooks):
    """A hosted service's hooks: a wallet check before, a hold when a call starts."""
    started = []
    fail_start = None

    def precheck(self, owner, brief):
        super().precheck(owner, brief)
        if owner == 'acct_poor':
            raise CallRefused('insufficient_credit', 'Top up to call.', status=402, balance='4.20',
                              affordableMinutes=21, topUpUrl='https://example.com/wallet')

    async def call_started(self, owner, record):
        await asyncio.sleep(0)
        if self.fail_start is not None:
            raise self.fail_start
        Wallet.started.append((owner, record['id'], record['status']))


class HookTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        Wallet.started = []
        Wallet.fail_start = None
        self.d = await TwoOwners(self.temp.name, Wallet).start()
        self.client = self.d.client

    async def asyncTearDown(self):
        await self.d.close()
        self.temp.cleanup()

    async def test_refusals_carry_status_and_details(self):
        poor = self.d.as_owner('acct_poor')
        response = await self.client.post('/v1/calls', json=brief(), headers=poor)
        self.assertEqual(response.status, 402)
        error = (await response.json())['error']
        self.assertEqual(error, {'code': 'insufficient_credit', 'message': 'Top up to call.',
                                 'balance': '4.20', 'affordableMinutes': 21,
                                 'topUpUrl': 'https://example.com/wallet'})
        self.assertEqual(self.d.store.list(limit=None), [])
        checked = await (await self.client.post('/v1/calls/check', json=brief(), headers=poor)).json()
        self.assertEqual((checked['ok'], checked['problems']), (False, ['Top up to call.']))

    async def test_call_started_runs_before_the_line(self):
        response = await self.client.post('/v1/calls', json=brief(), headers=self.d.as_owner('acct_a'))
        call = await response.json()
        self.assertEqual(Wallet.started, [('acct_a', call['id'], 'queued')])

    async def test_a_refused_start_leaves_no_call_waiting(self):
        Wallet.fail_start = CallRefused('too_many_active_calls', 'One call at a time.', status=429)
        response = await self.client.post('/v1/calls', json=brief(), headers=self.d.as_owner('acct_a'))
        self.assertEqual(response.status, 429)
        self.assertEqual((await response.json())['error']['code'], 'too_many_active_calls')
        [record] = self.d.store.list(limit=None)
        self.assertEqual((record['status'], record['endReason'], record['error']),
                         ('failed', 'refused', 'One call at a time.'))
        self.assertEqual(self.d.service.active_count(), 0)
        self.assertEqual(self.d.line.contexts, {})

        Wallet.fail_start = RuntimeError('ledger down')
        response = await self.client.post('/v1/calls', json=brief(to='+14155550199'),
                                          headers=self.d.as_owner('acct_a'))
        self.assertEqual(response.status, 503)
        self.assertEqual((await response.json())['error']['code'], 'call_start_failed')
        latest = self.d.store.list(limit=1)[0]
        self.assertEqual(latest['status'], 'failed')
        events = [event['type'] for event in self.d.store.events(latest['id'])]
        self.assertIn('call.started_hook_failed', events)
        self.assertEqual(self.d.service.active_count(), 0)

    async def test_active_calls_are_counted_per_owner(self):
        await self.client.post('/v1/calls', json=brief(), headers=LOCAL)
        self.assertEqual(self.d.service.active_count(owner='local'), 1)
        self.assertEqual(self.d.service.active_count(owner='acct_a'), 0)
        self.assertEqual(self.d.service.active_count(), 1)


class RequireOwnerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.d = await TwoOwners(self.temp.name, environ={
            'OPENAI_API_KEY': 'sk-test', 'SMITLINE_REQUIRE_OWNER': '1',
            'SMITLINE_ACCEPT_INBOUND': '1'}).start()
        self.client = self.d.client

    async def asyncTearDown(self):
        await self.d.close()
        self.temp.cleanup()

    async def test_nothing_acts_as_the_local_owner(self):
        for headers in (LOCAL, {'Authorization': f'Bearer {self.d.internal}'},
                        {'Authorization': f'Bearer {self.d.standard}'}):
            response = await self.client.get('/v1/calls', headers=headers)
            self.assertEqual(response.status, 401)
            self.assertEqual((await response.json())['error']['code'], 'owner_required')
        self.assertEqual((await self.client.get('/v1/calls', headers=self.d.as_owner('acct_a'))).status, 200)
        owned = {'Authorization': f'Bearer {self.d.owned}'}
        self.assertEqual((await self.client.get('/v1/profile', headers=owned)).status, 200)
        for headers in (LOCAL, self.d.as_owner('acct_a')):
            response = await self.client.post('/v1/meetings', json={'meetingUrl': ZOOM}, headers=headers)
            self.assertEqual(response.status, 403)
            self.assertEqual((await response.json())['error']['code'], 'local_owner_only')
        self.assertEqual(self.d.supervisor.started, [])
        # No local owner takes incoming calls either, and no request means no owner.
        self.assertIsNone(self.d.hooks.owner_for_number('+15005550006'))
        with self.assertRaises(CallRefused) as refused:
            self.d.hooks.owner_for(None)
        self.assertEqual(refused.exception.status, 401)


class OwnerStorageTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_local_owner_keeps_its_files(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            hooks = DefaultCallHooks(environ={}, env_file=root / '.env',
                                     notifier=WebhookNotifier('installation-secret'))
            base = root / '.smitline'
            self.assertEqual(hooks.owner_for(None), 'local')
            self.assertEqual(hooks.storage_root('local'), base)
            self.assertEqual(hooks.contact_book('local').path, base / 'contacts.json')
            self.assertEqual(hooks.do_not_call_list('local').path, base / 'do-not-call.json')
            self.assertEqual(hooks.profile_path, base / 'profile.json')
            self.assertEqual(hooks.profile_path_for('local'), base / 'profile.json')
            self.assertEqual(hooks.webhook_secret('local'), 'installation-secret')
            self.assertEqual(hooks.storage_root('acct_a'), base / 'owners' / 'acct_a')
            self.assertEqual(hooks.contact_book('acct_a').path, base / 'owners' / 'acct_a' / 'contacts.json')
            with self.assertRaises(ValueError):
                hooks.storage_root('../escape')
            self.assertFalse((base / 'owners').exists())
            # Inbound calls go to the local owner only when they are switched on.
            self.assertIsNone(hooks.owner_for_number('+15005550006'))
            on = DefaultCallHooks(environ={'SMITLINE_ACCEPT_INBOUND': '1'})
            self.assertEqual(on.owner_for_number('+15005550006'), 'local')

    def test_repeat_limits_count_only_the_owner_calls(self):
        now = at(18)()
        number = '+14155550142'
        records = [dict(placed_at(number, 60 * hours, now), owner='acct_b') for hours in (1, 3, 5, 7, 9)]
        hooks = DefaultCallHooks(environ={}, store=OwnedCalls(records), clock=at(18))
        hooks.precheck('acct_a', CallBrief.from_dict(phone_brief()))
        hooks.precheck('local', CallBrief.from_dict(phone_brief()))
        with self.assertRaises(CallRefused) as refused:
            hooks.precheck('acct_b', CallBrief.from_dict(phone_brief()))
        self.assertEqual(refused.exception.code, 'too_many_calls_to_number')
        self.assertEqual(refused.exception.status, 403)

    async def test_each_owner_signs_webhooks_with_its_own_secret(self):
        with tempfile.TemporaryDirectory() as temp:
            posted = []

            async def post(url, body, headers):
                posted.append((body, headers['X-Smitline-Signature']))
                return 200
            notifier = WebhookNotifier('installation-secret', post=post, allow_private=True)
            hooks = DefaultCallHooks(environ={}, env_file=Path(temp) / '.env', notifier=notifier)
            call = {'id': new_call_id(), 'status': 'completed',
                    'brief': {'notify': {'webhookUrl': 'https://example.com/hook'}}}
            await hooks.notify('local', call)
            await hooks.notify('acct_a', call)
            path = Path(temp) / '.smitline' / 'owners' / 'acct_a' / 'webhook.secret'
            secret = path.read_text().strip()
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertNotEqual(secret, 'installation-secret')
            self.assertEqual(posted[0][1], signature('installation-secret', posted[0][0]))
            self.assertEqual(posted[1][1], signature(secret, posted[1][0]))
            self.assertEqual(hooks.webhook_secret('acct_a'), secret)

    def test_api_tokens_carry_an_owner_and_a_kind(self):
        with tempfile.TemporaryDirectory() as temp:
            store = ApiTokenStore(Path(temp) / 'tokens.json')
            plain, entry = store.create('script')
            self.assertEqual(entry['kind'], 'standard')
            self.assertNotIn('owner', entry)
            internal, _ = store.create('gateway', kind='internal')
            owned, _ = store.create('acct', owner='acct_3f9c2a7b1d')
            self.assertEqual(store.verify(internal)['kind'], 'internal')
            self.assertEqual(store.verify(owned)['owner'], 'acct_3f9c2a7b1d')
            self.assertEqual(store.verify(plain)['kind'], 'standard')
            for bad in ({'kind': 'root'}, {'owner': 'Not An Id'}):
                with self.assertRaises(ValueError):
                    store.create('bad', **bad)
            out = io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
                code = tokens_main(['--root', temp, 'create', '--name', 'cloud-gateway', '--kind', 'internal'])
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(out.getvalue())['kind'], 'internal')

    def test_meeting_records_written_before_owners_are_local(self):
        payload = session().to_dict()
        self.assertNotIn('owner', payload)
        self.assertEqual(MeetingSession.from_dict(payload).owner, 'local')
        owned = MeetingSession.from_dict(dict(payload, owner='acct_a'))
        self.assertEqual(owned.to_dict()['owner'], 'acct_a')
        with self.assertRaises(ValueError):
            MeetingSession.from_dict(dict(payload, owner='ACCT'))


class TwoAccounts(DefaultCallHooks):
    """Each owner has its own phone credentials and its own number."""
    AUTH = {'local': 'tw-secret', 'acct_a': 'tw-acct-a'}
    NUMBERS = {'+15005550006': 'local', '+15005550007': 'acct_a'}

    def settings(self, owner):
        env = super().settings(owner)
        return dict(env, SMITLINE_OWNER_NAME='Ana') if owner == 'acct_a' else env

    def credentials(self, owner, provider):
        values = super().credentials(owner, provider)
        return dict(values, authToken=self.AUTH[owner]) if provider == 'twilio' else values

    def owner_for_number(self, number):
        return self.NUMBERS.get(number)


class GatewayOwnerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.h = PhoneHarness(self.temp.name)
        self.h.service.hooks = TwoAccounts(environ=self.h.env, store=self.h.store, clock=DAYTIME)
        app = create_gateway_app(self.h.line, self.h.service, current_url=lambda: PUBLIC)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        await self.h.service.shutdown()
        self.temp.cleanup()

    async def post_signed(self, path, params, token):
        signed = compute_signature(token, PUBLIC + path, params)
        return await self.client.post(path, data=params, headers={'X-Twilio-Signature': signed})

    def record(self, owner):
        now = self.h.store.now()
        record = {'version': 1, 'id': new_call_id(), 'owner': owner, 'channel': 'phone',
                  'direction': 'outbound', 'status': 'ringing', 'brief': {'to': '+14155550142'},
                  'createdAt': now, 'updatedAt': now, 'line': {}, 'result': None, 'usage': {}}
        self.h.store.create(record)
        return record

    async def test_callbacks_are_checked_with_the_call_owner_credentials(self):
        theirs, mine = self.record('acct_a'), self.record('local')
        for kind in ('status', 'amd', 'recording'):
            path = f'/twilio/{kind}/{theirs["id"]}'
            self.assertEqual((await self.post_signed(path, {'CallStatus': 'ringing'}, 'tw-secret')).status, 403)
            self.assertEqual((await self.post_signed(path, {'CallStatus': 'ringing'}, 'tw-acct-a')).status, 204)
            path = f'/twilio/{kind}/{mine["id"]}'
            self.assertEqual((await self.post_signed(path, {'CallStatus': 'ringing'}, 'tw-acct-a')).status, 403)
            self.assertEqual((await self.post_signed(path, {'CallStatus': 'ringing'}, 'tw-secret')).status, 204)

    async def test_incoming_calls_go_to_the_number_owner(self):
        params = {'From': '+14155550142', 'To': '+15005550007', 'CallSid': 'CA9'}
        self.assertEqual((await self.post_signed('/twilio/inbound', params, 'tw-secret')).status, 403)
        response = await self.post_signed('/twilio/inbound', params, 'tw-acct-a')
        self.assertEqual(response.status, 200)
        self.assertIn('<Stream', await response.text())
        [call] = self.h.service.list(owner='acct_a')
        self.assertEqual((call['direction'], call['brief']['onBehalfOf']), ('inbound', 'Ana'))
        self.assertEqual(self.h.service.list(owner='local'), [])
        # A number nobody owns is turned away.
        params = dict(params, To='+15005550008', CallSid='CA10')
        response = await self.post_signed('/twilio/inbound', params, 'tw-secret')
        self.assertIn('<Reject', await response.text())
        self.assertEqual(len(self.h.store.list(limit=None)), 1)


class MeetingOwnerEnvironmentTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.runtime = root / 'meeting-runtime'
        self.runtime.mkdir()
        self.seen = []
        test = self

        class Launcher(FakeLauncher):
            async def up(self, env):
                path = env.get('SMITLINE_MEETING_ENV_FILE')
                if path:
                    test.seen.append((Path(path).read_text(), os.stat(path).st_mode & 0o777))
                await super().up(env)
        self.launcher = Launcher()
        hooks = DefaultCallHooks(environ={'OPENAI_API_KEY': 'sk-shared', 'SMITLINE_VOICE': 'cedar',
                                          'SMITLINE_OWNER_NAME': "Ana O'Neil",
                                          'SMITLINE_PARTICIPANT_NAME': "Ana's assistant"})

        def supervisor(claim=None):
            return ProductionMeetingSupervisor(
                root, runtime_root=self.runtime, launcher=self.launcher, health=FakeHealth(),
                poll_interval=0.02, start_timeout=0.4, stop_timeout=0.4, claim=claim,
                owner_environment=lambda owner: meeting_environment(hooks, owner),
            ).bind_daemon(FakeSupervisorDaemon())
        self.make_supervisor = supervisor
        self.root = root
        self.supervisor = supervisor()

    async def asyncTearDown(self):
        await self.supervisor.shutdown()
        self.temporary.cleanup()

    async def test_an_owner_meeting_gets_its_key_voice_and_name(self):
        meeting = MeetingSession.from_dict(dict(session().to_dict(), owner='acct_a'))
        # Another owner's meeting runs in the pool: one container at a time refuses it.
        await self.supervisor.shutdown()
        self.supervisor = self.make_supervisor(claim={'path': pool_claim_path(self.root, meeting.id),
                                                      'record': {'owner': 'acct_a'}})
        await self.supervisor.start(meeting)
        [(body, mode)] = self.seen
        self.assertEqual(mode, 0o600)
        self.assertIn('OPENAI_API_KEY="sk-shared"\n', body)
        self.assertIn('SMITLINE_VOICE="cedar"\n', body)
        self.assertIn('SMITLINE_OWNER_NAME="Ana O\'Neil"\n', body)
        self.assertFalse(Path(self.launcher.ups[0]['SMITLINE_MEETING_ENV_FILE']).exists())
        # The owner's participant name reaches the meeting's runtime state, which the
        # container reads over its environment.
        state = read_json(meeting_state_path(self.runtime, meeting.id))
        self.assertEqual(state['participantName'], "Ana's assistant")
        self.assertEqual(RuntimeConfig.from_environ({
            'SMITLINE_RUNTIME_STATE': str(meeting_state_path(self.runtime, meeting.id))}).participant_name,
            "Ana's assistant")
        await self.supervisor.cancel(meeting.id)

    async def test_the_local_owner_meeting_reads_the_shared_env(self):
        meeting = session()
        await self.supervisor.start(meeting)
        self.assertNotIn('SMITLINE_MEETING_ENV_FILE', self.launcher.ups[0])
        self.assertEqual(self.seen, [])
        state = read_json(meeting_state_path(self.runtime, meeting.id))
        self.assertEqual(state['participantName'], 'Smitline AI')
        await self.supervisor.cancel(meeting.id)

    def test_env_file_values_arrive_exactly(self):
        from meeting_supervisor import owner_env_file
        body = owner_env_file({'A': "O'Neil", 'B': 'say "hi" \\ $HOME', 'C': 'two\nlines', 'D': ''})
        # A value compose cannot carry, or none, is blank: never the shared .env's value.
        self.assertEqual(body, 'A="O\'Neil"\nB="say \\"hi\\" \\\\ \\$HOME"\nC=""\nD=""\n')


if __name__ == '__main__':
    unittest.main()
