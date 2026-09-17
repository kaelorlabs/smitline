import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import test from 'node:test';
import { startFakeDaemon } from '../../sdk-typescript/test/fake-daemon.mjs';
import { describeEvent, parseArgs } from '../src/colleague.mjs';

const cli = fileURLToPath(new URL('../src/colleague.mjs', import.meta.url));
const ZOOM = 'https://zoom.us/j/555111222';

function runColleague(args, { env = {}, input, sigintAfterJoin, cwd } = {}) {
  return new Promise((resolve) => {
    const child = spawn(process.execPath, [cli, ...args], {
      cwd,
      env: { ...process.env, ...env },
      stdio: ['pipe', 'pipe', 'pipe'],
    });
    let stdout = '';
    let stderr = '';
    let sentSigint = false;
    child.stdout.on('data', (chunk) => { stdout += chunk; });
    child.stderr.on('data', (chunk) => {
      stderr += chunk;
      if (sigintAfterJoin && !sentSigint && stderr.includes('joined ')) {
        sentSigint = true;
        setTimeout(() => child.kill('SIGINT'), 30);
      }
    });
    if (input) child.stdin.write(input);
    child.stdin.end();
    const timer = setTimeout(() => {
      child.kill('SIGKILL');
    }, 8000);
    child.on('close', (code, signal) => {
      clearTimeout(timer);
      resolve({ code, signal, stdout, stderr });
    });
  });
}

test('parseArgs and privacy-safe event rendering', () => {
  const args = parseArgs(['join', '--meeting', ZOOM, '--wait', '--thread', 'thread-1']);
  assert.equal(args.meeting, ZOOM);
  assert.equal(args.wait, true);
  assert.equal(parseArgs(['join', '--meeting', ZOOM, '--no-camera'])['no-camera'], true);
  assert.equal(parseArgs(['join', '--meeting', ZOOM, '--screen-share'])['screen-share'], true);
  assert.equal(describeEvent({ type: 'transcript.final', text: 'secret meeting speech' }), 'transcript transcript.final');
  assert.equal(describeEvent({ type: 'delegation.started', taskId: 'task-9' }), 'delegation started task-9');
  assert.ok(!describeEvent({ type: 'transcript.final', text: 'secret meeting speech' }).includes('secret'));
  assert.equal(describeEvent({ type: 'presence.updated', visualState: 'working' }), 'presence working');
  assert.equal(describeEvent({ type: 'screen_share.observation', summary: 'secret slide' }), 'screen-share observation');
});

test('join --wait prints final JSON, never transcript text, and uses exit 0', async (t) => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'colleague-cli-'));
  t.after(() => fs.rm(root, { recursive: true, force: true }));
  const daemon = await startFakeDaemon({ root, readyDelayMs: 25 });
  t.after(() => daemon.close());
  const result = await runColleague([
    'join',
    '--meeting', ZOOM,
    '--agent', 'codex',
    '--thread', 'thread-cli',
    '--workspace', root,
    '--wait',
    '--root', root,
    '--port', String(daemon.port),
  ], { cwd: root });
  assert.equal(result.code, 0, result.stderr);
  assert.ok(!result.stdout.includes('secret meeting speech'));
  assert.ok(!result.stderr.includes('secret meeting speech'));
  assert.ok(!result.stderr.includes(daemon.token));
  const payload = JSON.parse(result.stdout);
  assert.equal(payload.handoff.meetingId, 'mtg-1');
  assert.ok(result.stderr.includes('joined mtg-1'));
  const saved = JSON.parse(await fs.readFile(path.join(root, '.colleague', 'cli-meeting.json'), 'utf8'));
  assert.equal(saved.meetingId, 'mtg-1');
});

test('exact continuity without --thread is validation exit 2', async () => {
  const result = await runColleague([
    'join',
    '--meeting', ZOOM,
    '--agent', 'codex',
    '--workspace', '/tmp',
    '--wait',
  ]);
  assert.equal(result.code, 2);
  assert.match(result.stderr, /--thread/);
});

test('context continuity permits local-portal without a thread', async (t) => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'colleague-cli-ctx-'));
  t.after(() => fs.rm(root, { recursive: true, force: true }));
  const daemon = await startFakeDaemon({ root, readyDelayMs: 10 });
  t.after(() => daemon.close());
  const result = await runColleague([
    'join',
    '--meeting', ZOOM,
    '--agent', 'cursor',
    '--workspace', root,
    '--context-continuity',
    '--wait',
    '--root', root,
    '--port', String(daemon.port),
  ], { cwd: root });
  assert.equal(result.code, 0, result.stderr);
  const created = [...daemon.state.meetings.values()][0];
  assert.equal(created.agentSession.sessionId, 'local-portal');
  assert.equal(created.agentSession.provider, 'cursor');
});

