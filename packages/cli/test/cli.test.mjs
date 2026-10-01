import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import test from 'node:test';
import { startFakeDaemon } from '../../sdk-typescript/test/fake-daemon.mjs';
import { DEFAULT_COLLEAGUE_ROOT, parseArgs } from '../src/smitline.mjs';

const cli = fileURLToPath(new URL('../src/smitline.mjs', import.meta.url));
const ZOOM = 'https://zoom.us/j/555111222';

test('installed CLI resolves the Smitline repository independently of caller cwd', () => {
  assert.equal(DEFAULT_COLLEAGUE_ROOT, path.resolve(path.dirname(cli), '../../..'));
});

test('CLI executes when invoked through an installed symlink', async (t) => {
  const installDir = await fs.mkdtemp(path.join(os.tmpdir(), 'colleague-cli-link-'));
  t.after(() => fs.rm(installDir, { recursive: true, force: true }));
  const installedCli = path.join(installDir, 'smitline');
  await fs.symlink(cli, installedCli);
  const result = await new Promise((resolve) => {
    const child = spawn(installedCli, ['help'], {
      cwd: installDir,
      env: process.env,
      stdio: ['ignore', 'pipe', 'pipe'],
    });
    let stdout = '';
    let stderr = '';
    child.stdout.on('data', (chunk) => { stdout += chunk; });
    child.stderr.on('data', (chunk) => { stderr += chunk; });
    child.on('close', (code) => resolve({ code, stdout, stderr }));
  });
  assert.equal(result.code, 0, result.stderr);
  assert.match(result.stdout, /^Usage:/);
  assert.match(result.stdout, /smitline call --meeting <url>/);
});

function runColleague(args, { env = {}, cwd, pipeThroughCat = false } = {}) {
  return new Promise((resolve) => {
    // spawn() hands the child a socket; `| cat` gives it a real pipe, as a shell user would.
    const [command, argv] = pipeThroughCat
      ? ['sh', ['-c', '"$0" "$@" | cat', process.execPath, cli, ...args]]
      : [process.execPath, [cli, ...args]];
    const child = spawn(command, argv, {
      cwd,
      env: { ...process.env, ...env },
      stdio: ['ignore', 'pipe', 'pipe'],
    });
    let stdout = '';
    let stderr = '';
    child.stdout.on('data', (chunk) => { stdout += chunk; });
    child.stderr.on('data', (chunk) => { stderr += chunk; });
    const timer = setTimeout(() => child.kill('SIGKILL'), 8000);
    child.on('close', (code, signal) => {
      clearTimeout(timer);
      resolve({ code, signal, stdout, stderr });
    });
  });
}

async function withDaemon(t, options = {}) {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'colleague-cli-'));
  t.after(() => fs.rm(root, { recursive: true, force: true }));
  await fs.writeFile(path.join(root, '.env'), 'COLLEAGUE_OWNER_NAME=Robin\n');
  const daemon = await startFakeDaemon({ root, ...options });
  t.after(() => daemon.close());
  return { root, daemon, common: ['--root', root, '--port', String(daemon.port)] };
}

test('--version and version print the release', async () => {
  const { version } = JSON.parse(await fs.readFile(path.join(DEFAULT_COLLEAGUE_ROOT, 'package.json'), 'utf8'));
  assert.match(version, /^\d+\.\d+\.\d+/);
  const env = { COLLEAGUE_VERSION: '' };
  for (const args of [['--version'], ['version']]) {
    const result = await runColleague(args, { env });
    assert.equal(result.code, 0, result.stderr);
    assert.equal(result.stdout, `smitline ${version}\n`);
  }
  const image = await runColleague(['--version'], { env: { COLLEAGUE_VERSION: '0.1.0-test' } });
  assert.equal(image.stdout, 'smitline 0.1.0-test\n');
  assert.match((await runColleague(['help'], { env })).stdout, /smitline version \| --version/);
});

test('parseArgs reads flags and values', () => {
  const args = parseArgs(['call', '--meeting', ZOOM, '--wait', '--objective', 'Take notes']);
  assert.equal(args.meeting, ZOOM);
  assert.equal(args.wait, true);
  assert.equal(args.objective, 'Take notes');
  assert.deepEqual(args._, ['call']);
});

