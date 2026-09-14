import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { once } from 'node:events';
import test from 'node:test';
import { launcherActive } from './lifecycle.mjs';

test('a launcher terminated by Stop no longer blocks another start', async () => {
  const child = spawn(process.execPath, ['-e', 'setInterval(() => {}, 1000)']);
  try {
    await once(child, 'spawn');
    assert.equal(launcherActive(child), true);
    const exited = once(child, 'exit');
    child.kill('SIGTERM');
    await exited;
    assert.equal(child.exitCode, null);
    assert.equal(child.signalCode, 'SIGTERM');
    assert.equal(launcherActive(child), false);
  } finally {
    if (launcherActive(child)) child.kill('SIGKILL');
  }
});
