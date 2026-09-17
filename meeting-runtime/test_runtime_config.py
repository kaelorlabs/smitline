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
        self.assertTrue(config.camera_enabled)
        self.assertTrue(config.camera_default_on)
        self.assertFalse(config.screen_share_enabled)

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

    def test_camera_settings_are_read_from_runtime_state_not_env(self):
        import json
        import tempfile
        from pathlib import Path
        from runtime_state import write_private_json
        from visual_presence import encode_avatar_bytes
        png = (
            b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01'
            b'\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f\x00\x00\x01'
            b'\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82'
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'runtime.json'
            write_private_json(path, {
                'version': 1,
                'meetingId': 'mtg-cam',
                'cameraEnabled': False,
                'cameraDefaultOn': False,
                'cameraAvatarDataUri': encode_avatar_bytes(png),
                'cameraAvatarPath': '/etc/passwd',
            })
            config = RuntimeConfig.from_environ({
                'COLLEAGUE_RUNTIME_STATE': str(path),
                'COLLEAGUE_CAMERA_ENABLED': '1',
            })
            self.assertFalse(config.camera_enabled)
            self.assertFalse(config.camera_default_on)
            self.assertTrue(config.camera_logo_data_uri.startswith('data:image/png'))
            self.assertNotIn('/etc/passwd', config.camera_logo_data_uri)

    def test_screen_share_cannot_be_enabled_from_env(self):
        config = RuntimeConfig.from_environ({'COLLEAGUE_SCREEN_SHARE': '1'})
        self.assertFalse(config.screen_share_enabled)


if __name__ == '__main__':
    unittest.main()
