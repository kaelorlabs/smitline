import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';

import { adoptOldSettings, migrateData, renamedEnvText } from '../src/old-names.mjs';

test('old COLLEAGUE_ variables set, and win over, their SMITLINE_ names', () => {
  const env = adoptOldSettings({ COLLEAGUE_MEETING_IMAGE: 'mine:local', SMITLINE_MEETING_IMAGE: 'image-default', SMITLINE_ROOT: '/data' });
  assert.equal(env.SMITLINE_MEETING_IMAGE, 'mine:local');
  assert.equal(env.SMITLINE_ROOT, '/data');
});

test('.env keys are renamed and a key already under its new name wins', () => {
  assert.equal(
    renamedEnvText('COLLEAGUE_OWNER_NAME=Sam\r\nexport COLLEAGUE_VOICE=cedar\nCOLLEAGUE_PHONE_PROVIDER=twilio\nSMITLINE_PHONE_PROVIDER=signalwire\n'),
    'SMITLINE_OWNER_NAME=Sam\r\nexport SMITLINE_VOICE=cedar\nSMITLINE_PHONE_PROVIDER=signalwire\n',
  );
});

test('migrateData moves .colleague to .smitline and leaves a link', (t) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'smitline-old-names-'));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  fs.mkdirSync(path.join(root, '.colleague'));
  fs.writeFileSync(path.join(root, '.colleague', 'mcp.token'), 't');
  fs.writeFileSync(path.join(root, '.env'), 'COLLEAGUE_OWNER_NAME=Sam\n', { mode: 0o600 });
  assert.deepEqual(migrateData(root), ['.env', '.colleague']);
  assert.equal(fs.readFileSync(path.join(root, '.smitline', 'mcp.token'), 'utf8'), 't');
  assert.ok(fs.lstatSync(path.join(root, '.colleague')).isSymbolicLink());
  assert.equal(fs.readFileSync(path.join(root, '.env'), 'utf8'), 'SMITLINE_OWNER_NAME=Sam\n');
  assert.equal(fs.statSync(path.join(root, '.env')).mode & 0o777, 0o600);
  assert.deepEqual(migrateData(root), []);
});
