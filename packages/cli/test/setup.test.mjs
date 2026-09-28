import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import fs from 'node:fs/promises';
import { statSync } from 'node:fs';
import http from 'node:http';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import test from 'node:test';
import {
  parseEnv, readEnv, registerAgents, renderSecretsPage, sanitizeSubmission, serveSecretsPage,
  setupStatus, validateSetting, writeEnv,
} from '../src/setup.mjs';

const cli = fileURLToPath(new URL('../src/colleague.mjs', import.meta.url));

async function tempRoot(t) {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'colleague-setup-'));
  t.after(() => fs.rm(root, { recursive: true, force: true }));
  return root;
}

test('.env merge keeps other lines and writes 0600', async (t) => {
  const root = await tempRoot(t);
  await fs.writeFile(path.join(root, '.env'), '# keys\nOPENAI_API_KEY=replace_with_key\nPORT=8093\n');
  writeEnv(root, { OPENAI_API_KEY: 'sk-new', COLLEAGUE_OWNER_NAME: 'Robin' });
  const text = await fs.readFile(path.join(root, '.env'), 'utf8');
  assert.equal(text, '# keys\nOPENAI_API_KEY=sk-new\nPORT=8093\nCOLLEAGUE_OWNER_NAME=Robin\n');
  assert.equal(statSync(path.join(root, '.env')).mode & 0o777, 0o600);
  assert.deepEqual(parseEnv('export A="x y"\nB=\'z\'\n#C=1'), { A: 'x y', B: 'z' });
  assert.equal(readEnv(root).COLLEAGUE_OWNER_NAME, 'Robin');
});

test('settings are validated and secrets are refused', () => {
  assert.equal(validateSetting('COLLEAGUE_OWNER_PHONE', '+1 (415) 555-0142'), '+14155550142');
  assert.equal(validateSetting('COLLEAGUE_VOICE', 'quartz'), 'quartz');
  assert.throws(() => validateSetting('COLLEAGUE_VOICE', 'alloy'), /one of/);
  assert.throws(() => validateSetting('OPENAI_API_KEY', 'sk'), /secret/);
  assert.throws(() => validateSetting('COLLEAGUE_PUBLIC_URL', 'http://x'), /https/);
  assert.throws(() => validateSetting('PATH', '/bin'), /unknown setting/);
  assert.throws(() => validateSetting('COLLEAGUE_OWNER_NAME', 'a\nb'), /one line/);
});

test('secrets page submission is validated and never echoes secrets', async (t) => {
  const { updates, errors } = sanitizeSubmission(new URLSearchParams({
    OPENAI_API_KEY: 'sk-abc', COLLEAGUE_OWNER_PHONE: 'nope', TWILIO_AUTH_TOKEN: 'has space',
  }));
  assert.deepEqual(updates, { OPENAI_API_KEY: 'sk-abc' });
  assert.equal(errors.length, 2);
  const html = renderSecretsPage({ OPENAI_API_KEY: 'sk-secret-value', COLLEAGUE_OWNER_NAME: 'Robin' }, '/setup/x');
  assert.ok(!html.includes('sk-secret-value'));
  assert.match(html, /Saved: Robin/);

  const root = await tempRoot(t);
  let pageUrl;
  const done = serveSecretsPage({ root, onUrl(url) { pageUrl = url; } });
  while (!pageUrl) await new Promise((resolve) => setTimeout(resolve, 10));
  const wrong = await fetch(new URL('/setup/guess', pageUrl));
  assert.equal(wrong.status, 404);
  const page = await fetch(pageUrl);
  assert.match(await page.text(), /Colleague AI setup/);
  const saved = await fetch(pageUrl, {
    method: 'POST',
    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    body: new URLSearchParams({ OPENAI_API_KEY: 'sk-live', COLLEAGUE_OWNER_NAME: 'Robin' }).toString(),
  });
  assert.equal(saved.status, 200);
  assert.deepEqual(await done, { saved: ['OPENAI_API_KEY', 'COLLEAGUE_OWNER_NAME'] });
  assert.equal(readEnv(root).OPENAI_API_KEY, 'sk-live');
});

