import assert from 'node:assert/strict';
import { PassThrough } from 'node:stream';
import { spawn } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import test from 'node:test';
import { ColleagueError } from '../../sdk-typescript/src/index.mjs';
import { createMcpSession, TOOL_DEFINITIONS, TASKS_EXTENSION } from '../src/session.mjs';
import { startStdioServer } from '../src/server.mjs';

const serverPath = fileURLToPath(new URL('../src/server.mjs', import.meta.url));

function context() {
  return {
    version: 1,
    objective: 'Support this live meeting.',
    currentTask: 'Join the meeting and help when asked.',
    summary: '',
    decisions: [],
    constraints: [],
    openQuestions: [],
    importantFiles: [],
    recentConversation: [],
  };
}

function permissions() {
  return {
    workspace: 'read-only',
    commands: 'approval-required',
    edits: 'disabled',
    network: 'approval-required',
    commits: 'disabled',
    pushes: 'disabled',
  };
}

function exactArgs(overrides = {}) {
  return {
    url: 'https://zoom.us/j/555',
    provider: 'codex',
    sessionId: 'thread-abc',
    workspace: '/tmp/colleague-workspace',
    context: context(),
    permissions: permissions(),
    continuity: 'exact',
    ...overrides,
  };
}

function tasksMeta(progressToken = 'tok-1') {
  return {
    progressToken,
    'io.modelcontextprotocol/clientCapabilities': {
      extensions: { [TASKS_EXTENSION]: {} },
    },
  };
}

function makeHandoff(meetingId, extra = {}) {
  return {
    version: 1,
    meetingId,
    startedAt: '2026-09-16T00:00:00Z',
    endedAt: '2026-09-16T00:01:00Z',
    summary: extra.summary || 'done',
    decisions: [],
    requirements: [],
    actionItems: [],
    unresolvedQuestions: [],
    filesDiscussed: [],
    workPerformed: [],
    artifacts: [],
    transcriptPath: `recordings/${meetingId}/transcript.jsonl`,
    recommendedNextAction: 'review',
    handoffId: `hnd-${meetingId}`,
    archivePath: `recordings/${meetingId}`,
    ...extra,
  };
}

function createFakeColleague({ autoHandoff = true } = {}) {
  let created = 0;
  const meetings = new Map();
  const handoffs = new Map();
  const colleague = {
    created: () => created,
    async joinMeeting(request) {
      created += 1;
      const id = `mtg-${created}`;
      const session = {
        id,
        platform: 'zoom',
        meetingUrl: request.url,
        agentSession: request.agentSession,
        context: request.context,
        permissions: request.permissions,
        state: 'joining',
        startedAt: '2026-09-16T00:00:00Z',
      };
      meetings.set(id, session);
      let settle;
      const finished = new Promise((resolve, reject) => { settle = { resolve, reject }; });
      const listeners = [];
      const handle = {
        id,
        finished,
        async status() { return session; },
        async addContext(ctx) { session.context = ctx; return session; },
        async cancel() {
          session.state = 'ended';
          const handoff = makeHandoff(id, { partial: true, endReason: 'cancelled' });
          handoffs.set(id, handoff);
          settle.resolve(handoff);
          return session;
        },
        async retryFinalization() {
          const handoff = makeHandoff(id, { summary: 'retried' });
          handoffs.set(id, handoff);
          return handoff;
        },
        on(_name, handler) {
          listeners.push(handler);
          return () => {};
        },
      };
      setTimeout(() => {
        session.state = 'live';
        for (const handler of listeners) handler({ type: 'meeting.live', meetingId: id });
        for (const handler of listeners) handler({ type: 'transcript.final', text: 'secret meeting speech', meetingId: id });
        for (const handler of listeners) handler({ type: 'delegation.started', taskId: 'task-1', meetingId: id });
        if (autoHandoff) {
          const handoff = makeHandoff(id);
          handoffs.set(id, handoff);
          for (const handler of listeners) handler({ type: 'handoff.ready', meetingId: id, handoff });
          settle.resolve(handoff);
        }
      }, 20);
      return handle;
    },
    _transport: {
      getMeeting: async (id) => {
        if (!meetings.has(id)) {
          const error = new ColleagueError('not found', { code: 'not_found', status: 404 });
          throw error;
        }
        return meetings.get(id);
      },
      updateContext: async (id, ctx) => {
        meetings.get(id).context = ctx;
        return meetings.get(id);
      },
      cancelMeeting: async (id) => {
        meetings.get(id).state = 'ended';
        return meetings.get(id);
      },
      getHandoff: async (id) => {
        if (!handoffs.has(id)) {
          throw new ColleagueError('handoff not ready', { code: 'not_ready', status: 404 });
        }
        return handoffs.get(id);
      },
      retryHandoff: async (id) => makeHandoff(id, { summary: 'retried' }),
      listApprovals: async (id) => ({ approvals: colleague.approvals?.get(id) || [] }),
      getApproval: async (id, approvalId) => {
        const found = (colleague.approvals?.get(id) || []).find((item) => item.id === approvalId);
        if (!found) throw new ColleagueError('not found', { code: 'not_found', status: 404 });
        return found;
      },
      decideApproval: async (id, approvalId, decision) => {
        const found = (colleague.approvals?.get(id) || []).find((item) => item.id === approvalId);
        if (!found) throw new ColleagueError('not found', { code: 'not_found', status: 404 });
        const value = typeof decision === 'string' ? decision : decision.decision;
        found.status = value;
        found.decision = value;
        return found;
      },
      listArtifacts: async (id) => ({ artifacts: colleague.artifacts?.get(id) || [] }),
      getArtifact: async (id, artifactId) => {
        const found = (colleague.artifacts?.get(id) || []).find((item) => item.id === artifactId);
        if (!found) throw new ColleagueError('not found', { code: 'not_found', status: 404 });
        return found;
      },
      listCommits: async (id) => ({ commits: colleague.commits?.get(id) || [] }),
      getCommit: async (id, operationId) => {
        const found = (colleague.commits?.get(id) || []).find((item) => item.id === operationId);
        if (!found) throw new ColleagueError('not found', { code: 'not_found', status: 404 });
        return found;
      },
      createCommit: async (id, payload) => {
        const created = { id: payload.id || 'cmt-1', meetingId: id, kind: 'commit', status: 'requested', request: payload };
        colleague.commits.set(id, [...(colleague.commits.get(id) || []), created]);
        return created;
      },
      listPushes: async (id) => ({ pushes: colleague.pushes?.get(id) || [] }),
      getPush: async (id, operationId) => {
        const found = (colleague.pushes?.get(id) || []).find((item) => item.id === operationId);
        if (!found) throw new ColleagueError('not found', { code: 'not_found', status: 404 });
        return found;
      },
      createPush: async (id, payload) => {
        const created = { id: payload.id || 'psh-1', meetingId: id, kind: 'push', status: 'requested', request: payload };
        colleague.pushes.set(id, [...(colleague.pushes.get(id) || []), created]);
        return created;
      },
    },
  };
  colleague.approvals = new Map();
  colleague.artifacts = new Map();
  colleague.commits = new Map();
  colleague.pushes = new Map();
  return colleague;
}

