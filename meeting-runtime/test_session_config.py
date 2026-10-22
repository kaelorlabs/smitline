import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from bridge import browser_environment, build_session_config
from meeting_intro import addressing_instructions, intro_event, introduce
from runtime_config import RuntimeConfig
from runtime_state import write_private_json
from test_schemas import context_payload


class BrowserEnvironmentTests(unittest.TestCase):
    def test_keys_and_meeting_details_stay_out_of_the_browser(self):
        env = browser_environment({
            'OPENAI_API_KEY': 'x', 'TWILIO_ACCOUNT_SID': 'x',
            'TWILIO_AUTH_TOKEN': 'x', 'SMITLINE_CONNECTOR_PASSPHRASE': 'x',
            'MEETING_URL': 'x', 'MEETING_PASSCODE': 'x', 'SOME_SECRET': 'x',
            'DISPLAY': ':99', 'PULSE_SERVER': 'unix:/tmp/pulse', 'SMITLINE_OWNER_NAME': 'Robin',
        })
        self.assertEqual(env, {'DISPLAY': ':99', 'PULSE_SERVER': 'unix:/tmp/pulse',
                               'SMITLINE_OWNER_NAME': 'Robin'})


class SessionConfigTests(unittest.TestCase):
    def test_general_policy_is_capability_based(self):
        config = build_session_config(RuntimeConfig.from_environ({}))
        prompt = config['instructions'].lower()
        self.assertIn('any meeting task you can handle reliably', prompt)
        self.assertIn('default to listening silently', prompt)
        self.assertIn('if it is unclear whether someone addressed you, remain silent', prompt)
        self.assertIn('continue through brief listener backchannels', prompt)
        self.assertIn('hand it to the backend instead of guessing', prompt)
        for removed in ('delegation policy', 'workspace', 'repository', 'codex', 'edited files'):
            self.assertNotIn(removed, prompt)
        self.assertFalse(config['store'])
        self.assertNotIn('input', config)
        self.assertEqual(config['model'], 'gpt-live-1')

    def test_hard_questions_use_responses_delegation(self):
        from phone_prompts import DEFAULT_BACKEND_MODEL
        config = build_session_config(RuntimeConfig.from_environ({'SMITLINE_OWNER_NAME': 'Robin'}))
        delegation = config['delegation']
        self.assertEqual(delegation['type'], 'responses')
        responses = delegation['responses']
        self.assertEqual(responses['model'], DEFAULT_BACKEND_MODEL)
        self.assertEqual(responses['tool_choice'], 'auto')
        self.assertEqual([tool['name'] for tool in responses['tools']], ['leave_meeting'])
        self.assertIn('on behalf of Robin', responses['instructions'])
        searching = build_session_config(RuntimeConfig.from_environ({
            'SMITLINE_MEETING_BACKEND_MODEL': 'gpt-5.6-mini', 'SMITLINE_MEETING_WEB_SEARCH': '1'}))
        self.assertEqual(searching['delegation']['responses']['model'], 'gpt-5.6-mini')
        self.assertEqual([tool.get('name', tool['type'])
                          for tool in searching['delegation']['responses']['tools']],
                         ['leave_meeting', 'web_search'])

    def test_the_agent_is_told_when_to_leave(self):
        config = build_session_config(RuntimeConfig.from_environ({}))
        prompt = config['instructions']
        self.assertIn('leave the meeting when a person in your place would', prompt)
        self.assertIn('when someone asks you to go (however they put it)', prompt)
        self.assertIn('when everyone else has left', prompt)
        self.assertIn('Finish what you are saying first', prompt)
        backend = config['delegation']['responses']['instructions']
        self.assertIn('Decide for yourself, the way a person would', backend)
        tool = config['delegation']['responses']['tools'][0]
        self.assertEqual(tool['parameters']['properties']['reason']['enum'],
                         ['completed', 'asked_to_leave', 'not_productive', 'other'])
        self.assertTrue(tool['strict'])

    def test_operator_guidance_and_handoff_input_are_applied(self):
        runtime = RuntimeConfig.from_environ({
            'SMITLINE_MEETING_INSTRUCTIONS': 'Focus on release blockers.',
        })
        config = build_session_config(runtime, {'context': context_payload()})
        self.assertIn('Focus on release blockers.', config['instructions'])
        backend = config['delegation']['responses']['instructions']
        self.assertIn('Focus on release blockers.', backend)
        self.assertIn('Ship the developer platform', backend)
        self.assertLessEqual(len(config['input']), 128)
        roles = [item['role'] for item in config['input']]
        self.assertEqual(roles[0], 'developer')
        self.assertIn('user', roles)

    def test_valid_voice_setting_selects_the_output_voice(self):
        config = build_session_config(RuntimeConfig.from_environ({'SMITLINE_VOICE': 'cinder'}))
        self.assertEqual(config['audio']['output'], {'voice': 'cinder'})
        extra = RuntimeConfig.from_environ({
            'SMITLINE_VOICE': 'aurora', 'SMITLINE_EXTRA_VOICES': 'aurora, lumen'})
        self.assertEqual(build_session_config(extra)['audio']['output'], {'voice': 'aurora'})
        self.assertNotIn('output', build_session_config(RuntimeConfig.from_environ({}))['audio'])

    def test_unknown_voice_is_ignored_with_a_log_line(self):
        with self.assertLogs('colleague.meeting', 'WARNING') as logs:
            runtime = RuntimeConfig.from_environ({'SMITLINE_VOICE': 'alloy'})
        self.assertIn('alloy', logs.output[0])
        config = build_session_config(runtime)
        self.assertEqual(config['audio'], {'format': {'type': 'audio/pcm', 'rate': 24000}})


