"""What an agent can tell the user about Smitline itself: GET /v1/how-to and the MCP tool
how_to_smitline.

Each topic is a few short paragraphs to pass on, and named links. The text here is for an
installation its owner runs: it pays its own providers, so it quotes no prices, and its account is
its own settings, read live. A hosted service gives its accounts its own text with the optional call
hook how_to(owner, topic, guide, client=...), which may replace or extend a topic's text and links.

The text informs the user; it gives the agent no instructions. What an agent should do is in the
tools' descriptions and the server's instructions.
"""
import re

from call_policy import PolicyProblem, call_limits, calling_hours

TOPICS = ('overview', 'using', 'following', 'pricing', 'account', 'troubleshooting', 'rules')
DEFAULT_TOPIC = 'overview'
# The app asking, as a hosted gateway names it (GET /v1/how-to?client=), such as chatgpt, claude or
# other: an app's directory may have rules that change what a hosted service says there. Any name of
# this shape is passed on to the hook; the text here is the same for every app.
CLIENT = re.compile(r'^[a-z][a-z0-9-]{0,31}$')

REPO = 'https://github.com/kaelorlabs/smitline'
DOCS = REPO + '/blob/main/docs/'


def _link(name, url):
    return {'name': name, 'url': url}


def _clock(moment):
    return moment.strftime('%I:%M %p').lstrip('0')


def _hours(settings):
    """The calling hours as words, from SMITLINE_CALLING_HOURS (8 AM to 9 PM by default)."""
    try:
        hours = calling_hours(settings)
    except PolicyProblem:
        return 'during the calling hours set in SMITLINE_CALLING_HOURS'
    if hours is None:
        return 'at any hour (calling hours are off)'
    start, end = hours
    return f"between {_clock(start)} and {_clock(end)} in the other person's time"


def _limits(settings):
    """The repeat limits as words, from SMITLINE_MAX_CALLS_PER_NUMBER and SMITLINE_MAX_CALLS_PER_HOUR."""
    try:
        per_number, per_hour = call_limits(settings)
    except PolicyProblem:
        return 'up to the repeat limits in its settings'
    to_number = (f'at most {per_number} times to one number in any 24 hours' if per_number
                 else 'with no limit on calls to one number')
    an_hour = f'at most {per_hour} times an hour' if per_hour else 'with no hourly limit'
    return f'{to_number}, and {an_hour}'


def _codes(settings):
    """The calling codes this installation may call (SMITLINE_ALLOWED_CALLING_CODES); () for any."""
    raw = str(settings.get('SMITLINE_ALLOWED_CALLING_CODES') or '')
    return tuple('+' + item for item in (part.strip().lstrip('+') for part in raw.split(','))
                 if item.isdigit())


def _overview(settings):
    return [
        'Smitline lets AI agents make phone calls, join Zoom, Microsoft Teams and Google Meet '
        'meetings, or start a meeting room people join from a link. The agent follows the call '
        'live, steers it and can do work while it runs, then gets back what was said and agreed.',
        'This Smitline runs on its owner\'s own computer or server. It is the open-source Smitline '
        'engine (Apache-2.0, github.com/kaelorlabs/smitline), listed in the official MCP Registry '
        'as io.github.kaelorlabs/smitline. Smitline is made by Kaelor Labs, which also runs it as a '
        'hosted service, Smitline Cloud, at smitline.com.',
    ], [
        _link('Source code', REPO),
        _link('Documentation', REPO + '#documentation'),
        _link('Report a problem', REPO + '/issues'),
        _link('Smitline Cloud', 'https://smitline.com'),
    ]


def _using(settings):
    return [
        'The agent gives Smitline a brief: the goal, what to find out, what it may agree to, what '
        'it must not share, and the facts the call needs as context. Smitline follows the '
        'call until it ends and reports what was said and agreed. People on the call can ask the '
        'agent things when the brief allows it (canDo). A call can run on its own or be followed '
        'live (topic following).',
        'Google Meet often turns guest bots away; with the invitation\'s phone number and PIN '
        '(dialIn), Smitline joins by phone instead. A new meeting room (to "new") is ready in '
        'seconds; joining a meeting by its link can take a minute or two.',
        'There are several voices (list_voices), with one default for phone calls and one for '
        'meetings. A first call to someone new can be practiced on the user\'s own phone first (a '
        'rehearsal).',
    ], [
        _link('Calls guide', DOCS + 'calls.md'),
        _link('Agents guide', DOCS + 'agents.md'),
    ]


def _following(settings):
    return [
        'Start it and let it run: with a complete brief and context, Smitline holds the conversation '
        'by itself and returns the result when the call ends, to read with get_call or wait_for_call. '
        'This suits an agent that cannot stay, such as when its turn has to end or the user is away. '
        'The voice handles what it can; when it cannot answer something, it tells the person the user '
        'will follow up, and those questions come back as openRequests in the result.',
        'Follow it live, which is better whenever possible: after start_call, the agent keeps calling '
        'wait_for_call in the same turn (since "" the first time, then live.cursor) until the status is '
        'completed, failed or canceled, and each answer\'s next says what to do. A wait comes back at '
        'once when someone on the call asks the agent something, when the status changes or at the '
        'end, and otherwise about every 12 seconds with what was said. With canDo in the brief, people '
        'on the call can ask the agent questions or tasks (live.requests, answered with '
        'reply_to_call_request), and the agent can steer with send_call_instruction, or end_call if it '
        'goes wrong. In apps that ask before each tool call, setting wait_for_call, get_call and '
        'list_calls, and the two tools used during a call (send_call_instruction and '
        'reply_to_call_request), to Always allow lets it follow and help without a click each time.',
    ], [
        _link('Following a call live', DOCS + 'agents.md#following-a-call-live'),
    ]


