"""What is talking on the other end of a phone call: a person, a hold queue, a menu, or something
else, judged from the other side's latest words by OpenAI's Decisions API.

In simulated calls GPT-Live never asked its backend to wait on hold, even when told plainly that
only its backend could (it sat through whole queues, paid, and greeted the recordings). So
Smitline does not wait for it: each thing the other side says is judged once they pause, and a
hold queue's announcement ("all of our representatives are busy", "please stay on the line") puts
the call on hold. After the voice is woken, the same question tells a person picking up from the
hold loop's next recording. See phone_hold.py and docs/phone.md#waiting-on-hold.

The Decisions API picks one of fixed choices and says how sure it is, about 10 times faster than
a Responses call, and bills input tokens only: a check of about 230 tokens costs about $0.00002
(gpt-6-luna, $0.10 per million). Tried on 2026-11-11: a queue message, a rep's greeting, a menu
and a French hold message came back hold, person, menu and hold, at 0.89 to 0.97 confidence.
"""
import json


DECISIONS_URL = 'https://api.openai.com/v1/decisions'
HOLD_JUDGE_MODEL = 'gpt-6-luna'
HOLD_JUDGE_TIMEOUT = 8.0
HOLD_JUDGE_LINES = 8
KINDS = ('person', 'hold', 'menu', 'other')
# What the call does with an answer: only one this sure.
SURE = 0.7
LINE_QUESTION = {
    'type': 'choice',
    'name': 'line',
    'instructions': ('The latest words from the other end of a phone call an AI assistant placed. '
                     'What is talking? Judge by what the words are, in any language.'),
    'choices': [
        {'value': 'person', 'description': 'A live person speaking: greeting the caller, asking or '
                                           'answering something, or saying they will be right back.'},
        {'value': 'hold', 'description': 'A recording or message meaning the caller is waiting on hold '
                                         'or in a queue (everyone is busy, calls are answered in order, '
                                         'stay on the line, an expected wait), or a person asking the '
                                         'caller to hold.'},
        {'value': 'menu', 'description': 'An automated phone menu reading options or asking the caller '
                                         'to press or say something.'},
        {'value': 'other', 'description': 'Anything else: a voicemail greeting, a call screener, or '
                                          'words that fit nothing here.'},
    ],
}


def hold_judge_request(model, earlier, latest):
    """One decision: `earlier` is [(speaker, text)] of the call so far, most recent last."""
    lines = [f"{'Assistant' if speaker == 'agent' else 'Line'}: {text}"
             for speaker, text in list(earlier)[-HOLD_JUDGE_LINES:] if str(text or '').strip()]
    context = ('Earlier on the call, most recent last:\n' + '\n'.join(lines) + '\n\n') if lines else ''
    return {
        'model': model,
        'input': f'{context}The latest words from the line:\n{latest}',
        'questions': [LINE_QUESTION],
    }


class HoldJudge:
    """Asks the Decisions API what is talking; counts its tokens for the call's usage."""

    def __init__(self, api_key, *, model=HOLD_JUDGE_MODEL, post=None):
        self.api_key = api_key
        self.model = model or HOLD_JUDGE_MODEL
        self._post = post or self._aiohttp_post
        self.tokens = {'input': 0, 'cached': 0, 'output': 0}
        self.calls = 0

    async def _aiohttp_post(self, payload):
        from aiohttp import ClientSession, ClientTimeout
        headers = {'Authorization': f'Bearer {self.api_key}', 'Content-Type': 'application/json'}
        async with ClientSession(timeout=ClientTimeout(total=HOLD_JUDGE_TIMEOUT)) as session:
            async with session.post(DECISIONS_URL, json=payload, headers=headers) as response:
                body = await response.json(content_type=None)
                if response.status >= 400:
                    raise RuntimeError(f'hold check failed ({response.status})')
                return body

    async def decide(self, earlier, latest):
        """(kind, confidence): 'person', 'hold', 'menu' or 'other', and how sure; (None, 0.0)
        when it could not be judged."""
        self.calls += 1
        try:
            response = await self._post(hold_judge_request(self.model, earlier, latest))
        except Exception:
            return None, 0.0
        usage = (response or {}).get('usage') or {}
        self.tokens['input'] += int(usage.get('input_tokens') or 0)
        self.tokens['cached'] += int(((usage.get('input_tokens_details') or {}).get('cached_tokens')) or 0)
        self.tokens['output'] += int(usage.get('output_tokens') or 0)
        answer = next((item for item in (response or {}).get('answers') or ()
                       if isinstance(item, dict) and item.get('name') == LINE_QUESTION['name']), {})
        kind = answer.get('choice')
        try:
            confidence = float(answer.get('confidence') or 0.0)
        except (TypeError, ValueError):
            confidence = 0.0
        return (kind, confidence) if kind in KINDS else (None, 0.0)


def sure(kind, confidence):
    """The kind when the judge was sure enough to act on it, else None."""
    return kind if kind in KINDS and confidence >= SURE else None


if __name__ == '__main__':  # python hold_judge.py "words": one decision with OPENAI_API_KEY
    import asyncio
    import os
    import sys
    print(json.dumps(asyncio.run(HoldJudge(os.environ['OPENAI_API_KEY']).decide([], ' '.join(sys.argv[1:])))))
