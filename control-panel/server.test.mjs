import assert from 'node:assert/strict';
import { once } from 'node:events';
import fs from 'node:fs';
import http from 'node:http';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';

import { createServer } from './server.mjs';

async function withServer(run) {
  const server = createServer();
  server.listen(0, '127.0.0.1');
  await once(server, 'listening');
  try {
    const { port } = server.address();
    await run(`http://127.0.0.1:${port}`);
  } finally {
    server.close();
    await once(server, 'close');
  }
}

test('serves the console with local security headers', async () => {
  await withServer(async base => {
    const response = await fetch(`${base}/`);
    assert.equal(response.status, 200);
    assert.match(response.headers.get('content-security-policy'), /default-src 'self'/);
    const html = await response.text();
    assert.match(html, /Meeting details/);
    assert.match(html, /<span class="req">Required<\/span>/);
    assert.match(html, /aria-required="true"/);
    assert.match(html, /id="meetingUrl-error"/);
    assert.match(html, /id="participantName-error"/);
    assert.match(html, /id="start-button" type="button"/);
  });
});

test('serves app.js and its visual-preview module', async () => {
  await withServer(async base => {
    const app = await fetch(`${base}/app.js`);
    const preview = await fetch(`${base}/visual-preview.mjs`);
    const validation = await fetch(`${base}/client-validation.mjs`);
    assert.equal(app.status, 200);
    assert.equal(preview.status, 200);
    assert.equal(validation.status, 200);
    assert.match(app.headers.get('content-type'), /javascript/);
    assert.match(preview.headers.get('content-type'), /javascript/);
    const appText = await app.text();
    assert.match(appText, /visual-preview\.mjs/);
    assert.match(appText, /client-validation\.mjs/);
    assert.match(appText, /createStartLock/);
    assert.match(appText, /if \(!startLock\.begin\(\)\) return/);
    assert.match(await preview.text(), /drawPresencePreview/);
    assert.match(await validation.text(), /clientMeetingErrors/);
  });
});

test('bootstrap excludes API credentials and mutations require a session token', async () => {
  await withServer(async base => {
    const bootstrap = await fetch(`${base}/api/bootstrap`);
    const body = await bootstrap.json();
    assert.equal(bootstrap.status, 200);
    assert.equal(typeof body.token, 'string');
    assert.equal(Array.isArray(body.context.sources), true);
    assert.equal('passcode' in body.settings, false);
    assert.equal('OPENAI_API_KEY' in body.settings, false);

    const mutation = await fetch(`${base}/api/start`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: '{}',
    });
    assert.equal(mutation.status, 403);
  });
});

test('adds private context without returning its extracted text in bootstrap', async () => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'colleague-server-context-'));
  const contextIndex = path.join(directory, 'index.json');
  const server = createServer({
    root: directory,
    contextIndex,
    daemon: {
      async getMeeting() {
        const error = new Error('Runtime daemon is not running.');
        error.code = 'daemon_offline';
        error.status = 503;
        throw error;
      },
    },
  });
  server.listen(0, '127.0.0.1');
  await once(server, 'listening');
  try {
    const base = `http://127.0.0.1:${server.address().port}`;
    const bootstrap = await (await fetch(`${base}/api/bootstrap`)).json();
    const response = await fetch(`${base}/api/context/add`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Origin': 'http://127.0.0.1:8095',
        'X-Colleague-Token': bootstrap.token,
      },
      body: JSON.stringify({ text: 'Private launch target is October 4.', files: [] }),
    });
    assert.equal(response.status, 200);
    const result = await response.json();
    assert.equal(result.sources.length, 1);
    assert.equal('text' in result.sources[0], false);
    const refreshed = await (await fetch(`${base}/api/bootstrap`)).json();
    assert.equal(refreshed.context.sources[0].name, 'Pasted meeting context');
    assert.equal(JSON.stringify(refreshed).includes('Private launch target'), false);
  } finally {
    server.close();
    await once(server, 'close');
  }
});

