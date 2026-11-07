import asyncio
from datetime import datetime, timedelta, timezone
import json
import os
import tempfile
import unittest
from pathlib import Path

from call_brief import BriefIncomplete, CallBrief, available_voices, disclosure_line, opening_line
from call_hooks import CallRefused, DefaultCallHooks, MissingCredentials, load_hooks, read_env_file
from call_notify import WebhookNotifier, load_or_create_secret, signature
from call_result import (
    MEETING_SUMMARY_INSTRUCTIONS, RESULT_SCHEMA, ResponsesSummarizer, SummaryUnavailable,
    fallback_result, meeting_facts, refused_join, result_from_handoff, result_without_conversation,
    transcript_text,
)
from call_store import CallNotFound, CallStore, new_call_id
from voice_core import (
    PCMU8, LiveSession, LiveSessionError, append_event, backend_usage_from, function_call_from,
    session_config,
)


ZOOM = 'https://us05web.zoom.us/j/123456789?pwd=abc'


def phone_brief(**overrides):
    payload = {
        'channel': 'phone',
        'to': '+1 (415) 555-0142',
        'onBehalfOf': 'Robin',
        'objective': 'Book a table for 4 at 7pm',
    }
    payload.update(overrides)
    return payload


class BriefTests(unittest.TestCase):
    def test_phone_brief_normalizes_number_and_defaults(self):
        brief = CallBrief.from_dict(phone_brief(mayAgreeTo=['6:30 to 7:30pm']))
        self.assertEqual(brief.to, '+14155550142')
        self.assertEqual(brief.max_minutes, 10)
        self.assertEqual(brief.to_dict()['mayAgreeTo'], ['6:30 to 7:30pm'])
        self.assertEqual(opening_line(brief), "Hi, I'm calling on behalf of Robin about")
        self.assertEqual(opening_line(brief, 'Sam'), "Hey Sam, I'm calling on behalf of Robin about")
        self.assertEqual(disclosure_line(brief), "I'm Robin's AI assistant")

    def test_missing_fields_come_with_questions(self):
        with self.assertRaises(BriefIncomplete) as caught:
            CallBrief.from_dict({'channel': 'phone', 'to': '+14155550142'})
        missing = caught.exception.to_dict()['missing']
        self.assertEqual([item['field'] for item in missing], ['objective', 'onBehalfOf'])
        self.assertTrue(all(item['question'].endswith('?') or item['question'].endswith('.')
                            for item in missing))

    def test_rejects_bad_values(self):
        cases = [
            phone_brief(to='555-0142'),
            phone_brief(channel='fax'),
            phone_brief(voice='alloy'),
            phone_brief(maxMinutes=0),
            phone_brief(maxMinutes=61),
            phone_brief(notify={'webhookUrl': 'http://example.com/hook'}),
            phone_brief(notify={'webhookUrl': 'https://user:pw@example.com/hook'}),
            phone_brief(notify={'webhookUrl': 'https://10.0.0.5/hook'}),
            phone_brief(notify={'webhookUrl': 'https://169.254.169.254/latest'}),
            phone_brief(notify={'webhookUrl': 'https://[::1]/hook'}),
            phone_brief(agentSession={'provider': 'codex'}),
            phone_brief(unexpected=True),
            phone_brief(language='english please'),
            {'channel': 'meeting', 'to': 'https://example.com/meeting', 'onBehalfOf': 'A',
             'objective': 'x'},
            {'channel': 'meeting', 'to': ZOOM, 'onBehalfOf': 'A', 'objective': 'x',
             'rehearsal': True},
        ]
        for payload in cases:
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                CallBrief.from_dict(payload)

    def test_accepts_localhost_webhooks_and_meetings(self):
        brief = CallBrief.from_dict(phone_brief(notify={'webhookUrl': 'http://127.0.0.1:9000/x'},
                                                voice='quartz'))
        self.assertEqual(brief.webhook_url, 'http://127.0.0.1:9000/x')
        meeting = CallBrief.from_dict({'channel': 'meeting', 'to': ZOOM, 'onBehalfOf': 'Robin',
                                       'objective': 'Help with the Q3 numbers'})
        self.assertEqual(meeting.platform, 'zoom')
        self.assertEqual(meeting.max_minutes, 120)

    def test_voice_list_is_extendable(self):
        self.assertIn('marin', available_voices({}))
        self.assertIn('newvoice', available_voices({'SMITLINE_EXTRA_VOICES': 'newvoice, Bad Name'}))
        self.assertNotIn('Bad Name', available_voices({'SMITLINE_EXTRA_VOICES': 'Bad Name'}))

    def test_rejects_secret_fields(self):
        with self.assertRaises(ValueError):
            CallBrief.from_dict(phone_brief(context='x', notify={'apiKey': 'x'}))


