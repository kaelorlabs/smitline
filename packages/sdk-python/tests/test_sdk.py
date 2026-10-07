import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import smitline
from smitline import (
    Colleague,
    ColleagueError,
    StartupError,
    ValidationError,
)
from smitline.client import LoopbackTransport


ZOOM = 'https://zoom.us/j/123456789'


class LoopbackHttpTests(unittest.TestCase):
    def test_token_rotation(self):
        seen_tokens = []
        auth_values = ['stale-token', 'rotated-token']

        def request(method, path, body=None, token=None):
            seen_tokens.append(token)
            if token != 'rotated-token':
                raise ColleagueError('unauthorized', code='unauthorized', status=401)
            if method == 'POST' and path == '/v1/calls':
                return {'id': 'call-0123456789abcdef', 'channel': body['channel'], 'status': 'queued', 'brief': body}
            raise AssertionError(path)

        def popping_auth():
            if auth_values:
                return auth_values.pop(0)
            return 'rotated-token'

        transport = LoopbackTransport(
            request=request,
            read_auth=popping_auth,
            is_port_open=lambda: True,
            autostart=False,
        )
        call = transport.start_call({'channel': 'meeting', 'to': ZOOM, 'objective': 'Take notes'})
        self.assertEqual(call['id'], 'call-0123456789abcdef')
        self.assertEqual(seen_tokens, ['stale-token', 'rotated-token'])
        self.assertNotIn('rotated-token', json.dumps(call))

    def test_daemon_autostart_uses_lock_and_token_file(self):
        spawns = []
        root = Path(tempfile.mkdtemp())
        (root / '.colleague').mkdir()
        (root / '.colleague' / 'daemon.auth').write_text('host-token\n')
        opened = {'value': False}

        def spawn():
            spawns.append(1)
            opened['value'] = True

        transport = LoopbackTransport(
            root=root,
            port=59999,
            spawn_daemon=spawn,
            is_port_open=lambda: opened['value'],
            startup_timeout_s=1,
        )
        transport._ensure_daemon()
        self.assertEqual(spawns, [1])
        self.assertEqual(transport._token, 'host-token')

    def test_daemon_not_running_is_a_startup_error(self):
        transport = LoopbackTransport(is_port_open=lambda: False, autostart=False)
        with self.assertRaises(StartupError):
            transport.list_voices()


