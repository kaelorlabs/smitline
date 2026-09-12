import tempfile
import unittest
from pathlib import Path

from codex_worker import run_job


class CodexWorkerTests(unittest.TestCase):
    def test_first_turn_starts_thread_and_later_turn_resumes_with_new_model(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executable = root / 'fake-codex'
            executable.write_text(
                '#!/usr/bin/env python3\n'
                'import json, pathlib, sys\n'
                'args = sys.argv\n'
                'result = pathlib.Path(args[args.index("-o") + 1])\n'
                'model = args[args.index("--model") + 1]\n'
                'result.write_text(model + "|" + str("resume" in args))\n'
                'print(json.dumps({"type":"thread.started","thread_id":"thread-123"}))\n'
            )
            executable.chmod(0o700)

            first = run_job(str(executable), {'task': 'Remember cobalt heron', 'model': 'gpt-5.6-luna'},
                            root / 'first.txt')
            second = run_job(str(executable), {'task': 'Recall it', 'model': 'gpt-5.6-sol'},
                             root / 'second.txt', first['session_id'])

            self.assertEqual(first['text'], 'gpt-5.6-luna|False')
            self.assertFalse(first['session_reused'])
            self.assertEqual(second['text'], 'gpt-5.6-sol|True')
            self.assertTrue(second['session_reused'])
            self.assertEqual(second['session_id'], 'thread-123')


if __name__ == '__main__':
    unittest.main()
