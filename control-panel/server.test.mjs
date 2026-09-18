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
    artifacts: new Map(),
    commits: new Map(),
    pushes: new Map(),
    async listApprovals(id) {
      calls.push({ method: 'GET', path: `/v1/meetings/${id}/approvals` });
      return { approvals: this.approvals.get(id) || [] };
    },
    async listArtifacts(id) {
      calls.push({ method: 'GET', path: `/v1/meetings/${id}/artifacts` });
      return { artifacts: this.artifacts.get(id) || [] };
    },
    async listCommits(id) {
      calls.push({ method: 'GET', path: `/v1/meetings/${id}/commits` });
      return { commits: this.commits.get(id) || [] };
    },
    async listPushes(id) {
      calls.push({ method: 'GET', path: `/v1/meetings/${id}/pushes` });
      return { pushes: this.pushes.get(id) || [] };
    },
    screenShare: new Map(),
    async getScreenShare(id) {
      calls.push({ method: 'GET', path: `/v1/meetings/${id}/screen-share` });
      return this.screenShare.get(id) || {
        status: { enabled: false, paused: false, capturing: false, degradedReason: 'disabled' },
        observations: [],
      };
    },
    async pauseScreenShare(id) {
      calls.push({ method: 'POST', path: `/v1/meetings/${id}/screen-share/pause`, body: {} });
      const current = this.screenShare.get(id) || { status: { enabled: true }, observations: [] };
      current.status = { ...current.status, enabled: true, paused: true, capturing: false };
      this.screenShare.set(id, current);
      return current;
    },
    async resumeScreenShare(id) {
      calls.push({ method: 'POST', path: `/v1/meetings/${id}/screen-share/resume`, body: {} });
      const current = this.screenShare.get(id) || { status: { enabled: true }, observations: [] };
      current.status = { ...current.status, enabled: true, paused: false };
      this.screenShare.set(id, current);
      return current;
    },
    async listScreenShareObservations(id) {
      calls.push({ method: 'GET', path: `/v1/meetings/${id}/screen-share/observations` });
      const current = this.screenShare.get(id) || { observations: [] };
      return { observations: current.observations || [] };
    },
    async listProviders() {
      calls.push({ method: 'GET', path: '/v1/providers' });
      return {
        providers: [
          { id: 'codex', installed: true, usable: true, exactSessionResume: true, contextContinuity: true },
        ],
      };
    },
    runner: { paired: false, pending: null },
    async runnerStatus() {
      calls.push({ method: 'GET', path: '/v1/runner' });
      return {
        paired: Boolean(this.runner.paired),
        mode: 'loopback',
        protocolVersion: 1,
        controlPlane: this.runner.paired ? 'mock-remote' : 'local',
      };
    },
    async pairRunner(payload = {}) {
      calls.push({ method: 'POST', path: '/v1/runner/pair', body: payload });
      this.runner.pending = { pairingId: 'pair-test1', pairingCode: 'ABCD2345', used: false };
      return {
        pairingId: 'pair-test1',
        pairingCode: 'ABCD2345',
        expiresAt: '2026-09-17T12:02:00Z',
      };
    },
    async completeRunnerPair(payload) {
      calls.push({ method: 'POST', path: '/v1/runner/pair/complete', body: payload });
      if (!this.runner.pending || this.runner.pending.used) {
        const error = new Error('pairing code was already used');
        error.status = 409;
        error.code = 'pairing_replay';
        throw error;
      }
      if (payload.pairingId !== this.runner.pending.pairingId || payload.pairingCode !== this.runner.pending.pairingCode) {
        const error = new Error('pairing code is invalid');
        error.status = 401;
        error.code = 'pairing_mismatch';
        throw error;
      }
      this.runner.pending.used = true;
      this.runner.paired = true;
      return {
        deviceId: 'dev-test1',
        deviceEnrollment: 'enroll-once-value',
        tenantId: 'ten-local',
        userId: 'usr-local',
      };
    },
    async unpairRunner() {
      calls.push({ method: 'POST', path: '/v1/runner/unpair', body: {} });
      this.runner = { paired: false, pending: null };
      return { paired: false, mode: 'loopback', controlPlane: 'local' };
    },
    async getArtifactContent(id, artifactId) {
      calls.push({ method: 'GET', path: `/v1/meetings/${id}/artifacts/${artifactId}/content` });
      const found = (this.artifacts.get(id) || []).find((item) => item.id === artifactId);
      if (!found) {
        const error = new Error('artifact not found');
        error.status = 404;
        error.code = 'not_found';
        throw error;
      }
      return { mediaType: 'application/json', body: Buffer.from(JSON.stringify(found)) };
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

async function withPanel(run, extra = {}) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'colleague-panel-'));
  fs.writeFileSync(path.join(root, '.env'), 'OPENAI_API_KEY=sk-test\n', { mode: 0o600 });
  const workspace = path.join(root, 'project');
  fs.mkdirSync(workspace);
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
    ...extra,
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
    const created = panel.daemon.calls.find((item) => item.path === '/v1/meetings');
    assert.ok(created);
    assert.equal(created.body.agentSession.sessionId, 'local-portal');
    assert.equal(created.body.agentSession.workspace, panel.workspace);
    assert.equal(created.body.screenShare.enabled, false);
    assert.ok(panel.daemon.calls.some((item) => item.path === '/v1/providers'));
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
    panel.daemon.artifacts.set(meetingId, [{
      id: 'art-1', kind: 'plan', description: 'Workspace action plan', bytes: 24,
      changedFiles: [{ path: 'src.py' }],
    }]);
    const workspaceStatus = await (await fetch(`${panel.base}/api/status`)).json();
    assert.equal(workspaceStatus.workspaceArtifacts[0].id, 'art-1');
    const workspaceHtml = await (await fetch(`${panel.base}/`)).text();
    assert.match(workspaceHtml, /Workspace activity/);
    panel.daemon.commits.set(meetingId, [{
      id: 'cmt-1', kind: 'commit', status: 'requested', meetingId,
    }]);
    const gitStatus = await (await fetch(`${panel.base}/api/status`)).json();
    assert.equal(gitStatus.gitOperations[0].id, 'cmt-1');
    const gitHtml = await (await fetch(`${panel.base}/`)).text();
    assert.match(gitHtml, /Git operations/);
    panel.daemon.screenShare.set(meetingId, {
      status: { enabled: true, paused: false, capturing: true, available: true, active: true },
      observations: [{
        id: 'obs-1', meetingId, summary: 'A red slide with a chart', confidence: 0.8,
        frameArtifactId: 'art-1', timestamp: '2026-09-16T00:00:00Z',
      }],
    });
    const shareStatus = await (await fetch(`${panel.base}/api/status`)).json();
    assert.equal(shareStatus.screenShare.status.enabled, true);
    assert.equal(shareStatus.screenShare.observations[0].summary, 'A red slide with a chart');
    const shareHtml = await (await fetch(`${panel.base}/`)).text();
    assert.match(shareHtml, /Understand shared content/);
    assert.match(shareHtml, /Shared content/);
    const pausedShare = await fetch(`${panel.base}/api/meetings/${meetingId}/screen-share/pause`, {
      method: 'POST',
      headers: panel.headers(bootstrap.token),
      body: JSON.stringify({}),
    });
    assert.equal(pausedShare.status, 200);
    assert.equal((await pausedShare.json()).status.paused, true);
    const downloaded = await fetch(`${panel.base}/api/meetings/${meetingId}/artifacts/art-1/content`, {
      headers: panel.headers(bootstrap.token),
    });
    assert.equal(downloaded.status, 200);
    assert.match(downloaded.headers.get('content-disposition') || '', /attachment/);
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