test('meeting controls reject unauthenticated requests', async () => {
  const server = createServer();
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  try {
    const base = `http://127.0.0.1:${server.address().port}`;
    for (const endpoint of ['/api/platforms/teams/connect', '/api/platforms/teams/disconnect', '/api/platforms/google/connect', '/api/platforms/google/disconnect']) {
      const result = await fetch(base + endpoint, { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({allowed:true}) });
      assert.equal(result.status, 403);
    }
  } finally { await new Promise(resolve => server.close(resolve)); }
});

const MEETING_FIELDS = new Set(['meetingUrl', 'context', 'camera', 'onBehalfOf', 'voice']);

function createFakeDaemon() {
  const meetings = new Map();
  const calls = [];
  let unavailable = false;
  return {
    calls,
    meetings,
    setUnavailable(value = true) { unavailable = value; },
    async ensure() {
      if (unavailable) {
        const error = new Error('Runtime daemon is unavailable. Start it or retry from the console.');
        error.status = 503;
        error.code = 'daemon_unavailable';
        throw error;
      }
    },
    async createMeeting(payload) {
      calls.push({ method: 'POST', path: '/v1/meetings', body: payload });
      await this.ensure();
      // Mirrors the daemon contract: coding-agent fields are refused.
      const unknown = Object.keys(payload).filter(key => !MEETING_FIELDS.has(key));
      if (unknown.length) {
        const error = new Error(`unknown meeting fields: ${unknown.join(', ')}`);
        error.status = 400;
        error.code = 'invalid_request';
        throw error;
      }
      if ([...meetings.values()].some(meeting => meeting.state !== 'ended')) {
        const error = new Error('meeting agent is already running');
        error.status = 409;
        error.code = 'capacity_exceeded';
        throw error;
      }
      const session = {
        id: `mtg-portal${String(meetings.size + 1).padStart(8, '0')}`,
        state: 'joining',
        meetingUrl: payload.meetingUrl,
        context: payload.context,
        onBehalfOf: payload.onBehalfOf,
      };
      meetings.set(session.id, session);
      return session;
    },
    async getMeeting(id) {
      calls.push({ method: 'GET', path: `/v1/meetings/${id}` });
      if (unavailable) {
        const error = new Error('Runtime daemon is unavailable. Start it or retry from the console.');
        error.status = 503;
        error.code = 'daemon_unavailable';
        throw error;
      }
      const session = meetings.get(id);
      if (!session) {
        const error = new Error('meeting not found');
        error.status = 404;
        error.code = 'not_found';
        throw error;
      }
      return session;
    },
    async cancelMeeting(id) {
      calls.push({ method: 'POST', path: `/v1/meetings/${id}/cancel`, body: {} });
      await this.ensure();
      const session = meetings.get(id);
      if (!session) {
        const error = new Error('meeting not found');
        error.status = 404;
        error.code = 'not_found';
        throw error;
      }
      session.state = 'ended';
      return session;
    },
    async updateContext(id, payload) {
      calls.push({ method: 'POST', path: `/v1/meetings/${id}/context`, body: payload });
      const session = meetings.get(id);
      if (!session) {
        const error = new Error('meeting not found');
        error.status = 404;
        error.code = 'not_found';
        throw error;
      }
      if (session.state === 'ended') {
        const error = new Error('cannot update context after the meeting has ended');
        error.status = 409;
        error.code = 'conflict';
        throw error;
      }
      session.context = payload;
      return session;
    },
  };
}

