import asyncio
import json
import unittest
from search_tool import LocalToolDispatcher

class DispatchTests(unittest.IsolatedAsyncioTestCase):
    async def test_outputs_before_continuation_and_duplicate_suppression(self):
        sent, queries = [], []
        state = {'web_search': {'started': 0, 'completed': 0}}
        async def send(event): sent.append(event)
        async def search(query):
            queries.append(query)
            return {'results': [{'title': 'Source', 'url': 'https://example.com', 'snippet': 'Evidence'}]}
        tool = LocalToolDispatcher(send, state, search)
        async def event(kind, **data):
            await tool.handle({'delegation_id': 'd1', 'event': {'type': kind, **data}})
        await event('response.created', response={'id': 'r1'})
        for call_id in ['c1', 'c2', 'c1']:
            await event('response.output_item.done', item={'type': 'function_call', 'call_id': call_id, 'name': 'search_web', 'arguments': json.dumps({'query': call_id})})
        self.assertEqual(sent, [])
        await event('response.completed', response={'id': 'r1', 'output': []})
        await asyncio.gather(*tool.tasks)
        self.assertEqual(queries, ['c1', 'c2'])
        self.assertEqual([e['type'] for e in sent], ['response.item.create', 'response.item.create', 'response.create'])
        self.assertEqual([e['item']['call_id'] for e in sent[:2]], ['c1', 'c2'])
        await tool.close()

    async def test_failure_returns_result_and_continues(self):
        sent = []
        async def send(event): sent.append(event)
        async def broken(query): raise TimeoutError()
        state = {'web_search': {'started': 0, 'completed': 0}}
        tool = LocalToolDispatcher(send, state, broken)
        await tool.execute([{'name':'search_web','call_id':'c3','arguments':'{"query":"test"}'}])
        self.assertIn('error', json.loads(sent[0]['item']['output']))
        self.assertEqual(sent[1]['type'], 'response.create')
        await tool.close()

if __name__ == '__main__': unittest.main()
