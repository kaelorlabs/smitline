import base64
import json
import os
import tempfile
import unittest
from pathlib import Path

from presence_pack import MAX_FRAME_BYTES, load_presence_pack
from visual_presence import VISUAL_STATES

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
        self.assertEqual(pack.total_bytes, 4 * len(WEBP) + len(PNG))

    def test_the_manifest_path_itself_works_too(self):
        write_pack(self.root)
        self.assertEqual(load_presence_pack(Path(self.root, 'manifest.json')).name, 'test')

    def test_page_payload_carries_each_frame_as_base64_with_its_type(self):
        write_pack(self.root)
        payload = load_presence_pack(self.root).page_payload()
        json.dumps(payload)
        frame = payload['clips']['listening']['frames'][0]
        self.assertEqual(frame['type'], 'image/png')
        self.assertEqual(base64.b64decode(frame['data']), PNG)
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