def _pricing(settings):
    return [
        'This Smitline is self-hosted, so there is no Smitline subscription, plan or wallet. Each '
        'call is paid to the owner\'s own providers, at their prices: OpenAI for the voice '
        '(GPT-Live), and SignalWire or Twilio for the phone line. A meeting joined by its link uses '
        'OpenAI only.',
        'Each finished call shows what it cost at those prices (cost, in get_call and list_calls), '
        'and the console\'s Calls page shows the totals.',
    ], [
        _link('Costs', DOCS + 'calls.md#cost'),
    ]


def _account(settings):
    codes = _codes(settings)
    where = f' It calls numbers in the calling codes {", ".join(codes)} only.' if codes else ''
    return [
        'This Smitline is self-hosted: there is no Smitline account, plan or balance, and no daily '
        'call limit beyond its own settings. The OpenAI and SignalWire or Twilio accounts it uses '
        'show their own balances.',
        f'From its settings, it calls {_hours(settings)}, {_limits(settings)}.{where} The owner '
        'changes these with smitline setup set (SMITLINE_CALLING_HOURS, '
        'SMITLINE_MAX_CALLS_PER_NUMBER, SMITLINE_MAX_CALLS_PER_HOUR, SMITLINE_ALLOWED_CALLING_CODES).',
    ], [
        _link('Phone settings', DOCS + 'phone.md'),
    ]


def _troubleshooting(settings):
    return ['\n'.join([
        f'Outside calling hours (outside_calling_hours): Smitline calls {_hours(settings)}. It can '
        'call later, or now if the user confirms the person expects the call (afterHours).',
        'Repeat limits (too_many_calls_to_number, too_many_calls): the refusal says when Smitline '
        'can call again.',
        'Do not call (do_not_call): the person asked not to be called again. Smitline calls them '
        'only after the number is taken off the do-not-call list, once they have agreed to calls.',
        'Premium-rate and satellite numbers (high_cost_number) are refused.',
        'Missing setup: the refusal names the key or setting to add with smitline setup.',
        'Google Meet turning guests away: with the invitation\'s phone number and PIN (dialIn), '
        'Smitline joins by phone.',
        'A meeting still connecting: joining by its link can take a minute or two.',
        'No answer or voicemail: the outcome is not_reached or voicemail, and the summary says '
        'whether a message was left.',
    ])], [
        _link('Troubleshooting', DOCS + 'troubleshooting.md'),
        _link('Phone guardrails', DOCS + 'phone.md#guardrails'),
    ]


def _rules(settings):
    return [
        'Smitline says it is an AI: each call opens with whom it is calling for and why, says '
        'during the call that it is their AI assistant, and answers honestly when asked. In a '
        'meeting, its name and camera tile say so.',
        'It never calls emergency, crisis or premium-rate numbers, respects do-not-call requests, '
        f'limits repeat calls, and calls {_hours(settings)}.',
        'The user is responsible for what they ask it to do, including any consent rules for '
        'calling people and for recording calls.',
    ], [
        _link('Phone guardrails', DOCS + 'phone.md#guardrails'),
        _link('Security and privacy', DOCS + 'security.md'),
    ]


WRITERS = {'overview': _overview, 'using': _using, 'following': _following, 'pricing': _pricing,
           'account': _account, 'troubleshooting': _troubleshooting, 'rules': _rules}


def guide(topic, settings):
    """{topic, text, links} for an installation its owner runs, from the owner's settings."""
    paragraphs, links = WRITERS[topic](settings)
    return {'topic': topic, 'text': '\n\n'.join(paragraphs), 'links': links}


def checked_change(found, changed):
    """The guide after a hook's answer: None keeps it; a dict's `text` (a string that is not blank)
    and `links` (a list of {name, url} strings) replace the guide's own, each left as it is when
    missing. Anything else is a mistake in the hook: ValueError."""
    if changed is None:
        return found
    if not isinstance(changed, dict):
        raise ValueError('how_to must return a dict or None')
    result = dict(found)
    if changed.get('text') is not None:
        if not isinstance(changed['text'], str) or not changed['text'].strip():
            raise ValueError('how_to text must be a string that is not blank')
        result['text'] = changed['text'].strip()
    if changed.get('links') is not None:
        links = changed['links']
        if not isinstance(links, list) or not all(
                isinstance(item, dict) and isinstance(item.get('name'), str) and isinstance(item.get('url'), str)
                for item in links):
            raise ValueError('how_to links must be a list of {name, url}')
        result['links'] = [_link(item['name'], item['url']) for item in links]
    return result
