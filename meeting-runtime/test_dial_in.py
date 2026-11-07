"""Joining a meeting by phone (dial_in.py): reading invitations, the brief, the way in, the keys,
the meeting voice on the phone line, and the call's result. No real call: the phone provider and
GPT-Live are the fakes test_phone.py uses."""
import asyncio
import base64
from datetime import datetime, timedelta, timezone
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import dial_in
import dial_in_details
from call_brief import MEETING_TO_ERROR, CallBrief
from call_costs import call_cost
from call_hooks import CallRefused, DefaultCallHooks
from call_service import CallError, CallService
from call_store import CallStore
from meeting_line import MeetingLine
from phone_line import PhoneLine
from test_phone import ENV, PUBLIC, FakeLive, FakeTwilio, FakeTwilioSocket, until


ZOOM_LINK = 'https://us06web.zoom.us/j/85951289482?pwd=bE9Xa2VhT1Zpc2N0d0lQZz09'
ZOOM_INVITE = """Smith Lee is inviting you to a scheduled Zoom meeting.

Topic: Smitline dial-in test
Time: Nov 5, 2026 10:00 AM Eastern Time (US and Canada)

Join Zoom Meeting
https://us06web.zoom.us/j/85951289482?pwd=bE9Xa2VhT1Zpc2N0d0lQZz09

Meeting ID: 859 5128 9482
Passcode: 4kT9xZ

---

One tap mobile
+14388097799,,85951289482#,,,,*123456# Canada
+16473744685,,85951289482#,,,,*123456# Canada

---

Dial by your location
• +1 438 809 7799 Canada
• +1 647 374 4685 Canada
• +44 203 481 5240 United Kingdom
• +1 646 931 3860 US (New York)
• 855 703 8985 Canada Toll-free

Meeting ID: 859 5128 9482
Passcode: 123456

Find your local number: https://us06web.zoom.us/u/kbS1aB2c3
"""
TEAMS_INVITE = """________________________________________________________________________________
Microsoft Teams Need help?
Join the meeting now
Meeting ID: 254 377 912 345
Passcode: Ab3cD4
________________________________
Dial in by phone
+1 323-849-4874,,123456789# United States, Los Angeles
(833) 255-2803,,123456789# United States (Toll-free)
Find a local number
Phone conference ID: 123 456 789#
For organizers: Meeting options | Reset dial-in PIN
________________________________________________________________________________
"""
# Google Calendar wraps the number and the PIN in bidi marks.
MEET_INVITE = """Smitline sync
Wednesday, November 5 · 3:00 – 3:30pm
Google Meet joining info
Video call link: https://meet.google.com/abc-defg-hij
Or dial: \u202a(US) +1 650-555-0123\u202c PIN: \u202a123 456 789\u202c#
More phone numbers: https://tel.meet/abc-defg-hij?pin=4458722187937
"""
MEET_LINK = 'https://meet.google.com/abc-defg-hij'
# A Teams organizer's audio conferencing number, in Los Angeles: not a published bridge.
TEAMS_NUMBER = '+13238494874'
NIGHT = datetime(2026, 11, 5, 8, 0, tzinfo=timezone.utc)  # 3 AM in New York, midnight in Los Angeles
DAY = datetime(2026, 11, 5, 18, 0, tzinfo=timezone.utc)   # 1 PM in New York, 10 AM in Los Angeles


def meeting_brief(**overrides):
    payload = {'channel': 'meeting', 'onBehalfOf': 'Smith', 'objective': 'Take notes on the launch plan'}
    payload.update(overrides)
    return payload


class InviteTests(unittest.TestCase):
    def test_a_zoom_invitation_gives_its_one_tap_number_and_the_phone_passcode(self):
        found = dial_in_details.parse_invite(ZOOM_INVITE)
        self.assertEqual(found['platform'], 'zoom')
        self.assertEqual(found['meetingId'], '85951289482')
        # The numeric phone passcode, never the web one (4kT9xZ).
        self.assertEqual(found['passcode'], '123456')
        self.assertEqual(found['numbers'][:2], [('+14388097799', ',,85951289482#,,,,*123456#'),
                                                ('+16473744685', ',,85951289482#,,,,*123456#')])
        # The other numbers count once each; 855 is a North American toll-free number.
        self.assertEqual([number for number, _ in found['numbers'][2:]],
                         ['+442034815240', '+16469313860', '+18557038985'])
        self.assertEqual(dial_in_details.resolve({'invite': ZOOM_INVITE}), {
            'number': '+14388097799', 'meetingId': '85951289482', 'passcode': '123456',
            'keys': ',,85951289482#,,,,*123456#', 'platform': 'zoom'})

    def test_a_teams_invitation_gives_the_phone_conference_id_not_the_app_meeting_id(self):
        found = dial_in_details.parse_invite(TEAMS_INVITE)
        self.assertEqual((found['platform'], found['meetingId'], found['passcode']),
                         ('teams', '123456789', None))
        self.assertEqual(found['numbers'], [('+13238494874', ',,123456789#'),
                                            ('+18332552803', ',,123456789#')])
        self.assertEqual(dial_in_details.resolve({'invite': TEAMS_INVITE}), {
            'number': '+13238494874', 'meetingId': '123456789', 'keys': ',,123456789#',
            'platform': 'teams'})

    def test_a_meet_invitation_with_bidi_marks_gives_the_pin(self):
        self.assertEqual(dial_in_details.resolve({'invite': MEET_INVITE}), {
            'number': '+16505550123', 'meetingId': '123456789', 'keys': ',,123456789#',
            'platform': 'meet'})

    def test_one_tap_numbers_in_their_usual_forms(self):
        self.assertEqual(dial_in_details.phone_address('+14388097799,,85951289482#,,,,*123456#'),
                         ('+14388097799', ',,85951289482#,,,,*123456#'))
        # A tel: link escapes # as %23; a dialer's ; is a wait.
        self.assertEqual(dial_in_details.phone_address('tel:+1-650-555-0123;123456789%23'),
                         ('+16505550123', ',123456789#'))
        self.assertEqual(dial_in_details.phone_address('+1 323-849-4874,,123456789#'),
                         ('+13238494874', ',,123456789#'))
        self.assertEqual(dial_in_details.phone_address('+14155550142'), ('+14155550142', None))
        for other in ('4155550142', ZOOM_LINK, 'new', '+1 415 call me'):
            self.assertIsNone(dial_in_details.phone_address(other), other)
        # Zoom's older one-tap form keys 0# for the participant ID, then the passcode.
        self.assertEqual(dial_in_details.split_keys(',,84538920231#,,,,0#,,778577#'),
                         ('84538920231', '778577'))
        self.assertEqual(dial_in_details.split_keys(',,123456789#'), ('123456789', None))

    def test_each_platform_keys_in_the_way_its_one_tap_number_does(self):
        self.assertEqual(dial_in_details.keys_for('zoom', '85951289482', '123456'), ',,85951289482#,,,,*123456#')
        self.assertEqual(dial_in_details.keys_for('zoom', '85951289482'), ',,85951289482#')
        self.assertEqual(dial_in_details.keys_for('teams', '123456789', '999'), ',,123456789#')
        self.assertEqual(dial_in_details.keys_for('meet', '123456789'), ',,123456789#')
        self.assertEqual(dial_in_details.keys_for(None, '5551234', '42'), ',,5551234#,,,,42#')
        # A comma is two seconds, as phone dialers take it; <Play digits> waits half a second per w.
        self.assertEqual(dial_in_details.play_digits(',,85951289482#,,,,*123456#'),
                         'wwww' * 2 + '85951289482#' + 'wwww' * 4 + '*123456#')

    def test_the_first_number_the_installation_may_call(self):
        uk = dial_in_details.resolve({'invite': ZOOM_INVITE}, settings={'SMITLINE_ALLOWED_CALLING_CODES': '44'})
        self.assertEqual((uk['number'], uk['keys']), ('+442034815240', ',,85951289482#,,,,*123456#'))
        # A premium-rate number is passed over for one that is not.
        numbers = [('+19005550100', None), ('+16469313860', None)]
        self.assertEqual(dial_in_details.pick_number(numbers, settings={})[0], '+16469313860')
        # With none allowed, the first, so the refusal names it.
        self.assertEqual(dial_in_details.pick_number(numbers, settings={'SMITLINE_ALLOWED_CALLING_CODES': '33'})[0],
                         '+19005550100')
        # A published bridge before another number, even its one-tap line: it keeps no calling hours.
        numbers = [(TEAMS_NUMBER, ',,85951289482#'), ('+16469313860', None)]
        self.assertEqual(dial_in_details.pick_number(numbers, '85951289482', settings={})[0], '+16469313860')
        self.assertEqual(dial_in_details.pick_number(numbers, '85951289482',
                                                     settings={'SMITLINE_DIAL_IN_BRIDGES': 'none'})[0],
                         TEAMS_NUMBER)

    def test_a_north_american_number_without_plus_counts_only_where_the_line_says_so(self):
        found = dial_in_details.parse_invite('Call 646 931 3860 for the meeting\nMeeting ID: 5551234')
        self.assertEqual(found['numbers'], [])
        found = dial_in_details.parse_invite('Call 646 931 3860 (US)\nMeeting ID: 5551234')
        self.assertEqual(found['numbers'], [('+16469313860', None)])

    def test_explicit_fields_win_and_must_agree(self):
        dial = dial_in_details.resolve({'invite': ZOOM_INVITE, 'number': '+16469313860'})
        self.assertEqual(dial['number'], '+16469313860')
        self.assertEqual(dial['keys'], ',,85951289482#,,,,*123456#')  # Zoom's keys, from the invite
        dial = dial_in_details.resolve({'number': '+16469313860', 'meetingId': 85951289482, 'passcode': '123 456'},
                               to=ZOOM_LINK)
        self.assertEqual(dial, {'number': '+16469313860', 'meetingId': '85951289482',
                                'passcode': '123456', 'keys': ',,85951289482#,,,,*123456#',
                                'platform': 'zoom'})
        with self.assertRaisesRegex(ValueError, 'not in the keys'):
            dial_in_details.resolve({'meetingId': '111222333'}, to='+14388097799,,85951289482#')
        # The Zoom link's own meeting ID is enough with a number.
        self.assertEqual(dial_in_details.resolve({'number': '+16469313860'}, to=ZOOM_LINK)['keys'],
                         ',,85951289482#')

    def test_what_a_dial_in_cannot_be(self):
        cases = [
            ({'meetingId': '123456789'}, None, 'dial-in number'),
            ({'number': '+16469313860'}, None, 'meeting ID'),
            ({'number': '6469313860', 'meetingId': '1234567'}, None, 'E.164'),
            ({'number': '+911', 'meetingId': '1234567'}, None, 'E.164|emergency'),
            ({'number': '+16469313860', 'meetingId': '12-ab'}, None, 'digits'),
            ({'invite': TEAMS_INVITE, 'passcode': '1234'}, None, 'no passcode'),
            ({'number': '+16469313860', 'keys': ',,12ab#'}, None, 'keys'),
            ({'number': '+16469313860', 'keys': ',' * 21 + '1#'}, None, 'keys'),
            ({'number': '+16469313860', 'meetingId': '1234567', 'platform': 'webex'}, None, 'platform'),
            ({'number': '+16469313860', 'meetingId': '1234567'}, 'https://acme.daily.co/room', 'Daily'),
            ({'number': '+16469313860', 'meetingId': '1234567', 'pin': '1'}, None, 'unknown'),
        ]
        for data, to, error in cases:
            with self.subTest(data=data), self.assertRaisesRegex(ValueError, error):
                dial_in_details.resolve(data, to=to)

    def test_the_bridge_announcements(self):
        for sentence, meaning in (
                ('Please wait, the meeting host will let you in soon.', 'waiting'),
                ('Please wait for the host to start this meeting.', 'waiting'),
                ('The meeting has not started yet.', 'waiting'),
                ('Please wait, someone in the meeting should let you in soon.', 'waiting'),
                ('You are in the lobby.', 'waiting'),
                ('The meeting ID you entered is invalid.', 'refused'),
                ('Invalid passcode, please try again.', 'refused'),
                ('This meeting has ended.', 'refused'),
                ('Please enter your participant ID followed by pound, or press pound to skip.', 'prompt'),
                ('Please say your name after the tone, then press pound.', 'prompt'),
                ('You are now in the meeting.', None),
                ('This meeting is being recorded.', None),
                ("Hi everyone, that number looks wrong to me.", None),
                ("Let's start with the launch plan.", None)):
            with self.subTest(sentence=sentence):
                self.assertEqual(dial_in_details.bridge_says(sentence), meaning)


