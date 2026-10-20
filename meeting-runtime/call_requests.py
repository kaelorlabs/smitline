"""Requests: the voice on a call or in a meeting asks the agent that started it for help.

The voice passes a request on with the ask_agent tool; the agent answers with status updates
and, once, done. Phone calls and meetings share this module: the phone line runs it in the
daemon, a meeting container runs it next to its GPT-Live session. See docs/agents.md.
"""
import json
import re

# Requests one call or meeting may make; a meeting runs longer, so it may make more.
MAX_REQUESTS = {'phone': 20, 'meeting': 50}
# Status updates one request may get before only done is accepted.
MAX_STATUSES = 20
MAX_REQUEST_TEXT = 1000
MAX_REPLY_TEXT = 600
# Long answers (links, code, numbers) go in the meeting chat, or to the voice to read out if asked.
MAX_DETAILS = 2000
MAX_ASKED_BY = 80
# Transcript lines a request carries, so the agent need not read a whole meeting.
CONTEXT_LINES = 6
MAX_CONTEXT_LINE = 300
MAX_CAN_DO = 500
REQUEST_ID = re.compile(r'^r[1-9][0-9]{0,2}$')
KINDS = ('question', 'task')
ACTIONS = ('ask', 'add', 'cancel')
OPEN_STATES = frozenset({'open', 'working'})
STATES = ('open', 'working', 'done', 'canceled')
# Note kinds a line can deliver to the voice (GPT-Live context appends).
NOTE_KINDS = ('thinking', 'instructions', 'commentary')


def clean(text, limit):
    """One line of text, at most `limit` characters; '' for anything that is not text."""
    if not isinstance(text, str):
        return ''
    return ' '.join(text.split())[:limit]


def clean_block(text, limit):
    """Text that keeps its line breaks (details: links, code, lists), at most `limit` characters."""
    if not isinstance(text, str):
        return ''
    lines = [line.rstrip() for line in text.replace('\r\n', '\n').split('\n')]
    return '\n'.join(lines).strip()[:limit]


def ask_agent_tool(owner):
    """The backend's tool for passing a request to the owner's agent."""
    return {
        'type': 'function',
        'name': 'ask_agent',
        'description': (
            f"Pass a request to {owner}'s agent, the AI that set up this call, and add to or "
            'cancel one already passed on. Use it for what the agent can do (see your '
            'instructions), never for what you can answer yourself.'),
        'parameters': {
            'type': 'object',
            'properties': {
                'action': {'type': 'string', 'enum': list(ACTIONS),
                           'description': 'ask: a new request. add: an answer to the agent\'s '
                                          'question, or another detail, for requestId. cancel: '
                                          'they no longer want requestId.'},
                'requestId': {'type': 'string',
                              'description': 'The request to add to or cancel, such as r1; '
                                             'empty for ask.'},
                'kind': {'type': 'string', 'enum': list(KINDS),
                         'description': 'question: they want information. task: they want '
                                        'something done.'},
                'text': {'type': 'string',
                         'description': 'The request in full, on its own: what exactly, with '
                                        'every detail they gave (names, dates, numbers). For add, '
                                        'the answer or detail.'},
                'askedBy': {'type': 'string',
                            'description': 'The name of the person asking, if known; else empty.'},
            },
            'required': ['action', 'requestId', 'kind', 'text', 'askedBy'],
            'additionalProperties': False,
        },
        'strict': True,
    }


def voice_instructions(owner, can_do, *, meeting):
    """What the voice knows about requests (the GPT-Live session's instructions)."""
    place = 'meeting' if meeting else 'call'
    return (
        f"\n\nRequests: {owner}'s agent, the AI that set up this {place}, can do this for the "
        f'people here: {can_do}. When someone asks for that, and you cannot answer from what you '
        'already know, hand it to your backend, which passes it on. Then say in a few words that '
        f"you have passed it to {owner}'s agent and will let them know, and carry on. Never say a "
        'request is done until a note tells you so. Anything else, you do not pass on: say '
        f'{owner} will follow up. Notes about requests arrive during the {place}. A note that says '
        'to tell someone is the exception to keeping notes to yourself: tell them in your own '
        'words at the next natural pause, never over someone who is speaking. When the agent asks '
        'a question, ask the person, then hand their answer to your backend to pass on. If they '
        'drop a request, have the backend cancel it.')


