import unittest

from bridge import browser_environment, build_session_config
from runtime_config import RuntimeConfig
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


if __name__ == '__main__':
    unittest.main()
