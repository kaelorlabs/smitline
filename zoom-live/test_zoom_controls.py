import unittest
from zoom_controls import accept_host_unmute

class Button:
    def __init__(self, enabled=True): self.clicked = False; self.enabled = enabled
    async def count(self): return 1
    async def is_visible(self): return True
    async def is_enabled(self): return self.enabled
    async def click(self, **kwargs): self.clicked = True
class Dialog:
    def __init__(self, text, visible=True, enabled=True):
        self.text = text; self.visible = visible; self.button = Button(enabled)
    async def is_visible(self): return self.visible
    async def inner_text(self): return self.text
    def get_by_role(self, role, **kwargs): return self.button
class Page:
    def __init__(self, dialogs): self.dialogs = dialogs
    def locator(self, selector):
        assert selector == '[role="dialog"], [role="alertdialog"]'
        return self
    async def all(self): return self.dialogs
class HostUnmuteTests(unittest.IsolatedAsyncioTestCase):
    async def test_accept_host_request(self):
        dialog = Dialog('The host would like you to unmute\nStay Muted\nUnmute')
        self.assertTrue(await accept_host_unmute(Page([dialog])))
        self.assertTrue(dialog.button.clicked)
    async def test_no_request_stays_muted(self):
        self.assertFalse(await accept_host_unmute(Page([])))
        for dialog in [Dialog('Audio settings\nUnmute'), Dialog('The host would like you to unmute', visible=False), Dialog('The host would like you to unmute', enabled=False)]:
            self.assertFalse(await accept_host_unmute(Page([dialog])))
            self.assertFalse(dialog.button.clicked)
class MicStateTests(unittest.IsolatedAsyncioTestCase):
    async def test_hidden_toolbar_and_unrelated_buttons(self):
        from zoom_controls import microphone_is_muted
        class Matches:
            def __init__(self, labels): self.labels = labels
            async def count(self): return len(self.labels)
        class Toolbar:
            def __init__(self, labels): self.labels = labels
            def get_by_role(self, role, *, name, include_hidden):
                self.outer.assertTrue(include_hidden)
                return Matches([label for label in self.labels if name.search(label)])
        for labels, expected in [(['Mute my microphone (Alt+A)'], False), (['Unmute my microphone (Alt+A)'], True), (['Mute All', 'Unmute All'], None)]:
            page = Toolbar(labels); page.outer = self
            self.assertEqual(await microphone_is_muted(page), expected)

if __name__ == '__main__': unittest.main()