class OnBehalfOfTests(unittest.TestCase):
    """onBehalfOf names a person. An AI, a model, an app or a bot is put right, or refused."""

    def test_ai_names_and_people(self):
        from call_brief import names_an_ai
        for value in ('ChatGPT', 'GPT-4o mini', 'gpt4o', 'Claude Opus 4.5', 'Claude.ai', 'Meta AI',
                      'GitHub Copilot', 'Grok by xAI', 'Perplexity AI', 'Smitline AI', 'ChatGPT (OpenAI)',
                      'chatgpt.com', "OpenAI's ChatGPT", 'Google Assistant', 'Virtual assistant',
                      'the agent', 'your assistant', "Robin's AI", 'A. I.', 'Bot123'):
            with self.subTest(value=value):
                self.assertTrue(names_an_ai(value))
        # Only the whole value counts: a person who shares a word with a product is a person.
        for value in ('Alexa Smith', 'Claude Monet', 'Siri Hustvedt', 'Gemini Ruiz', 'Ai Weiwei',
                      'Jan Bot', 'Mia Agent', 'Claude Max', 'Alexa Smart', "Robin's", "Robin's team",
                      'Smith via ChatGPT', 'Meta', 'Google', 'the user', '李明', 'Sara.Ivy', '', None, 42):
            with self.subTest(value=value):
                self.assertFalse(names_an_ai(value))

    def test_an_ai_becomes_the_owner_or_is_refused(self):
        from call_brief import NOT_A_PERSON, person_on_behalf_of
        self.assertEqual(person_on_behalf_of('ChatGPT', '  Smith   Lee '), 'Smith Lee')
        self.assertEqual(person_on_behalf_of('Alexa Smith', 'Smith Lee'), 'Alexa Smith')
        self.assertEqual(person_on_behalf_of('Alexa Smith', None), 'Alexa Smith')
        # The owner's own name stays, even when a product has it too.
        self.assertEqual(person_on_behalf_of('claude', 'Claude'), 'claude')
        # What is not a usable name is left for the brief's own checks to refuse.
        for value in (None, ['ChatGPT'], 'AI ' * 50):
            with self.subTest(value=value):
                self.assertEqual(person_on_behalf_of(value, 'Smith Lee'), value)
        for owner_name in (None, '', '   '):
            with self.subTest(owner_name=owner_name):
                with self.assertRaises(ValueError) as caught:
                    person_on_behalf_of('Claude', owner_name)
                self.assertEqual(str(caught.exception), NOT_A_PERSON)


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = CallStore(Path(self.temp.name) / 'calls')

    def tearDown(self):
        self.temp.cleanup()

    def record(self, **overrides):
        record = {'id': new_call_id(), 'owner': 'local', 'channel': 'phone', 'status': 'queued',
                  'brief': phone_brief(), 'createdAt': self.store.now()}
        record.update(overrides)
        return record

    def test_create_update_list_and_events(self):
        first = self.store.create(self.record(createdAt='2026-09-28T10:00:00Z'))
        second = self.store.create(self.record(createdAt='2026-09-28T11:00:00Z', owner='other'))
        self.store.update(first['id'], status='ringing')
        self.assertEqual(self.store.get(first['id'])['status'], 'ringing')
        self.assertEqual([r['id'] for r in self.store.list()], [second['id'], first['id']])
        self.assertEqual([r['id'] for r in self.store.list(owner='local')], [first['id']])
        one = self.store.append_event(first['id'], 'call.status', status='ringing')
        two = self.store.append_event(first['id'], 'call.transcript', speaker='agent', text='Hi')
        self.assertEqual((one['id'], two['id']), ('1', '2'))
        self.assertEqual([e['id'] for e in self.store.events(first['id'], after='1')], ['2'])
        mode = os.stat(Path(self.temp.name) / 'calls' / first['id'] / 'call.json').st_mode & 0o777
        self.assertEqual(mode, 0o600)

    def test_sequence_survives_a_new_store(self):
        record = self.store.create(self.record())
        self.store.append_event(record['id'], 'call.created')
        reopened = CallStore(Path(self.temp.name) / 'calls')
        self.assertEqual(reopened.append_event(record['id'], 'call.status')['id'], '2')

    def test_unknown_and_malformed_ids(self):
        for value in ('call-0000000000000000', '../etc', 'call-XYZ'):
            with self.assertRaises(CallNotFound):
                self.store.get(value)

    def test_refuses_secret_fields(self):
        with self.assertRaises(ValueError):
            self.store.create(self.record(authToken='x'))


class NoConversationTests(unittest.TestCase):
    def test_voicemail_without_a_message(self):
        from call_result import result_without_conversation
        result = result_without_conversation('voicemail')
        self.assertEqual((result['outcome'], result['source']), ('voicemail', 'status'))
        self.assertIsNone(result_without_conversation('voicemail', transcript=[
            {'speaker': 'agent', 'text': 'Hi, this is an AI assistant for Robin.'}]))


class NotifierTests(unittest.IsolatedAsyncioTestCase):
    def call(self, url='https://example.com/hook'):
        return {'id': 'call-1', 'status': 'completed', 'brief': {'notify': {'webhookUrl': url}}}

    async def test_signs_and_delivers(self):
        sent = []

        async def post(url, body, headers):
            sent.append((url, body, headers))
            return 204
        result = await WebhookNotifier('s3cret', post=post).deliver(self.call())
        self.assertEqual(result, {'delivered': True, 'attempts': 1})
        url, body, headers = sent[0]
        self.assertEqual(headers['X-Smitline-Signature'], signature('s3cret', body))
        self.assertEqual(json.loads(body)['type'], 'call.completed')

    async def test_retries_server_errors_but_not_client_errors(self):
        statuses = [500, 502, 200]
        slept = []

        async def post(url, body, headers):
            return statuses.pop(0)

        async def sleep(seconds):
            slept.append(seconds)
        notifier = WebhookNotifier('s', post=post, sleep=sleep, delays=(1, 2, 3))
        self.assertEqual((await notifier.deliver(self.call()))['attempts'], 3)
        self.assertEqual(slept, [1, 2])

        async def gone(url, body, headers):
            return 410
        self.assertEqual(await WebhookNotifier('s', post=gone).deliver(self.call()),
                         {'delivered': False, 'attempts': 1, 'error': 'rejected'})

        async def down(url, body, headers):
            raise OSError('connection refused')
        result = await WebhookNotifier('s', post=down, sleep=sleep, delays=()).deliver(self.call())
        self.assertEqual(result, {'delivered': False, 'attempts': 1, 'error': 'unreachable'})

    async def test_private_receivers_are_refused_unless_allowed(self):
        sent = []

        async def post(url, body, headers):
            sent.append(url)
            return 200

        async def private(url):
            return False
        refused = await WebhookNotifier('s', post=post, resolve=private).deliver(self.call())
        self.assertEqual(refused, {'delivered': False, 'attempts': 0, 'error': 'rejected'})
        self.assertEqual(sent, [])
        allowed = await WebhookNotifier('s', post=post, resolve=private,
                                        allow_private=True).deliver(self.call())
        self.assertTrue(allowed['delivered'])

    async def test_hostnames_are_resolved_before_delivery(self):
        from unittest import mock
        from call_notify import resolves_publicly

        def infos(address):
            return mock.AsyncMock(return_value=[(2, 1, 6, '', (address, 443))])
        loop = asyncio.get_running_loop()
        with mock.patch.object(loop, 'getaddrinfo', infos('10.0.0.5')):
            self.assertFalse(await resolves_publicly('https://hooks.example.com/x'))
        with mock.patch.object(loop, 'getaddrinfo', infos('93.184.216.34')):
            self.assertTrue(await resolves_publicly('https://hooks.example.com/x'))
        self.assertTrue(await resolves_publicly('http://localhost:9000/hook'))

    async def test_no_webhook_means_no_delivery(self):
        self.assertIsNone(await WebhookNotifier('s').deliver({'status': 'completed', 'brief': {}}))

    def test_secret_is_created_once_privately(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'webhook.secret'
            first = load_or_create_secret(path)
            self.assertEqual(first, load_or_create_secret(path))
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)