function fakeRunner(outcomes = {}) {
  return (binary, args) => {
    const key = `${binary} ${args.join(' ')}`;
    for (const [prefix, status] of Object.entries(outcomes)) {
      if (key.startsWith(prefix)) return { status, stdout: '', stderr: '' };
    }
    return { status: 1, stdout: '', stderr: 'not found' };
  };
}

test('status reports what to ask the user, and verifies keys when present', async (t) => {
  const root = await tempRoot(t);
  await fs.writeFile(path.join(root, 'start-runtime-daemon.sh'), '#!/bin/sh\n');
  await fs.mkdir(path.join(root, 'node_modules', 'mammoth'), { recursive: true });
  const runner = fakeRunner({ 'docker info': 0 });
  const find = (binary) => (binary === 'docker' ? '/usr/bin/docker' : null);
  const empty = await setupStatus({ root, env: {}, verify: false, runner, find });
  assert.equal(empty.ready, false);
  assert.deepEqual(empty.next.slice(0, 2).map((item) => item.id), ['openai_key', 'owner_name']);
  assert.match(empty.next[0].ask, /Do not paste it into this chat/);

  const requests = [];
  const fetchImpl = async (url) => {
    requests.push(String(url));
    if (String(url).includes('api.openai.com')) return new Response('{}', { status: 200 });
    if (String(url).endsWith('/Accounts/AC1.json')) return Response.json({ status: 'active', type: 'Trial' });
    if (String(url).includes('IncomingPhoneNumbers')) return Response.json({ incoming_phone_numbers: [{ phone_number: '+15005550006' }] });
    return Response.json({ outgoing_caller_ids: [{ phone_number: '+14155550100' }] });
  };
  await fs.writeFile(path.join(root, '.env'), [
    'OPENAI_API_KEY=sk-test', 'COLLEAGUE_OWNER_NAME=Robin', 'TWILIO_ACCOUNT_SID=AC1',
    'TWILIO_AUTH_TOKEN=tok', 'COLLEAGUE_CALLER_ID=+14155550100', 'COLLEAGUE_OWNER_PHONE=+14155550100',
  ].join('\n'));
  const full = await setupStatus({ root, env: {}, fetchImpl, runner, find });
  assert.equal(full.ready, true);
  assert.equal(full.phoneReady, true);
  assert.match(full.checks.find((c) => c.id === 'twilio').detail, /trial/);
  assert.ok(requests[0].endsWith('/v1/models/gpt-live-1'));

  const noAccess = await setupStatus({
    root, env: {}, runner, find, fetchImpl: async (url) => (String(url).includes('openai')
      ? new Response('{}', { status: 404 }) : fetchImpl(url)),
  });
  assert.equal(noAccess.ready, false);
  assert.match(noAccess.checks.find((c) => c.id === 'openai_key').detail, /paid API tier/);
});

test('register adds the stdio server to detected agents', async (t) => {
  const home = await tempRoot(t);
  await fs.mkdir(path.join(home, '.cursor'));
  const calls = [];
  const runner = (binary, args) => { calls.push([binary, ...args]); return { status: 0, stdout: '', stderr: '' }; };
  const find = (binary) => (binary === 'claude' ? '/usr/bin/claude' : null);
  const { results, manual } = registerAgents('/repo', { runner, find, home });
  assert.deepEqual(results.map((r) => r.id), ['claude-code', 'cursor']);
  const add = calls.find((c) => c[0] === 'claude' && c[2] === 'add');
  assert.deepEqual(add.slice(1, 6), ['mcp', 'add', '--scope', 'user', 'colleague-ai']);
  assert.equal(add.at(-1), '/repo/packages/mcp/src/server.mjs');
  const cursor = JSON.parse(await fs.readFile(path.join(home, '.cursor', 'mcp.json'), 'utf8'));
  assert.deepEqual(cursor.mcpServers['colleague-ai'].args, ['/repo/packages/mcp/src/server.mjs']);
  assert.equal(manual.args[0], '/repo/packages/mcp/src/server.mjs');
});

