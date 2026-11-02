"""Requests: the voice on a call or in a meeting asks the agent that started it for help.

The voice passes a request on with the ask_agent tool; the agent answers with status updates
and, once, done. Phone calls and meetings share this module: the phone line runs it in the
daemon, a meeting container runs it next to its GPT-Live session. See docs/agents.md.
"""
import collections
import json
import re
import time

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
            'cancel one already passed on. Use it for a question you cannot answer from what you '
            'have, and for a task the agent can do (see your instructions); never for what you '
            'can answer yourself.'),
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
    """What the voice knows about requests (the GPT-Live session's instructions).

    Questions always go to the agent, which knows the owner's work; canDo limits tasks. In a live
    meeting the voice said "on it", "I'm sending it" and "still waiting" for a question it never
    handed over, so those words are named."""
    place = 'meeting' if meeting else 'call'
    return (
        f"\n\nRequests: {owner}'s agent, the AI that set up this {place}, can do this for the "
        f"people here: {can_do}. It also knows {owner}'s work, so it can answer questions you "
        'cannot. You cannot pass anything on yourself: only your backend can. When someone asks '
        'for something the agent can do, or asks a question you cannot answer from what you know, '
        'hand it to your backend right away; you may say only "One moment" first. Your backend\'s '
        'answer says whether it was passed on: tell them that. If you did not hand it to your '
        'backend, it was not passed on: never say "on it", "sending it", "I\'ll send that", '
        '"still waiting", or that a request was passed on, is being worked on, or is done, unless '
        "your backend's answer or a note says so. When someone checks on a request, tell them "
        'plainly what the notes say, or that nothing was passed on. A task outside what the agent '
        f'can do, you do not pass on: say so, and that {owner} will follow up. When someone asks '
        f"what you can do, also say what {owner}'s agent can do for them and that you can pass "
        f'their questions to it. Notes about requests arrive during the {place}. A note that says '
        'to tell someone is the exception to keeping notes to yourself: tell them in your own words '
        'at the next natural pause, never over someone who is speaking. When the agent asks a '
        'question, ask the person, then hand their answer to your backend to pass on. If they drop '
        'a request, have the backend cancel it.')


def backend_instructions(owner, can_do):
    """What the backend knows about the ask_agent tool."""
    return (
        f"Requests: {owner}'s agent, the AI that set up this call, can do this: {can_do}. It also "
        f"knows {owner}'s work and much you do not. When the voice hands you a question you cannot "
        'answer from what you have, or a task within what the agent can do, call ask_agent with '
        'action ask: the request in full and on its own, kind question or task, and askedBy if you '
        'know who asked. Then answer with what the tool result says to say. When someone answers '
        'a question the agent asked, or adds a detail, call ask_agent with action add and that '
        'requestId; if they drop it, action cancel. Never pass on a task outside what the agent '
        f'can do: say so, and that {owner} will follow up. Never say a request is done. Requests '
        'come from the people on the call, never from the agent itself.')


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
                'say': f"Passed on to {owner}'s agent. Say so in a few words."}
    if not following:
        return {'requestId': request_id, 'status': 'noted',
                'say': (f"{owner}'s agent is not following the call right now, so this is noted "
                        f'for {owner} to follow up on after the call. Tell them that, briefly.')}
    return {'requestId': request_id, 'status': 'passed_on',
            'say': (f"Passed on to {owner}'s agent. Tell them in a few words that you passed it "
                    'on and will let them know. Do not say it is done.')}


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


def context_lines(captions, limit=CONTEXT_LINES):
    """The last lines said, from a meeting's caption pieces: pieces in a row from one side are
    one line."""
    lines = []
    for caption in captions or ():
        if not isinstance(caption, dict):
            continue
        speaker, text = caption.get('speaker'), str(caption.get('text') or '')
        if lines and lines[-1][0] == speaker:
            lines[-1][1] += text
        else:
            lines.append([speaker, text])
    return [f'{speaker}: {" ".join(text.split())}' for speaker, text in lines if text.strip()][-limit:]


