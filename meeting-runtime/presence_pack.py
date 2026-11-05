"""Presence packs: the pictures a meeting agent's camera tile plays for each presence state.

A pack is a folder holding manifest.json and the frames, as WebP or PNG files:

    {
      "format": "smitline-presence-pack",
      "version": 1,
      "name": "relay-arch",
      "width": 640, "height": 360, "fps": 12,
      "captionColor": "#2E2B3A",
      "clips": {
        "offstage":  {"frames": ["offstage.webp"], "loop": true},
        "arrive":    {"frames": ["arrive/000.webp", "arrive/001.webp"], "loop": false},
        "listening": {"frames": ["listening/000.webp"], "loop": true}
      },
      "states": {"joining": "offstage", "listening": "listening", "finalizing": "leave"},
      "arrival": "arrive"
    }

- A looping clip repeats while its state lasts. A clip with "loop": false plays once and holds its
  last frame (a goodbye that ends with the stage empty).
- "states" maps presence states (visual_presence.VISUAL_STATES) to clips. "listening" is
  required; the states a pack leaves out show the listening clip.
- "arrival", optional, plays once the first time the agent leaves "joining" (it is admitted and
  live), before that state's clip: people see it walk in rather than appear.
- "captionColor", optional, is the colour of the caption under the picture ("Arun's AI
  assistant"); white otherwise.
- "persona", optional, says who the character is and how it sounds, in a few sentences. The agent
  appears as the character, so the voice is given it as part of its instructions.
- "voice", optional, is the character's GPT-Live voice. It is used for the meeting unless the
  brief names a voice or the owner set a meeting voice (SMITLINE_MEETING_VOICE), before the
  installation's SMITLINE_VOICE (the phone calls' voice).

Frames are pictures only: the camera adds the caption itself, so a pack holds no names or text.
"""
from dataclasses import dataclass
import base64
import json
from pathlib import Path
import re

from visual_presence import VISUAL_STATES

FORMAT = 'smitline-presence-pack'
MAX_FRAME_BYTES = 512 * 1024
MAX_PACK_BYTES = 16 * 1024 * 1024
MAX_FRAMES_PER_CLIP = 360
MAX_CLIPS = 32
MAX_PERSONA = 800
MEDIA_TYPES = {'.webp': 'image/webp', '.png': 'image/png'}
_CLIP_NAME = re.compile(r'^[a-z][a-z0-9_-]{0,31}$')
_VOICE = re.compile(r'^[a-z][a-z0-9_-]{0,31}$')
_COLOUR = re.compile(r'^#[0-9A-Fa-f]{6}$')


@dataclass(frozen=True)
class Clip:
    frames: tuple
    media_types: tuple
    loop: bool


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

    def clip_for(self, state):
        """The clip a presence state shows; listening's for states the pack leaves out."""
        return self.states.get(state) or self.states['listening']

    @property
    def total_bytes(self):
        return sum(len(frame) for clip in self.clips.values() for frame in clip.frames)

    def page_payload(self):
        """The pack as JSON for the camera page: frames as base64, decoded there into bitmaps."""
        return {
            'width': self.width,
            'height': self.height,
            'fps': self.fps,
            'arrival': self.arrival,
            'captionColor': self.caption_color,
            'states': dict(self.states),
            'clips': {
                name: {
                    'loop': clip.loop,
                    'frames': [
                        {'type': media_type, 'data': base64.b64encode(frame).decode('ascii')}
                        for frame, media_type in zip(clip.frames, clip.media_types)
                    ],
                }
                for name, clip in self.clips.items()
            },
        }


def _fail(message):
    raise ValueError(f'Presence pack: {message}')


def _int(manifest, key, low, high):
    value = manifest.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or not low <= value <= high:
        _fail(f'"{key}" must be a whole number from {low} to {high}')
    return value


def _frame(root, relative):
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
    return data, media_type


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
    if not isinstance(manifest, dict) or manifest.get('format') != FORMAT or manifest.get('version') != 1:
        _fail(f'manifest.json must say "format": "{FORMAT}", "version": 1')

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
    clips, total = {}, 0
    for clip_name, raw in raw_clips.items():
        if not _CLIP_NAME.match(str(clip_name)):
            _fail(f'clip name {clip_name!r} must be short lowercase letters, digits, - or _')
        if not isinstance(raw, dict) or not isinstance(raw.get('loop'), bool):
            _fail(f'clip {clip_name!r} needs "frames" and "loop": true or false')
        relatives = raw.get('frames')
        if not isinstance(relatives, list) or not 1 <= len(relatives) <= MAX_FRAMES_PER_CLIP:
            _fail(f'clip {clip_name!r} must have 1 to {MAX_FRAMES_PER_CLIP} frames')
        frames, types = [], []
        for relative in relatives:
            data, media_type = _frame(root, relative)
            total += len(data)
            if total > MAX_PACK_BYTES:
                _fail(f'the frames come to more than {MAX_PACK_BYTES // (1024 * 1024)} MB')
            frames.append(data)
            types.append(media_type)
        clips[clip_name] = Clip(tuple(frames), tuple(types), raw['loop'])

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
                        ' '.join(persona.split()), voice)
