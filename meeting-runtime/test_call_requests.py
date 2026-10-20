"""Requests: the voice asks the agent that started the call; the agent answers with status and done."""
import asyncio
import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import call_requests
from api_tokens import ApiTokenStore
from call_brief import CallBrief
from call_requests import (
    CHAT_POINTER, MAX_STATUSES, RequestDesk, appends, done_note, late_note, status_note,
)
from call_service import CallError
from call_store import CallNotFound
from meeting_control import MeetingControl, add_routes
from runtime_daemon import create_app
from test_call_service import FakeLine, ServiceHarness, brief
from test_phone import FakeLive, PhoneHarness, until

CAN_DO = 'Look things up in the repo and edit the docs.'
README = 'Add the launch date to the README'


class NoteLine(FakeLine):
    """A phone line that keeps the notes the voice is given."""

    def __init__(self):
        super().__init__()
        self.notes = []
        self.deliverable = True

    async def deliver(self, call_id, note):
        self.notes.append(note)
        return self.deliverable


def ask(request_id='r1', text=README, **extra):
    return dict({'type': 'ask', 'id': request_id, 'kind': 'task', 'text': text,
                 'askedBy': 'Priya', 'context': ['meeting: Can Robin add the launch date?']}, **extra)


# The voice side ------------------------------------------------------------------------------

class RequestDeskTests(unittest.TestCase):
    def test_asks_are_numbered_and_carry_who_asked_and_what_was_said(self):
        desk = RequestDesk('Robin', limit=2)
        message, output = desk.handle({'action': 'ask', 'requestId': '', 'kind': 'task',
                                       'text': '  Add the   launch date ', 'askedBy': ''},
                                      asked_by='Priya', context=[f'line {n}' for n in range(9)])
        self.assertEqual(message, {'type': 'ask', 'id': 'r1', 'kind': 'task', 'text': 'Add the launch date',
                                   'askedBy': 'Priya', 'context': [f'line {n}' for n in range(3, 9)],
                                   'seq': 0})
        self.assertEqual(output['requestId'], 'r1')
        self.assertEqual(output['status'], 'passed_on')
        self.assertIn('will let them know', output['say'])
        # The backend's name for who asked wins over whoever the meeting shows speaking.
        message, _ = desk.handle({'action': 'ask', 'kind': 'question', 'text': 'Status of PR 80?',
                                  'askedBy': 'Sam'}, asked_by='Priya')
        self.assertEqual((message['id'], message['askedBy'], message['seq']), ('r2', 'Sam', 1))
        self.assertNotIn('context', message)
        # Over the limit, or empty: refused, and nothing goes to the agent.
        message, output = desk.handle({'action': 'ask', 'text': 'One more'})
        self.assertIsNone(message)
        self.assertEqual(output['status'], 'refused')
        self.assertIn('Robin will follow up', output['say'])
        self.assertEqual(desk.handle({'action': 'ask', 'text': '   '})[1]['status'], 'refused')
        self.assertEqual(len(desk.messages), 2)

    def test_nobody_following_is_said_straight_away(self):
        desk = RequestDesk('Robin', limit=5)
        message, output = desk.handle({'action': 'ask', 'text': 'Look up the invoice'}, following=False)
        self.assertEqual(message['id'], 'r1')
        self.assertEqual(output['status'], 'noted')
        self.assertIn('not following the call right now', output['say'])
        self.assertEqual(message['kind'], 'question')  # the default

    def test_add_and_cancel_need_an_open_request(self):
        desk = RequestDesk('Robin', limit=5)
        desk.handle({'action': 'ask', 'text': 'Add the date'})
        message, output = desk.handle({'action': 'add', 'requestId': 'r1', 'text': 'November 3'})
        self.assertEqual(message, {'type': 'add', 'id': 'r1', 'text': 'November 3', 'seq': 1})
        self.assertEqual(output['status'], 'added')
        self.assertEqual(desk.handle({'action': 'add', 'requestId': 'r1', 'text': ''})[1]['status'], 'refused')
        for unknown in ('r2', 'r0', 'x1', '', None):
            message, output = desk.handle({'action': 'add', 'requestId': unknown, 'text': 'more'})
            self.assertIsNone(message)
            self.assertEqual(output['status'], 'refused')
        message, output = desk.handle({'action': 'cancel', 'requestId': 'r1', 'text': ''})
        self.assertEqual(message, {'type': 'cancel', 'id': 'r1', 'seq': 2})
        self.assertEqual(output['status'], 'canceled')
        self.assertFalse(desk.is_open('r1'))
        self.assertIsNone(desk.handle({'action': 'cancel', 'requestId': 'r1'})[0])
        # A request the agent finished is closed the same way.
        desk.handle({'action': 'ask', 'text': 'Another'})
        desk.close('r2')
        self.assertIsNone(desk.handle({'action': 'add', 'requestId': 'r2', 'text': 'x'})[0])

    def test_tool_is_strict_and_complete(self):
        tool = call_requests.ask_agent_tool('Robin')
        self.assertTrue(tool['strict'])
        self.assertEqual(sorted(tool['parameters']['required']),
                         sorted(tool['parameters']['properties']))
        self.assertIn("Robin's agent", tool['description'])


class ContextTests(unittest.TestCase):
    def test_caption_pieces_become_lines(self):
        pieces = [{'speaker': 'meeting', 'text': ' Okay'}, {'speaker': 'meeting', 'text': ', so what'},
                  {'speaker': 'meeting', 'text': ' changed?'}, {'speaker': 'agent', 'text': ' Checking.'},
                  {'speaker': 'meeting', 'text': '  '}, 'junk']
        self.assertEqual(call_requests.context_lines(pieces),
                         ['meeting: Okay, so what changed?', 'agent: Checking.'])
        many = [{'speaker': 'meeting' if n % 2 else 'agent', 'text': f'line {n}'} for n in range(10)]
        self.assertEqual(call_requests.context_lines(many)[0], 'agent: line 4')
        self.assertEqual(call_requests.context_lines(None), [])


