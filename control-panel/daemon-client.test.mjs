import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';

import { createDaemonClient } from './daemon-client.mjs';

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
