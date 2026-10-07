import asyncio
import tempfile
import unittest
from pathlib import Path

from call_costs import PRICES_AS_OF, call_cost, provider_price_from, spend, with_cost
from call_service import CallService
from call_store import CallStore


def phone_record(**usage):
    return {
        'id': 'call-0123456789abcdef', 'owner': 'local', 'channel': 'phone', 'status': 'completed',
        'createdAt': '2026-09-29T23:30:15Z',
        'line': {'providerCallSid': '00000000-0000-0000-0000-000000000001'},
        'usage': usage,
    }


def by_kind(cost):
    return {item['kind']: item for item in cost['items']}


class ProviderPriceTests(unittest.TestCase):
    def test_twilio_and_signalwire_prices(self):
        self.assertEqual(provider_price_from({'price': '-0.01400', 'price_unit': 'USD'}),
                         {'amount': 0.014, 'currency': 'USD'})
        self.assertEqual(provider_price_from({'price': 0.017, 'price_unit': 'usd'}),
                         {'amount': 0.017, 'currency': 'USD'})

    def test_no_price_yet(self):
        self.assertIsNone(provider_price_from({'price': None}))
        self.assertIsNone(provider_price_from({'price': ''}))
        self.assertIsNone(provider_price_from({'price': 'n/a'}))
        self.assertIsNone(provider_price_from(None))


class CallCostTests(unittest.TestCase):
    def test_itemized_cost_with_the_providers_price(self):
        cost = call_cost(phone_record(
            voiceSeconds=30, phoneSeconds=53, phonePrice={'amount': 0.017, 'currency': 'USD'},
            backendTokens={'input': 1_000_000, 'cached': 500_000, 'output': 100_000, 'webSearches': 2},
            backendModel='gpt-5.6-terra',
            summaryTokens={'input': 1000, 'output': 500}, summaryModel='gpt-5.6-luna'))
        items = by_kind(cost)
        self.assertEqual(items['phone'], {'kind': 'phone', 'provider': 'signalwire', 'seconds': 53,
                                          'amount': 0.017, 'source': 'provider'})
        self.assertEqual(items['voice']['amount'], 0.025)           # 30 s at $0.05 a minute
        # 500k uncached at $2, 500k cached at $0.20, 100k out at $12, two searches at $0.01.
        self.assertEqual(items['backend']['amount'], 1.0 + 0.1 + 1.2 + 0.02)
        self.assertEqual(items['summary']['amount'], 0.0008)        # 1000 * 0.2 + 500 * 1.2, per million
        self.assertEqual(cost['phone'], 0.017)
        self.assertEqual(cost['total'], round(0.017 + 0.025 + 2.32 + 0.0008, 6))
        self.assertEqual(cost['openai'], round(cost['total'] - 0.017, 6))
        self.assertFalse(cost['estimated'])
        self.assertEqual(cost['unpriced'], [])
        self.assertEqual(cost['pricesAsOf'], PRICES_AS_OF)

    def test_phone_is_estimated_until_the_provider_reports(self):
        cost = call_cost(phone_record(voiceSeconds=60, phoneSeconds=61, phoneProvider='twilio'))
        phone = by_kind(cost)['phone']
        self.assertEqual((phone['source'], phone['amount'], phone['provider']), ('estimate', 0.028, 'twilio'))
        self.assertTrue(cost['estimated'])
        override = call_cost(phone_record(phoneSeconds=30),
                             {'COLLEAGUE_PHONE_PRICE_PER_MINUTE': '0.02'})
        self.assertEqual(by_kind(override)['phone']['amount'], 0.02)
        self.assertNotIn('ratePerCall', by_kind(override)['phone'])

    def test_signalwire_estimate_matches_its_bills(self):
        # SignalWire bills $0.011 a started minute plus $0.006 a call: 2 min 1 s is $0.039.
        phone = by_kind(call_cost(phone_record(phoneSeconds=121)))['phone']
        self.assertEqual(phone['provider'], 'signalwire')
        self.assertEqual((phone['amount'], phone['ratePerMinute'], phone['ratePerCall']),
                         (0.039, 0.011, 0.006))
        self.assertEqual(by_kind(call_cost(phone_record(phoneSeconds=53)))['phone']['amount'], 0.017)

    def test_provider_is_known_from_the_call_id(self):
        record = phone_record(phoneSeconds=10)
        record['line']['providerCallSid'] = 'CA' + '0' * 32
        self.assertEqual(by_kind(call_cost(record))['phone']['provider'], 'twilio')

    def test_unanswered_call_costs_nothing(self):
        cost = call_cost(phone_record())
        self.assertEqual((cost['items'], cost['total']), ([], 0.0))

    def test_unknown_model_is_listed_not_guessed(self):
        cost = call_cost(phone_record(backendTokens={'input': 10, 'output': 5},
                                      backendModel='gpt-9-mystery'))
        self.assertIsNone(by_kind(cost)['backend']['amount'])
        self.assertEqual(cost['unpriced'], ['gpt-9-mystery'])
        self.assertEqual(cost['total'], 0.0)

    def test_older_records_use_the_default_models(self):
        cost = call_cost(phone_record(backendTokens={'input': 10, 'output': 5},
                                      summaryTokens={'input': 10, 'output': 5}))
        items = by_kind(cost)
        self.assertEqual(items['backend']['model'], 'gpt-5.6-terra')
        self.assertEqual(items['summary']['model'], 'gpt-5.6-luna')

    def test_meetings_price_the_voice(self):
        cost = call_cost({'channel': 'meeting', 'usage': {'voiceSeconds': 600}})
        self.assertEqual([item['kind'] for item in cost['items']], ['voice'])
        self.assertEqual(cost['total'], 0.5)

    def test_with_cost_keeps_the_stored_cost(self):
        stored = dict(phone_record(voiceSeconds=60), cost={'total': 9})
        self.assertIs(with_cost(stored), stored)
        live = dict(phone_record(voiceSeconds=60), status='in_progress')
        self.assertNotIn('cost', with_cost(live))
        self.assertEqual(with_cost(phone_record(voiceSeconds=60))['cost']['total'], 0.05)


