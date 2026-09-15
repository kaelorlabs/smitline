import unittest

from runtime_config import RuntimeConfig


class RuntimeConfigTests(unittest.TestCase):
    def test_product_defaults_are_scenario_independent(self):
        config = RuntimeConfig.from_environ({})
        self.assertEqual(config.participant_name, 'Colleague AI')
        self.assertTrue(config.web_search_enabled)
        self.assertTrue(config.codex_enabled)
        self.assertFalse(config.charts_enabled)
        self.assertEqual(config.meeting_instructions, '')

    def test_accepts_operator_meeting_guidance(self):
        config = RuntimeConfig.from_environ({'COLLEAGUE_MEETING_INSTRUCTIONS': 'Focus on launch readiness.'})
        self.assertEqual(config.meeting_instructions, 'Focus on launch readiness.')

    def test_invalid_values_fail_at_startup(self):
        for env in ({'COLLEAGUE_CODEX_MODEL': 'anything'},
                    {'COLLEAGUE_ENABLE_CHARTS': '1', 'COLLEAGUE_ENABLE_CODEX': '0'},
                    {'COLLEAGUE_WORKSPACE': 'relative/path'},
                    {'COLLEAGUE_MEETING_INSTRUCTIONS': 'x' * 2001},
                    {'COLLEAGUE_PARTICIPANT_NAME': ''}):
            with self.assertRaises(ValueError):
                RuntimeConfig.from_environ(env)


if __name__ == '__main__':
    unittest.main()
