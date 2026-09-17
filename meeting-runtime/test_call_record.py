import json
import tempfile
import unittest
from pathlib import Path
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

    def test_meeting_id_directory_and_secret_rejection(self):
        with tempfile.TemporaryDirectory() as root:
            archive = CallRecord(root, meeting_id='mtg-archive000001')
            self.assertEqual(archive.directory, Path(root) / 'mtg-archive000001')
            self.assertTrue((archive.directory / 'archive.json').is_file())
            archive.transcript('meeting', 'Ship Friday.', False, start_ms=10, end_ms=40)
            archive.close(usage={'usageSeconds': 12}, end_reason='finished', stage='finished')
            again = CallRecord(root, meeting_id='mtg-archive000001')
            self.assertEqual(again.directory, archive.directory)
            usage = again.read_json('usage.json')
            self.assertEqual(usage['endReason'], 'finished')
            with self.assertRaises(ValueError):
                archive.write_json('bad.json', {'apiKey': 'sk-secret'})
            with self.assertRaises(ValueError):
                archive.event('note', access_token='secret')
            with self.assertRaises(ValueError):
                archive.append('../escape.txt', 'nope')
            with self.assertRaises(ValueError):
                CallRecord(root, meeting_id='../etc')
