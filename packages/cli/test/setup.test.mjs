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
  availableVoices, containerRegistration, createSignalWireTrunk, localMcpConnection, missingForDone, parseEnv, readEnv, registerAgents, renderSecretsPage, sanitizeSubmission,
  serveSecretsPage, setupStatus, validateSetting, windowsProfile, writeEnv,
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

  // A key assigned twice: readers take the last one, so repeats must go.
  await fs.writeFile(path.join(root, '.env'), 'COLLEAGUE_VOICE=marin\nPORT=1\nCOLLEAGUE_VOICE=quartz\n');
  writeEnv(root, { COLLEAGUE_VOICE: 'cinder' });
  assert.equal(await fs.readFile(path.join(root, '.env'), 'utf8'), 'COLLEAGUE_VOICE=cinder\nPORT=1\n');
  assert.equal(readEnv(root).COLLEAGUE_VOICE, 'cinder');
});

test('settings are validated and secrets are refused', () => {
  assert.equal(validateSetting('COLLEAGUE_OWNER_PHONE', '+1 (415) 555-0142'), '+14155550142');
  assert.equal(validateSetting('COLLEAGUE_VOICE', 'quartz'), 'quartz');
  assert.throws(() => validateSetting('COLLEAGUE_VOICE', 'alloy'), /one of/);
  assert.throws(() => validateSetting('OPENAI_API_KEY', 'sk'), /secret/);
  assert.throws(() => validateSetting('COLLEAGUE_PUBLIC_URL', 'http://x'), /https/);
  assert.throws(() => validateSetting('PATH', '/bin'), /unknown setting/);
  assert.throws(() => validateSetting('COLLEAGUE_OWNER_NAME', 'a\nb'), /one line/);
  // Voices OpenAI adds later can be allowed without a release.
  const env = { COLLEAGUE_EXTRA_VOICES: 'aurora, bad name,nova' };
  assert.deepEqual(availableVoices(env).slice(-2), ['aurora', 'nova']);
  assert.equal(validateSetting('COLLEAGUE_VOICE', 'aurora', { env }), 'aurora');
  assert.throws(() => validateSetting('COLLEAGUE_VOICE', 'aurora', { env: {} }), /one of/);
  assert.equal(validateSetting('COLLEAGUE_EXTRA_VOICES', 'aurora, nova'), 'aurora,nova');
  assert.throws(() => validateSetting('COLLEAGUE_EXTRA_VOICES', 'Aurora!'), /voice names/);
});

test('connector settings take an https origin and a passphrase of at least 12 characters', () => {
  assert.equal(validateSetting('COLLEAGUE_CONNECTOR_URL', 'https://colleague.example.com/'), 'https://colleague.example.com');
  assert.throws(() => validateSetting('COLLEAGUE_CONNECTOR_URL', 'http://colleague.example.com'), /https/);
  assert.throws(() => validateSetting('COLLEAGUE_CONNECTOR_URL', 'https://colleague.example.com/mcp'), /no path/);
  assert.throws(() => validateSetting('COLLEAGUE_CONNECTOR_PASSPHRASE', 'correct horse battery staple'), /secret/);
  const passphrase = 'correct horse battery staple';
  assert.deepEqual(sanitizeSubmission(new URLSearchParams({ COLLEAGUE_CONNECTOR_PASSPHRASE: passphrase })),
    { updates: { COLLEAGUE_CONNECTOR_PASSPHRASE: passphrase }, errors: [] });
  assert.match(sanitizeSubmission(new URLSearchParams({ COLLEAGUE_CONNECTOR_PASSPHRASE: 'too short' })).errors[0], /at least 12/);
  const html = renderSecretsPage({ COLLEAGUE_CONNECTOR_PASSPHRASE: passphrase }, '/setup/x');
  assert.match(html, /Remote connector \(server mode\)/);
  assert.ok(!html.includes(passphrase));
});

