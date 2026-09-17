"""Shared-content capture fixtures for Zoom and Teams."""
import unittest
from playwright.async_api import async_playwright

from adapters import create_adapter
from visual_hash import mean_rgb


ZOOM_SHARE = '''
<style>body { margin: 0 }</style>
<div style="display:flex">
  <div id="sharee-container" data-share-content="true" style="width:320px;height:180px;background:#ff0000"></div>
  <div class="gallery-video-container" style="width:320px;height:180px;background:#0000ff">gallery</div>
  <button aria-label="open the chat panel">Chat</button>
</div>
'''
TEAMS_SHARE = '''
<style>body { margin: 0 }</style>
<div style="display:flex">
  <div data-tid="calling-screen-sharing-stage" style="width:320px;height:180px;background:#ff0000"></div>
  <div data-tid="calling-participant-stream" style="width:320px;height:180px;background:#0000ff">gallery</div>
  <div data-tid="chat-pane">chat</div>
</div>
'''
AMBIGUOUS = '''
<div id="sharee-container" style="width:240px;height:160px;background:#111"></div>
<div class="sharee-container" style="width:240px;height:160px;background:#222"></div>
'''


class SharedContentFixtures(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.pw = await async_playwright().start()
        self.browser = await self.pw.chromium.launch(
            headless=True, executable_path=self.pw.chromium.executable_path, args=['--no-sandbox'])
        self.page = await self.browser.new_page(viewport={'width': 1280, 'height': 720})
        self.stop = __import__('asyncio').Event()

    async def asyncTearDown(self):
        await self.browser.close()
        await self.pw.stop()

    def zoom(self):
        return create_adapter('https://zoom.us/j/123', self.page, self.stop, lambda *_: None)

    def teams(self):
        return create_adapter('https://teams.microsoft.com/meet/123', self.page, self.stop, lambda *_: None)

    async def test_zoom_and_teams_capture_only_the_share_surface(self):
        await self.page.set_content(ZOOM_SHARE)
        zoom = self.zoom()
        self.assertTrue(zoom.capabilities.shared_content)
        png, located = await zoom.capture_shared_content()
        self.assertTrue(located.available)
        self.assertEqual(located.confidence, 'high')
        red, _green, blue = mean_rgb(png)
        self.assertGreater(red, 180)
        self.assertLess(blue, 80)
        await self.page.set_content(TEAMS_SHARE)
        png, located = await self.teams().capture_shared_content()
        self.assertTrue(located.available)
        red, _green, blue = mean_rgb(png)
        self.assertGreater(red, 180)
        self.assertLess(blue, 80)

    async def test_no_share_and_ambiguous_selectors_capture_nothing(self):
        await self.page.set_content('<div class="gallery-video-container">tiles</div><button>Chat</button>')
        png, located = await self.zoom().capture_shared_content()
        self.assertIsNone(png)
        self.assertFalse(located.available)
        self.assertEqual(located.reason, 'unavailable')
        await self.page.set_content(AMBIGUOUS)
        png, located = await self.zoom().capture_shared_content()
        self.assertIsNone(png)
        self.assertEqual(located.reason, 'selector_ambiguous')

    async def test_tiny_or_toolbar_matches_are_not_shared_content(self):
        await self.page.set_content(
            '<div id="sharee-container" style="width:20px;height:20px;background:#ff0"></div>')
        png, located = await self.zoom().capture_shared_content()
        self.assertIsNone(png)
        self.assertEqual(located.reason, 'unavailable')
