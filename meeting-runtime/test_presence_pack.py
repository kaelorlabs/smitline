import base64
import json
import os
import tempfile
import unittest
from pathlib import Path

from presence_pack import MAX_FRAME_BYTES, load_presence_pack
from visual_presence import VISUAL_STATES

try:
    import PIL  # noqa: F401
    HAS_PILLOW = True
except ImportError:
    HAS_PILLOW = False

PNG = b'\x89PNG\r\n\x1a\n' + b'\x00' * 24
WEBP = b'RIFF\x10\x00\x00\x00WEBPVP8 ' + b'\x00' * 16


def write_pack(root, manifest=None, files=None):
    files = files if files is not None else {
        'offstage.webp': WEBP, 'arrive/000.webp': WEBP, 'arrive/001.webp': WEBP,
        'listening/000.png': PNG, 'leave/000.webp': WEBP,
    }
    for relative, data in files.items():
        path = Path(root, relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    manifest = manifest if manifest is not None else {
        'format': 'smitline-presence-pack', 'version': 1, 'name': 'test',
        'width': 640, 'height': 360, 'fps': 12, 'captionColor': '#2E2B3A',
        'clips': {
            'offstage': {'frames': ['offstage.webp'], 'loop': True},
            'arrive': {'frames': ['arrive/000.webp', 'arrive/001.webp'], 'loop': False},
            'listening': {'frames': ['listening/000.png'], 'loop': True},
            'leave': {'frames': ['leave/000.webp'], 'loop': False},
        },
        'states': {'joining': 'offstage', 'listening': 'listening', 'finalizing': 'leave',
                   'ended': 'offstage'},
        'arrival': 'arrive',
    }
    Path(root, 'manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
    return manifest


class PresencePackTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = self.temp.name

    def tearDown(self):
        self.temp.cleanup()

    def edited(self, **changes):
        manifest = write_pack(self.root)
        manifest.update(changes)
        Path(self.root, 'manifest.json').write_text(json.dumps(manifest), encoding='utf-8')

    def test_loads_a_pack_and_fills_the_states_it_leaves_out_with_listening(self):
        write_pack(self.root)
        pack = load_presence_pack(self.root)
        self.assertEqual((pack.width, pack.height, pack.fps), (640, 360, 12))
        self.assertEqual(set(pack.states), set(VISUAL_STATES))
        self.assertEqual(pack.clip_for('working'), 'listening')
        self.assertEqual(pack.clip_for('finalizing'), 'leave')
        self.assertEqual(pack.arrival, 'arrive')
        self.assertEqual(pack.clips['arrive'].frames, (WEBP, WEBP))
        self.assertFalse(pack.clips['arrive'].loop)
        self.assertIsNone(pack.clips['arrive'].mouth)
        self.assertEqual(pack.version, 1)
        # The same picture counts once, however many frames show it.
        self.assertEqual(pack.total_bytes, len(WEBP) + len(PNG))

    def test_the_manifest_path_itself_works_too(self):
        write_pack(self.root)
        self.assertEqual(load_presence_pack(Path(self.root, 'manifest.json')).name, 'test')

    def test_page_payload_carries_each_picture_once_as_base64_with_its_type(self):
        write_pack(self.root)
        payload = load_presence_pack(self.root).page_payload()
        json.dumps(payload)
        pictures = payload['pictures']
        self.assertEqual(len(pictures), 2)  # the WebP all the WebP frames share, and the PNG
        frame = pictures[payload['clips']['listening']['frames'][0]]
        self.assertEqual(frame['type'], 'image/png')
        self.assertEqual(base64.b64decode(frame['data']), PNG)
        self.assertEqual(payload['clips']['arrive']['frames'], [0, 0])
        self.assertNotIn('mouth', payload['clips']['listening'])
        self.assertEqual(payload['states']['speaking'], 'listening')
        self.assertEqual(payload['captionColor'], '#2E2B3A')

    def test_frames_must_stay_inside_the_pack(self):
        outside = Path(self.root).parent / 'outside-frame.webp'
        outside.write_bytes(WEBP)
        self.addCleanup(outside.unlink)
        for relative in ('../outside-frame.webp', '/etc/passwd', 'a/../../outside-frame.webp', 'a\\b.webp'):
            with self.subTest(relative=relative):
                manifest = write_pack(self.root)
                manifest['clips']['listening']['frames'] = [relative]
                Path(self.root, 'manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
                with self.assertRaisesRegex(ValueError, 'inside the pack|outside the pack'):
                    load_presence_pack(self.root)

    @unittest.skipUnless(hasattr(os, 'symlink'), 'needs symlinks')
    def test_a_symlink_out_of_the_pack_is_refused(self):
        outside = Path(self.root).parent / 'outside-link-target.webp'
        outside.write_bytes(WEBP)
        self.addCleanup(outside.unlink)
        write_pack(self.root)
        link = Path(self.root, 'listening', 'linked.webp')
        os.symlink(outside, link)
        manifest = json.loads(Path(self.root, 'manifest.json').read_text())
        manifest['clips']['listening']['frames'] = ['listening/linked.webp']
        Path(self.root, 'manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'outside the pack'):
            load_presence_pack(self.root)

    def test_refuses_what_a_camera_cannot_use(self):
        cases = {
            'fps': ({'fps': 0}, 'fps'),
            'odd size': ({'width': 641}, 'even'),
            'no listening': ({'states': {'joining': 'offstage'}}, 'listening'),
            'unknown state': ({'states': {'listening': 'listening', 'dancing': 'arrive'}}, 'unknown presence state'),
            'missing clip': ({'states': {'listening': 'nope'}}, 'does not have'),
            'bad arrival': ({'arrival': 'nope'}, 'does not have'),
            'bad colour': ({'captionColor': 'navy'}, 'colour'),
            'wrong format': ({'format': 'something-else'}, 'format'),
        }
        for label, (changes, message) in cases.items():
            with self.subTest(label):
                self.edited(**changes)
                with self.assertRaisesRegex(ValueError, message):
                    load_presence_pack(self.root)

    def test_refuses_a_frame_that_is_not_the_picture_its_name_says(self):
        write_pack(self.root, files={
            'offstage.webp': PNG, 'arrive/000.webp': WEBP, 'arrive/001.webp': WEBP,
            'listening/000.png': PNG, 'leave/000.webp': WEBP,
        })
        with self.assertRaisesRegex(ValueError, 'not the picture'):
            load_presence_pack(self.root)

    def test_refuses_an_oversized_frame(self):
        write_pack(self.root, files={
            'offstage.webp': WEBP + b'\x00' * MAX_FRAME_BYTES, 'arrive/000.webp': WEBP,
            'arrive/001.webp': WEBP, 'listening/000.png': PNG, 'leave/000.webp': WEBP,
        })
        with self.assertRaisesRegex(ValueError, 'over'):
            load_presence_pack(self.root)

    def test_a_missing_manifest_says_so(self):
        with self.assertRaisesRegex(ValueError, 'no manifest.json'):
            load_presence_pack(self.root)

    def test_a_mouth_needs_version_2(self):
        manifest = write_pack(self.root)
        manifest['clips']['listening']['mouth'] = {'x': 0, 'y': 0, 'width': 8, 'height': 8,
                                                   'levels': ['listening/000.png']}
        Path(self.root, 'manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'needs "version": 2'):
            load_presence_pack(self.root)

    def test_only_versions_1_and_2_load(self):
        for version in (0, 3, True, '2'):
            with self.subTest(version=version):
                self.edited(version=version)
                with self.assertRaisesRegex(ValueError, '"version": 1 or 2'):
                    load_presence_pack(self.root)


def png(width, height):
    """A PNG header, as far as its size: enough for the loader, which does not decode."""
    return (b'\x89PNG\r\n\x1a\n' + b'\x00\x00\x00\x0dIHDR' + width.to_bytes(4, 'big')
            + height.to_bytes(4, 'big') + b'\x08\x02\x00\x00\x00' + b'\x00' * 8)


def webp(width, height, kind='VP8 '):
    """A WebP header of each kind Chrome and libwebp write, as far as its size."""
    if kind == 'VP8 ':
        body = b'\x00\x00\x00\x9d\x01\x2a' + width.to_bytes(2, 'little') + height.to_bytes(2, 'little')
    elif kind == 'VP8L':
        bits = (width - 1) | ((height - 1) << 14)
        body = b'\x2f' + bits.to_bytes(4, 'little')
    else:
        body = b'\x10\x00\x00\x00' + (width - 1).to_bytes(3, 'little') + (height - 1).to_bytes(3, 'little')
    body += b'\x00' * 8
    chunk = kind.encode() + len(body).to_bytes(4, 'little') + body
    return b'RIFF' + (len(chunk) + 4).to_bytes(4, 'little') + b'WEBP' + chunk


def write_v2_pack(root, **changes):
    """A 1280x720 pack whose speaking clip has a mouth: two body pictures, the first shown twice."""
    files = {
        'offstage.webp': webp(1280, 720), 'listening/000.webp': webp(1280, 720, 'VP8L'),
        'speaking/000.webp': webp(1280, 720) + b'a', 'speaking/001.webp': webp(1280, 720, 'VP8X'),
        'speaking/mouth-1.png': png(72, 64), 'speaking/mouth-2.png': png(72, 64) + b'b',
    }
    files.update(changes.pop('files', {}))
    manifest = {
        'format': 'smitline-presence-pack', 'version': 2, 'name': 'test',
        'width': 1280, 'height': 720, 'fps': 24,
        'clips': {
            'offstage': {'frames': ['offstage.webp'], 'loop': True},
            'listening': {'frames': ['listening/000.webp'], 'loop': True},
            'speaking': {'frames': ['speaking/000.webp', 'speaking/001.webp', 'speaking/000.webp'],
                         'loop': True,
                         'mouth': {'x': 664, 'y': 196, 'width': 72, 'height': 64,
                                   'levels': ['speaking/mouth-1.png', 'speaking/mouth-2.png']}},
        },
        'states': {'joining': 'offstage', 'listening': 'listening', 'speaking': 'speaking'},
    }
    manifest.update(changes)
    return write_pack(root, manifest, files)


class MouthPackTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = self.temp.name

    def tearDown(self):
        self.temp.cleanup()

    def test_loads_a_speaking_clip_with_its_mouth(self):
        write_v2_pack(self.root)
        pack = load_presence_pack(self.root)
        self.assertEqual((pack.version, pack.width, pack.height, pack.fps), (2, 1280, 720, 24))
        speaking = pack.clips['speaking']
        self.assertIs(speaking.frames[0], speaking.frames[2])  # one file, read once
        mouth = speaking.mouth
        self.assertEqual((mouth.x, mouth.y, mouth.width, mouth.height), (664, 196, 72, 64))
        self.assertEqual(mouth.levels, (png(72, 64), png(72, 64) + b'b'))
        self.assertEqual(mouth.media_types, ('image/png', 'image/png'))
        self.assertIsNone(pack.clips['listening'].mouth)
        self.assertEqual(len(pack.pictures()), 6)

    def test_page_payload_names_the_mouth_pictures_by_number(self):
        write_v2_pack(self.root)
        payload = load_presence_pack(self.root).page_payload()
        json.dumps(payload)
        speaking = payload['clips']['speaking']
        self.assertEqual(speaking['frames'][0], speaking['frames'][2])
        mouth = speaking['mouth']
        self.assertEqual({k: mouth[k] for k in ('x', 'y', 'width', 'height')},
                         {'x': 664, 'y': 196, 'width': 72, 'height': 64})
        pictures = payload['pictures']
        self.assertEqual([base64.b64decode(pictures[n]['data']) for n in mouth['levels']],
                         [png(72, 64), png(72, 64) + b'b'])
        self.assertEqual(len(pictures), 6)

    def test_a_file_shown_many_times_counts_once_towards_the_limit(self):
        import presence_pack
        big = webp(1280, 720) + b'\x00' * (400 * 1024)
        write_v2_pack(self.root, files={'speaking/000.webp': big})
        original = presence_pack.MAX_PACK_BYTES
        presence_pack.MAX_PACK_BYTES = 600 * 1024
        try:
            pack = load_presence_pack(self.root)  # 400 KB shown twice: under 600 KB
            self.assertLess(pack.total_bytes, 600 * 1024)
            write_v2_pack(self.root, files={'speaking/000.webp': big, 'speaking/001.webp': big + b'x'})
            with self.assertRaisesRegex(ValueError, 'more than'):
                load_presence_pack(self.root)
        finally:
            presence_pack.MAX_PACK_BYTES = original

    def test_refuses_pictures_of_the_wrong_size(self):
        cases = {
            'a frame': {'files': {'speaking/001.webp': webp(640, 360)}},
            'a frame it cannot measure': {'files': {'listening/000.webp': b'RIFF\x10\x00\x00\x00WEBPVP8 ' + b'\x00' * 16}},
            'a mouth picture': {'files': {'speaking/mouth-2.png': png(72, 66)}},
        }
        for label, changes in cases.items():
            with self.subTest(label):
                write_v2_pack(self.root, **changes)
                with self.assertRaisesRegex(ValueError, 'must be'):
                    load_presence_pack(self.root)

    def test_refuses_a_mouth_the_camera_cannot_draw(self):
        cases = {
            'outside the frame': ({'x': 1240}, '"x" must be'),
            'no levels': ({'levels': []}, 'levels'),
            'too many levels': ({'levels': ['speaking/mouth-1.png'] * 9}, 'levels'),
            'a box of nothing': ({'width': 0}, '"width" must be'),
            'not a box': (None, 'mouth'),
        }
        for label, (change, message) in cases.items():
            with self.subTest(label):
                manifest = write_v2_pack(self.root)
                mouth = manifest['clips']['speaking']['mouth']
                if change is None:
                    manifest['clips']['speaking']['mouth'] = 'open'
                else:
                    mouth.update(change)
                Path(self.root, 'manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
                with self.assertRaisesRegex(ValueError, message):
                    load_presence_pack(self.root)

    def test_reads_the_size_of_each_kind_of_picture(self):
        from presence_pack import picture_size
        self.assertEqual(picture_size(png(1280, 720)), (1280, 720))
        for kind in ('VP8 ', 'VP8L', 'VP8X'):
            with self.subTest(kind=kind):
                self.assertEqual(picture_size(webp(1280, 720, kind)), (1280, 720))
        self.assertIsNone(picture_size(b'not a picture'))

    @unittest.skipUnless(HAS_PILLOW, 'needs Pillow')
    def test_reads_the_size_of_pictures_a_real_encoder_wrote(self):
        import io
        from PIL import Image
        from presence_pack import picture_size
        for options in ({'format': 'WEBP', 'quality': 90}, {'format': 'WEBP', 'lossless': True},
                        {'format': 'PNG'}):
            with self.subTest(options=options):
                out = io.BytesIO()
                Image.new('RGB', (322, 180), (120, 140, 200)).save(out, **options)
                self.assertEqual(picture_size(out.getvalue()), (322, 180))


class PersonaTests(unittest.TestCase):
    PERSONA = ("You appear in this meeting as the Relay arch, Smitline's mascot.\n  Sound warm "
               "and gentle.")

    def test_a_pack_carries_its_character_tidied_to_one_paragraph(self):
        with tempfile.TemporaryDirectory() as root:
            manifest = write_pack(root)
            manifest['persona'] = self.PERSONA.replace('\n', ' ')
            Path(root, 'manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
            self.assertEqual(load_presence_pack(root).persona,
                             "You appear in this meeting as the Relay arch, Smitline's mascot. "
                             'Sound warm and gentle.')
            for bad in ('x' * 801, 'two\nlines', 7):
                with self.subTest(bad=str(bad)[:12]):
                    manifest['persona'] = bad
                    Path(root, 'manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
                    with self.assertRaisesRegex(ValueError, 'persona'):
                        load_presence_pack(root)
            del manifest['persona']
            Path(root, 'manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
            self.assertEqual(load_presence_pack(root).persona, '')

    def test_the_voice_is_given_the_character_it_appears_as(self):
        import bridge
        from runtime_config import RuntimeConfig
        runtime = RuntimeConfig.from_environ({'SMITLINE_MEETING_INTRO': '0'})
        self.assertNotIn('Who you are in this meeting', bridge.build_session_config(runtime)['instructions'])
        with tempfile.TemporaryDirectory() as root:
            manifest = write_pack(root)
            manifest['persona'] = 'You appear in this meeting as the Relay arch. Sound warm.'
            Path(root, 'manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
            pack = load_presence_pack(root)
        self.assertIs(bridge.with_persona(runtime, None), runtime)
        instructions = bridge.build_session_config(bridge.with_persona(runtime, pack))['instructions']
        self.assertIn('Who you are in this meeting: You appear in this meeting as the Relay arch. '
                      'Sound warm.', instructions)

    def test_the_character_speaks_in_its_own_voice_unless_the_brief_names_one(self):
        import bridge
        from runtime_config import RuntimeConfig
        from unittest.mock import patch
        runtime = RuntimeConfig.from_environ({'SMITLINE_MEETING_INTRO': '0', 'SMITLINE_VOICE': 'marin'})
        with tempfile.TemporaryDirectory() as root:
            manifest = write_pack(root)
            manifest['voice'] = 'bossa'
            Path(root, 'manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
            pack = load_presence_pack(root)
            self.assertEqual(pack.voice, 'bossa')
            for bad in ('Bossa!', 7):
                with self.subTest(bad=bad):
                    manifest['voice'] = bad
                    Path(root, 'manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
                    with self.assertRaisesRegex(ValueError, 'voice'):
                        load_presence_pack(root)
        self.assertEqual(bridge.with_persona(runtime, pack).voice, 'bossa')
        config = bridge.build_session_config(bridge.with_persona(runtime, pack))
        self.assertEqual(config['audio']['output'], {'voice': 'bossa'})
        # A voice the brief asked for wins over the character's.
        self.assertEqual(bridge.with_persona(runtime, pack, {'voice': 'quartz'}).voice, 'marin')
        # A voice GPT-Live does not have leaves the meeting's own.
        import dataclasses
        odd = dataclasses.replace(pack, voice='nosuchvoice')
        with patch('builtins.print'):
            self.assertEqual(bridge.with_persona(runtime, odd).voice, 'marin')


class SpeakerNoteTests(unittest.TestCase):
    def test_says_who_is_speaking_and_marks_the_owner(self):
        from meeting_intro import speaker_note
        self.assertEqual(speaker_note('Priya Shah', 'Arun Raj'),
                         'Priya Shah is speaking now. When you answer them, you can use their name. '
                         'Do not announce this.')
        self.assertTrue(speaker_note('Arun Raj', 'Arun Raj').startswith(
            'Arun Raj (probably Arun, the person you work for) is speaking now.'))
        self.assertTrue(speaker_note('  Priya   Shah ', '').startswith('Priya Shah is speaking now.'))


class Writer:
    audio_format = object()
    chunk_size = 480


class CameraChoiceTests(unittest.TestCase):
    def test_settings_choose_the_mascot_style_and_its_pack(self):
        from runtime_config import RuntimeConfig
        runtime = RuntimeConfig.from_environ({'SMITLINE_MEETING_CAMERA_STYLE': 'Mascot',
                                              'SMITLINE_MEETING_CAMERA_PACK': ' /packs/relay-arch '})
        self.assertEqual((runtime.camera_style, runtime.camera_pack), ('mascot', '/packs/relay-arch'))
        with self.assertRaises(ValueError):
            RuntimeConfig.from_environ({'SMITLINE_MEETING_CAMERA_STYLE': 'video'})

    def test_a_readable_pack_gives_the_mascot_camera(self):
        import dataclasses
        import bridge
        from runtime_config import RuntimeConfig
        with tempfile.TemporaryDirectory() as root:
            write_pack(root)
            runtime = dataclasses.replace(RuntimeConfig.from_environ({}), camera_style='mascot',
                                          camera_pack=root)
            feed = bridge.camera_feed_for(runtime, Writer(), bridge.presence_pack_for(runtime))
        self.assertEqual(feed.style, 'mascot')
        self.assertEqual((feed.pack['fps'], feed.pack['arrival']), (12, 'arrive'))

    def test_a_pack_that_cannot_be_read_leaves_the_still_picture(self):
        import dataclasses
        from unittest.mock import patch
        import bridge
        from runtime_config import RuntimeConfig
        for pack_path in ('', '/nowhere/at/all'):
            with self.subTest(pack_path=pack_path):
                runtime = dataclasses.replace(RuntimeConfig.from_environ({}), camera_style='mascot',
                                              camera_pack=pack_path)
                with patch('builtins.print') as printed:
                    pack = bridge.presence_pack_for(runtime)
                    feed = bridge.camera_feed_for(runtime, Writer(), pack)
                self.assertEqual(feed.style, 'still')
                self.assertIsNone(feed.pack)
                self.assertIn('still picture instead', printed.call_args.args[0])


if __name__ == '__main__':
    unittest.main()