async function withPanel(run, extra = {}) {
  const { env = 'OPENAI_API_KEY=sk-test\n', ...options } = extra;
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'colleague-panel-'));
  fs.writeFileSync(path.join(root, '.env'), env, { mode: 0o600 });
  const runtimeRoot = path.join(root, 'meeting-runtime');
  const daemon = extra.daemon || createFakeDaemon();
  const accountSpawns = [];
  const server = createServer({
    root,
    runtimeRoot,
    contextIndex: path.join(runtimeRoot, 'context', 'index.json'),
    meetingEnv: path.join(root, '.env.meeting'),
    daemon,
    spawnAccount(env) {
      accountSpawns.push(env);
      return { stdout: { on() {} }, stderr: { on() {} }, on() {}, exitCode: null, signalCode: null };
    },
    runCommand: async () => ({ code: 0, stdout: 'ok', stderr: '' }),
    ...options,
    daemon,
    root,
    runtimeRoot,
    meetingEnv: path.join(root, '.env.meeting'),
  });
  server.listen(0, '127.0.0.1');
  await once(server, 'listening');
  const base = `http://127.0.0.1:${server.address().port}`;
  try {
    await run({
      base, root, runtimeRoot, daemon, accountSpawns,
      async bootstrap() {
        return (await fetch(`${base}/api/bootstrap`)).json();
      },
      headers(csrf) {
        return {
          'Content-Type': 'application/json',
          Origin: 'http://127.0.0.1:8095',
          'X-Colleague-Token': csrf,
        };
      },
      settings: {
        meetingUrl: 'https://us05web.zoom.us/j/123456789?pwd=opaque',
        participantName: 'Colleague AI',
        meetingInstructions: 'Stay brief.',
        camera: { enabled: true, defaultOn: false },
      },
    });
  } finally {
    server.close();
    await once(server, 'close');
  }
}

test('start uses the daemon, keeps .env.meeting operator-managed, and hides daemon secrets', async () => {
  await withPanel(async panel => {
    const bootstrap = await panel.bootstrap();
    const started = await fetch(`${panel.base}/api/start`, {
      method: 'POST',
      headers: panel.headers(bootstrap.token),
      body: JSON.stringify(panel.settings),
    });
    assert.equal(started.status, 202);
    const body = await started.json();
    assert.equal(body.started, true);
    const created = panel.daemon.calls.find((item) => item.path === '/v1/meetings');
    assert.ok(created);
    assert.deepEqual(Object.keys(created.body).sort(), ['camera', 'context', 'meetingUrl']);
    assert.equal(created.body.meetingUrl, panel.settings.meetingUrl);
    assert.equal(created.body.context.objective, 'Stay brief.');
    assert.deepEqual(created.body.camera, { enabled: true, defaultOn: false });
    assert.equal(fs.existsSync(path.join(panel.root, '.env.meeting')), false);
    assert.equal(fs.existsSync(path.join(panel.root, '.colleague', 'portal-active.json')), true);
    assert.equal(fs.existsSync(path.join(panel.runtimeRoot, 'run', 'portal-active.json')), false);
    assert.equal(fs.existsSync(path.join(panel.runtimeRoot, 'run', 'daemon.auth')), false);
    assert.equal(fs.existsSync(path.join(panel.runtimeRoot, 'run', 'daemon-data')), false);
    const status = await (await fetch(`${panel.base}/api/status`)).json();
    assert.equal(status.running, true);
    assert.equal(status.phase, 'starting');
    const encoded = JSON.stringify({ bootstrap, status, body });
    assert.equal(encoded.includes('Bearer'), false);
    assert.equal('OPENAI_API_KEY' in (bootstrap.settings || {}), false);
  });
});

test('start sends the owner name from .env as onBehalfOf', async () => {
  await withPanel(async panel => {
    const bootstrap = await panel.bootstrap();
    const started = await fetch(`${panel.base}/api/start`, {
      method: 'POST',
      headers: panel.headers(bootstrap.token),
      // Stale coding-agent fields from an old page are not forwarded.
      body: JSON.stringify({ ...panel.settings, model: 'gpt-5.5', workspace: '/tmp', tools: { codex: true }, screenShare: { enabled: true } }),
    });
    assert.equal(started.status, 202);
    const created = panel.daemon.calls.find((item) => item.path === '/v1/meetings');
    assert.equal(created.body.onBehalfOf, 'Sam Rivera');
    assert.deepEqual(Object.keys(created.body).sort(), ['camera', 'context', 'meetingUrl', 'onBehalfOf']);
    assert.equal(JSON.stringify(await (await fetch(`${panel.base}/api/status`)).json()).includes('sk-test'), false);
  }, { env: 'OPENAI_API_KEY=sk-test\nCOLLEAGUE_OWNER_NAME="Sam Rivera"\n' });
});