class NoteTests(unittest.TestCase):
    request = {'id': 'r1', 'text': README, 'askedBy': 'Priya'}

    def test_a_status_is_quiet_unless_announced(self):
        quiet = status_note(self.request, 'Picked up.', owner='Robin', announce=False)
        self.assertEqual(quiet['kind'], 'thinking')
        self.assertIn('Do not announce this', quiet['text'])
        said = status_note(self.request, 'Nov 3 or Nov 10?', owner='Robin', announce=True)
        self.assertEqual(said['kind'], 'instructions')
        self.assertIn('Tell Priya', said['text'])
        self.assertIn('never over someone speaking', said['text'])
        self.assertNotIn('closes', said)

    def test_done_closes_and_says_whether_it_worked(self):
        done = done_note(self.request, 'Added it, PR 81.', owner='Robin', ok=True)
        self.assertEqual((done['kind'], done['closes']), ('instructions', True))
        self.assertIn('Request r1 is done', done['text'])
        failed = done_note({'id': 'r2', 'text': 'Deploy'}, 'No access.', owner='Robin', ok=False)
        self.assertIn('could not complete request r2', failed['text'])
        self.assertIn('the person who asked', failed['text'])
        late = late_note(self.request, owner='Robin')
        self.assertEqual(late['kind'], 'thinking')
        self.assertIn('not been picked up', late['text'])

    def test_details_go_to_the_chat_or_to_the_voice(self):
        note = dict(done_note(self.request, 'Added it.', owner='Robin', ok=True), details='https://x/pr/81')
        posted = appends(note, chat_posted=True)
        self.assertEqual(len(posted), 1)
        self.assertTrue(posted[0][1].endswith(CHAT_POINTER))
        spoken = appends(note)
        self.assertEqual([kind for kind, _ in spoken], ['thinking', 'instructions'])
        self.assertIn('https://x/pr/81', spoken[0][1])
        self.assertIn('only if someone asks', spoken[0][1])


class BriefTests(unittest.TestCase):
    def test_can_do_is_optional_and_tidied(self):
        self.assertIsNone(CallBrief.from_dict(brief()).can_do)
        parsed = CallBrief.from_dict(brief(canDo='  Look things up\n in the repo  '))
        self.assertEqual(parsed.can_do, 'Look things up in the repo')
        self.assertEqual(parsed.to_dict()['canDo'], 'Look things up in the repo')
        with self.assertRaises(ValueError):
            CallBrief.from_dict(brief(canDo='x' * 501))
        with self.assertRaises(ValueError):
            CallBrief.from_dict(brief(canDo=['a list']))


# The call service --------------------------------------------------------------------------

class ServiceRequestTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.line = NoteLine()
        self.h = ServiceHarness(self.temp.name, lines={'phone': self.line})
        self.service = self.h.service
        self.now = [1000.0]
        self.service._clock = lambda: self.now[0]

    async def asyncTearDown(self):
        await self.service.shutdown()
        self.temp.cleanup()

    async def start(self, **extra):
        record = await self.service.create(brief(**dict({'canDo': CAN_DO}, **extra)))
        await until(lambda: record['id'] in self.line.contexts
                    and self.h.store.get(record['id'])['status'] == 'in_progress')
        return record['id'], self.line.contexts[record['id']]

    async def follow(self, call_id):
        """The agent checks in on the call (a live wait), so it counts as following."""
        await self.service.wait(call_id, owner='local', timeout=0, since='')

    async def test_a_request_wakes_the_agent_and_its_thread_runs_to_done(self):
        call_id, ctx = await self.start()
        loop = asyncio.get_running_loop()
        waiting = asyncio.create_task(self.service.wait(call_id, owner='local', timeout=30, since=''))
        await until(lambda: self.service.agent_following(call_id))
        started = loop.time()
        ctx.request(ask())
        answer = await asyncio.wait_for(waiting, 2)
        # At once: a request does not wait for the conversation to settle.
        self.assertLess(loop.time() - started, 1.0)
        self.assertEqual([(r['type'], r['id'], r['askedBy']) for r in answer['live']['requests']],
                         [('ask', 'r1', 'Priya')])
        self.assertEqual(answer['live']['requests'][0]['context'], ['meeting: Can Robin add the launch date?'])
        self.assertTrue(answer['live']['transcript'])
        request = answer['requests'][0]
        self.assertEqual((request['state'], request['kind'], request['text']), ('open', 'task', README))

        result = await self.service.reply(call_id, 'r1', owner='local', type='status', text='Picked up.')
        self.assertTrue(result['delivered'])
        self.assertEqual(result['request']['state'], 'working')
        self.assertEqual(self.line.notes[-1]['kind'], 'thinking')
        await self.service.reply(call_id, 'r1', owner='local', type='status',
                                 text='Nov 3 or Nov 10?', announce=True)
        self.assertEqual(self.line.notes[-1]['kind'], 'instructions')
        self.assertIn('Tell Priya', self.line.notes[-1]['text'])

        # The answer to the agent's question comes back on the same request.
        ctx.request({'type': 'add', 'id': 'r1', 'text': 'Nov 3'})
        result = await self.service.reply(call_id, 'r1', owner='local', type='done',
                                          text='Added "Launch: Nov 3" to the README.',
                                          details='https://github.com/example/pull/81')
        note = self.line.notes[-1]
        self.assertTrue(note['closes'])
        self.assertEqual(note['details'], 'https://github.com/example/pull/81')
        self.assertIn('for Priya', note['chat'])
        request = result['request']
        self.assertEqual((request['state'], request['ok'], request['answer']),
                         ('done', True, 'Added "Launch: Nov 3" to the README.'))
        self.assertEqual([(e['from'], e['type']) for e in request['thread']],
                         [('call', 'ask'), ('agent', 'status'), ('agent', 'status'),
                          ('call', 'add'), ('agent', 'done')])
        self.assertTrue(all(e.get('delivered') for e in request['thread'] if e['from'] == 'agent'))
        self.assertTrue(request['thread'][2]['announce'])
        with self.assertRaises(CallError) as caught:
            await self.service.reply(call_id, 'r1', owner='local', type='done', text='Again.')
        self.assertEqual((caught.exception.status, caught.exception.code), (409, 'request_closed'))
        events = [e['type'] for e in self.service.events(call_id)]
        self.assertEqual(events.count('call.request'), 2)
        self.assertEqual(events.count('call.request_reply'), 3)

    async def test_requests_only_skips_the_transcript(self):
        call_id, ctx = await self.start()
        first = await self.service.wait(call_id, owner='local', timeout=0, since='', only='requests')
        self.assertNotIn('transcript', first['live'])
        cursor = first['live']['cursor']
        loop = asyncio.get_running_loop()
        started = loop.time()
        waiting = asyncio.create_task(self.service.wait(call_id, owner='local', timeout=0.4,
                                                        since=cursor, only='requests'))
        await asyncio.sleep(0.05)
        ctx.add_transcript('other', 'Just chatting about the weekend.')
        quiet = await waiting
        self.assertGreaterEqual(loop.time() - started, 0.35)  # what was said did not wake it
        self.assertEqual(quiet['live']['requests'], [])
        waiting = asyncio.create_task(self.service.wait(call_id, owner='local', timeout=5,
                                                        since=quiet['live']['cursor'], only='requests'))
        await asyncio.sleep(0.05)
        ctx.request(ask())
        woke = await asyncio.wait_for(waiting, 1)
        self.assertEqual([r['id'] for r in woke['live']['requests']], ['r1'])

    async def test_following_lasts_a_while_after_the_last_contact(self):
        call_id, ctx = await self.start()
        self.assertFalse(self.service.agent_following(call_id))
        self.assertFalse(ctx.agent_following())
        await self.service.wait(call_id, owner='local', timeout=0)  # not live: not following
        self.assertFalse(self.service.agent_following(call_id))
        await self.follow(call_id)
        self.assertTrue(ctx.agent_following())
        self.now[0] += 89
        self.assertTrue(self.service.agent_following(call_id))
        self.now[0] += 2
        self.assertFalse(self.service.agent_following(call_id))

    async def test_a_request_nobody_picks_up_is_flagged_to_the_voice(self):
        self.service.pickup_seconds = 0.05
        call_id, ctx = await self.start()
        await self.follow(call_id)
        ctx.request(ask())
        ctx.request(ask('r2', 'What is the PR number?'))
        await self.service.reply(call_id, 'r2', owner='local', type='status', text='Picked up.')
        await until(lambda: any('not been picked up' in note['text'] for note in self.line.notes))
        await asyncio.sleep(0.1)
        late = [note for note in self.line.notes if 'not been picked up' in note['text']]
        self.assertEqual([note['request'] for note in late], ['r1'])
        self.assertEqual([e['data'] for e in self.service.events(call_id) if e['type'] == 'call.request_late'],
                         [{'id': 'r1'}])

    async def test_no_follower_no_pickup_check(self):
        self.service.pickup_seconds = 0.01
        call_id, ctx = await self.start()
        ctx.request(ask())
        await asyncio.sleep(0.1)
        self.assertEqual(self.line.notes, [])

    async def test_open_requests_become_follow_ups_and_done_still_counts_after_the_call(self):
        call_id, ctx = await self.start()
        ctx.request(ask())
        ctx.request(ask('r2', 'Fix the typo', askedBy=None, context=None))
        ctx.request({'type': 'cancel', 'id': 'r2'})
        self.line.proceed.set()
        done = await self.service.wait(call_id, owner='local', timeout=5)
        self.assertEqual(done['status'], 'completed')
        self.assertEqual(done['result']['openRequests'],
                         [{'id': 'r1', 'kind': 'task', 'text': README, 'askedBy': 'Priya'}])
        with self.assertRaises(CallError) as caught:
            await self.service.reply(call_id, 'r1', owner='local', type='status', text='Working.')
        self.assertEqual(caught.exception.code, 'call_ended')
        result = await self.service.reply(call_id, 'r1', owner='local', type='done', text='Added it.')
        self.assertFalse(result['delivered'])
        self.assertEqual(result['request']['state'], 'done')
        self.assertEqual(self.line.notes, [])
        with self.assertRaises(CallError) as caught:
            await self.service.reply(call_id, 'r2', owner='local', type='done', text='Fixed.')
        self.assertEqual(caught.exception.code, 'request_closed')

    async def test_the_phone_line_context_is_the_call_s_last_lines(self):
        call_id, ctx = await self.start()
        ctx.request(ask(context=None))
        request = self.h.store.get(call_id)['requests'][0]
        self.assertEqual(request['context'][-1], 'other: Sure, 7pm is booked.')

    async def test_what_does_not_fit_is_ignored(self):
        call_id, ctx = await self.start()
        self.assertIsNone(ctx.request(ask('x1')))
        self.assertIsNone(ctx.request(ask(text='  ')))
        self.assertIsNotNone(ctx.request(ask()))
        self.assertIsNone(ctx.request(ask()))  # the same id twice
        self.assertIsNone(ctx.request({'type': 'add', 'id': 'r1', 'text': ''}))
        self.assertIsNone(ctx.request({'type': 'add', 'id': 'r9', 'text': 'more'}))
        self.assertIsNone(ctx.request({'type': 'shout', 'id': 'r1', 'text': 'hey'}))
        self.assertIsNone(ctx.request('r1'))
        self.assertEqual(len(self.h.store.get(call_id)['requests']), 1)
        with self.assertRaises(CallError) as caught:
            await self.service.reply(call_id, 'r7', owner='local', type='status', text='Hi.')
        self.assertEqual((caught.exception.status, caught.exception.code), (404, 'request_not_found'))
        for bad in ({'type': 'later', 'text': 'x'}, {'type': 'status', 'text': '  '}):
            with self.assertRaises(CallError) as caught:
                await self.service.reply(call_id, 'r1', owner='local', **bad)
            self.assertEqual(caught.exception.status, 422)
        with self.assertRaises(CallNotFound):
            await self.service.reply(call_id, 'r1', owner='someone-else', type='status', text='Mine.')

    async def test_requests_are_off_without_can_do(self):
        call_id, ctx = await self.start(canDo=None)
        self.assertIsNone(ctx.request(ask()))
        self.assertNotIn('requests', self.h.store.get(call_id))

    async def test_status_updates_are_capped(self):
        call_id, ctx = await self.start()
        ctx.request(ask())
        for n in range(MAX_STATUSES):
            await self.service.reply(call_id, 'r1', owner='local', type='status', text=f'Step {n}.')
        with self.assertRaises(CallError) as caught:
            await self.service.reply(call_id, 'r1', owner='local', type='status', text='One more.')
        self.assertEqual((caught.exception.status, caught.exception.code), (429, 'too_many_updates'))
        await self.service.reply(call_id, 'r1', owner='local', type='done', text='Done.')

    async def test_a_line_that_cannot_deliver_says_so(self):
        call_id, ctx = await self.start()
        ctx.request(ask())
        self.line.deliverable = False
        result = await self.service.reply(call_id, 'r1', owner='local', type='status', text='Picked up.')
        self.assertFalse(result['delivered'])
        self.assertFalse(result['request']['thread'][-1]['delivered'])

    async def test_lines_without_deliver_fall_back_to_instructions(self):
        line = FakeLine()
        line.instructions = []

        async def instruct(call_id, text, *, silent=False):
            line.instructions.append((text, silent))
            return True
        line.instruct = instruct
        self.service.lines['phone'] = line
        record = await self.service.create(brief(canDo=CAN_DO))
        await until(lambda: record['id'] in line.contexts)
        line.contexts[record['id']].request(ask())
        await self.service.reply(record['id'], 'r1', owner='local', type='status', text='Picked up.')
        self.assertEqual(line.instructions[-1][1], True)
        line.proceed.set()