test('call --meeting joins the meeting and waits for the result', async (t) => {
  const { root, daemon, common } = await withDaemon(t, { pollsBeforeDone: 1 });
  const result = await runColleague(['call', ...common, '--meeting', ZOOM, '--objective', 'Take notes', '--wait'], { cwd: root });
  assert.equal(result.code, 0, result.stderr);
  assert.match(result.stderr, /Joining the meeting \(call call-0000000000000001\)/);
  assert.match(result.stderr, /Waiting to be let into the meeting/);
  assert.match(result.stderr, /Result: Achieved\. Done\./);
  assert.equal(JSON.parse(result.stdout).status, 'completed');
  assert.ok(!result.stderr.includes(daemon.token));
  const brief = daemon.state.calls.get('call-0000000000000001').brief;
  assert.deepEqual(brief, { channel: 'meeting', to: ZOOM, onBehalfOf: 'Robin', objective: 'Take notes' });
});

test('--channel meeting, or a --to that is an invite URL, joins a meeting', async (t) => {
  const { root, daemon, common } = await withDaemon(t);
  const explicit = await runColleague(['call', ...common, '--channel', 'meeting', '--to', ZOOM, '--objective', 'Listen'], { cwd: root });
  assert.equal(explicit.code, 0, explicit.stderr);
  const inferred = await runColleague(['call', ...common, '--to', 'https://meet.google.com/aaa-bbbb-ccc', '--objective', 'Listen'], { cwd: root });
  assert.equal(inferred.code, 0, inferred.stderr);
  const phone = await runColleague(['call', ...common, '--to', '+14155550142', '--objective', 'Book a table'], { cwd: root });
  assert.equal(phone.code, 0, phone.stderr);
  assert.deepEqual([...daemon.state.calls.values()].map((call) => call.brief.channel), ['meeting', 'meeting', 'phone']);
  const bad = await runColleague(['call', ...common, '--channel', 'fax', '--to', ZOOM, '--objective', 'Listen'], { cwd: root });
  assert.equal(bad.code, 2);
  assert.match(bad.stderr, /--channel must be phone or meeting/);
  assert.equal(daemon.state.created, 3);
});

test('calls list, get, instruct, and end reach the daemon', async (t) => {
  const { root, daemon, common } = await withDaemon(t);
  const placed = await runColleague(['call', ...common, '--meeting', ZOOM, '--objective', 'Listen'], { cwd: root });
  const callId = JSON.parse(placed.stdout).id;
  const listed = await runColleague(['calls', 'list', ...common], { cwd: root });
  assert.deepEqual(JSON.parse(listed.stdout).calls.map((call) => call.id), [callId]);
  const got = await runColleague(['calls', 'get', '--call-id', callId, ...common], { cwd: root });
  assert.equal(JSON.parse(got.stdout).channel, 'meeting');
  const instructed = await runColleague(['calls', 'instruct', '--call-id', callId, '--text', 'Ask about the launch date', '--silent', ...common], { cwd: root });
  assert.equal(instructed.code, 0, instructed.stderr);
  assert.deepEqual(daemon.state.instructions, [{ callId, text: 'Ask about the launch date', silent: true }]);
  const ended = await runColleague(['calls', 'end', '--call-id', callId, ...common], { cwd: root });
  assert.equal(JSON.parse(ended.stdout).status, 'canceled');
});

test('output larger than a pipe buffer arrives whole', async (t) => {
  const { root, daemon, common } = await withDaemon(t);
  const line = 'The caller asked about the launch date and the budget. '.repeat(40);
  for (let i = 0; i < 60; i += 1) {
    const id = `call-${String(i).padStart(16, '0')}`;
    daemon.state.calls.set(id, { id, channel: 'phone', status: 'completed', transcript: [{ speaker: 'contact', text: line }] });
  }
  const listed = await runColleague(['calls', 'list', '--limit', '100', ...common], { cwd: root, pipeThroughCat: true });
  assert.equal(listed.code, 0, listed.stderr);
  assert.ok(listed.stdout.length > 128 * 1024, `only ${listed.stdout.length} bytes`);
  assert.equal(JSON.parse(listed.stdout).calls.length, 60);
});

test('startup failure is exit 3', async (t) => {
  const { root, common } = await withDaemon(t, {
    failCreate: { status: 503, code: 'supervisor_unavailable', message: 'supervisor unavailable' },
  });
  const result = await runColleague(['call', ...common, '--meeting', ZOOM, '--objective', 'Listen'], { cwd: root });
  assert.equal(result.code, 3, result.stderr);
});

test('removed commands are unknown', async () => {
  for (const command of ['join', 'status', 'cancel', 'handoff', 'approvals', 'runner', 'providers']) {
    const result = await runColleague([command]);
    assert.equal(result.code, 2, command);
    assert.match(result.stderr, /unknown command/);
  }
});
