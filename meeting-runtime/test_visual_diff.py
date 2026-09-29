"""Tile signatures, settling, animated-region masks, and reuse of analyzed screens."""
import random
import struct
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest import mock

import screen_share_pipeline
import visual_hash
from artifact_store import ArtifactStore
from screen_share import VisualObservation, parse_screen_share_settings
from screen_share_pipeline import ScreenShareBus, ScreenShareCaptureLoop, ScreenShareHost
from visual_analysis import VisualAnalysisProvider
from visual_diff import (
    ScreenChangeTracker, compare, frame_signature, is_significant, parse_mask,
    signature_from_rgb, tile_grid,
)
from visual_hash import decode_png_rgb


WHITE = (255, 255, 255)
INK = (25, 25, 30)
DEFAULT_MIN_CHANGE = 0.08


def _chunk(kind, payload):
    body = kind + payload
    return struct.pack('>I', len(payload)) + body + struct.pack('>I', zlib.crc32(body) & 0xffffffff)


def _predict(kind, a, b, c):
    if kind == 1:
        return a
    if kind == 2:
        return b
    if kind == 3:
        return (a + b) // 2
    if kind == 4:
        p = a + b - c
        pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
        return a if pa <= pb and pa <= pc else (b if pb <= pc else c)
    return 0


def encode_png(width, height, pixels, *, color_type=2, filters=None):
    """Tiny PNG encoder. filters=None writes unfiltered rows; otherwise cycles the given types."""
    channels = {0: 1, 2: 3, 4: 2, 6: 4}[color_type]
    stride = width * channels
    raw = bytearray()
    prior = bytes(stride)
    for y in range(height):
        row = bytes(pixels[y * stride:(y + 1) * stride])
        kind = 0 if filters is None else filters[y % len(filters)]
        raw.append(kind)
        if kind == 0:
            raw.extend(row)
        else:
            for i in range(stride):
                a = row[i - channels] if i >= channels else 0
                c = prior[i - channels] if i >= channels else 0
                raw.append((row[i] - _predict(kind, a, prior[i], c)) & 255)
        prior = row
    header = struct.pack('>IIBBBBB', width, height, 8, color_type, 0, 0, 0)
    return (b'\x89PNG\r\n\x1a\n' + _chunk(b'IHDR', header)
            + _chunk(b'IDAT', zlib.compress(bytes(raw), 6)) + _chunk(b'IEND', b''))


class Canvas:
    def __init__(self, width, height, color=WHITE):
        self.width = width
        self.height = height
        self.pixels = bytearray(bytes(color) * (width * height))

    def copy(self):
        clone = Canvas(1, 1)
        clone.width, clone.height, clone.pixels = self.width, self.height, bytearray(self.pixels)
        return clone

    def rect(self, x, y, w, h, color):
        left, right = max(0, x), min(self.width, x + w)
        if right <= left:
            return self
        for row in range(max(0, y), min(self.height, y + h)):
            start = (row * self.width + left) * 3
            self.pixels[start:start + (right - left) * 3] = bytes(color) * (right - left)
        return self

    def text(self, x, y, chars, *, seed, size=24, color=INK):
        """Draw glyph-like strokes: each character is two to four 2px bars in a size-high cell."""
        rng = random.Random(seed)
        advance = size * 3 // 5
        for index in range(chars):
            left = x + index * advance
            if rng.random() < 0.15:
                continue
            for _ in range(rng.randint(2, 4)):
                if rng.random() < 0.5:
                    self.rect(left + rng.randint(0, advance - 4), y, 2, size, color)
                else:
                    self.rect(left, y + rng.randint(0, size - 2), advance - 3, 2, color)
        return self

    def signature(self):
        return signature_from_rgb(self.width, self.height, bytes(self.pixels))

    def png(self):
        return encode_png(self.width, self.height, self.pixels)