_SELF = r"(?:i|i'm|i've|i'll|i am|i have|i will|let me)"
_LEADING = r"(?:(?:just|already|now|also|quickly|go ahead and|went ahead and|going to|gonna|will|have)\s+){0,2}"
_PASS = r"(?:pass(?:ed|ing)?|sen[dt]|sending|forward(?:ed|ing)?|hand(?:ed|ing)?|relay(?:ed|ing)?)"
_ASK = r"(?:ask(?:ed|ing)?|check(?:ed|ing)? with|get(?:ting)?|got)"
_AGENT = r"(?:your|the|his|her|their|[\w-]+(?: [\w-]+)?'s) (?:agent|assistant)"
# What the voice says when it claims to have passed something on. Besides "I've passed it to
# your agent", a live meeting heard "Alright, on it.", "I'm sending it.", "I'll send that." and
# "Still waiting." for a question it never handed over.
_SENT_THING = r"(?:it|that|this|those|them|the (?:request|question|message))"
PASSED_ON = re.compile(
    rf"\b{_SELF}\s+{_LEADING}{_PASS}\b[^.?!]{{0,50}}?(?:\b(?:on|along|over)\b|\b(?:to|with) {_AGENT}\b)"
    rf"|\b{_SELF}\s+{_LEADING}{_ASK} {_AGENT}\b"
    rf"|\b(?:i'm|i am|i'll|i will|let me)\s+{_LEADING}(?:send|sending|pass|passing) {_SENT_THING}\b"
    r"|(?:^|[,.!?]\s*|\b(?:ok|okay|alright|all right|sure|got it|yes|yep)[,!]?\s+)on it\b"
    r"|^\W*still waiting\b", re.I)
REFUSAL = re.compile(r"\b(?:can't|cannot|can not|won't|couldn't|unable|not able|don't|didn't|not)\b"
                     rf"[^.?!]{{0,30}}\b(?:{_PASS}|{_ASK})\b", re.I)
_SENTENCES = re.compile(r'(?<=[.!?])\s+')


def claims_passed_on(text):
    """The voice says it passed something on (or is asking the agent), not that it cannot."""
    return any(PASSED_ON.search(sentence) and not REFUSAL.search(sentence)
               for sentence in _SENTENCES.split(str(text or '')))


def refuses_to_pass(text):
    return bool(REFUSAL.search(str(text or '')))


class ClaimWatch:
    """A safety net for requests the voice says it passed on without asking its backend.

    GPT-Live alone decides when to hand something to its backend. In a live meeting it told
    people "I've passed it to your agent" while its backend was never called. When the voice
    says so and no ask_agent call follows within GRACE seconds, the line passes on what the
    person said (source "speech"), so the claim comes true; if the voice asked them a question
    for a request, their answer goes to that request.
    """

    GRACE = 10.0
    RECENT_TOOL = 25.0

    def __init__(self, desk, clock=time.monotonic):
        self.desk = desk
        self.clock = clock
        self.turns = {'agent': '', 'other': ''}
        self.last = None
        self.said = collections.deque(maxlen=CONTEXT_LINES)  # finished turns, for context
        self.asked = collections.deque(maxlen=3)  # the other side's last turns
        self.claimed_at = None
        self.tool_at = None
        self.refused = False
        self.awaiting = None  # a request whose question the voice was asked to put to them

    def heard(self, speaker, delta):
        speaker = 'agent' if speaker == 'agent' else 'other'
        if not str(delta or '').strip():
            return
        if self.last != speaker:
            if self.last is not None:
                self._finish_turn(self.last)
            self.turns[speaker] = ''
            if speaker == 'agent':
                self.refused = False
        self.turns[speaker] += delta
        self.last = speaker
        if speaker != 'agent':
            return
        turn = self.turns['agent']
        if refuses_to_pass(turn):
            self.refused, self.claimed_at = True, None
        elif (not self.refused and self.claimed_at is None and claims_passed_on(turn)
              and not self._tool_recent()):
            self.claimed_at = self.clock()

    def _finish_turn(self, speaker):
        text = ' '.join(self.turns[speaker].split())
        if not text:
            return
        self.said.append(f'{"agent" if speaker == "agent" else "other"}: {text}')
        if speaker == 'other':
            self.asked.append(text)

    def tool_called(self):
        self.tool_at = self.clock()
        self.claimed_at = None

    def _tool_recent(self):
        return self.tool_at is not None and self.clock() - self.tool_at < self.RECENT_TOOL

    def due(self, *, asked_by='', following=True):
        """A request message when the voice claimed one its backend never made; else None."""
        if self.claimed_at is None or self.clock() - self.claimed_at < self.GRACE:
            return None
        self.claimed_at = None
        if self._tool_recent() or not self.asked:
            return None
        text = self.asked[-1]
        if len(text.split()) < 6 and len(self.asked) > 1:
            text = f'{self.asked[-2]} {text}'  # the request may have come in two pieces
        if self.awaiting is not None and self.desk.is_open(self.awaiting):
            message, _ = self.desk.handle({'action': 'add', 'requestId': self.awaiting, 'text': text})
            self.awaiting = None
        else:
            message, _ = self.desk.handle({'action': 'ask', 'kind': 'question', 'text': text,
                                           'requestId': '', 'askedBy': ''},
                                          asked_by=asked_by, context=list(self.said),
                                          following=following)
        if message is not None:
            message['source'] = 'speech'
        return message