class BridgeTests(unittest.TestCase):
    """Published meeting bridges keep no calling hours or call limits; any other number does."""

    def test_the_published_list_is_zooms_own_us_and_canada_numbers_with_their_source(self):
        import json
        import re
        data = json.loads(dial_in_details.BRIDGES_FILE.read_text(encoding='utf-8'))
        # Teams and Google Meet numbers belong to an organizer or a pool: they keep the phone rules.
        self.assertEqual(set(data) - {'about'}, {'zoom'})
        numbers = set()
        for group in data['zoom']:
            self.assertRegex(group['source'], r'^https://zoom\.us/u/\w+$')
            self.assertEqual(group['read'], '2026-11-05')
            for entry in group['numbers']:
                self.assertIn(entry['country'], ('US', 'CA'))
                self.assertIn(entry['kind'], ('toll', 'toll-free'))
                self.assertRegex(entry['number'], r'^\+1[2-9]\d{9}$')
                # The number is the page's own, as shown there.
                self.assertEqual(re.sub(r'\D', '', entry['shown'])[-10:], entry['number'][-10:])
                numbers.add(entry['number'])
        self.assertEqual(len(numbers), 32)
        self.assertLessEqual({'+16469313860', '+14388097799', '+16473744685', '+18335480276'}, numbers)
        self.assertNotIn(TEAMS_NUMBER, numbers)
        self.assertEqual(dial_in_details.published_bridges(), frozenset(numbers))

    def test_the_setting_extends_or_replaces_the_list(self):
        published = dial_in_details.published_bridges()
        bridges = dial_in_details.bridge_numbers
        self.assertEqual(bridges({}), published)
        self.assertEqual(bridges({'SMITLINE_DIAL_IN_BRIDGES': 'published, +1 514 555 0100'}),
                         published | {'+15145550100'})
        self.assertEqual(bridges({'SMITLINE_DIAL_IN_BRIDGES': '+15145550100'}), {'+15145550100'})
        self.assertEqual(bridges({'SMITLINE_DIAL_IN_BRIDGES': 'none'}), frozenset())
        # A mistake never makes a number a bridge: it only keeps numbers under the phone rules.
        self.assertEqual(bridges({'SMITLINE_DIAL_IN_BRIDGES': 'publishd, 15145550100, all'}), frozenset())
        self.assertTrue(dial_in_details.is_bridge('+16469313860', {}))
        self.assertFalse(dial_in_details.is_bridge('+16469313860', {'SMITLINE_DIAL_IN_BRIDGES': 'none'}))
        self.assertFalse(dial_in_details.is_bridge(TEAMS_NUMBER, {}))

    def test_a_missing_or_damaged_list_leaves_every_number_under_the_phone_rules(self):
        with tempfile.TemporaryDirectory() as temp:
            for name, content in (('missing.json', None), ('broken.json', 'not json'),
                                  ('odd.json', '{"zoom": [{"numbers": [{"number": "16469313860"}]}]}'),
                                  ('shape.json', '{"zoom": {"numbers": []}}')):
                path = Path(temp) / name
                if content is not None:
                    path.write_text(content, encoding='utf-8')
                with mock.patch.object(dial_in_details, 'BRIDGES_FILE', path):
                    self.assertEqual(dial_in_details.published_bridges(), frozenset(), name)
                    self.assertFalse(dial_in_details.is_bridge('+16469313860'), name)


class BriefTests(unittest.TestCase):
    def test_after_hours_is_for_phone_calls_and_meetings_with_a_dial_in(self):
        self.assertTrue(CallBrief.from_dict(meeting_brief(to=TEAMS_NUMBER + ',,123456789#',
                                                          afterHours=True)).after_hours)
        self.assertTrue(CallBrief.from_dict(meeting_brief(to=MEET_LINK, dialIn={'invite': MEET_INVITE},
                                                          afterHours=True)).after_hours)
        with self.assertRaises(ValueError) as refused:
            CallBrief.from_dict(meeting_brief(to=ZOOM_LINK, afterHours=True))
        self.assertIn('meetings with a dialIn', str(refused.exception))

    def test_to_can_be_the_dial_in_number_itself(self):
        brief = CallBrief.from_dict(meeting_brief(to='+14388097799,,85951289482#,,,,*123456#'))
        self.assertEqual(brief.to, '+14388097799')
        self.assertTrue(brief.joins_by_phone)
        self.assertIsNone(brief.platform)  # unknown: no link and no invitation
        self.assertEqual(brief.to_dict()['dialIn'], {
            'number': '+14388097799', 'meetingId': '85951289482', 'passcode': '123456',
            'keys': ',,85951289482#,,,,*123456#'})
        self.assertEqual(brief.max_minutes, 120)  # a meeting's

    def test_a_link_with_its_dial_in_keeps_the_link(self):
        brief = CallBrief.from_dict(meeting_brief(to=ZOOM_LINK, dialIn={'invite': ZOOM_INVITE}))
        self.assertEqual((brief.to, brief.platform, brief.joins_by_phone), (ZOOM_LINK, 'zoom', False))
        self.assertEqual(brief.dial_in['number'], '+14388097799')
        # The invitation text itself is not kept: the brief stores what will be dialed.
        self.assertNotIn('invite', brief.to_dict()['dialIn'])
        again = CallBrief.from_dict(brief.to_dict())
        self.assertEqual(again.to_dict(), brief.to_dict())

    def test_the_invitation_alone_is_enough(self):
        brief = CallBrief.from_dict(meeting_brief(dialIn={'invite': TEAMS_INVITE}))
        self.assertEqual((brief.to, brief.platform), ('+13238494874', 'teams'))
        brief = CallBrief.from_dict(meeting_brief(to='', dialIn={'invite': MEET_INVITE}))
        self.assertEqual((brief.to, brief.platform), ('+16505550123', 'meet'))

    def test_what_a_meeting_brief_refuses(self):
        cases = [
            ({'channel': 'phone', 'to': '+14155550142', 'dialIn': {'invite': ZOOM_INVITE}}, 'only for meetings'),
            ({'to': 'new', 'dialIn': {'invite': ZOOM_INVITE}}, 'already exists'),
            ({'to': 'https://example.com/meeting'}, 'dial-in number'),
            ({'to': '+911'}, 'E.164|emergency'),
            ({'to': '+14388097799'}, 'meeting ID'),
        ]
        for overrides, error in cases:
            with self.subTest(overrides=overrides), self.assertRaisesRegex(ValueError, error):
                CallBrief.from_dict(meeting_brief(**overrides))
        with self.assertRaises(ValueError) as wrong:
            CallBrief.from_dict(meeting_brief(to='https://example.com/meeting'))
        self.assertEqual(str(wrong.exception), MEETING_TO_ERROR)

    def test_a_meeting_brief_without_to_or_dial_in_still_asks_for_to(self):
        from call_brief import BriefIncomplete
        with self.assertRaises(BriefIncomplete) as missing:
            CallBrief.from_dict(meeting_brief())
        self.assertEqual(missing.exception.missing, ('to',))


