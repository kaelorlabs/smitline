import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';
import {
  Colleague,
  ValidationError,
  StartupError,
  createLoopbackTransport,
  isManaged,
  MANAGED_NOT_RUNNING,
} from '../src/index.mjs';
import { startFakeDaemon } from './fake-daemon.mjs';

const ZOOM = 'https://zoom.us/j/123456789';

async function withDaemon(t, options, fn) {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'colleague-sdk-'));
  const daemon = await startFakeDaemon({ root, ...options });
  t.after(async () => {
    await daemon.close();
    await fs.rm(root, { recursive: true, force: true });
  });
  const transport = createLoopbackTransport({
    root,
    port: daemon.port,
    autostart: false,
    spawnDaemon: null,
  });
  const colleague = new Colleague({ transport });
  return fn({ root, daemon, transport, colleague });
}

test('the SDK exposes calls only, for calls and meetings only', () => {
  const methods = Object.getOwnPropertyNames(Colleague.prototype).filter((name) => name !== 'constructor').sort();
  assert.deepEqual(methods, [
    'checkCall', 'endCall', 'getCall', 'getDoNotCall', 'getProfile', 'instructCall', 'listCalls',
    'listVoices', 'startCall', 'transferCall', 'updateDoNotCall', 'updateProfile', 'waitForCall',
  ]);
});

test('a meeting is joined with startCall and followed to its result', async (t) => {
  await withDaemon(t, { pollsBeforeDone: 1 }, async ({ colleague, daemon }) => {
    const brief = { channel: 'meeting', to: ZOOM, objective: 'Take notes on the roadmap review' };
    const call = await colleague.startCall(brief);
    assert.equal(call.channel, 'meeting');
    assert.deepEqual(daemon.state.calls.get(call.id).brief, brief);
    const waiting = await colleague.waitForCall(call.id, 5);
    assert.equal(waiting.status, 'waiting');
    const done = await colleague.waitForCall(call.id, 5);
    assert.equal(done.status, 'completed');
    assert.equal(done.result.outcome, 'achieved');
    const wait = daemon.state.requests.find((item) => item.path.endsWith('/wait'));
    assert.equal(wait.search, '?timeout=5');
  });
});

test('phone calls can be listed, steered, ended, and transferred', async (t) => {
  await withDaemon(t, {}, async ({ colleague, daemon }) => {
    const call = await colleague.startCall({ channel: 'phone', to: '+14155550142', objective: 'Book a table' });
    assert.equal((await colleague.getCall(call.id)).id, call.id);
    assert.deepEqual((await colleague.listCalls(5)).map((item) => item.id), [call.id]);
    assert.deepEqual(await colleague.instructCall(call.id, 'Ask about parking'), { delivered: true });
    assert.deepEqual(daemon.state.instructions, [{ callId: call.id, text: 'Ask about parking' }]);
    assert.deepEqual(await colleague.transferCall(call.id), { transferred: true });
    assert.equal((await colleague.endCall(call.id)).status, 'canceled');
    assert.deepEqual((await colleague.listVoices()).voices, ['marin', 'cinder']);
    const check = await colleague.checkCall({ channel: 'phone', to: '+14155550142' });
    assert.equal(check.ok, false);
  });
});

test('an incomplete brief is a ValidationError with the questions to ask', async (t) => {
  await withDaemon(t, {}, async ({ colleague }) => {
    await assert.rejects(() => colleague.startCall({ channel: 'phone', to: '+14155550142' }), (error) => {
      assert.ok(error instanceof ValidationError);
      assert.equal(error.details.missing[0].question, 'What should the call achieve?');
      return true;
    });
  });
});

test('stale token is rotated from host-only auth file without exposing it', async (t) => {
  await withDaemon(t, {}, async ({ colleague, daemon }) => {
    const call = await colleague.startCall({ channel: 'phone', to: '+14155550142', objective: 'x' });
    daemon.rotateToken('brand-new-token');
    await daemon.writeAuth('brand-new-token');
    const fetched = await colleague.getCall(call.id);
    assert.equal(fetched.id, call.id);
    assert.equal(daemon.state.lastAuthorization, 'Bearer brand-new-token');
    const dumped = JSON.stringify(fetched);
    assert.ok(!dumped.includes('brand-new-token'));
    assert.ok(!dumped.includes('test-daemon-token'));
  });
});

test('startup errors are StartupError', async (t) => {
  await withDaemon(t, { failCreate: { status: 503, code: 'supervisor_unavailable', message: 'supervisor unavailable' } }, async ({ colleague }) => {
    await assert.rejects(() => colleague.startCall({ channel: 'phone', to: '+14155550142', objective: 'x' }), StartupError);
  });
});

