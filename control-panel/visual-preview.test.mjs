import assert from 'node:assert/strict';
import test from 'node:test';
import { isPrivateVisualText, VISUAL_STATES } from './visual-preview.mjs';

test('preview states are explicit and never carry meeting secrets', () => {
  assert.deepEqual(VISUAL_STATES, [
    'joining', 'listening', 'working', 'speaking', 'finalizing', 'needs_attention', 'ended',
  ]);
  assert.equal(isPrivateVisualText('listening'), false);
  assert.equal(isPrivateVisualText('Checking project'), true);
  assert.equal(isPrivateVisualText('customer transcript from acme'), true);
});
