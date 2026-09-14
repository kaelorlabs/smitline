import unittest

from bridge import build_session_config
from runtime_config import RuntimeConfig


class SessionConfigTests(unittest.TestCase):
    def test_general_policy_is_capability_based(self):
        config = build_session_config(RuntimeConfig.from_environ({}))
        prompt = config['instructions'].lower()
        self.assertIn('any meeting task you can handle reliably', prompt)
        self.assertIn('without requiring a wake phrase', prompt)
        self.assertIn('choose a tool based on the task rather than the topic', prompt)

    def test_operator_guidance_and_tool_permissions_are_applied(self):
        runtime = RuntimeConfig.from_environ({
            'COLLEAGUE_ENABLE_WEB_SEARCH': '0',
            'COLLEAGUE_ENABLE_CODEX': '1',
            'COLLEAGUE_ENABLE_CHARTS': '1',
            'COLLEAGUE_MEETING_INSTRUCTIONS': 'Focus on release blockers.',
        })
        config = build_session_config(runtime)
        self.assertIn('Focus on release blockers.', config['instructions'])
        self.assertIn('chart or plot requests', config['instructions'])
        tools = config['delegation']['responses']['tools']
        self.assertEqual([tool['name'] for tool in tools], ['run_codex'])


if __name__ == '__main__':
    unittest.main()