class SpendTests(unittest.TestCase):
    def test_days_follow_the_readers_time_zone(self):
        late = dict(phone_record(), createdAt='2026-09-30T02:00:00Z',
                    cost={'total': 0.1, 'phone': 0.02, 'openai': 0.08, 'estimated': True})
        early = dict(phone_record(), createdAt='2026-09-29T15:00:00Z',
                     cost={'total': 0.2, 'phone': 0.03, 'openai': 0.17, 'estimated': False})
        live = dict(phone_record(), status='in_progress', cost=None)
        eastern = spend([late, early, live], tz_offset_minutes=-240)
        self.assertEqual(eastern['days'], [{'day': '2026-09-29', 'calls': 2, 'total': 0.3,
                                            'phone': 0.05, 'openai': 0.25, 'estimated': True,
                                            'seconds': 0}])
        utc = spend([late, early])
        self.assertEqual([day['day'] for day in utc['days']], ['2026-09-29', '2026-09-30'])
        self.assertEqual(utc['currency'], 'USD')


    def test_days_add_up_connected_time(self):
        cost = {'total': 0.1, 'phone': 0, 'openai': 0.1, 'estimated': False}
        from_result = dict(phone_record(), cost=cost, result={'durationSeconds': 268.4})
        from_times = dict(phone_record(), cost=cost, result=None,
                          answeredAt='2026-09-29T15:00:00Z', endedAt='2026-09-29T15:01:30Z')
        unanswered = dict(phone_record(), cost=cost, result=None, answeredAt=None)
        day = spend([from_result, from_times, unanswered])['days'][0]
        self.assertEqual((day['calls'], day['seconds']), (3, 268 + 90))


class PricingLine:
    channel = 'phone'

    def __init__(self, prices):
        self.prices = list(prices)
        self.asked = []

    async def provider_price(self, credentials, record):
        credentials('twilio')
        self.asked.append(record['id'])
        return self.prices.pop(0) if self.prices else None


class Hooks:
    environ = {}

    def credentials(self, owner, provider):
        return {'provider': 'signalwire'}


class PriceLookupTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = CallStore(Path(self.temp.name) / 'calls')

    async def asyncTearDown(self):
        self.temp.cleanup()

    def service(self, line, **kwargs):
        return CallService(self.store, hooks=Hooks(), lines={'phone': line}, environ={}, **kwargs)

    def stored(self, call_id='call-0123456789abcdef', **usage):
        record = dict(phone_record(phoneSeconds=53, voiceSeconds=30, **usage), id=call_id)
        record['cost'] = call_cost(record)
        return self.store.create(record)

    async def test_price_is_recorded_once_the_provider_has_it(self):
        line = PricingLine([None, {'amount': 0.017, 'currency': 'USD'}])
        service = self.service(line, price_check_delays=(0, 0, 0))
        record = self.stored()
        await service._settle_price(record['id'])
        saved = self.store.get(record['id'])
        self.assertEqual(line.asked, [record['id']] * 2)
        self.assertEqual(saved['usage']['phonePrice'], {'amount': 0.017, 'currency': 'USD'})
        self.assertEqual(by_kind(saved['cost'])['phone']['source'], 'provider')
        self.assertFalse(saved['cost']['estimated'])

    async def test_backfill_looks_up_older_calls_once(self):
        line = PricingLine([{'amount': 0.028, 'currency': 'USD'}])
        service = self.service(line)
        self.stored('call-00000000000000a1')
        self.stored('call-00000000000000a2', phonePrice={'amount': 0.01, 'currency': 'USD'})
        records = service.list(limit=None)
        service.backfill_prices(records)
        service.backfill_prices(records)
        await asyncio.gather(*service._tasks)
        self.assertEqual(line.asked, ['call-00000000000000a1'])
        self.assertEqual(self.store.get('call-00000000000000a1')['cost']['phone'], 0.028)

    async def test_list_adds_costs_to_older_calls(self):
        service = self.service(PricingLine([]))
        record = dict(phone_record(voiceSeconds=60), id='call-00000000000000b1')
        self.store.create(record)
        self.assertEqual(service.list(limit=None)[0]['cost']['openai'], 0.05)
        self.assertNotIn('cost', self.store.get('call-00000000000000b1'))


if __name__ == '__main__':
    unittest.main()
