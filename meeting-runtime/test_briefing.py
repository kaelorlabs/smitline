import json
import stat
import tempfile
import unittest
from pathlib import Path

from aiohttp.test_utils import TestClient, TestServer

from briefing import (
    VOICE_NOTES_PREFACE, backend_background, contact_for, load_profile, merge_profile,
    parse_session_context, save_profile, validate_profile, voice_input, voice_notes,
)
from call_brief import CallBrief
from call_hooks import DefaultCallHooks
from call_service import CallService
from call_store import CallStore
from runtime_daemon import create_app


PROFILE = {
    'about': 'Robin runs a small design studio.',
    'style': 'Friendly and direct.',
    'boundaries': ['Never discuss money.'],
    'people': [{'name': 'Sam', 'relationship': 'close friend', 'phone': '+1 (415) 555-0143',
                'notes': 'Works in product; likes blunt feedback.'}],
}
SESSION = {
    'summary': 'Smitline lets any agent make phone calls and join meetings.',
    'facts': ['It is open source under Apache-2.0.', 'The first live calls happened today.'],
    'decisions': ['Direct SIP is next.'],
    'openQuestions': ['Launch now or wait?'],
    'details': 'Pricing: SignalWire about $0.008 a minute plus GPT-Live $0.05 a minute.',
}


class ProfileTests(unittest.TestCase):
    def test_profiles_are_validated_and_normalized(self):
        profile = validate_profile(PROFILE)
        self.assertEqual(profile['version'], 1)
        self.assertEqual(profile['people'][0]['phone'], '+14155550143')
        for bad, message in (
            ({'nickname': 'x'}, 'unknown profile fields'),
            ({'about': 'x' * 2001}, 'at most 2000'),
            ({'people': [{'relationship': 'friend'}]}, 'name is required'),
            ({'people': [{'name': 'A'}, {'name': 'a'}]}, 'unique'),
            ({'people': [{'name': 'A', 'phone': '555'}]}, 'E.164'),
            ({'people': [{'name': 'A', 'password': 'x'}]}, 'Refused'),
        ):
            with self.subTest(bad=bad), self.assertRaisesRegex(ValueError, message):
                validate_profile(bad)

    def test_updates_merge_people_by_name(self):
        merged = merge_profile(validate_profile(PROFILE), {
            'style': 'Warm.',
            'people': [{'name': 'sam', 'notes': 'Prefers texts.'}, {'name': 'Maya', 'relationship': 'colleague'}],
        })
        self.assertEqual(merged['style'], 'Warm.')
        sam = contact_for(merged, '+14155550143')
        self.assertEqual((sam['relationship'], sam['notes']), ('close friend', 'Prefers texts.'))
        self.assertEqual([p['name'] for p in merged['people']], ['Sam', 'Maya'])
        removed = merge_profile(merged, {'removePeople': ['MAYA']})
        self.assertEqual([p['name'] for p in removed['people']], ['Sam'])
        with self.assertRaisesRegex(ValueError, 'unknown profile fields'):
            merge_profile(merged, {'version': 2})

    def test_profiles_are_saved_privately(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / '.colleague' / 'profile.json'
            self.assertEqual(load_profile(path), {'version': 1})
            save_profile(path, PROFILE)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(load_profile(path)['about'], PROFILE['about'])
            path.write_text('{not json')
            self.assertEqual(load_profile(path), {'version': 1})


class SessionContextTests(unittest.TestCase):
    def test_text_or_structured_context(self):
        self.assertEqual(parse_session_context('  Some background. '), {'summary': 'Some background.'})
        self.assertEqual(parse_session_context(None), {})
        self.assertEqual(parse_session_context(SESSION)['facts'][1], 'The first live calls happened today.')
        for bad, message in (({'notes': 'x'}, 'unknown context fields'),
                             ({'details': 'x' * 24001}, 'at most 24000'),
                             ({'facts': 'one'}, 'list of text'),
                             ({'token': 'x'}, 'Refused')):
            with self.subTest(bad=bad), self.assertRaisesRegex(ValueError, message):
                parse_session_context(bad)

    def test_briefs_accept_the_new_fields_and_round_trip(self):
        brief = CallBrief.from_dict({
            'channel': 'phone', 'to': '+14155550143', 'onBehalfOf': 'Robin', 'objective': 'Get feedback',
            'context': SESSION, 'questions': ['Launch now or wait?'], 'tone': 'casual',
            'contact': {'name': 'Sam', 'relationship': 'friend'},
        })
        self.assertEqual(brief.session_context['details'], SESSION['details'])
        again = CallBrief.from_dict(brief.to_dict())
        self.assertEqual((again.questions, again.tone, again.contact),
                         (('Launch now or wait?',), 'casual', {'name': 'Sam', 'relationship': 'friend'}))
        self.assertEqual(CallBrief.from_dict({**brief.to_dict(), 'context': 'Plain text.'}).session_context,
                         {'summary': 'Plain text.'})


class AssemblyTests(unittest.TestCase):
    def test_the_voice_gets_the_gist_and_the_backend_gets_everything(self):
        profile = validate_profile(PROFILE)
        contact = contact_for(profile, '+14155550143')
        session = parse_session_context(SESSION)
        notes = voice_notes(profile, contact, session, 'Robin')
        self.assertTrue(notes.startswith(VOICE_NOTES_PREFACE))
        self.assertIn('About Robin: Robin runs a small design studio', notes)
        self.assertIn("Speaking with: Sam, Robin's close friend. Works in product", notes)
        self.assertIn('- The first live calls happened today.', notes)
        self.assertNotIn('Pricing', notes)          # long details stay with the backend
        self.assertNotIn('Never discuss money', notes)
        background = backend_background(profile, contact, session, 'Robin')
        self.assertIn('Pricing: SignalWire', background)
        self.assertIn('- Never discuss money.', background)
        message = voice_input(notes)[0]
        self.assertEqual((message['role'], message['content'][0]['type']), ('developer', 'input_text'))
        self.assertIsNone(voice_input(''))
        self.assertEqual(voice_notes({}, None, {}, 'Robin'), '')

    def test_notes_are_kept_short(self):
        session = {'summary': 'word ' * 5000}
        self.assertLess(len(voice_notes({}, None, session, 'Robin')), 1800 * 4 + 10)


class ProfileApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        store = CallStore(root / 'calls')
        hooks = DefaultCallHooks(environ={'OPENAI_API_KEY': 'sk-test'}, env_file=root / '.env', store=store)
        service = CallService(store, hooks=hooks, lines={}, environ={})
        app = create_app(root=root / 'daemon', auth_token='launch-token', call_service=service)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()
        self.auth = {'Authorization': 'Bearer launch-token'}
        self.path = root / '.colleague' / 'profile.json'

    async def asyncTearDown(self):
        await self.client.close()
        self.temp.cleanup()

    async def test_read_and_update_the_profile(self):
        response = await self.client.get('/v1/profile', headers=self.auth)
        self.assertEqual(await response.json(), {'version': 1})
        response = await self.client.patch('/v1/profile', json=PROFILE, headers=self.auth)
        self.assertEqual(response.status, 200)
        self.assertEqual(json.loads(self.path.read_text())['people'][0]['name'], 'Sam')
        response = await self.client.patch('/v1/profile', json={'people': [{'name': 'Sam', 'phone': '12'}]},
                                           headers=self.auth)
        self.assertEqual(response.status, 422)
        self.assertEqual((await self.client.get('/v1/profile')).status, 401)
