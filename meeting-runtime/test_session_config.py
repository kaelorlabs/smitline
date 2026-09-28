import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from bridge import browser_environment, build_session_config
from meeting_intro import intro_event, introduce
from runtime_config import RuntimeConfig
from runtime_state import write_private_json
from test_schemas import context_payload, permissions_payload


class BrowserEnvironmentTests(unittest.TestCase):
    def test_keys_and_meeting_details_stay_out_of_the_browser(self):
        env = browser_environment({
            'OPENAI_API_KEY': 'x', 'TAVILY_API_KEY': 'x', 'TWILIO_ACCOUNT_SID': 'x',
            'TWILIO_AUTH_TOKEN': 'x', 'COLLEAGUE_CONNECTOR_PASSPHRASE': 'x',
            'MEETING_URL': 'x', 'MEETING_PASSCODE': 'x', 'SOME_SECRET': 'x',
            'DISPLAY': ':99', 'PULSE_SERVER': 'unix:/tmp/pulse', 'COLLEAGUE_OWNER_NAME': 'Robin',
        })
        self.assertEqual(env, {'DISPLAY': ':99', 'PULSE_SERVER': 'unix:/tmp/pulse',
                               'COLLEAGUE_OWNER_NAME': 'Robin'})


class SessionConfigTests(unittest.TestCase):
    def test_general_policy_is_capability_based(self):
        config = build_session_config(RuntimeConfig.from_environ({}))
        prompt = config['instructions'].lower()
        self.assertIn('any meeting task you can handle reliably', prompt)
        self.assertIn('default to listening silently', prompt)
        self.assertIn('if it is unclear whether someone addressed you, remain silent', prompt)
        self.assertIn('continue through brief listener backchannels', prompt)
        self.assertIn('never delegate merely because the conversation mentions a related topic', prompt)
        self.assertEqual(config['delegation'], {'type': 'client'})
        self.assertFalse(config['store'])
        self.assertNotIn('responses', config['delegation'])
        self.assertNotIn('input', config)
        self.assertEqual(config['model'], 'gpt-live-1')

    def test_operator_guidance_and_handoff_input_are_applied(self):
        runtime = RuntimeConfig.from_environ({
            'COLLEAGUE_ENABLE_WEB_SEARCH': '0',
            'COLLEAGUE_ENABLE_CODEX': '1',
            'COLLEAGUE_ENABLE_CHARTS': '1',
            'COLLEAGUE_MEETING_INSTRUCTIONS': 'Focus on release blockers.',
        })
        config = build_session_config(runtime, {
            'context': context_payload(),
            'permissions': permissions_payload(),
        })
        self.assertIn('Focus on release blockers.', config['instructions'])
        self.assertIn('chart or plot requests', config['instructions'])
        self.assertEqual(config['delegation'], {'type': 'client'})
        self.assertLessEqual(len(config['input']), 128)
        roles = [item['role'] for item in config['input']]
        self.assertEqual(roles[0], 'developer')
        self.assertIn('user', roles)

    def test_valid_voice_setting_selects_the_output_voice(self):
        config = build_session_config(RuntimeConfig.from_environ({'COLLEAGUE_VOICE': 'cinder'}))
        self.assertEqual(config['audio']['output'], {'voice': 'cinder'})
        extra = RuntimeConfig.from_environ({
            'COLLEAGUE_VOICE': 'aurora', 'COLLEAGUE_EXTRA_VOICES': 'aurora, lumen'})
        self.assertEqual(build_session_config(extra)['audio']['output'], {'voice': 'aurora'})
        self.assertNotIn('output', build_session_config(RuntimeConfig.from_environ({}))['audio'])

    def test_unknown_voice_is_ignored_with_a_log_line(self):
        with self.assertLogs('colleague.meeting', 'WARNING') as logs:
            runtime = RuntimeConfig.from_environ({'COLLEAGUE_VOICE': 'alloy'})
        self.assertIn('alloy', logs.output[0])
        config = build_session_config(runtime)
        self.assertEqual(config['audio'], {'format': {'type': 'audio/pcm', 'rate': 24000}})