test('startup failure is exit 3', async (t) => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'colleague-cli-start-'));
  t.after(() => fs.rm(root, { recursive: true, force: true }));
  const daemon = await startFakeDaemon({
    root,
    failCreate: { status: 503, code: 'supervisor_unavailable', message: 'supervisor unavailable' },
  });
  t.after(() => daemon.close());
  const result = await runColleague([
    'join',
    '--meeting', ZOOM,
    '--agent', 'codex',
    '--thread', 'thread-cli',
    '--workspace', root,
    '--wait',
    '--root', root,
    '--port', String(daemon.port),
  ], { cwd: root });
  assert.equal(result.code, 3, result.stderr);
});

test('partial handoff is exit 5', async (t) => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'colleague-cli-partial-'));
  t.after(() => fs.rm(root, { recursive: true, force: true }));
  const daemon = await startFakeDaemon({ root, readyDelayMs: 10, partial: true });
  t.after(() => daemon.close());
  const result = await runColleague([
    'join',
    '--meeting', ZOOM,
    '--agent', 'codex',
    '--thread', 'thread-cli',
    '--workspace', root,
    '--wait',
    '--root', root,
    '--port', String(daemon.port),
  ], { cwd: root });
  assert.equal(result.code, 5, result.stderr);
  assert.equal(JSON.parse(result.stdout).handoff.partial, true);
});

test('Ctrl-C requests cancellation, waits for handoff, and exits 130', async (t) => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'colleague-cli-int-'));
  t.after(() => fs.rm(root, { recursive: true, force: true }));
  const daemon = await startFakeDaemon({ root, autoHandoff: false, readyDelayMs: 5 });
  t.after(() => daemon.close());
  const result = await runColleague([
    'join',
    '--meeting', ZOOM,
    '--agent', 'codex',
    '--thread', 'thread-cli',
    '--workspace', root,
    '--wait',
    '--root', root,
    '--port', String(daemon.port),
  ], { cwd: root, sigintAfterJoin: true });
  assert.equal(result.code, 130, result.stderr + result.stdout);
  assert.ok(daemon.state.cancels >= 1);
  assert.match(result.stderr, /interrupt/);
  assert.ok(!result.stdout.includes(daemon.token));
});

test('status, cancel, context add, and handoff commands use the saved meeting id', async (t) => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'colleague-cli-cmds-'));
  t.after(() => fs.rm(root, { recursive: true, force: true }));
  const daemon = await startFakeDaemon({ root, autoHandoff: false, readyDelayMs: 5 });
  t.after(() => daemon.close());
  const joined = await runColleague([
    'join',
    '--meeting', ZOOM,
    '--agent', 'codex',
    '--thread', 'thread-cli',
    '--workspace', root,
    '--root', root,
    '--port', String(daemon.port),
  ], { cwd: root });
  assert.equal(joined.code, 0, joined.stderr);
  const status = await runColleague(['status', '--root', root, '--port', String(daemon.port)], { cwd: root });
  assert.equal(status.code, 0, status.stderr);
  assert.equal(JSON.parse(status.stdout).id, 'mtg-1');
  const contextPath = path.join(root, 'context.json');
  await fs.writeFile(contextPath, JSON.stringify({
    version: 1,
    objective: 'follow up',
    currentTask: 'notes',
    summary: '',
    decisions: [],
    constraints: [],
    openQuestions: [],
    importantFiles: [],
    recentConversation: [],
  }));
  const context = await runColleague([
    'context', 'add', '--file', contextPath, '--root', root, '--port', String(daemon.port),
  ], { cwd: root });
  assert.equal(context.code, 0, context.stderr);
  const cancel = await runColleague(['cancel', '--root', root, '--port', String(daemon.port)], { cwd: root });
  assert.equal(cancel.code, 0, cancel.stderr);
  const handoff = await runColleague(['handoff', 'get', '--root', root, '--port', String(daemon.port)], { cwd: root });
  assert.equal(handoff.code, 0, handoff.stderr);
  const retry = await runColleague(['handoff', 'retry', '--root', root, '--port', String(daemon.port)], { cwd: root });
  assert.equal(retry.code, 0, retry.stderr);
});