class MeetingIntroTests(unittest.TestCase):
    INTRO = "Hi everyone, {}'s AI assistant here."

    def write_state(self, directory, payload):
        path = Path(directory) / 'runtime.json'
        write_private_json(path, {'version': 1, 'meetingId': 'mtg-intro0000001', **payload})
        return {'SMITLINE_RUNTIME_STATE': str(path), 'SMITLINE_OWNER_NAME': 'Owner Setting'}

    def test_meetings_join_quietly_and_know_who_they_are(self):
        runtime = RuntimeConfig.from_environ({'SMITLINE_OWNER_NAME': 'Robin'})
        self.assertFalse(runtime.meeting_intro)
        self.assertEqual(runtime.participant_name, 'Smitline AI')
        self.assertIsNone(intro_event(runtime))
        prompt = build_session_config(runtime)['instructions']
        self.assertNotIn('Opening disclosure', prompt)
        self.assertIn("Who you are: Robin's AI assistant.", prompt)
        self.assertIn('Do not introduce yourself unprompted', prompt)
        from meeting_intro import camera_caption
        self.assertEqual(camera_caption('Robin Rivera'), "Robin's AI assistant")
        self.assertEqual(camera_caption(''), 'AI assistant')
        self.assertEqual(camera_caption('A' * 60), 'AI assistant')

    def test_the_spoken_hello_is_opt_in_and_names_the_owner_setting(self):
        runtime = RuntimeConfig.from_environ({'SMITLINE_OWNER_NAME': 'Robin', 'SMITLINE_MEETING_INTRO': '1'})
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
                currentTask='Take part in this meeting on behalf of Jordan Lee.')})
            runtime = RuntimeConfig.from_environ(dict(env, SMITLINE_MEETING_INTRO='1'))
        self.assertEqual(runtime.owner_name, 'Jordan Lee')
        self.assertIn(self.INTRO.format('Jordan'), intro_event(runtime)['content'])

    def test_explicit_field_wins_and_other_tasks_are_not_parsed(self):
        with tempfile.TemporaryDirectory() as directory:
            env = self.write_state(directory, {
                'onBehalfOf': 'Explicit Name',
                'context': context_payload(
                    currentTask='Take part in this meeting on behalf of Jordan Lee.')})
            self.assertEqual(RuntimeConfig.from_environ(env).owner_name, 'Explicit Name')
            env = self.write_state(directory, {'context': context_payload(
                currentTask='Draft the notes on behalf of the team.')})
            self.assertEqual(RuntimeConfig.from_environ(env).owner_name, 'Owner Setting')

    def test_missing_name_uses_the_person_who_invited_me(self):
        runtime = RuntimeConfig.from_environ({'SMITLINE_MEETING_INTRO': 'on'})
        self.assertIn("Hi everyone, I'm an AI assistant for the person who invited me.",
                      intro_event(runtime)['content'])

    def test_people_are_named_as_people_say_them(self):
        from meeting_intro import spoken_name
        self.assertEqual(spoken_name('Arun Raj'), 'Arun')
        self.assertEqual(spoken_name('Dr. Lee'), 'Dr. Lee')
        self.assertEqual(spoken_name('  '), '')

    def test_the_voice_switches_language_and_stops_replying_to_thanks(self):
        prompt = build_session_config(RuntimeConfig.from_environ({}))['instructions']
        self.assertIn('Hindi, Tamil, or Telugu, switch right away', prompt)
        self.assertIn('do not reply to thanks', prompt)

    def test_people_can_address_it_as_smitline_or_as_heard(self):
        prompt = build_session_config(RuntimeConfig.from_environ({}))['instructions']
        self.assertIn('people address you as "Smitline AI" or "Smitline".', prompt)
        for heard in ('"smit line"', '"Smith line"', '"Smithline"', '"Smitlin"'):
            self.assertIn(heard, prompt)
        self.assertNotIn('Colleague', prompt)

    def test_a_custom_participant_name_is_accepted_alongside_smitline(self):
        runtime = RuntimeConfig.from_environ({'SMITLINE_PARTICIPANT_NAME': 'Robin Bot'})
        prompt = build_session_config(runtime)['instructions']
        self.assertIn('people address you as "Robin Bot" or "Smitline".', prompt)
        self.assertEqual(addressing_instructions('smitline'), addressing_instructions(''))

    def test_addressing_names_stay_when_the_intro_is_off(self):
        runtime = RuntimeConfig.from_environ({'SMITLINE_MEETING_INTRO': '0'})
        prompt = build_session_config(runtime)['instructions']
        self.assertIn('"Smitline"', prompt)

    def test_intro_can_be_switched_off(self):
        runtime = RuntimeConfig.from_environ({'SMITLINE_MEETING_INTRO': '0',
                                              'SMITLINE_OWNER_NAME': 'Robin'})
        self.assertIsNone(intro_event(runtime))
        self.assertNotIn('Opening disclosure', build_session_config(runtime)['instructions'])