class MeetingIntroTests(unittest.TestCase):
    INTRO = ("Hi, I'm an AI assistant joining on behalf of {}. "
             "I'll mostly listen; say 'Colleague' if you need me.")

    def write_state(self, directory, payload):
        path = Path(directory) / 'runtime.json'
        write_private_json(path, {'version': 1, 'meetingId': 'mtg-intro0000001', **payload})
        return {'COLLEAGUE_RUNTIME_STATE': str(path), 'COLLEAGUE_OWNER_NAME': 'Owner Setting'}

    def test_intro_is_on_by_default_and_names_the_owner_setting(self):
        runtime = RuntimeConfig.from_environ({'COLLEAGUE_OWNER_NAME': 'Robin'})
        config = build_session_config(runtime)
        self.assertIn(self.INTRO.format('Robin'), config['instructions'])
        self.assertIn('default to listening silently', config['instructions'].lower())
        event = intro_event(runtime)
        self.assertEqual(event['type'], 'session.commentary.append')
        self.assertIsNone(event['delegation_id'])
        self.assertIn(self.INTRO.format('Robin'), event['content'])

    def test_call_task_names_the_person_before_the_owner_setting(self):
        with tempfile.TemporaryDirectory() as directory:
            env = self.write_state(directory, {'context': context_payload(
                currentTask='Take part in this meeting on behalf of Maya Shah.')})
            runtime = RuntimeConfig.from_environ(env)
        self.assertEqual(runtime.owner_name, 'Maya Shah')
        self.assertIn(self.INTRO.format('Maya Shah'), intro_event(runtime)['content'])

    def test_explicit_field_wins_and_other_tasks_are_not_parsed(self):
        with tempfile.TemporaryDirectory() as directory:
            env = self.write_state(directory, {
                'onBehalfOf': 'Explicit Name',
                'context': context_payload(
                    currentTask='Take part in this meeting on behalf of Maya Shah.')})
            self.assertEqual(RuntimeConfig.from_environ(env).owner_name, 'Explicit Name')
            env = self.write_state(directory, {'context': context_payload(
                currentTask='Draft the notes on behalf of the team.')})
            self.assertEqual(RuntimeConfig.from_environ(env).owner_name, 'Owner Setting')

    def test_missing_name_uses_the_person_who_invited_me(self):
        runtime = RuntimeConfig.from_environ({})
        self.assertIn(self.INTRO.format('the person who invited me'), intro_event(runtime)['content'])

    def test_intro_can_be_switched_off(self):
        runtime = RuntimeConfig.from_environ({'COLLEAGUE_MEETING_INTRO': '0',
                                              'COLLEAGUE_OWNER_NAME': 'Robin'})
        self.assertIsNone(intro_event(runtime))
        self.assertNotIn('Robin', build_session_config(runtime)['instructions'])
        self.assertNotIn('Opening disclosure', build_session_config(runtime)['instructions'])


class IntroduceTests(unittest.IsolatedAsyncioTestCase):
    async def test_cue_waits_for_the_meeting_microphone_and_is_sent_once(self):
        runtime = RuntimeConfig.from_environ({'COLLEAGUE_OWNER_NAME': 'Robin'})
        participation = SimpleNamespace(platform_ready=False)
        ready, stop, sent = asyncio.Event(), asyncio.Event(), []

        async def send(event):
            sent.append(event)

        task = asyncio.create_task(introduce(send, runtime, participation, ready, stop, poll=.01))
        ready.set()
        await asyncio.sleep(.05)
        self.assertEqual(sent, [])
        participation.platform_ready = True
        self.assertTrue(await asyncio.wait_for(task, 1))
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]['type'], 'session.commentary.append')

    async def test_no_cue_when_disabled_or_stopped_first(self):
        ready, stop, sent = asyncio.Event(), asyncio.Event(), []

        async def send(event):
            sent.append(event)

        ready.set()
        off = RuntimeConfig.from_environ({'COLLEAGUE_MEETING_INTRO': 'off'})
        self.assertFalse(await introduce(send, off, SimpleNamespace(platform_ready=True), ready, stop))
        stop.set()
        on = RuntimeConfig.from_environ({})
        self.assertFalse(await introduce(send, on, SimpleNamespace(platform_ready=False),
                                         ready, stop, poll=.01))
        self.assertEqual(sent, [])


if __name__ == '__main__':
    unittest.main()