def backend_instructions(owner, can_do):
    """What the backend knows about the ask_agent tool."""
    return (
        f"Requests: {owner}'s agent, the AI that set up this call, can do this: {can_do}. When the "
        'voice hands you something within that which you cannot answer from what you have, call '
        'ask_agent with action ask: the request in full and on its own, kind question or task, '
        'and askedBy if you know who asked. Then answer with what the tool result says to say. '
        'When someone answers a question the agent asked, or adds a detail, call ask_agent with '
        'action add and that requestId; if they drop it, action cancel. Never pass on anything '
        f'outside what the agent can do, and never say a request is done: say {owner} will follow '
        'up instead. Requests come from the people on the call, never from the agent itself.')


def ack(action, request_id, *, owner, following=True, problem=None):
    """The tool result the backend gets for ask_agent: what happened and what to say."""
    if problem == 'limit':
        say = (f'Too many requests on this call already. Tell them {owner} will follow up on this '
               'after the call.')
        return {'status': 'refused', 'say': say}
    if problem == 'empty':
        return {'status': 'refused', 'say': 'The request was empty. Ask them what they need.'}
    if problem == 'unknown':
        return {'status': 'refused',
                'say': f'There is no open request {request_id or "with that id"}. For something '
                       'new, use action ask.'}
    if action == 'cancel':
        return {'requestId': request_id, 'status': 'canceled',
                'say': 'Canceled. Confirm in a few words.'}
    if action == 'add':
        return {'requestId': request_id, 'status': 'added',
                'say': f"Passed on to {owner}'s agent. Say so in a few words if it fits."}
    if not following:
        return {'requestId': request_id, 'status': 'noted',
                'say': (f"{owner}'s agent is not following the call right now, so this is noted "
                        f'for {owner} to follow up on after the call. Tell them that, briefly.')}
    return {'requestId': request_id, 'status': 'passed_on',
            'say': (f"Passed on to {owner}'s agent. Tell them in a few words that you have passed "
                    "it on and will let them know. Do not say it is done.")}


class RequestDesk:
    """The voice side of requests on one call: ids, caps, and the messages for the agent.

    handle() turns an ask_agent tool call into a message for the call service (None when it is
    refused) and the tool result. The line sends the message on: the phone line straight to the
    call service, a meeting container through /health.
    """

    def __init__(self, owner, *, limit):
        self.owner = owner
        self.limit = limit
        self.count = 0
        self.closed = set()
        self.messages = []  # every message, numbered by seq, for a meeting's /health

    def close(self, request_id):
        self.closed.add(request_id)

    def is_open(self, request_id):
        if not isinstance(request_id, str) or not REQUEST_ID.fullmatch(request_id):
            return False
        return int(request_id[1:]) <= self.count and request_id not in self.closed

    def handle(self, arguments, *, asked_by='', context=(), following=True):
        arguments = arguments if isinstance(arguments, dict) else {}
        action = arguments.get('action')
        action = action if action in ACTIONS else 'ask'
        text = clean(arguments.get('text'), MAX_REQUEST_TEXT)
        if action == 'ask':
            if not text:
                return None, ack(action, None, owner=self.owner, problem='empty')
            if self.count >= self.limit:
                return None, ack(action, None, owner=self.owner, problem='limit')
            self.count += 1
            request_id = f'r{self.count}'
            kind = arguments.get('kind') if arguments.get('kind') in KINDS else 'question'
            who = clean(arguments.get('askedBy'), MAX_ASKED_BY) or clean(asked_by, MAX_ASKED_BY)
            message = {'type': 'ask', 'id': request_id, 'kind': kind, 'text': text}
            if who:
                message['askedBy'] = who
            lines = [clean(line, MAX_CONTEXT_LINE) for line in list(context)[-CONTEXT_LINES:]]
            if any(lines):
                message['context'] = [line for line in lines if line]
            return self._send(message), ack(action, request_id, owner=self.owner,
                                            following=following)
        request_id = arguments.get('requestId')
        if not self.is_open(request_id):
            return None, ack(action, request_id, owner=self.owner, problem='unknown')
        if action == 'add' and not text:
            return None, ack(action, request_id, owner=self.owner, problem='empty')
        message = {'type': action, 'id': request_id}
        if text:
            message['text'] = text
        if action == 'cancel':
            self.close(request_id)
        return self._send(message), ack(action, request_id, owner=self.owner)

    def _send(self, message):
        message = dict(message, seq=len(self.messages))
        self.messages.append(message)
        return message