test('approvals commands require explicit meeting and approval ids', async (t) => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'colleague-cli-appr-'));
  t.after(() => fs.rm(root, { recursive: true, force: true }));
  const daemon = await startFakeDaemon({ root, autoHandoff: false, readyDelayMs: 5 });
  t.after(() => daemon.close());
  const joined = await runColleague([
    'join', '--meeting', ZOOM, '--agent', 'codex', '--thread', 'thread-cli',
    '--workspace', root, '--root', root, '--port', String(daemon.port),
  ], { cwd: root });
  assert.equal(joined.code, 0, joined.stderr);
  const missing = await runColleague(['approvals', 'list', '--root', root, '--port', String(daemon.port)], { cwd: root });
  assert.equal(missing.code, 2);
  const created = await fetch(`http://127.0.0.1:${daemon.port}/v1/meetings/mtg-1/approvals`, {
    method: 'POST',
    headers: { Authorization: `Bearer ${daemon.token}`, 'Content-Type': 'application/json' },
    body: JSON.stringify({ category: 'commands', summary: 'Run a workspace lookup' }),
  });
  const approval = await created.json();
  const listed = await runColleague([
    'approvals', 'list', '--meeting-id', 'mtg-1', '--root', root, '--port', String(daemon.port),
  ], { cwd: root });
  assert.equal(listed.code, 0, listed.stderr);
  assert.equal(JSON.parse(listed.stdout).approvals[0].id, approval.id);
  const decided = await runColleague([
    'approvals', 'decide', '--meeting-id', 'mtg-1', '--approval-id', approval.id,
    '--decision', 'denied', '--root', root, '--port', String(daemon.port),
  ], { cwd: root });
  assert.equal(decided.code, 0, decided.stderr);
  assert.equal(JSON.parse(decided.stdout).status, 'denied');
  assert.ok(!listed.stdout.includes(daemon.token));
  daemon.state.artifacts.set('mtg-1', [{
    id: 'art-1', kind: 'plan', description: 'Workspace action plan', meetingId: 'mtg-1',
  }]);
  const missingArtifacts = await runColleague(['artifacts', 'list', '--root', root, '--port', String(daemon.port)], { cwd: root });
  assert.equal(missingArtifacts.code, 2);
  const artifacts = await runColleague([
    'artifacts', 'list', '--meeting-id', 'mtg-1', '--root', root, '--port', String(daemon.port),
  ], { cwd: root });
  assert.equal(artifacts.code, 0, artifacts.stderr);
  assert.equal(JSON.parse(artifacts.stdout).artifacts[0].id, 'art-1');
  const missingCommits = await runColleague(['commits', 'list', '--root', root, '--port', String(daemon.port)], { cwd: root });
  assert.equal(missingCommits.code, 2);
  const createdCommit = await runColleague([
    'commits', 'create', '--meeting-id', 'mtg-1', '--expected-head', 'a'.repeat(40),
    '--message', 'Record reviewed helper changes', '--file', `helper.py:${'b'.repeat(64)}`,
    '--root', root, '--port', String(daemon.port),
  ], { cwd: root });
  assert.equal(createdCommit.code, 0, createdCommit.stderr);
  assert.equal(JSON.parse(createdCommit.stdout).kind, 'commit');
  const listedCommits = await runColleague([
    'commits', 'list', '--meeting-id', 'mtg-1', '--root', root, '--port', String(daemon.port),
  ], { cwd: root });
  assert.equal(JSON.parse(listedCommits.stdout).commits[0].kind, 'commit');
  const createdPush = await runColleague([
    'pushes', 'create', '--meeting-id', 'mtg-1', '--commit-sha', 'a'.repeat(40),
    '--remote', 'origin', '--branch', 'colleague-work',
    '--root', root, '--port', String(daemon.port),
  ], { cwd: root });
  assert.equal(createdPush.code, 0, createdPush.stderr);
  assert.equal(JSON.parse(createdPush.stdout).kind, 'push');
  const missingShare = await runColleague(['screen-share', 'status', '--root', root, '--port', String(daemon.port)], { cwd: root });
  assert.equal(missingShare.code, 2);
  const share = await runColleague([
    'screen-share', 'status', '--meeting-id', 'mtg-1', '--root', root, '--port', String(daemon.port),
  ], { cwd: root });
  assert.equal(share.code, 0, share.stderr);
  assert.equal(JSON.parse(share.stdout).status.paused, false);
});
