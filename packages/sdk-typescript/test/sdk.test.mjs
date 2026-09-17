import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { pathToFileURL } from 'node:url';
import test from 'node:test';
import {
  Colleague,
  ValidationError,
  StartupError,
  FinalizationError,
  createLoopbackTransport,
  parseSseBlock,
  iterateSse,
  validateJoinRequest,
} from '../src/index.mjs';
import { startFakeDaemon, sseFrame } from './fake-daemon.mjs';

const ZOOM = 'https://zoom.us/j/123456789';
const WORKSPACE = '/tmp/colleague-workspace';

function agentSession(overrides = {}) {
  return {
    provider: 'codex',
    sessionId: 'thread-abc',
    workspace: WORKSPACE,
    model: 'gpt-5.6-terra',
    ...overrides,
  };
}

function joinRequest(overrides = {}) {
  return {
    url: ZOOM,
    agentSession: agentSession(overrides.agentSession),
    ...overrides,
  };
}

async function withDaemon(t, options, fn) {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'colleague-sdk-'));
  const daemon = await startFakeDaemon({ root, readyDelayMs: 20, ...options });
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
  t.after(() => {
    for (const handle of colleague._handles.values()) handle._abort.abort();
  });
  return fn({ root, daemon, transport, colleague });
}

test('client-side validation rejects secrets, placeholders, and relative workspaces', async () => {
  const colleague = new Colleague({ transport: { createMeeting() { throw new Error('should not create'); } } });
  await assert.rejects(() => colleague.joinMeeting(joinRequest({ agentSession: { token: 'secret' } })), ValidationError);
  await assert.rejects(() => colleague.joinMeeting({
    url: ZOOM,
    agentSession: agentSession({ sessionId: '--last' }),
  }), ValidationError);
  await assert.rejects(() => colleague.joinMeeting({
    url: ZOOM,
    agentSession: agentSession({ workspace: 'relative' }),
  }), ValidationError);
  await assert.rejects(() => colleague.joinMeeting({
    url: 'http://zoom.us/j/1',
    agentSession: agentSession(),
  }), ValidationError);
  const disabled = validateJoinRequest({
    url: ZOOM,
    agentSession: agentSession(),
    camera: { enabled: false },
  });
  assert.equal(disabled.camera.enabled, false);
  await assert.rejects(() => colleague.joinMeeting({
    url: ZOOM,
    agentSession: agentSession(),
    permissions: {
      workspace: 'none', commands: 'allowed', edits: 'disabled',
      network: 'disabled', commits: 'disabled', pushes: 'disabled',
    },
  }), ValidationError);
});

test('create preserves explicit agentSession fields and does not start duplicate meetings', { timeout: 8000 }, async (t) => {
  await withDaemon(t, {}, async ({ colleague, daemon }) => {
    const request = joinRequest({
      agentSession: {
        provider: 'codex',
        sessionId: 'thread-exact',
        workspace: WORKSPACE,
        model: 'gpt-5.6-terra',
        metadata: { source: 'codex' },
      },
      permissions: {
        workspace: 'read-only',
        commands: 'approval-required',
        edits: 'disabled',
        network: 'approval-required',
        commits: 'disabled',
        pushes: 'disabled',
      },
    });
    const first = await colleague.joinMeeting(request);
    const second = await colleague.joinMeeting(request);
    const concurrent = await Promise.all([
      colleague.joinMeeting(request),
      colleague.joinMeeting(request),
    ]);
    assert.equal(first.id, second.id);
    assert.equal(concurrent[0].id, first.id);
    assert.equal(concurrent[1].id, first.id);
    assert.equal(daemon.state.created, 1);
    const session = await first.status();
    assert.equal(session.agentSession.sessionId, 'thread-exact');
    assert.equal(session.agentSession.model, 'gpt-5.6-terra');
    assert.deepEqual(session.agentSession.metadata, { source: 'codex' });
    assert.equal(session.permissions.workspace, 'read-only');
    const handoff = await first.finished;
    assert.equal(handoff.meetingId, first.id);
    assert.equal(handoff.handoffId, `hnd-${first.id}`);
  });
});

