import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path

from codex_tool import CodexJobClient


class CodexJobClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_round_trip_preserves_selected_model_and_cleans_job(self):
        with tempfile.TemporaryDirectory() as directory:
            jobs = Path(directory)
            (jobs / 'heartbeat').write_text(str(time.time()))
            client = CodexJobClient(jobs, timeout=2, session_key='a' * 24)

            async def worker():
                for _ in range(100):
                    requests = list(jobs.glob('*.request.json'))
                    if requests:
                        request = requests[0]
                        data = json.loads(request.read_text())
                        self.assertEqual(data, {'task': 'Review this function', 'model': 'gpt-5.6-sol',
                                                'session_key': 'a' * 24})
                        response = request.with_name(request.name.replace('.request.json', '.response.json'))
                        response.write_text(json.dumps({'model': data['model'], 'text': 'Looks good'}))
                        return
                    await asyncio.sleep(0.01)
                self.fail('request was not created')

            worker_task = asyncio.create_task(worker())
            result = await client.run('Review this function', 'gpt-5.6-sol')
            await worker_task
            self.assertEqual(result, {'model': 'gpt-5.6-sol', 'text': 'Looks good'})
            self.assertEqual(list(jobs.glob('*.request.json')), [])
            self.assertEqual(list(jobs.glob('*.response.json')), [])

    async def test_rejects_unknown_model_before_creating_job(self):
        with tempfile.TemporaryDirectory() as directory:
            jobs = Path(directory)
            (jobs / 'heartbeat').write_text(str(time.time()))
            result = await CodexJobClient(jobs, session_key='b' * 24).run('Do work', 'made-up-model')
            self.assertIn('error', result)
            self.assertEqual(list(jobs.glob('*.request.json')), [])


if __name__ == '__main__':
    unittest.main()
