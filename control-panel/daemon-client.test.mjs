import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';

import { createDaemonClient, DEFAULT_DAEMON_READY_MS } from './daemon-client.mjs';

function tempRoot() {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'daemon-client-'));
  fs.mkdirSync(path.join(root, '.colleague'), { recursive: true, mode: 0o700 });
  return root;
}

test('starts the daemon when the port is closed and waits for the auth token', async () => {
  const root = tempRoot();
  const tokenPath = path.join(root, '.colleague', 'daemon.auth');
  const spawns = [];
  const client = createDaemonClient({
    root,
    tokenPath,
    isPortOpen: async () => fs.existsSync(tokenPath),
    spawnDaemon() {
      spawns.push({ detached: true, script: 'start-runtime-daemon.sh' });
      fs.writeFileSync(tokenPath, 'daemon-secret-token\n', { mode: 0o600 });
      return { unref() { this.unrefed = true; }, on() {}, unrefed: false };
    },
    fetchImpl: async (url, init) => {
      assert.match(init.headers.Authorization, /^Bearer daemon-secret-token$/);
      assert.equal(init.headers.Authorization.includes('daemon-secret-token'), true);
      return new Response(JSON.stringify({ id: 'mtg-1', state: 'joining' }), { status: 201 });
    },
    timeoutMs: 200,
    pollMs: 5,
  });
  const created = await client.createMeeting({ meetingUrl: 'https://us05web.zoom.us/j/1' });
  assert.equal(created.id, 'mtg-1');
  assert.equal(spawns.length, 1);
  assert.equal(client.child.unrefed, true);
});

test('fails fast when the daemon process exits before writing an auth token', async () => {
  const root = tempRoot();
  const listeners = {};
  const client = createDaemonClient({
    root,
    tokenPath: path.join(root, '.colleague', 'daemon.auth'),
    isPortOpen: async () => false,
    spawnDaemon() {
      return {
        unref() {},
        on(event, fn) { listeners[event] = fn; },
      };
    },
    timeoutMs: 2000,
    pollMs: 5,
  });
  const pending = client.ensure();
  await Promise.resolve();
  listeners.exit?.(1);
  await assert.rejects(pending, /failed to start/);
});

test('fails fast when the daemon child has already exited', async () => {
  const root = tempRoot();
  const client = createDaemonClient({
    root,
    tokenPath: path.join(root, '.colleague', 'daemon.auth'),
    isPortOpen: async () => false,
    spawnDaemon() {
      return { exitCode: 1, unref() {}, on() {} };
    },
    timeoutMs: 2000,
    pollMs: 5,
  });
  await assert.rejects(client.ensure(), /failed to start/);
});

test('times out when the daemon never writes an auth token', async () => {
  const root = tempRoot();
  const started = Date.now();
  const client = createDaemonClient({
    root,
    tokenPath: path.join(root, '.colleague', 'daemon.auth'),
    isPortOpen: async () => false,
    spawnDaemon() { return { unref() {}, on() {} }; },
    timeoutMs: 40,
    pollMs: 5,
  });
  await assert.rejects(client.ensure(), /did not become ready/);
  assert.ok(Date.now() - started < 1000);
  assert.equal(DEFAULT_DAEMON_READY_MS, 60_000);
});

test('default spawn writes daemon.log when the launcher is missing', async () => {
  const root = tempRoot();
  const client = createDaemonClient({
    root,
    // The launcher comes from the code root; point it at an empty directory.
    codeRoot: root,
    managed: false,
    tokenPath: path.join(root, '.colleague', 'daemon.auth'),
    isPortOpen: async () => false,
    timeoutMs: 800,
    pollMs: 10,
  });
  await assert.rejects(client.ensure(), /failed to start|did not become ready/);
  const logPath = path.join(root, '.colleague', 'daemon.log');
  assert.equal(fs.existsSync(logPath), true);
  assert.equal(fs.statSync(path.join(root, '.colleague')).mode & 0o777, 0o700);
});

