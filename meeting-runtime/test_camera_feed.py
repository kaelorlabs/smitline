import asyncio
import unittest

try:
    from joinly.providers.browser.camera_feed import (
        CameraFeed, _LOGO_SVG, build_camera_override_script,
    )
    HAS_JOINLY = True
except ImportError:
    HAS_JOINLY = False
    CameraFeed = None
    _LOGO_SVG = ''
    build_camera_override_script = None


def _has_colleague_feed():
    if not HAS_JOINLY:
        return False
    try:
        return '__setVisualState' in build_camera_override_script()
    except TypeError:
        return False


@unittest.skipUnless(_has_colleague_feed(), 'Colleague CameraFeed presence API is required')
class CameraFeedScriptTests(unittest.TestCase):
    def test_script_is_smitline_branded_without_private_text(self):
        script = build_camera_override_script()
        self.assertIn('Smitline', script)
        self.assertIn('__setVisualState', script)
        self.assertIn('fxListening', script)
        self.assertNotIn('Virtual Camera', script)
        self.assertNotIn('fillText', script)
        lowered = script.lower()
        for token in ('checking project', 'preparing handoff', 'transcript', 'prompt',
                      'filename', 'customer', 'api key', 'secret meeting'):
            self.assertNotIn(token, lowered)
        self.assertIn('data:image/svg+xml', _LOGO_SVG)

    def test_the_only_text_is_the_caption_it_is_given(self):
        for style in ('still', 'animated'):
            with self.subTest(style=style):
                plain = build_camera_override_script(style=style)
                self.assertNotIn('fillText', plain)
                self.assertNotIn('__CAPTION__', plain)
                script = build_camera_override_script(style=style, caption="Arun's AI assistant")
                self.assertEqual(script.count('fillText('), 1)
                self.assertIn('ctx.fillText("Arun\'s AI assistant", W / 2, H * 0.9)', script)
                self.assertNotIn('__CAPTION__', script)
                # A caption cannot break out of its string.
                tricky = build_camera_override_script(style=style, caption='"); alert(1); ("')
                self.assertIn(r'ctx.fillText("\"); alert(1); (\"", W / 2, H * 0.9)', tricky)

    def test_disabled_feed_does_not_wrap_or_install(self):
        class Writer:
            audio_format = object()
            chunk_size = 480
        writer = Writer()
        feed = CameraFeed(writer, enabled=False)
        self.assertIs(feed.audio_writer, writer)

    def test_still_style_draws_on_state_changes_at_a_low_frame_rate(self):
        script = build_camera_override_script(style='still')
        self.assertIn('__setVisualState', script)
        self.assertIn('captureStream(FPS)', script)
        self.assertIn('FPS = 5', script)
        self.assertIn('W = 640, H = 360', script)
        self.assertNotIn('requestAnimationFrame', script)
        self.assertNotIn('__setBands', script)
        self.assertNotIn('fillText', script)
        # Both styles route camera requests to the canvas the same way.
        for style in ('still', 'animated'):
            with self.subTest(style=style):
                patched = build_camera_override_script(style=style)
                self.assertIn('md.getUserMedia = async', patched)
                self.assertIn('RTCPeerConnection.prototype.addTrack', patched)
                self.assertNotIn('{device_patch}', patched)

    def test_still_feed_skips_the_amplitude_writer(self):
        class Writer:
            audio_format = object()
            chunk_size = 480
        writer = Writer()
        self.assertIs(CameraFeed(writer, style='still').audio_writer, writer)
        self.assertIsNot(CameraFeed(writer, style='animated').audio_writer, writer)

    def test_unknown_style_fails_closed(self):
        with self.assertRaises(ValueError):
            build_camera_override_script(style='video')
        with self.assertRaises(ValueError):
            CameraFeed(object(), style='video')

    @staticmethod
    def _pack():
        frame = {'type': 'image/webp', 'data': 'U0VDUkVULUZSQU1F'}
        return {
            'width': 640, 'height': 360, 'fps': 12, 'arrival': 'arrive',
            'captionColor': '#2E2B3A',
            'states': {'joining': 'offstage', 'listening': 'listening', 'working': 'listening',
                       'speaking': 'listening', 'finalizing': 'offstage',
                       'needs_attention': 'listening', 'ended': 'offstage'},
            'clips': {'offstage': {'loop': True, 'frames': [frame]},
                      'arrive': {'loop': False, 'frames': [frame, frame]},
                      'listening': {'loop': True, 'frames': [frame]}},
        }

    def test_mascot_style_plays_a_pack_it_asks_for_without_carrying_it(self):
        pack = self._pack()
        script = build_camera_override_script(style='mascot', pack=pack,
                                              caption="Arun's AI assistant")
        self.assertIn('W = 640, H = 360, FPS = 12', script)
        self.assertIn('captureStream(FPS)', script)
        self.assertIn('window.__smitlinePresencePack', script)
        self.assertIn('__setVisualState', script)
        # The frames reach the page through the exposed function, never in this script, which
        # every frame of the meeting page runs.
        self.assertNotIn('U0VDUkVULUZSQU1F', script)
        self.assertIn('const CAPTION = "Arun\'s AI assistant";', script)
        self.assertIn('const CAPTION_COLOR = "#2E2B3A";', script)
        self.assertEqual(script.count('fillText('), 1)
        self.assertNotIn('__CAPTION', script)
        self.assertNotIn('{device_patch}', script)
        self.assertIn('md.getUserMedia = async', script)
        tricky = build_camera_override_script(style='mascot', pack=pack,
                                              caption='"); alert(1); ("')
        self.assertIn(r'const CAPTION = "\"); alert(1); (\"";', tricky)

    def test_mascot_style_needs_a_pack(self):
        with self.assertRaises(ValueError):
            build_camera_override_script(style='mascot')
        with self.assertRaises(ValueError):
            CameraFeed(object(), style='mascot')

    def test_mascot_feed_offers_its_pack_to_the_page_before_its_script(self):
        calls = []

        class Page:
            async def expose_function(self, name, function):
                calls.append(('expose', name, function()))

            async def add_init_script(self, script):
                calls.append(('script', '__smitlinePresencePack' in script))

        class Writer:
            audio_format = object()
            chunk_size = 480
        pack = self._pack()
        asyncio.run(CameraFeed(Writer(), style='mascot', pack=pack).install(Page()))
        self.assertEqual(calls, [('expose', '__smitlinePresencePack', pack), ('script', True)])
        calls.clear()
        asyncio.run(CameraFeed(Writer(), style='still', pack=pack).install(Page()))
        self.assertEqual(calls, [('script', False)])

    def test_joinly_effects_map_onto_visual_states(self):
        from joinly.providers.browser.camera_feed import _JOINLY_EFFECT_TO_VISUAL
        self.assertEqual(_JOINLY_EFFECT_TO_VISUAL['typing'], 'working')
        self.assertEqual(_JOINLY_EFFECT_TO_VISUAL['thinking'], 'joining')
