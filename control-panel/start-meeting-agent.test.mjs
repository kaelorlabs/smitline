import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';
import { fileURLToPath } from 'node:url';

const ROOT = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const SCRIPT = path.join(ROOT, 'start-meeting-agent.sh');
const SECRETS = ['tw-auth-secret', 'sip-password-secret', 'whsec_webhook-secret'];

// A data root like the image's, and a docker that records what compose would get: its
// arguments, the viewer token, and the one env file it would read.
function setup({ meetingEnv } = {}) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'smitline-start-meeting-'));
  const bin = path.join(dir, 'bin');
  const data = path.join(dir, 'data');
  fs.mkdirSync(bin);
  fs.mkdirSync(data, { mode: 0o700 });
  fs.writeFileSync(path.join(data, '.env'), [
    'OPENAI_API_KEY=sk-meeting-test-key',
    `TWILIO_AUTH_TOKEN=${SECRETS[0]}`,
    `SMITLINE_SIP_PASSWORD=${SECRETS[1]}`,
    `OPENAI_WEBHOOK_SECRET=${SECRETS[2]}`,
    'SMITLINE_OWNER_NAME=Robin',
    '',
  ].join('\n'), { mode: 0o600 });
  if (meetingEnv) fs.writeFileSync(path.join(data, '.env.meeting'), meetingEnv, { mode: 0o600 });
  const log = path.join(dir, 'docker.log');
  fs.writeFileSync(path.join(bin, 'docker'), `#!/bin/sh
{
  echo "ARGS $*"
  echo "VIEWER \${SMITLINE_VIEWER_TOKEN:-}"
  echo "ENV_FILE \${SMITLINE_MEETING_ENV_FILE:-}"
  if [ -n "\${SMITLINE_MEETING_ENV_FILE:-}" ]; then
    echo "ENV_MODE $(stat -c %a "$SMITLINE_MEETING_ENV_FILE")"
    sed 's/^/ENV /' "$SMITLINE_MEETING_ENV_FILE"
  fi
} >> "$DOCKER_LOG"
`, { mode: 0o755 });
  const env = {
    PATH: `${bin}:/usr/bin:/bin`,
    HOME: dir,
    DOCKER_LOG: log,
    SMITLINE_ROOT: data,
    SMITLINE_MEETING_DATA: path.join(data, 'meetings'),
    SMITLINE_MEETING_IMAGE: 'ghcr.io/kaelorlabs/smitline-meeting:test',
  };
  return { data, env, log: () => fs.readFileSync(log, 'utf8') };
}

test('the meeting launcher has valid shell syntax and never names .env for compose', () => {
  const syntax = spawnSync('bash', ['-n', SCRIPT], { encoding: 'utf8' });
  assert.equal(syntax.status, 0, syntax.stderr);
  assert.equal(fs.readFileSync(SCRIPT, 'utf8').includes('SMITLINE_MEETING_ENV_FILE="$DATA/.env'), false);
});

test('a sign-in gets no settings or keys, and its view a token kept for the console', () => {
  const run = setup();
  const result = spawnSync('bash', [SCRIPT], { encoding: 'utf8', env: { ...run.env, SMITLINE_AUTH_MODE: 'google' } });
  assert.equal(result.status, 0, result.stderr);
  const log = run.log();
  assert.match(log, /^ARGS compose -f \S+compose\.meeting\.image\.yaml up -d --pull missing meeting-agent$/m);
  assert.match(log, /^ENV_FILE $/m);
  assert.equal(log.includes('sk-meeting-test-key'), false);
  const token = /^VIEWER (\S+)$/m.exec(log)[1];
  assert.match(token, /^[A-Za-z0-9_-]{32,128}$/);
  const file = path.join(run.data, '.smitline', 'meeting-view-token');
  assert.equal(fs.readFileSync(file, 'utf8'), `${token}\n`);
  assert.equal(fs.statSync(file).mode & 0o777, 0o600);
  assert.equal(fs.statSync(path.dirname(file)).mode & 0o777, 0o700);
  // Not a terminal: the console reads the token from its file, so the output never has it.
  assert.equal(result.stdout.includes(token), false);
  // The folders the image mounts as subpaths of the volume exist before compose runs.
  for (const name of ['run', 'recordings', 'profiles']) {
    assert.ok(fs.statSync(path.join(run.data, 'meetings', name)).isDirectory(), name);
  }
});

test('a meeting started by hand gets only what the meeting reads, in a private file deleted after', () => {
  const run = setup({
    meetingEnv: 'MEETING_URL=https://zoom.us/j/1234567890\nMEETING_PASSCODE=98$76\nSMITLINE_PARTICIPANT_NAME=Robin notes\n',
  });
  const result = spawnSync('bash', [SCRIPT], { encoding: 'utf8', env: run.env });
  assert.equal(result.status, 0, result.stderr);
  const log = run.log();
  const envFile = /^ENV_FILE (\S+)$/m.exec(log)[1];
  assert.match(log, /^ENV_MODE 600$/m);
  assert.match(log, /^ENV OPENAI_API_KEY="sk-meeting-test-key"$/m);
  assert.match(log, /^ENV MEETING_URL="https:\/\/zoom\.us\/j\/1234567890"$/m);
  assert.match(log, /^ENV MEETING_PASSCODE="98\\\$76"$/m);
  assert.match(log, /^ENV SMITLINE_PARTICIPANT_NAME="Robin notes"$/m);
  for (const secret of SECRETS) assert.equal(log.includes(secret), false, secret);
  assert.equal(fs.existsSync(envFile), false);
  assert.equal(fs.existsSync(path.dirname(envFile)), false);
  assert.match(/^VIEWER (\S+)$/m.exec(log)[1], /^[A-Za-z0-9_-]{32,128}$/);
});