test('the meetings console has no coding-agent controls or routes', async () => {
  await withPanel(async panel => {
    const html = await (await fetch(`${panel.base}/`)).text();
    for (const text of ['Codex', 'Cursor', 'Claude', 'workspace"', 'runner', 'approval', 'Screen-share', 'Retry']) {
      assert.equal(html.includes(text), false, text);
    }
    const bootstrap = await panel.bootstrap();
    assert.equal('models' in bootstrap, false);
    assert.deepEqual(Object.keys(bootstrap.settings).sort(), ['hasPasscode', 'meetingInstructions', 'meetingUrl', 'participantName', 'platform']);
    for (const key of ['lease', 'pendingApprovals', 'workspaceArtifacts', 'gitOperations', 'screenShare', 'providers', 'runner', 'continuity', 'provider']) {
      assert.equal(key in bootstrap.status, false, key);
    }
    const preflight = await fetch(`${panel.base}/api/preflight`, {
      method: 'POST',
      headers: panel.headers(bootstrap.token),
      body: JSON.stringify({ ...panel.settings, tools: { codex: true, cursor: true, claudeCode: true, webSearch: true } }),
    });
    assert.deepEqual(await preflight.json(), { ready: true, errors: {} });
    for (const [method, route] of [
      ['GET', '/api/runner'], ['POST', '/api/runner/pair'], ['POST', '/api/runner/pair/complete'], ['POST', '/api/runner/unpair'],
      ['GET', '/api/meetings/mtg-1/artifacts'], ['GET', '/api/meetings/mtg-1/artifacts/art-1/content'],
      ['POST', '/api/sessions/mtg-1/retry'], ['POST', '/api/meetings/mtg-1/approvals/appr-1/decision'],
      ['GET', '/api/meetings/mtg-1/screen-share'], ['POST', '/api/meetings/mtg-1/screen-share/pause'],
      ['POST', '/api/meetings/mtg-1/screen-share/resume'],
    ]) {
      const response = await fetch(`${panel.base}${route}`, {
        method,
        headers: panel.headers(bootstrap.token),
        ...(method === 'POST' ? { body: '{}' } : {}),
      });
      assert.equal(response.status, 404, route);
    }
  }, {
    runCommand: async (command) => {
      assert.equal(command, 'docker');
      return { code: 0, stdout: 'ok', stderr: '' };
    },
  });
});

test('duplicate start is rejected and stop/cancel is idempotent', async () => {
  await withPanel(async panel => {
    const bootstrap = await panel.bootstrap();
    const start = () => fetch(`${panel.base}/api/start`, {
      method: 'POST',
      headers: panel.headers(bootstrap.token),
      body: JSON.stringify(panel.settings),
    });
    assert.equal((await start()).status, 202);
    const duplicate = await start();
    assert.equal(duplicate.status, 409);
    assert.equal(panel.daemon.calls.filter(call => call.path === '/v1/meetings').length, 1);
    const stop = () => fetch(`${panel.base}/api/stop`, {
      method: 'POST',
      headers: panel.headers(bootstrap.token),
      body: '{}',
    });
    assert.equal((await stop()).status, 200);
    assert.equal((await stop()).status, 200);
    const status = await (await fetch(`${panel.base}/api/status`)).json();
    assert.equal(status.running, false);
  });
});

test('concurrent starts during a slow Docker preflight create one meeting', async () => {
  let releaseDocker;
  const dockerGate = new Promise((resolve) => { releaseDocker = resolve; });
  let dockerCalls = 0;
  try {
    await withPanel(async panel => {
      const bootstrap = await panel.bootstrap();
      const start = () => fetch(`${panel.base}/api/start`, {
        method: 'POST',
        headers: panel.headers(bootstrap.token),
        body: JSON.stringify(panel.settings),
      });
      const first = start();
      await new Promise((resolve) => setTimeout(resolve, 80));
      const second = start();
      await new Promise((resolve) => setTimeout(resolve, 80));
      assert.equal(dockerCalls, 1);
      releaseDocker();
      const statuses = [(await first).status, (await second).status].sort();
      assert.deepEqual(statuses, [202, 409]);
      assert.equal(panel.daemon.calls.filter(call => call.path === '/v1/meetings').length, 1);
    }, {
      dockerTimeoutMs: 2000,
      runCommand: async (command) => {
        if (command === 'docker') {
          dockerCalls += 1;
          await dockerGate;
        }
        return { code: 0, stdout: 'ok', stderr: '' };
      },
    });
  } finally {
    releaseDocker();
  }
});