# The REST API --------------------------------------------------------------------------------

class RequestApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.line = NoteLine()
        self.h = ServiceHarness(self.temp.name, lines={'phone': self.line})
        app = create_app(root=Path(self.temp.name) / 'daemon', auth_token='launch-token',
                         call_service=self.h.service,
                         api_tokens=ApiTokenStore(Path(self.temp.name) / 'api-tokens.json'))
        self.client = TestClient(TestServer(app))
        await self.client.start_server()
        self.auth = {'Authorization': 'Bearer launch-token'}

    async def asyncTearDown(self):
        await self.client.close()
        await self.h.service.shutdown()
        self.temp.cleanup()

    async def post(self, path, body):
        response = await self.client.post(path, json=body, headers=self.auth)
        return response.status, await response.json()

    async def test_reply_and_wait_for_requests(self):
        status, call = await self.post('/v1/calls', brief(canDo=CAN_DO))
        self.assertEqual(status, 201)
        self.assertEqual(call['brief']['canDo'], CAN_DO)
        await until(lambda: call['id'] in self.line.contexts
                    and self.h.store.get(call['id'])['status'] == 'in_progress')
        response = await self.client.get(f'/v1/calls/{call["id"]}/wait?timeout=0&only=everything',
                                         headers=self.auth)
        self.assertEqual(response.status, 422)
        # only=requests follows live from the start without a cursor.
        response = await self.client.get(f'/v1/calls/{call["id"]}/wait?timeout=0&only=requests',
                                         headers=self.auth)
        live = (await response.json())['live']
        self.assertEqual((live['requests'], 'transcript' in live), ([], False))
        self.line.contexts[call['id']].request(ask())
        base = f'/v1/calls/{call["id"]}/requests/r1/reply'
        for bad in ({'type': 'status'}, {'type': 'maybe', 'text': 'x'},
                    {'type': 'status', 'text': 'x', 'ok': True},
                    {'type': 'done', 'text': 'x', 'announce': True},
                    {'type': 'status', 'text': 'x', 'announce': 'yes'},
                    {'type': 'done', 'text': 'x', 'details': 5},
                    {'type': 'done', 'text': 'x', 'extra': 1}, ['done']):
            status, body = await self.post(base, bad)
            self.assertEqual(status, 422, bad)
        status, body = await self.post(base, {'type': 'status', 'text': 'Picked up.', 'announce': False})
        self.assertEqual((status, body['delivered'], body['request']['state']), (200, True, 'working'))
        status, body = await self.post(base, {'type': 'done', 'text': 'Done.', 'ok': False})
        self.assertEqual((status, body['request']['ok']), (200, False))
        status, body = await self.post(base, {'type': 'done', 'text': 'Done.'})
        self.assertEqual((status, body['error']['code']), (409, 'request_closed'))
        status, body = await self.post(f'/v1/calls/{call["id"]}/requests/r5/reply',
                                       {'type': 'done', 'text': 'Done.'})
        self.assertEqual((status, body['error']['code']), (404, 'request_not_found'))
        self.line.proceed.set()