class HookTests(unittest.TestCase):
    def test_env_file_and_process_environment(self):
        with tempfile.TemporaryDirectory() as temp:
            env_file = Path(temp) / '.env'
            env_file.write_text('# comment\nOPENAI_API_KEY="from-file"\nexport TWILIO_ACCOUNT_SID=AC1\n')
            self.assertEqual(read_env_file(env_file)['TWILIO_ACCOUNT_SID'], 'AC1')
            hooks = DefaultCallHooks(environ={}, env_file=env_file)
            self.assertEqual(hooks.credentials('local', 'openai'), {'apiKey': 'from-file'})
            with self.assertRaises(MissingCredentials) as caught:
                hooks.credentials('local', 'twilio')
            self.assertEqual(caught.exception.missing,
                         ('TWILIO_AUTH_TOKEN', 'TWILIO_FROM_NUMBER or SMITLINE_CALLER_ID'))
            override = DefaultCallHooks(environ={'OPENAI_API_KEY': 'from-env'}, env_file=env_file)
            self.assertEqual(override.credentials('local', 'openai')['apiKey'], 'from-env')

    def test_calling_code_allow_list(self):
        hooks = DefaultCallHooks(environ={'SMITLINE_ALLOWED_CALLING_CODES': '1, +44',
                                          'SMITLINE_CALLING_HOURS': 'off'})
        hooks.precheck('local', CallBrief.from_dict(phone_brief()))
        hooks.precheck('local', CallBrief.from_dict(phone_brief(to='+442079460123')))
        with self.assertRaises(CallRefused):
            hooks.precheck('local', CallBrief.from_dict(phone_brief(to='+919876543210')))
        DefaultCallHooks(environ={'SMITLINE_CALLING_HOURS': 'off'}).precheck(
            'local', CallBrief.from_dict(phone_brief(to='+919876543210')))

    def test_load_hooks(self):
        self.assertIsInstance(load_hooks(None, environ={}), DefaultCallHooks)
        hooks = load_hooks('call_hooks:DefaultCallHooks', environ={})
        self.assertIsInstance(hooks, DefaultCallHooks)
        with self.assertRaises(ValueError):
            load_hooks('call_hooks')


