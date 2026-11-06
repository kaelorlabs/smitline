"""The mascot camera's page script in a real browser: the meeting image's Chromium, headless.

A small pack of solid colours plays on a local page that asks for the camera, and the test reads
the camera track's frames: the walk-in, the speaking clip with the mouth the voice asks for, and
the camera going off once the meeting has ended.
"""
import asyncio
import io
import socket
import struct
import time
import unittest

from presence_pack import Clip, Mouth, PresencePack
from visual_presence import VISUAL_STATES
from voice_activity import VoiceActivity

try:
    from aiohttp import web
    from PIL import Image
    from playwright.async_api import async_playwright
    from joinly.providers.browser.camera_feed import CameraFeed
    READY = True
except ImportError:
    READY = False

W, H = 160, 90
COLOURS = {'black': (0, 0, 0), 'red': (230, 30, 30), 'green': (30, 200, 60), 'blue': (40, 60, 220),
           'white': (250, 250, 250), 'yellow': (240, 220, 40), 'pink': (240, 120, 180)}
MOUTH = (60, 40, 30, 20)  # x, y, width, height
LOUD = struct.pack('<240h', *([12000, -12000] * 120))
SILENCE = bytes(480)


def picture(colour, size=(W, H)):
    out = io.BytesIO()
    Image.new('RGB', size, COLOURS[colour]).save(out, 'WEBP', lossless=True)
    return out.getvalue()


def pack():
    one = lambda colour, count=1: (picture(colour),) * count  # noqa: E731
    clips = {
        'offstage': Clip(one('black'), ('image/webp',), True),
        'arrive': Clip(one('red', 3), ('image/webp',) * 3, False),
        'listening': Clip(one('green', 2), ('image/webp',) * 2, True),
        'speaking': Clip(one('blue', 2), ('image/webp',) * 2, True,
                         Mouth(*MOUTH, (picture('pink', MOUTH[2:]), picture('white', MOUTH[2:])),
                               ('image/webp',) * 2)),
        'leave': Clip(one('yellow', 3) + one('black'), ('image/webp',) * 4, False),
    }
    states = {state: 'listening' for state in VISUAL_STATES}
    states.update(joining='offstage', speaking='speaking', finalizing='leave', ended='leave')
    return PresencePack('test', W, H, 24, clips, states, 'arrive', None, '', '', 2)


class Writer:
    audio_format = object()
    chunk_size = 480


# Served from localhost, a secure context, so the page may ask for a camera.
PAGE = '<!doctype html><title>camera</title>'
GRAB = """async ([x, y]) => {
    const bitmap = await window.__grab.grabFrame();
    const canvas = new OffscreenCanvas(bitmap.width, bitmap.height);
    const context = canvas.getContext('2d');
    context.drawImage(bitmap, 0, 0);
    return Array.from(context.getImageData(x, y, 1, 1).data.slice(0, 3));
}"""


def colour_of(rgb):
    return min(COLOURS, key=lambda name: sum((a - b) ** 2 for a, b in zip(COLOURS[name], rgb)))


