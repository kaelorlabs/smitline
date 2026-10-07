import unittest

from runtime_config import RuntimeConfig


class RuntimeConfigTests(unittest.TestCase):
    def test_product_defaults_are_scenario_independent(self):
        config = RuntimeConfig.from_environ({})
        from phone_prompts import DEFAULT_BACKEND_MODEL
        self.assertEqual(config.participant_name, 'Smitline')
        self.assertEqual(config.meeting_instructions, '')
        self.assertTrue(config.camera_enabled)
        self.assertTrue(config.camera_default_on)
        self.assertEqual(config.backend_model, DEFAULT_BACKEND_MODEL)
        self.assertFalse(config.web_search)

    def test_backend_model_and_web_search_come_from_env(self):
        config = RuntimeConfig.from_environ({
            'SMITLINE_MEETING_BACKEND_MODEL': 'gpt-5.6-mini', 'SMITLINE_MEETING_WEB_SEARCH': '1'})
        self.assertEqual(config.backend_model, 'gpt-5.6-mini')
        self.assertTrue(config.web_search)

    def test_accepts_operator_meeting_guidance(self):
        config = RuntimeConfig.from_environ({'SMITLINE_MEETING_INSTRUCTIONS': 'Focus on launch readiness.'})
        self.assertEqual(config.meeting_instructions, 'Focus on launch readiness.')

    def test_invalid_values_fail_at_startup(self):
        for env in ({'SMITLINE_MEETING_BACKEND_MODEL': 'two words'},
                    {'SMITLINE_MEETING_INSTRUCTIONS': 'x' * 2001},
                    {'SMITLINE_PARTICIPANT_NAME': ''}):
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
                'SMITLINE_RUNTIME_STATE': str(path),
                'SMITLINE_CAMERA_ENABLED': '1',
            })
            self.assertFalse(config.camera_enabled)
            self.assertFalse(config.camera_default_on)
            self.assertTrue(config.camera_logo_data_uri.startswith('data:image/png'))
            self.assertNotIn('/etc/passwd', config.camera_logo_data_uri)


if __name__ == '__main__':
    unittest.main()