# A meeting line, the real phone line, and the call service, with the provider and GPT-Live faked.

class Seats:
    """The meeting pool's capacity, as MeetingLine.ready reads it."""

    def __init__(self):
        self.full = False

    def capacity_problem(self, owner):
        return (409, 'capacity_exceeded', 'No meeting seat is free.') if self.full else None


class BrowserMeetings:
    """A daemon whose browser meetings the test scripts: their states, then the handoff."""

    def __init__(self):
        self.supervisor = Seats()
        self.created = []
        self.states = ['joining', 'ended']
        self.handoff = {'summary': '', 'endReason': 'authentication_required'}

    async def create_meeting(self, payload, owner='local'):
        self.created.append(payload)
        return SimpleNamespace(id='mtg-1', platform='meet', state='joining')

    async def get_meeting(self, meeting_id, owner=None):
        state = self.states.pop(0) if len(self.states) > 1 else self.states[0]
        return SimpleNamespace(id=meeting_id, state=state)

    async def get_handoff(self, meeting_id, owner=None):
        return dict(self.handoff)

    async def cancel_meeting(self, meeting_id):
        self.states = ['ended']


class MeetingSummarizer:
    briefs = []
    facts = []

    async def summarize(self, brief, transcript, *, duration_seconds=0, facts=()):
        MeetingSummarizer.briefs.append(brief)
        MeetingSummarizer.facts.append(list(facts))
        return {'outcome': 'achieved', 'summary': 'Took notes.', 'details': [], 'decisions': [],
                'actionItems': [], 'openQuestions': [], 'transcript': transcript,
                'durationSeconds': duration_seconds, 'source': 'summary_model'}


class DialHarness:
    def __init__(self, temp, env=None, flavor='twilio', daemon=None):
        self.env = dict(ENV, **(env or {}))
        self.twilio = FakeTwilio()
        self.twilio.flavor = flavor
        self.store = CallStore(Path(temp) / 'calls')
        # Daytime where every test number rings; a test sets NIGHT to be outside calling hours.
        self.now = DAY
        self.hooks = DefaultCallHooks(environ=self.env, store=self.store, clock=lambda: self.now)

        async def public_url():
            return PUBLIC
        self.phone = PhoneLine(public_url=public_url, environ=lambda: self.env,
                               twilio_factory=lambda creds: self.twilio, live_factory=FakeLive,
                               status_grace=0.2)
        self.daemon = daemon or BrowserMeetings()

        async def no_sleep(_seconds):
            await asyncio.sleep(0.005)
        self.meeting = MeetingLine(self.daemon, sleep=no_sleep, phone=self.phone, handoff_timeout=1)
        self.service = CallService(self.store, hooks=self.hooks,
                                   lines={'phone': self.phone, 'meeting': self.meeting},
                                   summarizer_factory=lambda owner: MeetingSummarizer(), environ={},
                                   shutdown_grace=0.2)

    async def join(self, **overrides):
        record = await self.service.create(meeting_brief(**overrides))
        await until(lambda: self.twilio.created and self.phone.session(record['id']))
        return record, self.phone.session(record['id'])

    async def connect(self, record, session):
        ws = FakeTwilioSocket()
        claimed = self.phone.claim(record['id'], session.token)
        assert claimed is session
        task = asyncio.create_task(session.run(ws, {'streamSid': 'MZ1', 'callSid': 'CA123'}))
        await until(lambda: FakeLive.instances and FakeLive.instances[-1] is session.live)
        session.live.push({'type': 'session.started', 'session': {'id': 'sess_1'}})
        await until(lambda: session.live.started.is_set())
        return ws, session.live, task

    def status(self, record):
        return self.store.get(record['id'])['status']

    def events(self, record, kind):
        return [event['data'] for event in self.service.events(record['id']) if event['type'] == kind]


def said(live, text, *, meeting=True):
    kind = 'session.input_transcript.delta' if meeting else 'session.output_transcript.delta'
    live.push({'type': kind, 'delta': text})


FAST = {'WATCH_EVERY': 0.02, 'ADMIT_SETTLE': 0.3, 'SENTENCE_PAUSE': 0.1,
        'INTRO_QUIET_SECONDS': 0.2, 'LEAVE_QUIET_SECONDS': 0.1}


class DialInCallTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        FakeLive.instances = []
        MeetingSummarizer.briefs = []
        MeetingSummarizer.facts = []
        self.temp = tempfile.TemporaryDirectory()
        # Meetings with a link join by phone here too (hosted, until each gets a VM of its own).
        self.h = DialHarness(self.temp.name, env={'SMITLINE_MEETING_DIAL_IN_FIRST': 'all'})
        self.patches = [mock.patch.object(dial_in, name, value) for name, value in FAST.items()]
        for patch in self.patches:
            patch.start()

    async def asyncTearDown(self):
        for patch in self.patches:
            patch.stop()
        await self.h.service.shutdown()
        self.temp.cleanup()

    async def test_the_provider_keys_in_the_meeting_with_its_own_dtmf_before_the_voice_comes_on(self):
        for flavor in ('twilio', 'signalwire'):
            with self.subTest(flavor=flavor):
                self.h.twilio.flavor = flavor
                self.h.twilio.created = []
                record, session = await self.h.join(to=ZOOM_LINK, dialIn={'invite': ZOOM_INVITE},
                                                    maxMinutes=30)
                created = self.h.twilio.created[0]
                self.assertEqual((created['to'], created['from_']), ('+14388097799', '+15005550006'))
                twiml = created['twiml']
                # <Play digits> first: each comma of the one-tap number is two seconds of w.
                self.assertIn('<Play digits="wwwwwwww85951289482#wwwwwwwwwwwwwwww*123456#"/>'
                              '<Connect><Stream url="wss://abc-123.trycloudflare.com/twilio/media"',
                              twiml)
                self.assertIn(session.token, twiml)
                # No answering-machine check (a bridge is a machine), and never recorded.
                self.assertIsNone(created.get('amd_callback'))
                self.assertFalse(created.get('record'))
                # The provider's own limit covers the keys, ten minutes of waiting, and the meeting.
                self.assertGreater(created['time_limit'], 30 * 60 + 600)
                line = self.h.store.get(record['id'])['line']
                self.assertEqual((line['joinedBy'], line['platform'], line['providerCallSid']),
                                 ('phone', 'zoom', 'CA123'))
                self.assertEqual(self.h.events(record, 'call.dial_in'), [
                    {'reason': 'dial_in_first', 'number': '+14388097799', 'platform': 'zoom'}])
                await self.h.service.end(record['id'], owner='local')
                await self.h.service.wait(record['id'], timeout=5, owner='local')

    async def test_a_meeting_with_only_its_number_joins_by_phone(self):
        record, session = await self.h.join(to='+14388097799,,85951289482#,,,,*123456#')
        self.assertEqual(self.h.events(record, 'call.dial_in'),
                         [{'reason': 'no_link', 'number': '+14388097799'}])
        self.assertEqual(self.h.daemon.created, [])  # no browser meeting
        self.assertEqual(self.h.status(record), 'connecting')

    async def test_the_voice_is_the_meeting_voice_on_the_phone_line(self):
        record, session = await self.h.join(to=ZOOM_LINK, dialIn={'invite': ZOOM_INVITE},
                                            canDo='look things up', voice='quartz')
        ws, live, task = await self.h.connect(record, session)
        config = live.config
        self.assertEqual(config['audio']['format'], {'type': 'audio/pcmu', 'rate': 8000})
        self.assertEqual(config['audio']['output'], {'voice': 'quartz'})
        instructions = config['instructions']
        # The participation policy browser meetings use, word for word, and the phone line's facts.
        from meeting_voice import PARTICIPATION_POLICY
        self.assertIn(PARTICIPATION_POLICY, instructions)
        self.assertIn("on the meeting's phone line", instructions)
        self.assertIn('cannot see anything or press keys', instructions)
        self.assertIn("Smith's AI assistant", instructions)
        self.assertIn('say it', instructions)  # nobody sees a tile: it says it is an AI
        self.assertNotIn('calling on behalf of', instructions)  # not a phone call's opening
        tools = [tool.get('name') for tool in config['delegation']['responses']['tools']]
        self.assertEqual(tools, ['leave_meeting', 'ask_agent'])
        self.assertIn('Take notes on the launch plan', config['delegation']['responses']['instructions'])
        # Joined by phone, it is not yet in the meeting: the bridge says when.
        self.assertEqual(self.h.status(record), 'connecting')
        ws.push({'event': 'stop'})
        await asyncio.wait_for(task, 3)

    async def test_waiting_to_be_let_in_is_not_meeting_time(self):
        record, session = await self.h.join(to=ZOOM_LINK, dialIn={'invite': ZOOM_INVITE})
        ws, live, task = await self.h.connect(record, session)
        said(live, 'Please wait, the meeting host will let you in soon.')
        await until(lambda: self.h.status(record) == 'waiting')
        self.assertEqual(self.h.events(record, 'call.waiting'), [{}])
        await asyncio.sleep(0.5)  # longer than ADMIT_SETTLE: still waiting
        self.assertEqual(self.h.status(record), 'waiting')
        # Nothing is cued while it waits.
        self.assertFalse(any(kind == 'session.commentary.append' for kind, _ in live.appends))
        session.started_at -= 120  # two minutes in the waiting room
        session.waiting_since -= 120
        said(live, "Hi Smith's assistant, welcome. Let's begin.")
        await until(lambda: self.h.status(record) == 'in_progress')
        # The hello at the first pause: the spoken disclosure, in Arun's words.
        await until(lambda: any(kind == 'session.commentary.append' for kind, _ in live.appends))
        cue = next(text for kind, text in live.appends if kind == 'session.commentary.append')
        self.assertIn("Hey, this is Smith's AI assistant. I'm here to answer any questions.", cue)
        said(live, "Hey, this is Smith's AI assistant. I'm here to answer any questions.", meeting=False)
        await until(lambda: session.disclosed.is_set())
        ws.push({'event': 'stop'})  # the host ends the meeting
        await asyncio.wait_for(task, 3)
        self.h.phone.on_status(record['id'], {'CallStatus': 'completed', 'CallDuration': '150'})
        done = await self.h.service.wait(record['id'], timeout=5, owner='local')
        self.assertEqual((done['channel'], done['status'], done['endReason']),
                         ('meeting', 'completed', 'meeting_ended'))
        # Billed from when it was let in, not for the two minutes in the waiting room.
        self.assertLess(done['usage']['voiceSeconds'], 5)
        self.assertEqual(done['usage']['phoneSeconds'], 150)
        self.assertEqual({entry['speaker'] for entry in done['result']['transcript']}, {'meeting', 'agent'})
        self.assertEqual(MeetingSummarizer.briefs[-1]['channel'], 'meeting')
        # Its cost has the phone line, estimated until the provider reports its price.
        self.assertEqual([item['kind'] for item in done['cost']['items']][:2], ['phone', 'voice'])
        self.assertEqual(done['cost']['phone'], round(0.014 * 3, 6))

    async def test_a_quiet_meeting_is_joined_from_the_start(self):
        record, session = await self.h.join(to='+14388097799,,85951289482#')
        ws, live, task = await self.h.connect(record, session)
        await until(lambda: self.h.status(record) == 'in_progress')
        self.assertEqual(session.live_at, session.started_at)
        ws.push({'event': 'stop'})
        await asyncio.wait_for(task, 3)
        done = await self.h.service.wait(record['id'], timeout=5, owner='local')
        # Nobody spoke: the result says so, as a meeting's.
        self.assertEqual(done['result']['outcome'], 'not_reached')
        self.assertIn('nobody spoke', done['result']['summary'])

    async def test_the_hello_waits_for_a_pause_and_is_not_said_twice(self):
        record, session = await self.h.join(to='+14388097799,,85951289482#')
        ws, live, task = await self.h.connect(record, session)
        said(live, "Okay so the first item is the budget, and I think we should")
        talking = asyncio.create_task(self._keep_talking(ws, 0.6, then_quiet=3.0))
        await until(lambda: self.h.status(record) == 'in_progress')
        await asyncio.sleep(0.4)
        self.assertFalse(any(kind == 'session.commentary.append' for kind, _ in live.appends))
        await until(lambda: any(kind == 'session.commentary.append' for kind, _ in live.appends))
        self.assertEqual(self.h.events(record, 'call.disclosure_cued'), [{'sent': True}])
        talking.cancel()
        ws.push({'event': 'stop'})
        await asyncio.wait_for(task, 3)

    async def _keep_talking(self, ws, seconds, then_quiet=0.0):
        """Their audio, 20 ms at a time: speech for `seconds`, then quiet, as a phone line sends."""
        loud = base64.b64encode(b'\x9a' * 160).decode()
        quiet = base64.b64encode(b'\xff' * 160).decode()
        loop = asyncio.get_running_loop()
        start = loop.time()
        while loop.time() < start + seconds + then_quiet:
            payload = loud if loop.time() < start + seconds else quiet
            ws.push({'event': 'media', 'media': {'track': 'inbound', 'payload': payload}})
            await asyncio.sleep(0.02)

    async def test_answering_before_the_hello_is_the_disclosure(self):
        record, session = await self.h.join(to='+14388097799,,85951289482#')
        ws, live, task = await self.h.connect(record, session)
        said(live, 'Smitline, are you there?')
        await until(lambda: self.h.status(record) == 'in_progress')
        said(live, "Yes, I'm Smith's AI assistant, here to take notes.", meeting=False)
        await until(lambda: session.disclosed.is_set())
        await asyncio.sleep(0.5)
        self.assertEqual(self.h.events(record, 'call.disclosure_cued'), [])
        ws.push({'event': 'stop'})
        await asyncio.wait_for(task, 3)

    async def test_what_it_said_in_the_waiting_room_did_not_reach_the_meeting(self):
        record, session = await self.h.join(to='+14388097799,,85951289482#')
        ws, live, task = await self.h.connect(record, session)
        said(live, 'Please wait, the meeting host will let you in soon.')
        await until(lambda: self.h.status(record) == 'waiting')
        said(live, "Okay, I'm Smith's AI assistant, waiting to be let in.", meeting=False)
        await until(lambda: session.disclosed.is_set())
        said(live, 'Welcome, everyone.')
        await until(lambda: self.h.status(record) == 'in_progress')
        await until(lambda: self.h.events(record, 'call.disclosure_cued'), timeout=5)
        ws.push({'event': 'stop'})
        await asyncio.wait_for(task, 3)

    async def test_a_refused_meeting_id_ends_the_call_at_once(self):
        record, session = await self.h.join(to='+14388097799,,85951289482#')
        ws, live, task = await self.h.connect(record, session)
        said(live, 'The meeting ID you entered is invalid. Please try again.')
        await until(lambda: ws.closed)
        await asyncio.wait_for(task, 3)
        self.assertIn(('CA123', {'status': 'completed'}), self.h.twilio.updates)
        done = await self.h.service.wait(record['id'], timeout=5, owner='local')
        self.assertEqual(done['endReason'], 'dial_in_refused')
        self.assertEqual(self.h.events(record, 'call.dial_in_refused'),
                         [{'said': 'The meeting ID you entered is invalid.'}])
        self.assertEqual(done['usage']['voiceSeconds'], 0)  # never in the meeting
        self.assertEqual(self.h.events(record, 'call.disclosure_cued'), [])

    async def test_nobody_lets_it_in(self):
        record, session = await self.h.join(to='+14388097799,,85951289482#')
        ws, live, task = await self.h.connect(record, session)
        said(live, 'Please wait for the host to start this meeting.')
        await until(lambda: self.h.status(record) == 'waiting')
        with mock.patch.object(dial_in, 'WAITING_LIMIT', 0.2):
            await until(lambda: ws.closed)
        await asyncio.wait_for(task, 3)
        done = await self.h.service.wait(record['id'], timeout=5, owner='local')
        self.assertEqual(done['endReason'], 'not_admitted')
        self.assertEqual(done['usage']['voiceSeconds'], 0)
        self.assertEqual(len(self.h.events(record, 'call.not_admitted')), 1)

    async def test_leave_meeting_hangs_up_and_says_why(self):
        record, session = await self.h.join(to='+14388097799,,85951289482#')
        ws, live, task = await self.h.connect(record, session)
        said(live, "Let's start.")
        await until(lambda: self.h.status(record) == 'in_progress')
        live.push({'type': 'response.event', 'delegation_id': 'd1', 'event': {
            'type': 'response.output_item.done', 'item': {
                'type': 'function_call', 'call_id': 'fc1', 'name': 'leave_meeting',
                'arguments': '{"reason": "completed"}'}}})
        await until(lambda: ws.closed)
        await asyncio.wait_for(task, 3)
        self.assertEqual(live.outputs, [])  # leaving needs no tool result
        done = await self.h.service.wait(record['id'], timeout=5, owner='local')
        self.assertEqual(done['endReason'], 'meeting_ended')
        self.assertEqual(self.h.events(record, 'call.agent_left'), [{'reason': 'left_completed'}])

    async def test_asked_to_leave_it_goes_after_its_goodbye(self):
        record, session = await self.h.join(to='+14388097799,,85951289482#')
        ws, live, task = await self.h.connect(record, session)
        said(live, 'Thanks Smitline, you can drop off now.')
        await until(lambda: self.h.status(record) == 'in_progress')
        said(live, 'Sure, bye everyone!', meeting=False)
        # It spoke but never said it is an AI (asked to go before the hello): it says so first.
        await until(lambda: any('have not yet told them' in text for _, text in live.appends), timeout=5)
        self.assertFalse(ws.closed)
        said(live, "Oh, and just so you know, I'm Smith's AI assistant.", meeting=False)
        await until(lambda: ws.closed, timeout=5)
        await asyncio.wait_for(task, 3)
        self.assertEqual(self.h.events(record, 'call.agent_left'), [{'reason': 'left_asked_to_leave'}])
        self.assertEqual(self.h.events(record, 'call.disclosure'), [{'verified': True}])

    async def test_a_goodbye_to_someone_else_does_not_end_it(self):
        judge = mock.AsyncMock(return_value=(False, 'other'))
        record, session = await self.h.join(to='+14388097799,,85951289482#')
        session.judge.decide = judge
        ws, live, task = await self.h.connect(record, session)
        said(live, "I have to run, bye everyone.")
        await until(lambda: self.h.status(record) == 'in_progress')
        said(live, 'Bye Priya, talk soon!', meeting=False)
        await until(lambda: judge.await_count == 1, timeout=5)
        await asyncio.sleep(0.3)
        self.assertFalse(ws.closed)
        ws.push({'event': 'stop'})
        await asyncio.wait_for(task, 3)

    async def test_a_bridge_asking_for_a_name_after_the_tone_is_not_a_voicemail(self):
        record, session = await self.h.join(dialIn={'invite': TEAMS_INVITE})
        ws, live, task = await self.h.connect(record, session)
        said(live, 'Please say your name after the tone, then press pound.')
        await asyncio.sleep(0.15)
        self.assertFalse(session.voicemail_heard)
        self.assertEqual(self.h.events(record, 'call.voicemail_heard'), [])
        self.assertEqual(self.h.status(record), 'connecting')  # a prompt is not people
        ws.push({'event': 'stop'})
        await asyncio.wait_for(task, 3)

    async def test_max_minutes_count_from_when_it_is_in(self):
        record, session = await self.h.join(to='+14388097799,,85951289482#', maxMinutes=1)
        ws, live, task = await self.h.connect(record, session)
        said(live, "Let's start.")
        await until(lambda: self.h.status(record) == 'in_progress')
        session.live_at -= 61
        await until(lambda: ws.closed, timeout=5)
        await asyncio.wait_for(task, 3)
        done = await self.h.service.wait(record['id'], timeout=5, owner='local')
        self.assertEqual(done['endReason'], 'max_duration')
        self.assertEqual(len(self.h.events(record, 'call.max_duration')), 1)

    async def test_it_leaves_when_nobody_has_spoken_for_the_idle_limit(self):
        self.h.env['SMITLINE_MEETING_IDLE_LEAVE_SECONDS'] = '1'
        record, session = await self.h.join(to='+14388097799,,85951289482#')
        ws, live, task = await self.h.connect(record, session)
        said(live, "Let's start.")
        await until(lambda: ws.closed, timeout=5)
        await asyncio.wait_for(task, 3)
        self.assertEqual(self.h.events(record, 'call.agent_left'), [{'reason': 'idle'}])

    async def test_the_agent_follows_and_steers_it_like_any_meeting(self):
        self.h.env['SMITLINE_LISTEN'] = '1'
        record, session = await self.h.join(to='+14388097799,,85951289482#')
        ws, live, task = await self.h.connect(record, session)
        said(live, "Let's start.")
        await until(lambda: self.h.status(record) == 'in_progress')
        answer = await self.h.service.instruct(record['id'], 'The budget is $40k.', owner='local')
        self.assertEqual(answer, {'delivered': True})
        self.assertIn(('session.instructions.append', "From Smith's agent: The budget is $40k."),
                      live.appends)
        self.assertIs(self.h.service.listen(record['id'], owner='local'), session.tap)
        with self.assertRaises(CallError) as refused:
            await self.h.service.transfer(record['id'], owner='local')
        self.assertEqual(refused.exception.code, 'unsupported')
        # end_call leaves the meeting, as for any meeting.
        await self.h.service.end(record['id'], owner='local')
        await until(lambda: ws.closed)
        await asyncio.wait_for(task, 3)
        done = await self.h.service.wait(record['id'], timeout=5, owner='local')
        self.assertEqual(done['endReason'], 'canceled')

    async def test_busy_and_unanswered_numbers_are_not_reached(self):
        for status, reason, words in (('busy', 'busy', 'was busy'),
                                      ('no-answer', 'no_answer', 'did not answer')):
            with self.subTest(status=status):
                record, _session = await self.h.join(to='+14388097799,,85951289482#')
                self.h.phone.on_status(record['id'], {'CallStatus': status, 'CallDuration': '0'})
                done = await self.h.service.wait(record['id'], timeout=5, owner='local')
                self.assertEqual((done['status'], done['endReason']), ('completed', reason))
                self.assertEqual(done['result']['outcome'], 'not_reached')
                self.assertIn(words, done['result']['summary'])
                self.assertEqual(done['usage']['voiceSeconds'], 0)

    async def test_the_provider_price_of_a_meeting_joined_by_phone_is_looked_up(self):
        self.h.twilio.price = '-0.04200'
        record = {'channel': 'meeting', 'line': {'joinedBy': 'phone', 'providerCallSid': 'CA9'}}
        price = await self.h.meeting.provider_price(lambda provider: {}, record)
        self.assertEqual(price, {'amount': 0.042, 'currency': 'USD'})
        browser = {'channel': 'meeting', 'line': {'meetingId': 'mtg-1'}}
        self.assertIsNone(await self.h.meeting.provider_price(lambda provider: {}, browser))


class WayInTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        FakeLive.instances = []
        self.temp = tempfile.TemporaryDirectory()
        self.h = DialHarness(self.temp.name)

    async def asyncTearDown(self):
        await self.h.service.shutdown()
        self.temp.cleanup()

    def way(self, brief, settings=None):
        brief = CallBrief.from_dict(brief)
        return dial_in.way_in(self.h.meeting, settings or {}, 'local', brief,
                              lambda provider: self.h.hooks.credentials('local', provider), self.h.hooks)

    def test_a_link_meeting_dials_in_only_while_its_number_may_be_dialed(self):
        meet = meeting_brief(to=MEET_LINK, dialIn={'invite': MEET_INVITE})
        first = {'SMITLINE_MEETING_DIAL_IN_FIRST': 'all'}
        self.assertEqual(self.way(meet, first), ('phone', 'dial_in_first'))
        self.h.daemon.supervisor.full = True
        self.assertEqual(self.way(meet), ('phone', 'no_seat'))
        # Midnight where the Google Meet number rings: the meeting keeps to its link.
        self.h.now = NIGHT
        self.assertEqual(self.way(meet, first), ('link', None))
        self.assertEqual(self.way(meet), ('link', None))
        with self.assertRaises(CallRefused) as refused:
            self.h.meeting.ready(self.h.hooks, 'local', CallBrief.from_dict(meet))
        self.assertEqual(refused.exception.code, 'capacity_exceeded')
        # Zoom's own numbers keep no calling hours.
        zoom = meeting_brief(to=ZOOM_LINK, dialIn={'invite': ZOOM_INVITE})
        self.assertEqual(self.way(zoom), ('phone', 'no_seat'))
        self.assertEqual(self.way(zoom, first), ('phone', 'dial_in_first'))

    async def test_a_number_only_meeting_outside_calling_hours_is_refused_before_it_rings(self):
        self.h.now = NIGHT
        with self.assertRaises(CallError) as refused:
            await self.h.service.create(meeting_brief(to=TEAMS_NUMBER + ',,123456789#'))
        self.assertEqual(refused.exception.code, 'outside_calling_hours')
        self.assertIn('It can dial in at 8:00 AM there, in about 8 hours.', refused.exception.message)
        self.assertEqual(self.h.twilio.created, [])
        self.assertEqual(self.h.store.list(owner='local', limit=None), [])
        # The user confirms the meeting is on now: it dials.
        record = await self.h.service.create(meeting_brief(to=TEAMS_NUMBER + ',,123456789#', afterHours=True))
        await until(lambda: self.h.twilio.created)
        self.assertEqual(self.h.twilio.created[0]['to'], TEAMS_NUMBER)
        await self.h.service.end(record['id'], owner='local')
        await self.h.service.wait(record['id'], timeout=5, owner='local')

    async def test_a_browser_turned_away_outside_calling_hours_does_not_dial_and_says_why(self):
        self.h.now = NIGHT
        # Dialing in first waits for the calling hours too: until then the link is the way in.
        self.h.env['SMITLINE_MEETING_DIAL_IN_FIRST'] = 'all'
        record = await self.h.service.create(meeting_brief(to=MEET_LINK, dialIn={'invite': MEET_INVITE}))
        done = await self.h.service.wait(record['id'], timeout=5, owner='local')
        self.assertEqual(len(self.h.daemon.created), 1)  # it went by the link
        self.assertEqual(self.h.twilio.created, [])
        self.assertEqual(self.h.events(record, 'call.dial_in_skipped'), [{'code': 'outside_calling_hours'}])
        summary = done['result']['summary']
        self.assertIn('Smitline did not join by phone instead: +16505550123 is not a meeting', summary)
        self.assertIn('It can dial in at 8:00 AM there', summary)
        # The agent reads why, and what it can do: the phone rules' own words (afterHours, when).
        self.assertEqual(done['result']['outcome'], 'not_reached')
        self.assertIn('did not join by phone instead', done['result']['reason'])
        self.assertIn('It can dial in at 8:00 AM there', done['result']['hint'])
        self.assertIn('afterHours', done['result']['hint'])

    async def test_a_link_turned_away_without_dial_in_says_why_and_how_to_join_by_phone(self):
        # What a meeting's handoff says when Google refuses the guest browser right after Join.
        self.h.daemon.handoff = {
            'summary': 'Take notes on the launch plan Meeting ended (authentication_required) with 0 '
                       'transcript entries (partial).',
            'endReason': 'authentication_required', 'joinRefused': 'guest_refused'}
        record = await self.h.service.create(meeting_brief(to=MEET_LINK))
        done = await self.h.service.wait(record['id'], timeout=5, owner='local')
        result = done['result']
        self.assertEqual((done['status'], result['outcome']), ('completed', 'not_reached'))
        self.assertEqual(result['reason'], 'Google Meet did not let Smitline in as a guest.')
        # The summary says what happened, never the objective as if it were done.
        self.assertEqual(result['summary'], result['reason'])
        self.assertIn('Join by phone number and PIN', result['hint'])
        self.assertIn('as dialIn and without the link', result['hint'])
        self.assertEqual(self.h.twilio.created, [])
        self.assertEqual(self.h.events(record, 'call.join_refused'),
                         [{'platform': 'meet', 'kind': 'guest_refused'}])
        self.assertEqual(self.h.events(record, 'call.dial_in_skipped'), [])

    async def test_any_platform_that_turns_the_link_away_is_named_with_its_phone_details(self):
        self.h.daemon.handoff = {'summary': '', 'endReason': 'authentication_required',
                                 'joinRefused': 'restricted'}
        record = await self.h.service.create(meeting_brief(
            to='https://teams.microsoft.com/l/meetup-join/abc?context=x'))
        result = (await self.h.service.wait(record['id'], timeout=5, owner='local'))['result']
        self.assertEqual(result['reason'], 'Microsoft Teams lets only invited people into this '
                         'meeting, so Smitline could not join from the link.')
        self.assertIn('the phone number and phone conference ID', result['hint'])
        self.assertEqual(self.h.events(record, 'call.join_refused'),
                         [{'platform': 'teams', 'kind': 'restricted'}])

    async def test_a_link_turned_away_where_this_installation_cannot_dial_in(self):
        for key in ('TWILIO_ACCOUNT_SID', 'TWILIO_AUTH_TOKEN'):
            self.h.env.pop(key)
        problem = dial_in.phone_problem(self.h.phone, lambda name: self.h.hooks.credentials('local', name))
        self.assertIsNotNone(problem)
        self.h.daemon.handoff = {'summary': '', 'endReason': 'authentication_required',
                                 'joinRefused': 'guest_refused'}
        # With the dial-in: it could not dial, and says why.
        record = await self.h.service.create(meeting_brief(to=MEET_LINK, dialIn={'invite': MEET_INVITE}))
        result = (await self.h.service.wait(record['id'], timeout=5, owner='local'))['result']
        self.assertIn('this installation cannot dial in', result['reason'])
        self.assertEqual(result['hint'], problem.message)
        # Without it: the hint asks for the dial-in and says what joining by phone needs here.
        record = await self.h.service.create(meeting_brief(to=MEET_LINK))
        result = (await self.h.service.wait(record['id'], timeout=5, owner='local'))['result']
        self.assertTrue(result['hint'].startswith('Ask the user for the Join by phone number'))
        self.assertTrue(result['hint'].endswith(problem.message))
        self.assertEqual(self.h.twilio.created, [])

    def test_the_way_in(self):
        linked = meeting_brief(to=ZOOM_LINK, dialIn={'invite': ZOOM_INVITE})
        self.assertEqual(self.way(meeting_brief(to=ZOOM_LINK)), ('link', None))
        self.assertEqual(self.way(linked), ('link', None))
        self.assertEqual(self.way(meeting_brief(to='+14388097799,,85951289482#')), ('phone', 'no_link'))
        for setting in ('zoom', 'meet, zoom', 'all', 'ALL'):
            self.assertEqual(self.way(linked, {'SMITLINE_MEETING_DIAL_IN_FIRST': setting}),
                             ('phone', 'dial_in_first'), setting)
        self.assertEqual(self.way(linked, {'SMITLINE_MEETING_DIAL_IN_FIRST': 'teams,meet'}), ('link', None))
        self.h.daemon.supervisor.full = True
        self.assertEqual(self.way(linked), ('phone', 'no_seat'))
        # Without phone calls set up, a full pool stays a capacity refusal.
        for key in ('TWILIO_ACCOUNT_SID', 'TWILIO_AUTH_TOKEN'):
            self.h.env.pop(key)
        self.assertEqual(self.way(linked), ('link', None))
        with self.assertRaises(CallRefused) as refused:
            self.h.meeting.ready(self.h.hooks, 'local', CallBrief.from_dict(linked))
        self.assertEqual(refused.exception.code, 'capacity_exceeded')
        with self.assertRaises(CallRefused) as refused:
            self.h.meeting.ready(self.h.hooks, 'local',
                                 CallBrief.from_dict(meeting_brief(to='+14388097799,,85951289482#')))
        self.assertEqual(refused.exception.code, 'not_configured')

    async def test_a_line_without_phone_calls_refuses_a_number_only_meeting(self):
        line = MeetingLine(self.h.daemon)
        with self.assertRaises(CallRefused) as refused:
            line.ready(self.h.hooks, 'local',
                       CallBrief.from_dict(meeting_brief(to='+14388097799,,85951289482#')))
        self.assertEqual(refused.exception.code, 'dial_in_unavailable')

    async def test_a_platform_that_turns_the_browser_away_is_joined_by_phone(self):
        record = await self.h.service.create(meeting_brief(
            to='https://meet.google.com/abc-defg-hij', dialIn={'invite': MEET_INVITE}))
        await until(lambda: self.h.twilio.created and self.h.phone.session(record['id']), timeout=5)
        self.assertEqual(len(self.h.daemon.created), 1)  # it tried the browser first
        self.assertEqual(self.h.twilio.created[0]['to'], '+16505550123')
        self.assertEqual(self.h.events(record, 'call.dial_in'),
                         [{'reason': 'platform_refused', 'number': '+16505550123', 'platform': 'meet'}])
        # The refusal is on the call; a handoff from before the refusal kinds names none.
        self.assertEqual(self.h.events(record, 'call.join_refused'), [{'platform': 'meet'}])
        session = self.h.phone.session(record['id'])
        await self.h.service.end(record['id'], owner='local')
        done = await self.h.service.wait(record['id'], timeout=5, owner='local')
        self.assertTrue(session.canceled)
        # It went in by phone instead, so the result has nothing to explain.
        self.assertFalse({'reason', 'hint'} & set(done['result']))

    async def test_a_meeting_never_admitted_does_not_dial_in(self):
        # Not being let in is the host's choice, not the platform refusing the browser.
        self.h.daemon.handoff = {'summary': '', 'endReason': 'needs_attention'}
        record = await self.h.service.create(meeting_brief(
            to='https://meet.google.com/abc-defg-hij', dialIn={'invite': MEET_INVITE}))
        done = await self.h.service.wait(record['id'], timeout=5, owner='local')
        self.assertEqual(done['status'], 'completed')
        self.assertEqual(self.h.twilio.created, [])

    async def test_a_brief_check_reports_the_dial_in(self):
        checked = await self.h.service.check(meeting_brief(dialIn={'invite': ZOOM_INVITE}))
        self.assertTrue(checked['ok'], checked['problems'])
        self.assertEqual(checked['brief']['to'], '+14388097799')
        self.assertEqual(checked['brief']['dialIn']['keys'], ',,85951289482#,,,,*123456#')
        self.assertEqual(checked['warnings'], [])

    async def test_a_brief_check_warns_of_a_google_meet_link_without_its_dial_in(self):
        checked = await self.h.service.check(meeting_brief(to=MEET_LINK))
        # A warning never stops the call.
        self.assertEqual((checked['ok'], checked['problems']), (True, []))
        self.assertEqual(checked['warnings'], [dial_in.MEET_WITHOUT_DIAL_IN])
        self.assertIn('dialIn', checked['warnings'][0])
        for quiet in (meeting_brief(to=MEET_LINK, dialIn={'invite': MEET_INVITE}),
                      meeting_brief(to=ZOOM_LINK), meeting_brief(dialIn={'invite': MEET_INVITE})):
            with self.subTest(brief=quiet):
                self.assertEqual((await self.h.service.check(quiet))['warnings'], [])
        # Where Smitline could not dial in anyway, the dial-in would not help: no warning.
        for key in ('TWILIO_ACCOUNT_SID', 'TWILIO_AUTH_TOKEN'):
            self.h.env.pop(key)
        self.assertEqual((await self.h.service.check(meeting_brief(to=MEET_LINK)))['warnings'], [])

    async def test_a_phone_call_check_has_no_warnings(self):
        from test_phone import brief as phone_brief
        checked = await self.h.service.check(phone_brief())
        self.assertEqual(checked['warnings'], [])