test('daemon autostart is serialized and uses the host-only token file', async (t) => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'colleague-autostart-'));
  t.after(() => fs.rm(root, { recursive: true, force: true }));
  const daemon = await startFakeDaemon({ root });
  t.after(() => daemon.close());
  let spawns = 0;
  let open = false;
  const transport = createLoopbackTransport({
    root,
    port: daemon.port,
    isPortOpen: async () => open,
    spawnDaemon: () => {
      spawns += 1;
      open = true;
      return { unref() {} };
    },
    startupTimeoutMs: 2000,
  });
  const call = await transport.startCall({ channel: 'meeting', to: ZOOM, objective: 'Listen in' });
  assert.ok(call.id);
  assert.equal(spawns, 1);
  const concurrent = await Promise.all([transport.getCall(call.id), transport.getCall(call.id)]);
  assert.equal(spawns, 1);
  assert.equal(concurrent[0].id, call.id);
});

test('profile reads and updates, and silent notes, use the daemon routes', async () => {
  const seen = [];
  const transport = createLoopbackTransport({
    root: os.tmpdir(),
    autostart: false,
    spawnDaemon: null,
    isPortOpen: async () => true,
    readAuth: async () => 'tok',
    fetchImpl: async (url, init) => {
      seen.push([init.method, new URL(url).pathname, init.body ? JSON.parse(init.body) : null]);
      return new Response('{}', { status: 200, headers: { 'content-type': 'application/json' } });
    },
  });
  const colleague = new Colleague({ transport });
  await colleague.getProfile();
  await colleague.updateProfile({ about: 'Robin builds Smitline.' });
  await colleague.getDoNotCall();
  await colleague.updateDoNotCall({ remove: ['+14155550142'] });
  await colleague.instructCall('call-0123456789abcdef', 'He tried it yesterday', { silent: true });
  await colleague.instructCall('call-0123456789abcdef', 'Ask about parking');
  assert.deepEqual(seen, [
    ['GET', '/v1/profile', null],
    ['PATCH', '/v1/profile', { about: 'Robin builds Smitline.' }],
    ['GET', '/v1/do-not-call', null],
    ['PATCH', '/v1/do-not-call', { remove: ['+14155550142'] }],
    ['POST', '/v1/calls/call-0123456789abcdef/instructions', { text: 'He tried it yesterday', silent: true }],
    ['POST', '/v1/calls/call-0123456789abcdef/instructions', { text: 'Ask about parking' }],
  ]);
});

test('managed (COLLEAGUE_MANAGED=1): the SDK never starts a daemon and says to restart the container', async (t) => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'colleague-managed-'));
  t.after(() => fs.rm(root, { recursive: true, force: true }));
  assert.equal(isManaged({ COLLEAGUE_MANAGED: '1' }), true);
  assert.equal(isManaged({ COLLEAGUE_MANAGED: '0' }), false);
  assert.equal(isManaged({}), false);
  let spawns = 0;
  const previous = process.env.COLLEAGUE_MANAGED;
  process.env.COLLEAGUE_MANAGED = '1';
  let viaEnv;
  try {
    viaEnv = createLoopbackTransport({ root, port: 1, isPortOpen: async () => false });
  } finally {
    if (previous === undefined) delete process.env.COLLEAGUE_MANAGED;
    else process.env.COLLEAGUE_MANAGED = previous;
  }
  await assert.rejects(() => viaEnv.listCalls(), (error) => (
    error instanceof StartupError && error.code === 'daemon_unavailable' && error.message === MANAGED_NOT_RUNNING
  ));
  const explicit = createLoopbackTransport({
    root, port: 1, managed: true, isPortOpen: async () => false,
    spawnDaemon: () => { spawns += 1; return { unref() {} }; },
  });
  await assert.rejects(() => explicit.startCall({ channel: 'meeting', to: ZOOM, objective: 'x' }), /docker restart smitline/);
  assert.equal(spawns, 0);
});

test('the daemon launcher comes from codeRoot while COLLEAGUE_ROOT points at the data', async (t) => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'colleague-data-'));
  t.after(() => fs.rm(root, { recursive: true, force: true }));
  const daemon = await startFakeDaemon({ root });
  t.after(() => daemon.close());
  let open = false;
  let spawned;
  const transport = createLoopbackTransport({
    root,
    codeRoot: '/opt/colleague-code',
    managed: false,
    port: daemon.port,
    isPortOpen: async () => open,
    spawnDaemon: (options) => { spawned = options; open = true; return { unref() {} }; },
    startupTimeoutMs: 2000,
  });
  await transport.listCalls();
  assert.equal(spawned.root, root);
  assert.equal(spawned.codeRoot, '/opt/colleague-code');
});