# Phone calls ---------------------------------------------------------------------------------

def function_call(name, arguments, call_id='fc-1'):
    return {'type': 'response.event', 'delegation_id': 'dlg-1',
            'event': {'type': 'response.output_item.done',
                      'item': {'type': 'function_call', 'call_id': call_id, 'name': name,
                               'arguments': json.dumps(arguments)}}}


class PhoneRequestTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        FakeLive.instances = []
        self.temp = tempfile.TemporaryDirectory()
        self.h = PhoneHarness(self.temp.name)

    async def asyncTearDown(self):
        await self.h.service.shutdown()
        self.temp.cleanup()

    async def test_the_voice_asks_and_hears_back(self):
        record, session = await self.h.dial(canDo=CAN_DO, contact={'name': 'Sam Rivera'})
        ws, live, task = await self.h.connect(record, session)
        responses = live.config['delegation']['responses']
        self.assertEqual([tool.get('name') for tool in responses['tools']], ['end_call', 'ask_agent'])
        self.assertIn('ask_agent', responses['instructions'])
        self.assertIn(CAN_DO, live.config['instructions'])
        self.assertIn("Robin's agent", live.config['instructions'])
        live.push({'type': 'session.started', 'session': {'id': 'sess_1'}})
        # Nobody follows the call yet: the voice says so at once.
        live.push(function_call('ask_agent', {'action': 'ask', 'requestId': '', 'kind': 'question',
                                              'text': 'When does the order ship?', 'askedBy': ''}))
        await until(lambda: live.outputs)
        call_id, output = live.outputs[0]
        self.assertEqual(call_id, 'fc-1')
        self.assertEqual((json.loads(output)['requestId'], json.loads(output)['status']), ('r1', 'noted'))
        stored = self.h.store.get(record['id'])['requests'][0]
        self.assertEqual((stored['id'], stored['askedBy'], stored['kind']), ('r1', 'Sam', 'question'))
        # The agent follows now; the next request is passed on.
        await self.h.service.wait(record['id'], owner='local', timeout=0, since='')
        live.push(function_call('ask_agent', {'action': 'ask', 'requestId': '', 'kind': 'task',
                                              'text': 'Email the receipt', 'askedBy': 'Sam'},
                                call_id='fc-2'))
        await until(lambda: len(live.outputs) == 2)
        self.assertEqual(json.loads(live.outputs[1][1])['status'], 'passed_on')
        result = await self.h.service.reply(record['id'], 'r1', owner='local', type='done',
                                            text='It ships Friday.', details='Tracking: 1Z999')
        self.assertTrue(result['delivered'])
        kinds = [kind for kind, _ in live.appends[-2:]]
        self.assertEqual(kinds, ['session.thinking.append', 'session.instructions.append'])
        self.assertIn('Tracking: 1Z999', live.appends[-2][1])
        self.assertIn('It ships Friday.', live.appends[-1][1])
        self.assertFalse(session.desk.is_open('r1'))
        self.assertTrue(session.desk.is_open('r2'))
        await ws.close()
        await asyncio.wait_for(task, 3)

    async def test_no_can_do_no_tool(self):
        record, session = await self.h.dial()
        ws, live, task = await self.h.connect(record, session)
        responses = live.config['delegation']['responses']
        self.assertEqual([tool.get('name') for tool in responses['tools']], ['end_call'])
        self.assertNotIn('ask_agent', responses['instructions'])
        self.assertIsNone(session.desk)
        live.push({'type': 'session.started', 'session': {'id': 'sess_1'}})
        live.push(function_call('ask_agent', {'action': 'ask', 'text': 'Anything'}))
        await asyncio.sleep(0.1)
        self.assertEqual(live.outputs, [])
        await ws.close()
        await asyncio.wait_for(task, 3)


# Meetings: inside the container ------------------------------------------------------------

class ChatAdapter:
    def __init__(self, fail=False):
        self.sent = []
        self.fail = fail

    async def send_chat_message(self, text):
        if self.fail:
            raise RuntimeError('chat is closed')
        self.sent.append(text)