class CallTests(unittest.IsolatedAsyncioTestCase):
    def test_the_sdk_exposes_calls_only(self):
        self.assertNotIn('MeetingHandle', smitline.__all__)
        methods = sorted(name for name in vars(Colleague) if not name.startswith('_'))
        self.assertEqual(methods, [
            'check_call', 'end_call', 'forget_contact', 'get_call', 'get_contact', 'get_do_not_call',
            'get_profile', 'instruct_call', 'list_calls', 'list_contacts', 'list_voices',
            'save_call_note', 'start_call', 'transfer_call', 'update_contact', 'update_do_not_call',
            'update_profile', 'wait_for_call',
        ])

    async def test_contacts_notes_and_list_filters(self):
        seen = []

        def request(method, path, body=None, token=None):
            seen.append((method, path, body))
            if path.startswith('/v1/calls?'):
                return {'calls': []}
            if path == '/v1/contacts':
                return {'contacts': [{'number': '+14155550142'}]}
            return {}

        transport = LoopbackTransport(request=request, read_auth=lambda: 't',
                                      is_port_open=lambda: True, autostart=False)
        client = Colleague(transport=transport)
        await client.list_calls(5, task='roof-quotes', contact='+14155550142')
        await client.save_call_note('call-0123456789abcdef', 'Apex: $14,200.')
        self.assertEqual(len(await client.list_contacts()), 1)
        await client.update_contact('+14155550142', {'autoContext': True})
        await client.forget_contact('+14155550142')
        self.assertEqual([(method, path) for method, path, _ in seen], [
            ('GET', '/v1/calls?limit=5&contact=%2B14155550142&task=roof-quotes'),
            ('POST', '/v1/calls/call-0123456789abcdef/note'),
            ('GET', '/v1/contacts'),
            ('PATCH', '/v1/contacts/%2B14155550142'),
            ('DELETE', '/v1/contacts/%2B14155550142'),
        ])
        self.assertEqual(seen[1][2], {'text': 'Apex: $14,200.'})

    async def test_call_methods_use_the_calls_api(self):
        seen = []

        def request(method, path, body=None, token=None):
            seen.append((method, path, body))
            if path == '/v1/calls':
                return {'id': 'call-0123456789abcdef', 'status': 'queued'}
            if path.startswith('/v1/calls?'):
                return {'calls': [{'id': 'call-0123456789abcdef'}]}
            return {'id': 'call-0123456789abcdef', 'status': 'completed'}

        transport = LoopbackTransport(request=request, read_auth=lambda: 't',
                                      is_port_open=lambda: True, autostart=False)
        client = Colleague(transport=transport)
        brief = {'channel': 'phone', 'to': '+14155550142', 'onBehalfOf': 'Robin',
                 'objective': 'Book a table'}
        self.assertEqual((await client.start_call(brief))['status'], 'queued')
        self.assertEqual((await client.wait_for_call('call-0123456789abcdef', 999))['status'],
                         'completed')
        self.assertEqual(len(await client.list_calls(500)), 1)
        await client.instruct_call('call-0123456789abcdef', 'Ask about parking')
        self.assertEqual(seen[0], ('POST', '/v1/calls', brief))
        self.assertEqual(seen[1][1], '/v1/calls/call-0123456789abcdef/wait?timeout=300')
        self.assertEqual(seen[2][1], '/v1/calls?limit=100')
        self.assertEqual(seen[3], ('POST', '/v1/calls/call-0123456789abcdef/instructions',
                                   {'text': 'Ask about parking'}))
        await client.instruct_call('call-0123456789abcdef', 'He tried it yesterday', silent=True)
        await client.get_profile()
        await client.update_profile({'about': 'Robin builds Smitline.'})
        self.assertEqual(seen[4][2], {'text': 'He tried it yesterday', 'silent': True})
        self.assertEqual(seen[5][:2], ('GET', '/v1/profile'))
        self.assertEqual(seen[6], ('PATCH', '/v1/profile', {'about': 'Robin builds Smitline.'}))
        await client.get_do_not_call()
        await client.update_do_not_call({'remove': ['+14155550142']})
        self.assertEqual(seen[7][:2], ('GET', '/v1/do-not-call'))
        self.assertEqual(seen[8], ('PATCH', '/v1/do-not-call', {'remove': ['+14155550142']}))

    async def test_a_meeting_is_joined_with_start_call(self):
        seen = []

        def request(method, path, body=None, token=None):
            seen.append((method, path, body))
            if path == '/v1/calls':
                return {'id': 'call-0123456789abcdef', 'channel': body['channel'], 'status': 'queued'}
            if path.endswith('/end'):
                return {'id': 'call-0123456789abcdef', 'status': 'canceled'}
            if path.endswith('/transfer'):
                return {'transferred': True}
            if path == '/v1/voices':
                return {'default': 'marin', 'voices': ['marin']}
            return {'id': 'call-0123456789abcdef', 'channel': 'meeting', 'status': 'waiting'}

        client = Colleague(transport=LoopbackTransport(request=request, read_auth=lambda: 't',
                                                       is_port_open=lambda: True, autostart=False))
        brief = {'channel': 'meeting', 'to': ZOOM, 'objective': 'Take notes on the roadmap review'}
        call = await client.start_call(brief)
        self.assertEqual(call['channel'], 'meeting')
        self.assertEqual((await client.get_call(call['id']))['status'], 'waiting')
        self.assertEqual((await client.end_call(call['id']))['status'], 'canceled')
        self.assertEqual(await client.transfer_call(call['id']), {'transferred': True})
        self.assertEqual((await client.list_voices())['default'], 'marin')
        self.assertEqual(seen[0], ('POST', '/v1/calls', brief))

    def test_error_details_are_kept(self):
        from smitline.client import _map_http_error
        error = _map_http_error(422, {'error': {
            'code': 'brief_incomplete', 'message': 'brief is missing objective',
            'missing': [{'field': 'objective', 'question': 'What should the call achieve?'}]}}, 'x')
        self.assertIsInstance(error, ValidationError)
        self.assertEqual(error.details['missing'][0]['field'], 'objective')
        self.assertIsInstance(_map_http_error(503, {'error': {'code': 'daemon_unavailable'}}, 'x'), StartupError)


if __name__ == '__main__':
    unittest.main()
