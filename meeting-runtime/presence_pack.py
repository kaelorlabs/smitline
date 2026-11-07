"""Presence packs: the pictures a meeting agent's camera tile plays for each presence state.

A pack is a folder holding manifest.json and the frames, as WebP or PNG files:

    {
      "format": "smitline-presence-pack",
      "version": 2,
      "name": "relay-arch",
      "width": 1280, "height": 720, "fps": 24,
      "captionColor": "#2E2B3A",
      "clips": {
        "offstage":  {"frames": ["offstage.webp"], "loop": true},
        "arrive":    {"frames": ["arrive/000.webp", "arrive/001.webp"], "loop": false},
        "listening": {"frames": ["listening/000.webp"], "loop": true},
        "speaking":  {"frames": ["speaking/000.webp", "speaking/001.webp"], "loop": true,
                      "mouth": {"x": 664, "y": 196, "width": 72, "height": 64,
                                "levels": ["speaking/mouth-1.png", "speaking/mouth-2.png"]}}
      },
      "states": {"joining": "offstage", "listening": "listening", "speaking": "speaking",
                 "finalizing": "leave"},
      "arrival": "arrive"
    }

- A looping clip repeats while its state lasts. A clip with "loop": false plays once and holds its
  last frame (a goodbye that ends with the stage empty).
- "states" maps presence states (visual_presence.VISUAL_STATES) to clips. "listening" is
  required; the states a pack leaves out show the listening clip.
- "arrival", optional, plays once the first time the agent is present (admitted and live), before
  that state's clip: people see it walk in rather than appear.
- "captionColor", optional, is the colour of the caption under the picture ("Smith's AI
  assistant"); white otherwise.
- "persona", optional, says who the character is and how it sounds, in a few sentences. The agent
  appears as the character, so the voice is given it as part of its instructions.
- "voice", optional, is the character's GPT-Live voice. It is used for the meeting unless the
  brief names a voice or the owner set a meeting voice (SMITLINE_MEETING_VOICE), before the
  installation's SMITLINE_VOICE (the phone calls' voice).
- A file named by several frames is read once and counts once towards the pack's size, so a loop
  that shows a picture again costs nothing more.

Version 2 adds a mouth that follows the voice. A clip may carry "mouth": the pictures of the open
mouth from quiet to loud ("levels", up to 8) and the box they cover, in frame pixels. The clip's own
frames show the mouth closed; while the clip plays, the camera draws the picture for the voice's
loudness at that moment into the box, so nothing else may change inside the box from one frame of
the clip to the next. In a version 2 pack every frame is the pack's size and every mouth picture
the box's size. Version 1 packs (no mouth) load as before.

Frames are pictures only: the camera adds the caption itself, so a pack holds no names or text.
"""
from dataclasses import dataclass
import base64
import json
from pathlib import Path
import re

from visual_presence import VISUAL_STATES

FORMAT = 'smitline-presence-pack'
VERSIONS = (1, 2)
MAX_FRAME_BYTES = 512 * 1024
# The Relay arch at 1280x720 and 24 fps comes to 12 MB of pictures (WebP quality 0.9), at 640x360
# to 4 MB; 24 MB leaves a 1280x720 pack room for fewer repeated pictures or a higher quality.
MAX_PACK_BYTES = 24 * 1024 * 1024
MAX_FRAMES_PER_CLIP = 360
MAX_CLIPS = 32
MAX_MOUTH_LEVELS = 8
MAX_PERSONA = 800
MEDIA_TYPES = {'.webp': 'image/webp', '.png': 'image/png'}
_CLIP_NAME = re.compile(r'^[a-z][a-z0-9_-]{0,31}$')
_VOICE = re.compile(r'^[a-z][a-z0-9_-]{0,31}$')
_COLOUR = re.compile(r'^#[0-9A-Fa-f]{6}$')


@dataclass(frozen=True)
class Mouth:
    """The open-mouth pictures of a clip, quiet to loud, and the box they are drawn into."""
    x: int
    y: int
    width: int
    height: int
    levels: tuple
    media_types: tuple


@dataclass(frozen=True)
class Clip:
    frames: tuple
    media_types: tuple
    loop: bool
    mouth: object = None


