"""What an agent can tell the user about Smitline (how_to.py, GET /v1/how-to, the MCP tool
how_to_smitline): this installation's own text for each topic, read from the owner's settings, and
the optional how_to hook a hosted service uses to say something else or more, live."""
import tempfile
import unittest
from pathlib import Path

from aiohttp.test_utils import TestClient, TestServer

from call_hooks import CallRefused, DefaultCallHooks
from call_service import CallService
from call_store import CallStore
import how_to
from runtime_daemon import create_app

# What a self-hosted installation never says: a hosted service's prices and plans.
HOSTED = ('$', 'Free plan', 'Pro', 'wallet at', 'app.smitline.com', 'support@smitline.com')


class GuideTests(unittest.TestCase):
    def test_every_topic_has_short_plain_text_and_named_https_links(self):
        self.assertEqual(how_to.TOPICS, ('overview', 'using', 'following', 'pricing', 'account', 'troubleshooting', 'rules'))
        for topic in how_to.TOPICS:
            with self.subTest(topic=topic):
                found = how_to.guide(topic, {})
                self.assertEqual(set(found), {'topic', 'text', 'links'})
                self.assertEqual(found['topic'], topic)
                self.assertTrue(found['text'].strip())
                self.assertLess(len(found['text']), 1500)
                self.assertNotIn('**', found['text'])  # plain text, no Markdown
                self.assertTrue(found['links'])
                for link in found['links']:
                    self.assertEqual(set(link), {'name', 'url'})
                    self.assertTrue(link['url'].startswith('https://'), link)
                for hosted in HOSTED:
                    self.assertNotIn(hosted, found['text'])

    def test_overview_says_what_smitline_is_and_who_makes_it(self):
        text = how_to.guide('overview', {})['text']
        for fact in ('make phone calls', 'Zoom, Microsoft Teams and Google Meet', 'start a meeting room',
                     'follows the call live', 'open-source', 'Apache-2.0', 'github.com/kaelorlabs/smitline',
                     'io.github.kaelorlabs/smitline', 'made by Kaelor Labs', 'Smitline Cloud'):
            self.assertIn(fact, text)
        urls = [link['url'] for link in how_to.guide('overview', {})['links']]
        self.assertIn('https://github.com/kaelorlabs/smitline', urls)

    def test_overview_ends_with_first_things_to_try(self):
        # Arun, 2026-11-06: right after connecting, an agent offers both, a call to the user's own
        # phone or a real errand, and says where to follow a call and hear it.
        guide = how_to.guide('overview', {})
        last = guide['text'].split('\n\n')[-1]
        for fact in ('To try it', '"call me now"', 'your own phone', 'a local restaurant', 'book a table',
                     'order dinner', 'http://127.0.0.1:8095/calls', 'Listen in', 'SMITLINE_LISTEN is 1'):
            self.assertIn(fact, last)
        self.assertIn(how_to.DOCS + 'control-panel.md', [link['url'] for link in guide['links']])

    def test_pricing_and_account_say_the_owner_pays_their_own_providers(self):
        for topic in ('pricing', 'account'):
            text = how_to.guide(topic, {})['text']
            self.assertIn('self-hosted', text)
            self.assertIn('OpenAI', text)
            self.assertIn('SignalWire or Twilio', text)
        self.assertIn('no Smitline subscription, plan or wallet', how_to.guide('pricing', {})['text'])

    def test_using_informs_the_user_and_gives_the_agent_no_orders(self):
        text = how_to.guide('using', {})['text']
        for fact in ('brief', 'until it ends', 'canDo', 'dialIn', 'to "new"', 'a minute or two', 'list_voices',
                     'rehearsal', 'the facts the call needs'):
            self.assertIn(fact, text)
        self.assertIn('(topic following)', text)
        for order in ('You must', 'you must', 'Always ', 'Never ', 'Call wait_for_call', 'Do not'):
            self.assertNotIn(order, text)

    def test_following_explains_the_two_ways_to_run_a_call(self):
        found = how_to.guide('following', {})
        let_run, live = found['text'].split('\n\n')
        self.assertTrue(let_run.startswith('Start it and let it run:'))
        for fact in ('complete brief', 'returns the result when the call ends', 'get_call or wait_for_call',
                     'cannot stay', 'the user will follow up', 'openRequests'):
            self.assertIn(fact, let_run)
        self.assertTrue(live.startswith('Follow it live, which is better whenever possible:'))
        for fact in ('keeps calling wait_for_call in the same turn', 'since "" the first time, then live.cursor',
                     'completed, failed or canceled', "next says what to do", 'at once when someone on the call asks',
                     'about every 12 seconds', 'canDo', 'live.requests', 'reply_to_call_request', 'send_call_instruction',
                     'end_call', 'Always allow', 'wait_for_call, get_call and list_calls',
                     'send_call_instruction and reply_to_call_request'):
            self.assertIn(fact, live)
        # How Smitline works, not orders: the agent is spoken of, not to.
        for order in ('You must', 'you must', 'Keep calling', 'Call wait_for_call', 'Do not', 'Never '):
            self.assertNotIn(order, found['text'])
        self.assertEqual(found['links'], [{'name': 'Following a call live',
                                           'url': how_to.DOCS + 'agents.md#following-a-call-live'}])

    def test_following_says_what_the_daemon_does(self):
        # The wait it describes is the daemon's: about every 12 seconds of talk.
        from call_service import LIVE_SETTLE_SECONDS
        self.assertEqual(LIVE_SETTLE_SECONDS, 12.0)
        self.assertIn('about every 12 seconds', how_to.guide('following', {})['text'])

    def test_the_account_reads_the_owners_settings_live(self):
        text = how_to.guide('account', {})['text']
        self.assertIn("between 8:00 AM and 9:00 PM in the other person's time", text)
        self.assertIn('at most 5 times to one number in any 24 hours, and at most 20 times an hour', text)
        self.assertNotIn('calling codes +', text)
        text = how_to.guide('account', {'SMITLINE_CALLING_HOURS': '09:30-17:00', 'SMITLINE_MAX_CALLS_PER_NUMBER': '2',
                                        'SMITLINE_MAX_CALLS_PER_HOUR': '0',
                                        'SMITLINE_ALLOWED_CALLING_CODES': '1, +44'})['text']
        self.assertIn("between 9:30 AM and 5:00 PM in the other person's time", text)
        self.assertIn('at most 2 times to one number in any 24 hours, and with no hourly limit', text)
        self.assertIn('calling codes +1, +44 only', text)
        text = how_to.guide('account', {'SMITLINE_CALLING_HOURS': 'off', 'SMITLINE_MAX_CALLS_PER_NUMBER': '0'})['text']
        self.assertIn('at any hour (calling hours are off)', text)
        self.assertIn('with no limit on calls to one number', text)
        # A setting the guardrails cannot read refuses calls; the guide still answers.
        text = how_to.guide('account', {'SMITLINE_CALLING_HOURS': 'nine to five', 'SMITLINE_MAX_CALLS_PER_HOUR': 'many'})['text']
        self.assertIn('during the calling hours set in SMITLINE_CALLING_HOURS', text)
        self.assertIn('up to the repeat limits in its settings', text)

    def test_rules_and_troubleshooting_use_the_same_calling_hours(self):
        late = {'SMITLINE_CALLING_HOURS': '10:00-18:00'}
        for topic in ('rules', 'troubleshooting'):
            self.assertIn("between 8:00 AM and 9:00 PM in the other person's time", how_to.guide(topic, {})['text'])
            self.assertIn("between 10:00 AM and 6:00 PM in the other person's time", how_to.guide(topic, late)['text'])
        rules = how_to.guide('rules', {})['text']
        for fact in ('it is an AI', 'emergency, crisis or premium-rate numbers', 'do-not-call', 'responsible'):
            self.assertIn(fact, rules)
        trouble = how_to.guide('troubleshooting', {})['text']
        for code in ('outside_calling_hours', 'too_many_calls_to_number', 'do_not_call', 'high_cost_number',
                     'dialIn', 'not_reached', 'voicemail'):
            self.assertIn(code, trouble)

    def test_a_hooks_answer_replaces_the_text_the_links_or_both(self):
        found = how_to.guide('overview', {})
        self.assertIs(how_to.checked_change(found, None), found)
        text_only = how_to.checked_change(found, {'text': '  Hosted.  '})
        self.assertEqual((text_only['text'], text_only['links']), ('Hosted.', found['links']))
        links_only = how_to.checked_change(found, {'links': [{'name': 'Site', 'url': 'https://smitline.com', 'x': 1}]})
        self.assertEqual((links_only['text'], links_only['links']),
                         (found['text'], [{'name': 'Site', 'url': 'https://smitline.com'}]))
        both = how_to.checked_change(found, {'text': 'Hosted.', 'links': []})
        self.assertEqual((both['topic'], both['text'], both['links']), ('overview', 'Hosted.', []))
        for bad in ('Hosted.', ['Hosted.'], {'text': ''}, {'text': '   '}, {'text': 7}, {'links': 'https://smitline.com'},
                    {'links': [{'name': 'Site'}]}, {'links': [{'name': 'Site', 'url': 7}]}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                how_to.checked_change(found, bad)


class Hosted(DefaultCallHooks):
    """A hosted service's hooks: its own text for the account topic, more for the overview."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.asked = []
        self.answer = None

    async def how_to(self, owner, topic, guide, *, client=None):
        self.asked.append((owner, topic, client))
        if self.answer is not None:
            return self.answer(owner, topic, guide, client)
        if topic == 'account':
            return {'text': f'{owner} is on the Free plan.', 'links': [{'name': 'Wallet', 'url': 'https://app.test/wallet'}]}
        if topic == 'overview':
            guide['links'].append({'name': 'Changed in place', 'url': 'https://example.test'})
            return {'text': guide['text'] + '\n\nThis is the hosted service.'}
        return None


class DaemonCase(unittest.IsolatedAsyncioTestCase):
    """The daemon's routes with a CallService on hooks_class."""
    hooks_class = DefaultCallHooks

    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.environ = {'OPENAI_API_KEY': 'sk-test'}
        store = CallStore(root / 'calls')
        self.hooks = self.hooks_class(environ=self.environ, env_file=root / '.env', store=store)
        self.service = CallService(store, hooks=self.hooks, lines={}, environ={})
        app = create_app(root=root / 'daemon', auth_token='launch-token', call_service=self.service)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()
        self.auth = {'Authorization': 'Bearer launch-token'}

    async def asyncTearDown(self):
        await self.client.close()
        self.temp.cleanup()

    async def get(self, path, headers=None):
        response = await self.client.get(path, headers=self.auth if headers is None else headers)
        return response.status, await response.json()

    async def refused(self, query, says):
        status, body = await self.get(f'/v1/how-to?{query}')
        self.assertEqual((status, body['error']['code']), (422, 'invalid_request'), query)
        self.assertIn(says, body['error']['message'])


# What is refused before anything is read: (query, what the refusal says).
REFUSED = (('topic=prices', 'topic must be one of: overview, using, following, pricing, account, troubleshooting, rules'),
           ('topic=', 'topic must be one of'), ('topic=ACCOUNT', 'topic must be one of'),
           ('client=', 'client must be an app name: lowercase letters, digits and dashes, such as chatgpt'),
           ('client=ChatGPT', 'client must be an app name'), ('client=chat%20gpt', 'client must be an app name'),
           ('client=-claude', 'client must be an app name'), ('client=' + 'a' * 33, 'client must be an app name'))
# Apps a hosted gateway may name: any name of the shape is passed on, known or not.
APPS = ('chatgpt', 'claude', 'grok', 'muse', 'mistral', 'perplexity', 'other', 'some-new-app2')


class HowToApiTests(DaemonCase):
    async def test_each_topic_is_served_and_overview_is_the_default(self):
        status, body = await self.get('/v1/how-to')
        self.assertEqual(status, 200)
        self.assertEqual(body, how_to.guide('overview', self.environ))
        for topic in how_to.TOPICS:
            with self.subTest(topic=topic):
                status, body = await self.get(f'/v1/how-to?topic={topic}')
                self.assertEqual((status, body), (200, how_to.guide(topic, self.environ)))

    async def test_the_account_topic_reads_the_settings_on_each_request(self):
        _, before = await self.get('/v1/how-to?topic=account')
        self.assertIn('at most 20 times an hour', before['text'])
        self.environ['SMITLINE_MAX_CALLS_PER_HOUR'] = '3'
        _, after = await self.get('/v1/how-to?topic=account')
        self.assertIn('at most 3 times an hour', after['text'])

    async def test_an_installations_own_text_is_the_same_for_every_app(self):
        _, plain = await self.get('/v1/how-to?topic=pricing')
        for client in APPS:
            with self.subTest(client=client):
                self.assertEqual(await self.get(f'/v1/how-to?topic=pricing&client={client}'), (200, plain))

    async def test_an_unknown_topic_or_app_is_refused(self):
        for query, says in REFUSED:
            with self.subTest(query=query):
                await self.refused(query, says)

    async def test_it_needs_a_credential(self):
        response = await self.client.get('/v1/how-to')
        self.assertEqual(response.status, 401)


class HostedHowToTests(DaemonCase):
    """The same route with a hosted service's how_to hook."""
    hooks_class = Hosted

    async def test_a_topic_the_hook_leaves_keeps_the_installations_text(self):
        status, body = await self.get('/v1/how-to?topic=rules')
        self.assertEqual((status, body), (200, how_to.guide('rules', self.environ)))

    async def test_the_hook_replaces_a_topic_for_its_owner(self):
        status, body = await self.get('/v1/how-to?topic=account')
        self.assertEqual(status, 200)
        self.assertEqual(body, {'topic': 'account', 'text': 'local is on the Free plan.',
                                'links': [{'name': 'Wallet', 'url': 'https://app.test/wallet'}]})

    async def test_the_hook_extends_a_topic_and_cannot_change_the_guide_it_was_shown(self):
        status, body = await self.get('/v1/how-to')
        own = how_to.guide('overview', self.environ)
        self.assertEqual(status, 200)
        self.assertEqual(body['text'], own['text'] + '\n\nThis is the hosted service.')
        # Its links were a copy: changing them did nothing, and it said nothing about links.
        self.assertEqual(body['links'], own['links'])

    async def test_the_hook_hears_the_app_asking(self):
        for client in APPS:
            self.assertEqual((await self.get(f'/v1/how-to?topic=account&client={client}'))[0], 200)
        await self.get('/v1/how-to?topic=using')
        self.assertEqual(self.hooks.asked,
                         [('local', 'account', client) for client in APPS] + [('local', 'using', None)])

    async def test_an_unknown_topic_or_app_never_reaches_the_hook(self):
        for query, says in REFUSED:
            await self.refused(query, says)
        self.assertEqual(self.hooks.asked, [])

    async def test_a_refusal_from_the_hook_is_answered_as_it_is(self):
        def refuse(owner, topic, guide, client):
            raise CallRefused('service_unavailable', 'Try again in a minute.', status=503, retryAfter=30)
        self.hooks.answer = refuse
        status, body = await self.get('/v1/how-to?topic=account')
        self.assertEqual(status, 503)
        self.assertEqual(body['error'], {'code': 'service_unavailable', 'message': 'Try again in a minute.',
                                         'retryAfter': 30})

    async def test_an_answer_the_core_cannot_read_is_an_error_not_a_guess(self):
        self.hooks.answer = lambda owner, topic, guide, client: {'text': ['not', 'text']}
        status, body = await self.get('/v1/how-to?topic=account')
        self.assertEqual((status, body['error']['code']), (502, 'invalid_hook_answer'))


class HooksWithoutHowToTests(unittest.IsolatedAsyncioTestCase):
    """Hooks written before how_to existed keep the installation's own text."""

    async def test_the_guide_stands(self):
        class Older:
            def settings(self, owner):
                return {'SMITLINE_CALLING_HOURS': 'off'}

        service = CallService(CallStore(Path(tempfile.mkdtemp()) / 'calls'), hooks=Older(), lines={}, environ={})
        self.assertEqual(await service.how_to('local', 'account'), how_to.guide('account', {'SMITLINE_CALLING_HOURS': 'off'}))
        self.assertEqual(await service.how_to('local'), how_to.guide('overview', {}))


if __name__ == '__main__':
    unittest.main()