test('preflight reports Docker unavailable when docker info never returns', async () => {
  await withPanel(async panel => {
    const bootstrap = await panel.bootstrap();
    const started = Date.now();
    const response = await fetch(`${panel.base}/api/preflight`, {
      method: 'POST',
      headers: panel.headers(bootstrap.token),
      body: JSON.stringify(panel.settings),
    });
    const body = await response.json();
    assert.equal(response.status, 200);
    assert.equal(body.ready, false);
    assert.match(body.errors.docker, /Docker is unavailable/);
    assert.ok(Date.now() - started < 1500);
  }, {
    dockerTimeoutMs: 40,
    runCommand: (command) => {
      if (command === 'docker') return new Promise(() => {});
      return Promise.resolve({ code: 0, stdout: 'ok', stderr: '' });
    },
  });
});

test('recovers an active meeting after the portal process restarts', async () => {
  await withPanel(async panel => {
    const bootstrap = await panel.bootstrap();
    await fetch(`${panel.base}/api/start`, {
      method: 'POST',
      headers: panel.headers(bootstrap.token),
      body: JSON.stringify(panel.settings),
    });
    const meetingId = panel.daemon.calls[0] ? [...panel.daemon.meetings.keys()][0] : null;
    assert.ok(meetingId);
    const restarted = createServer({
      root: panel.root,
      runtimeRoot: panel.runtimeRoot,
      contextIndex: path.join(panel.runtimeRoot, 'context', 'index.json'),
      daemon: panel.daemon,
      runCommand: async () => ({ code: 0, stdout: '', stderr: '' }),
    });
    restarted.listen(0, '127.0.0.1');
    await once(restarted, 'listening');
    try {
      const status = await (await fetch(`http://127.0.0.1:${restarted.address().port}/api/status`)).json();
      assert.equal(status.running, true);
      assert.equal(status.meetingId, meetingId);
      assert.equal(status.phase, 'starting');
    } finally {
      restarted.close();
      await once(restarted, 'close');
    }
  });
});

test('context updates go to the daemon and are rejected after end', async () => {
  await withPanel(async panel => {
    const bootstrap = await panel.bootstrap();
    await fetch(`${panel.base}/api/start`, {
      method: 'POST',
      headers: panel.headers(bootstrap.token),
      body: JSON.stringify(panel.settings),
    });
    const added = await fetch(`${panel.base}/api/context/add`, {
      method: 'POST',
      headers: panel.headers(bootstrap.token),
      body: JSON.stringify({ text: 'Private launch target is October 4.', files: [] }),
    });
    assert.equal(added.status, 200);
    assert.equal(panel.daemon.calls.some(call => call.path.endsWith('/context')), true);
    await fetch(`${panel.base}/api/stop`, {
      method: 'POST',
      headers: panel.headers(bootstrap.token),
      body: '{}',
    });
    // Stop clears the active id; seed an ended meeting to assert the truthful reject.
    const endedId = [...panel.daemon.meetings.keys()][0];
    panel.daemon.meetings.get(endedId).state = 'ended';
    fs.mkdirSync(path.join(panel.root, '.colleague'), { recursive: true, mode: 0o700 });
    fs.writeFileSync(path.join(panel.root, '.colleague', 'portal-active.json'), JSON.stringify({ meetingId: endedId }));
    const rejected = await fetch(`${panel.base}/api/context/add`, {
      method: 'POST',
      headers: panel.headers(bootstrap.token),
      body: JSON.stringify({ text: 'After the call.', files: [] }),
    });
    assert.equal(rejected.status, 409);
  });
});