class MeetingControlTests(unittest.IsolatedAsyncioTestCase):
    def control(self, **state):
        control = MeetingControl('tok-1', dict(state), owner='Robin', can_do=CAN_DO)
        self.sent = []

        async def send(kind, text):
            self.sent.append((kind, text))
            return True
        control.send = send
        return control

    def test_only_the_token_opens_it(self):
        control = MeetingControl('tok-1', {}, owner='Robin')
        self.assertTrue(control.authorized('Bearer tok-1'))
        for header in (None, '', 'tok-1', 'Bearer tok-2', 'Basic tok-1', 'Bearer '):
            self.assertFalse(control.authorized(header), header)
        # No token (a meeting started by hand): nothing gets in.
        self.assertFalse(MeetingControl('', {}, owner='Robin').authorized('Bearer '))
        self.assertIsNone(MeetingControl('', {}, owner='Robin').desk)

    async def test_notes_reach_the_voice_and_details_the_chat(self):
        control = self.control(chatAvailable=True)
        control.adapter = ChatAdapter()
        request = {'id': 'r1', 'text': README, 'askedBy': 'Priya'}
        note = dict(done_note(request, 'Added it.', owner='Robin', ok=True), type='note',
                    details='https://x/pr/81', chat='From Robin\'s agent:\nhttps://x/pr/81')
        status, body = await control.handle(note)
        self.assertEqual((status, body), (200, {'delivered': True, 'chat': True}))
        self.assertEqual(control.adapter.sent, ["From Robin's agent:\nhttps://x/pr/81"])
        self.assertEqual(len(self.sent), 1)
        self.assertTrue(self.sent[0][1].endswith(CHAT_POINTER))
        # No chat: the voice gets the details to read out if asked.
        for state, adapter in (({'chatAvailable': False}, ChatAdapter()),
                               ({'chatAvailable': True}, ChatAdapter(fail=True))):
            control = self.control(**state)
            control.adapter = adapter
            status, body = await control.handle(note)
            self.assertEqual(body, {'delivered': True, 'chat': False})
            self.assertEqual([kind for kind, _ in self.sent], ['thinking', 'instructions'])

    async def test_done_closes_the_request_on_the_voice_side(self):
        control = self.control()
        output = control.ask({'action': 'ask', 'text': README})
        self.assertEqual((output['requestId'], output['status']), ('r1', 'noted'))
        self.assertEqual(control.state['requests'][0]['seq'], 0)
        await control.handle({'type': 'following', 'value': True})
        self.assertTrue(control.following)
        self.assertEqual(control.ask({'action': 'ask', 'text': 'Another'})['status'], 'passed_on')
        await control.handle(dict(done_note({'id': 'r1', 'text': README}, 'Done.', owner='Robin',
                                            ok=True), type='note'))
        self.assertFalse(control.desk.is_open('r1'))
        self.assertTrue(control.desk.is_open('r2'))
        await control.handle({'type': 'following', 'value': 'yes'})
        self.assertFalse(control.following)

    async def test_what_it_refuses(self):
        control = self.control()
        for payload in (None, [], {'type': 'shout'}, {'type': 'note', 'kind': 'speak', 'text': 'x'},
                        {'type': 'note', 'kind': 'thinking', 'text': '  '}):
            status, _ = await control.handle(payload)
            self.assertEqual(status, 422, payload)
        control.send = None
        status, _ = await control.handle({'type': 'note', 'kind': 'thinking', 'text': 'Hi'})
        self.assertEqual(status, 409)

    async def test_the_routes_want_the_token(self):
        from listening import AudioTap
        control = self.control()
        tap = AudioTap('pcm16', 24000)
        app = web.Application()
        add_routes(app, control, tap)
        async with TestClient(TestServer(app)) as client:
            note = {'type': 'note', 'kind': 'instructions', 'text': 'Say hello to Priya.'}
            for headers in ({}, {'Authorization': 'Bearer nope'}):
                self.assertEqual((await client.post('/control', json=note, headers=headers)).status, 403)
                self.assertEqual((await client.get('/listen', headers=headers)).status, 403)
            response = await client.post('/control', json=note, headers={'Authorization': 'Bearer tok-1'})
            self.assertEqual((response.status, await response.json()), (200, {'delivered': True, 'chat': False}))
            self.assertEqual(self.sent, [('instructions', 'Say hello to Priya.')])
            response = await client.post('/control', data=b'not json',
                                         headers={'Authorization': 'Bearer tok-1'})
            self.assertEqual(response.status, 422)
            listen = await client.get('/listen', headers={'Authorization': 'Bearer tok-1'})
            self.assertEqual(json.loads(await listen.content.readline())['type'], 'format')
            tap.close()


class BridgeRequestTests(unittest.IsolatedAsyncioTestCase):
    def config(self, meeting_state, **env):
        import bridge
        from runtime_config import RuntimeConfig
        runtime = RuntimeConfig.from_environ(dict({'SMITLINE_OWNER_NAME': 'Robin'}, **env))
        return bridge.build_session_config(runtime, meeting_state)

    def test_the_session_has_the_tool_only_with_can_do(self):
        with_requests = self.config({'canDo': CAN_DO})
        responses = with_requests['delegation']['responses']
        self.assertEqual([tool['name'] for tool in responses['tools']], ['ask_agent'])
        self.assertIn('ask_agent', responses['instructions'])
        self.assertIn(CAN_DO, with_requests['instructions'])
        self.assertIn("Robin's agent, the AI that set up this meeting", with_requests['instructions'])
        both = self.config({'canDo': CAN_DO}, SMITLINE_MEETING_WEB_SEARCH='1')
        self.assertEqual([tool.get('name', tool['type']) for tool in both['delegation']['responses']['tools']],
                         ['ask_agent', 'web_search'])
        without = self.config({})
        self.assertNotIn('tools', without['delegation']['responses'])
        self.assertNotIn('Requests:', without['instructions'])

    async def test_a_request_goes_out_on_health_and_a_note_comes_back_in(self):
        import bridge
        from runtime_config import RuntimeConfig
        from test_participation import Adapter, FakeLiveSocket, IdleSpeaker, Mic, RecordingArchive
        socket = FakeLiveSocket(bridge)
        state = copy.deepcopy(bridge.state)
        control = MeetingControl('tok-1', state, owner='Robin', can_do=CAN_DO)
        with patch.object(bridge, 'ClientSession', socket.client), \
                patch.object(bridge, 'record', RecordingArchive()), \
                patch.object(bridge, 'state', state), \
                patch.object(bridge, 'control', control), \
                patch.object(bridge, 'stop', asyncio.Event()):
            task = asyncio.create_task(bridge.run_voice(
                IdleSpeaker(), Mic(), None, 'test-only',
                RuntimeConfig.from_environ({'SMITLINE_MEETING_INTRO': '0'}), Adapter(),
                {'canDo': CAN_DO}))
            try:
                await until(lambda: any(e['type'] == 'session.start' for e in socket.sent))
                status, _ = await control.handle({'type': 'note', 'kind': 'thinking', 'text': 'Early'})
                self.assertEqual(status, 200)  # the session exists, but is not live yet
                self.assertFalse(any(e['type'] == 'session.thinking.append' for e in socket.sent))
                await socket.events.put({'type': 'session.started'})
                await socket.events.put({'type': 'session.input_transcript.delta',
                                         'delta': 'Can Robin add the launch date?',
                                         'start_ms': 0, 'end_ms': 900, 'event_id': 'tr-1'})
                await socket.events.put(function_call('ask_agent', {
                    'action': 'ask', 'requestId': '', 'kind': 'task', 'text': README,
                    'askedBy': 'Priya'}))
                await until(lambda: any(e['type'] == 'response.item.create' for e in socket.sent))
                item = next(e for e in socket.sent if e['type'] == 'response.item.create')
                more = next(e for e in socket.sent if e['type'] == 'response.create')
                # GPT-Live refuses a delegation_id on either event (it ended the first live test).
                self.assertNotIn('delegation_id', item)
                self.assertNotIn('delegation_id', more)
                self.assertEqual(item['item']['call_id'], 'fc-1')
                self.assertEqual(json.loads(item['item']['output'])['requestId'], 'r1')
                self.assertEqual(state['requests'][0]['id'], 'r1')
                self.assertEqual(state['requests'][0]['askedBy'], 'Priya')
                self.assertEqual(state['requests'][0]['context'], ['meeting: Can Robin add the launch date?'])
                # An error after start-up refuses one event; the meeting goes on.
                await socket.events.put({'type': 'error', 'error': {'code': 'invalid_event',
                                                                    'message': 'Unknown field.'}})
                await until(lambda: state.get('api_error') == 'invalid_event')
                self.assertEqual(state['stage'], 'live')
                status, body = await control.handle({'type': 'note', 'kind': 'instructions',
                                                     'text': 'Tell Priya it is done.'})
                self.assertEqual(body['delivered'], True)
                sent = [e for e in socket.sent if e['type'] == 'session.instructions.append']
                self.assertEqual(sent[-1]['content'], 'Tell Priya it is done.')
                await socket.events.put({'type': 'session.closed', 'usage': {'seconds': 1}})
                await asyncio.wait_for(task, 2)
                self.assertIsNone(control.send)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)


