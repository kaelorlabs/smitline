import os
import tempfile
import unittest
from pathlib import Path

from old_names import adopt_old_settings, migrate_data, renamed_env_text


class OldSettingsTests(unittest.TestCase):
    def test_old_variables_set_and_win_over_the_new_names(self):
        env = {'COLLEAGUE_MEETING_IMAGE': 'mine:local', 'SMITLINE_MEETING_IMAGE': 'image-default',
               'COLLEAGUE_VOICE': 'cedar', 'SMITLINE_ROOT': '/data'}
        adopt_old_settings(env)
        self.assertEqual(env['SMITLINE_MEETING_IMAGE'], 'mine:local')
        self.assertEqual(env['SMITLINE_VOICE'], 'cedar')
        self.assertEqual(env['SMITLINE_ROOT'], '/data')

    def test_env_keys_are_renamed_and_duplicates_dropped(self):
        text = ('# keys\nCOLLEAGUE_OWNER_NAME=Sam\nexport COLLEAGUE_VOICE = cedar\n'
                'COLLEAGUE_PHONE_PROVIDER=twilio\nSMITLINE_PHONE_PROVIDER=signalwire\nOPENAI_API_KEY=x\n')
        self.assertEqual(renamed_env_text(text),
                         '# keys\nSMITLINE_OWNER_NAME=Sam\nexport SMITLINE_VOICE = cedar\n'
                         'SMITLINE_PHONE_PROVIDER=signalwire\nOPENAI_API_KEY=x\n')


class MigrateDataTests(unittest.TestCase):
    def test_moves_the_folder_and_leaves_a_link(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / '.colleague').mkdir()
            (root / '.colleague' / 'mcp.token').write_text('t')
            env = root / '.env'
            env.write_text('COLLEAGUE_OWNER_NAME=Sam\n')
            os.chmod(env, 0o600)
            self.assertEqual(migrate_data(root), ['.env', '.colleague'])
            self.assertEqual((root / '.smitline' / 'mcp.token').read_text(), 't')
            self.assertTrue((root / '.colleague').is_symlink())
            self.assertEqual((root / '.colleague' / 'mcp.token').read_text(), 't')
            self.assertEqual(env.read_text(), 'SMITLINE_OWNER_NAME=Sam\n')
            self.assertEqual(env.stat().st_mode & 0o777, 0o600)
            self.assertEqual(migrate_data(root), [])

    def test_leaves_an_existing_new_folder_alone(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / '.colleague').mkdir()
            (root / '.smitline').mkdir()
            self.assertEqual(migrate_data(root), [])
            self.assertFalse((root / '.colleague').is_symlink())


if __name__ == '__main__':
    unittest.main()
