"""Tile signatures, settle-before-select, and animated-region masks for shared screens.

A frame is split into about 64x36 tiles (rows follow the aspect ratio). Each tile keeps its
mean luminance and mean horizontal and vertical luminance gradient over at most 6x6 sampled
pixels, so a 1920x1080 frame costs about 83k pixel samples (~45 ms in CPython) after decoding.
The change score is the square root of the changed-tile fraction: roughly the side of the
changed area relative to the frame, so minChange 0.08 needs about 15 of 2304 tiles.
"""
from collections import deque
from dataclasses import dataclass
from math import sqrt

from visual_hash import decode_png_rgb


GRID_COLUMNS = 64
MAX_GRID_ROWS = 64
SAMPLES_PER_TILE = 6
LUMA_TOLERANCE = 8
EDGE_TOLERANCE = 8
MASK_WINDOW = 6
MASK_AFTER = 4
LOCAL_CHANGE_LIMIT = 0.5


@dataclass(frozen=True)
class FrameSignature:
    columns: int
    rows: int
    luma: bytes
    edge_x: bytes
    edge_y: bytes

    @property
    def tiles(self):
        return self.columns * self.rows


@dataclass(frozen=True)
class TileChange:
    changed: frozenset
    fraction: float
    score: float
    box: tuple = None


def tile_grid(width, height):
    columns = max(1, min(GRID_COLUMNS, width))
    rows = max(1, min(MAX_GRID_ROWS, height, round(columns * height / width)))
    return columns, rows


def _spans(length, count):
    return [((index * length) // count, ((index + 1) * length) // count) for index in range(count)]


def _positions(start, end, length):
    count = min(SAMPLES_PER_TILE, end - start)
    positions = []
    for index in range(count):
        at = start + ((2 * index + 1) * (end - start)) // (2 * count)
        positions.append((at, at + 1 if at + 1 < length else max(at - 1, 0)))
    return positions


def frame_signature(png):
    width, height, rgb = decode_png_rgb(png)
    return signature_from_rgb(width, height, rgb)


def signature_from_rgb(width, height, rgb):
    if width < 1 or height < 1 or len(rgb) != width * height * 3:
        raise ValueError('RGB payload does not match dimensions')
    columns, rows = tile_grid(width, height)
    stride = width * 3
    samples = [[(3 * x, 3 * nx) for x, nx in _positions(start, end, width)]
               for start, end in _spans(width, columns)]
    luma = bytearray(columns * rows)
    edge_x = bytearray(columns * rows)
    edge_y = bytearray(columns * rows)
    for row, (top, bottom) in enumerate(_spans(height, rows)):
        ys = _positions(top, bottom, height)
        totals = [[0, 0, 0] for _ in range(columns)]
        for y, ny in ys:
            line = rgb[y * stride:(y + 1) * stride]
            below = rgb[ny * stride:(ny + 1) * stride]
            for column, points in enumerate(samples):
                level = across = down = 0
                for i, j in points:
                    value = line[i] * 77 + line[i + 1] * 150 + line[i + 2] * 29
                    level += value
                    across += abs(value - line[j] * 77 - line[j + 1] * 150 - line[j + 2] * 29)
                    down += abs(value - below[i] * 77 - below[i + 1] * 150 - below[i + 2] * 29)
                total = totals[column]
                total[0] += level
                total[1] += across
                total[2] += down
        for column, (level, across, down) in enumerate(totals):
            count = len(ys) * len(samples[column]) * 256
            index = row * columns + column
            luma[index] = level // count
            edge_x[index] = across // count
            edge_y[index] = down // count
    return FrameSignature(columns, rows, bytes(luma), bytes(edge_x), bytes(edge_y))


def changed_tiles(left, right):
    """Tile indexes that differ, or None when the frames share no tile grid."""
    if left is None or right is None or (left.columns, left.rows) != (right.columns, right.rows):
        return None
    changed = []
    for index, (a, b, c, d, e, f) in enumerate(zip(
            left.luma, right.luma, left.edge_x, right.edge_x, left.edge_y, right.edge_y)):
        if abs(a - b) > LUMA_TOLERANCE or abs(c - d) > EDGE_TOLERANCE or abs(e - f) > EDGE_TOLERANCE:
            changed.append(index)
    return frozenset(changed)


def summarize(changed, signature, mask=frozenset()):
    tiles = signature.tiles
    if changed is None:
        changed = frozenset(range(tiles))
    changed = changed - mask
    fraction = len(changed) / tiles
    box = None
    if changed:
        columns = [index % signature.columns for index in changed]
        rows = [index // signature.columns for index in changed]
        box = (min(columns), min(rows), max(columns) + 1, max(rows) + 1)
    return TileChange(changed, fraction, sqrt(fraction), box)


def compare(left, right, mask=frozenset()):
    return summarize(changed_tiles(left, right), right, mask)


def is_significant(change, min_change):
    return bool(change.changed) and change.score >= min_change


def parse_mask(value, signature):
    if not isinstance(value, list) or len(value) > signature.tiles * LOCAL_CHANGE_LIMIT:
        return frozenset()
    for item in value:
        if isinstance(item, bool) or not isinstance(item, int) or not 0 <= item < signature.tiles:
            return frozenset()
    return frozenset(value)


class ScreenChangeTracker:
    """Select a capture once it has settled and differs from the last selected frame."""

    def __init__(self, *, min_change, settle_ticks=1, window=MASK_WINDOW, mask_after=MASK_AFTER):
        self.min_change = min_change
        self.settle_ticks = settle_ticks
        self.mask_after = mask_after
        self.previous = None
        self.selected = None
        self.stable_ticks = 0
        self._history = deque(maxlen=window)
        self._counts = {}

    def masked(self):
        return frozenset(tile for tile, count in self._counts.items() if count >= self.mask_after)

    def observe(self, signature):
        step = changed_tiles(self.previous, signature)
        if step is None:
            self._history.clear()
            self._counts.clear()
        elif len(step) <= signature.tiles * LOCAL_CHANGE_LIMIT:
            # Whole-screen switches say nothing about which regions keep animating.
            self._record(step)
        mask = self.masked()
        if step is None or is_significant(summarize(step, signature, mask), self.min_change):
            self.stable_ticks = 0
        else:
            self.stable_ticks += 1
        self.previous = signature
        if self.stable_ticks < self.settle_ticks:
            return None
        change = compare(self.selected, signature, mask)
        if self.selected is not None and not is_significant(change, self.min_change):
            return None
        return change

    def select(self, signature):
        self.selected = signature

    def _record(self, changed):
        if len(self._history) == self._history.maxlen:
            for tile in self._history[0]:
                self._counts[tile] -= 1
                if not self._counts[tile]:
                    del self._counts[tile]
        self._history.append(changed)
        for tile in changed:
            self._counts[tile] = self._counts.get(tile, 0) + 1
