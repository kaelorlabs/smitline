import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';
import { fileURLToPath } from 'node:url';

const ROOT = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const SCRIPT = path.join(ROOT, 'start-runtime-daemon.sh');

test('daemon launcher has valid shell syntax and bootstraps aiohttp from the repo pin', () => {
  const syntax = spawnSync('bash', ['-n', SCRIPT], { encoding: 'utf8' });
  assert.equal(syntax.status, 0, syntax.stderr);
  const script = fs.readFileSync(SCRIPT, 'utf8');
  assert.match(script, /requirements-daemon\.txt/);
  assert.match(script, /COLLEAGUE_PYTHON_VENV|\$ROOT\/\.venv/);
  assert.equal(script.includes('.env'), false);
  const requirements = fs.readFileSync(path.join(ROOT, 'meeting-runtime', 'requirements-daemon.txt'), 'utf8');
  assert.match(requirements, /aiohttp>=3\.11\.18/);
});

test('COLLEAGUE_PYTHON without aiohttp fails closed before the daemon starts', () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'colleague-python-'));
  const fakePython = path.join(dir, 'python');
  fs.writeFileSync(fakePython, '#!/bin/sh\nexit 1\n', { mode: 0o755 });
  const result = spawnSync('bash', [SCRIPT], {
    encoding: 'utf8',
    env: { ...process.env, COLLEAGUE_PYTHON: fakePython },
  });
  assert.equal(result.status, 1);
  assert.match(result.stderr, /aiohttp/);
  assert.equal(result.stderr.includes('daemon_main.py'), false);
});
