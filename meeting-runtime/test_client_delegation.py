import asyncio
import unittest

from client_delegation import ClientDelegation, speakable_result
from delegation_router import DelegationRouter
from providers.base import CodingAgentProvider
from runtime_config import RuntimeConfig
from test_schemas import context_payload, permissions_payload


class FakeProvider(CodingAgentProvider):
    def __init__(self, delay=0, result=None, error=None):
        self.delay = delay
        self.result = result or {'text': 'The worker lock is exclusive per host.'}
        self.error = error
        self.calls = []
        self.started = asyncio.Event()

    async def run(self, request, cancel):
        self.calls.append(request)
        self.started.set()
        deadline = asyncio.get_running_loop().time() + self.delay
        while asyncio.get_running_loop().time() < deadline:
            if cancel is not None and cancel.is_set():
                return {'error': 'cancelled'}
            await asyncio.sleep(0.01)
        if self.error:
            return {'error': self.error}
        return dict(self.result)


class Record:
    def __init__(self):
        self.events = []
        self.directory = '/tmp'

    def event(self, kind, **data):
        self.events.append((kind, data))

    def transcript(self, *args, **kwargs):
        self.event('transcript', args=args, **kwargs)


def created(delegation_id='item_lock_1', offset_ms=900, target='client'):
    return {
        'type': 'session.delegation.created',
        'event_id': 'event_delegation',
        'offset_ms': offset_ms,
        'delegation': {'id': delegation_id, 'type': 'delegation', 'target': target},
    }


class ClientDelegationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.sent = []
        self.record = Record()
        self.state = {'muted': True, 'stage': 'live', 'backend_status': 'idle', 'captions': []}
        self.runtime = RuntimeConfig.from_environ({})
        self.provider = FakeProvider(delay=0.02)
        self.router = DelegationRouter(providers={'codex': self.provider})
        self.session = ClientDelegation(
            send=self._send, record=self.record, state=self.state, runtime=self.runtime,
            meeting_state={
                'context': context_payload(),
                'permissions': permissions_payload(),
                'workspace': '/Users/Taylor/project',
                'provider': 'codex',
                'defaultCodexModel': 'gpt-5.6-terra',
            },
            router=self.router, progress_interval=0.05)

    async def _send(self, payload):
        self.sent.append(payload)

    def _kinds(self):
        return [item['type'] for item in self.sent]

    async def test_progress_and_commentary_use_original_delegation_id(self):
        self.session.note_transcript({
            'type': 'session.input_transcript.delta',
            'delta': 'What does the worker lock do?',
            'start_ms': 100, 'end_ms': 900, 'event_id': 'tr-1',
        })
        task = self.session.submit(created())
        await task
        self.assertEqual(self.provider.calls[0].request_text, 'What does the worker lock do?')
        self.assertIn('Ship the developer platform', str(self.provider.calls[0].handoff.to_dict()
                                                         if hasattr(self.provider.calls[0].handoff, 'to_dict')
                                                         else self.provider.calls[0].handoff))
        self.assertTrue(all(item.get('delegation_id') == 'item_lock_1'
                            for item in self.sent if 'append' in item.get('type', '')))
        self.assertIn('session.thinking.append', self._kinds())
        self.assertIn('session.commentary.append', self._kinds())
        commentary = [item for item in self.sent if item['type'] == 'session.commentary.append']
        self.assertIn('exclusive', commentary[0]['content'])
        self.assertEqual([kind for kind, _ in self.record.events].count('delegation.started'), 1)
        self.assertEqual([kind for kind, _ in self.record.events].count('delegation.completed'), 1)

    async def test_request_text_is_only_the_triggering_utterance(self):
        self.session.note_transcript({
            'type': 'session.input_transcript.delta', 'delta': 'First we should talk about billing.',
            'start_ms': 50, 'end_ms': 400, 'event_id': 'old-1',
        })
        self.session.note_transcript({
            'type': 'session.output_transcript.delta', 'delta': 'Okay.',
            'start_ms': 450, 'end_ms': 700, 'event_id': 'old-a',
        })
        self.session.note_transcript({
            'type': 'session.input_transcript.delta', 'delta': 'What about invoices?',
            'start_ms': 800, 'end_ms': 1100, 'event_id': 'old-2',
        })
        self.session.note_transcript({
            'type': 'session.output_transcript.delta', 'delta': 'Go on.',
            'start_ms': 1200, 'end_ms': 1400, 'event_id': 'old-b',
        })
        self.session.note_transcript({
            'type': 'session.input_transcript.delta', 'delta': 'What does ',
            'start_ms': 2000, 'end_ms': 2200, 'event_id': 'cur-a',
        })
        self.session.note_transcript({
            'type': 'session.input_transcript.delta', 'delta': 'the worker lock do?',
            'start_ms': 2250, 'end_ms': 2600, 'event_id': 'cur-b',
        })
        await self.session.submit(created('item_latest', offset_ms=2600))
        request = self.provider.calls[0]
        self.assertEqual(request.request_text, 'What does the worker lock do?')
        self.assertNotIn('billing', request.request_text)
        self.assertIn('billing', request.transcript)
        self.assertIn('invoices', request.transcript)

    async def test_no_task_text_is_recoverable_and_does_not_invent_work(self):
        await self.session.submit(created('item_empty', offset_ms=10))
        commentary = [item for item in self.sent if item['type'] == 'session.commentary.append']
        self.assertTrue(commentary)
        self.assertIn('repeat', commentary[0]['content'].lower())
        self.assertEqual(self.provider.calls, [])

    async def test_unknown_events_and_non_client_targets_are_ignored(self):
        self.assertIsNone(self.session.note_transcript({'type': 'session.future.unknown'}))
        self.assertIsNone(self.session.submit(created(target='responses')))
        self.assertEqual(self.provider.calls, [])
        self.assertEqual(self.sent, [])

    async def test_duplicate_delegation_is_exactly_once(self):
        self.session.note_transcript({
            'type': 'session.input_transcript.delta', 'delta': 'Inspect the lock.',
            'start_ms': 1, 'end_ms': 2,
        })
        first = self.session.submit(created('item_dup'))
        second = self.session.submit(created('item_dup'))
        self.assertIsNone(second)
        await first
        self.assertEqual(len(self.provider.calls), 1)

    async def test_failure_sends_recoverable_commentary(self):
        self.provider.error = 'Codex worker is not connected'
        self.session.note_transcript({
            'type': 'session.input_transcript.delta', 'delta': 'Inspect the queue.',
            'start_ms': 1, 'end_ms': 2,
        })
        await self.session.submit(created('item_fail'))
        commentary = [item['content'] for item in self.sent if item['type'] == 'session.commentary.append']
        self.assertTrue(any('could not complete' in text.lower() for text in commentary))

    async def test_close_cancels_and_drops_late_results(self):
        self.provider.delay = 0.4
        self.session.note_transcript({
            'type': 'session.input_transcript.delta', 'delta': 'Take your time.',
            'start_ms': 1, 'end_ms': 2,
        })
        task = self.session.submit(created('item_late'))
        await self.provider.started.wait()
        await self.session.close()
        await asyncio.gather(task, return_exceptions=True)
        await asyncio.sleep(0.05)
        self.assertFalse(any(item['type'] == 'session.commentary.append' for item in self.sent))

    async def test_speakable_result_clips_and_maps_errors(self):
        self.assertIn('stopped', speakable_result({'error': 'cancelled'}).lower())
        self.assertLessEqual(len(speakable_result({'text': 'word ' * 5000}).split()), 600)


class CodexProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_job_payload_omits_control_secrets_and_uses_spoken_request(self):
        from providers.codex import CodexProvider
        from providers.base import ProviderRequest
        from test_schemas import context_payload, permissions_payload

        captured = {}

        class MemoryClient:
            async def run(self, task, model, cancel=None):
                captured['task'] = task
                captured['model'] = model
                return {'text': 'The lock is exclusive.', 'model': model}

        provider = CodexProvider(client=MemoryClient(), context_search=lambda query: {
            'results': [{'source': 'Plan.md', 'passage': 'Launch is October 4.'}]
        })
        result = await provider.run(ProviderRequest(
            delegation_id='item_secret',
            request_text='What does the worker lock do?',
            handoff=context_payload(),
            transcript='What does the worker lock do?',
            model='gpt-5.6-terra',
            permissions=permissions_payload(network='disabled'),
            workspace='/Users/Taylor/project',
            provider='codex',
        ), None)
        self.assertEqual(result['text'], 'The lock is exclusive.')
        task = captured['task']
        self.assertIn('What does the worker lock do?', task)
        self.assertIn('Launch is October 4', task)
        lowered = task.lower()
        self.assertNotIn('bearer', lowered)
        self.assertNotIn('lease', lowered)
        self.assertNotIn('openai', lowered)
        self.assertNotIn('daemon.auth', lowered)
        self.assertNotIn('tavily', lowered)


if __name__ == '__main__':
    unittest.main()
