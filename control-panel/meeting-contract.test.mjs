import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';

import {
  buildMeetingCreatePayload,
  contextHandoffFromSources,
  meetingBusy,
  meetingIsActive,
  permissionsForTools,
  phaseFromDaemon,
  readActiveMeetingId,
  writeActiveMeetingId,
} from './meeting-contract.mjs';

test('builds a local portal meeting payload with least-privilege permissions', () => {
  const payload = buildMeetingCreatePayload({
    meetingUrl: 'https://us05web.zoom.us/j/123',
    model: 'gpt-5.6-terra',
    workspace: '/Users/Taylor/project',
    meetingInstructions: 'Stay brief.',
    tools: { webSearch: true, codex: true, charts: false },
  }, { sources: [{ name: 'notes.txt', text: 'Launch Friday.' }] });
  assert.equal(payload.agentSession.provider, 'codex');
  assert.equal(payload.agentSession.sessionId, 'local-portal');
  assert.equal(payload.agentSession.metadata.source, 'local-portal');
  assert.equal(payload.permissions.workspace, 'read-only');
  assert.equal(payload.permissions.network, 'allowed');
  assert.equal(payload.permissions.edits, 'disabled');
  assert.equal(payload.context.objective, 'Stay brief.');
  assert.equal(payload.context.importantFiles[0], 'notes.txt');
  assert.equal(payload.context.recentConversation[0].role, 'user');
});

test('disables workspace and network when those tools are off', () => {
  assert.deepEqual(permissionsForTools({}), {
    workspace: 'none', commands: 'disabled', edits: 'disabled',
    network: 'disabled', commits: 'disabled', pushes: 'disabled',
  });
  const payload = buildMeetingCreatePayload({
    meetingUrl: 'https://teams.microsoft.com/l/meetup-join/abc',
    model: 'gpt-5.6-terra',
    tools: { webSearch: false, codex: false },
  }, { workspace: '/tmp/ws' });
  assert.equal(payload.agentSession.provider, 'generic');
  assert.equal(payload.permissions.workspace, 'none');
});

test('maps daemon meeting state onto existing product phases', () => {
  assert.equal(phaseFromDaemon({ session: { state: 'joining' } }), 'starting');
  assert.equal(phaseFromDaemon({
    session: { state: 'joining' }, health: { stage: 'waiting_for_admission' },
  }), 'waiting_for_admission');
  assert.equal(phaseFromDaemon({ session: { state: 'live' } }), 'live');
  assert.equal(phaseFromDaemon({ session: { state: 'ended' } }), 'meeting_ended');
  assert.equal(phaseFromDaemon({}), 'stopped');
  assert.equal(phaseFromDaemon({
    session: { state: 'joining' }, daemonError: new Error('down'),
  }), 'needs_attention');
  assert.equal(meetingIsActive({ state: 'joining' }), true);
  assert.equal(meetingBusy({ running: false, phase: 'joining' }), true);
  assert.equal(meetingBusy({ running: false, phase: 'stopped' }), false);
});

test('persists the active meeting id for portal restart recovery', () => {
  const projectRoot = fs.mkdtempSync(path.join(os.tmpdir(), 'portal-active-'));
  writeActiveMeetingId(projectRoot, 'mtg-portal00000001');
  assert.equal(readActiveMeetingId(projectRoot), 'mtg-portal00000001');
  const stored = fs.readFileSync(path.join(projectRoot, '.colleague', 'portal-active.json'), 'utf8');
  assert.match(stored, /mtg-portal00000001/);
  assert.equal(fs.statSync(path.join(projectRoot, '.colleague', 'portal-active.json')).mode & 0o777, 0o600);
  assert.equal(fs.statSync(path.join(projectRoot, '.colleague')).mode & 0o777, 0o700);
  assert.equal(fs.existsSync(path.join(projectRoot, 'meeting-runtime', 'run', 'portal-active.json')), false);
});

test('context handoff keeps source text in recent conversation', () => {
  const handoff = contextHandoffFromSources([{ name: 'plan.md', text: 'Ship Friday.' }]);
  assert.equal(handoff.version, 1);
  assert.match(handoff.recentConversation[0].text, /Ship Friday/);
});