test('does not spawn when a healthy daemon is already listening', async () => {
  const root = tempRoot();
  const tokenPath = path.join(root, '.colleague', 'daemon.auth');
  fs.writeFileSync(tokenPath, 'existing-token\n', { mode: 0o600 });
  let spawns = 0;
  const client = createDaemonClient({
    root,
    tokenPath,
    isPortOpen: async () => true,
    spawnDaemon() { spawns += 1; return { unref() {}, on() {} }; },
    fetchImpl: async (_url, init) => {
      assert.equal(init.headers.Authorization, 'Bearer existing-token');
      return new Response(JSON.stringify({ id: 'mtg-1', state: 'live' }), { status: 200 });
    },
  });
  const meeting = await client.getMeeting('mtg-1', { startIfNeeded: false });
  assert.equal(meeting.state, 'live');
  assert.equal(spawns, 0);
});

test('re-reads a rotated auth token after 401 and never logs the secret', async () => {
  const root = tempRoot();
  const tokenPath = path.join(root, '.colleague', 'daemon.auth');
  fs.writeFileSync(tokenPath, 'stale-token\n', { mode: 0o600 });
  const seen = [];
  const client = createDaemonClient({
    root,
    tokenPath,
    isPortOpen: async () => true,
    spawnDaemon() { throw new Error('should not spawn'); },
    fetchImpl: async (_url, init) => {
      seen.push(init.headers.Authorization);
      if (init.headers.Authorization === 'Bearer stale-token') {
        fs.writeFileSync(tokenPath, 'fresh-token\n', { mode: 0o600 });
        return new Response(JSON.stringify({ error: { code: 'unauthorized', message: 'authorization required' } }), { status: 401 });
      }
      return new Response(JSON.stringify({ id: 'mtg-1', state: 'live' }), { status: 200 });
    },
  });
  const meeting = await client.getMeeting('mtg-1', { startIfNeeded: false });
  assert.equal(meeting.state, 'live');
  assert.deepEqual(seen, ['Bearer stale-token', 'Bearer fresh-token']);
});

test('status probes do not start a daemon when none is running', async () => {
  const root = tempRoot();
  let spawns = 0;
  const client = createDaemonClient({
    root,
    tokenPath: path.join(root, '.colleague', 'daemon.auth'),
    isPortOpen: async () => false,
    spawnDaemon() { spawns += 1; return { unref() {}, on() {} }; },
  });
  await assert.rejects(() => client.getMeeting('mtg-1', { startIfNeeded: false }), /not running/);
  assert.equal(spawns, 0);
});

test('managed (COLLEAGUE_MANAGED=1): never spawns, and says to restart the container', async () => {
  const root = tempRoot();
  let spawns = 0;
  const previous = process.env.COLLEAGUE_MANAGED;
  process.env.COLLEAGUE_MANAGED = '1';
  let client;
  try {
    client = createDaemonClient({
      root,
      isPortOpen: async () => false,
      spawnDaemon() { spawns += 1; return { unref() {}, on() {} }; },
      timeoutMs: 200,
      pollMs: 5,
    });
  } finally {
    if (previous === undefined) delete process.env.COLLEAGUE_MANAGED;
    else process.env.COLLEAGUE_MANAGED = previous;
  }
  assert.equal(client.managed, true);
  assert.equal(client.tokenPath, path.join(root, '.colleague', 'daemon.auth'));
  await assert.rejects(client.createMeeting({ meetingUrl: 'https://us05web.zoom.us/j/1' }), (error) => (
    /docker restart smitline/.test(error.message) && error.code === 'daemon_unavailable' && error.status === 503
  ));
  await assert.rejects(client.getMeeting('mtg-1'), /docker restart smitline/);
  assert.equal(spawns, 0);
  assert.equal(fs.existsSync(path.join(root, '.colleague', 'daemon.log')), false);

  // A running daemon is used as usual.
  fs.writeFileSync(path.join(root, '.colleague', 'daemon.auth'), 'managed-token\n', { mode: 0o600 });
  const running = createDaemonClient({
    root,
    managed: true,
    isPortOpen: async () => true,
    spawnDaemon() { spawns += 1; },
    fetchImpl: async (_url, init) => {
      assert.equal(init.headers.Authorization, 'Bearer managed-token');
      return new Response(JSON.stringify({ id: 'mtg-2', state: 'joining' }), { status: 201 });
    },
  });
  assert.equal((await running.createMeeting({ meetingUrl: 'https://us05web.zoom.us/j/1' })).id, 'mtg-2');
  assert.equal(spawns, 0);
});
