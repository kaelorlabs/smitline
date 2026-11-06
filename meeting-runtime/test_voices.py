"""Default voices: which voice each kind of call speaks with when its brief names none, on every
path a call takes, and how the owner's agent changes the defaults (PATCH /v1/profile voices, the
save_voices hook, GET /v1/voices). Phone calls: the brief's voice, SMITLINE_VOICE, marin.
Meetings, by link or by phone: the brief's voice, SMITLINE_MEETING_VOICE, in the browser a
presence pack's voice, SMITLINE_VOICE, marin."""
import asyncio
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from aiohttp.test_utils import TestClient, TestServer

import dial_in
from call_brief import CallBrief
from call_hooks import VOICE_SETTINGS, CallRefused, DefaultCallHooks, read_env_file, write_env_file
from call_service import CallError, CallService
from call_store import CallStore
from meeting_line import MeetingLine, meeting_payload, meeting_voice, voice_defaults
from runtime_daemon import create_app
from test_call_service import ZOOM, ServiceHarness, brief

BOTH = {'SMITLINE_VOICE': 'quartz', 'SMITLINE_MEETING_VOICE': 'cinder'}


class VoiceDefaultsTests(unittest.TestCase):
    def test_each_kind_of_call_has_its_own_default(self):
        self.assertEqual(voice_defaults({}), {'phone': 'marin', 'meeting': 'marin'})
        self.assertEqual(voice_defaults({'SMITLINE_VOICE': 'quartz'}), {'phone': 'quartz', 'meeting': 'quartz'})
        self.assertEqual(voice_defaults(BOTH), {'phone': 'quartz', 'meeting': 'cinder'})
        self.assertEqual(voice_defaults({'SMITLINE_MEETING_VOICE': 'cinder'}),
                         {'phone': 'marin', 'meeting': 'cinder'})

    def test_a_meeting_voice_the_installation_lacks_is_no_default(self):
        self.assertEqual(meeting_voice({'SMITLINE_MEETING_VOICE': 'alloy'}), '')
        self.assertEqual(voice_defaults({'SMITLINE_VOICE': 'quartz', 'SMITLINE_MEETING_VOICE': 'alloy'}),
                         {'phone': 'quartz', 'meeting': 'quartz'})
        self.assertEqual(meeting_voice({'SMITLINE_MEETING_VOICE': 'aurora', 'SMITLINE_EXTRA_VOICES': 'aurora'}),
                         'aurora')
        # Only the owner's settings count, never this process's environment.
        with mock.patch.dict(os.environ, {'SMITLINE_MEETING_VOICE': 'cinder'}):
            self.assertEqual(meeting_voice({}), '')
            self.assertEqual(meeting_voice(None), '')


class MeetingPayloadTests(unittest.TestCase):
    """What the meeting line asks the daemon for: the meeting's voice reaches it as `voice`."""

    def payload(self, settings, **overrides):
        return meeting_payload(CallBrief.from_dict(brief(channel='meeting', to=ZOOM, **overrides)), settings)

    def test_the_briefs_voice_then_the_meeting_voice(self):
        self.assertEqual(self.payload(BOTH, voice='vesper')['voice'], 'vesper')
        self.assertEqual(self.payload(BOTH)['voice'], 'cinder')

    def test_without_a_meeting_voice_the_meeting_keeps_its_own_default(self):
        # Not SMITLINE_VOICE: in the meeting, a presence pack's voice comes before it.
        self.assertNotIn('voice', self.payload({'SMITLINE_VOICE': 'quartz'}))
        self.assertNotIn('voice', self.payload({'SMITLINE_MEETING_VOICE': 'alloy'}))
        self.assertNotIn('voice', self.payload(None))