test('daemon unavailability is truthful and Teams auth cannot race a live meeting', async () => {
  await withPanel(async panel => {
    const bootstrap = await panel.bootstrap();
    panel.daemon.setUnavailable(true);
    const failed = await fetch(`${panel.base}/api/start`, {
      method: 'POST',
      headers: panel.headers(bootstrap.token),
      body: JSON.stringify(panel.settings),
    });
    assert.equal(failed.status, 503);
    panel.daemon.setUnavailable(false);
    assert.equal((await fetch(`${panel.base}/api/start`, {
      method: 'POST',
      headers: panel.headers(bootstrap.token),
      body: JSON.stringify(panel.settings),
    })).status, 202);
    const teams = await fetch(`${panel.base}/api/platforms/teams/connect`, {
      method: 'POST',
      headers: panel.headers(bootstrap.token),
      body: '{}',
    });
    assert.equal(teams.status, 409);
    assert.equal(panel.accountSpawns.length, 0);
    const google = await fetch(`${panel.base}/api/platforms/google/connect`, {
      method: 'POST',
      headers: panel.headers(bootstrap.token),
      body: '{}',
    });
    assert.equal(google.status, 409);
    assert.equal(panel.accountSpawns.length, 0);
  });
});

test('history lists and downloads structured handoff status without secrets', async () => {
  await withPanel(async panel => {
    const meetingId = 'mtg-portal00000001';
    const directory = path.join(panel.runtimeRoot, 'recordings', meetingId);
    fs.mkdirSync(directory, { recursive: true, mode: 0o700 });
    fs.writeFileSync(path.join(directory, 'transcript.txt'), 'Meeting: Ship Friday.');
    fs.writeFileSync(path.join(directory, 'handoff.json'), JSON.stringify({
      version: 1,
      meetingId,
      summary: 'Meeting ended (cancelled) with 1 transcript entries (partial).',
      handoffId: `hnd-${meetingId}`,
      partial: true,
      endReason: 'cancelled',
      apiKey: 'sk-should-be-stripped',
    }));
    fs.writeFileSync(path.join(directory, 'finalization.json'), JSON.stringify({
      status: 'append_failed',
      handoffId: `hnd-${meetingId}`,
      partial: true,
      endReason: 'cancelled',
      meetingId,
    }));
    const status = await (await fetch(`${panel.base}/api/status`)).json();
    const listed = status.sessions.find(item => item.id === meetingId);
    assert.equal(listed.handoffStatus, 'failed');
    assert.equal(listed.partial, true);
    assert.equal(listed.hasHandoff, true);
    const detail = await (await fetch(`${panel.base}/api/sessions/${meetingId}`)).json();
    assert.match(detail.transcript, /Ship Friday/);
    assert.equal(detail.handoffStatus, 'failed');
    assert.equal(detail.handoff.handoffId, `hnd-${meetingId}`);
    assert.equal('apiKey' in detail.handoff, false);
    assert.equal(JSON.stringify({ status, detail }).includes('sk-should-be-stripped'), false);
    const traversal = await fetch(`${panel.base}/api/sessions/not.valid`);
    assert.equal(traversal.status, 400);
  });
});

test('calls view reads calls openly and protects end and transfer', async () => {
  const actions = [];
  const callId = 'call-0123456789abcdef';
  const server = createServer({
    root: fs.mkdtempSync(path.join(os.tmpdir(), 'colleague-server-calls-')),
    daemon: {
      async listCalls(limit, tzOffset) { actions.push(['list', limit, tzOffset]); return { calls: [{ id: callId, status: 'in_progress' }] }; },
      async getCall(id) { return { id, status: 'in_progress' }; },
      async callEvents(id, after) { actions.push(['events', id, after]); return { events: [] }; },
      async endCall(id) { actions.push(['end', id]); return { id, status: 'summarizing' }; },
      async transferCall(id) { actions.push(['transfer', id]); return { transferred: true }; },
    },
  });
  server.listen(0, '127.0.0.1');
  await once(server, 'listening');
  try {
    const base = `http://127.0.0.1:${server.address().port}`;
    const page = await fetch(`${base}/calls`);
    assert.match(await page.text(), /id="transcript"/);
    assert.equal((await (await fetch(`${base}/api/calls?tzOffset=-240`)).json()).calls[0].id, callId);
    assert.deepEqual(actions.shift(), ['list', 50, '-240']);
    await fetch(`${base}/api/calls/${callId}/events?after=4`);
    assert.deepEqual(actions[0], ['events', callId, '4']);
    const denied = await fetch(`${base}/api/calls/${callId}/transfer`, { method: 'POST', body: '{}' });
    assert.equal(denied.status, 403);
    const bootstrap = await (await fetch(`${base}/api/bootstrap`)).json();
    const ended = await fetch(`${base}/api/calls/${callId}/end`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Origin: 'http://127.0.0.1:8095', 'X-Colleague-Token': bootstrap.token },
      body: '{}',
    });
    assert.equal(ended.status, 200);
    assert.deepEqual(actions.at(-1), ['end', callId]);
    // Malformed call ids never reach the daemon; unmatched API paths fall to the auth check.
    assert.equal((await fetch(`${base}/api/calls/call-XYZ`)).status, 403);
    assert.equal(actions.length, 2);
  } finally {
    server.close();
    await once(server, 'close');
  }
});