@dataclass(frozen=True)
class PresencePack:
    name: str
    width: int
    height: int
    fps: int
    clips: dict
    states: dict
    arrival: object = None
    caption_color: object = None
    persona: str = ''
    voice: str = ''
    version: int = 1

    def clip_for(self, state):
        """The clip a presence state shows; listening's for states the pack leaves out."""
        return self.states.get(state) or self.states['listening']

    def pictures(self):
        """Every distinct picture of the pack (frames and mouths), once each, in pack order."""
        seen = {}
        for clip in self.clips.values():
            pairs = list(zip(clip.frames, clip.media_types))
            if clip.mouth is not None:
                pairs += list(zip(clip.mouth.levels, clip.mouth.media_types))
            for data, media_type in pairs:
                seen.setdefault(data, media_type)
        return list(seen.items())

    @property
    def total_bytes(self):
        return sum(len(data) for data, _type in self.pictures())

    def page_payload(self):
        """The pack as JSON for the camera page. Each distinct picture is in "pictures" once, as
        base64; clips name their frames and mouth pictures by their number in that list."""
        numbers = {}
        pictures = []
        for data, media_type in self.pictures():
            numbers[data] = len(pictures)
            pictures.append({'type': media_type, 'data': base64.b64encode(data).decode('ascii')})
        clips = {}
        for name, clip in self.clips.items():
            entry = {'loop': clip.loop, 'frames': [numbers[data] for data in clip.frames]}
            if clip.mouth is not None:
                mouth = clip.mouth
                entry['mouth'] = {'x': mouth.x, 'y': mouth.y, 'width': mouth.width,
                                  'height': mouth.height,
                                  'levels': [numbers[data] for data in mouth.levels]}
            clips[name] = entry
        return {
            'width': self.width,
            'height': self.height,
            'fps': self.fps,
            'arrival': self.arrival,
            'captionColor': self.caption_color,
            'states': dict(self.states),
            'clips': clips,
            'pictures': pictures,
        }


def picture_size(data):
    """(width, height) from a PNG's or WebP's header, or None if it cannot be read."""
    if data.startswith(b'\x89PNG\r\n\x1a\n') and len(data) >= 24 and data[12:16] == b'IHDR':
        return int.from_bytes(data[16:20], 'big'), int.from_bytes(data[20:24], 'big')
    if len(data) >= 30 and data[:4] == b'RIFF' and data[8:12] == b'WEBP':
        chunk = data[12:16]
        if chunk == b'VP8 ' and data[23:26] == b'\x9d\x01\x2a':
            return (int.from_bytes(data[26:28], 'little') & 0x3FFF,
                    int.from_bytes(data[28:30], 'little') & 0x3FFF)
        if chunk == b'VP8L' and data[20] == 0x2F:
            bits = int.from_bytes(data[21:25], 'little')
            return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
        if chunk == b'VP8X':
            return int.from_bytes(data[24:27], 'little') + 1, int.from_bytes(data[27:30], 'little') + 1
    return None


def _fail(message):
    raise ValueError(f'Presence pack: {message}')


def _int(manifest, key, low, high, where=''):
    value = manifest.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or not low <= value <= high:
        _fail(f'{where}"{key}" must be a whole number from {low} to {high}')
    return value


def _frame_path(root, relative):
    if not isinstance(relative, str) or not relative or '\\' in relative or relative.startswith('/'):
        _fail(f'frame paths are relative paths inside the pack, not {relative!r}')
    parts = relative.split('/')
    if any(part in ('', '.', '..') for part in parts):
        _fail(f'frame paths are relative paths inside the pack, not {relative!r}')
    path = (root / relative).resolve()
    if root not in path.parents:
        _fail(f'frame {relative!r} is outside the pack')
    media_type = MEDIA_TYPES.get(path.suffix.lower())
    if media_type is None:
        _fail(f'frame {relative!r} must be a .webp or .png file')
    return path, media_type


def _read_frame(path, relative, media_type):
    try:
        size = path.stat().st_size
    except OSError:
        _fail(f'frame {relative!r} is missing')
    if size > MAX_FRAME_BYTES:
        _fail(f'frame {relative!r} is over {MAX_FRAME_BYTES // 1024} KB')
    data = path.read_bytes()
    is_png = data.startswith(b'\x89PNG\r\n\x1a\n')
    is_webp = len(data) >= 12 and data[:4] == b'RIFF' and data[8:12] == b'WEBP'
    if (media_type == 'image/png' and not is_png) or (media_type == 'image/webp' and not is_webp):
        _fail(f'frame {relative!r} is not the picture its name says')
    return data


class _Reader:
    """Reads a pack's pictures: each file once, however many frames name it, within the limits."""

    def __init__(self, root, version):
        self.root = root
        self.version = version
        self.files = {}
        self.total = 0

    def picture(self, relative, size):
        path, media_type = _frame_path(self.root, relative)
        data = self.files.get(path)
        if data is None:
            data = self.files[path] = _read_frame(path, relative, media_type)
            self.total += len(data)
            if self.total > MAX_PACK_BYTES:
                _fail(f'the pictures come to more than {MAX_PACK_BYTES // (1024 * 1024)} MB')
        actual = picture_size(data)
        if self.version >= 2 and actual != size:
            found = f'{actual[0]}x{actual[1]}' if actual else 'of a size its header does not give'
            _fail(f'picture {relative!r} must be {size[0]}x{size[1]}, not {found}')
        return data, media_type