test('secrets page submission is validated and never echoes secrets', async (t) => {
  const { updates, errors } = sanitizeSubmission(new URLSearchParams({
    OPENAI_API_KEY: 'sk-abc', COLLEAGUE_OWNER_PHONE: 'nope', TWILIO_AUTH_TOKEN: 'has space',
  }));
  // Whitespace pasted into a key (a wrapped display adds line breaks) is dropped, not rejected.
  assert.deepEqual(updates, { OPENAI_API_KEY: 'sk-abc', TWILIO_AUTH_TOKEN: 'hasspace' });
  assert.equal(errors.length, 1);
  const pasted = sanitizeSubmission(new URLSearchParams({
    SIGNALWIRE_API_TOKEN: ' SWAPI-abc\n def\u200b ', COLLEAGUE_CONNECTOR_PASSPHRASE: 'correct horse\nbattery staple',
  }));
  assert.deepEqual(pasted.updates, { SIGNALWIRE_API_TOKEN: 'SWAPI-abcdef' });
  assert.match(pasted.errors[0], /must be one line/);
  const html = renderSecretsPage({ OPENAI_API_KEY: 'sk-secret-value', COLLEAGUE_OWNER_NAME: 'Robin' }, '/setup/x');
  assert.ok(!html.includes('sk-secret-value'));
  assert.match(html, /Saved: Robin/);

  const root = await tempRoot(t);
  let pageUrl;
  const savedEvents = [];
  const done = serveSecretsPage({ root, onUrl(url) { pageUrl = url; }, onSaved(keys) { savedEvents.push(keys); } });
  while (!pageUrl) await new Promise((resolve) => setTimeout(resolve, 10));
  const wrong = await fetch(new URL('/setup/guess', pageUrl));
  assert.equal(wrong.status, 404);
  const page = await fetch(pageUrl);
  const first = await page.text();
  assert.match(first, /Colleague AI setup/);
  assert.match(first, /name="action" value="done"/);

  // Each save keeps the page open for more.
  const saved = await postForm(pageUrl, { OPENAI_API_KEY: 'sk-live', COLLEAGUE_OWNER_NAME: 'Robin' });
  assert.equal(saved.status, 200);
  const afterSave = await saved.text();
  assert.match(afterSave, /Saved on this computer/);
  assert.match(afterSave, /OpenAI API key and Your name/);
  assert.ok(!afterSave.includes('sk-live'));
  assert.equal(readEnv(root).OPENAI_API_KEY, 'sk-live');

  // A field that needs a fix does not throw away the valid ones next to it.
  const partial = await postForm(pageUrl, { TWILIO_ACCOUNT_SID: 'AC-secret-sid', COLLEAGUE_OWNER_PHONE: 'nope' });
  assert.equal(partial.status, 422);
  const partialHtml = await partial.text();
  assert.match(partialHtml, /aria-invalid="true"/);
  assert.match(partialHtml, /Your phone number must include the country code/);
  assert.ok(!partialHtml.includes('AC-secret-sid'));
  assert.equal(readEnv(root).TWILIO_ACCOUNT_SID, 'AC-secret-sid');

  // Done refuses to finish while a phone provider is half set up, and keeps what was typed.
  const refused = await postForm(pageUrl, { OPENAI_API_KEY: 'sk-newer', COLLEAGUE_OWNER_PHONE: '+14155550142', action: 'done' });
  assert.equal(refused.status, 422);
  assert.match(await refused.text(), /Twilio Auth Token is required for Twilio phone calls/);
  assert.equal(readEnv(root).OPENAI_API_KEY, 'sk-newer');
  // Done saves what was typed, shows the closing page, and reports every key once.
  const finished = await postForm(pageUrl, { TWILIO_AUTH_TOKEN: 'tok\n en', action: 'done' });
  assert.equal(finished.status, 200);
  assert.match(await finished.text(), /All set/);
  assert.deepEqual(await done, { saved: ['OPENAI_API_KEY', 'COLLEAGUE_OWNER_NAME', 'TWILIO_ACCOUNT_SID', 'COLLEAGUE_OWNER_PHONE', 'TWILIO_AUTH_TOKEN'] });
  assert.deepEqual(savedEvents, [['OPENAI_API_KEY', 'COLLEAGUE_OWNER_NAME'], ['TWILIO_ACCOUNT_SID'], ['OPENAI_API_KEY', 'COLLEAGUE_OWNER_PHONE'], ['TWILIO_AUTH_TOKEN']]);
  assert.equal(readEnv(root).TWILIO_AUTH_TOKEN, 'token');
  const closed = await fetch(pageUrl).then((response) => response.status, () => 'closed');
  assert.ok([410, 'closed'].includes(closed), String(closed));
});

function postForm(url, fields) {
  return fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    body: new URLSearchParams(fields).toString(),
  });
}

async function servedUrl(options) {
  let pageUrl;
  const done = serveSecretsPage({ ...options, onUrl(url) { pageUrl = url; } });
  while (!pageUrl) await new Promise((resolve) => setTimeout(resolve, 10));
  return { done, pageUrl };
}

test('the secrets page times out: with saves it resolves, without it rejects', async (t) => {
  const idle = await servedUrl({ root: await tempRoot(t), timeoutMs: 200 });
  await assert.rejects(idle.done, /timed out/);

  const root = await tempRoot(t);
  const used = await servedUrl({ root, timeoutMs: 600 });
  assert.equal((await postForm(used.pageUrl, { COLLEAGUE_OWNER_NAME: 'Robin' })).status, 200);
  assert.deepEqual(await used.done, { saved: ['COLLEAGUE_OWNER_NAME'], timedOut: true });
});

