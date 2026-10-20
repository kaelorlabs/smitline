import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import http from 'node:http';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';
import {
  Smitline,
  ValidationError,
  StartupError,
  RuntimeError,
  createLoopbackTransport,
  createRemoteTransport,
  isManaged,
  MANAGED_NOT_RUNNING,
} from '../src/index.mjs';
import { startFakeDaemon } from './fake-daemon.mjs';

const ZOOM = 'https://zoom.us/j/123456789';

async function withDaemon(t, options, fn) {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'smitline-sdk-'));
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
  const colleague = new Smitline({ transport });
  return fn({ root, daemon, transport, colleague });
}

test('the SDK exposes calls only, for calls and meetings only', () => {
  const methods = Object.getOwnPropertyNames(Smitline.prototype).filter((name) => name !== 'constructor').sort();
  assert.deepEqual(methods, [
    'checkCall', 'downloadRecording', 'endCall', 'forgetContact', 'getCall', 'getContact', 'getDoNotCall', 'getProfile',
    'instructCall', 'listCalls', 'listContacts', 'listVoices', 'replyToRequest', 'saveCallNote', 'startCall',
    'transferCall', 'updateContact', 'updateDoNotCall', 'updateProfile', 'waitForCall',
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
    await colleague.waitForCall(call.id, 5, { since: '' });
    await colleague.waitForCall(call.id, 5, { since: '12' });
    await colleague.waitForCall(call.id, 5, { since: '12', only: 'requests' });
    const follows = daemon.state.requests.filter((item) => item.path.endsWith('/wait')).slice(-3);
    assert.deepEqual(follows.map((item) => item.search),
      ['?timeout=5&since=', '?timeout=5&since=12', '?timeout=5&since=12&only=requests']);
    const answered = await colleague.replyToRequest(call.id, 'r1', { type: 'done', text: 'Added it.', details: 'PR 81' });
    assert.equal(answered.delivered, true);
    assert.deepEqual(daemon.state.replies, [{ callId: call.id, requestId: 'r1', type: 'done', text: 'Added it.', details: 'PR 81' }]);
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

test('a remote daemon gets the fixed token and extra headers, and is never started', async (t) => {
  const daemon = await startFakeDaemon({ token: 'cai_remote' });
  t.after(() => daemon.close());
  const colleague = new Smitline({
    baseUrl: `http://127.0.0.1:${daemon.port}/`,
    token: 'cai_remote',
    headers: { 'X-Smitline-Owner': 'acct_3f9c2a7b1d', Authorization: 'Bearer ignored' },
    autostart: false,
    root: '/nonexistent',
  });
  const call = await colleague.startCall({ channel: 'phone', to: '+14155550142', objective: 'x' });
  assert.equal((await colleague.getCall(call.id)).id, call.id);
  for (const request of daemon.state.requests) {
    assert.equal(request.headers.authorization, 'Bearer cai_remote');
    assert.equal(request.headers['x-smitline-owner'], 'acct_3f9c2a7b1d');
  }
  // A wrong token is an error at once: no daemon.auth to reread, no retry.
  daemon.rotateToken('other');
  const before = daemon.state.requests.length;
  await assert.rejects(colleague.getCall(call.id), (error) => error.status === 401);
  assert.equal(daemon.state.requests.length, before + 1);
});

test('a remote daemon needs a URL and a token', () => {
  assert.throws(() => createRemoteTransport({ baseUrl: 'http://127.0.0.1:1' }), ValidationError);
  assert.throws(() => new Smitline({ baseUrl: 'ftp://example.com', token: 't' }), ValidationError);
  let spawned = false;
  const transport = createRemoteTransport({ baseUrl: 'https://smitline.example.com', token: 't',
    spawnDaemon: () => { spawned = true; } });
  assert.equal(transport.origin, 'https://smitline.example.com');
  assert.equal(spawned, false);
});

/** A hosted daemon stand-in: answers each route with a canned status and body, and records requests. */
async function startScriptedDaemon(t, routes) {
  const requests = [];
  const server = http.createServer((request, response) => {
    const chunks = [];
    request.on('data', (chunk) => chunks.push(chunk));
    request.on('end', () => {
      const url = new URL(request.url, 'http://127.0.0.1');
      requests.push({ method: request.method, path: url.pathname, search: url.search, headers: request.headers,
        body: Buffer.concat(chunks).toString('utf8') });
      const answer = routes[`${request.method} ${url.pathname}`] || { status: 404, body: { error: { code: 'not_found', message: 'no such route' } } };
      const raw = Buffer.isBuffer(answer.body) ? answer.body : Buffer.from(JSON.stringify(answer.body));
      response.writeHead(answer.status, { 'Content-Type': answer.contentType || 'application/json' });
      response.end(raw);
    });
  });
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  t.after(() => new Promise((resolve) => server.close(resolve)));
  return { requests, url: `http://127.0.0.1:${server.address().port}` };
}

test('a remote daemon keeps the status, code and details of every refusal', async (t) => {
  const refusals = [
    [402, { code: 'insufficient_credit', message: 'Top up to call.', balance: '4.20', affordableMinutes: 21, topUpUrl: 'https://example.com/wallet' }, RuntimeError],
    [429, { code: 'too_many_active_calls', message: 'One call at a time.', limit: 1 }, RuntimeError],
    [403, { code: 'verification_required', message: 'Verify your phone.', verifyUrl: 'https://example.com/verify' }, RuntimeError],
    [401, { code: 'owner_required', message: 'This deployment serves accounts: authenticate as one.' }, RuntimeError],
    [400, { code: 'invalid_owner', message: 'An owner id is lowercase letters, digits, _ and -, at most 64 characters.' }, ValidationError],
    [503, { code: 'call_start_failed', message: 'The call could not be started.' }, StartupError],
  ];
  for (const [status, error, kind] of refusals) {
    const daemon = await startScriptedDaemon(t, { 'POST /v1/calls': { status, body: { error } } });
    const colleague = new Smitline({ baseUrl: daemon.url, token: 'cai_gateway', headers: { 'X-Smitline-Owner': 'acct_a' } });
    const { code, message, ...details } = error;
    await assert.rejects(colleague.startCall({ channel: 'phone', to: '+14155550142', objective: 'x' }), (thrown) => {
      assert.ok(thrown instanceof kind, `${status} is a ${kind.name}`);
      assert.equal(thrown.status, status);
      assert.equal(thrown.code, code);
      assert.equal(thrown.message, message);
      if (Object.keys(details).length) assert.deepEqual(thrown.details, details);
      else assert.equal(thrown.details, undefined);
      return true;
    });
    // A refusal is final: the remote transport never retries it.
    assert.equal(daemon.requests.length, 1);
  }
});

test('a remote daemon long-polls and downloads with its own token and headers only', async (t) => {
  const done = { id: 'call-0000000000000001', status: 'completed', result: { outcome: 'achieved' } };
  const daemon = await startScriptedDaemon(t, {
    'GET /prefix/v1/calls/call-0000000000000001/wait': { status: 200, body: done },
    'GET /prefix/v1/calls/call-0000000000000001/recording': { status: 200, body: Buffer.from('RIFF....WAVE'), contentType: 'audio/wav' },
    'GET /prefix/v1/calls': { status: 200, body: { calls: [done], spend: { days: [] } } },
  });
  const colleague = new Smitline({
    baseUrl: `${daemon.url}/prefix/`,
    token: 'cai_gateway',
    headers: { 'x-smitline-owner': 'acct_a', authorization: 'Bearer stolen', AUTHORIZATION: 'Bearer stolen' },
  });
  assert.equal((await colleague.waitForCall(done.id, 9999)).status, 'completed');
  const audio = await colleague.downloadRecording(done.id, { format: 'wav' });
  assert.equal(audio.contentType, 'audio/wav');
  assert.equal(Buffer.from(audio.data).toString(), 'RIFF....WAVE');
  assert.deepEqual(await colleague.listCalls(5, { channel: 'phone' }), [done]);
  assert.deepEqual(daemon.requests.map((request) => request.path + request.search), [
    '/prefix/v1/calls/call-0000000000000001/wait?timeout=280',
    '/prefix/v1/calls/call-0000000000000001/recording?format=wav',
    '/prefix/v1/calls?limit=5&channel=phone',
  ]);
  for (const request of daemon.requests) {
    assert.equal(request.headers.authorization, 'Bearer cai_gateway');
    assert.equal(request.headers['x-smitline-owner'], 'acct_a');
  }
});

test('a remote daemon never reads daemon.auth, starts a daemon, or probes a local port', async () => {
  const touched = [];
  const options = {
    baseUrl: 'http://127.0.0.1:1',
    token: 'cai_gateway',
    managed: false,
    fs: { readFile: async (file) => { touched.push(['readFile', file]); return 'local-token'; } },
    readAuth: async () => { touched.push(['readAuth']); return 'local-token'; },
    spawnDaemon: () => { touched.push(['spawn']); return { unref() {} }; },
    isPortOpen: async () => { touched.push(['isPortOpen']); return false; },
    fetchImpl: async () => { throw new Error('connect ECONNREFUSED 127.0.0.1:1 with Bearer cai_gateway'); },
  };
  for (const colleague of [new Smitline(options), new Smitline({ transport: createRemoteTransport(options) })]) {
    await assert.rejects(colleague.listCalls(), (error) => {
      assert.ok(error instanceof StartupError);
      assert.equal(error.code, 'daemon_unavailable');
      assert.ok(!error.message.includes('cai_gateway'), 'the token is not echoed');
      return true;
    });
  }
  assert.deepEqual(touched, []);
});

test('startup errors are StartupError', async (t) => {
  await withDaemon(t, { failCreate: { status: 503, code: 'supervisor_unavailable', message: 'supervisor unavailable' } }, async ({ colleague }) => {
    await assert.rejects(() => colleague.startCall({ channel: 'phone', to: '+14155550142', objective: 'x' }), StartupError);
  });
});

test('daemon autostart is serialized and uses the host-only token file', async (t) => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'smitline-autostart-'));
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
  const colleague = new Smitline({ transport });
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

test('managed (SMITLINE_MANAGED=1): the SDK never starts a daemon and says to restart the container', async (t) => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'smitline-managed-'));
  t.after(() => fs.rm(root, { recursive: true, force: true }));
  assert.equal(isManaged({ SMITLINE_MANAGED: '1' }), true);
  assert.equal(isManaged({ SMITLINE_MANAGED: '0' }), false);
  assert.equal(isManaged({}), false);
  let spawns = 0;
  const previous = process.env.SMITLINE_MANAGED;
  process.env.SMITLINE_MANAGED = '1';
  let viaEnv;
  try {
    viaEnv = createLoopbackTransport({ root, port: 1, isPortOpen: async () => false });
  } finally {
    if (previous === undefined) delete process.env.SMITLINE_MANAGED;
    else process.env.SMITLINE_MANAGED = previous;
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

test('the daemon launcher comes from codeRoot while SMITLINE_ROOT points at the data', async (t) => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'smitline-data-'));
  t.after(() => fs.rm(root, { recursive: true, force: true }));
  const daemon = await startFakeDaemon({ root });
  t.after(() => daemon.close());
  let open = false;
  let spawned;
  const transport = createLoopbackTransport({
    root,
    codeRoot: '/opt/smitline-code',
    managed: false,
    port: daemon.port,
    isPortOpen: async () => open,
    spawnDaemon: (options) => { spawned = options; open = true; return { unref() {} }; },
    startupTimeoutMs: 2000,
  });
  await transport.listCalls();
  assert.equal(spawned.root, root);
  assert.equal(spawned.codeRoot, '/opt/smitline-code');
});