class MeetingMachineTests(unittest.IsolatedAsyncioTestCase):
    """With the meeting router: Zoom, Teams and Meet meetings on machines of their own from a
    launcher plugin, rooms in this computer's pool, one meeting at a time. A meeting joined by
    phone takes neither a machine nor a seat, so it still joins when every seat is taken."""

    async def asyncSetUp(self):
        from meeting_supervisor import MeetingPool, MeetingRouter, RemoteMeetingPool
        from runtime_daemon import RuntimeDaemon
        from test_meeting_pool import ComposeRunner
        from test_meeting_supervisor import FakeHealth, FakeLauncher
        from test_remote_meetings import FakeProvider, owner_environment
        FakeLive.instances = []
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        runtime = root / 'meeting-runtime'
        runtime.mkdir()
        self.containers = []

        def container(project, env):
            self.containers.append(FakeLauncher())
            return self.containers[-1]
        local = MeetingPool(root, runtime_root=runtime, runner=ComposeRunner(), environ={},
                            max_meetings=1, ports=(18100, 18119), poll_interval=0.02,
                            start_timeout=0.4, stop_timeout=0.4, launcher_factory=container,
                            health_factory=lambda port: FakeHealth(), port_free=lambda port: True)
        self.provider = FakeProvider()
        remote = RemoteMeetingPool(root, launcher=self.provider, runtime_root=runtime, max_meetings=1,
                                   poll_interval=0.02, start_timeout=30.0, stop_timeout=1.0,
                                   archive_timeout=1.0)
        self.router = MeetingRouter(local, remote, remote_platforms={'zoom', 'teams', 'meet'},
                                    max_meetings=1)
        self.router.owner_environment = owner_environment
        self.daemon = RuntimeDaemon(root / 'daemon', supervisor=self.router)
        self.router.bind_daemon(self.daemon)
        self.h = DialHarness(self.temp.name, daemon=self.daemon)

    async def asyncTearDown(self):
        await self.h.service.shutdown()
        for meeting_id in self.daemon.meetings.list_ids():
            if self.daemon.meetings.get(meeting_id).session.state != 'ended':
                await self.daemon.cancel_meeting(meeting_id)
        await until(lambda: not self.daemon._start_tasks)
        await self.router.shutdown()
        for machine in self.provider.machines.values():
            await machine.vanish()
        self.daemon.close()
        self.temp.cleanup()

    def held(self):
        """The machines made, the containers started, and the meetings the router counts."""
        return sorted(self.provider.machines), len(self.containers), sorted(self.router.active_ids())

    async def test_a_meeting_joined_by_phone_takes_no_machine_and_no_seat(self):
        from test_runtime_daemon import create_payload
        record, _session = await self.h.join(to='+14388097799,,85951289482#')
        self.assertEqual(self.held(), ([], 0, []))
        self.assertIsNone(self.router.capacity_problem('local'))
        # Its seat is still free: a Zoom meeting takes it, on a machine of its own.
        zoom = await self.daemon.create_meeting(create_payload())
        await until(lambda: zoom.id in self.provider.machines)
        self.assertIsNotNone(self.router.capacity_problem('local'))
        # With every seat taken, a meeting with its link and dial-in joins by phone, as one with
        # only its number does; neither makes a machine or waits for a seat.
        linked = await self.h.service.create(meeting_brief(to=ZOOM_LINK, dialIn={'invite': ZOOM_INVITE}))
        number = await self.h.service.create(meeting_brief(to='+16469313860,,85951289482#'))
        await until(lambda: self.h.phone.session(linked['id']) and self.h.phone.session(number['id']))
        self.assertEqual([event['reason'] for event in self.h.events(linked, 'call.dial_in')], ['no_seat'])
        self.assertEqual(self.held(), ([zoom.id], 0, [zoom.id]))
        for call in (record, linked, number):
            await self.h.service.end(call['id'], owner='local')
            done = await self.h.service.wait(call['id'], timeout=5, owner='local')
            self.assertEqual(done['line']['joinedBy'], 'phone')
        self.assertEqual(self.held(), ([zoom.id], 0, [zoom.id]))


class GuardrailTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = dict(ENV)
        self.store = CallStore(Path(self.temp.name) / 'calls')
        self.now = NIGHT
        self.hooks = DefaultCallHooks(environ=self.env, store=self.store, clock=lambda: self.now)

    def tearDown(self):
        self.temp.cleanup()

    def check(self, **overrides):
        self.hooks.precheck('local', CallBrief.from_dict(meeting_brief(**overrides)))

    def refused(self, **overrides):
        with self.assertRaises(CallRefused) as refused:
            self.check(**overrides)
        return refused.exception

    def add_calls(self, count, number, *, joined_by='phone', channel='meeting', status='completed'):
        """Earlier calls to `number` two hours ago: meetings joined by `joined_by`, or phone calls."""
        stamp = (self.now.replace(minute=0) - timedelta(hours=2)).isoformat().replace('+00:00', 'Z')
        start = len(self.store.list(owner='local', limit=None))
        for index in range(start, start + count):
            brief = {'to': number} if channel == 'phone' else {
                'to': ZOOM_LINK, 'dialIn': {'number': number, 'meetingId': '123456789', 'keys': ',,123456789#'}}
            line = ({'providerCallSid': 'CA1'} if channel == 'phone' else
                    {'joinedBy': 'phone', 'providerCallSid': 'CA1'} if joined_by == 'phone' else
                    {'meetingId': 'mtg-1'})
            self.store.create({'version': 1, 'id': f'call-{index:016x}', 'owner': 'local',
                               'channel': channel, 'direction': 'outbound', 'status': status,
                               'brief': brief, 'createdAt': stamp, 'updatedAt': stamp, 'line': line})

    def test_a_dial_in_number_that_is_not_a_published_bridge_keeps_calling_hours(self):
        # Teams and Google Meet numbers are an organizer's or a pool's: a phone call's hours.
        for to, wait in ((TEAMS_NUMBER + ',,123456789#', 8), ('+16505550123,,123456789#', 8),
                         ('+12125550123,,123456789#', 5)):
            problem = self.refused(to=to)
            self.assertEqual(problem.code, 'outside_calling_hours')
            self.assertIn("is not a meeting platform's published dial-in number", problem.message)
            self.assertIn(f'It can dial in at 8:00 AM there, in about {wait} hours.', problem.message)
            self.assertIn("Zoom's own dial-in numbers have no calling hours", problem.message)
            self.assertIn('afterHours', problem.message)
        # Late in the evening it is tomorrow morning.
        self.now = datetime(2026, 11, 6, 4, 30, tzinfo=timezone.utc)  # 8:30 PM in Los Angeles
        self.check(to=TEAMS_NUMBER + ',,123456789#')
        self.now = datetime(2026, 11, 6, 5, 30, tzinfo=timezone.utc)  # 9:30 PM there
        self.assertIn('at 8:00 AM there, in about 11 hours', self.refused(to=TEAMS_NUMBER + ',,123456789#').message)
        # The user confirms the meeting is on now, or the installation keeps no calling hours.
        self.check(to=TEAMS_NUMBER + ',,123456789#', afterHours=True)
        self.env['SMITLINE_CALLING_HOURS'] = 'off'
        self.check(to=TEAMS_NUMBER + ',,123456789#')

    def test_a_published_bridge_keeps_no_calling_hours(self):
        self.check(to='+16469313860,,85951289482#')  # Zoom's New York number at 3 AM there
        self.check(to='+14388097799,,85951289482#')  # and its Montreal one
        self.check(dialIn={'invite': ZOOM_INVITE})
        # Without the published list it is a number like any other.
        self.env['SMITLINE_DIAL_IN_BRIDGES'] = 'none'
        problem = self.refused(to='+16469313860,,85951289482#')
        self.assertEqual(problem.code, 'outside_calling_hours')
        self.assertIn('It can dial in at 8:00 AM there, in about 5 hours.', problem.message)
        # A number the installation adds is a bridge too.
        self.env['SMITLINE_DIAL_IN_BRIDGES'] = 'published,' + TEAMS_NUMBER
        self.check(to=TEAMS_NUMBER + ',,123456789#')
        self.check(to='+16469313860,,85951289482#')

    def test_a_meeting_with_a_link_checks_the_hours_only_when_it_dials(self):
        # It joins by the link; the phone rules apply when it comes to dialing (dial_in.phone_rules).
        self.check(to=MEET_LINK, dialIn={'invite': MEET_INVITE})
        with self.assertRaises(CallRefused) as refused:
            self.hooks.check_dial_in('local', CallBrief.from_dict(
                meeting_brief(to=MEET_LINK, dialIn={'invite': MEET_INVITE})))
        self.assertEqual(refused.exception.code, 'outside_calling_hours')

    def test_dial_ins_count_toward_the_call_limits(self):
        self.now = DAY
        self.add_calls(4, TEAMS_NUMBER)
        # Meetings by their link, over or still on: nothing was dialed.
        self.add_calls(2, TEAMS_NUMBER, joined_by='link')
        self.add_calls(2, TEAMS_NUMBER, joined_by='link', status='in_progress')
        self.check(to=TEAMS_NUMBER + ',,123456789#')
        self.add_calls(1, TEAMS_NUMBER)
        problem = self.refused(to=TEAMS_NUMBER + ',,123456789#')
        self.assertEqual(problem.code, 'too_many_calls_to_number')
        self.assertIn(f'Smitline has called {TEAMS_NUMBER} 5 times in the last 24 hours, the most '
                      'SMITLINE_MAX_CALLS_PER_NUMBER allows. It can call again in about 22 hours.',
                      problem.message)
        self.assertIn("Zoom's own dial-in numbers have no calling hours or call limits", problem.message)
        # A phone call to the number counts them, and a dial-in counts phone calls.
        phone = {'channel': 'phone', 'to': TEAMS_NUMBER, 'onBehalfOf': 'Smith', 'objective': 'Ask about the invoice'}
        with self.assertRaises(CallRefused) as refused:
            self.hooks.precheck('local', CallBrief.from_dict(phone))
        self.assertEqual(refused.exception.code, 'too_many_calls_to_number')
        self.add_calls(5, '+12125550123', channel='phone')
        self.assertEqual(self.refused(to='+12125550123,,123456789#').code, 'too_many_calls_to_number')
        # The hourly limit counts every call placed.
        self.env['SMITLINE_MAX_CALLS_PER_HOUR'] = '3'
        self.now = self.now.replace(hour=16, minute=30)  # half an hour after those calls
        self.assertEqual(self.refused(to='+16505550123,,123456789#').code, 'too_many_calls')

    def test_the_dial_in_number_gets_the_phone_guardrails(self):
        self.check(to='+16469313860,,85951289482#')  # 3 AM there: a bridge keeps no hours
        with self.assertRaises(CallRefused) as refused:
            self.check(to='+19005550100,,85951289482#')
        self.assertEqual(refused.exception.code, 'high_cost_number')
        self.env['SMITLINE_ALLOWED_CALLING_CODES'] = '44'
        with self.assertRaises(CallRefused) as refused:
            self.check(to='+16469313860,,85951289482#')
        self.assertEqual(refused.exception.code, 'destination_not_allowed')
        del self.env['SMITLINE_ALLOWED_CALLING_CODES']
        self.hooks.update_do_not_call('local', {'add': ['+16469313860']})
        with self.assertRaises(CallRefused) as refused:
            self.check(to=ZOOM_LINK, dialIn={'number': '+16469313860', 'meetingId': '85951289482'})
        self.assertEqual(refused.exception.code, 'do_not_call')
        # A meeting without a dial-in has nothing to check here.
        self.check(to=ZOOM_LINK)

    def test_a_bridge_number_has_no_call_limits(self):
        self.now = DAY
        self.env['SMITLINE_MAX_CALLS_PER_HOUR'] = '3'
        self.add_calls(6, '+16469313860')
        self.now = self.now.replace(hour=16, minute=30)
        self.check(to='+16469313860,,85951289482#')
        self.env['SMITLINE_DIAL_IN_BRIDGES'] = 'none'
        self.assertEqual(self.refused(to='+16469313860,,85951289482#').code, 'too_many_calls_to_number')