def _mouth(reader, clip_name, raw, width, height):
    if not isinstance(raw, dict):
        _fail(f'clip {clip_name!r}: "mouth" must give "x", "y", "width", "height" and "levels"')
    where = f'clip {clip_name!r} mouth: '
    box_width = _int(raw, 'width', 2, width, where)
    box_height = _int(raw, 'height', 2, height, where)
    x = _int(raw, 'x', 0, width - box_width, where)
    y = _int(raw, 'y', 0, height - box_height, where)
    levels = raw.get('levels')
    if not isinstance(levels, list) or not 1 <= len(levels) <= MAX_MOUTH_LEVELS:
        _fail(f'{where}"levels" must list 1 to {MAX_MOUTH_LEVELS} pictures, quiet to loud')
    pictures = [reader.picture(relative, (box_width, box_height)) for relative in levels]
    return Mouth(x, y, box_width, box_height, tuple(data for data, _type in pictures),
                 tuple(media_type for _data, media_type in pictures))


def load_presence_pack(path):
    """Read and check a pack folder (or its manifest.json); raises ValueError saying what is wrong."""
    path = Path(path)
    manifest_path = path if path.name == 'manifest.json' else path / 'manifest.json'
    root = manifest_path.parent.resolve()
    try:
        manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    except FileNotFoundError:
        _fail(f'no manifest.json in {root}')
    except (OSError, ValueError) as exc:
        _fail(f'manifest.json cannot be read: {exc}')
    version = manifest.get('version') if isinstance(manifest, dict) else None
    if (not isinstance(manifest, dict) or manifest.get('format') != FORMAT
            or isinstance(version, bool) or version not in VERSIONS):
        _fail(f'manifest.json must say "format": "{FORMAT}", "version": 1 or 2')

    width = _int(manifest, 'width', 64, 1920)
    height = _int(manifest, 'height', 64, 1080)
    if width % 2 or height % 2:
        _fail('"width" and "height" must be even, for the video encoder')
    fps = _int(manifest, 'fps', 1, 30)
    name = manifest.get('name') or root.name
    if not isinstance(name, str) or len(name) > 64:
        _fail('"name" must be a short text')

    raw_clips = manifest.get('clips')
    if not isinstance(raw_clips, dict) or not raw_clips or len(raw_clips) > MAX_CLIPS:
        _fail(f'"clips" must name 1 to {MAX_CLIPS} clips')
    reader = _Reader(root, version)
    clips = {}
    for clip_name, raw in raw_clips.items():
        if not _CLIP_NAME.match(str(clip_name)):
            _fail(f'clip name {clip_name!r} must be short lowercase letters, digits, - or _')
        if not isinstance(raw, dict) or not isinstance(raw.get('loop'), bool):
            _fail(f'clip {clip_name!r} needs "frames" and "loop": true or false')
        relatives = raw.get('frames')
        if not isinstance(relatives, list) or not 1 <= len(relatives) <= MAX_FRAMES_PER_CLIP:
            _fail(f'clip {clip_name!r} must have 1 to {MAX_FRAMES_PER_CLIP} frames')
        pictures = [reader.picture(relative, (width, height)) for relative in relatives]
        mouth = None
        if 'mouth' in raw:
            if version < 2:
                _fail(f'clip {clip_name!r} has a "mouth", which needs "version": 2')
            mouth = _mouth(reader, clip_name, raw['mouth'], width, height)
        clips[clip_name] = Clip(tuple(data for data, _type in pictures),
                                tuple(media_type for _data, media_type in pictures),
                                raw['loop'], mouth)

    raw_states = manifest.get('states')
    if not isinstance(raw_states, dict) or 'listening' not in raw_states:
        _fail('"states" must map presence states to clips, "listening" at least')
    states = {}
    for state, clip_name in raw_states.items():
        if state not in VISUAL_STATES:
            _fail(f'unknown presence state {state!r}; the states are {", ".join(VISUAL_STATES)}')
        if clip_name not in clips:
            _fail(f'state {state!r} shows clip {clip_name!r}, which the pack does not have')
        states[state] = clip_name
    for state in VISUAL_STATES:
        states.setdefault(state, states['listening'])

    arrival = manifest.get('arrival')
    if arrival is not None and arrival not in clips:
        _fail(f'"arrival" names clip {arrival!r}, which the pack does not have')
    caption_color = manifest.get('captionColor')
    if caption_color is not None and not (isinstance(caption_color, str) and _COLOUR.match(caption_color)):
        _fail('"captionColor" must be a colour like #2E2B3A')
    persona = manifest.get('persona') or ''
    if not isinstance(persona, str) or len(persona) > MAX_PERSONA or any(
            ord(char) < 32 for char in persona):
        _fail(f'"persona" must be one paragraph of at most {MAX_PERSONA} characters')
    voice = manifest.get('voice') or ''
    if not isinstance(voice, str) or (voice and not _VOICE.match(voice)):
        _fail('"voice" must be a voice name such as bossa')
    return PresencePack(name, width, height, fps, clips, states, arrival, caption_color,
                        ' '.join(persona.split()), voice, version)