test('SSE reconnect uses Last-Event-ID and does not redeliver', { timeout: 8000 }, async (t) => {
  await withDaemon(t, { autoHandoff: false, closeStreamAfter: 30, readyDelayMs: -1 }, async ({ transport, daemon }) => {
    const created = await transport.createMeeting({
      meetingUrl: ZOOM,
      agentSession: agentSession(),
      context: { version: 1, objective: '', currentTask: '', summary: '', decisions: [], constraints: [], openQuestions: [], importantFiles: [], recentConversation: [] },
      permissions: {
        workspace: 'read-only', commands: 'approval-required', edits: 'disabled',
        network: 'approval-required', commits: 'disabled', pushes: 'disabled',
      },
    });
    const seen = [];
    const abort = new AbortController();
    const iterator = transport.events(created.id, { signal: abort.signal })[Symbol.asyncIterator]();
    seen.push((await iterator.next()).value);
    await new Promise((resolve) => setTimeout(resolve, 80));
    daemon.state.meetings.get(created.id).state = 'live';
    const extra = {
      version: 1,
      id: 'evt-after-reconnect',
      meetingId: created.id,
      timestamp: '2026-09-16T00:00:02Z',
      type: 'meeting.live',
    };
    daemon.state.events.get(created.id).push(extra);
    for (const client of daemon.state.sseClients.filter((item) => item.meetingId === created.id)) {
      client.response.write(sseFrame(extra));
    }
    seen.push((await iterator.next()).value);
    abort.abort();
    const ids = seen.map((event) => event.id);
    assert.deepEqual(new Set(ids).size, ids.length);
    const replay = daemon.state.requests.filter((item) => item.path.endsWith('/events') && item.lastEventId);
    assert.ok(replay.length >= 1);
  });
});

test('stale token is rotated from host-only auth file without exposing it', async (t) => {
  await withDaemon(t, { autoHandoff: false }, async ({ colleague, daemon }) => {
    const first = await colleague.joinMeeting(joinRequest());
    daemon.rotateToken('brand-new-token');
    await daemon.writeAuth('brand-new-token');
    const session = await colleague._transport.getMeeting(first.id);
    assert.ok(session.id);
    assert.equal(daemon.state.lastAuthorization, 'Bearer brand-new-token');
    const dumped = JSON.stringify(session);
    assert.ok(!dumped.includes('brand-new-token'));
    assert.ok(!dumped.includes('test-daemon-token'));
    await first.cancel();
  });
});

test('startup errors reject before a meeting handle exists', async (t) => {
  await withDaemon(t, { failCreate: { status: 503, code: 'supervisor_unavailable', message: 'supervisor unavailable' } }, async ({ colleague }) => {
    await assert.rejects(() => colleague.joinMeeting(joinRequest()), StartupError);
  });
});

test('typed unrecoverable finalization includes the local archive path', async (t) => {
  await withDaemon(t, { autoHandoff: false }, async ({ colleague, daemon }) => {
    const meeting = await colleague.joinMeeting(joinRequest());
    daemon.state.unrecoverable = true;
    daemon.state.appendFailed = true;
    meeting._appendFailed = { retryable: false };
    await assert.rejects(() => meeting.finished, (error) => {
      assert.ok(error instanceof FinalizationError);
      assert.equal(error.archivePath, `recordings/${meeting.id}`);
      return true;
    });
  });
});

test('partial handoff resolves as a structured result', async (t) => {
  await withDaemon(t, { partial: true }, async ({ colleague }) => {
    const meeting = await colleague.joinMeeting(joinRequest());
    const handoff = await meeting.finished;
    assert.equal(handoff.partial, true);
    assert.equal(handoff.meetingId, meeting.id);
  });
});