class SurfaceTests(unittest.TestCase):
    def test_the_api_schema_has_every_brief_field_and_the_dial_in(self):
        import json
        from call_brief import BRIEF_FIELDS
        spec = json.loads((Path(__file__).with_name('openapi.json')).read_text(encoding='utf-8'))
        schemas = spec['components']['schemas']
        self.assertEqual(set(schemas['Brief']['properties']), set(BRIEF_FIELDS))
        self.assertEqual(set(schemas['Brief']['properties']['dialIn']['properties']),
                         set(dial_in_details.DIAL_IN_FIELDS))
        self.assertLessEqual({'not_admitted', 'dial_in_refused', 'meeting_ended', 'busy', 'no_answer'},
                             set(schemas['Call']['properties']['endReason']['enum']))
        # Agents read that a dial-in keeps calling hours, which afterHours lifts.
        self.assertIn('meetings with a dialIn', schemas['Brief']['properties']['afterHours']['description'])
        self.assertIn('calling hours and call limits included', schemas['Brief']['properties']['dialIn']['description'])
        # And to pass it up front for Google Meet, which often turns guest browsers away.
        for field in ('to', 'dialIn'):
            self.assertIn('Google Meet', schemas['Brief']['properties'][field]['description'], field)

    def test_the_api_schema_has_every_result_field_and_the_checks_warnings(self):
        import json
        from call_result import build_result, refused_join
        spec = json.loads((Path(__file__).with_name('openapi.json')).read_text(encoding='utf-8'))
        schemas = spec['components']['schemas']
        result = schemas['Result']
        # reason and hint: only when Smitline knows why a call got nowhere.
        self.assertLessEqual({'reason', 'hint'}, set(result['properties']))
        self.assertFalse({'reason', 'hint'} & set(result['required']))
        refused = refused_join('meet', 'guest_refused')
        produced = build_result(outcome='not_reached', summary=refused['summary'], do_not_call=True,
                                reason=refused['reason'], hint=refused['hint'])
        self.assertLessEqual(set(produced), set(result['properties']))
        self.assertEqual(schemas['CheckResult']['properties']['warnings']['type'], 'array')
        self.assertNotIn('warnings', schemas['CheckResult']['required'])

    def test_the_daemon_gives_its_meeting_line_the_phone_line(self):
        from daemon_main import build_app, build_parser
        with tempfile.TemporaryDirectory() as temp:
            args = build_parser().parse_args(['--root', temp, '--runtime-root', str(Path(temp) / 'runtime')])
            app, _host, _port, _token_path = build_app(args)
            try:
                lines = app.call_service.lines
                self.assertIs(lines['meeting'].phone, lines['phone'])
            finally:
                app.runtime_daemon.close()


class CostTests(unittest.TestCase):
    def test_a_meeting_joined_by_phone_pays_for_its_line(self):
        usage = {'voiceSeconds': 120, 'phoneSeconds': 150, 'phoneProvider': 'signalwire'}
        by_phone = call_cost({'channel': 'meeting', 'usage': usage,
                              'line': {'joinedBy': 'phone', 'providerCallSid': 'CA1'}})
        # SignalWire's estimate: three started minutes at $0.011 and $0.006 a call.
        self.assertEqual(by_phone['phone'], round(3 * 0.011 + 0.006, 6))
        self.assertEqual(by_phone['openai'], round(120 / 60 * 0.05, 6))
        browser = call_cost({'channel': 'meeting', 'usage': usage, 'line': {'meetingId': 'mtg-1'}})
        self.assertEqual(browser['phone'], 0)


if __name__ == '__main__':
    unittest.main()
