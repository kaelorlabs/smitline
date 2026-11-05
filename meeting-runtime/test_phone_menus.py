"""Phone menus: a brief with phoneMenus lets the voice press keys ("press 2 for billing")."""
import unittest

from call_brief import CallBrief
from test_phone import brief


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


if __name__ == '__main__':
    unittest.main()
