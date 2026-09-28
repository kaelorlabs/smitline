"""Screen-share settings, hashing, analysis fail-closed, and pipeline tests."""
import asyncio
import contextlib
import io
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from artifact_store import ArtifactStore
from screen_share import VisualObservation, parse_screen_share_settings, public_status
from screen_share_pipeline import ScreenShareBus, ScreenShareCaptureLoop, ScreenShareHost
from visual_analysis import (
    CodexVisualAnalysisProvider, StaticVisualAnalysisProvider, VisualAnalysisUnavailable,
    _extract_observation_json, detect_codex_image_flag,
)
from visual_diff import compare, frame_signature, is_significant
from visual_hash import sha256_hex, solid_png


def observation(summary, index=0):
    return VisualObservation.from_dict({
        'id': f'obs-{index}', 'meetingId': 'mtg-abc123',
        'timestamp': f'2026-09-17T00:00:0{index}Z', 'summary': summary,
        'frameArtifactId': f'art-{index}', 'confidence': 0.8,
    })


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
    def test_change_detection_and_tile_signature(self):
        red = solid_png(32, 32, 255, 0, 0)
        red2 = solid_png(32, 32, 250, 0, 0)
        blue = solid_png(32, 32, 0, 0, 255)
        self.assertEqual(sha256_hex(red), sha256_hex(solid_png(32, 32, 255, 0, 0)))
        self.assertFalse(is_significant(compare(frame_signature(red), frame_signature(red2)), 0.08))
        self.assertGreater(compare(frame_signature(red), frame_signature(blue)).score, 0.4)

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
        adapter = FakeAdapter(frames=[red, red, red, blue])
        with tempfile.TemporaryDirectory() as directory:
            bus = ScreenShareBus(directory, 'mtg-abc123')
            state = {'floorState': 'listening'}
            appended = []
            loop = ScreenShareCaptureLoop(
                adapter=adapter, settings={'enabled': True, 'minChange': 0.05, 'maxFrames': 2},
                bus=bus, state=state, send=appended.append)
            settling = await loop.tick()
            self.assertTrue(settling['available'])
            self.assertEqual(bus.list_inbox(), [])
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
            state['floorState'] = 'listening'
            await loop.drain_observations()
            self.assertEqual(len(appended), 2)
            self.assertIn('A red slide', appended[1]['content'])

    async def test_observations_during_speech_keep_only_the_latest(self):
        with tempfile.TemporaryDirectory() as directory:
            bus = ScreenShareBus(directory, 'mtg-abc123')
            state = {'floorState': 'speaking'}
            appended = []
            loop = ScreenShareCaptureLoop(
                adapter=FakeAdapter(), settings={'enabled': True}, bus=bus, state=state,
                send=appended.append)
            for index, summary in enumerate(('First slide', 'Second slide', 'Third slide')):
                bus.write_outbox(observation(summary, index))
                await loop.drain_observations()
            self.assertEqual(appended, [])
            state['floorState'] = 'listening'
            await loop.drain_observations()
            await loop.drain_observations()
            self.assertEqual(len(appended), 1)
            self.assertIn('Third slide', appended[0]['content'])

    async def test_capture_errors_degrade_without_ending_the_loop(self):
        good = solid_png(48, 48, 0, 200, 0)
        adapter = FakeAdapter(frames=[b'not a png', good, good, good])
        with tempfile.TemporaryDirectory() as directory:
            bus = ScreenShareBus(directory, 'mtg-abc123')
            state = {}
            stop = asyncio.Event()
            ticks = []

            async def sleep(_interval):
                ticks.append(dict(state['screenShare']))
                if len(ticks) == 4:
                    stop.set()

            loop = ScreenShareCaptureLoop(
                adapter=adapter, settings={'enabled': True}, bus=bus, state=state,
                stop=stop, sleep=sleep)
            write_inbox = bus.write_inbox
            failures = []

            def failing_once(png, meta):
                if not failures:
                    failures.append(meta['id'])
                    raise PermissionError('Permission denied: inbox')
                return write_inbox(png, meta)

            bus.write_inbox = failing_once
            logged = io.StringIO()
            with contextlib.redirect_stdout(logged):
                await loop.run()
            self.assertIn('PermissionError', logged.getvalue())
            reasons = [tick.get('degradedReason') for tick in ticks]
            # Bad frame, settling, failed write, then the same screen is retried and written.
            self.assertEqual(reasons, ['unavailable', None, 'unavailable', None])
            self.assertTrue(ticks[3]['available'])
            self.assertEqual(len(failures), 1)
            self.assertEqual(len(bus.list_inbox()), 1)

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


class CodexAnalysisTests(unittest.IsolatedAsyncioTestCase):
    PROMPT_ECHO = 'echo \'Return JSON {"summary": str, "confidence": number}\' >&2\n'

    def fake_codex(self, directory, body):
        path = Path(directory) / 'codex'
        path.write_text('#!/bin/sh\n' + body)
        path.chmod(0o700)
        return CodexVisualAnalysisProvider(command=str(path), help_text='--image <path>')

    async def while_ticking(self, coroutine):
        """Run the coroutine and count how often the event loop got to run meanwhile."""
        task = asyncio.ensure_future(coroutine)
        ticks = 0
        while not task.done():
            await asyncio.sleep(0.02)
            ticks += 1
        return await task, ticks

    def analyze(self, provider):
        return provider.analyze(
            solid_png(8, 8, 1, 2, 3), meeting_id='mtg-abc123', frame_artifact_id='art-1')

    async def test_analysis_keeps_the_event_loop_free_and_reads_stdout(self):
        with tempfile.TemporaryDirectory() as directory:
            provider = self.fake_codex(directory, self.PROMPT_ECHO + (
                'echo \'{"summary": "From stderr", "confidence": 0.1}\' >&2\n'
                'sleep 0.3\n'
                'echo \'{"summary": "A roadmap slide", "confidence": 0.8, "visibleText": ["Q3"]}\'\n'))
            result, ticks = await self.while_ticking(self.analyze(provider))
        self.assertEqual(result.summary, 'A roadmap slide')
        self.assertGreater(ticks, 5)

    async def test_json_only_on_stderr_is_not_an_observation(self):
        with tempfile.TemporaryDirectory() as directory:
            provider = self.fake_codex(directory, self.PROMPT_ECHO + (
                'echo \'{"summary": "From stderr", "confidence": 0.1}\' >&2\n'))
            with self.assertRaises(VisualAnalysisUnavailable):
                await self.analyze(provider)

    async def test_injected_runner_runs_in_a_worker_thread(self):
        def runner(args, **kwargs):
            time.sleep(0.3)
            return SimpleNamespace(stdout='{"summary": "Threaded", "confidence": 0.5}',
                                   stderr='{"summary": "Ignored"}')

        provider = CodexVisualAnalysisProvider(
            command='/usr/bin/codex', runner=runner, help_text='--image <path>')
        result, ticks = await self.while_ticking(self.analyze(provider))
        self.assertEqual(result.summary, 'Threaded')
        self.assertGreater(ticks, 5)

    def test_parser_skips_a_prompt_echo(self):
        text = 'Return JSON {"summary": str}\n{"summary": "Real", "confidence": 0.9}\nDone {'
        self.assertEqual(_extract_observation_json(text)['summary'], 'Real')
        self.assertIsNone(_extract_observation_json('no json {"summary": str}'))


if __name__ == '__main__':
    unittest.main()
