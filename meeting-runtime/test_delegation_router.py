import asyncio
import unittest

from delegation_router import DelegationRouter
from providers.base import CodingAgentProvider, ProviderRequest


class FakeProvider(CodingAgentProvider):
    def __init__(self, delay=0, result=None, error=None):
        self.delay = delay
        self.result = result or {'text': 'The lock is exclusive.'}
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
        return self.result


def request(delegation_id='item_1', text='What does the worker lock do?', **overrides):
    payload = dict(
        delegation_id=delegation_id, request_text=text, session_status='live',
        provider='codex', workspace='/Users/Taylor/project')
    payload.update(overrides)
    return ProviderRequest(**payload)


class DelegationRouterTests(unittest.IsolatedAsyncioTestCase):
    async def test_one_turn_at_a_time_and_idempotent_retry(self):
        events = []
        first = FakeProvider(delay=0.05, result={'text': 'one'})
        router = DelegationRouter(providers={'codex': first}, event_sink=lambda *a, **k: events.append((a[0], k)))
        cancel = asyncio.Event()
        early = asyncio.create_task(router.execute(request('item_a'), cancel))
        await first.started.wait()
        second_provider_calls_before = len(first.calls)
        queued = asyncio.create_task(router.execute(request('item_b', text='And the heartbeat?'), cancel))
        await asyncio.sleep(0.02)
        self.assertEqual(len(first.calls), second_provider_calls_before)
        first_result, second_result = await asyncio.gather(early, queued)
        self.assertEqual(first_result['text'], 'one')
        self.assertEqual(second_result['text'], 'one')
        self.assertEqual([item.delegation_id for item in first.calls], ['item_a', 'item_b'])
        again = await router.execute(request('item_a'), cancel)
        self.assertIs(again, first_result)
        self.assertEqual(len(first.calls), 2)
        types = [item[0] for item in events]
        self.assertEqual(types.count('delegation.started'), 2)
        self.assertEqual(types.count('delegation.completed'), 2)

    async def test_cancel_skips_provider_work(self):
        events = []
        provider = FakeProvider(delay=1)
        router = DelegationRouter(providers={'codex': provider},
                                  event_sink=lambda kind, **payload: events.append(kind))
        cancel = asyncio.Event()
        task = asyncio.create_task(router.execute(request('item_c'), cancel))
        await provider.started.wait()
        cancel.set()
        result = await task
        self.assertEqual(result, {'error': 'cancelled'})
        self.assertIn('delegation.cancelled', events)

    async def test_inactive_meeting_and_unknown_provider(self):
        provider = FakeProvider()
        router = DelegationRouter(providers={'codex': provider})
        inactive = await router.execute(request('item_inactive', session_status='ended'))
        self.assertIn('error', inactive)
        self.assertEqual(provider.calls, [])
        missing = await router.execute(request('item_cursor', provider='cursor'))
        self.assertIn('unsupported coding agent provider', missing['error'])


if __name__ == '__main__':
    unittest.main()