async function fakeCallsDaemon(root) {
  const token = 'fake-token';
  await fs.mkdir(path.join(root, '.colleague'), { recursive: true });
  await fs.writeFile(path.join(root, '.colleague', 'daemon.auth'), `${token}\n`);
  const seen = [];
  let polls = 0;
  const server = http.createServer(async (request, response) => {
    const chunks = [];
    for await (const chunk of request) chunks.push(chunk);
    const body = chunks.length ? JSON.parse(Buffer.concat(chunks).toString('utf8')) : null;
    seen.push({ method: request.method, url: request.url, body });
    const send = (status, payload) => {
      response.writeHead(status, { 'Content-Type': 'application/json' });
      response.end(JSON.stringify(payload));
    };
    if (request.headers.authorization !== `Bearer ${token}`) return send(401, { error: { code: 'unauthorized' } });
    if (request.url === '/v1/calls' && request.method === 'POST') {
      if (!body.objective) {
        return send(422, { error: { code: 'brief_incomplete', message: 'brief is missing objective', missing: [{ field: 'objective', question: 'What should the call achieve?' }] } });
      }
      return send(201, { id: 'call-0123456789abcdef', status: 'queued', brief: body });
    }
    if (request.url.startsWith('/v1/calls/call-0123456789abcdef/wait')) {
      polls += 1;
      return send(200, { id: 'call-0123456789abcdef', status: polls > 1 ? 'completed' : 'ringing', result: { outcome: 'achieved', summary: 'Booked.' } });
    }
    return send(404, { error: { code: 'not_found' } });
  });
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  return { port: server.address().port, seen, close: () => new Promise((resolve) => server.close(resolve)) };
}

function runCli(args, env = {}) {
  return new Promise((resolve) => {
    const child = spawn(process.execPath, [cli, ...args], { env: { ...process.env, ...env }, stdio: ['ignore', 'pipe', 'pipe'] });
    let stdout = '';
    let stderr = '';
    child.stdout.on('data', (chunk) => { stdout += chunk; });
    child.stderr.on('data', (chunk) => { stderr += chunk; });
    child.on('close', (code) => resolve({ code, stdout, stderr }));
  });
}

test('call builds a brief, waits for the result, and shows questions for missing fields', async (t) => {
  const root = await tempRoot(t);
  await fs.writeFile(path.join(root, '.env'), 'COLLEAGUE_OWNER_NAME=Robin\n');
  const daemon = await fakeCallsDaemon(root);
  t.after(daemon.close);
  const common = ['--root', root, '--port', String(daemon.port)];
  const done = await runCli(['call', ...common, '--to', '+14155550142', '--objective', 'Book a table',
    '--agree', '6:30pm; 7pm', '--never-share', 'card number', '--rehearsal', '--wait']);
  assert.equal(done.code, 0, done.stderr);
  assert.equal(JSON.parse(done.stdout).result.outcome, 'achieved');
  assert.match(done.stderr, /call ringing/);
  const brief = daemon.seen.find((item) => item.method === 'POST').body;
  assert.deepEqual(brief, {
    channel: 'phone', to: '+14155550142', onBehalfOf: 'Robin', objective: 'Book a table',
    mayAgreeTo: ['6:30pm', '7pm'], mustNotShare: ['card number'], rehearsal: true,
  });
  const missing = await runCli(['call', ...common, '--to', '+14155550142']);
  assert.equal(missing.code, 2);
  assert.match(missing.stderr, /missing objective: What should the call achieve\?/);
});
