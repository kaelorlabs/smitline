import assert from 'node:assert/strict';
import test from 'node:test';
import { ownerName, parseEnv, publicSettings, serializeSettings, validateSettings } from './config.mjs';

const valid = {
  meetingUrl: 'https://us05web.zoom.us/j/123456789?pwd=opaque',
  participantName: 'Colleague AI',
  meetingInstructions: 'Focus on release readiness.',
};

test('validates a product meeting configuration', () => {
  assert.deepEqual(validateSettings(valid), { valid: true, errors: {} });
});

test('ignores stale coding-agent settings', () => {
  const result = validateSettings({
    ...valid,
    model: 'retired-model', workspace: 'relative/path',
    tools: { codex: true, cursor: true, charts: true },
  });
  assert.deepEqual(result, { valid: true, errors: {} });
});

test('rejects unsafe meeting settings', () => {
  const result = validateSettings({ ...valid, meetingUrl: 'http://example.com/x', participantName: '' });
  assert.equal(result.valid, false);
  assert.ok(result.errors.meetingUrl);
  assert.ok(result.errors.participantName);
});

test('bounds operator-provided meeting instructions', () => {
  const result = validateSettings({ ...valid, meetingInstructions: 'x'.repeat(2001) });
  assert.equal(result.valid, false);
  assert.match(result.errors.meetingInstructions, /2,000/);
});

test('serializes only runtime settings and preserves a hidden passcode', () => {
  const text = serializeSettings({ ...valid, keepPasscode: true }, { MEETING_PASSCODE: 'existing' });
  assert.equal(parseEnv(text).MEETING_PASSCODE, 'existing');
  assert.ok(!text.includes('OPENAI_API_KEY'));
  assert.deepEqual(publicSettings(parseEnv(text)), {
    platform: 'zoom', meetingUrl: valid.meetingUrl, hasPasscode: true,
    participantName: 'Colleague AI', meetingInstructions: 'Focus on release readiness.',
  });
  assert.equal(/CODEX|WORKSPACE|ENABLE_/.test(text), false);
  assert.equal(text.includes('COLLEAGUE_CAMERA'), false);
  assert.equal(text.includes('avatar'), false);
});

test('reads the owner name for onBehalfOf and drops unusable values', () => {
  assert.equal(ownerName(parseEnv('COLLEAGUE_OWNER_NAME="Sam  Rivera"\n')), 'Sam Rivera');
  assert.equal(ownerName({}), '');
  assert.equal(ownerName({ COLLEAGUE_OWNER_NAME: '   ' }), '');
  assert.equal(ownerName({ COLLEAGUE_OWNER_NAME: 'x'.repeat(81) }), '');
});

test('Teams invites validate while lookalike hosts and credentials fail', () => {
  for (const meetingUrl of ['https://teams.microsoft.com/l/meetup-join/abc', 'https://teams.live.com/meet/123?p=secret']) {
    assert.equal(validateSettings({ ...valid, meetingUrl }).valid, true);
  }
  for (const meetingUrl of ['https://teams.microsoft.com.evil.org/meet/123', 'https://user:pass@zoom.us/j/123', 'https://zoom.us:8443/j/123', 'https://zoom.us/j/123/extra']) {
    assert.equal(validateSettings({ ...valid, meetingUrl }).valid, false);
  }
});

test('Google Meet invites validate while malformed codes and lookalikes fail', () => {
  for (const meetingUrl of ['https://meet.google.com/aaa-bbbb-ccc', 'https://meet.google.com/abc-defg-hij?authuser=0']) {
    assert.equal(validateSettings({ ...valid, meetingUrl }).valid, true);
  }
  for (const meetingUrl of [
    'https://meet.google.com/abc',
    'https://meet.google.com/landing',
    'https://meet.google.com.evil.org/aaa-bbbb-ccc',
    'https://www.meet.google.com/aaa-bbbb-ccc',
    'http://meet.google.com/aaa-bbbb-ccc',
  ]) {
    assert.equal(validateSettings({ ...valid, meetingUrl }).valid, false);
  }
});
