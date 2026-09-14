import assert from 'node:assert/strict';
import test from 'node:test';
import { parseEnv, publicSettings, serializeSettings, validateSettings } from './config.mjs';

const valid = {
  meetingUrl: 'https://us05web.zoom.us/j/123456789?pwd=opaque',
  participantName: 'Colleague AI', model: 'gpt-5.6-terra',
  workspace: '/work/product', meetingInstructions: 'Focus on release readiness.',
  tools: { webSearch: true, codex: true, charts: false },
};

test('validates a product meeting configuration', () => {
  assert.deepEqual(validateSettings(valid, { isDirectory: value => value === '/work/product' }), { valid: true, errors: {} });
});

test('rejects unsafe or contradictory meeting settings', () => {
  const result = validateSettings({ ...valid, meetingUrl: 'http://example.com/x', participantName: '', tools: { codex: false, charts: true } });
  assert.equal(result.valid, false);
  assert.ok(result.errors.meetingUrl);
  assert.ok(result.errors.participantName);
  assert.ok(result.errors.charts);
});

test('bounds operator-provided meeting instructions', () => {
  const result = validateSettings({ ...valid, meetingInstructions: 'x'.repeat(2001) }, { isDirectory: () => true });
  assert.equal(result.valid, false);
  assert.match(result.errors.meetingInstructions, /2,000/);
});

test('serializes only runtime settings and preserves a hidden passcode', () => {
  const text = serializeSettings({ ...valid, keepPasscode: true }, { ZOOM_PASSCODE: 'existing' });
  assert.equal(parseEnv(text).ZOOM_PASSCODE, 'existing');
  assert.ok(!text.includes('OPENAI_API_KEY'));
  assert.equal(publicSettings(parseEnv(text)).tools.webSearch, true);
});