async function call(session, method, params, id = 1) {
  return session.dispatch({ jsonrpc: '2.0', id, method, params });
}

async function callTool(session, name, args, extra = {}) {
  return session.dispatch({
    jsonrpc: '2.0',
    id: extra.id || 2,
    method: 'tools/call',
    params: { name, arguments: args, _meta: extra.meta },
  });
}

test('initialize advertises tools and the tasks extension', async () => {
  const session = createMcpSession({ colleague: createFakeColleague(), log() {} });
  const init = await call(session, 'initialize', {
    protocolVersion: '2025-06-18',
    clientInfo: { name: 'test', version: '1' },
    capabilities: {},
  });
  assert.equal(init.result.serverInfo.name, 'colleague-ai');
  assert.ok(init.result.capabilities.extensions[TASKS_EXTENSION]);
  assert.match(init.result.instructions, /sessionId/);
  const listed = await call(session, 'tools/list', {});
  const names = listed.result.tools.map((tool) => tool.name);
  assert.deepEqual(names, [
    'start_meeting', 'get_meeting_status', 'add_meeting_context',
    'cancel_meeting', 'get_meeting_handoff', 'retry_meeting_handoff',
    'list_meeting_approvals', 'get_meeting_approval', 'decide_meeting_approval',
    'list_meeting_artifacts', 'get_meeting_artifact',
    'list_meeting_commits', 'get_meeting_commit', 'create_meeting_commit',
    'list_meeting_pushes', 'get_meeting_push', 'create_meeting_push',
  ]);
  assert.equal(listed.result.tools.length, TOOL_DEFINITIONS.length);
});

test('exact start requires explicit session and preserves it', async () => {
  const colleague = createFakeColleague();
  const session = createMcpSession({ colleague, log() {} });
  const result = await callTool(session, 'start_meeting', exactArgs());
  const body = result.result.structuredContent;
  assert.equal(body.meetingId, 'mtg-1');
  assert.equal(body.continuity, 'exact');
  const status = await callTool(session, 'get_meeting_status', { meetingId: 'mtg-1' });
  assert.equal(status.result.structuredContent.agentSession.sessionId, 'thread-abc');
  assert.equal(colleague.created(), 1);
});

