import contextlib
import io
import json
import unittest
import urllib.error

import daily_rooms


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def read(self):
        return json.dumps(self.payload).encode()


class FakeOpener:
    def __init__(self, payload=None, error=None):
        self.payload, self.error, self.requests = payload or {}, error, []

    def __call__(self, request, timeout=None):
        self.requests.append(request)
        if self.error:
            raise self.error
        return FakeResponse(self.payload)


class DailyRoomsTests(unittest.TestCase):
    def test_creates_a_public_room_that_expires_and_ejects_everyone(self):
        opener = FakeOpener({'url': 'https://kaelor.daily.co/abc', 'name': 'abc'})
        self.assertEqual(daily_rooms.create_room('key-1', 30, opener=opener),
                         ('https://kaelor.daily.co/abc', 'abc'))
        request = opener.requests[0]
        self.assertEqual((request.get_method(), request.full_url), ('POST', 'https://api.daily.co/v1/rooms'))
        self.assertEqual(request.get_header('Authorization'), 'Bearer key-1')
        body = json.loads(request.data)
        self.assertEqual(body['privacy'], 'public')
        self.assertTrue(body['properties']['eject_at_room_exp'])
        self.assertFalse(body['properties']['enable_knocking'])

    def test_the_room_lasts_the_minutes_asked(self):
        self.assertEqual(daily_rooms.room_properties(30, now=1000)['properties']['exp'], 1000 + 30 * 60)
        for minutes in (0, 24 * 60 + 1):
            with self.subTest(minutes=minutes), self.assertRaises(ValueError):
                daily_rooms.room_properties(minutes)

    def test_deletes_a_room_by_name_only(self):
        opener = FakeOpener()
        daily_rooms.delete_room('key-1', 'abc', opener=opener)
        self.assertEqual((opener.requests[0].get_method(), opener.requests[0].full_url),
                         ('DELETE', 'https://api.daily.co/v1/rooms/abc'))
        with self.assertRaises(ValueError):
            daily_rooms.delete_room('key-1', 'https://kaelor.daily.co/abc', opener=opener)

    def test_an_error_from_daily_says_its_code_and_message(self):
        error = urllib.error.HTTPError('https://api.daily.co/v1/rooms', 401, 'Unauthorized', {},
                                       io.BytesIO(b'{"error":"authentication-error"}'))
        with self.assertRaisesRegex(RuntimeError, '401.*authentication-error'):
            daily_rooms.create_room('bad', opener=FakeOpener(error=error))

    def test_prints_only_the_link(self):
        out = io.StringIO()
        original = daily_rooms.create_room
        daily_rooms.create_room = lambda key, minutes: ('https://kaelor.daily.co/abc', 'abc')
        try:
            daily_rooms.main(['create', '--minutes', '15'], environ={'SMITLINE_DAILY_API_KEY': 'key-1'}, out=out)
        finally:
            daily_rooms.create_room = original
        self.assertEqual(out.getvalue(), 'https://kaelor.daily.co/abc\n')

    def test_needs_the_key(self):
        errors = io.StringIO()
        with contextlib.redirect_stderr(errors), self.assertRaises(SystemExit):
            daily_rooms.main(['create'], environ={}, out=io.StringIO())
        self.assertIn('SMITLINE_DAILY_API_KEY', errors.getvalue())


if __name__ == '__main__':
    unittest.main()