class ResultTests(unittest.IsolatedAsyncioTestCase):
    transcript = [{'speaker': 'agent', 'text': "Hi, I'm an AI assistant calling for Robin."},
                  {'speaker': 'other', 'text': 'Sure, 7pm works. Confirmation LG-2291.'}]

    def test_results_without_a_conversation(self):
        self.assertEqual(result_without_conversation('no_answer')['outcome'], 'not_reached')
        self.assertEqual(result_without_conversation('busy')['summary'], 'The line was busy.')
        self.assertEqual(result_without_conversation('canceled')['outcome'], 'canceled')
        self.assertEqual(result_without_conversation('hangup')['outcome'], 'not_reached')
        self.assertIsNone(result_without_conversation('hangup', transcript=self.transcript))

    def test_meeting_handoff_mapping(self):
        result = result_from_handoff({
            'summary': 'Agreed on the Q3 correction.',
            'decisions': [{'text': 'Use $800,000'}],
            'actionItems': [{'text': 'Send the deck', 'owner': 'Casey'}],
            'unresolvedQuestions': ['Who presents?'],
            'partial': True,
        }, duration_seconds=600)
        self.assertEqual(result['outcome'], 'partial')
        self.assertEqual(result['decisions'], ['Use $800,000'])
        self.assertEqual(result['actionItems'], ['Casey: Send the deck'])
        self.assertEqual(result['openQuestions'], ['Who presents?'])
        self.assertEqual(result['durationSeconds'], 600)
        # Nothing judged the objective, so a handoff alone is never "achieved".
        self.assertEqual(result_from_handoff({'summary': 'Agreed.', 'decisions': ['Ship Friday']})['outcome'], 'partial')
        self.assertEqual(result_from_handoff({'summary': 'Meeting ended (finished) with 0 transcript entries.'})['outcome'],
                         'not_reached')
        # A plain handoff says nothing of why or what next.
        self.assertFalse({'reason', 'hint'} & set(result))

    def test_a_handoff_that_says_why_gives_the_result_a_reason_and_a_hint(self):
        result = result_from_handoff({'summary': 'Zoom asked Smitline to sign in.',
                                      'reason': ' Zoom asked Smitline\nto sign in. ',
                                      'hint': 'Ask the user for the dial-in.', 'endReason': 'x'})
        self.assertEqual((result['outcome'], result['reason'], result['hint']),
                         ('not_reached', 'Zoom asked Smitline to sign in.', 'Ask the user for the dial-in.'))
        self.assertNotIn('hint', result_from_handoff({'summary': 'x', 'reason': 'y', 'hint': '  '}))

    def test_a_refused_join_says_why_and_how_to_get_in_by_phone(self):
        # No dial-in in the brief: the platform named, and the invitation's phone details asked for.
        meet = refused_join('meet', 'guest_refused')
        self.assertEqual(meet['reason'], 'Google Meet did not let Smitline in as a guest.')
        self.assertEqual(meet['summary'], meet['reason'])
        self.assertEqual(meet['hint'], 'Ask the user for the Join by phone number and PIN from the '
                         'invitation, if it has them, then start again with them as dialIn and '
                         'without the link (the invitation text works), so Smitline joins by phone.')
        for platform, kind, said, details in (
                ('teams', 'restricted', 'Microsoft Teams lets only invited people', 'phone conference ID'),
                ('zoom', 'sign_in_required', 'Zoom asked Smitline to sign in', 'the meeting ID and the passcode'),
                ('meet', None, 'Google Meet did not let Smitline join from the link', 'PIN'),
                ('meet', 'something_new', 'Google Meet did not let Smitline join from the link', 'PIN')):
            with self.subTest(platform=platform, kind=kind):
                refused = refused_join(platform, kind)
                self.assertTrue(refused['reason'].startswith(said), refused['reason'])
                self.assertIn(details, refused['hint'])
                self.assertIn('dialIn', refused['hint'])
        # A platform with no phone numbers gets the reason alone.
        self.assertIsNone(refused_join(None, 'guest_refused')['hint'])
        self.assertEqual(refused_join(None)['reason'], 'The meeting platform did not let Smitline '
                         'join from the link.')

    def test_a_refused_join_that_could_not_dial_in_says_why_not(self):
        hours = CallRefused('outside_calling_hours', 'It can dial in at 8:00 AM there.')
        skipped = refused_join('meet', 'guest_refused', dial_in=True, skipped=hours)
        self.assertEqual(skipped['summary'], 'Google Meet did not let Smitline in as a guest. '
                         'Smitline did not join by phone instead: It can dial in at 8:00 AM there.')
        self.assertIn('did not join by phone instead', skipped['reason'])
        self.assertEqual(skipped['hint'], 'It can dial in at 8:00 AM there.')
        # This installation cannot dial at all.
        unset = CallRefused('dial_in_unavailable', 'Joining a meeting by phone needs phone calls set up.')
        cannot = refused_join('meet', 'guest_refused', dial_in=True, phone_problem=unset)
        self.assertIn('this installation cannot dial in', cannot['reason'])
        self.assertEqual(cannot['hint'], unset.message)
        # Without the dial-in, the hint asks for it and says what joining by phone still needs.
        ask = refused_join('meet', 'guest_refused', phone_problem=unset)
        self.assertTrue(ask['hint'].startswith('Ask the user for the Join by phone number'))
        self.assertTrue(ask['hint'].endswith(unset.message))

    def test_meeting_facts(self):
        self.assertEqual(meeting_facts(), [])
        self.assertEqual(meeting_facts(participants={'count': 0, 'names': []}, ended='meeting_ended'), [])
        facts = meeting_facts(room_created=True, participants={'count': 2, 'names': ['Ana', 'Ben']},
                              ended='left_asked_to_leave')
        self.assertEqual(facts, [
            'The assistant set up this meeting room itself, for this meeting.',
            'Besides the assistant, up to 2 people were in the meeting at once; the meeting showed '
            'these names: Ana, Ben.',
            'The assistant left when someone in the meeting asked it to.'])
        self.assertEqual(meeting_facts(participants={'count': 1}),
                         ['Besides the assistant, up to 1 person was in the meeting at once.'])
        self.assertEqual(meeting_facts(participants={'names': ['Ana']}, by_phone=True), [
            'The assistant joined by phone, so it could not see who was in the meeting.',
            'Besides the assistant, the meeting showed these people: Ana.'])
        many = meeting_facts(participants={'count': 20, 'names': [f'P{n}' for n in range(20)]})
        self.assertTrue(many[0].endswith('P11, and 8 more.'), many[0])
        for ended in ('alone_in_meeting', 'idle', 'max_duration', 'canceled', 'left_completed'):
            self.assertEqual(len(meeting_facts(ended=ended)), 1, ended)
        self.assertEqual(meeting_facts(participants={'count': True, 'names': 'Ana'}), [])

    async def test_summarizer_request_and_parse(self):
        captured = {}

        async def post(payload):
            captured.update(payload)
            return {'output': [{'type': 'message', 'content': [{'type': 'output_text', 'text': json.dumps({
                'outcome': 'achieved', 'summary': 'Booked for 7pm.',
                'details': [{'label': 'Confirmation', 'value': 'LG-2291'}, {'label': '', 'value': 'x'}],
                'decisions': ['7pm'], 'actionItems': [], 'openQuestions': [], 'doNotCall': False,
            })}]}], 'usage': {'input_tokens': 900, 'output_tokens': 80}}
        summarizer = ResponsesSummarizer('sk-test', post=post)
        result = await summarizer.summarize({'objective': 'Book', 'onBehalfOf': 'Robin'},
                                            self.transcript, duration_seconds=65)
        self.assertEqual(captured['text']['format']['schema'], RESULT_SCHEMA)
        self.assertTrue(captured['text']['format']['strict'])
        self.assertFalse(captured['store'])
        self.assertIn('Other party: Sure, 7pm works.', captured['input'])
        self.assertEqual(result['details'], [{'label': 'Confirmation', 'value': 'LG-2291'}])
        self.assertEqual(result['summaryTokens'], {'input': 900, 'cached': 0, 'output': 80})
        self.assertEqual(result['summaryModel'], summarizer.model)
        self.assertEqual(result['transcript'][1]['speaker'], 'other')
        self.assertNotIn('doNotCall', result)
        self.assertIn('doNotCall', RESULT_SCHEMA['required'])

    async def test_a_meeting_is_summarized_against_its_objective(self):
        captured = {}

        async def post(payload):
            captured.update(payload)
            return {'output': [{'type': 'message', 'content': [{'type': 'output_text', 'text': json.dumps({
                'outcome': 'achieved', 'summary': 'The team agreed to two PRs.', 'details': [],
                'decisions': ['Split the work into two PRs'], 'actionItems': ['Note the decision in the PR'],
                'openQuestions': [], 'doNotCall': True,
            })}]}], 'usage': {}}
        transcript = [{'speaker': 'meeting', 'text': 'Two PRs works for me.'},
                      {'speaker': 'agent', 'text': 'I will note that in the PR.'}]
        result = await ResponsesSummarizer('sk-test', post=post).summarize(
            {'channel': 'meeting', 'objective': 'Agree how to split the work', 'onBehalfOf': 'Robin'},
            transcript, duration_seconds=268)
        self.assertIn('video meeting', captured['instructions'])
        self.assertIn('never list a question that was answered', captured['instructions'])
        self.assertIn('Participants: Two PRs works for me.', captured['input'])
        self.assertIn('Attending on behalf of: Robin', captured['input'])
        self.assertEqual(result['decisions'], ['Split the work into two PRs'])
        self.assertEqual(result['source'], 'summary_model')
        self.assertEqual(len(result['transcript']), 2)
        # Nobody can ask a meeting not to call again.
        self.assertNotIn('doNotCall', result)
        # Without facts, the input has no facts section.
        self.assertNotIn('Facts from Smitline', captured['input'])

    async def test_a_meeting_summary_is_told_what_the_transcript_cannot_show(self):
        captured = {}

        async def post(payload):
            captured.update(payload)
            return {'output': [{'type': 'message', 'content': [{'type': 'output_text', 'text': json.dumps({
                'outcome': 'failed', 'summary': 'Two people joined; none of them was heard.',
                'details': [], 'decisions': [], 'actionItems': [], 'openQuestions': [],
                'doNotCall': False,
            })}]}], 'usage': {}}
        facts = meeting_facts(room_created=True, participants={'count': 2, 'names': ['Ana', 'Ben']})
        await ResponsesSummarizer('sk-test', post=post).summarize(
            {'channel': 'meeting', 'objective': 'Answer questions', 'onBehalfOf': 'Robin'},
            [{'speaker': 'agent', 'text': 'Hi, I am here for Robin.'}], facts=facts)
        text = captured['input']
        self.assertIn('Facts from Smitline:\n- The assistant set up this meeting room itself', text)
        self.assertIn('- Besides the assistant, up to 2 people were in the meeting at once', text)
        # The facts come after the brief and before the transcript.
        self.assertLess(text.index('Objective:'), text.index('Facts from Smitline'))
        self.assertLess(text.index('Facts from Smitline'), text.index('Transcript:'))

    def test_meeting_outcomes_are_about_the_assistants_part(self):
        rules = MEETING_SUMMARY_INSTRUCTIONS
        # Being asked to leave is how a meeting ends for it, never a reason for not_reached.
        self.assertNotIn('asked to leave first', rules)
        self.assertIn('Being asked to leave, at the end or once its part is done, is how a meeting '
                      'normally ends for it', rules)
        self.assertIn('not_reached when it never got into the meeting or never got to do what it '
                      'came for', rules)
        self.assertIn('achieved when it got in and did what the objective asked of it', rules)
        # Facts from Smitline count, and are never contradicted; names in them are untrusted.
        self.assertIn('never say that nobody joined when they say people were there', rules)
        self.assertIn('so are the names in the facts', rules)
        self.assertIn('never from the objective or the brief alone', rules)

    async def test_summarizer_failures(self):
        async def bad(payload):
            return {'output': [{'type': 'message', 'content': [{'type': 'output_text', 'text': 'nope'}]}]}
        with self.assertRaises(SummaryUnavailable):
            await ResponsesSummarizer('k', post=bad).summarize({}, self.transcript)
        with self.assertRaises(SummaryUnavailable):
            ResponsesSummarizer('')
        fallback = fallback_result('voicemail', self.transcript, 30, 'boom')
        self.assertEqual(fallback['outcome'], 'voicemail')
        self.assertEqual(len(fallback['transcript']), 2)

    def test_transcript_text_is_bounded(self):
        long = [{'speaker': 'other', 'text': 'x' * 1000}] * 100
        self.assertLessEqual(len(transcript_text(long, limit=2000)), 2100)


