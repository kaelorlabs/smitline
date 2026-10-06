"""What a call cost, in US dollars: the phone provider's bill plus OpenAI usage at list prices.

Phone: the provider's own price for the call once it reports one (Twilio and SignalWire
fill it in shortly after a call ends). Until then, an estimate from the minutes.
OpenAI: GPT-Live seconds and model tokens, priced from the tables below. OpenAI does not
report a price per call, so these amounts are calculated from usage, not billed amounts.
When prices change, update the tables and PRICES_AS_OF; stored calls keep the cost they
were given when they ended.
"""
import math
import re
from datetime import datetime, timedelta

from call_result import DEFAULT_SUMMARY_MODEL
from phone_prompts import DEFAULT_BACKEND_MODEL

PRICES_AS_OF = '2026-09-29'
CURRENCY = 'USD'
# GPT-Live bills voice sessions per second at a per-minute rate.
VOICE_MODEL = 'gpt-live-1'
VOICE_PER_MINUTE = {'gpt-live-1': 0.05}
# Text models in USD per million tokens: input, cached input, output.
TOKEN_PRICES = {
    'gpt-6-astra': (10.00, 1.00, 50.00),
    'gpt-5.6-sol': (4.00, 0.40, 20.00),
    'gpt-5.6-terra': (2.00, 0.20, 12.00),
    'gpt-5.6-luna': (0.20, 0.02, 1.20),
    'gpt-5.5': (5.00, 0.50, 30.00),
}
WEB_SEARCH_PER_CALL = 0.01
# Only until the provider reports its price: USD per started minute, typical North American
# rates. COLLEAGUE_PHONE_PRICE_PER_MINUTE overrides both.
PHONE_PER_MINUTE = {'twilio': 0.014, 'signalwire': 0.017}
DEFAULT_PHONE_PER_MINUTE = 0.014
TERMINAL = frozenset({'completed', 'failed', 'canceled'})
TWILIO_SID = re.compile(r'^CA[0-9a-f]{32}$')
UUID = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')


def _money(value):
    return round(float(value), 6)


def provider_price_from(body):
    """The price in a Twilio or SignalWire call resource, or None while it is not known yet.

    Twilio reports charges as negative strings ("-0.01400"); SignalWire as positive numbers.
    """
    raw = (body or {}).get('price')
    if raw is None or raw == '':
        return None
    try:
        amount = abs(float(raw))
    except (TypeError, ValueError):
        return None
    return {'amount': _money(amount), 'currency': str(body.get('price_unit') or CURRENCY).upper()}


def phone_provider(record):
    usage = record.get('usage') or {}
    line = record.get('line') or {}
    provider = usage.get('phoneProvider') or line.get('provider')
    if provider:
        return provider
    sid = str(line.get('providerCallSid') or '')
    if TWILIO_SID.match(sid):
        return 'twilio'
    if UUID.match(sid):
        return 'signalwire'
    return None


def _phone_rate(provider, environ):
    override = (environ or {}).get('COLLEAGUE_PHONE_PRICE_PER_MINUTE')
    if override:
        try:
            return float(override)
        except ValueError:
            pass
    return PHONE_PER_MINUTE.get(provider, DEFAULT_PHONE_PER_MINUTE)


def _phone_item(record, environ):
    usage = record.get('usage') or {}
    provider = phone_provider(record)
    seconds = usage.get('phoneSeconds')
    price = usage.get('phonePrice')
    item = {'kind': 'phone', 'provider': provider, 'seconds': seconds}
    if isinstance(price, dict) and price.get('amount') is not None \
            and str(price.get('currency') or CURRENCY).upper() == CURRENCY:
        return dict(item, amount=_money(price['amount']), source='provider')
    if not seconds:
        return None  # never answered: carriers do not charge for unanswered calls
    rate = _phone_rate(provider, environ)
    return dict(item, amount=_money(math.ceil(seconds / 60) * rate), source='estimate',
                ratePerMinute=rate)


def _voice_item(usage):
    seconds = usage.get('voiceSeconds') or usage.get('usageSeconds') or 0
    if not seconds:
        return None
    rate = VOICE_PER_MINUTE[VOICE_MODEL]
    return {'kind': 'voice', 'model': VOICE_MODEL, 'seconds': seconds,
            'amount': _money(seconds / 60 * rate), 'source': 'list_price', 'ratePerMinute': rate}