test('approval tools require explicit meeting and approval ids', async () => {
  const colleague = createFakeColleague({ autoHandoff: false });
  const session = createMcpSession({ colleague, log() {} });
  await callTool(session, 'start_meeting', exactArgs());
  const missing = await callTool(session, 'decide_meeting_approval', { decision: 'approved' });
  assert.equal(missing.result.isError, true);
  colleague.approvals.set('mtg-1', [{
    id: 'appr-1', meetingId: 'mtg-1', category: 'commands',
    summary: 'Run a workspace lookup', status: 'pending',
  }]);
  const listed = await callTool(session, 'list_meeting_approvals', { meetingId: 'mtg-1' });
  assert.equal(listed.result.structuredContent.approvals[0].id, 'appr-1');
  const decided = await callTool(session, 'decide_meeting_approval', {
    meetingId: 'mtg-1', approvalId: 'appr-1', decision: 'denied',
  });
  assert.equal(decided.result.structuredContent.approval.status, 'denied');
  const dumped = JSON.stringify(decided);
  assert.equal(dumped.includes('secret meeting speech'), false);
  colleague.artifacts.set('mtg-1', [{
    id: 'art-1', meetingId: 'mtg-1', kind: 'plan', description: 'Workspace action plan',
  }]);
  const artifacts = await callTool(session, 'list_meeting_artifacts', { meetingId: 'mtg-1' });
  assert.equal(artifacts.result.structuredContent.artifacts[0].id, 'art-1');
  const missingArtifact = await callTool(session, 'get_meeting_artifact', { meetingId: 'mtg-1' });
  assert.equal(missingArtifact.result.isError, true);
  const missingCommit = await callTool(session, 'create_meeting_commit', { expectedHead: 'a'.repeat(40) });
  assert.equal(missingCommit.result.isError, true);
  const createdCommit = await callTool(session, 'create_meeting_commit', {
    meetingId: 'mtg-1',
    expectedHead: 'a'.repeat(40),
    message: 'Record reviewed helper changes',
    files: [{ path: 'helper.py', sha256: 'b'.repeat(64) }],
  });
  assert.equal(createdCommit.result.structuredContent.commit.status, 'requested');
  const listedCommits = await callTool(session, 'list_meeting_commits', { meetingId: 'mtg-1' });
  assert.equal(listedCommits.result.structuredContent.commits[0].id, 'cmt-1');
  const createdPush = await callTool(session, 'create_meeting_push', {
    meetingId: 'mtg-1',
    commitSha: 'a'.repeat(40),
    remote: 'origin',
    branch: 'colleague-work',
  });
  assert.equal(createdPush.result.structuredContent.push.kind, 'push');
});

test('rejects last/latest and does not invent a session', async () => {
  const session = createMcpSession({ colleague: createFakeColleague(), log() {} });
  for (const sessionId of ['last', '--last', 'latest', '--latest']) {
    const result = await callTool(session, 'start_meeting', exactArgs({ sessionId }));
    assert.equal(result.result.isError, true);
    assert.match(result.result.structuredContent.message, /sessionId|exact continuity|last/i);
  }
  const missing = await callTool(session, 'start_meeting', exactArgs({ sessionId: undefined, continuity: undefined }));
  assert.equal(missing.result.isError, true);
});

test('context continuity uses local-portal only when explicitly requested', async () => {
  const session = createMcpSession({ colleague: createFakeColleague(), log() {} });
  const result = await callTool(session, 'start_meeting', exactArgs({
    continuity: 'context',
    sessionId: undefined,
  }));
  const status = await callTool(session, 'get_meeting_status', {
    meetingId: result.result.structuredContent.meetingId,
  });
  assert.equal(status.result.structuredContent.agentSession.sessionId, 'local-portal');
  assert.equal(status.result.structuredContent.agentSession.metadata.continuity, 'context');
});

test('polling client gets a handle immediately and later a handoff', async () => {
  const session = createMcpSession({ colleague: createFakeColleague({ autoHandoff: true }), log() {} });
  const started = await callTool(session, 'start_meeting', exactArgs({ waitUntilHandoff: true }));
  assert.equal(started.result.structuredContent.meetingId, 'mtg-1');
  assert.match(started.result.structuredContent.note, /poll get_meeting_handoff/);
  assert.notEqual(started.result.resultType, 'task');
  await new Promise((resolve) => setTimeout(resolve, 40));
  const handoff = await callTool(session, 'get_meeting_handoff', { meetingId: 'mtg-1' });
  assert.equal(handoff.result.structuredContent.handoff.handoffId, 'hnd-mtg-1');
});

