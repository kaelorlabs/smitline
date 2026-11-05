import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import test from 'node:test';
import { readEnv, validateSetting } from '../src/setup.mjs';

const cli = fileURLToPath(new URL('../src/smitline.mjs', import.meta.url));

function runCli(args) {
  return new Promise((resolve) => {
    const child = spawn(process.execPath, [cli, ...args], { stdio: ['ignore', 'pipe', 'pipe'] });
    let stdout = '';
    let stderr = '';
    child.stdout.on('data', (chunk) => { stdout += chunk; });
    child.stderr.on('data', (chunk) => { stderr += chunk; });
    child.on('close', (code) => resolve({ code, stdout, stderr }));
  });
}

test('the meeting voice is a setting of its own, checked like the phone calls\' one', () => {
  assert.equal(validateSetting('SMITLINE_MEETING_VOICE', 'cinder'), 'cinder');
  assert.throws(() => validateSetting('SMITLINE_MEETING_VOICE', 'alloy'), /SMITLINE_MEETING_VOICE must be one of: marin/);
  const env = { SMITLINE_EXTRA_VOICES: 'aurora' };
  assert.equal(validateSetting('SMITLINE_MEETING_VOICE', 'aurora', { env }), 'aurora');
});

test('setup voice --meeting sets the meeting voice, and --meeting default goes back to the phone one', async (t) => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'smitline-voices-'));
  t.after(() => fs.rm(root, { recursive: true, force: true }));
  await fs.writeFile(path.join(root, '.env'), 'SMITLINE_VOICE=quartz\n');
  let run = await runCli(['setup', 'voice', '--meeting', 'cinder', '--root', root]);
  assert.equal(run.code, 0, run.stderr);
  let shown = JSON.parse(run.stdout);
  assert.deepEqual([shown.voice, shown.meetingVoice], ['quartz', 'cinder']);
  assert.ok(shown.voices.includes('cinder'));
  assert.equal(readEnv(root).SMITLINE_MEETING_VOICE, 'cinder');

  run = await runCli(['setup', 'voice', '--meeting', 'default', '--root', root]);
  assert.equal(run.code, 0, run.stderr);
  shown = JSON.parse(run.stdout);
  assert.deepEqual([shown.voice, shown.meetingVoice], ['quartz', 'quartz']);
  assert.equal(await fs.readFile(path.join(root, '.env'), 'utf8'), 'SMITLINE_VOICE=quartz\n');

  const bad = await runCli(['setup', 'voice', '--meeting', 'alloy', '--root', root]);
  assert.equal(bad.code, 2);
  assert.match(bad.stderr, /--meeting must be one of/);
  assert.equal(readEnv(root).SMITLINE_MEETING_VOICE, undefined);

  run = await runCli(['setup', 'set', 'SMITLINE_MEETING_VOICE', 'vesper', '--root', root]);
  assert.equal(run.code, 0, run.stderr);
  assert.equal(readEnv(root).SMITLINE_MEETING_VOICE, 'vesper');
});
