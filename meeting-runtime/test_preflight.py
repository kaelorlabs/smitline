import tempfile
import unittest
from pathlib import Path

from preflight import validate


class PreflightTests(unittest.TestCase):
    def configured_root(self, directory):
        root = Path(directory)
        (root / '.env').write_text('OPENAI_API_KEY=secret\nTAVILY_API_KEY=search-secret\n')
        (root / '.env.meeting').write_text(
            'MEETING_URL=https://us05web.zoom.us/j/123456789\n'
            'COLLEAGUE_MEETING_INSTRUCTIONS=Focus on release readiness.\n'
        )
        return root

    def test_valid_configuration_returns_runtime_without_secrets(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = validate(self.configured_root(directory))
            self.assertEqual(runtime.meeting_instructions, 'Focus on release readiness.')
            self.assertFalse(hasattr(runtime, 'openai_api_key'))

    def test_reports_all_actionable_configuration_errors(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / '.env').write_text('OPENAI_API_KEY=replace_with_your_project_api_key\n')
            (root / '.env.meeting').write_text(
                'MEETING_URL=http://example.com/room\n'
                f'COLLEAGUE_MEETING_INSTRUCTIONS={"x" * 2001}\n'
            )
            with self.assertRaises(ValueError) as caught:
                validate(root)
            message = str(caught.exception)
            self.assertIn('OPENAI_API_KEY', message)
            self.assertIn('TAVILY_API_KEY', message)
            self.assertIn('HTTPS Zoom, Teams, or Google Meet', message)
            self.assertIn('COLLEAGUE_MEETING_INSTRUCTIONS', message)
            self.assertNotIn('replace_with_your_project_api_key', message)

    def test_tavily_is_optional_when_web_search_is_disabled(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.configured_root(directory)
            (root / '.env').write_text('OPENAI_API_KEY=secret\n')
            with (root / '.env.meeting').open('a') as stream:
                stream.write('COLLEAGUE_ENABLE_WEB_SEARCH=0\n')
            self.assertFalse(validate(root).web_search_enabled)


if __name__ == '__main__':
    unittest.main()
