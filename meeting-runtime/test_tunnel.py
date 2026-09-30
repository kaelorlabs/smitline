import asyncio
import unittest
from unittest import mock

from aiohttp import web

import tunnel
from tunnel import answers, ask_dns, dns_answers, public_addresses, reachable

REAL_SLEEP = asyncio.sleep


class PublicDnsTests(unittest.IsolatedAsyncioTestCase):
    def test_dns_answers_keeps_ipv4_records_only(self):
        data = {'Answer': [
            {'name': 'x.trycloudflare.com', 'type': 5, 'data': 'alias.example.'},
            {'name': 'x.trycloudflare.com', 'type': 1, 'data': '104.16.230.132'},
            {'type': 1, 'data': 7},
            'junk',
            {'type': 1, 'data': '104.16.231.132'},
        ]}
        self.assertEqual(dns_answers(data), ['104.16.230.132', '104.16.231.132'])
        self.assertEqual(dns_answers({'Status': 3}), [])
        self.assertEqual(dns_answers(None), [])

    async def test_public_addresses_takes_the_first_provider_that_knows_the_name(self):
        asked = []

        async def ask(endpoint, host):
            asked.append((endpoint, host))
            return ['104.16.231.132'] if 'google' in endpoint else []

        self.assertEqual(await public_addresses('x.trycloudflare.com', ask), ['104.16.231.132'])
        self.assertEqual(asked, [('https://1.1.1.1/dns-query', 'x.trycloudflare.com'),
                                 ('https://dns.google/resolve', 'x.trycloudflare.com')])

        async def nobody(endpoint, host):
            return []

        self.assertEqual(await public_addresses('x.trycloudflare.com', nobody), [])

    async def test_ask_dns_reads_json_answers_and_tolerates_failures(self):
        seen = []

        async def dns_query(request):
            seen.append((dict(request.query), request.headers.get('Accept'), request.headers.get('Connection')))
            if request.query['name'] == 'down.example':
                return web.Response(status=503)
            if request.query['name'] == 'garbled.example':
                return web.Response(text='not json')
            return web.json_response({'Answer': [{'type': 1, 'data': '104.16.230.132'}]})

        app = web.Application()
        app.router.add_get('/dns-query', dns_query)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, '127.0.0.1', 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        try:
            endpoint = f'http://127.0.0.1:{port}/dns-query'
            self.assertEqual(await ask_dns(endpoint, 'x.trycloudflare.com'), ['104.16.230.132'])
            self.assertEqual(seen[0][0], {'name': 'x.trycloudflare.com', 'type': 'A'})
            self.assertEqual(seen[0][1], 'application/dns-json')
            self.assertEqual(seen[0][2], 'close')
            self.assertEqual(await ask_dns(endpoint, 'down.example'), [])
            self.assertEqual(await ask_dns(endpoint, 'garbled.example'), [])
        finally:
            await runner.cleanup()
        self.assertEqual(await ask_dns(f'http://127.0.0.1:{port}/dns-query', 'x'), [])


class AnswersTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        app = web.Application()
        app.router.add_get('/healthz', lambda request: web.Response(text=request.host))
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, '127.0.0.1', 0)
        await site.start()
        self.port = site._server.sockets[0].getsockname()[1]

    async def asyncTearDown(self):
        await self.runner.cleanup()

    async def test_pinned_addresses_reach_a_name_this_computer_cannot_resolve(self):
        url = f'http://tunnel.invalid:{self.port}/healthz'
        self.assertEqual(await answers(url), 'ClientConnectorDNSError')
        self.assertIsNone(await answers(url, ['127.0.0.1']))
        self.assertIsNone(await answers(f'http://127.0.0.1:{self.port}/healthz'))
        self.assertEqual(await answers(f'http://127.0.0.1:{self.port}/missing'), 'HTTP 404')


class ReachableTests(unittest.IsolatedAsyncioTestCase):
    async def test_public_dns_finds_a_tunnel_this_computers_dns_does_not_know_yet(self):
        rounds = {'public': 0}

        async def fake_public(host):
            self.assertEqual(host, 'x.trycloudflare.com')
            rounds['public'] += 1
            return ['104.16.230.132'] if rounds['public'] >= 2 else []

        async def fake_answers(url, addresses=None):
            return None if addresses == ['104.16.230.132'] else 'ClientConnectorDNSError'

        with mock.patch.object(tunnel, 'public_addresses', fake_public), \
                mock.patch.object(tunnel, 'answers', fake_answers), \
                mock.patch.object(tunnel.asyncio, 'sleep', lambda seconds: REAL_SLEEP(0)):
            self.assertTrue(await reachable('https://x.trycloudflare.com/healthz', 30))
        self.assertEqual(rounds['public'], 2)

    async def test_this_computers_dns_still_counts_and_nothing_means_unreachable(self):
        async def no_public(host):
            return []

        async def direct_only(url, addresses=None):
            return None if addresses is None else 'HTTP 530'

        with mock.patch.object(tunnel, 'public_addresses', no_public), \
                mock.patch.object(tunnel, 'answers', direct_only):
            self.assertTrue(await reachable('https://x.trycloudflare.com/healthz', 30))

        async def never(url, addresses=None):
            return 'HTTP 530' if addresses else 'ClientConnectorDNSError'

        async def one_public(host):
            return ['104.16.230.132']

        problems = []
        with mock.patch.object(tunnel, 'public_addresses', no_public), \
                mock.patch.object(tunnel, 'answers', never):
            self.assertFalse(await reachable('https://x.trycloudflare.com/healthz', 0.5, problems))
        self.assertEqual(problems, ['ClientConnectorDNSError'])
        with mock.patch.object(tunnel, 'public_addresses', one_public), \
                mock.patch.object(tunnel, 'answers', never):
            self.assertFalse(await reachable('https://x.trycloudflare.com/healthz', 0.5, problems))
        self.assertEqual(problems[-1], 'HTTP 530 through public DNS')


if __name__ == '__main__':
    unittest.main()