class IntroduceTests(unittest.IsolatedAsyncioTestCase):
    async def test_cue_waits_for_the_meeting_microphone_and_is_sent_once(self):
        runtime = RuntimeConfig.from_environ({'SMITLINE_OWNER_NAME': 'Robin', 'SMITLINE_MEETING_INTRO': '1'})
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

    async def test_cue_waits_for_a_pause_in_the_conversation(self):
        runtime = RuntimeConfig.from_environ({'SMITLINE_OWNER_NAME': 'Robin', 'SMITLINE_MEETING_INTRO': '1'})
        ready, stop, sent = asyncio.Event(), asyncio.Event(), []
        now, heard = [100.0], [None]

        async def send(event):
            sent.append(event)
        ready.set()
        task = asyncio.create_task(introduce(
            send, runtime, SimpleNamespace(platform_ready=True), ready, stop, poll=.01,
            heard_at=lambda: heard[0], clock=lambda: now[0], quiet=2.0, max_wait=45.0))
        for _ in range(5):  # someone keeps talking: no intro over them
            now[0] += 1.0
            heard[0] = now[0]
            await asyncio.sleep(.03)
        self.assertEqual(sent, [])
        now[0] += 2.5  # a pause
        self.assertTrue(await asyncio.wait_for(task, 1))
        self.assertEqual(len(sent), 1)

    async def test_a_meeting_that_never_pauses_still_hears_the_disclosure(self):
        runtime = RuntimeConfig.from_environ({'SMITLINE_OWNER_NAME': 'Robin', 'SMITLINE_MEETING_INTRO': '1'})
        ready, stop, sent = asyncio.Event(), asyncio.Event(), []
        now = [100.0]

        async def send(event):
            sent.append(event)
        ready.set()
        task = asyncio.create_task(introduce(
            send, runtime, SimpleNamespace(platform_ready=True), ready, stop, poll=.01,
            heard_at=lambda: now[0], clock=lambda: now[0], quiet=2.0, max_wait=45.0))
        await asyncio.sleep(.03)
        self.assertEqual(sent, [])
        now[0] += 46
        self.assertTrue(await asyncio.wait_for(task, 1))
        # Nobody heard yet: a quiet room gets it after the quiet time.
        sent.clear()
        now[0] = 0.0
        task = asyncio.create_task(introduce(
            send, runtime, SimpleNamespace(platform_ready=True), ready, stop, poll=.01,
            heard_at=lambda: None, clock=lambda: now[0], quiet=2.0))
        await asyncio.sleep(.03)
        self.assertEqual(sent, [])
        now[0] = 2.0
        self.assertTrue(await asyncio.wait_for(task, 1))

    async def test_no_cue_when_disabled_or_stopped_first(self):
        ready, stop, sent = asyncio.Event(), asyncio.Event(), []

        async def send(event):
            sent.append(event)

        ready.set()
        off = RuntimeConfig.from_environ({'SMITLINE_MEETING_INTRO': 'off'})
        self.assertFalse(await introduce(send, off, SimpleNamespace(platform_ready=True), ready, stop))
        stop.set()
        on = RuntimeConfig.from_environ({'SMITLINE_MEETING_INTRO': '1'})
        self.assertFalse(await introduce(send, on, SimpleNamespace(platform_ready=False),
                                         ready, stop, poll=.01))
        self.assertEqual(sent, [])


if __name__ == '__main__':
    unittest.main()