test('the secrets page explains saved fields, errors, and the finish without echoing values', () => {
  const saved = { OPENAI_API_KEY: 'sk-secret-value', TWILIO_AUTH_TOKEN: 'token-secret' };
  const errors = ['COLLEAGUE_OWNER_PHONE must be an E.164 number such as +14155550142'];
  const html = renderSecretsPage(saved, '/setup/x', '', { savedNow: ['OPENAI_API_KEY'], errors, done: true });
  assert.ok(!html.includes('sk-secret-value') && !html.includes('token-secret'));
  assert.match(html, /<input id="COLLEAGUE_OWNER_PHONE"[^>]*aria-invalid="true"/);
  assert.match(html, /<a href="#COLLEAGUE_OWNER_PHONE">/);
  assert.match(html, /Show my own number/);
  const finished = renderSecretsPage(saved, '/setup/x', '', { finished: true });
  assert.match(finished, /still missing/);
  assert.match(finished, /Your name/);
  assert.ok(!finished.includes('<form'));
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
  const runner = fakeRunner({ 'docker info': 0, 'python3 -c': 0 });
  const find = (binary) => (binary === 'docker' ? '/usr/bin/docker' : null);
  const empty = await setupStatus({ root, env: {}, verify: false, runner, find });
  assert.equal(empty.ready, false);
  assert.equal(empty.firstCallReady, false);
  assert.deepEqual(empty.next.slice(0, 2).map((item) => item.id), ['openai_key', 'owner_name']);
  // Until the user opts into phone calls, only that question is a next step.
  assert.ok(empty.next.some((item) => item.id === 'phone_account'));
  assert.ok(!empty.next.some((item) => ['caller_id', 'owner_phone', 'public_url'].includes(item.id)));
  assert.deepEqual(empty.optional.map((item) => item.id), ['caller_id', 'owner_phone']);

  assert.match(empty.checks.find((c) => c.id === 'runtime').detail, /^Python on this computer/);
  // Without a usable Python, Docker runs Colleague AI; without either, that comes first.
  const noVenv = await setupStatus({ root, env: {}, verify: false, runner: fakeRunner({ 'docker info': 0 }), find });
  const runtime = noVenv.checks.find((c) => c.id === 'runtime');
  assert.equal(runtime.ok, true);
  assert.match(runtime.detail, /^Docker/);
  const neither = await setupStatus({ root, env: {}, verify: false, runner: fakeRunner({}), find });
  assert.equal(neither.checks.find((c) => c.id === 'runtime').ok, false);
  assert.equal(neither.next[0].id, 'runtime');
  assert.match(neither.next[0].ask, /Docker/);
  assert.match(empty.next[0].ask, /Do not paste it into this chat/);

  const requests = [];
  const fetchImpl = async (url) => {
    requests.push(String(url));
    if (String(url).includes('api.openai.com')) return new Response('{}', { status: 200 });
    if (String(url).endsWith('/Accounts/AC1.json')) return Response.json({ status: 'active', type: 'Full' });
    if (String(url).includes('IncomingPhoneNumbers')) return Response.json({ incoming_phone_numbers: [{ phone_number: '+15005550006' }] });
    return Response.json({ outgoing_caller_ids: [{ phone_number: '+14155550100' }] });
  };
  const envLines = [
    'OPENAI_API_KEY=sk-test', 'COLLEAGUE_OWNER_NAME=Robin', 'TWILIO_ACCOUNT_SID=AC1',
    'TWILIO_AUTH_TOKEN=tok', 'COLLEAGUE_OWNER_PHONE=+14155550100',
  ];
  // No number chosen yet.
  await fs.writeFile(path.join(root, '.env'), envLines.join('\n'));
  const withoutFrom = await setupStatus({ root, env: {}, fetchImpl, runner, find });
  assert.equal(withoutFrom.phoneReady, false);
  // One number in the account: no question, the agent sets it.
  const callerStep = withoutFrom.next.find((item) => item.id === 'caller_id');
  assert.deepEqual(callerStep.suggest, { key: 'TWILIO_FROM_NUMBER', value: '+15005550006' });
  assert.equal(callerStep.ask, undefined);
  // A verified mobile alone is enough for outgoing calls.
  await fs.writeFile(path.join(root, '.env'), [...envLines, 'COLLEAGUE_CALLER_ID=+14155550100'].join('\n'));
  const callerOnly = await setupStatus({ root, env: {}, fetchImpl, runner, find });
  assert.equal(callerOnly.phoneReady, true);
  assert.match(callerOnly.checks.find((c) => c.id === 'caller_id').detail, /incoming calls also need TWILIO_FROM_NUMBER/);
  await fs.writeFile(path.join(root, '.env'), [...envLines, 'TWILIO_FROM_NUMBER=+15005550006'].join('\n'));
  const full = await setupStatus({ root, env: {}, fetchImpl, runner, find });
  assert.equal(full.ready, true);
  assert.equal(full.phoneReady, true);
  assert.equal(full.firstCallReady, full.checks.find((c) => c.id === 'owner_phone').ok === true);
  const offline = await setupStatus({ root, env: {}, fetchImpl, runner, find, verify: false });
  assert.equal(offline.checks.find((c) => c.id === 'caller_id').ok, true);
  // Twilio's trial strips <Stream>, so it cannot carry a Colleague AI call.
  const twilioTrial = await setupStatus({
    root, env: {}, runner, find, fetchImpl: async (url) => (String(url).endsWith('/Accounts/AC1.json')
      ? Response.json({ status: 'active', type: 'Trial' }) : fetchImpl(url)),
  });
  const trialAccount = twilioTrial.checks.find((c) => c.id === 'phone_account');
  assert.equal(trialAccount.ok, false);
  assert.match(trialAccount.detail, /free trial blocks the live call audio/);
  assert.equal(twilioTrial.phoneReady, false);
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

test('register from WSL also adds Windows apps and installs the call skills', async (t) => {
  const repo = await tempRoot(t);
  for (const name of ['call-with-colleague-ai', 'setup-colleague-ai']) {
    await fs.mkdir(path.join(repo, '.agents', 'skills', name), { recursive: true });
    await fs.writeFile(path.join(repo, '.agents', 'skills', name, 'SKILL.md'), `# ${name}\n`);
  }
  const home = await tempRoot(t);
  await fs.mkdir(path.join(home, '.claude'));
  const userProfile = await tempRoot(t);
  const appData = path.join(userProfile, 'AppData', 'Roaming');
  await fs.mkdir(path.join(appData, 'Claude'), { recursive: true });
  await fs.writeFile(path.join(appData, 'Claude', 'claude_desktop_config.json'), JSON.stringify({ mcpServers: { other: { command: 'x' } } }));
  await fs.mkdir(path.join(userProfile, '.claude'));
  const calls = [];
  const runner = (binary, args) => { calls.push([binary, ...args]); return { status: 0, stdout: '', stderr: '' }; };
  const { results, skills, manual } = registerAgents(repo, {
    runner, find: () => null, home, windows: { userProfile, appData, distro: 'Ubuntu' },
  });
  const server = path.join(repo, 'packages', 'mcp', 'src', 'server.mjs');
  const desktop = JSON.parse(await fs.readFile(path.join(appData, 'Claude', 'claude_desktop_config.json'), 'utf8'));
  assert.deepEqual(desktop.mcpServers.other, { command: 'x' });
  assert.deepEqual(desktop.mcpServers['colleague-ai'], {
    command: 'wsl.exe', args: ['-d', 'Ubuntu', '--exec', process.execPath, server],
  });
  assert.ok(results.some((r) => r.id === 'claude-desktop' && r.registered));
  const windowsClaude = calls.find((c) => c[0] === 'cmd.exe' && c.includes('add'));
  assert.deepEqual(windowsClaude.slice(-6), ['wsl.exe', '-d', 'Ubuntu', '--exec', process.execPath, server]);
  assert.deepEqual(skills.map((s) => s.id), ['claude', 'claude-windows']);
  assert.deepEqual(skills[0].installed, ['call-with-colleague-ai']);
  await fs.access(path.join(home, '.claude', 'skills', 'call-with-colleague-ai', 'SKILL.md'));
  await fs.access(path.join(userProfile, '.claude', 'skills', 'call-with-colleague-ai', 'SKILL.md'));
  assert.equal(manual.windows.command, 'wsl.exe');

  // Outside WSL there is no Windows side.
  assert.equal(windowsProfile({ runner, find: () => null }), null);
  const translated = windowsProfile({
    find: () => '/bin/x',
    runner: (binary, args) => (binary === 'cmd.exe'
      ? { status: 0, stdout: args[2] === '%USERPROFILE%' ? 'C:\\Users\\sam\r\n' : 'C:\\Users\\sam\\AppData\\Roaming\r\n' }
      : { status: 0, stdout: `/mnt/c/${args[1].slice(3).replaceAll('\\', '/')}\n` }),
  });
  assert.equal(translated.userProfile, '/mnt/c/Users/sam');
  assert.equal(translated.appData, '/mnt/c/Users/sam/AppData/Roaming');
});

test('setup secrets starts the page in the background and prints its address', async (t) => {
  const root = await tempRoot(t);
  const started = await runCli(['setup', 'secrets', '--no-open', '--root', root], { COLLEAGUE_SETUP_PAGE_TIMEOUT_MS: '5000' });
  assert.equal(started.code, 0, started.stderr);
  const { url, opened, next } = JSON.parse(started.stdout);
  assert.equal(opened, false);
  assert.match(url, /^http:\/\/127\.0\.0\.1:\d+\/setup\//);
  assert.match(next, /press Done/);
  // The command has returned, and the page is still being served.
  const page = await fetch(url);
  assert.equal(page.status, 200);
  assert.match(page.headers.get('content-security-policy'), /default-src 'none'/);
  assert.doesNotMatch(await page.text(), /Tavily/);
  const saved = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    body: new URLSearchParams({ OPENAI_API_KEY: 'sk-test', COLLEAGUE_OWNER_NAME: 'Robin', action: 'done' }).toString(),
  });
  assert.equal(saved.status, 200);
  assert.equal(readEnv(root).COLLEAGUE_OWNER_NAME, 'Robin');
});

test('a voice preview calls the owner in that voice without saving it', async (t) => {
  const root = await tempRoot(t);
  await fs.writeFile(path.join(root, '.env'), 'COLLEAGUE_OWNER_NAME=Robin\nCOLLEAGUE_OWNER_PHONE=+14155550100\n');
  const daemon = await fakeCallsDaemon(root);
  t.after(daemon.close);
  const preview = await runCli(['setup', 'voice', '--preview', 'cinder', '--root', root, '--port', String(daemon.port)]);
  assert.equal(preview.code, 0, preview.stderr);
  const brief = daemon.seen.find((item) => item.method === 'POST').body;
  assert.equal(brief.voice, 'cinder');
  assert.equal(brief.to, '+14155550100');
  assert.equal(readEnv(root).COLLEAGUE_VOICE, undefined);
  const bad = await runCli(['setup', 'voice', '--preview', 'alloy', '--root', root]);
  assert.equal(bad.code, 2);
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
    if (request.url === '/v1/profile') {
      return send(200, request.method === 'PATCH' ? { version: 1, ...body } : { version: 1 });
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
  assert.match(done.stderr, /Calling \+1 415 555 0142 \(call call-0123456789abcdef\)/);
  assert.match(done.stderr, /Ringing…/);
  assert.match(done.stderr, /Result: Achieved\. Booked\./);
  const brief = daemon.seen.find((item) => item.method === 'POST').body;
  assert.deepEqual(brief, {
    channel: 'phone', to: '+14155550142', onBehalfOf: 'Robin', objective: 'Book a table',
    mayAgreeTo: ['6:30pm', '7pm'], mustNotShare: ['card number'], rehearsal: true,
  });
  const missing = await runCli(['call', ...common, '--to', '+14155550142']);
  assert.equal(missing.code, 2);
  assert.match(missing.stderr, /missing objective: What should the call achieve\?/);
});

test('setup secrets --wait stops on SIGTERM', async (t) => {
  const root = await tempRoot(t);
  const child = spawn(process.execPath, [cli, 'setup', 'secrets', '--wait', '--no-open', '--root', root], { stdio: ['ignore', 'pipe', 'pipe'] });
  let stderr = '';
  child.stderr.on('data', (chunk) => { stderr += chunk; });
  while (!/http:\/\/127/.test(stderr)) await new Promise((resolve) => setTimeout(resolve, 20));
  assert.doesNotMatch(stderr, /Could not open a browser/);
  const exited = new Promise((resolve) => child.on('close', resolve));
  child.kill('SIGTERM');
  assert.equal(await exited, 130);
});

test('status verifies a SignalWire account on its Space and suggests its number', async (t) => {
  const root = await tempRoot(t);
  await fs.writeFile(path.join(root, 'start-runtime-daemon.sh'), '#!/bin/sh\n');
  await fs.mkdir(path.join(root, 'node_modules', 'mammoth'), { recursive: true });
  await fs.writeFile(path.join(root, '.env'), [
    'OPENAI_API_KEY=sk-test', 'COLLEAGUE_OWNER_NAME=Robin', 'COLLEAGUE_OWNER_PHONE=+14165550100',
    'SIGNALWIRE_SPACE=acme.signalwire.com', 'SIGNALWIRE_PROJECT_ID=1a2b3c4d-0000', 'SIGNALWIRE_API_TOKEN=PT-secret',
  ].join('\n'));
  const requests = [];
  const fetchImpl = async (url, options) => {
    requests.push([String(url), options?.headers?.Authorization]);
    if (String(url).includes('api.openai.com')) return new Response('{}', { status: 200 });
    if (String(url).endsWith('/Accounts/1a2b3c4d-0000.json')) return Response.json({ status: 'active', type: 'Trial' });
    if (String(url).includes('IncomingPhoneNumbers')) return Response.json({ incoming_phone_numbers: [{ phone_number: '+16475550123' }] });
    return Response.json({ outgoing_caller_ids: [] });
  };
  const runner = fakeRunner({ 'docker info': 0, 'python3 -c': 0 });
  const status = await setupStatus({ root, env: {}, fetchImpl, runner, find: () => null });
  const account = status.checks.find((c) => c.id === 'phone_account');
  assert.equal(account.label, 'SignalWire account');
  assert.equal(account.ok, true);
  assert.match(account.detail, /trial: calls only numbers you bought or verified/);
  const accountRequest = requests.find(([url]) => url.includes('/Accounts/1a2b3c4d-0000.json'));
  assert.equal(accountRequest[0], 'https://acme.signalwire.com/api/laml/2010-04-01/Accounts/1a2b3c4d-0000.json');
  assert.equal(accountRequest[1], `Basic ${Buffer.from('1a2b3c4d-0000:PT-secret').toString('base64')}`);
  const caller = status.next.find((item) => item.id === 'caller_id');
  assert.deepEqual(caller.suggest, { key: 'SIGNALWIRE_FROM_NUMBER', value: '+16475550123' });
  assert.match(status.checks.find((c) => c.id === 'owner_phone').detail, /verified in SignalWire/);
  assert.equal(validateSetting('SIGNALWIRE_SPACE', 'https://Acme.signalwire.com/dashboard'), 'acme.signalwire.com');
  assert.equal(validateSetting('SIGNALWIRE_SPACE', 'acme'), 'acme.signalwire.com');
  assert.throws(() => validateSetting('SIGNALWIRE_API_TOKEN', 'PT-1'), /secret/);
  assert.throws(() => validateSetting('COLLEAGUE_PHONE_PROVIDER', 'vonage'), /twilio or signalwire/);
});

test('the setup page offers SignalWire first and folds Twilio away', () => {
  const html = renderSecretsPage({}, '/setup/x');
  assert.ok(html.indexOf('SignalWire (free trial)') < html.indexOf('Twilio (upgraded account)'));
  assert.match(html, /<details class="card"><summary>Twilio \(upgraded account\)<\/summary>/);
  assert.match(html, /name="SIGNALWIRE_API_TOKEN" type="password"/);
  assert.match(html, /name="SIGNALWIRE_SIGNING_KEY" type="password"/);
  assert.match(html, /href="https:\/\/signalwire.com"/);
  const saved = renderSecretsPage({ SIGNALWIRE_API_TOKEN: 'PT-very-secret' }, '/setup/x');
  assert.ok(!saved.includes('PT-very-secret'));
});

test('Done needs the OpenAI key and a complete SignalWire setup', () => {
  assert.deepEqual(missingForDone({}), ['OpenAI API key is required', 'Your name is required']);
  const base = { OPENAI_API_KEY: 'sk-live', COLLEAGUE_OWNER_NAME: 'Robin' };
  assert.deepEqual(missingForDone(base), []);
  assert.deepEqual(missingForDone({ ...base, SIGNALWIRE_SPACE: 'acme.signalwire.com', SIGNALWIRE_PROJECT_ID: 'p', SIGNALWIRE_SIGNING_KEY: 'PSK' }),
    ['API token is required for SignalWire phone calls']);
  assert.deepEqual(missingForDone({ ...base, OPENAI_API_KEY: 'replace_with_your_project_api_key' }), ['OpenAI API key is required']);
});

test('sip-trunk creates the SignalWire script and SIP address OpenAI dials out through', async () => {
  const requests = [];
  const fetchImpl = async (url, options) => {
    requests.push([String(url), JSON.parse(options.body), options.headers.Authorization]);
    if (String(url).endsWith('/swml_scripts')) return Response.json({ id: 'res_1' });
    return Response.json({ id: 'addr_1', uri: 'sip:*@acme-colleague-ai-openai.dapp.signalwire.com' });
  };
  const env = { SIGNALWIRE_SPACE: 'acme.signalwire.com', SIGNALWIRE_PROJECT_ID: 'p-1', SIGNALWIRE_API_TOKEN: 'PT-x',
    SIGNALWIRE_FROM_NUMBER: '+14155550124' };
  const trunk = await createSignalWireTrunk({ env, fetchImpl, password: 'generated-secret' });
  assert.deepEqual(trunk.settings, {
    COLLEAGUE_SIP_TRUNK_URL: 'sips:acme-colleague-ai-openai.dapp.signalwire.com:5061',
    COLLEAGUE_SIP_USERNAME: '+14155550124',
    COLLEAGUE_SIP_PASSWORD: 'generated-secret',
    COLLEAGUE_PHONE_AUDIO: 'sip',
  });
  const [script, address] = requests;
  assert.equal(script[0], 'https://acme.signalwire.com/api/fabric/resources/swml_scripts');
  assert.equal(script[2], `Basic ${Buffer.from('p-1:PT-x').toString('base64')}`);
  const connect = script[1].contents.sections.main[0].connect;
  assert.equal(connect.from, '+14155550124');
  assert.match(connect.to, /sips\?:/);
  assert.equal(address[0], 'https://acme.signalwire.com/api/fabric/sip_addresses');
  assert.deepEqual({ ...address[1], password: 'hidden' }, {
    name: 'colleague-ai-openai', calling_handler_resource_id: 'res_1', user: '*', encryption: 'required',
    codecs: ['OPUS', 'PCMU', 'PCMA'], password: 'hidden' });

  await assert.rejects(createSignalWireTrunk({ env: { ...env, SIGNALWIRE_API_TOKEN: '' }, fetchImpl }), /Set up SignalWire first/);
  const refusing = async () => new Response(JSON.stringify({ errors: [{ detail: 'Name has already been taken' }] }), { status: 422 });
  await assert.rejects(createSignalWireTrunk({ env, fetchImpl: refusing }), /422\): Name has already been taken/);
});

test('status says how call audio travels and what direct SIP still needs', async (t) => {
  const root = await tempRoot(t);
  await fs.writeFile(path.join(root, 'start-runtime-daemon.sh'), '#!/bin/sh\n');
  const runner = fakeRunner({ 'python3 -c': 0 });
  const find = () => null;
  const relay = await setupStatus({ root, env: {}, verify: false, runner, find });
  assert.match(relay.checks.find((c) => c.id === 'phone_audio').detail, /^relayed through this computer/);
  await fs.writeFile(path.join(root, '.env'), [
    'COLLEAGUE_PHONE_AUDIO=sip',
    'COLLEAGUE_SIP_TRUNK_URL=sips:a.dapp.signalwire.com:5061',
    'COLLEAGUE_SIP_USERNAME=+14155550124',
  ].join('\n'));
  const partial = await setupStatus({ root, env: {}, verify: false, runner, find });
  const audio = partial.checks.find((c) => c.id === 'phone_audio');
  assert.equal(audio.ok, false);
  assert.match(audio.detail, /missing COLLEAGUE_SIP_PASSWORD/);
  // OpenAI dials out, so nothing here has to be reachable from the internet.
  assert.equal(partial.checks.find((c) => c.id === 'public_url').ok, true);
  assert.equal(validateSetting('COLLEAGUE_PHONE_AUDIO', 'sip-webhook'), 'sip-webhook');
  assert.throws(() => validateSetting('COLLEAGUE_PHONE_AUDIO', 'webrtc'), /relay, sip, sip-webhook/);
  assert.throws(() => validateSetting('COLLEAGUE_SIP_TRUNK_URL', 'sip:host'), /sips:/);
  assert.throws(() => validateSetting('OPENAI_PROJECT_ID', 'abc'), /proj_/);
  assert.throws(() => validateSetting('COLLEAGUE_SIP_PASSWORD', 'x'), /secret/);
});

test('profile commands and the new call flags reach the daemon', async (t) => {
  const root = await tempRoot(t);
  await fs.writeFile(path.join(root, '.env'), 'COLLEAGUE_OWNER_NAME=Robin\n');
  const daemon = await fakeCallsDaemon(root);
  t.after(daemon.close);
  const common = ['--root', root, '--port', String(daemon.port)];
  const person = await runCli(['profile', 'person', ...common, '--name', 'Sam', '--relationship', 'close friend',
    '--phone', '+14155550143']);
  assert.equal(person.code, 0, person.stderr);
  const removed = await runCli(['profile', 'person', ...common, '--name', 'Sam', '--remove']);
  assert.equal(removed.code, 0, removed.stderr);
  const about = await runCli(['profile', 'set', ...common, '--about', 'Robin builds Colleague AI.', '--boundaries', 'No money talk; No family details']);
  assert.equal(about.code, 0, about.stderr);
  const shown = await runCli(['profile', 'show', ...common]);
  assert.equal(JSON.parse(shown.stdout).version, 1);
  const patches = daemon.seen.filter((item) => item.method === 'PATCH').map((item) => item.body);
  assert.deepEqual(patches, [
    { people: [{ name: 'Sam', relationship: 'close friend', phone: '+14155550143' }] },
    { removePeople: ['Sam'] },
    { about: 'Robin builds Colleague AI.', boundaries: ['No money talk', 'No family details'] },
  ]);
  const contextFile = path.join(root, 'context.json');
  await fs.writeFile(contextFile, JSON.stringify({ summary: 'Colleague AI lets agents call.', details: 'Pricing.' }));
  const placed = await runCli(['call', ...common, '--to', '+14155550143', '--objective', 'Get feedback',
    '--questions', 'Launch now or wait?; Who would use it?', '--tone', 'casual', '--context-file', contextFile]);
  assert.equal(placed.code, 0, placed.stderr);
  const brief = daemon.seen.find((item) => item.method === 'POST' && item.url === '/v1/calls').body;
  assert.deepEqual(brief.questions, ['Launch now or wait?', 'Who would use it?']);
  assert.equal(brief.tone, 'casual');
  assert.deepEqual(brief.context, { summary: 'Colleague AI lets agents call.', details: 'Pricing.' });
});

async function closedPort() {
  const server = http.createServer();
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  const { port } = server.address();
  await new Promise((resolve) => server.close(resolve));
  return port;
}

async function listing(dir) {
  const out = [];
  for (const entry of await fs.readdir(dir, { recursive: true })) out.push(entry);
  return out.sort();
}

test('managed status skips host-only checks and says Colleague AI runs in its container', async (t) => {
  const root = await tempRoot(t);
  await fs.writeFile(path.join(root, '.env'), 'OPENAI_API_KEY=sk-test\nCOLLEAGUE_OWNER_NAME=Robin\n');
  const ran = [];
  const runner = (binary, args) => { ran.push(`${binary} ${args.join(' ')}`); return fakeRunner({ 'docker info': 0 })(binary, args); };
  const status = await setupStatus({ root, env: {}, verify: false, runner, find: () => null, managed: true });
  const ids = status.checks.map((c) => c.id);
  for (const hostOnly of ['platform', 'checkout', 'line_endings', 'dependencies']) assert.ok(!ids.includes(hostOnly), hostOnly);
  const runtime = status.checks.find((c) => c.id === 'runtime');
  assert.equal(runtime.ok, true);
  assert.match(runtime.detail, /container/);
  assert.equal(status.checks.find((c) => c.id === 'docker').ok, true);
  for (const kept of ['node', 'openai_key', 'owner_name', 'phone_account', 'caller_id', 'owner_phone', 'phone_audio', 'public_url', 'agents', 'daemon']) {
    assert.ok(ids.includes(kept), kept);
  }
  assert.ok(!ran.some((command) => command.startsWith('python3') || command.startsWith('claude') || command.startsWith('codex')));
  assert.equal(status.ready, true);
  assert.equal(status.meetingsReady, true);
  // Without Docker access, meetings are what is missing; the basics are still ready.
  const noDocker = await setupStatus({ root, env: {}, verify: false, runner: fakeRunner({}), find: () => null, managed: true });
  assert.equal(noDocker.ready, true);
  assert.equal(noDocker.meetingsReady, false);
  // Outside the container the host checks are still there.
  const host = await setupStatus({ root, env: {}, verify: false, runner: fakeRunner({ 'docker info': 0 }), find: () => null, managed: false });
  assert.ok(host.checks.some((c) => c.id === 'line_endings'));
});

test('managed setup status from the CLI', async (t) => {
  const root = await tempRoot(t);
  const port = await closedPort();
  const result = await runCli(['setup', 'status', '--json', '--no-verify'], {
    COLLEAGUE_MANAGED: '1', COLLEAGUE_ROOT: root, COLLEAGUE_DAEMON_PORT: String(port),
  });
  const report = JSON.parse(result.stdout);
  assert.ok(!report.checks.some((c) => c.id === 'line_endings'));
  assert.match(report.checks.find((c) => c.id === 'runtime').detail, /container/);
  const daemon = report.checks.find((c) => c.id === 'daemon');
  assert.equal(daemon.ok, false);
  assert.match(daemon.fix, /docker restart colleague/);
});

test('managed setup register prints how to connect host agents and writes nothing there', async (t) => {
  const root = await tempRoot(t);
  const home = await tempRoot(t);
  await fs.mkdir(path.join(home, '.claude'));
  await fs.mkdir(path.join(home, '.cursor'));
  const env = { COLLEAGUE_MANAGED: '1', COLLEAGUE_ROOT: root, HOME: home, PATH: process.env.PATH };
  const result = await runCli(['setup', 'register', '--json'], env);
  assert.equal(result.code, 0, result.stderr);
  const report = JSON.parse(result.stdout);
  const token = (await fs.readFile(path.join(root, '.colleague', 'mcp.token'), 'utf8')).trim();
  assert.match(token, /^[A-Za-z0-9_-]{43}$/);
  assert.equal(report.managed, true);
  assert.deepEqual(report.mcp, { url: 'http://127.0.0.1:8095/mcp', headers: { Authorization: `Bearer ${token}` } });
  assert.equal(report.agents['claude-code'].command,
    `claude mcp add --transport http --scope user colleague-ai http://127.0.0.1:8095/mcp --header "Authorization: Bearer ${token}"`);
  assert.equal(report.agents.codex.toml,
    `[mcp_servers.colleague-ai]\nurl = "http://127.0.0.1:8095/mcp"\nhttp_headers = { Authorization = "Bearer ${token}" }\n`);
  assert.deepEqual(report.agents.cursor.json, {
    mcpServers: { 'colleague-ai': { url: 'http://127.0.0.1:8095/mcp', headers: { Authorization: `Bearer ${token}` } } },
  });
  assert.deepEqual(report.agents['claude-desktop'].json, {
    mcpServers: { 'colleague-ai': { command: 'docker', args: ['exec', '-i', 'colleague', 'colleague', 'mcp'] } },
  });
  const code = path.resolve(path.dirname(cli), '../../..');
  assert.equal(report.skill.path, `${code}/.agents/skills/call-with-colleague-ai/SKILL.md`);
  assert.equal(report.skill.copy['claude-code'], `docker cp colleague:${code}/.agents/skills/call-with-colleague-ai ~/.claude/skills/`);
  // Nothing on the "host" side changed; only the token was made, under the data root.
  assert.deepEqual(await listing(home), ['.claude', '.cursor']);
  assert.deepEqual(await listing(root), ['.colleague', path.join('.colleague', 'mcp.token')]);

  // The same token again, and a plain-text version for a person.
  const text = await runCli(['setup', 'register'], env);
  assert.equal(text.code, 0, text.stderr);
  assert.match(text.stdout, /claude mcp add --transport http --scope user colleague-ai http:\/\/127\.0\.0\.1:8095\/mcp/);
  assert.ok(text.stdout.includes(`Authorization: Bearer ${token}`));
  assert.match(text.stdout, /"command": "docker"/);
  assert.match(text.stdout, /docker cp colleague:/);
  assert.equal(containerRegistration({ codeRoot: '/app', token: 't', container: 'c2' }).skill.copy.codex,
    'docker cp c2:/app/.agents/skills/call-with-colleague-ai ~/.codex/skills/');
});

test('register outside the container also offers the local HTTP endpoint', async (t) => {
  const home = await tempRoot(t);
  const runner = () => ({ status: 0, stdout: '', stderr: '' });
  const http = localMcpConnection({ token: 'tok', port: 8095 });
  assert.deepEqual(http, { url: 'http://127.0.0.1:8095/mcp', headers: { Authorization: 'Bearer tok' } });
  const { manual } = registerAgents('/repo', { runner, find: () => null, home, windows: null, http });
  assert.deepEqual(manual.http, http);
  assert.equal(manual.args[0], '/repo/packages/mcp/src/server.mjs');
  assert.match(manual.note, /Streamable HTTP to http:\/\/127\.0\.0\.1:8095\/mcp/);
});

test('managed setup start reports the daemon and never starts one', async (t) => {
  const root = await tempRoot(t);
  const port = await closedPort();
  const env = { COLLEAGUE_MANAGED: '1', COLLEAGUE_ROOT: root, COLLEAGUE_DAEMON_PORT: String(port) };
  const down = await runCli(['setup', 'start'], env);
  assert.equal(down.code, 3);
  assert.deepEqual(JSON.parse(down.stdout), {
    running: false, started: false, port, managed: true,
    error: 'Colleague AI is not running; restart the container: docker restart colleague',
  });
  await assert.rejects(fs.access(path.join(root, '.colleague', 'daemon.log')));

  const daemon = http.createServer((request, response) => response.end());
  await new Promise((resolve) => daemon.listen(0, '127.0.0.1', resolve));
  t.after(() => new Promise((resolve) => daemon.close(resolve)));
  const up = await runCli(['setup', 'start'], { ...env, COLLEAGUE_DAEMON_PORT: String(daemon.address().port) });
  assert.equal(up.code, 0, up.stderr);
  assert.deepEqual(JSON.parse(up.stdout), { running: true, started: false, port: daemon.address().port, managed: true });

  // A call with the daemon down fails at once with the same advice.
  const call = await runCli(['call', '--to', '+14155550142', '--objective', 'Book a table', '--port', String(port)], env);
  assert.equal(call.code, 3);
  assert.match(call.stderr, /Colleague AI is not running; restart the container: docker restart colleague/);
  assert.match(call.stderr, /Hint: Restart the container/);
});

test('COLLEAGUE_ROOT is where the CLI keeps .env and the local MCP token', async (t) => {
  const root = await tempRoot(t);
  const saved = await runCli(['setup', 'set', 'COLLEAGUE_OWNER_NAME', 'Robin'], { COLLEAGUE_ROOT: root });
  assert.equal(saved.code, 0, saved.stderr);
  assert.equal(readEnv(root).COLLEAGUE_OWNER_NAME, 'Robin');
  assert.equal(statSync(path.join(root, '.env')).mode & 0o777, 0o600);
  const { dataRoot } = await import('../src/colleague.mjs');
  const previous = process.env.COLLEAGUE_ROOT;
  process.env.COLLEAGUE_ROOT = root;
  try {
    assert.equal(dataRoot({}), root);
    assert.equal(dataRoot({ root: '/elsewhere' }), '/elsewhere');
  } finally {
    if (previous === undefined) delete process.env.COLLEAGUE_ROOT;
    else process.env.COLLEAGUE_ROOT = previous;
  }
  assert.equal(dataRoot({}), path.resolve(previous || path.resolve(path.dirname(cli), '../../..')));
});

test('colleague mcp serves MCP on stdin and stdout', async (t) => {
  const root = await tempRoot(t);
  const child = spawn(process.execPath, [cli, 'mcp'], {
    env: { ...process.env, COLLEAGUE_ROOT: root }, stdio: ['pipe', 'pipe', 'pipe'],
  });
  let stdout = '';
  child.stdout.on('data', (chunk) => { stdout += chunk; });
  child.stdin.write(`${JSON.stringify({ jsonrpc: '2.0', id: 1, method: 'initialize', params: { protocolVersion: '2025-06-18' } })}\n`);
  child.stdin.end(`${JSON.stringify({ jsonrpc: '2.0', id: 2, method: 'tools/list', params: {} })}\n`);
  const code = await new Promise((resolve) => child.on('close', resolve));
  assert.equal(code, 0);
  const replies = stdout.split('\n').filter(Boolean).map((line) => JSON.parse(line));
  assert.deepEqual(replies.map((reply) => reply.id), [1, 2]);
  assert.equal(replies[0].result.serverInfo.name, 'colleague-ai');
  assert.equal(replies[1].result.tools.length, 11);
});
