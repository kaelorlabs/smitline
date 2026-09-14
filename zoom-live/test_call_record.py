import json
import tempfile
import unittest
from call_record import CallRecord


class CallRecordTests(unittest.TestCase):
    def test_transcript_is_readable_before_shutdown_and_runs_do_not_overwrite(self):
        with tempfile.TemporaryDirectory() as root:
            first = CallRecord(root)
            first.transcript('meeting', 'What is the ', True)
            first.transcript('meeting', 'release risk?', True)
            first.transcript('agent', 'The main risk is the untested migration.', True)
            second = CallRecord(root)
            self.assertNotEqual(first.directory, second.directory)
            text = (first.directory / 'transcript.txt').read_text()
            self.assertIn('What is the release risk?', text)
            self.assertIn('generated, playback not guaranteed', text)
            events = [json.loads(line) for line in (first.directory / 'events.jsonl').read_text().splitlines()]
            self.assertEqual(len(events), 3)
            self.assertTrue(events[-1]['muted'])
            self.assertEqual((first.directory / 'events.jsonl').stat().st_mode & 0o777, 0o600)