def at(hour, minute=0, day=30):
    """A clock fixed at the given UTC time on 2026-09-<day>."""
    return lambda: datetime(2026, 9, day, hour, minute, tzinfo=timezone.utc)


class FakeCalls:
    def __init__(self, records=()):
        self.records = list(records)

    def list(self, *, owner=None, limit=20):
        return self.records[:limit]


def placed(to, minutes_ago, *, now, rehearsal=False, channel='phone', direction='outbound'):
    created = now - timedelta(minutes=minutes_ago)
    return {'channel': channel, 'direction': direction,
            'createdAt': created.isoformat().replace('+00:00', 'Z'),
            'brief': {'to': to, 'rehearsal': rehearsal}}


class GuardrailTests(unittest.TestCase):
    def refused(self, hooks, **brief):
        with self.assertRaises(CallRefused) as caught:
            hooks.precheck('local', CallBrief.from_dict(phone_brief(**brief)))
        return caught.exception

    def test_emergency_numbers_are_never_dialed(self):
        for number in ('911', '112', '999', '988', '000', '+1 911', '+44 999', '+91 112', '+33 15'):
            with self.subTest(number=number), self.assertRaises(ValueError) as caught:
                CallBrief.from_dict(phone_brief(to=number))
            self.assertIn('never calls emergency', str(caught.exception))
        # Ordinary numbers that merely contain those digits are fine.
        CallBrief.from_dict(phone_brief(to='+1 415 911 0142'))
        with self.assertRaises(ValueError) as caught:
            CallBrief.from_dict(phone_brief(to='5550'))
        self.assertIn('E.164', str(caught.exception))

    def test_premium_and_satellite_numbers_are_refused(self):
        hooks = DefaultCallHooks(environ={'SMITLINE_CALLING_HOURS': 'off'})
        self.assertEqual(self.refused(hooks, to='+1 900 555 0100').code, 'high_cost_number')
        self.assertIn('premium-rate', self.refused(hooks, to='+44 871 234 5678').message)
        self.assertIn('satellite', self.refused(hooks, to='+882 123 456 789').message)
        hooks.precheck('local', CallBrief.from_dict(phone_brief(to='+1 800 555 0100')))
        allowed = DefaultCallHooks(environ={'SMITLINE_CALLING_HOURS': 'off',
                                            'SMITLINE_ALLOW_PREMIUM_NUMBERS': '1'})
        allowed.precheck('local', CallBrief.from_dict(phone_brief(to='+1 900 555 0100')))

    def test_calls_happen_in_the_recipients_daytime(self):
        # 06:00 UTC is 11 PM the evening before in San Francisco.
        night = DefaultCallHooks(environ={}, clock=at(6))
        refused = self.refused(night)
        self.assertEqual(refused.code, 'outside_calling_hours')
        self.assertIn('11:00 PM for +14155550142 (America/Los_Angeles)', refused.message)
        self.assertIn('afterHours', refused.message)
        # The same moment is 7 AM in London (still too early) and 11:30 AM in India.
        self.assertIn('7:00 AM', self.refused(night, to='+442079460123').message)
        night.precheck('local', CallBrief.from_dict(phone_brief(to='+919876543210')))
        # The user confirmed the person expects the call.
        night.precheck('local', CallBrief.from_dict(phone_brief(afterHours=True)))
        # The owner's own phone may ring at any hour.
        own = DefaultCallHooks(environ={'SMITLINE_OWNER_PHONE': '+14155550142'}, clock=at(6))
        own.precheck('local', CallBrief.from_dict(phone_brief()))
        for hours in ('off', '07:00-23:30'):
            DefaultCallHooks(environ={'SMITLINE_CALLING_HOURS': hours}, clock=at(6)).precheck(
                'local', CallBrief.from_dict(phone_brief()))
        for hours in ('late', '21:00-08:00', '08:00-25:00'):
            self.assertEqual(self.refused(DefaultCallHooks(
                environ={'SMITLINE_CALLING_HOURS': hours}, clock=at(18))).code, 'invalid_calling_hours')
        with self.assertRaises(ValueError):
            CallBrief.from_dict(phone_brief(channel='meeting', to=ZOOM, afterHours=True))
        self.assertTrue(CallBrief.from_dict(phone_brief(afterHours=True)).to_dict()['afterHours'])
        self.assertNotIn('afterHours', CallBrief.from_dict(phone_brief()).to_dict())

    def test_numbers_spanning_time_zones_ring_while_any_is_in_daytime(self):
        # A Russian mobile may ring from Kaliningrad to Kamchatka. At 22:00 UTC it is 1 AM in
        # Moscow but 8 AM in Vladivostok; at 19:00 UTC it is night in every one of its zones.
        DefaultCallHooks(environ={}, clock=at(22)).precheck(
            'local', CallBrief.from_dict(phone_brief(to='+79161234567')))
        late = self.refused(DefaultCallHooks(environ={}, clock=at(19)), to='+79161234567')
        self.assertIn('every time zone', late.message)

    def test_repeat_calls_are_limited(self):
        now = at(18)()
        number = '+14155550142'
        calls = FakeCalls([placed_at(number, 60 * hours, now) for hours in (1, 3, 5, 7, 9)])
        hooks = DefaultCallHooks(environ={}, store=calls, clock=at(18))
        refused = self.refused(hooks)
        self.assertEqual(refused.code, 'too_many_calls_to_number')
        self.assertIn('called +14155550142 5 times', refused.message)
        self.assertIn('again in about 15 hours', refused.message)
        hooks.precheck('local', CallBrief.from_dict(phone_brief(to='+14155550199')))
        # Rehearsals, incoming calls, and older calls do not count; the owner has no limit.
        calls.records[0]['brief']['rehearsal'] = True
        calls.records[1]['direction'] = 'inbound'
        calls.records.append(placed_at(number, 60 * 25, now))
        hooks.precheck('local', CallBrief.from_dict(phone_brief()))
        calls.records[0]['brief']['rehearsal'] = False
        calls.records[1]['direction'] = 'outbound'
        DefaultCallHooks(environ={'SMITLINE_OWNER_PHONE': number}, store=calls,
                         clock=at(18)).precheck('local', CallBrief.from_dict(phone_brief()))
        DefaultCallHooks(environ={'SMITLINE_MAX_CALLS_PER_NUMBER': '0'}, store=calls,
                         clock=at(18)).precheck('local', CallBrief.from_dict(phone_brief()))
        self.assertEqual(self.refused(DefaultCallHooks(
            environ={'SMITLINE_MAX_CALLS_PER_NUMBER': 'many'}, store=calls,
            clock=at(18))).code, 'invalid_call_limit')

        busy = FakeCalls([placed_at(f'+1415555{index:04d}', index + 1, now) for index in range(20)])
        refused = self.refused(DefaultCallHooks(environ={}, store=busy, clock=at(18)))
        self.assertEqual(refused.code, 'too_many_calls')
        self.assertIn('placed 20 calls in the last hour', refused.message)
        DefaultCallHooks(environ={'SMITLINE_MAX_CALLS_PER_HOUR': '25'}, store=busy,
                         clock=at(18)).precheck('local', CallBrief.from_dict(phone_brief()))

    def test_calls_that_never_reached_the_phone_do_not_count(self):
        now = at(18)()
        number = '+14155550142'
        limits = {'SMITLINE_MAX_CALLS_PER_NUMBER': '1', 'SMITLINE_MAX_CALLS_PER_HOUR': '1'}
        # Refused when it started (no credit), failed before the provider placed it, canceled
        # before it was dialed: nobody's phone rang.
        never = [dict(placed_at(number, 10, now), status='failed', endReason='refused', line={}),
                 dict(placed_at(number, 20, now), status='failed', endReason='error', line={}),
                 dict(placed_at(number, 30, now), status='canceled', endReason='canceled', line={})]
        DefaultCallHooks(environ=dict(limits), store=FakeCalls(never), clock=at(18)).precheck(
            'local', CallBrief.from_dict(phone_brief()))
        # A call the provider placed counts, however it ended, and so does one still being placed.
        for counted in (dict(placed_at(number, 40, now), status='failed', endReason='error',
                             line={'providerCallSid': 'CA1'}),
                        dict(placed_at(number, 40, now), status='completed', line={'liveSessionId': 'rtc_1'}),
                        dict(placed_at(number, 1, now), status='queued', line={})):
            with self.subTest(record=counted):
                refused = self.refused(DefaultCallHooks(environ=dict(limits), store=FakeCalls(never + [counted]),
                                                        clock=at(18)))
                self.assertEqual(refused.code, 'too_many_calls_to_number')
                self.assertIn('called +14155550142 1 times', refused.message)

    def test_record_is_a_phone_only_flag(self):
        self.assertTrue(CallBrief.from_dict(phone_brief(record=True)).to_dict()['record'])
        self.assertNotIn('record', CallBrief.from_dict(phone_brief()).to_dict())
        for bad in ({'record': 'yes'}, {'channel': 'meeting', 'to': ZOOM, 'record': True}):
            with self.subTest(brief=bad), self.assertRaises(ValueError):
                CallBrief.from_dict(phone_brief(**bad))

    def test_do_not_call_list(self):
        with tempfile.TemporaryDirectory() as temp:
            hooks = DefaultCallHooks(environ={}, env_file=Path(temp) / '.env', clock=at(18))
            listed = hooks.update_do_not_call('local', {'add': [
                '+1 (415) 555-0142', {'number': '+14155550199', 'reason': 'Asked by email'}]})
            self.assertEqual([e['number'] for e in listed['numbers']], ['+14155550142', '+14155550199'])
            self.assertEqual(listed['numbers'][0]['addedAt'], '2026-09-30T18:00:00Z')
            path = Path(temp) / '.smitline' / 'do-not-call.json'
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            refused = self.refused(hooks)
            self.assertEqual(refused.code, 'do_not_call')
            self.assertIn('since 2026-09-30', refused.message)
            # Adding twice keeps one entry; removing lets calls through again.
            hooks.update_do_not_call('local', {'add': ['+14155550142']})
            self.assertEqual(len(hooks.do_not_call('local')['numbers']), 2)
            hooks.update_do_not_call('local', {'remove': ['+14155550142']})
            hooks.precheck('local', CallBrief.from_dict(phone_brief()))
            for bad in ({}, {'add': '+14155550142'}, {'drop': []}, {'add': [{'number': '1'}]},
                        {'add': [{'number': '+14155550142', 'note': 'x'}]}):
                with self.subTest(update=bad), self.assertRaises(ValueError):
                    hooks.update_do_not_call('local', bad)
            # A damaged list refuses calls instead of forgetting who asked.
            path.write_text('{not json')
            self.assertEqual(self.refused(hooks).code, 'do_not_call_unreadable')