@unittest.skipUnless(READY, 'needs Playwright, Chromium, aiohttp and Pillow (the meeting image)')
class MascotPageTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        async def page(_request):
            return web.Response(text=PAGE, content_type='text/html')
        app = web.Application()
        app.router.add_get('/', page)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        sock = socket.socket()
        sock.bind(('127.0.0.1', 0))
        await web.SockSite(self.runner, sock).start()
        self.url = f'http://127.0.0.1:{sock.getsockname()[1]}/'
        self.playwright = await async_playwright().start()
        try:
            self.browser = await self.playwright.chromium.launch(
                channel='chromium', headless=True,
                args=['--no-sandbox', '--disable-gpu', '--use-fake-ui-for-media-stream'])
        except Exception as error:
            await self.playwright.stop()
            await self.runner.cleanup()
            self.skipTest(f'Chromium did not start: {str(error)[:120]}')
        self.page = await self.browser.new_page()

    async def asyncTearDown(self):
        await self.browser.close()
        await self.playwright.stop()
        await self.runner.cleanup()

    async def colour(self, x, y):
        return colour_of(await self.page.evaluate(GRAB, [x, y]))

    async def wait_for(self, x, y, colour, seconds=5):
        deadline = time.monotonic() + seconds
        seen = None
        while time.monotonic() < deadline:
            seen = await self.colour(x, y)
            if seen == colour:
                return
            await asyncio.sleep(0.02)
        self.fail(f'({x}, {y}) is {seen}, not {colour}')

    async def test_plays_the_pack_with_the_voices_mouth_then_turns_the_camera_off(self):
        voice = VoiceActivity()
        feed = CameraFeed(Writer(), style='mascot', pack=pack().page_payload(), voice=voice)
        await feed.install(self.page)
        await self.page.goto(self.url)
        hint = await self.page.evaluate("""async () => {
            const stream = await navigator.mediaDevices.getUserMedia({video: true});
            window.__track = stream.getVideoTracks()[0];
            window.__grab = new ImageCapture(window.__track);
            return window.__track.contentHint;
        }""")
        self.assertEqual(hint, 'detail')
        mouth = (MOUTH[0] + MOUTH[2] // 2, MOUTH[1] + MOUTH[3] // 2)
        await self.wait_for(20, 20, 'black')  # the empty stage while joining

        feed.set_visual_state('listening')    # walks in, then listens
        await self.wait_for(20, 20, 'green')
        feed.set_visual_state('speaking')
        await self.wait_for(20, 20, 'blue')
        self.assertEqual(await self.colour(*mouth), 'blue')  # the clip's own mouth, shut

        for _ in range(12):                    # the voice: loud, as the microphone writes it
            voice.feed(LOUD)
            await asyncio.sleep(0.01)
        await self.wait_for(*mouth, 'white')   # wide open: the second mouth picture
        self.assertEqual(await self.colour(20, 20), 'blue')   # and only in its box
        self.assertEqual(await self.colour(MOUTH[0] + MOUTH[2] + 2, mouth[1]), 'blue')
        while voice.speaking:                  # quiet again: the mouth closes
            voice.feed(SILENCE)
            await asyncio.sleep(0.01)
        await self.wait_for(*mouth, 'blue')

        state = await self.page.evaluate('window.__track.readyState')
        self.assertEqual(state, 'live')
        feed.set_visual_state('ended')         # waves goodbye, leaves the stage empty, goes off
        deadline = time.monotonic() + 5
        while state == 'live' and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
            state = await self.page.evaluate('window.__track.readyState')
        self.assertEqual(state, 'ended')

    async def test_a_camera_that_never_arrived_goes_off_at_the_end(self):
        feed = CameraFeed(Writer(), style='mascot', pack=pack().page_payload(), voice=VoiceActivity())
        await feed.install(self.page)
        await self.page.goto(self.url)
        await self.page.evaluate("""async () => {
            const stream = await navigator.mediaDevices.getUserMedia({video: true});
            window.__track = stream.getVideoTracks()[0];
            window.__grab = new ImageCapture(window.__track);
        }""")
        await self.wait_for(20, 20, 'black')
        feed.set_visual_state('ended')
        deadline = time.monotonic() + 3
        state = 'live'
        while state == 'live' and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
            state = await self.page.evaluate('window.__track.readyState')
        self.assertEqual(state, 'ended')

    async def test_turn_off_stops_the_tracks_at_once(self):
        feed = CameraFeed(Writer(), style='still')
        await feed.install(self.page)
        await self.page.goto(self.url)
        await self.page.evaluate("""async () => {
            const stream = await navigator.mediaDevices.getUserMedia({video: true});
            window.__track = stream.getVideoTracks()[0];
        }""")
        self.assertEqual(await self.page.evaluate('window.__track.readyState'), 'live')
        await feed.turn_off()
        self.assertEqual(await self.page.evaluate('window.__track.readyState'), 'ended')


if __name__ == '__main__':
    unittest.main()
