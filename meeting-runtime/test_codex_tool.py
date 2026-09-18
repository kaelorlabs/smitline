import asyncio
import json
import os
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
            client = CodexJobClient(
                jobs, timeout=2, session_id='thread-origin-1', continuity='exact',
                workspace='/Users/Taylor/project', authorize_model=True)

            async def worker():
                for _ in range(100):
                    requests = list(jobs.glob('*.request.json'))
                    if requests:
                        request = requests[0]
                        data = json.loads(request.read_text())
                        self.assertEqual(data['task'], 'Review this function')
                        self.assertEqual(data['model'], 'gpt-5.6-sol')
                        self.assertEqual(data['session_id'], 'thread-origin-1')
                        self.assertEqual(data['continuity'], 'exact')
                        self.assertTrue(data['authorize_model'])
                        self.assertNotIn('session_key', data)
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

    async def test_does_not_hash_meeting_url_as_session_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            jobs = Path(directory)
            (jobs / 'heartbeat').write_text(str(time.time()))
            os.environ['MEETING_URL'] = 'https://us05web.zoom.us/j/999'
            client = CodexJobClient(jobs, timeout=2, continuity='context')

            async def worker():
                for _ in range(100):
                    requests = list(jobs.glob('*.request.json'))
                    if requests:
                        data = json.loads(requests[0].read_text())
                        self.assertNotIn('session_key', data)
                        self.assertNotIn('session_id', data)
                        self.assertEqual(data['continuity'], 'context')
                        response = requests[0].with_name(
                            requests[0].name.replace('.request.json', '.response.json'))
                        response.write_text(json.dumps({'text': 'ok', 'continuity': 'context'}))
                        return
                    await asyncio.sleep(0.01)
                self.fail('request was not created')

            worker_task = asyncio.create_task(worker())
            try:
                result = await client.run('Hello', 'gpt-5.6-luna')
            finally:
                os.environ.pop('MEETING_URL', None)
            await worker_task
            self.assertEqual(result['text'], 'ok')

    async def test_cancel_stops_waiting_and_cleans_job(self):
        with tempfile.TemporaryDirectory() as directory:
            jobs = Path(directory)
            (jobs / 'heartbeat').write_text(str(time.time()))
            client = CodexJobClient(jobs, timeout=2, session_id='thread-origin-1', continuity='exact')
            cancel = asyncio.Event()

            async def run():
                return await client.run('Long running review', 'gpt-5.6-terra', cancel=cancel)

            task = asyncio.create_task(run())
            for _ in range(50):
                if list(jobs.glob('*.request.json')):
                    break
                await asyncio.sleep(0.01)
            self.assertTrue(list(jobs.glob('*.request.json')))
            cancel.set()
            result = await task
            self.assertEqual(result, {'error': 'cancelled'})
            self.assertEqual(list(jobs.glob('*.request.json')), [])
            self.assertEqual(list(jobs.glob('*.response.json')), [])
            self.assertEqual(list(jobs.glob('*.cancel')), [])

    async def test_rejects_unknown_model_before_creating_job(self):
        with tempfile.TemporaryDirectory() as directory:
            jobs = Path(directory)
            (jobs / 'heartbeat').write_text(str(time.time()))
            result = await CodexJobClient(jobs, session_id='thread-origin-1').run(
                'Do work', 'made-up-model')
            self.assertIn('error', result)
            self.assertEqual(list(jobs.glob('*.request.json')), [])

    async def test_progress_file_is_forwarded(self):
        with tempfile.TemporaryDirectory() as directory:
            jobs = Path(directory)
            (jobs / 'heartbeat').write_text(str(time.time()))
            seen = []
            client = CodexJobClient(jobs, timeout=2, session_id='thread-origin-1', continuity='exact')

            async def worker():
                for _ in range(100):
                    requests = list(jobs.glob('*.request.json'))
                    if requests:
                        job_id = requests[0].name.replace('.request.json', '')
                        (jobs / f'{job_id}.progress.json').write_text(
                            json.dumps({'message': 'reading worker.lock'}))
                        await asyncio.sleep(0.3)
                        response = requests[0].with_name(job_id + '.response.json')
                        response.write_text(json.dumps({'text': 'done'}))
                        return
                    await asyncio.sleep(0.01)
                self.fail('request was not created')

            worker_task = asyncio.create_task(worker())
            result = await client.run('Inspect', 'gpt-5.6-luna', on_progress=seen.append)
            await worker_task
            self.assertEqual(result['text'], 'done')
            self.assertIn('reading worker.lock', seen)


if __name__ == '__main__':
    unittest.main()