test('console only answers loopback host names', async () => {
  const { loopbackHost } = await import('./server.mjs');
  for (const host of ['127.0.0.1:8095', 'localhost:8095', '[::1]:8095', 'LOCALHOST']) {
    assert.equal(loopbackHost(host), true);
  }
  for (const host of ['evil.example:8095', '127.0.0.1.nip.io:8095', '', undefined]) {
    assert.equal(loopbackHost(host), false);
  }
  await withServer(async (base) => {
    const port = new URL(base).port;
    const rebound = await new Promise((resolve) => {
      http.get({ host: '127.0.0.1', port, path: '/api/calls', headers: { Host: `evil.example:${port}` } },
        (response) => { response.resume(); resolve(response.statusCode); });
    });
    assert.equal(rebound, 421);
  });
});

test('data paths follow COLLEAGUE_ROOT and COLLEAGUE_MEETING_DATA, and default to the checkout', async () => {
  const { consolePaths } = await import('./server.mjs');
  const code = path.resolve(path.dirname(new URL(import.meta.url).pathname), '..');
  const unset = consolePaths({ env: {} });
  assert.equal(unset.root, code);
  assert.equal(unset.runtimeRoot, path.join(code, 'meeting-runtime'));
  assert.equal(unset.contextIndex, path.join(code, 'meeting-runtime', 'context', 'index.json'));
  assert.equal(unset.meetingEnv, path.join(code, '.env.meeting'));
  const image = consolePaths({ env: { COLLEAGUE_ROOT: '/data', COLLEAGUE_MEETING_DATA: '/data/meetings' } });
  assert.deepEqual(image, {
    codeRoot: code,
    root: '/data',
    runtimeRoot: '/data/meetings',
    contextIndex: '/data/meetings/context/index.json',
    meetingEnv: '/data/.env.meeting',
    recordings: '/data/meetings/recordings',
    profileRoot: '/data/meetings/profiles',
  });
  // Only the data root moved: meeting data stays in the checkout.
  assert.equal(consolePaths({ env: { COLLEAGUE_ROOT: '/data' } }).runtimeRoot, path.join(code, 'meeting-runtime'));
  // An explicit root keeps its own meeting-runtime directory.
  assert.equal(consolePaths({ env: {}, root: '/tmp/x' }).runtimeRoot, '/tmp/x/meeting-runtime');
});

