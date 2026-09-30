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
  phaseFromDaemon,
  readActiveMeetingId,
  writeActiveMeetingId,
} from './meeting-contract.mjs';

test('builds the meeting payload from the link, reference context, and camera', () => {
  const payload = buildMeetingCreatePayload({
    meetingUrl: 'https://us05web.zoom.us/j/123',
    meetingInstructions: 'Stay brief.',
    camera: { enabled: true, defaultOn: false, avatarDataUri: 'data:image/png;base64,AA==' },
  }, { sources: [{ name: 'notes.txt', text: 'Launch Friday.' }] });
  assert.deepEqual(Object.keys(payload).sort(), ['camera', 'context', 'meetingUrl']);
  assert.equal(payload.meetingUrl, 'https://us05web.zoom.us/j/123');
  assert.equal(payload.context.objective, 'Stay brief.');
  assert.equal(payload.context.importantFiles[0], 'notes.txt');
  assert.equal(payload.context.recentConversation[0].role, 'user');
  assert.deepEqual(payload.camera, { enabled: true, defaultOn: false, avatarDataUri: 'data:image/png;base64,AA==' });
});

test('camera defaults on and onBehalfOf is sent only when the owner is known', () => {
  const plain = buildMeetingCreatePayload({ meetingUrl: 'https://meet.google.com/aaa-bbbb-ccc' });
  assert.deepEqual(plain.camera, { enabled: true, defaultOn: true });
  assert.equal('onBehalfOf' in plain, false);
  assert.equal('onBehalfOf' in buildMeetingCreatePayload({ meetingUrl: 'x' }, { onBehalfOf: '  ' }), false);
  const owned = buildMeetingCreatePayload({ meetingUrl: 'x' }, { onBehalfOf: ' Sam Rivera ' });
  assert.equal(owned.onBehalfOf, 'Sam Rivera');
});

test('never forwards coding-agent fields from stale console input', () => {
  const payload = buildMeetingCreatePayload({
    meetingUrl: 'https://teams.microsoft.com/l/meetup-join/abc',
    model: 'gpt-5.5',
    workspace: '/tmp/ws',
    tools: { codex: true, webSearch: true },
    screenShare: { enabled: true },
  });
  for (const key of ['agentSession', 'permissions', 'screenShare', 'model', 'workspace', 'tools']) {
    assert.equal(key in payload, false, key);
  }
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
