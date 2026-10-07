import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';

import {
  buildMeetingBrief,
  contextHandoffFromSources,
  meetingBusy,
  meetingIsActive,
  phaseFromDaemon,
  readActiveMeeting,
  readActiveMeetingId,
  writeActiveMeetingId,
} from './meeting-contract.mjs';

test('builds the calls API brief from the link, objective, guidance, and camera', () => {
  const brief = buildMeetingBrief({
    meetingUrl: 'https://us05web.zoom.us/j/123',
    objective: ' Agree the launch date. ',
    meetingInstructions: 'Stay brief.',
    camera: { enabled: true, defaultOn: false, avatarDataUri: 'data:image/png;base64,AA==' },
  });
  assert.deepEqual(Object.keys(brief).sort(), ['camera', 'channel', 'context', 'objective', 'to']);
  assert.equal(brief.channel, 'meeting');
  assert.equal(brief.to, 'https://us05web.zoom.us/j/123');
  assert.equal(brief.objective, 'Agree the launch date.');
  assert.deepEqual(brief.context, { summary: 'Stay brief.' });
  assert.deepEqual(brief.camera, { enabled: true, defaultOn: false, avatarDataUri: 'data:image/png;base64,AA==' });
});

test('camera defaults on and onBehalfOf is sent only when the owner is known', () => {
  const plain = buildMeetingBrief({ meetingUrl: 'https://meet.google.com/aaa-bbbb-ccc' });
  assert.deepEqual(plain.camera, { enabled: true, defaultOn: true });
  assert.equal('onBehalfOf' in plain, false);
  assert.equal('context' in plain, false);
  assert.equal('onBehalfOf' in buildMeetingBrief({ meetingUrl: 'x' }, { onBehalfOf: '  ' }), false);
  const owned = buildMeetingBrief({ meetingUrl: 'x' }, { onBehalfOf: ' Sam Rivera ' });
  assert.equal(owned.onBehalfOf, 'Sam Rivera');
});

test('never forwards coding-agent fields from stale console input', () => {
  const payload = buildMeetingBrief({
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
  const stored = fs.readFileSync(path.join(projectRoot, '.smitline', 'portal-active.json'), 'utf8');
  assert.match(stored, /mtg-portal00000001/);
  assert.equal(fs.statSync(path.join(projectRoot, '.smitline', 'portal-active.json')).mode & 0o777, 0o600);
  assert.equal(fs.statSync(path.join(projectRoot, '.smitline')).mode & 0o777, 0o700);
  assert.equal(fs.existsSync(path.join(projectRoot, 'meeting-runtime', 'run', 'portal-active.json')), false);
  // A meeting started through the calls API also keeps its call and objective.
  writeActiveMeetingId(projectRoot, null, { callId: 'call-0123456789abcdef', objective: 'Agree the date.' });
  assert.deepEqual(readActiveMeeting(projectRoot), { callId: 'call-0123456789abcdef', objective: 'Agree the date.' });
});

test('context handoff keeps source text in recent conversation', () => {
  const handoff = contextHandoffFromSources([{ name: 'plan.md', text: 'Ship Friday.' }]);
  assert.equal(handoff.version, 1);
  assert.match(handoff.recentConversation[0].text, /Ship Friday/);
  assert.equal(handoff.objective, 'Support this live meeting from the local portal.');
  assert.equal(handoff.importantFiles[0], 'plan.md');
  // With the meeting's objective, the guidance moves into the summary instead of replacing it.
  const goal = contextHandoffFromSources([{ name: 'plan.md', text: 'Ship Friday.' }], { objective: 'Agree the date.', meetingInstructions: 'Stay brief.' });
  assert.equal(goal.objective, 'Agree the date.');
  assert.match(goal.summary, /^Guidance: Stay brief\.\n\nplan\.md: Ship Friday\./);
  assert.equal(contextHandoffFromSources([], { meetingInstructions: 'Stay brief.' }).objective, 'Stay brief.');
});
