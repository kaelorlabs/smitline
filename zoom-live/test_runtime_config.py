import unittest

from runtime_config import RuntimeConfig


class RuntimeConfigTests(unittest.TestCase):
    def test_standard_profile_is_the_product_default(self):
        config = RuntimeConfig.from_environ({})
        self.assertEqual(config.profile, 'standard')
        self.assertEqual(config.participant_name, 'Colleague AI')
        self.assertFalse(config.demo_data_enabled)

    def test_demo_profiles_enable_demo_data(self):
        self.assertTrue(RuntimeConfig.from_environ({'COLLEAGUE_PROFILE': 'demo'}).demo_data_enabled)
        self.assertTrue(RuntimeConfig.from_environ({'COLLEAGUE_PROFILE': 'fact_check'}).demo_data_enabled)

    def test_legacy_fact_check_setting_remains_supported(self):
        self.assertEqual(RuntimeConfig.from_environ({'COLLEAGUE_FACT_CHECK': '1'}).profile, 'fact_check')

    def test_invalid_values_fail_at_startup(self):
        for env in ({'COLLEAGUE_PROFILE': 'sales-demo'},
                    {'COLLEAGUE_CODEX_MODEL': 'anything'},
                    {'COLLEAGUE_PARTICIPANT_NAME': ''}):
            with self.assertRaises(ValueError):
                RuntimeConfig.from_environ(env)


if __name__ == '__main__':
    unittest.main()
