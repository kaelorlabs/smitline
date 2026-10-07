"""Contacts, tasks, and the notes of earlier calls a new call starts with."""
import tempfile
import unittest
from pathlib import Path

from aiohttp.test_utils import TestClient, TestServer

from call_brief import CallBrief
from call_service import CallError, MAX_CARRIED, note_line
from call_store import new_call_id
from contacts import ContactBook, ContactsUnreadable
from runtime_daemon import create_app
from test_call_service import ServiceHarness, brief


def finished(h, *, to='+14155550142', day=1, channel='phone', direction='outbound', task=None,
             summary='Quoted $14,200.', details=(), note=None, status='completed', contact=None):
    """A finished call in the store, created on 2026-09-<day> (before the harness's clock)."""
    when = f'2026-09-{day:02d}T15:00:00Z'
    record = {
        'version': 1, 'id': new_call_id(), 'owner': 'local', 'channel': channel,
        'direction': direction, 'status': status,
        'brief': {'channel': channel, 'to': to, 'onBehalfOf': 'Robin', 'objective': 'Get a quote',
                  **({'task': task} if task else {}), **({'contact': contact} if contact else {})},
        'createdAt': when, 'updatedAt': when, 'endedAt': when, 'line': {}, 'usage': {},
        'result': {'outcome': 'achieved', 'summary': summary, 'details': list(details),
                   'decisions': [], 'actionItems': [], 'openQuestions': [], 'transcript': []},
    }
    if note:
        record['carryNote'] = {'text': note, 'by': 'agent', 'at': when}
    h.store.create(record)
    return record['id']