test('runner pairing reveals the code once and omits enrollment from status', async () => {
  await withPanel(async panel => {
    const html = await (await fetch(`${panel.base}/`)).text();
    assert.match(html, /Pair runner/);
    assert.match(html, /shown once/);
    const bootstrap = await panel.bootstrap();
    assert.equal(bootstrap.status.runner.paired, false);
    assert.equal('pairingCode' in bootstrap.status.runner, false);
    const started = await fetch(`${panel.base}/api/runner/pair`, {
      method: 'POST',
      headers: panel.headers(bootstrap.token),
      body: '{}',
    });
    assert.equal(started.status, 201);
    const pairing = await started.json();
    assert.equal(typeof pairing.pairingCode, 'string');
    const completed = await fetch(`${panel.base}/api/runner/pair/complete`, {
      method: 'POST',
      headers: panel.headers(bootstrap.token),
      body: JSON.stringify({ pairingId: pairing.pairingId, pairingCode: pairing.pairingCode }),
    });
    assert.equal(completed.status, 201);
    const enrollment = await completed.json();
    assert.equal('deviceEnrollment' in enrollment, false);
    const replay = await fetch(`${panel.base}/api/runner/pair/complete`, {
      method: 'POST',
      headers: panel.headers(bootstrap.token),
      body: JSON.stringify({ pairingId: pairing.pairingId, pairingCode: pairing.pairingCode }),
    });
    assert.equal(replay.status, 409);
    const status = await (await fetch(`${panel.base}/api/status`)).json();
    assert.equal(status.runner.paired, true);
    const dumped = JSON.stringify({ pairing, enrollment, status });
    assert.equal(dumped.includes('enroll-once-value'), false);
    const unpaired = await fetch(`${panel.base}/api/runner/unpair`, {
      method: 'POST',
      headers: panel.headers(bootstrap.token),
      body: '{}',
    });
    assert.equal((await unpaired.json()).paired, false);
  });
});