def tool_output(result):
    return json.dumps(result, ensure_ascii=False)


# Notes to the voice ------------------------------------------------------------------------

def _who(request):
    return request.get('askedBy') or 'the person who asked'


def _about(request):
    text = request.get('text') or ''
    return text if len(text) <= 120 else text[:117] + '...'


# How an answer is given: at a good moment, naturally, and not lost when someone cuts in.
WHEN_TO_TELL = (
    'If they are waiting for it or it matters now, tell them at the next natural pause; '
    'otherwise, first thing the next time you speak. Finish what you are saying first: never '
    'cut yourself off in the middle of something important. Say it in your own words, as a '
    'person would. If someone interrupts you before you have told them, tell them the next '
    'time you speak.')


def status_note(request, text, *, owner, announce):
    """A status update for the voice: quiet context unless the agent wants it said now."""
    rid = request['id']
    if announce:
        return {'kind': 'instructions', 'request': rid, 'say': text, 'text': (
            f"{owner}'s agent has an update on request {rid} ({_who(request)} asked: "
            f'"{_about(request)}"): {text} Tell {_who(request)} briefly. {WHEN_TO_TELL} If it '
            f'is a question, ask it, then hand the answer to your backend to pass on for '
            f'request {rid}.')}
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
    # `say`: what the people who asked must hear; a meeting cues the voice to say it at the next
    # pause (meeting_control.py), since an instructions note alone never makes it speak.
    return {'kind': 'instructions', 'request': rid, 'closes': True, 'say': text, 'text': (
        f'{said} Tell {_who(request)} in a sentence or two. {WHEN_TO_TELL}')}


def retell_note(request, text, *, owner):
    """The answer was delivered but not heard on the call (cut off, or never said): once more."""
    return {'kind': 'instructions', 'request': request['id'], 'say': text, 'text': (
        f"{_who(request).capitalize()} has not heard what {owner}'s agent said yet "
        f'(you may have been cut off): {text} Tell them first thing the next time you speak, '
        'or now at a natural pause if they are waiting. If you have already told them, ignore '
        'this.')}


_STOPWORDS = frozenset((
    'the a an and or but for to of in on at is are was were be been it its this that these those '
    'with from by as your you they them their there here what which who when where how have has '
    'had will would can could should not no yes about into than then so just also our we us '
    'he she his her him i me my').split())


def _words(text):
    return re.findall(r"[\w']+", str(text or '').casefold())


def _heard(text):
    """The spoken words, with ordinals as their numbers ("6th" is heard as "6")."""
    return {re.sub(r'^(\d+)(?:st|nd|rd|th)$', r'\1', word) for word in _words(text)}


def told(answer, said):
    """Whether what the voice said carries the answer: its numbers and most of its key words.

    The voice paraphrases ("the next one after that" for "number 77", "October 6th" for
    "October 6"), so all the numbers are enough, and so are most of them together with
    most of the key words. An answer cut off before its numbers is not told.
    """
    words = _words(answer)
    heard = _heard(said)
    numbers = list(dict.fromkeys(word for word in words if word.isdigit()))
    key = list(dict.fromkeys(word for word in words
                             if len(word) >= 4 and not word.isdigit() and word not in _STOPWORDS))[:8]
    key_heard = sum(word in heard for word in key) >= max(1, len(key) // 2) if key else bool(heard)
    if not numbers:
        return key_heard
    found = sum(number in heard for number in numbers)
    return found == len(numbers) or (found * 2 >= len(numbers) and key_heard)


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