def placed_at(to, minutes_ago, now):
    return placed(to, minutes_ago, now=now)


class FakeMessage:
    def __init__(self, data):
        from aiohttp import WSMsgType
        self.type = WSMsgType.TEXT
        self.data = json.dumps(data)


class FakeWs:
    def __init__(self, incoming):
        self.sent = []
        self.incoming = incoming
        self.closed = False

    async def send_json(self, payload):
        self.sent.append(payload)

    async def close(self):
        self.closed = True

    def __aiter__(self):
        async def generator():
            for item in self.incoming:
                yield FakeMessage(item)
        return generator()


class VoiceCoreTests(unittest.IsolatedAsyncioTestCase):
    def test_config_and_appends(self):
        config = session_config(instructions='Be brief.', audio_format=PCMU8, voice='quartz',
                                delegation={'type': 'responses', 'responses': {'model': 'm'}})
        self.assertEqual(config['audio'], {'format': {'type': 'audio/pcmu', 'rate': 8000},
                                           'output': {'voice': 'quartz'}})
        self.assertFalse(config['store'])
        event = append_event('session.commentary.append', 'Say hello', None)
        self.assertIsNone(event['delegation_id'])
        self.assertIsNone(append_event('session.thinking.append', '   '))
        with self.assertRaises(ValueError):
            append_event('session.update', 'x')

    async def test_session_lifecycle(self):
        ws = FakeWs([
            {'type': 'session.started', 'session': {'id': 'sess_1'}},
            {'type': 'session.output_audio.delta', 'delta': 'AAAA'},
            {'type': 'session.usage.updated', 'usage': {'seconds': 12}},
            {'type': 'session.closed', 'reason': 'close_requested', 'usage': {'seconds': 30}},
            {'type': 'session.output_audio.delta', 'delta': 'never'},
        ])

        async def connect(url, key):
            self.assertEqual(key, 'sk-test')
            return ws, None
        seen = []
        async with LiveSession('sk-test', {'model': 'gpt-live-1'}, connect=connect) as live:
            self.assertEqual(ws.sent[0]['type'], 'session.start')
            self.assertFalse(await live.send_audio(b'\x00\x01'))
            async for event in live.events():
                seen.append(event['type'])
                if event['type'] == 'session.output_audio.delta':
                    self.assertTrue(await live.send_audio('AAAA'))
        self.assertEqual(seen, ['session.started', 'session.output_audio.delta',
                                'session.usage.updated', 'session.closed'])
        self.assertEqual((live.session_id, live.usage_seconds, live.close_reason),
                         ('sess_1', 30, 'close_requested'))
        self.assertEqual(ws.sent[1], {'type': 'session.input_audio.append', 'audio': 'AAAA'})
        self.assertTrue(ws.closed)

    async def test_startup_error_raises(self):
        ws = FakeWs([{'type': 'error', 'error': {'code': 'model_not_found'}}])

        async def connect(url, key):
            return ws, None
        with self.assertRaises(LiveSessionError) as caught:
            async with LiveSession('k', {}, connect=connect) as live:
                async for _event in live.events():
                    pass
        self.assertEqual(caught.exception.code, 'model_not_found')

    async def test_function_calls_and_usage(self):
        event = {'type': 'response.event', 'delegation_id': 'd1', 'event': {
            'type': 'response.output_item.done',
            'item': {'type': 'function_call', 'call_id': 'c1', 'name': 'end_call',
                     'arguments': '{"reason": "done"}'}}}
        self.assertEqual(function_call_from(event), {
            'delegation_id': 'd1', 'call_id': 'c1', 'name': 'end_call',
            'arguments': {'reason': 'done'}})
        self.assertIsNone(function_call_from({'type': 'session.started'}))
        usage = backend_usage_from({'type': 'response.event', 'event': {
            'type': 'response.completed', 'response': {'usage': {'input_tokens': 5, 'output_tokens': 2}}}})
        self.assertEqual(usage, {'input': 5, 'cached': 0, 'output': 2, 'webSearches': 0})

        ws = FakeWs([])

        async def connect(url, key):
            return ws, None
        async with LiveSession('k', {}, connect=connect) as live:
            await live.submit_function_output('c1', {'ok': True})
        self.assertEqual(ws.sent[1]['item'], {'type': 'function_call_output', 'call_id': 'c1',
                                              'output': '{"ok": true}'})
        self.assertEqual(ws.sent[2]['type'], 'response.create')
        # GPT-Live refuses a delegation_id on either event.
        self.assertNotIn('delegation_id', ws.sent[1])
        self.assertNotIn('delegation_id', ws.sent[2])