class MeetingContainerVoiceTests(unittest.TestCase):
    """What the meeting itself speaks with (bridge.py): the voice the daemon put in its runtime
    state, else a presence pack's voice, else SMITLINE_VOICE, else GPT-Live's own default."""

    def voice(self, session_voice=None, env=None, pack_voice=None):
        import bridge
        from agent_sessions import MeetingSession
        from presence_pack import load_presence_pack
        from runtime_config import RuntimeConfig
        from runtime_state import read_json, state_from_session, write_private_json
        from test_presence_pack import write_pack
        from test_schemas import meeting_session_payload
        session = MeetingSession.from_dict(meeting_session_payload(voice=session_voice))
        with tempfile.TemporaryDirectory() as root:
            state_path = Path(root, 'runtime.json')
            write_private_json(state_path, state_from_session(session))
            environ = dict(env or {}, SMITLINE_RUNTIME_STATE=str(state_path), SMITLINE_MEETING_INTRO='0')
            runtime = RuntimeConfig.from_environ(environ)
            meeting_state = read_json(state_path)
            pack = None
            if pack_voice:
                manifest = write_pack(Path(root, 'pack'))
                manifest['voice'] = pack_voice
                Path(root, 'pack', 'manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
                pack = load_presence_pack(Path(root, 'pack'))
            runtime = bridge.with_persona(runtime, pack, meeting_state)
            config = bridge.build_session_config(runtime, meeting_state)
        return config['audio'].get('output', {}).get('voice')

    def test_the_voice_the_daemon_chose_wins_over_the_pack_and_the_phone_voice(self):
        self.assertEqual(self.voice('cinder', {'SMITLINE_VOICE': 'quartz'}, pack_voice='bossa'), 'cinder')

    def test_without_one_the_pack_then_the_phone_voice(self):
        self.assertEqual(self.voice(None, {'SMITLINE_VOICE': 'quartz'}, pack_voice='bossa'), 'bossa')
        self.assertEqual(self.voice(None, {'SMITLINE_VOICE': 'quartz'}), 'quartz')
        self.assertIsNone(self.voice(None, {}))  # GPT-Live's default, marin


class MeetingCallThroughTheDaemonTests(unittest.IsolatedAsyncioTestCase):
    """A meeting call's voice, as the real daemon keeps it for the meeting it starts."""

    async def session_voice(self, environ, **overrides):
        from test_runtime_daemon import FakeSupervisor
        from test_schemas import handoff_payload
        with tempfile.TemporaryDirectory() as temp:
            harness = ServiceHarness(temp, environ=dict({'OPENAI_API_KEY': 'sk-test'}, **environ))

            def lines(daemon):
                harness.service.lines = {'meeting': MeetingLine(daemon, poll_interval=0.01, handoff_timeout=5)}
                return harness.service
            app = create_app(root=Path(temp) / 'daemon', auth_token='t', supervisor=FakeSupervisor(),
                             call_service_factory=lines)
            daemon = app.runtime_daemon
            try:
                record = await harness.service.create(brief(channel='meeting', to=ZOOM, **overrides))
                call = await harness.service.wait(record['id'], timeout=0.2, owner='local')
                self.assertNotEqual(call['status'], 'failed', call.get('error'))
                session = await daemon.get_meeting(call['line']['meetingId'])
                daemon.transition(session.id, 'ended')
                daemon.store_handoff(handoff_payload(meetingId=session.id, startedAt=session.started_at))
                await harness.service.wait(record['id'], timeout=5, owner='local')
                return session.voice
            finally:
                await harness.service.shutdown()
                daemon.close()

    async def test_the_meeting_voice_then_the_briefs(self):
        self.assertEqual(await self.session_voice(BOTH), 'cinder')
        self.assertEqual(await self.session_voice(BOTH, voice='vesper'), 'vesper')
        self.assertIsNone(await self.session_voice({'SMITLINE_VOICE': 'quartz'}))

    async def test_an_extra_voice_in_the_owners_settings_is_one_of_theirs(self):
        # SMITLINE_EXTRA_VOICES in .env is not in this process's environment.
        self.assertNotIn('aurora', os.environ.get('SMITLINE_EXTRA_VOICES', ''))
        extra = {'SMITLINE_EXTRA_VOICES': 'aurora', 'SMITLINE_MEETING_VOICE': 'aurora'}
        self.assertEqual(await self.session_voice(extra), 'aurora')
        self.assertEqual(await self.session_voice({'SMITLINE_EXTRA_VOICES': 'aurora'}, voice='aurora'), 'aurora')


class MeetingsApiVoiceTests(unittest.IsolatedAsyncioTestCase):
    """The console's /v1/meetings starts a meeting with the owner's meeting voice too."""

    async def test_a_meeting_without_a_voice_gets_the_meeting_voice(self):
        from test_runtime_daemon import FakeSupervisor, create_payload
        with tempfile.TemporaryDirectory() as temp:
            store = CallStore(Path(temp) / 'calls')
            hooks = DefaultCallHooks(environ=dict(BOTH, OPENAI_API_KEY='sk-test'), store=store)
            service = CallService(store, hooks=hooks, lines={}, environ={})
            supervisor = FakeSupervisor()
            app = create_app(root=Path(temp) / 'daemon', auth_token='t', supervisor=supervisor,
                             call_service=service)
            client = TestClient(TestServer(app))
            await client.start_server()
            try:
                auth = {'Authorization': 'Bearer t'}
                for payload, expected in ((create_payload(), 'cinder'), (create_payload(voice='vesper'), 'vesper')):
                    response = await client.post('/v1/meetings', json=payload, headers=auth)
                    self.assertEqual(response.status, 201, await response.text())
                    self.assertEqual(supervisor.sessions[-1].voice, expected)
                hooks._environ.pop('SMITLINE_MEETING_VOICE')
                response = await client.post('/v1/meetings', json=create_payload(), headers=auth)
                self.assertEqual(response.status, 201)
                self.assertIsNone(supervisor.sessions[-1].voice)
            finally:
                await client.close()


class DialInVoiceTests(unittest.TestCase):
    """A meeting joined by phone speaks with a meeting's voice; there is no presence pack."""

    def voice(self, settings, **overrides):
        from test_dial_in import ZOOM_INVITE, ZOOM_LINK, meeting_brief
        parsed = CallBrief.from_dict(meeting_brief(to=ZOOM_LINK, dialIn={'invite': ZOOM_INVITE}, **overrides),
                                     environ=settings)
        return dial_in.voice_config(parsed, settings)['audio']['output']['voice']

    def test_the_briefs_voice_then_the_meeting_voice_then_the_phone_voice(self):
        self.assertEqual(self.voice(BOTH, voice='vesper'), 'vesper')
        self.assertEqual(self.voice(BOTH), 'cinder')
        self.assertEqual(self.voice({'SMITLINE_VOICE': 'quartz'}), 'quartz')
        self.assertEqual(self.voice({'SMITLINE_VOICE': 'quartz', 'SMITLINE_MEETING_VOICE': 'alloy'}), 'quartz')
        self.assertEqual(self.voice({}), 'marin')


class DialInCallVoiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_line_reads_the_owners_meeting_voice(self):
        from test_dial_in import FAST, ZOOM_INVITE, ZOOM_LINK, DialHarness
        from test_phone import FakeLive
        FakeLive.instances = []
        with tempfile.TemporaryDirectory() as temp, \
                mock.patch.multiple(dial_in, **FAST):
            h = DialHarness(temp, env=dict(BOTH, SMITLINE_MEETING_DIAL_IN_FIRST='all'))
            try:
                record, session = await h.join(to=ZOOM_LINK, dialIn={'invite': ZOOM_INVITE})
                self.assertEqual(session.config()['audio']['output'], {'voice': 'cinder'})
                await h.service.end(record['id'], owner='local')
                await h.service.wait(record['id'], timeout=5, owner='local')
            finally:
                await h.service.shutdown()


class PhoneVoiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_phone_call_speaks_with_the_phone_voice_never_the_meeting_one(self):
        from test_phone import ENV, FakeLive, PhoneHarness
        FakeLive.instances = []
        for env, overrides, expected in ((BOTH, {}, 'quartz'), (BOTH, {'voice': 'vesper'}, 'vesper'),
                                         ({'SMITLINE_MEETING_VOICE': 'cinder'}, {}, 'marin')):
            with self.subTest(env=env, overrides=overrides), tempfile.TemporaryDirectory() as temp:
                h = PhoneHarness(temp, env=dict(ENV, **env))
                try:
                    record, session = await h.dial(**overrides)
                    self.assertEqual(session.config()['audio']['output'], {'voice': expected})
                    await h.service.end(record['id'], owner='local')
                    await h.service.wait(record['id'], timeout=5, owner='local')
                finally:
                    await h.service.shutdown()


class VoicesApiTests(unittest.IsolatedAsyncioTestCase):
    """The owner's agent reads the defaults (GET /v1/voices, GET /v1/profile) and changes them
    (PATCH /v1/profile voices); the default hooks keep them in .env."""

    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.env_file = self.root / '.env'
        self.env_file.write_text('# Smitline\nSMITLINE_OWNER_NAME=Robin\nSMITLINE_VOICE=vesper\n')
        self.environ = {'OPENAI_API_KEY': 'sk-test'}
        store = CallStore(self.root / 'calls')
        self.hooks = DefaultCallHooks(environ=self.environ, env_file=self.env_file, store=store)
        self.service = CallService(store, hooks=self.hooks, lines={}, environ={})
        app = create_app(root=self.root / 'daemon', auth_token='launch-token', call_service=self.service)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()
        self.auth = {'Authorization': 'Bearer launch-token'}
        self.profile_file = self.root / '.smitline' / 'profile.json'

    async def asyncTearDown(self):
        await self.client.close()
        self.temp.cleanup()

    async def patch(self, body):
        response = await self.client.patch('/v1/profile', json=body, headers=self.auth)
        return response.status, await response.json()

    async def get(self, path):
        return await (await self.client.get(path, headers=self.auth)).json()

    async def test_an_agent_sets_and_clears_the_default_voices(self):
        listed = await self.get('/v1/voices')
        self.assertEqual(listed['default'], 'vesper')
        self.assertEqual(listed['defaults'], {'phone': 'vesper', 'meeting': 'vesper'})
        self.assertIn('cinder', listed['voices'])
        status, profile = await self.patch({'voices': {'meeting': 'cinder'}})
        self.assertEqual((status, profile['voices']), (200, {'phone': 'vesper', 'meeting': 'cinder'}))
        self.assertEqual(read_env_file(self.env_file)['SMITLINE_MEETING_VOICE'], 'cinder')
        self.assertEqual((await self.get('/v1/voices'))['defaults'], {'phone': 'vesper', 'meeting': 'cinder'})
        status, profile = await self.patch({'voices': {'phone': 'quartz', 'meeting': ''}})
        self.assertEqual((status, profile['voices']), (200, {'phone': 'quartz', 'meeting': 'quartz'}))
        self.assertEqual(self.env_file.read_text(), '# Smitline\nSMITLINE_OWNER_NAME=Robin\nSMITLINE_VOICE=quartz\n')
        self.assertEqual(stat.S_IMODE(self.env_file.stat().st_mode), 0o600)
        status, profile = await self.patch({'voices': {'phone': ''}})
        self.assertEqual(profile['voices'], {'phone': 'marin', 'meeting': 'marin'})
        # The profile shows them; an update of the voices alone leaves the saved profile alone.
        self.assertEqual(await self.get('/v1/profile'), {'version': 1, 'voices': {'phone': 'marin', 'meeting': 'marin'}})
        self.assertFalse(self.profile_file.exists())

    async def test_the_profile_and_the_voices_change_together(self):
        status, profile = await self.patch({'about': 'Robin builds Smitline.', 'voices': {'phone': 'cinder'}})
        self.assertEqual(status, 200)
        self.assertEqual((profile['about'], profile['voices']['phone']), ('Robin builds Smitline.', 'cinder'))
        self.assertNotIn('voices', json.loads(self.profile_file.read_text()))  # voices are settings
        status, profile = await self.patch({'style': 'Brief.'})
        self.assertEqual((profile['about'], profile['style'], profile['voices']['phone']),
                         ('Robin builds Smitline.', 'Brief.', 'cinder'))

    async def test_a_voice_it_does_not_have_is_refused_and_nothing_is_saved(self):
        before = self.env_file.read_text()
        status, body = await self.patch({'about': 'Robin builds Smitline.', 'voices': {'meeting': 'alloy'}})
        self.assertEqual((status, body['error']['code']), (422, 'invalid_request'))
        self.assertTrue(body['error']['message'].startswith('voices.meeting must be one of: marin, vesper, '))
        self.assertFalse(self.profile_file.exists())
        for bad, says in (({'voices': 'cinder'}, 'voices must be an object'),
                          ({'voices': {'phone': 7}}, 'voices.phone must be a string'),
                          ({'voices': {'phone': 'Cinder'}}, 'voices.phone must be one of'),
                          ({'voices': {'camera': 'cinder'}}, 'unknown voices fields: camera'),
                          ({'about': 7, 'voices': {'phone': 'cinder'}}, 'about must be text')):
            with self.subTest(bad=bad):
                status, body = await self.patch(bad)
                self.assertEqual(status, 422)
                self.assertIn(says, body['error']['message'])
        self.assertEqual(self.env_file.read_text(), before)
        self.assertFalse(self.profile_file.exists())
        # Voices the installation adds (SMITLINE_EXTRA_VOICES) can be chosen.
        self.environ['SMITLINE_EXTRA_VOICES'] = 'aurora'
        status, profile = await self.patch({'voices': {'meeting': 'aurora'}})
        self.assertEqual((status, profile['voices']['meeting']), (200, 'aurora'))

    async def test_a_voice_set_in_the_environment_stays(self):
        self.environ['SMITLINE_VOICE'] = 'quartz'
        before = self.env_file.read_text()
        status, body = await self.patch({'about': 'Robin.', 'voices': {'phone': 'cinder'}})
        self.assertEqual((status, body['error']['code']), (409, 'set_in_environment'))
        self.assertIn('SMITLINE_VOICE is set in the environment', body['error']['message'])
        self.assertEqual(self.env_file.read_text(), before)
        self.assertFalse(self.profile_file.exists())
        status, profile = await self.patch({'voices': {'meeting': 'cinder'}})
        self.assertEqual((status, profile['voices']), (200, {'phone': 'quartz', 'meeting': 'cinder'}))


class SaveVoicesTests(unittest.IsolatedAsyncioTestCase):
    def hooks(self, temp, environ=None, cls=DefaultCallHooks):
        env_file = Path(temp) / '.env'
        store = CallStore(Path(temp) / 'calls')
        hooks = cls(environ=environ or {'OPENAI_API_KEY': 'sk-test'}, env_file=env_file, store=store)
        return hooks, CallService(store, hooks=hooks, lines={}, environ={}), env_file

    async def test_the_default_hooks_keep_only_the_installations_own_voices(self):
        with tempfile.TemporaryDirectory() as temp:
            hooks, service, env_file = self.hooks(temp)
            with self.assertRaises(CallError) as refused:
                await service.update_profile('acct_a', {'voices': {'meeting': 'cinder'}})
            self.assertEqual((refused.exception.status, refused.exception.code), (501, 'unsupported'))
            self.assertFalse(env_file.exists())
            with self.assertRaises(CallRefused):
                DefaultCallHooks(environ={}).save_voices('local', {'phone': 'cinder'})  # no .env

    async def test_hooks_that_keep_no_voices_save_nothing(self):
        class NoVoices(DefaultCallHooks):
            save_voices = None
        with tempfile.TemporaryDirectory() as temp:
            hooks, service, env_file = self.hooks(temp, cls=NoVoices)
            with self.assertRaises(CallError) as refused:
                await service.update_profile('local', {'about': 'Robin.', 'voices': {'phone': 'cinder'}})
            self.assertEqual((refused.exception.status, refused.exception.code), (501, 'unsupported'))
            self.assertEqual(service.profile('local'), {'version': 1, 'voices': {'phone': 'marin', 'meeting': 'marin'}})
            # The rest of the profile still works without them.
            saved = await service.update_profile('local', {'about': 'Robin.'})
            self.assertEqual(saved['about'], 'Robin.')

    async def test_hooks_with_voices_per_owner_get_the_owner_and_may_be_async(self):
        class PerOwner(DefaultCallHooks):
            saved = {}

            def settings(self, owner):
                return dict(self.environ, **self.saved.get(owner, {}))

            async def save_voices(self, owner, voices):
                await asyncio.sleep(0)
                mine = self.saved.setdefault(owner, {})
                mine.update({VOICE_SETTINGS[kind]: name for kind, name in voices.items()})
        with tempfile.TemporaryDirectory() as temp:
            hooks, service, env_file = self.hooks(temp, cls=PerOwner)
            saved = await service.update_profile('acct_a', {'voices': {'meeting': 'cinder', 'phone': 'quartz'}})
            self.assertEqual(saved['voices'], {'phone': 'quartz', 'meeting': 'cinder'})
            self.assertEqual(service.profile('acct_b')['voices'], {'phone': 'marin', 'meeting': 'marin'})
            self.assertEqual(PerOwner.saved, {'acct_a': {'SMITLINE_VOICE': 'quartz', 'SMITLINE_MEETING_VOICE': 'cinder'}})
            self.assertFalse(env_file.exists())


class WriteEnvFileTests(unittest.TestCase):
    def test_it_merges_as_setup_set_does(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / '.env'
            path.write_text('# keys\nexport SMITLINE_VOICE = marin\nPORT=1\nSMITLINE_VOICE=quartz\n\n')
            os.chmod(path, 0o644)
            write_env_file(path, {'SMITLINE_VOICE': 'cinder', 'SMITLINE_MEETING_VOICE': 'vesper'})
            self.assertEqual(path.read_text(), '# keys\nSMITLINE_VOICE=cinder\nPORT=1\nSMITLINE_MEETING_VOICE=vesper\n')
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(read_env_file(path), {'SMITLINE_VOICE': 'cinder', 'PORT': '1',
                                                   'SMITLINE_MEETING_VOICE': 'vesper'})
            write_env_file(path, {'SMITLINE_MEETING_VOICE': None, 'NOT_SET': None})
            self.assertEqual(path.read_text(), '# keys\nSMITLINE_VOICE=cinder\nPORT=1\n')
            with self.assertRaises(ValueError):
                write_env_file(path, {'SMITLINE_VOICE': 'cinder\nOPENAI_API_KEY=sk-x'})
            self.assertEqual(path.read_text(), '# keys\nSMITLINE_VOICE=cinder\nPORT=1\n')
            self.assertEqual(sorted(os.listdir(temp)), ['.env'])  # no temporary file left

    def test_it_makes_the_file(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / '.env'
            write_env_file(path, {'SMITLINE_MEETING_VOICE': 'cinder', 'SMITLINE_VOICE': None})
            self.assertEqual(path.read_text(), 'SMITLINE_MEETING_VOICE=cinder\n')


if __name__ == '__main__':
    unittest.main()