# Meetings: the daemon side ------------------------------------------------------------------

class MeetingSessionTests(unittest.TestCase):
    def test_can_do_travels_from_brief_to_runtime_state(self):
        from agent_sessions import MeetingSession
        from meeting_line import meeting_payload
        from runtime_state import state_from_session
        from test_schemas import meeting_session_payload
        payload = meeting_payload(CallBrief.from_dict(brief(
            channel='meeting', to='https://us05web.zoom.us/j/123456789?pwd=abc', canDo=CAN_DO)))
        self.assertEqual(payload['canDo'], CAN_DO)
        session = MeetingSession.from_dict(meeting_session_payload(canDo=CAN_DO))
        self.assertEqual(session.to_dict()['canDo'], CAN_DO)
        self.assertEqual(state_from_session(session)['canDo'], CAN_DO)
        self.assertNotIn('canDo', state_from_session(MeetingSession.from_dict(meeting_session_payload())))
        with self.assertRaises(ValueError):
            MeetingSession.from_dict(meeting_session_payload(canDo='x' * 501))

    def test_the_daemon_keeps_requests_by_seq(self):
        from runtime_daemon import RuntimeDaemon
        daemon = RuntimeDaemon.__new__(RuntimeDaemon)
        import threading
        daemon._lock_guard = threading.RLock()
        daemon._captions, daemon._requests = {}, {}
        daemon.note_requests('mtg-1', [{'seq': 0, 'type': 'ask', 'id': 'r1'},
                                       {'seq': 1, 'type': 'add', 'id': 'r1'}, 'junk', {'seq': True}])
        daemon.note_requests('mtg-1', [{'seq': 1, 'type': 'add', 'id': 'r1'},
                                       {'seq': 2, 'type': 'cancel', 'id': 'r1'}])
        daemon.note_requests('mtg-1', 'not a list')
        self.assertEqual([m['seq'] for m in daemon.requests_after('mtg-1', -1)], [0, 1, 2])
        self.assertEqual([m['seq'] for m in daemon.requests_after('mtg-1', 1)], [2])
        daemon.forget_captions('mtg-1')
        self.assertEqual(daemon.requests_after('mtg-1', -1), [])


class SupervisorControlTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from meeting_supervisor import ProductionMeetingSupervisor
        from test_meeting_supervisor import FakeDaemon, FakeHealth, FakeLauncher
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        (root / 'meeting-runtime').mkdir()
        self.launcher = FakeLauncher()
        self.supervisor = ProductionMeetingSupervisor(
            root, runtime_root=root / 'meeting-runtime', launcher=self.launcher,
            health=FakeHealth(), poll_interval=0.02, start_timeout=0.4, stop_timeout=0.4,
        ).bind_daemon(FakeDaemon())

    async def asyncTearDown(self):
        await self.supervisor.shutdown()
        self.temporary.cleanup()

    async def test_each_meeting_gets_its_own_token(self):
        from runtime_state import meeting_state_path, read_json
        from test_meeting_supervisor import session
        first = session()
        await self.supervisor.start(first)
        token = self.launcher.ups[0]['SMITLINE_CONTROL_TOKEN']
        self.assertGreaterEqual(len(token), 40)
        self.assertEqual(self.supervisor.control_headers(first.id), {'Authorization': 'Bearer ' + token})
        # Kept out of the runtime state, which the browser side can read.
        self.assertNotIn(token, json.dumps(read_json(meeting_state_path(self.supervisor.runtime_root, first.id))))
        await self.supervisor.cancel(first.id)
        self.assertIsNone(self.supervisor.control_headers(first.id))
        second = session(id='mtg-second0000001')
        await self.supervisor.start(second)
        self.assertNotEqual(self.launcher.ups[1]['SMITLINE_CONTROL_TOKEN'], token)
        await self.supervisor.cancel(second.id)

    async def test_control_posts_to_the_running_container(self):
        got = []

        async def control(request):
            got.append((request.headers.get('Authorization'), await request.json()))
            if (await request.json()).get('type') == 'boom':
                return web.json_response({'error': 'no'}, status=409)
            return web.json_response({'delivered': True})
        app = web.Application()
        app.router.add_post('/control', control)
        server = TestServer(app)
        await server.start_server()
        try:
            self.supervisor.health = SimpleNamespace(url=str(server.make_url('/health')))
            self.supervisor._active_id = 'mtg-a'
            self.supervisor._control_token = 'tok-a'
            self.assertEqual(await self.supervisor.control('mtg-a', {'type': 'note'}), {'delivered': True})
            self.assertEqual(got, [('Bearer tok-a', {'type': 'note'})])
            self.assertIsNone(await self.supervisor.control('mtg-a', {'type': 'boom'}))
            self.assertIsNone(await self.supervisor.control('mtg-b', {'type': 'note'}))
            self.supervisor._control_token = None  # adopted after a restart
            self.assertIsNone(await self.supervisor.control('mtg-a', {'type': 'note'}))
        finally:
            self.supervisor._active_id = None
            await server.close()
        # A container that is gone: no answer, no error.
        self.supervisor.health = SimpleNamespace(url='http://127.0.0.1:9/health')
        self.supervisor._active_id = 'mtg-a'
        self.supervisor._control_token = 'tok-a'
        self.assertIsNone(await self.supervisor.control('mtg-a', {'type': 'note'}))
        self.supervisor._active_id = None

    async def test_health_brings_the_requests_to_the_daemon(self):
        got = []
        self.supervisor.daemon.note_requests = lambda meeting_id, items: got.append((meeting_id, items))
        from test_meeting_supervisor import session
        await self.supervisor._apply_health(session(), {'stage': 'live', 'requests': [{'seq': 0}]})
        self.assertEqual(got, [(session().id, [{'seq': 0}])])


class MeetingLineRequestTests(unittest.IsolatedAsyncioTestCase):
    async def test_requests_flow_both_ways_through_the_meeting(self):
        from meeting_line import MeetingLine
        controls = []
        messages = [{'seq': 0, 'type': 'ask', 'id': 'r1', 'kind': 'task', 'text': README},
                    {'seq': 1, 'type': 'add', 'id': 'r1', 'text': 'Nov 3'}]
        states = iter(['joining', 'live', 'live', 'live', 'ended'])
        current = [None]

        async def control(meeting_id, message):
            controls.append((current[0], message))
            return {'delivered': True}

        async def get_meeting(meeting_id, owner):
            current[0] = next(states)
            return SimpleNamespace(id=meeting_id, state=current[0], platform='zoom')

        async def get_handoff(meeting_id, owner):
            return {}

        daemon = SimpleNamespace(
            supervisor=SimpleNamespace(control=control),
            requests_after=lambda mid, seq: [dict(m) for m in messages if m['seq'] > seq],
            get_meeting=get_meeting, get_handoff=get_handoff)
        line = MeetingLine(daemon, poll_interval=0, sleep=lambda _s: asyncio.sleep(0))
        line._meetings['call-1'] = 'mtg-1'
        got = []
        following = iter([False, False, True, True, True])
        ctx = SimpleNamespace(
            call_id='call-1', owner='local', event=lambda *a, **k: None,
            brief=SimpleNamespace(can_do=CAN_DO, max_minutes=60),
            request=got.append, agent_following=lambda: next(following),
            set_status=lambda *a, **k: None, add_transcript=lambda *a: None)
        finished = []

        async def finish(reason, **kwargs):
            finished.append(reason)
        ctx.finish = finish
        await line._follow(ctx, 'mtg-1')
        self.assertEqual(got, [{'type': 'ask', 'id': 'r1', 'kind': 'task', 'text': README},
                               {'type': 'add', 'id': 'r1', 'text': 'Nov 3'}])
        # Told once while live that nobody follows, then once that the agent does; never while
        # it is still joining.
        self.assertEqual(controls, [('live', {'type': 'following', 'value': False}),
                                    ('live', {'type': 'following', 'value': True})])
        self.assertEqual(finished, ['meeting_ended'])

    async def test_notes_and_instructions_go_into_the_meeting(self):
        from meeting_line import MeetingLine
        controls = []

        async def control(meeting_id, message):
            controls.append((meeting_id, message))
            return {'delivered': message.get('kind') != 'commentary'}
        line = MeetingLine(SimpleNamespace(supervisor=SimpleNamespace(control=control)))
        self.assertFalse(await line.deliver('call-unknown', {'kind': 'thinking', 'text': 'x'}))
        self.assertFalse(await line.instruct('call-unknown', 'x'))
        line._meetings['call-1'] = 'mtg-1'
        self.assertTrue(await line.instruct('call-1', 'Mention the budget.'))
        self.assertTrue(await line.instruct('call-1', 'The CFO joined.', silent=True))
        self.assertFalse(await line.deliver('call-1', {'kind': 'commentary', 'text': 'Hi'}))
        self.assertEqual(controls, [
            ('mtg-1', {'kind': 'instructions', 'text': 'Mention the budget.', 'type': 'note'}),
            ('mtg-1', {'kind': 'thinking', 'text': 'The CFO joined.', 'type': 'note'}),
            ('mtg-1', {'kind': 'commentary', 'text': 'Hi', 'type': 'note'})])
        # A supervisor without control (or one that fails) delivers nothing.
        self.assertFalse(await MeetingLine(SimpleNamespace(supervisor=None)).deliver('call-1', {}))

        async def broken(meeting_id, message):
            raise RuntimeError('down')
        line.daemon.supervisor.control = broken
        self.assertFalse(await line.instruct('call-1', 'x'))


if __name__ == '__main__':
    unittest.main()