test('task-capable waitUntilHandoff returns CreateTaskResult and hides transcript text', async () => {
  const session = createMcpSession({ colleague: createFakeColleague(), log() {} });
  const started = await callTool(session, 'start_meeting', exactArgs({ waitUntilHandoff: true }), { meta: tasksMeta() });
  assert.equal(started.result.resultType, 'task');
  assert.equal(started.result.status, 'working');
  await new Promise((resolve) => setTimeout(resolve, 50));
  const got = await call(session, 'tasks/get', { taskId: started.result.taskId, _meta: tasksMeta() }, 9);
  assert.equal(got.result.status, 'completed');
  assert.equal(got.result.result.structuredContent.handoff.meetingId, 'mtg-1');
  const dumped = JSON.stringify(session.notifications);
  assert.ok(!dumped.includes('secret meeting speech'));
  assert.ok(dumped.includes('transcript transcript.final') || dumped.includes('delegation started') || dumped.includes('handoff ready'));
});

test('each remaining tool maps onto the SDK handle', async () => {
  const session = createMcpSession({ colleague: createFakeColleague({ autoHandoff: false }), log() {} });
  await callTool(session, 'start_meeting', exactArgs());
  const updated = await callTool(session, 'add_meeting_context', {
    meetingId: 'mtg-1',
    context: { ...context(), objective: 'follow up' },
  });
  assert.equal(updated.result.structuredContent.context.objective, 'follow up');
  const retried = await callTool(session, 'retry_meeting_handoff', { meetingId: 'mtg-1' });
  assert.equal(retried.result.structuredContent.handoff.summary, 'retried');
  const cancelled = await callTool(session, 'cancel_meeting', { meetingId: 'mtg-1' });
  assert.equal(cancelled.result.structuredContent.cancelled, true);
  assert.equal(cancelled.result.structuredContent.handoff.partial, true);
});

test('startup errors are mapped without leaking tokens', async () => {
  const colleague = {
    async joinMeeting() {
      throw new ColleagueError('Bearer secret-token-value rejected', { code: 'startup', status: 503 });
    },
  };
  const session = createMcpSession({ colleague, log() {} });
  const result = await callTool(session, 'start_meeting', exactArgs());
  assert.equal(result.result.isError, true);
  assert.ok(!JSON.stringify(result).includes('secret-token-value'));
});

test('shutdown cancels live meetings unless leave-running is set', async () => {
  const session = createMcpSession({ colleague: createFakeColleague({ autoHandoff: false }), log() {} });
  await callTool(session, 'start_meeting', exactArgs());
  const stopped = await session.shutdown();
  assert.deepEqual(stopped.cancelled, ['mtg-1']);
  const left = createMcpSession({
    colleague: createFakeColleague({ autoHandoff: false }),
    leaveRunningOnShutdown: true,
    log() {},
  });
  await callTool(left, 'start_meeting', exactArgs());
  const result = await left.shutdown();
  assert.deepEqual(result.leftRunning, ['mtg-1']);
});

test('stdio stdout is protocol frames only', async () => {
  const stdin = new PassThrough();
  const stdout = new PassThrough();
  let out = '';
  stdout.on('data', (chunk) => { out += chunk; });
  const { session } = startStdioServer({
    stdin,
    stdout,
    colleague: createFakeColleague(),
    installSignals: false,
    log() {},
  });
  stdin.write(`${JSON.stringify({ jsonrpc: '2.0', id: 1, method: 'initialize', params: { protocolVersion: '2025-06-18' } })}\n`);
  stdin.write(`${JSON.stringify({ jsonrpc: '2.0', id: 2, method: 'tools/list', params: {} })}\n`);
  await new Promise((resolve) => setTimeout(resolve, 40));
  await session.shutdown();
  assert.ok(!out.includes('secret'));
  assert.ok(!out.includes('Bearer'));
  for (const line of out.split('\n').filter(Boolean)) {
    const parsed = JSON.parse(line);
    assert.equal(parsed.jsonrpc, '2.0');
  }
});

test('spawned process does not print logs on stdout', async () => {
  const child = spawn(process.execPath, [serverPath], { stdio: ['pipe', 'pipe', 'pipe'] });
  let stdout = '';
  let stderr = '';
  child.stdout.on('data', (chunk) => { stdout += chunk; });
  child.stderr.on('data', (chunk) => { stderr += chunk; });
  child.stdin.write(`${JSON.stringify({ jsonrpc: '2.0', id: 1, method: 'initialize', params: {} })}\n`);
  const deadline = Date.now() + 2000;
  while (Date.now() < deadline && !(stdout.includes('"jsonrpc"'))) {
    await new Promise((resolve) => setTimeout(resolve, 20));
  }
  child.kill('SIGTERM');
  await new Promise((resolve) => child.on('close', resolve));
  assert.ok(stdout.includes('"jsonrpc":"2.0"') || stdout.includes('"jsonrpc": "2.0"'));
  for (const line of stdout.split('\n').filter(Boolean)) {
    const parsed = JSON.parse(line);
    assert.equal(parsed.jsonrpc, '2.0');
  }
  assert.ok(!stdout.toLowerCase().includes('daemon.auth'));
  assert.ok(!stdout.includes('Bearer'));
});