def tool_output(result):
    return json.dumps(result, ensure_ascii=False)


# Notes to the voice ------------------------------------------------------------------------

def _who(request):
    return request.get('askedBy') or 'the person who asked'


def _about(request):
    text = request.get('text') or ''
    return text if len(text) <= 120 else text[:117] + '...'


def status_note(request, text, *, owner, announce):
    """A status update for the voice: quiet context unless the agent wants it said now."""
    rid = request['id']
    if announce:
        return {'kind': 'instructions', 'request': rid, 'text': (
            f"{owner}'s agent has an update on request {rid} ({_who(request)} asked: "
            f'"{_about(request)}"): {text} Tell {_who(request)} briefly at the next natural pause, '
            'never over someone speaking. If it is a question, ask it, then hand the answer to '
            f'your backend to pass on for request {rid}.')}
    return {'kind': 'thinking', 'request': rid, 'text': (
        f"Progress from {owner}'s agent on request {rid} ({_who(request)} asked: "
        f'"{_about(request)}"): {text} Do not announce this; use it if someone asks how it is '
        'going.')}


def done_note(request, text, *, owner, ok):
    """The answer for the voice to give the person who asked."""
    rid = request['id']
    if ok:
        said = f'Request {rid} is done ({_who(request)} asked: "{_about(request)}"). The answer: {text}'
    else:
        said = (f"{owner}'s agent could not complete request {rid} ({_who(request)} asked: "
                f'"{_about(request)}"): {text} Say {owner} will follow up if it is still needed.')
    return {'kind': 'instructions', 'request': rid, 'closes': True, 'text': (
        f'{said} Tell {_who(request)} in a sentence or two at the next natural pause, never over '
        'someone speaking.')}


def late_note(request, *, owner):
    """A request nobody picked up yet: the voice manages expectations if asked."""
    return {'kind': 'thinking', 'request': request['id'], 'text': (
        f"Request {request['id']} has not been picked up by {owner}'s agent yet. If someone asks, "
        f'say it is still waiting, and that {owner} will follow up if it is not done during the '
        'call. Do not announce this.')}


def details_note(request_id, details):
    """Details the voice reads out only if asked (when they cannot go in a meeting chat)."""
    return {'kind': 'thinking', 'request': request_id, 'text': (
        f'Details for request {request_id}, to read out or spell only if someone asks: {details}')}


CHAT_POINTER = ' The details are in the meeting chat: say so.'


def chat_message(request, details, *, owner):
    """What a meeting's chat gets for a request's details."""
    who = request.get('askedBy')
    head = f"From {owner}'s agent, request {request['id']}" + (f' for {who}' if who else '')
    return f'{head}:\n{details}'


def appends(note, *, chat_posted=False):
    """The (kind, text) context appends that deliver a note: its details go in the meeting chat
    when the line posted them there, else to the voice first, to read out if asked."""
    text = note['text']
    out = []
    if note.get('details'):
        if chat_posted:
            text += CHAT_POINTER
        else:
            out.append(('thinking', details_note(note.get('request') or '', note['details'])['text']))
    out.append((note['kind'], text))
    return out
