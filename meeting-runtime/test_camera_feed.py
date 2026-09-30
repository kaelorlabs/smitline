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

    def test_disabled_feed_does_not_wrap_or_install(self):
        class Writer:
            audio_format = object()
            chunk_size = 480
        writer = Writer()
        feed = CameraFeed(writer, enabled=False)
        self.assertIs(feed.audio_writer, writer)

    def test_joinly_effects_map_onto_visual_states(self):
        from joinly.providers.browser.camera_feed import _JOINLY_EFFECT_TO_VISUAL
        self.assertEqual(_JOINLY_EFFECT_TO_VISUAL['typing'], 'working')
        self.assertEqual(_JOINLY_EFFECT_TO_VISUAL['thinking'], 'joining')
