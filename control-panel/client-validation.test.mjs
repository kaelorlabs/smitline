import assert from 'node:assert/strict';
import test from 'node:test';

import { clientMeetingErrors, createStartLock } from './client-validation.mjs';

test('client validation blocks empty or non-HTTPS required fields', () => {
  assert.deepEqual(clientMeetingErrors({ meetingUrl: '', participantName: '' }), {
    meetingUrl: 'Paste a Zoom, Teams, or Google Meet invitation.',
    objective: 'Say what this meeting should achieve.',
    participantName: 'Enter the name that should appear in the meeting.',
  });
  assert.equal(
    clientMeetingErrors({ meetingUrl: 'http://zoom.us/j/1', participantName: 'Smitline' }).meetingUrl,
    'Use a full https:// Zoom, Teams, or Google Meet invitation.',
  );
  assert.equal(
    clientMeetingErrors({ meetingUrl: 'not-a-url', participantName: 'Smitline' }).meetingUrl,
    'Paste a full https:// invitation URL.',
  );
  assert.deepEqual(
    clientMeetingErrors({
      meetingUrl: 'https://us05web.zoom.us/j/123456789',
      participantName: 'Smitline',
      objective: 'Take notes on the launch plan.',
    }),
    {},
  );
});

test('start lock ignores overlapping begins until the first start ends', () => {
  const lock = createStartLock();
  assert.equal(lock.begin(), true);
  assert.equal(lock.inFlight, true);
  assert.equal(lock.begin(), false);
  lock.end();
  assert.equal(lock.inFlight, false);
  assert.equal(lock.begin(), true);
});