def blend(left, right):
    mixed = left.copy()
    mixed.pixels = bytearray((a + b) // 2 for a, b in zip(left.pixels, right.pixels))
    return mixed


def slide(width=1280, height=720, bullets=2, seed=1):
    canvas = Canvas(width, height)
    canvas.rect(0, 0, width, height // 8, (31, 78, 121))
    canvas.text(width // 16, height // 32, 16, seed=seed * 100, size=height // 20, color=WHITE)
    for bullet in range(bullets):
        canvas.text(width // 10, height // 4 + bullet * height // 9, 24, seed=seed * 100 + bullet + 1,
                    size=height // 27)
    return canvas


def code_view(first_line, width=1280, height=720, line_height=18):
    canvas = Canvas(width, height, (30, 30, 30))
    rng = random.Random(5)
    lengths = [(rng.randint(0, 3), rng.randint(0, 40)) for _ in range(200)]
    for screen_row in range(height // line_height):
        indent, length = lengths[first_line + screen_row]
        canvas.text(40 + indent * 32, 6 + screen_row * line_height, length,
                    seed=first_line + screen_row, size=12, color=(210, 210, 210))
    return canvas


def cursor(canvas, x, y):
    return canvas.copy().rect(x, y, 12, 19, (0, 0, 0)).rect(x + 2, y + 2, 7, 13, WHITE)


def video(canvas, frame, box=(760, 380, 400, 240)):
    """Paint a block that changes every frame, like an embedded video or webcam thumbnail."""
    x, y, w, h = box
    shaded = canvas.copy()
    for row in range(y, y + h, 20):
        for column in range(x, x + w, 20):
            level = 220 if (frame + row // 20 + column // 20) % 2 else 40
            shaded.rect(column, row, 20, 20, (level, level, level))
    return shaded


class DecodeTests(unittest.TestCase):
    def tearDown(self):
        visual_hash.USE_PILLOW = True

    def test_every_filter_and_color_type_round_trips(self):
        rng = random.Random(3)
        width, height = 23, 17
        for color_type, channels in ((0, 1), (2, 3), (4, 2), (6, 4)):
            pixels = bytearray(rng.randrange(256) for _ in range(width * height * channels))
            stride = width * channels
            pixels[5 * stride:6 * stride] = pixels[4 * stride:5 * stride]
            for filters in ([0], [1], [2], [3], [4], [0, 1, 2, 3, 4]):
                png = encode_png(width, height, pixels, color_type=color_type, filters=filters)
                expected = bytearray()
                for index in range(width * height):
                    pixel = pixels[index * channels:(index + 1) * channels]
                    expected.extend(pixel[:3] if channels >= 3 else bytes((pixel[0],)) * 3)
                for use_pillow in (False, True):
                    visual_hash.USE_PILLOW = use_pillow
                    self.assertEqual(decode_png_rgb(png), (width, height, bytes(expected)),
                                     (color_type, filters, use_pillow))

    def test_zero_residual_rows_reproduce_the_row_above(self):
        canvas = Canvas(64, 36).rect(5, 5, 20, 10, INK).rect(30, 20, 20, 8, (200, 30, 30))
        png = encode_png(64, 36, canvas.pixels, filters=[4, 2])
        visual_hash.USE_PILLOW = False
        self.assertEqual(decode_png_rgb(png)[2], bytes(canvas.pixels))

    def test_pure_python_and_pillow_signatures_match(self):
        png = slide().png()
        visual_hash.USE_PILLOW = False
        pure = frame_signature(png)
        visual_hash.USE_PILLOW = True
        self.assertEqual(frame_signature(png), pure)

    def test_rejects_non_png_and_unsupported_formats(self):
        with self.assertRaises(ValueError):
            frame_signature(b'not a png at all, just bytes')
        header = struct.pack('>IIBBBBB', 4, 4, 16, 2, 0, 0, 0)
        sixteen_bit = (b'\x89PNG\r\n\x1a\n' + _chunk(b'IHDR', header)
                       + _chunk(b'IDAT', zlib.compress(b'\x00' * 100)) + _chunk(b'IEND', b''))
        with self.assertRaises(ValueError):
            decode_png_rgb(sixteen_bit)


class TileSignatureTests(unittest.TestCase):
    def test_grid_follows_aspect_ratio_and_small_frames(self):
        self.assertEqual(tile_grid(1920, 1080), (64, 36))
        self.assertEqual(tile_grid(1280, 720), (64, 36))
        self.assertEqual(tile_grid(1024, 768), (64, 48))
        self.assertEqual(tile_grid(1080, 1920), (64, 64))
        self.assertEqual(tile_grid(32, 32), (32, 32))
        self.assertEqual(tile_grid(3000, 20), (64, 1))
        signature = Canvas(7, 5).signature()
        self.assertEqual((signature.columns, signature.rows), (7, 5))

    def test_identical_frames_and_noise_do_not_change(self):
        base = slide(640, 360)
        self.assertEqual(compare(base.signature(), base.signature()).changed, frozenset())
        rng = random.Random(9)
        noisy = base.copy()
        noisy.pixels = bytearray(min(255, max(0, value + rng.randint(-6, 6))) for value in base.pixels)
        change = compare(base.signature(), noisy.signature())
        self.assertEqual(change.changed, frozenset())
        self.assertIsNone(change.box)
        self.assertFalse(is_significant(change, DEFAULT_MIN_CHANGE))

    def test_new_text_line_on_same_background_is_significant(self):
        before = slide(bullets=2)
        after = slide(bullets=3)
        change = compare(before.signature(), after.signature())
        self.assertTrue(is_significant(change, DEFAULT_MIN_CHANGE), change.score)
        # The third bullet spans y 340-366 and starts at x 128: tile rows 17-18 from column 6.
        left, top, right, bottom = change.box
        self.assertEqual((left, top, bottom), (6, 17, 19))
        self.assertGreaterEqual(right - left, 10)

    def test_same_template_new_slide_is_significant(self):
        change = compare(slide(seed=1).signature(), slide(seed=2).signature())
        self.assertTrue(is_significant(change, DEFAULT_MIN_CHANGE), change.score)

    def test_small_scroll_is_significant(self):
        change = compare(code_view(10).signature(), code_view(12).signature())
        self.assertTrue(is_significant(change, DEFAULT_MIN_CHANGE), change.score)
        self.assertGreater(change.fraction, 0.05)

    def test_mouse_cursor_is_below_threshold(self):
        base = slide()
        one = cursor(base, 600, 500)
        change = compare(base.signature(), one.signature())
        self.assertGreater(len(change.changed), 0)
        self.assertLessEqual(len(change.changed), 4)
        self.assertFalse(is_significant(change, DEFAULT_MIN_CHANGE))
        moved = cursor(base, 900, 200)
        self.assertFalse(is_significant(
            compare(one.signature(), moved.signature()), DEFAULT_MIN_CHANGE))

    def test_resolution_change_is_a_new_screen(self):
        change = compare(Canvas(320, 240).signature(), Canvas(320, 180).signature())
        self.assertEqual(change.fraction, 1.0)
        self.assertEqual(compare(None, Canvas(320, 180).signature()).score, 1.0)

    def test_mask_excludes_tiles_and_host_masks_are_validated(self):
        before, after = slide(bullets=2), slide(bullets=3)
        change = compare(before.signature(), after.signature())
        masked = compare(before.signature(), after.signature(), mask=change.changed)
        self.assertEqual(masked.changed, frozenset())
        signature = after.signature()
        self.assertEqual(parse_mask([1, 2, 3], signature), frozenset({1, 2, 3}))
        self.assertEqual(parse_mask([1, True], signature), frozenset())
        self.assertEqual(parse_mask([signature.tiles], signature), frozenset())
        self.assertEqual(parse_mask('1,2', signature), frozenset())
        self.assertEqual(parse_mask(list(range(signature.tiles)), signature), frozenset())


class TrackerTests(unittest.TestCase):
    def feed(self, tracker, canvases):
        selected = []
        for index, canvas in enumerate(canvases):
            signature = canvas.signature()
            if tracker.observe(signature) is not None:
                tracker.select(signature)
                selected.append(index)
        return selected

    def test_mid_transition_frame_waits_until_settled(self):
        first, second = slide(seed=1), slide(seed=2)
        middle = blend(first, second)
        tracker = ScreenChangeTracker(min_change=DEFAULT_MIN_CHANGE, settle_ticks=1)
        self.assertEqual(self.feed(tracker, [first, first, middle, second, second, second]), [1, 4])

    def test_settle_ticks_are_configurable(self):
        first, second = slide(seed=1), slide(seed=2)
        immediate = ScreenChangeTracker(min_change=DEFAULT_MIN_CHANGE, settle_ticks=0)
        self.assertEqual(self.feed(immediate, [first, first, second, second]), [0, 2])
        patient = ScreenChangeTracker(min_change=DEFAULT_MIN_CHANGE, settle_ticks=2)
        self.assertEqual(self.feed(patient, [first, first, first, second, second, second]), [2, 5])

    def test_continuous_scroll_is_selected_once_it_stops(self):
        frames = [code_view(0), code_view(0)] + [code_view(line) for line in (3, 6, 9)]
        frames += [code_view(9), code_view(9)]
        tracker = ScreenChangeTracker(min_change=DEFAULT_MIN_CHANGE, settle_ticks=1)
        self.assertEqual(self.feed(tracker, frames), [1, 5])

    def test_animated_region_is_masked_and_recovers(self):
        base = slide()
        tracker = ScreenChangeTracker(min_change=DEFAULT_MIN_CHANGE, settle_ticks=1)
        animated = [video(base, frame) for frame in range(8)]
        self.assertEqual(self.feed(tracker, animated), [4])
        masked = tracker.masked()
        self.assertEqual(masked, frozenset(
            row * 64 + column for row in range(19, 31) for column in range(38, 58)))
        with_text = [video(slide(bullets=3), frame) for frame in range(8, 11)]
        self.assertEqual(self.feed(tracker, with_text), [1])
        still = video(slide(bullets=3), 10)
        self.assertEqual(self.feed(tracker, [still, still]), [])
        self.assertEqual(tracker.masked(), masked)
        self.assertEqual(self.feed(tracker, [still]), [0])
        self.assertEqual(tracker.masked(), frozenset())
        changed = still.copy().text(780, 400, 20, seed=77, size=30, color=(250, 200, 0))
        self.assertEqual(self.feed(tracker, [changed, changed]), [1])

    def test_whole_screen_switches_do_not_build_a_mask(self):
        tracker = ScreenChangeTracker(min_change=DEFAULT_MIN_CHANGE, settle_ticks=1)
        light, dark = Canvas(320, 180), Canvas(320, 180, (0, 0, 0))
        self.feed(tracker, [light, dark, light, dark, light, dark, light, dark])
        self.assertEqual(tracker.masked(), frozenset())


class FakeAdapter:
    capabilities = type('C', (), {'shared_content': True})()

    def __init__(self, frames):
        self.frames = list(frames)

    async def capture_shared_content(self):
        located = type('S', (), {'available': True, 'confidence': 'high', 'reason': None})()
        return self.frames.pop(0), located


class CountingAnalyzer(VisualAnalysisProvider):
    def __init__(self):
        self.calls = 0

    def available(self):
        return True

    async def analyze(self, png, *, meeting_id, frame_artifact_id, context='', timestamp=None,
                      observation_id=None, cancel=None):
        self.calls += 1
        return VisualObservation.from_dict({
            'id': observation_id, 'meetingId': meeting_id,
            'timestamp': timestamp or '2026-09-28T00:00:00Z',
            'summary': 'Slide number %d' % self.calls, 'frameArtifactId': frame_artifact_id,
            'confidence': 0.8, 'visibleText': ['Roadmap'],
        })


class CaptureLoopTests(unittest.IsolatedAsyncioTestCase):
    async def run_loop(self, frames, settings):
        with tempfile.TemporaryDirectory() as directory:
            bus = ScreenShareBus(directory, 'mtg-diff01')
            loop = ScreenShareCaptureLoop(
                adapter=FakeAdapter([frame.png() for frame in frames]),
                settings=dict({'enabled': True}, **settings), bus=bus, state={})
            written = []
            for index in range(len(frames)):
                status = await loop.tick()
                self.assertIsNone(status.get('degradedReason'))
                for path, meta in bus.list_inbox():
                    written.append((index, meta))
                    bus.drop_inbox(path)
            return written

    async def test_capture_selects_settled_frames_only(self):
        first, second = slide(640, 360, seed=1), slide(640, 360, seed=2)
        frames = [first, first, cursor(first, 300, 200), blend(first, second), second, second]
        written = await self.run_loop(frames, {})
        self.assertEqual([index for index, _meta in written], [1, 5])
        self.assertNotIn('maskedTiles', written[0][1])

    async def test_capture_without_settling_selects_immediately(self):
        first, second = slide(640, 360, seed=1), slide(640, 360, seed=2)
        written = await self.run_loop([first, second, second], {'settleTicks': 0})
        self.assertEqual([index for index, _meta in written], [0, 1])

    async def test_masked_tiles_are_sent_to_the_host(self):
        base = slide(640, 360)
        frames = [video(base, frame, box=(380, 190, 200, 120)) for frame in range(6)]
        written = await self.run_loop(frames, {})
        self.assertEqual([index for index, _meta in written], [4])
        masked = written[0][1]['maskedTiles']
        self.assertTrue(masked and all(isinstance(item, int) for item in masked))


class ReuseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = ArtifactStore(Path(self.directory.name) / 'artifacts')
        self.analyzer = CountingAnalyzer()
        self.host = ScreenShareHost(artifacts=self.store, analyzer=self.analyzer)
        self.events = []
        self.stored = []

    async def asyncTearDown(self):
        self.directory.cleanup()

    async def ingest(self, canvas, **extra):
        return await self.host.ingest_png(
            'mtg-diff01', canvas.png(), settings={'enabled': True},
            emit=lambda kind, **payload: self.events.append((kind, payload)),
            store_observation=self.stored.append, **extra)

    async def test_returning_to_an_earlier_slide_reuses_the_observation(self):
        slides = [slide(640, 360, seed=seed) for seed in (1, 2, 3)]
        for canvas in slides:
            result = await self.ingest(canvas)
            self.assertNotIn('reused', result)
        self.assertEqual(self.analyzer.calls, 3)
        artifacts = len(self.store.list('mtg-diff01'))
        self.events.clear()
        again = await self.ingest(cursor(slides[0], 300, 250))
        self.assertEqual(self.analyzer.calls, 3)
        self.assertTrue(again['reused'])
        observation = again['observation']
        self.assertTrue(observation['reused'])
        self.assertEqual(observation['summary'], 'Slide number 1')
        self.assertEqual(observation['frameArtifactId'], self.stored[0]['frameArtifactId'])
        self.assertNotEqual(observation['id'], self.stored[0]['id'])
        self.assertEqual([kind for kind, _payload in self.events], ['screen_share.observation'])
        self.assertTrue(self.events[0][1]['observation']['reused'])
        self.assertEqual(self.stored[-1], observation)
        self.assertEqual(len(self.store.list('mtg-diff01')), artifacts)
        repeat = await self.ingest(cursor(slides[0], 300, 250))
        self.assertEqual(repeat, {'skipped': 'duplicate'})
        fresh = await self.ingest(slide(640, 360, seed=4))
        self.assertIsNone(fresh.get('reused'))
        self.assertEqual(self.analyzer.calls, 4)

    async def test_reuse_honors_the_container_mask(self):
        base = slide(640, 360, seed=1)
        box = (380, 190, 200, 120)
        await self.ingest(video(base, 1, box))
        await self.ingest(slide(640, 360, seed=2))
        unmasked = await self.ingest(video(base, 2, box))
        self.assertIsNone(unmasked.get('reused'))
        self.assertEqual(self.analyzer.calls, 3)
        await self.ingest(slide(640, 360, seed=2))
        region = [row * 64 + column for row in range(19, 31) for column in range(38, 58)]
        masked = await self.ingest(video(base, 3, box), masked_tiles=region)
        self.assertTrue(masked['reused'])
        self.assertEqual(self.analyzer.calls, 3)

    async def test_reuse_skips_missing_frames_and_cache_is_bounded(self):
        first = slide(640, 360, seed=1)
        await self.ingest(first)
        await self.ingest(slide(640, 360, seed=2))
        with mock.patch.object(self.store, 'get', side_effect=FileNotFoundError):
            missing = await self.ingest(first)
        self.assertIsNone(missing.get('reused'))
        self.assertEqual(self.analyzer.calls, 3)
        with mock.patch.object(screen_share_pipeline, 'SEEN_SCREENS', 2):
            for seed in (5, 6, 7):
                await self.ingest(slide(640, 360, seed=seed))
        self.assertEqual(len(self.host._seen['mtg-diff01']), 2)
        self.assertEqual(self.analyzer.calls, 6)

    async def test_unchanged_and_backpressure_are_preserved(self):
        base = slide(640, 360)
        await self.ingest(base)
        nudged = cursor(base, 100, 100)
        self.assertEqual(await self.ingest(nudged), {'skipped': 'unchanged'})
        self.host._inflight.add('mtg-diff01')
        self.assertEqual(await self.ingest(slide(640, 360, seed=9)), {'skipped': 'backpressure'})
        self.host._inflight.discard('mtg-diff01')
        self.assertEqual(self.analyzer.calls, 1)


class ReusedObservationTests(unittest.IsolatedAsyncioTestCase):
    async def test_reused_flag_round_trips_and_reaches_the_voice_session(self):
        payload = {
            'id': 'obs-1', 'meetingId': 'mtg-diff01', 'timestamp': '2026-09-28T00:00:00Z',
            'summary': 'A roadmap slide', 'frameArtifactId': 'art-1', 'confidence': 0.7,
        }
        plain = VisualObservation.from_dict(payload)
        self.assertNotIn('reused', plain.to_dict())
        reused = VisualObservation.from_dict(dict(payload, reused=True))
        self.assertTrue(reused.to_dict()['reused'])
        with self.assertRaises(ValueError):
            VisualObservation.from_dict(dict(payload, reused='yes'))
        with tempfile.TemporaryDirectory() as directory:
            bus = ScreenShareBus(directory, 'mtg-diff01')
            sent = []
            loop = ScreenShareCaptureLoop(
                adapter=FakeAdapter([]), settings={'enabled': True}, bus=bus, state={},
                send=sent.append)
            bus.write_outbox(reused)
            await loop.drain_observations()
        self.assertEqual(len(sent), 1)
        self.assertIn('earlier screen', sent[0]['content'])
        self.assertIn('A roadmap slide', sent[0]['content'])


class SettingsTests(unittest.TestCase):
    def test_existing_payloads_still_parse(self):
        legacy = {
            'enabled': True, 'captureIntervalMs': 4000, 'minChange': 0.08, 'maxFrames': 20,
            'maxBytes': 8_000_000, 'retentionSeconds': 3600,
        }
        settings = parse_screen_share_settings(legacy)
        self.assertEqual(settings['minChange'], 0.08)
        self.assertEqual(settings['settleTicks'], 1)
        self.assertEqual(parse_screen_share_settings({'minChange': 0})['minChange'], 0.0)
        self.assertEqual(parse_screen_share_settings(None)['settleTicks'], 1)
        self.assertEqual(parse_screen_share_settings(settings), settings)

    def test_settle_ticks_are_bounded(self):
        self.assertEqual(parse_screen_share_settings({'settleTicks': 0})['settleTicks'], 0)
        self.assertEqual(parse_screen_share_settings({'settleTicks': 5})['settleTicks'], 5)
        for bad in (-1, 6, 1.5, True, '1'):
            with self.assertRaises(ValueError):
                parse_screen_share_settings({'settleTicks': bad})
        with self.assertRaises(ValueError):
            parse_screen_share_settings({'minChange': 1.5})
        with self.assertRaises(ValueError):
            parse_screen_share_settings({'settleTicks': 1, 'maskWindow': 4})


if __name__ == '__main__':
    unittest.main()
