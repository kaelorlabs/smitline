import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from context_tool import context_available, search_context


class ContextToolTests(unittest.TestCase):
    def test_returns_ranked_passages_with_source_names(self):
        with tempfile.TemporaryDirectory() as directory:
            index = Path(directory) / 'index.json'
            index.write_text(json.dumps({'version': 1, 'sources': [
                {'name': 'Launch plan.md', 'kind': 'text', 'text': 'Launch is October 4. The owner is Maya.'},
                {'name': 'Support notes.txt', 'kind': 'text', 'text': 'The support rotation changes in November.'},
            ]}))
            self.assertTrue(context_available(index))
            result = asyncio.run(search_context('When is the launch?', index))
            self.assertEqual(result['results'][0]['source'], 'Launch plan.md')
            self.assertIn('October 4', result['results'][0]['passage'])

    def test_missing_context_and_invalid_queries_fail_cleanly(self):
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / 'missing.json'
            self.assertFalse(context_available(missing))
            self.assertIn('error', asyncio.run(search_context('launch', missing)))
            self.assertIn('error', asyncio.run(search_context('', missing)))


if __name__ == '__main__':
    unittest.main()
