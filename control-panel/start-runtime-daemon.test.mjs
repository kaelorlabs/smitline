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
  assert.match(script, /SMITLINE_PYTHON_VENV|\$ROOT\/\.venv/);
  assert.equal(script.includes('.env'), false);
  const requirements = fs.readFileSync(path.join(ROOT, 'meeting-runtime', 'requirements-daemon.txt'), 'utf8');
  assert.match(requirements, /aiohttp>=3\.11\.18/);
});

test('SMITLINE_PYTHON without aiohttp fails closed before the daemon starts', () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'smitline-python-'));
  const fakePython = path.join(dir, 'python');
  fs.writeFileSync(fakePython, '#!/bin/sh\nexit 1\n', { mode: 0o755 });
  const result = spawnSync('bash', [SCRIPT], {
    encoding: 'utf8',
    env: { ...process.env, SMITLINE_PYTHON: fakePython },
  });
  assert.equal(result.status, 1);
  assert.match(result.stderr, /aiohttp/);
  assert.equal(result.stderr.includes('daemon_main.py'), false);
});

function fakeTools({ python = 1, dockerInfo = 0 } = {}) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'smitline-launcher-'));
  const bin = path.join(dir, 'bin');
  fs.mkdirSync(bin);
  fs.writeFileSync(path.join(bin, 'python3'), `#!/bin/sh\nexit ${python}\n`, { mode: 0o755 });
  fs.writeFileSync(path.join(bin, 'docker'), `#!/bin/sh
echo "$@" >> "$DOCKER_LOG"
case "$1" in
  info) [ "$2" = "-f" ] && echo "Ubuntu 24.04"; exit ${dockerInfo} ;;
  image) exit 1 ;;
  build) cat > /dev/null; exit 0 ;;
esac
exit 0
`, { mode: 0o755 });
  const log = path.join(dir, 'docker.log');
  const env = {
    PATH: `${bin}:/usr/bin:/bin`,
    DOCKER_LOG: log,
    SMITLINE_PYTHON_VENV: path.join(dir, 'venv'),
    SMITLINE_DAEMON_PORT: '9876',
    OPENAI_API_KEY: 'sk-launcher-test-value',
  };
  return { env, log: () => (fs.existsSync(log) ? fs.readFileSync(log, 'utf8') : '') };
}

test('without a usable Python the launcher builds the image and runs the daemon in Docker', () => {
  const tools = fakeTools();
  const result = spawnSync('bash', [SCRIPT], { encoding: 'utf8', env: tools.env });
  assert.equal(result.status, 0, result.stderr);
  const lines = tools.log().trim().split('\n');
  assert.ok(lines.some((line) => /^build --quiet --label smitline\.hash=[0-9a-f]{12} -t smitline-daemon:local -f Dockerfile\.daemon -$/.test(line)));
  const run = lines.find((line) => line.startsWith('run '));
  assert.match(run, /--name smitline-daemon-9876 --network host/);
  assert.ok(run.includes(`-v ${ROOT}:${ROOT} -w ${ROOT}`));
  assert.match(run, /--user \d+:\d+/);
  assert.match(run, /-e OPENAI_API_KEY( |$)/);
  assert.equal(run.includes('sk-launcher-test-value'), false);
  assert.match(run, /meeting-runtime\/daemon_main\.py --host 127\.0\.0\.1 --port 9876$/);
});

test('the launcher explains what to install when neither Python nor Docker can run the daemon', () => {
  const tools = fakeTools({ dockerInfo: 1 });
  const result = spawnSync('bash', [SCRIPT], { encoding: 'utf8', env: tools.env });
  assert.equal(result.status, 1);
  assert.match(result.stderr, /Docker \(recommended\) or Python/);
  assert.equal(tools.log().includes('run '), false);
  const bogus = spawnSync('bash', [SCRIPT], { encoding: 'utf8', env: { ...tools.env, SMITLINE_DAEMON_RUNTIME: 'cloud' } });
  assert.equal(bogus.status, 1);
  assert.match(bogus.stderr, /auto, host, or docker/);
});
