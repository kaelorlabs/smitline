"""Phone menus: a brief with phoneMenus lets the voice press keys ("press 2 for billing")."""
import math
import unittest

from call_brief import CallBrief
from call_recorder import ULAW
from phone_keys import FREQUENCIES, key_audio, menu_keys, shown_keys
from test_phone import brief


def goertzel(samples, frequency, rate=8000):
    k = 2 * math.cos(2 * math.pi * frequency / rate)
    a = b = 0.0
    for sample in samples:
        a, b = sample + k * a - b, a
    return a * a + b * b - k * a * b


def parsed(**overrides):
    return CallBrief.from_dict(brief(**overrides))


class BriefTests(unittest.TestCase):
    def test_off_unless_the_brief_turns_it_on(self):
        self.assertFalse(parsed().phone_menus)
        self.assertNotIn('phoneMenus', parsed().to_dict())
        on = parsed(phoneMenus=True)
        self.assertTrue(on.phone_menus)
        self.assertIs(on.to_dict()['phoneMenus'], True)
        self.assertEqual(CallBrief.from_dict(on.to_dict()), on)

    def test_fails_closed(self):
        with self.assertRaises(ValueError):
            parsed(phoneMenus='yes')
        with self.assertRaisesRegex(ValueError, 'only for phone calls'):
            CallBrief.from_dict({'channel': 'meeting', 'to': 'https://zoom.us/j/123456789',
                                 'onBehalfOf': 'Robin', 'objective': 'Take notes',
                                 'phoneMenus': True})


class KeyTests(unittest.TestCase):
    def test_keys_are_digits_star_hash_and_pauses(self):
        self.assertEqual(menu_keys('2'), '2')
        self.assertEqual(menu_keys(' 408-555 (1212)# '), '4085551212#')
        self.assertEqual(menu_keys('1W2w'), '1w2w')
        for bad in ('', 'ww', '12a', 'A', '1' * 33, 12, None):
            with self.assertRaises(ValueError):
                menu_keys(bad)

    def test_long_entries_show_only_their_length(self):
        self.assertEqual(shown_keys('2'), '2')
        self.assertEqual(shown_keys('1w0#'), '10#')
        self.assertEqual(shown_keys('w4085551212#'), '11 keys')

    def test_each_tone_is_its_two_frequencies(self):
        bands = (697, 770, 852, 941, 1209, 1336, 1477, 1633)
        for key in '0123456789*#':
            audio = key_audio(key)
            self.assertEqual(len(audio), 1600)  # 100 ms of tone, 100 ms of quiet
            samples = [ULAW[byte] for byte in audio[:800]]
            power = {band: goertzel(samples, band) for band in bands}
            self.assertEqual(sorted(sorted(power, key=power.get)[-2:]), list(FREQUENCIES[key]))
            self.assertEqual(set(audio[800:]), {0xFF})


if __name__ == '__main__':
    unittest.main()