class ContactBookTests(unittest.TestCase):
    def test_saves_clears_and_forgets(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / '.smitline' / 'contacts.json'
            book = ContactBook(path)
            entry = book.update('+1 (415) 555-0142', {'name': ' City  Dental ', 'autoContext': True})
            self.assertEqual((entry['number'], entry['name'], entry['autoContext']),
                             ('+14155550142', 'City Dental', True))
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(book.update('+14155550142', {'notes': 'Ask for Maria.'})['name'], 'City Dental')
            # Clearing everything drops the entry rather than keeping an empty one.
            book.update('+14155550142', {'name': '', 'notes': '', 'autoContext': False})
            self.assertEqual(book.entries(), [])
            book.update('+14155550142', {'name': 'City Dental'})
            self.assertTrue(book.forget('+14155550142'))
            self.assertFalse(book.forget('+14155550142'))

    def test_refuses_bad_input_and_never_overwrites_a_damaged_file(self):
        with tempfile.TemporaryDirectory() as temp:
            book = ContactBook(Path(temp) / 'contacts.json')
            for number, changes in (('555', {'name': 'x'}), ('+14155550142', {'phone': 'x'}),
                                    ('+14155550142', {'autoContext': 'yes'}),
                                    ('+14155550142', {'notes': 'x' * 601}), ('+14155550142', {}),
                                    ('+14155550142', {'name': {'apiKey': 'sk-1'}})):
                with self.subTest(changes=changes), self.assertRaises(ValueError):
                    book.update(number, changes)
            (Path(temp) / 'contacts.json').write_text('{not json')
            with self.assertRaises(ContactsUnreadable):
                book.update('+14155550142', {'name': 'City Dental'})
            self.assertEqual((Path(temp) / 'contacts.json').read_text(), '{not json')


class BriefTests(unittest.TestCase):
    def test_task_and_carry_from(self):
        parsed = CallBrief.from_dict(brief(task={'id': 'roof-quotes-oct', 'title': ' Roof  quotes '},
                                           carryFrom={'task': True, 'contact': False,
                                                      'calls': ['call-0123456789abcdef'] * 2}))
        self.assertEqual(parsed.to_dict()['task'], {'id': 'roof-quotes-oct', 'title': 'Roof quotes'})
        self.assertEqual(parsed.to_dict()['carryFrom'],
                         {'task': True, 'contact': False, 'calls': ['call-0123456789abcdef']})
        self.assertNotIn('carried', parsed.to_dict())
        bad = (
            {'task': {'id': 'Roof Quotes'}}, {'task': {'title': 'x'}}, {'task': {'id': 'a', 'owner': 'x'}},
            {'carryFrom': {'task': True}}, {'carryFrom': {'calls': ['call-1']}},
            {'carryFrom': {'calls': [f'call-{i:016x}' for i in range(11)]}},
            {'carryFrom': {'everything': True}}, {'carryFrom': {'contact': 'yes'}},
            {'channel': 'meeting', 'to': 'https://us05web.zoom.us/j/123456789', 'carryFrom': {'contact': True}},
        )
        for overrides in bad:
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                CallBrief.from_dict(brief(**overrides))

    def test_carried_notes_join_the_context_without_entering_the_stored_brief(self):
        import dataclasses
        from briefing import EARLIER_CALLS_HEADING, backend_background, voice_notes
        from meeting_line import context_from_brief
        parsed = dataclasses.replace(CallBrief.from_dict(brief(context='Indoor is fine.')),
                                     carried=('Apex Roofing (2026-09-01): Quoted $14,200.',))
        session = parsed.session_context
        self.assertEqual(session['earlierCalls'], ['Apex Roofing (2026-09-01): Quoted $14,200.'])
        for text in (voice_notes({}, None, session, 'Robin'), backend_background({}, None, session, 'Robin')):
            self.assertIn(EARLIER_CALLS_HEADING, text)
            self.assertIn('- Apex Roofing (2026-09-01): Quoted $14,200.', text)
        meeting = dataclasses.replace(CallBrief.from_dict(brief(channel='meeting', to='https://us05web.zoom.us/j/123456789')),
                                      carried=('Apex Roofing (2026-09-01): Quoted $14,200.',))
        self.assertIn('Quoted $14,200.', context_from_brief(meeting)['summary'])
        self.assertNotIn('earlierCalls', CallBrief.from_dict(brief()).session_context)


class CarriedContextTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.h = ServiceHarness(self.temp.name)
        self.h.line.proceed.set()

    async def asyncTearDown(self):
        await self.h.service.shutdown()
        self.temp.cleanup()

    async def start(self, **overrides):
        record = await self.h.service.create(brief(to='+14155550199', **overrides))
        return record, self.h.line.contexts

    async def test_a_task_carries_its_earlier_calls_newest_first(self):
        task = {'id': 'roof-quotes'}
        apex = finished(self.h, to='+14155550101', day=1, task=task, contact={'name': 'Apex Roofing'},
                        summary='Quoted $14,200.', details=[{'label': 'Start', 'value': 'Nov 20'}])
        summit = finished(self.h, to='+14155550102', day=2, task=task, note='Summit: $12,900 incl. gutters.')
        finished(self.h, to='+14155550103', day=3, task={'id': 'other-job'})
        finished(self.h, to='+14155550104', day=4, task=task, status='failed', summary='')
        record, _ = await self.start(task=task, carryFrom={'task': True})
        carried = record['carried']
        self.assertEqual([item['callId'] for item in carried], [summit, apex])
        self.assertEqual(carried[0]['by'], 'agent')
        self.assertEqual(carried[1], {'callId': apex, 'contact': 'Apex Roofing', 'at': '2026-09-01T15:00:00Z',
                                      'text': 'Quoted $14,200. Start: Nov 20', 'by': 'result'})
        stored = self.h.store.get(record['id'])
        self.assertEqual(stored['carried'], carried)
        self.assertNotIn('carried', stored['brief'])
        await self.h.service.wait(record['id'], timeout=5)
        ctx = self.h.line.contexts[record['id']]
        self.assertEqual(ctx.brief.session_context['earlierCalls'], [note_line(item) for item in carried])

    async def test_nothing_is_carried_unless_asked_or_switched_on(self):
        finished(self.h, to='+14155550199', day=1, note='Booked the cleaning.')
        record, _ = await self.start()
        self.assertNotIn('carried', record)
        self.h.hooks.contact_book.update('+14155550199', {'autoContext': True})
        record, _ = await self.start()
        self.assertEqual([item['text'] for item in record['carried']], ['Booked the cleaning.'])
        # The brief can still say no for one call.
        record, _ = await self.start(carryFrom={'contact': False})
        self.assertNotIn('carried', record)

    async def test_named_calls_must_exist_and_limits_hold(self):
        with self.assertRaises(CallError) as caught:
            await self.start(carryFrom={'calls': ['call-0123456789abcdef']})
        self.assertEqual(caught.exception.status, 422)
        ids = [finished(self.h, to='+14155550199', day=day, note='x' * 300) for day in range(1, 16)]
        record, _ = await self.start(carryFrom={'contact': True})
        self.assertEqual(len(record['carried']), MAX_CARRIED)
        self.assertEqual(record['carried'][0]['callId'], ids[-1])
        record, _ = await self.start(carryFrom={'calls': [ids[0]]})
        self.assertEqual([item['callId'] for item in record['carried']], [ids[0]])
        check = await self.h.service.check(brief(to='+14155550199', carryFrom={'contact': True}))
        self.assertEqual(check['carried'], MAX_CARRIED)

    async def test_incoming_calls_never_start_with_earlier_calls(self):
        finished(self.h, to='+14155550199', day=1, note='Booked the cleaning.')
        self.h.hooks.contact_book.update('+14155550199', {'autoContext': True})
        record = await self.h.service.create_inbound(CallBrief.from_dict(brief(to='+14155550199')),
                                                     'local', self.h.line.start)
        self.assertNotIn('carried', record)

    async def test_notes_are_saved_on_finished_calls_only(self):
        record, _ = await self.start()
        with self.assertRaises(CallError) as caught:
            self.h.service.save_note(record['id'], 'Too early.')
        self.assertEqual(caught.exception.status, 409)
        await self.h.service.wait(record['id'], timeout=5)
        saved = self.h.service.save_note(record['id'], '  Booked  for 7pm.  ')
        self.assertEqual(saved['carryNote']['text'], 'Booked for 7pm.')
        self.assertEqual(saved['carryNote']['by'], 'agent')
        for text in ('', 'x' * 1201, 42):
            with self.subTest(text=text), self.assertRaises(CallError):
                self.h.service.save_note(record['id'], text)

    async def test_contacts_group_calls_by_number(self):
        finished(self.h, to='+14155550101', day=1, contact={'name': 'Apex Roofing'})
        finished(self.h, to='+14155550101', day=3, direction='inbound', summary='They called back.')
        finished(self.h, to='+14155550102', day=2)
        finished(self.h, to='https://us05web.zoom.us/j/123456789', channel='meeting', day=4)
        self.h.hooks.contact_book.update('+14155550103', {'name': 'Never called', 'notes': 'New plumber.'})
        listed = self.h.service.contacts('local')['contacts']
        self.assertEqual([(item['number'], item['calls']) for item in listed],
                         [('+14155550101', 2), ('+14155550102', 1), ('+14155550103', 0)])
        self.assertEqual(listed[0]['name'], 'Apex Roofing')
        one = self.h.service.contact('local', '+1 415 555 0101')
        self.assertEqual([item['direction'] for item in one['history']], ['inbound', 'outbound'])
        self.assertEqual(one['nextCall'][0]['text'], 'They called back.')
        updated = self.h.service.update_contact('local', '+14155550101', {'name': 'Apex', 'autoContext': True})
        self.assertEqual((updated['name'], updated['autoContext']), ('Apex', True))
        self.assertEqual(self.h.service.forget_contact('local', '+14155550101'), {'forgotten': True})
        self.assertEqual(self.h.service.contact('local', '+14155550101')['name'], 'Apex Roofing')
        with self.assertRaises(CallError) as caught:
            self.h.service.contact('local', '+14155550999')
        self.assertEqual(caught.exception.status, 404)


class ContactApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.h = ServiceHarness(self.temp.name)
        app = create_app(root=Path(self.temp.name) / 'daemon', auth_token='launch-token',
                         call_service=self.h.service)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()
        self.auth = {'Authorization': 'Bearer launch-token'}

    async def asyncTearDown(self):
        await self.client.close()
        self.temp.cleanup()

    async def test_routes(self):
        apex = finished(self.h, to='+14155550101', day=1, task={'id': 'roof-quotes'})
        finished(self.h, to='+14155550102', day=2)
        listed = await (await self.client.get('/v1/calls?task=roof-quotes', headers=self.auth)).json()
        self.assertEqual([call['id'] for call in listed['calls']], [apex])
        listed = await (await self.client.get('/v1/calls?contact=%2B14155550102', headers=self.auth)).json()
        self.assertEqual(len(listed['calls']), 1)
        for query in ('task=Roof%20Quotes', 'contact=555'):
            response = await self.client.get(f'/v1/calls?{query}', headers=self.auth)
            self.assertEqual(response.status, 422, query)
        response = await self.client.post(f'/v1/calls/{apex}/note', json={'text': 'Apex: $14,200.'}, headers=self.auth)
        self.assertEqual((await response.json())['carryNote']['text'], 'Apex: $14,200.')
        response = await self.client.post(f'/v1/calls/{apex}/note', json={'text': 'x', 'by': 'me'}, headers=self.auth)
        self.assertEqual(response.status, 422)
        contacts = await (await self.client.get('/v1/contacts', headers=self.auth)).json()
        self.assertEqual(len(contacts['contacts']), 2)
        response = await self.client.patch('/v1/contacts/%2B14155550101', json={'name': 'Apex Roofing'}, headers=self.auth)
        self.assertEqual((await response.json())['name'], 'Apex Roofing')
        one = await (await self.client.get('/v1/contacts/%2B14155550101', headers=self.auth)).json()
        self.assertEqual(one['nextCall'][0]['text'], 'Apex: $14,200.')
        response = await self.client.delete('/v1/contacts/%2B14155550101', headers=self.auth)
        self.assertEqual(await response.json(), {'forgotten': True})
        self.assertEqual((await self.client.get('/v1/contacts')).status, 401)


if __name__ == '__main__':
    unittest.main()