def _model_item(kind, counts, model):
    counts = counts or {}
    tokens_in = int(counts.get('input') or 0)
    cached = min(int(counts.get('cached') or 0), tokens_in)
    tokens_out = int(counts.get('output') or 0)
    searches = int(counts.get('webSearches') or 0)
    if not (tokens_in or tokens_out or searches):
        return None
    item = {'kind': kind, 'model': model, 'input': tokens_in, 'cached': cached,
            'output': tokens_out, 'source': 'list_price'}
    if searches:
        item['webSearches'] = searches
    prices = TOKEN_PRICES.get(model)
    if prices is None:
        return dict(item, amount=None)
    price_in, price_cached, price_out = prices
    amount = ((tokens_in - cached) * price_in + cached * price_cached
              + tokens_out * price_out) / 1_000_000 + searches * WEB_SEARCH_PER_CALL
    return dict(item, amount=_money(amount))


def call_cost(record, environ=None):
    """Itemized cost of one call from its usage record."""
    environ = environ or {}
    usage = record.get('usage') or {}
    items = []
    if record.get('channel') == 'phone':
        items.append(_phone_item(record, environ))
    items.append(_voice_item(usage))
    backend_model = usage.get('backendModel') or environ.get('COLLEAGUE_PHONE_BACKEND_MODEL') \
        or DEFAULT_BACKEND_MODEL
    items.append(_model_item('backend', usage.get('backendTokens'), backend_model))
    summary_model = usage.get('summaryModel') or environ.get('COLLEAGUE_SUMMARY_MODEL') \
        or DEFAULT_SUMMARY_MODEL
    items.append(_model_item('summary', usage.get('summaryTokens'), summary_model))
    items = [item for item in items if item is not None]
    priced = [item['amount'] for item in items if item['amount'] is not None]
    phone = sum(item['amount'] for item in items if item['kind'] == 'phone' and item['amount'] is not None)
    return {
        'currency': CURRENCY,
        'total': _money(sum(priced)),
        'phone': _money(phone),
        'openai': _money(sum(priced) - phone),
        'estimated': any(item['source'] == 'estimate' for item in items),
        'unpriced': sorted({item['model'] for item in items if item['amount'] is None}),
        'items': items,
        'pricesAsOf': PRICES_AS_OF,
    }


def with_cost(record, environ=None):
    """A finished call with its cost: the stored one, or one calculated now for older calls."""
    if record.get('status') not in TERMINAL or record.get('cost'):
        return record
    return dict(record, cost=call_cost(record, environ))


def _created(record):
    try:
        return datetime.fromisoformat(str(record.get('createdAt') or '').replace('Z', '+00:00'))
    except ValueError:
        return None


def _talk_seconds(record):
    """How long the call or meeting was connected, in whole seconds; 0 when it never connected."""
    seconds = (record.get('result') or {}).get('durationSeconds')
    if isinstance(seconds, (int, float)) and not isinstance(seconds, bool) and seconds > 0:
        return round(seconds)
    try:
        answered = datetime.fromisoformat(str(record.get('answeredAt')).replace('Z', '+00:00'))
        ended = datetime.fromisoformat(str(record.get('endedAt')).replace('Z', '+00:00'))
    except ValueError:
        return 0
    return max(0, round((ended - answered).total_seconds()))


def spend(records, *, tz_offset_minutes=0):
    """Finished calls' costs and connected time per local day, oldest first, for totals and a chart."""
    offset = timedelta(minutes=tz_offset_minutes)
    days = {}
    for record in records:
        cost = record.get('cost')
        created = _created(record)
        if record.get('status') not in TERMINAL or not cost or created is None:
            continue
        day = (created + offset).date().isoformat()
        bucket = days.setdefault(day, {'day': day, 'calls': 0, 'total': 0.0, 'phone': 0.0,
                                       'openai': 0.0, 'estimated': False, 'seconds': 0})
        bucket['calls'] += 1
        bucket['seconds'] += _talk_seconds(record)
        for key in ('total', 'phone', 'openai'):
            bucket[key] = _money(bucket[key] + (cost.get(key) or 0))
        bucket['estimated'] = bucket['estimated'] or bool(cost.get('estimated'))
    return {'currency': CURRENCY, 'pricesAsOf': PRICES_AS_OF,
            'days': [days[day] for day in sorted(days)]}