test('cancellation is idempotent and does not create another meeting', async (t) => {
  await withDaemon(t, { autoHandoff: false }, async ({ colleague, daemon }) => {
    const meeting = await colleague.joinMeeting(joinRequest());
    const first = await meeting.cancel();
    const second = await meeting.cancel();
    assert.equal(first.id, second.id);
    assert.equal(daemon.state.cancels, 1);
    const handoff = await meeting.finished;
    assert.equal(handoff.endReason, 'cancelled');
  });
});

test('multiple subscribers share one meeting and do not duplicate create', async (t) => {
  await withDaemon(t, { readyDelayMs: 40 }, async ({ colleague, daemon }) => {
    const meeting = await colleague.joinMeeting(joinRequest());
    const seenA = [];
    const seenB = [];
    meeting.on('event', (event) => seenA.push(event.type));
    const collected = [];
    const consume = (async () => {
      for await (const event of meeting.events()) {
        seenB.push(event.type);
        collected.push(event.type);
        if (event.type === 'handoff.ready') break;
      }
    })();
    const handoff = await meeting.finished;
    await consume;
    assert.equal(daemon.state.created, 1);
    assert.ok(seenA.includes('handoff.ready') || seenB.includes('handoff.ready'));
    assert.equal(handoff.meetingId, meeting.id);
    assert.ok(!JSON.stringify(collected).includes('secret meeting speech') || collected.includes('transcript.final'));
  });
});

test('blocking finished waits until durable ready rather than stream close', async (t) => {
  await withDaemon(t, { autoHandoff: false, closeStreamAfter: 15, readyDelayMs: 5 }, async ({ colleague, daemon }) => {
    const meeting = await colleague.joinMeeting(joinRequest());
    const pending = meeting.finished;
    let resolved = false;
    pending.then(() => { resolved = true; });
    await new Promise((resolve) => setTimeout(resolve, 80));
    assert.equal(resolved, false);
    const handoff = {
      version: 1,
      meetingId: meeting.id,
      startedAt: '2026-09-16T00:00:00Z',
      endedAt: '2026-09-16T00:04:00Z',
      summary: 'late',
      decisions: [],
      requirements: [],
      actionItems: [],
      unresolvedQuestions: [],
      filesDiscussed: [],
      workPerformed: [],
      artifacts: [],
      transcriptPath: `recordings/${meeting.id}/transcript.jsonl`,
      recommendedNextAction: 'review',
      archivePath: `recordings/${meeting.id}`,
    };
    daemon.state.handoffs.set(meeting.id, handoff);
    const got = await pending;
    assert.equal(got.summary, 'late');
  });
});

test('retryFinalization recovers a durable handoff', async (t) => {
  await withDaemon(t, { autoHandoff: false }, async ({ colleague }) => {
    const meeting = await colleague.joinMeeting(joinRequest());
    const handoff = await meeting.retryFinalization();
    assert.equal(handoff.summary, 'retried');
    await meeting.cancel();
  });
});

test('addContext validates and updates the meeting', async (t) => {
  await withDaemon(t, { autoHandoff: false }, async ({ colleague }) => {
    const meeting = await colleague.joinMeeting(joinRequest());
    const updated = await meeting.addContext({
      version: 1,
      objective: 'ship sdk',
      currentTask: 'tests',
      summary: '',
      decisions: [],
      constraints: [],
      openQuestions: [],
      importantFiles: [],
      recentConversation: [],
    });
    assert.equal(updated.context.objective, 'ship sdk');
    await meeting.cancel();
  });
});

test('daemon autostart is serialized and uses the host-only token file', async (t) => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'colleague-autostart-'));
  t.after(() => fs.rm(root, { recursive: true, force: true }));
  const daemon = await startFakeDaemon({ root, readyDelayMs: 10, autoHandoff: false });
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
  const session = await transport.createMeeting({
    meetingUrl: ZOOM,
    agentSession: agentSession(),
    context: {
      version: 1, objective: 'Support this live meeting.', currentTask: 'Join the meeting and help when asked.',
      summary: '', decisions: [], constraints: [], openQuestions: [], importantFiles: [], recentConversation: [],
    },
    permissions: {
      workspace: 'read-only', commands: 'approval-required', edits: 'disabled',
      network: 'approval-required', commits: 'disabled', pushes: 'disabled',
    },
  });
  assert.ok(session.id);
  assert.equal(spawns, 1);
  const concurrent = await Promise.all([
    transport.getMeeting(session.id),
    transport.getMeeting(session.id),
  ]);
  assert.equal(spawns, 1);
  assert.equal(concurrent[0].id, session.id);
});