test('the console reads settings, recordings, and profiles from the data directories', async () => {
  const data = fs.mkdtempSync(path.join(os.tmpdir(), 'colleague-data-'));
  const meetings = path.join(data, 'meetings');
  fs.mkdirSync(path.join(meetings, 'profiles'), { recursive: true });
  fs.writeFileSync(path.join(meetings, 'profiles', 'teams-connected'), '');
  fs.mkdirSync(path.join(meetings, 'recordings', 'mtg-1'), { recursive: true });
  fs.writeFileSync(path.join(meetings, 'recordings', 'mtg-1', 'transcript.txt'), 'hello');
  fs.writeFileSync(path.join(data, '.env.meeting'), 'MEETING_URL=https://us05web.zoom.us/j/123456789\n');
  const previous = { root: process.env.COLLEAGUE_ROOT, meetings: process.env.COLLEAGUE_MEETING_DATA };
  process.env.COLLEAGUE_ROOT = data;
  process.env.COLLEAGUE_MEETING_DATA = meetings;
  let server;
  try {
    server = createServer({
      daemon: { async getMeeting() { throw Object.assign(new Error('Runtime daemon is not running.'), { code: 'daemon_offline', status: 503 }); } },
      runCommand: async () => ({ code: 0, stdout: '', stderr: '' }),
    });
  } finally {
    for (const [key, value] of [['COLLEAGUE_ROOT', previous.root], ['COLLEAGUE_MEETING_DATA', previous.meetings]]) {
      if (value === undefined) delete process.env[key];
      else process.env[key] = value;
    }
  }
  server.listen(0, '127.0.0.1');
  await once(server, 'listening');
  try {
    const base = `http://127.0.0.1:${server.address().port}`;
    assert.deepEqual(await (await fetch(`${base}/api/platforms/teams/status`)).json(), { connected: true });
    assert.deepEqual(await (await fetch(`${base}/api/platforms/google/status`)).json(), { connected: false });
    const bootstrap = await (await fetch(`${base}/api/bootstrap`)).json();
    assert.equal(bootstrap.settings.meetingUrl, 'https://us05web.zoom.us/j/123456789');
    assert.deepEqual(bootstrap.status.sessions.map((item) => item.id), ['mtg-1']);
    assert.equal((await (await fetch(`${base}/api/sessions/mtg-1`)).json()).transcript, 'hello');
  } finally {
    server.close();
    await once(server, 'close');
    fs.rmSync(data, { recursive: true, force: true });
  }
});

test('/mcp is the local agents endpoint, behind its own token rather than the console session', async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'colleague-console-mcp-'));
  const logs = [];
  const server = createServer({
    root,
    runtimeRoot: path.join(root, 'meeting-runtime'),
    daemon: {},
    runCommand: async () => ({ code: 0, stdout: '', stderr: '' }),
    mcpLog: (line) => logs.push(line),
  });
  server.listen(0, '127.0.0.1');
  await once(server, 'listening');
  const port = server.address().port;
  const post = (headers, body) => new Promise((resolve, reject) => {
    const text = JSON.stringify(body);
    const req = http.request({
      host: '127.0.0.1', port, path: '/mcp', method: 'POST',
      headers: { Host: `127.0.0.1:${port}`, 'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(text), ...headers },
    }, (response) => {
      let data = '';
      response.on('data', (chunk) => { data += chunk; });
      response.on('end', () => resolve({ status: response.statusCode, headers: response.headers, body: data ? JSON.parse(data) : null }));
    });
    req.on('error', reject);
    req.end(text);
  });
  const initialize = { jsonrpc: '2.0', id: 1, method: 'initialize', params: { protocolVersion: '2025-06-18' } };
  try {
    // The console's session token is no use here, and browsers are refused.
    const bootstrap = await (await fetch(`http://127.0.0.1:${port}/api/bootstrap`)).json();
    assert.equal((await post({ 'X-Colleague-Token': bootstrap.token }, initialize)).status, 401);
    assert.equal((await post({ Authorization: `Bearer ${bootstrap.token}` }, initialize)).status, 401);
    const token = fs.readFileSync(path.join(root, '.colleague', 'mcp.token'), 'utf8').trim();
    assert.equal(fs.statSync(path.join(root, '.colleague', 'mcp.token')).mode & 0o777, 0o600);
    assert.equal((await post({ Authorization: `Bearer ${token}`, Origin: `http://127.0.0.1:${port}` }, initialize)).status, 403);
    assert.equal((await post({ Authorization: `Bearer ${token}`, Host: `evil.example:${port}` }, initialize)).status, 421);
    const opened = await post({ Authorization: `Bearer ${token}` }, initialize);
    assert.equal(opened.status, 200);
    const listed = await post({ Authorization: `Bearer ${token}`, 'Mcp-Session-Id': opened.headers['mcp-session-id'] },
      { jsonrpc: '2.0', id: 2, method: 'tools/list' });
    assert.equal(listed.body.result.tools.length, 11);
    assert.ok(logs.some((line) => /session opened/.test(line)));
    assert.ok(!logs.join('\n').includes(token));
  } finally {
    server.close();
    server.closeAllConnections();
    await once(server, 'close');
    fs.rmSync(root, { recursive: true, force: true });
  }
});
