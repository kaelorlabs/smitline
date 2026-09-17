import assert from 'node:assert/strict';
import { once } from 'node:events';
import fs from 'node:fs';
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
    assert.match(await response.text(), /Meeting details/);
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
  const server = createServer({ contextIndex });
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
    for (const endpoint of ['/api/platforms/teams/connect', '/api/platforms/teams/disconnect']) {
      const result = await fetch(base + endpoint, { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({allowed:true}) });
      assert.equal(result.status, 403);
    }
  } finally { await new Promise(resolve => server.close(resolve)); }
});

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
        agentSession: payload.agentSession,
        context: payload.context,
        permissions: payload.permissions,
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
    async leaseStatus() {
      return { state: 'in_meeting' };
    },
    approvals: new Map(),
    async listApprovals(id) {
      calls.push({ method: 'GET', path: `/v1/meetings/${id}/approvals` });
      return { approvals: this.approvals.get(id) || [] };
    },
    async decideApproval(id, approvalId, body) {
      calls.push({ method: 'POST', path: `/v1/meetings/${id}/approvals/${approvalId}/decision`, body });
      const current = this.approvals.get(id) || [];
      const found = current.find((item) => item.id === approvalId);
      if (!found) {
        const error = new Error('approval not found');
        error.status = 404;
        error.code = 'not_found';
        throw error;
      }
      found.status = body.decision;
      found.decision = body.decision;
      return found;
    },
    async getHandoff(id) {
      calls.push({ method: 'GET', path: `/v1/meetings/${id}/handoff` });
      const handoff = this.handoffs.get(id);
      if (!handoff) {
        const error = new Error('handoff is not ready');
        error.status = 404;
        error.code = 'not_found';
        throw error;
      }
      return handoff;
    },
    async retryHandoff(id) {
      calls.push({ method: 'POST', path: `/v1/meetings/${id}/handoff/retry`, body: {} });
      if (this.failRetry) {
        const error = new Error('exact append still failed');
        error.status = 409;
        error.code = 'handoff_append_failed';
        throw error;
      }
      const handoff = {
        version: 1,
        meetingId: id,
        summary: 'Meeting ended.',
        handoffId: `hnd-${id}`,
        partial: false,
      };
      this.handoffs.set(id, handoff);
      return handoff;
    },
    handoffs: new Map(),
    failRetry: false,
  };
}

async function withPanel(run) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'colleague-panel-'));
  fs.writeFileSync(path.join(root, '.env'), 'OPENAI_API_KEY=sk-test\n', { mode: 0o600 });
  const workspace = path.join(root, 'project');
  fs.mkdirSync(workspace);
  const runtimeRoot = path.join(root, 'meeting-runtime');
  const daemon = createFakeDaemon();
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
  });
  server.listen(0, '127.0.0.1');
  await once(server, 'listening');
  const base = `http://127.0.0.1:${server.address().port}`;
  try {
    await run({
      base, root, workspace, runtimeRoot, daemon, accountSpawns,
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
        model: 'gpt-5.6-terra',
        workspace,
        meetingInstructions: 'Stay brief.',
        tools: { webSearch: false, codex: true, charts: false },
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
    assert.equal(panel.daemon.calls[0].path, '/v1/meetings');
    assert.equal(panel.daemon.calls[0].body.agentSession.sessionId, 'local-portal');
    assert.equal(panel.daemon.calls[0].body.agentSession.workspace, panel.workspace);
    assert.equal(fs.existsSync(path.join(panel.root, '.env.meeting')), false);
    assert.equal(fs.existsSync(path.join(panel.root, '.colleague', 'portal-active.json')), true);
    assert.equal(fs.existsSync(path.join(panel.runtimeRoot, 'run', 'portal-active.json')), false);
    assert.equal(fs.existsSync(path.join(panel.runtimeRoot, 'run', 'daemon.auth')), false);
    assert.equal(fs.existsSync(path.join(panel.runtimeRoot, 'run', 'daemon-data')), false);
    const status = await (await fetch(`${panel.base}/api/status`)).json();
    assert.equal(status.running, true);
    assert.equal(status.phase, 'starting');
    assert.equal(status.continuity, 'context');
    const encoded = JSON.stringify({ bootstrap, status, body });
    assert.equal(encoded.includes('Bearer'), false);
    assert.equal('OPENAI_API_KEY' in (bootstrap.settings || {}), false);
  });
});

test('status lists pending approvals and decide posts a single decision', async () => {
  await withPanel(async panel => {
    const bootstrap = await panel.bootstrap();
    const started = await fetch(`${panel.base}/api/start`, {
      method: 'POST',
      headers: panel.headers(bootstrap.token),
      body: JSON.stringify(panel.settings),
    });
    assert.equal(started.status, 202);
    const meetingId = [...panel.daemon.meetings.keys()][0];
    panel.daemon.approvals.set(meetingId, [{
      id: 'appr-1',
      meetingId,
      category: 'commands',
      summary: 'Run a workspace lookup',
      scope: { host: 'workspace' },
      status: 'pending',
      expiresAt: '2026-09-16T00:15:00Z',
    }]);
    const status = await (await fetch(`${panel.base}/api/status`)).json();
    assert.equal(status.pendingApprovals[0].id, 'appr-1');
    assert.equal(status.pendingApprovals[0].summary, 'Run a workspace lookup');
    const html = await (await fetch(`${panel.base}/`)).text();
    assert.match(html, /Pending approvals/);
    assert.equal(html.includes('Approve all'), false);
    const decided = await fetch(`${panel.base}/api/meetings/${meetingId}/approvals/appr-1/decision`, {
      method: 'POST',
      headers: panel.headers(bootstrap.token),
      body: JSON.stringify({ decision: 'denied' }),
    });
    assert.equal(decided.status, 200);
    const call = panel.daemon.calls.find((item) => String(item.path).includes('/decision'));
    assert.equal(call.body.decision, 'denied');
    const dumped = JSON.stringify({ status, html, decided: await decided.json() });
    assert.equal(dumped.includes('sk-test'), false);
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
    const denied = await fetch(`${panel.base}/api/sessions/${meetingId}/retry`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: '{}',
    });
    assert.equal(denied.status, 403);
    const traversal = await fetch(`${panel.base}/api/sessions/not.valid`);
    assert.equal(traversal.status, 400);
    const bootstrap = await panel.bootstrap();
    panel.daemon.failRetry = true;
    const failed = await fetch(`${panel.base}/api/sessions/${meetingId}/retry`, {
      method: 'POST',
      headers: panel.headers(bootstrap.token),
      body: '{}',
    });
    assert.equal(failed.status, 409);
    panel.daemon.failRetry = false;
    const retried = await fetch(`${panel.base}/api/sessions/${meetingId}/retry`, {
      method: 'POST',
      headers: panel.headers(bootstrap.token),
      body: '{}',
    });
    assert.equal(retried.status, 200);
    const body = await retried.json();
    assert.equal(body.handoffStatus, 'ready');
    assert.equal(body.handoff.handoffId, `hnd-${meetingId}`);
    assert.equal(JSON.stringify(body).includes('sk-should-be-stripped'), false);
  });
});
