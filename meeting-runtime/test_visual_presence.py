import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from visual_presence import (
    AVATAR_MAX_BYTES, DEFAULT_AVATAR_DATA_URI, Presence, apply_platform_camera,
    encode_avatar_bytes, load_avatar_from_path, map_visual_state, parse_camera_settings,
    presence_public_fields, rendered_state_is_private,
)


PNG = (
    b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01'
    b'\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f\x00\x00\x01'
    b'\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82'
)


class MappingTests(unittest.TestCase):
    def test_lifecycle_work_and_speech_restore_listening(self):
        self.assertEqual(map_visual_state({'stage': 'joining'}), 'joining')
        self.assertEqual(map_visual_state({'stage': 'waiting_for_admission'}), 'joining')
        self.assertEqual(map_visual_state({'stage': 'live', 'floorState': 'listening'}), 'listening')
        self.assertEqual(map_visual_state({
            'stage': 'live', 'backend_status': 'working', 'floorState': 'listening',
        }), 'working')
        self.assertEqual(map_visual_state({
            'stage': 'live', 'backend_status': 'working', 'floorState': 'speaking',
        }), 'speaking')
        self.assertEqual(map_visual_state({
            'stage': 'live', 'backend_status': 'working', 'floorState': 'listening',
        }), 'working')
        self.assertEqual(map_visual_state({
            'stage': 'live', 'backend_status': 'idle', 'floorState': 'listening',
        }), 'listening')
        self.assertEqual(map_visual_state({'stage': 'live', 'finalizing': True}), 'finalizing')
        self.assertEqual(map_visual_state({'stage': 'needs_attention'}), 'needs_attention')
        self.assertEqual(map_visual_state({'stage': 'meeting_ended'}), 'ended')

    def test_working_does_not_change_listening_flag(self):
        state = {'stage': 'live', 'listening': True, 'backend_status': 'working',
                 'floorState': 'listening'}
        self.assertEqual(map_visual_state(state), 'working')
        self.assertTrue(state['listening'])

    def test_health_payload_has_no_private_strings(self):
        payload = presence_public_fields({
            'stage': 'live', 'cameraEnabled': True, 'cameraState': 'on',
            'visualState': 'working', 'captions': ['secret customer transcript'],
        })
        dumped = json.dumps(payload)
        self.assertFalse(rendered_state_is_private(dumped))
        self.assertNotIn('transcript', dumped.lower())
        self.assertNotIn('customer', dumped.lower())
        self.assertEqual(payload['visualState'], 'working')


class AvatarTests(unittest.TestCase):
    def test_encodes_safe_types_and_rejects_unsafe(self):
        uri = encode_avatar_bytes(PNG, 'image/png')
        self.assertTrue(uri.startswith('data:image/png;base64,'))
        with self.assertRaises(ValueError):
            encode_avatar_bytes(b'not-an-image')
        with self.assertRaises(ValueError):
            encode_avatar_bytes(PNG + b'x' * (AVATAR_MAX_BYTES + 1))
        with self.assertRaises(ValueError):
            encode_avatar_bytes(b'<svg xmlns="http://www.w3.org/2000/svg" onload="alert(1)"></svg>')
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'mark.png'
            path.write_bytes(PNG)
            loaded = load_avatar_from_path(path)
            self.assertTrue(loaded.startswith('data:image/png;base64,'))
            link = Path(directory) / 'link.png'
            os.symlink(path, link)
            with self.assertRaises(ValueError):
                load_avatar_from_path(link)
            with self.assertRaises(ValueError):
                load_avatar_from_path('relative.png')

    def test_create_payload_copies_avatar_and_defaults(self):
        parsed = parse_camera_settings(None)
        self.assertTrue(parsed['enabled'])
        self.assertTrue(parsed['defaultOn'])
        self.assertIsNone(parsed['avatarDataUri'])
        disabled = parse_camera_settings({'enabled': False, 'defaultOn': False})
        self.assertFalse(disabled['enabled'])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'logo.png'
            path.write_bytes(PNG)
            copied = parse_camera_settings({'avatarPath': str(path)})
            self.assertTrue(copied['avatarDataUri'].startswith('data:image/png'))
        with self.assertRaises(ValueError):
            parse_camera_settings({'unknown': True})


class PresenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_platform_camera_degrades_without_raising(self):
        class Blocked:
            capabilities = SimpleNamespace(camera=True)
            async def enable_camera(self):
                raise RuntimeError('host policy')
            async def get_camera_state(self):
                return 'blocked'
        state = {}
        result = await apply_platform_camera(Blocked(), enabled=True, default_on=True, state=state)
        self.assertEqual(result, 'degraded')
        self.assertEqual(state['cameraState'], 'degraded')
        self.assertEqual(state['degradedReason'], 'platform_blocked')
        off = {}
        self.assertEqual(await apply_platform_camera(Blocked(), enabled=False, default_on=True, state=off), 'off')
        self.assertEqual(off['cameraState'], 'off')

    async def test_presence_sync_is_deterministic_and_restores(self):
        calls = []
        feed = SimpleNamespace(set_visual_state=lambda name: calls.append(name))
        state = {'stage': 'live', 'floorState': 'listening', 'backend_status': 'idle',
                 'cameraEnabled': True}
        presence = Presence(state, feed)
        self.assertEqual(presence.sync(), 'listening')
        state['backend_status'] = 'working'
        self.assertEqual(presence.sync(), 'working')
        state['floorState'] = 'speaking'
        self.assertEqual(presence.sync(), 'speaking')
        state['floorState'] = 'listening'
        self.assertEqual(presence.sync(), 'working')
        state['backend_status'] = 'idle'
        self.assertEqual(presence.sync(), 'listening')
        self.assertEqual(calls, ['listening', 'working', 'speaking', 'working', 'listening'])

    def test_default_mark_is_colleague_branded(self):
        self.assertIn('Colleague%20AI', DEFAULT_AVATAR_DATA_URI)
        self.assertFalse(rendered_state_is_private(DEFAULT_AVATAR_DATA_URI))


if __name__ == '__main__':
    unittest.main()
