"""Screen-share settings, hashing, analysis fail-closed, and pipeline tests."""
import asyncio
import tempfile
import unittest
from pathlib import Path

from artifact_store import ArtifactStore
from screen_share import VisualObservation, parse_screen_share_settings, public_status
from screen_share_pipeline import ScreenShareBus, ScreenShareCaptureLoop, ScreenShareHost
from visual_analysis import (
    CodexVisualAnalysisProvider, StaticVisualAnalysisProvider, detect_codex_image_flag,
)
from visual_hash import average_hash, change_score, sha256_hex, solid_png


class FakeAdapter:
    capabilities = type('C', (), {'shared_content': True})()

    def __init__(self, frames=None, state=None):
        self.frames = list(frames or [])
        self.state = state

    async def capture_shared_content(self):
        if self.frames:
            png = self.frames.pop(0)
            located = type('S', (), {'available': True, 'confidence': 'high', 'reason': None})()
            return png, located
        located = type('S', (), {
            'available': False, 'confidence': 'none', 'reason': 'unavailable'})()
        return None, located


class ScreenShareSchemaTests(unittest.TestCase):
    def test_defaults_are_disabled_and_bounded(self):
        settings = parse_screen_share_settings(None)
        self.assertFalse(settings['enabled'])
        self.assertEqual(settings['captureIntervalMs'], 4000)
        with self.assertRaises(ValueError):
            parse_screen_share_settings({'enabled': True, 'captureIntervalMs': 200})
        with self.assertRaises(ValueError):
            parse_screen_share_settings({'argv': ['ffmpeg']})
        with self.assertRaises(ValueError):
            VisualObservation.from_dict({
                'id': 'obs-1', 'meetingId': 'mtg-abc123', 'timestamp': '2026-09-17T00:00:00Z',
                'summary': 'password is hunter2', 'frameArtifactId': 'art-1', 'confidence': 0.2,
            })

    def test_status_omits_paths_and_bytes(self):
        status = public_status({
            'enabled': True, 'available': True, 'active': True, 'path': '/tmp/frame.png',
            'png': 'secret',
        })
        dumped = str(status)
        self.assertNotIn('/tmp', dumped)
        self.assertNotIn('secret', dumped)
        self.assertTrue(status['enabled'])


class HashAndPipelineTests(unittest.IsolatedAsyncioTestCase):
    def test_change_detection_and_perceptual_hash(self):
        red = solid_png(32, 32, 255, 0, 0)
        red2 = solid_png(32, 32, 250, 0, 0)
        blue = solid_png(32, 32, 0, 0, 255)
        self.assertEqual(sha256_hex(red), sha256_hex(solid_png(32, 32, 255, 0, 0)))
        self.assertLess(change_score(average_hash(red), average_hash(red2)), 0.2)
        self.assertGreater(change_score(average_hash(red), average_hash(blue)), 0.4)

    def test_codex_image_support_is_feature_detected(self):
        self.assertIsNone(detect_codex_image_flag('Usage: codex exec [prompt]'))
        self.assertEqual(detect_codex_image_flag('  --image <path>  Attach a local image'), '--image')
        provider = CodexVisualAnalysisProvider(command='/bin/false', help_text='no images here')
        self.assertFalse(provider.available())

    async def test_disabled_loop_never_captures(self):
        adapter = FakeAdapter(frames=[solid_png(32, 32, 1, 2, 3)])
        with tempfile.TemporaryDirectory() as directory:
            bus = ScreenShareBus(directory, 'mtg-abc123')
            state = {}
            loop = ScreenShareCaptureLoop(
                adapter=adapter, settings={'enabled': False}, bus=bus, state=state)
            await loop.tick()
            self.assertEqual(loop.state['screenShare']['degradedReason'], 'disabled')
            self.assertEqual(bus.list_inbox(), [])

    async def test_rate_backpressure_retention_and_analysis(self):
        red = solid_png(48, 48, 255, 0, 0)
        blue = solid_png(48, 48, 0, 0, 255)
        adapter = FakeAdapter(frames=[red, red, blue])
        with tempfile.TemporaryDirectory() as directory:
            bus = ScreenShareBus(directory, 'mtg-abc123')
            state = {'floorState': 'listening'}
            appended = []
            loop = ScreenShareCaptureLoop(
                adapter=adapter, settings={'enabled': True, 'minChange': 0.05, 'maxFrames': 2},
                bus=bus, state=state, send=appended.append)
            first = await loop.tick()
            self.assertTrue(first['available'])
            self.assertEqual(len(bus.list_inbox()), 1)
            second = await loop.tick()
            self.assertEqual(second['degradedReason'], 'backpressure')
            store = ArtifactStore(Path(directory) / 'artifacts')
            events = []
            observations = []
            host = ScreenShareHost(
                artifacts=store,
                analyzer=StaticVisualAnalysisProvider({'summary': 'A red slide', 'confidence': 0.9}),
            )
            path, meta = bus.list_inbox()[0]
            result = await host.ingest_png(
                'mtg-abc123', path.read_bytes(), settings={'enabled': True, 'minChange': 0.05},
                emit=lambda kind, **payload: events.append(kind),
                store_observation=observations.append,
            )
            bus.drop_inbox(path)
            self.assertIsNotNone(result['observation'])
            self.assertIn('screen_share.observation', events)
            self.assertIn('artifact.created', events)
            drained = await loop.drain_observations()
            self.assertEqual(drained, [])
            bus.write_outbox(VisualObservation.from_dict(result['observation']))
            drained = await loop.drain_observations()
            self.assertEqual(len(drained), 1)
            self.assertEqual(appended[0]['type'], 'session.thinking.append')
            self.assertIn('Shared content:', appended[0]['content'])
            self.assertNotIn('\\x89PNG', appended[0]['content'])
            state['floorState'] = 'speaking'
            bus.write_outbox(VisualObservation.from_dict(result['observation']))
            await loop.drain_observations()
            self.assertEqual(len(appended), 1)

    async def test_unavailable_analyzer_fail_closed(self):
        png = solid_png(32, 32, 9, 9, 9)
        with tempfile.TemporaryDirectory() as directory:
            store = ArtifactStore(Path(directory) / 'artifacts')
            events = []
            host = ScreenShareHost(artifacts=store, analyzer=StaticVisualAnalysisProvider(available=False))
            result = await host.ingest_png(
                'mtg-abc123', png, settings={'enabled': True},
                emit=lambda kind, **payload: events.append((kind, payload)),
                store_observation=lambda *_: None,
            )
            self.assertEqual(result['reason'], 'analyzer_unavailable')
            self.assertTrue(any(kind == 'screen_share.failed' for kind, _payload in events))
            self.assertTrue(any(kind == 'screen_share.frame_selected' for kind, _payload in events))


if __name__ == '__main__':
    unittest.main()