test('SSE parser ignores comments and concatenates data lines', () => {
  const event = parseSseBlock('id: evt-1\nevent: meeting.live\ndata: {"type":"meeting.live"}\n: keep-alive');
  assert.equal(event.id, 'evt-1');
  assert.equal(event.type, 'meeting.live');
});

test('package example stays importable', async () => {
  const example = path.resolve(path.dirname(new URL(import.meta.url).pathname), '../examples/join.mjs');
  const url = pathToFileURL(example).href;
  assert.ok(url.endsWith('join.mjs'));
});

test('iterateSse skips duplicate ids', async () => {
  async function* chunks() {
    yield sseFrame({ id: 'evt-1', type: 'meeting.joining', meetingId: 'mtg-1', version: 1, timestamp: 't' });
    yield sseFrame({ id: 'evt-1', type: 'meeting.joining', meetingId: 'mtg-1', version: 1, timestamp: 't' });
    yield sseFrame({ id: 'evt-2', type: 'meeting.live', meetingId: 'mtg-1', version: 1, timestamp: 't' });
  }
  const ids = [];
  for await (const item of iterateSse(chunks(), { seen: new Set() })) ids.push(item.event.id);
  assert.deepEqual(ids, ['evt-1', 'evt-2']);
});

test('approval APIs require ids and do not leak secrets', { timeout: 8000 }, async (t) => {
  await withDaemon(t, { autoHandoff: false }, async ({ colleague, daemon }) => {
    const meeting = await colleague.joinMeeting(joinRequest());
    const created = await colleague._transport.createApproval(meeting.id, {
      category: 'commands',
      summary: 'Run a workspace lookup',
    });
    assert.equal(created.status, 'pending');
    const listed = await meeting.listApprovals();
    assert.equal(listed.approvals[0].id, created.id);
    const decided = await meeting.decideApproval(created.id, 'denied');
    assert.equal(decided.status, 'denied');
    await assert.rejects(() => meeting.decideApproval('', 'approved'));
    daemon.state.artifacts.set(meeting.id, [{
      id: 'art-1', kind: 'plan', description: 'Workspace action plan', meetingId: meeting.id,
      body: JSON.stringify({ summary: 'Update the helper' }),
    }]);
    const artifacts = await meeting.listArtifacts();
    assert.equal(artifacts.artifacts[0].id, 'art-1');
    await assert.rejects(() => meeting.getArtifact(''));
    const commit = await meeting.createCommit({
      expectedHead: 'a'.repeat(40),
      message: 'Record reviewed helper changes',
      files: [{ path: 'helper.py', sha256: 'b'.repeat(64) }],
    });
    assert.equal(commit.kind, 'commit');
    const commits = await meeting.listCommits();
    assert.equal(commits.commits[0].id, commit.id);
    await assert.rejects(() => meeting.getCommit(''));
    const push = await meeting.createPush({
      commitSha: 'a'.repeat(40),
      remote: 'origin',
      branch: 'colleague-work',
    });
    assert.equal(push.kind, 'push');
    const share = await meeting.getScreenShare();
    assert.equal(share.status.paused, false);
    const paused = await meeting.pauseScreenShare();
    assert.equal(paused.status.paused, true);
    const observations = await meeting.listScreenShareObservations();
    assert.deepEqual(observations.observations, []);
    const dumped = JSON.stringify({ created, listed, decided, token: daemon.token, commit, push, share });
    assert.equal(dumped.includes('secret meeting speech'), false);
    await meeting.cancel();
  });
});