if __name__ == '__main__':
    unittest.main()


class MeetingPayloadTests(unittest.TestCase):
    def test_meeting_carries_the_name_and_voice(self):
        from meeting_line import meeting_payload
        from runtime_state import environ_from_state
        brief = CallBrief.from_dict({
            "channel": "meeting", "to": "https://zoom.us/j/1234567890", "onBehalfOf": "Sam",
            "objective": "Take notes", "voice": "cinder",
        })
        payload = meeting_payload(brief)
        # maxMinutes: the core's default for a meeting (120) when the brief names none.
        self.assertEqual(set(payload), {"meetingUrl", "context", "onBehalfOf", "voice", "maxMinutes"})
        self.assertEqual(payload["maxMinutes"], 120)
        self.assertEqual(payload["onBehalfOf"], "Sam")
        self.assertEqual(payload["voice"], "cinder")
        self.assertEqual(environ_from_state({"voice": "cinder"})["SMITLINE_VOICE"], "cinder")
        plain = meeting_payload(CallBrief.from_dict({
            "channel": "meeting", "to": "https://zoom.us/j/1234567890", "onBehalfOf": "Sam",
            "objective": "Take notes"}))
        self.assertEqual(set(plain), {"meetingUrl", "context", "onBehalfOf", "maxMinutes"})
        avatar = 'data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciLz4='
        shown = CallBrief.from_dict({
            "channel": "meeting", "to": "https://zoom.us/j/1234567890", "onBehalfOf": "Sam",
            "objective": "Take notes", "camera": {"enabled": True, "defaultOn": False, "avatarDataUri": avatar}})
        camera = meeting_payload(shown)["camera"]
        self.assertEqual((camera["enabled"], camera["defaultOn"]), (True, False))
        # The meeting daemon checks the camera again; the checked avatar must still pass.
        from visual_presence import parse_camera_settings
        self.assertEqual(parse_camera_settings(camera), camera)
        # The stored brief keeps the camera choice but not the image.
        self.assertEqual(shown.to_dict()["camera"], {"enabled": True, "defaultOn": False})
        for camera, channel in (({"avatarPath": "/etc/passwd"}, "meeting"), ({"enabled": "yes"}, "meeting"),
                                ({"enabled": False}, "phone")):
            with self.subTest(camera=camera, channel=channel), self.assertRaises(ValueError):
                CallBrief.from_dict({
                    "channel": channel, "to": "https://zoom.us/j/1234567890" if channel == "meeting" else "+14155550142",
                    "onBehalfOf": "Sam", "objective": "Take notes", "camera": camera})
        with self.assertRaises(ValueError):
            CallBrief.from_dict({
                "channel": "meeting", "to": "https://zoom.us/j/1234567890", "onBehalfOf": "Sam",
                "objective": "Take notes",
                "agentSession": {"provider": "codex", "sessionId": "thread-1", "workspace": "/w"}})


class OutcomeRulesTests(unittest.TestCase):
    def test_the_summary_rules_keep_failed_for_technical_problems(self):
        from call_result import SUMMARY_INSTRUCTIONS
        self.assertIn('never because the other person hung up', SUMMARY_INSTRUCTIONS)
        self.assertIn('answered but was busy or ended the call', SUMMARY_INSTRUCTIONS)
        self.assertIn('with no message left is not_reached, not voicemail', SUMMARY_INSTRUCTIONS)
        self.assertIn('Never say a message was left', SUMMARY_INSTRUCTIONS)
