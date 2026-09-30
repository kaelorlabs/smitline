import assert from 'node:assert/strict';
import crypto from 'node:crypto';
import { spawn } from 'node:child_process';
import fs from 'node:fs/promises';
import { statSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import test from 'node:test';
import { CALL_TOOL_DEFINITIONS } from '../src/session.mjs';
import { createConnector, loadConnectorConfig } from '../src/remote.mjs';

const PASSPHRASE = 'correct horse battery staple';
const CALL_ID = 'call-0123456789abcdef';
const REDIRECT = 'https://agent.example/callback';
const BRIEF = { channel: 'phone', to: '+14155550142', onBehalfOf: 'Robin', objective: 'Book a table for 4 at 7pm' };
const cli = fileURLToPath(new URL('../../cli/src/colleague.mjs', import.meta.url));
const remote = fileURLToPath(new URL('../src/remote.mjs', import.meta.url));

function fakeColleague() {
  const seen = [];
  return {
    seen,
    async startCall(brief) { seen.push(['start', brief]); return { id: CALL_ID, status: 'queued' }; },
    async checkCall() { return { ok: true, problems: [] }; },
    async waitForCall(id) { return { id, status: 'completed', result: { outcome: 'achieved' } }; },
    async getCall(id) { return { id, status: 'ringing' }; },
    async listCalls() { return []; },
    async instructCall() { return { delivered: true }; },
    async endCall(id) { return { id, status: 'summarizing' }; },
    async transferCall() { return { transferred: true }; },
    async listVoices() { return { default: 'marin', voices: ['marin'] }; },
  };
}

async function tempRoot(t) {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'colleague-connector-'));
  t.after(() => fs.rm(root, { recursive: true, force: true }));
  return root;
}

async function startServer(t, options = {}) {
  const root = await tempRoot(t);
  const clock = { now: Date.now() };
  const logs = [];
  const colleague = fakeColleague();
  const connector = createConnector({
    root, passphrase: PASSPHRASE, colleague, now: () => clock.now, log: (line) => logs.push(line), ...options,
  });
  await connector.listen(0, '127.0.0.1');
  t.after(() => connector.close());
  return { root, clock, logs, colleague, connector, base: connector.url };
}

function pkce() {
  const verifier = crypto.randomBytes(32).toString('base64url');
  return { verifier, challenge: crypto.createHash('sha256').update(verifier).digest('base64url') };
}

async function register(base, overrides = {}, headers = {}) {
  const response = await fetch(`${base}/oauth/register`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...headers },
    body: JSON.stringify({
      client_name: 'Test agent',
      redirect_uris: [REDIRECT],
      grant_types: ['authorization_code', 'refresh_token'],
      response_types: ['code'],
      token_endpoint_auth_method: 'none',
      ...overrides,
    }),
  });
  return { status: response.status, body: await response.json() };
}

function authorizeUrl(base, client, challenge, extra = {}) {
  const url = new URL(`${base}/oauth/authorize`);
  const params = {
    response_type: 'code',
    client_id: client.client_id,
    redirect_uri: client.redirect_uris[0],
    state: 'state-123',
    code_challenge: challenge,
    code_challenge_method: 'S256',
    resource: `${base}/mcp`,
    ...extra,
  };
  for (const [key, value] of Object.entries(params)) if (value !== undefined) url.searchParams.set(key, value);
  return url;
}

function unescapeHtml(text) {
  return text.replace(/&quot;/g, '"').replace(/&#39;/g, "'").replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&amp;/g, '&');
}

async function approvalForm(url) {
  const page = await fetch(url);
  const html = await page.text();
  assert.equal(page.status, 200, html);
  const fields = {};
  for (const match of html.matchAll(/<input type="hidden" name="([^"]+)" value="([^"]*)">/g)) fields[match[1]] = unescapeHtml(match[2]);
  return { html, fields, headers: page.headers };
}

function postForm(base, fields) {
  return fetch(`${base}/oauth/authorize`, {
    method: 'POST',
    redirect: 'manual',
    headers: { 'Content-Type': 'application/x-www-form-urlencoded', Origin: base },
    body: new URLSearchParams(fields),
  });
}

async function approve(base, url, { passphrase = PASSPHRASE, decision = 'approve', tamper = {} } = {}) {
  const { fields } = await approvalForm(url);
  return postForm(base, { ...fields, ...tamper, passphrase, decision });
}

function codeFrom(response) {
  assert.equal(response.status, 303);
  return new URL(response.headers.get('location')).searchParams.get('code');
}

async function tokenRequest(base, params) {
  const response = await fetch(`${base}/oauth/token`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    body: new URLSearchParams(params),
  });
  return { status: response.status, body: await response.json(), headers: response.headers };
}

async function signIn(base) {
  const { body: client } = await register(base);
  const { verifier, challenge } = pkce();
  const code = codeFrom(await approve(base, authorizeUrl(base, client, challenge)));
  const tokens = await tokenRequest(base, {
    grant_type: 'authorization_code', code, redirect_uri: REDIRECT, code_verifier: verifier, client_id: client.client_id,
  });
  assert.equal(tokens.status, 200);
  return { client, tokens: tokens.body, code };
}

async function mcp(base, token, body, { session, headers = {} } = {}) {
  const response = await fetch(`${base}/mcp`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      Accept: 'application/json, text/event-stream',
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...(session ? { 'Mcp-Session-Id': session, 'MCP-Protocol-Version': '2025-06-18' } : {}),
      ...headers,
    },
    body: typeof body === 'string' ? body : JSON.stringify(body),
  });
  const text = await response.text();
  return { status: response.status, headers: response.headers, body: text ? JSON.parse(text) : null };
}

const INITIALIZE = {
  jsonrpc: '2.0', id: 1, method: 'initialize',
  params: { protocolVersion: '2025-06-18', capabilities: {}, clientInfo: { name: 'test', version: '1' } },
};
const PING = { jsonrpc: '2.0', id: 9, method: 'ping' };

async function openSession(base, token) {
  const init = await mcp(base, token, INITIALIZE);
  assert.equal(init.status, 200);
  return init.headers.get('mcp-session-id');
}

function runCli(args, env = {}) {
  return new Promise((resolve) => {
    const child = spawn(process.execPath, args, { env: { ...process.env, ...env }, stdio: ['ignore', 'pipe', 'pipe'] });
    let stdout = '';
    let stderr = '';
    child.stdout.on('data', (chunk) => { stdout += chunk; });
    child.stderr.on('data', (chunk) => { stderr += chunk; });
    child.on('close', (code) => resolve({ code, stdout, stderr }));
  });
}

test('discovery, registration, approval, tokens, and a call over MCP', async (t) => {
  const { base, colleague } = await startServer(t);
  const unauthenticated = await mcp(base, null, INITIALIZE);
  assert.equal(unauthenticated.status, 401);
  const metadataUrl = /resource_metadata="([^"]+)"/.exec(unauthenticated.headers.get('www-authenticate'))[1];
  const resource = await (await fetch(metadataUrl)).json();
  assert.equal(resource.resource, `${base}/mcp`);
  const issuer = resource.authorization_servers[0];
  const server = await (await fetch(`${issuer}/.well-known/oauth-authorization-server`)).json();
  assert.equal(server.issuer, base);
  assert.deepEqual(server.code_challenge_methods_supported, ['S256']);
  assert.deepEqual(server.token_endpoint_auth_methods_supported, ['none']);

  const registered = await register(server.registration_endpoint.replace('/oauth/register', ''));
  assert.equal(registered.status, 201);
  const client = registered.body;
  assert.equal(client.token_endpoint_auth_method, 'none');
  assert.equal(client.client_secret, undefined);
  assert.deepEqual(client.redirect_uris, [REDIRECT]);

  const { verifier, challenge } = pkce();
  const form = await approvalForm(authorizeUrl(base, client, challenge));
  assert.match(form.html, /Allow Test agent to use Colleague AI\?/);
  assert.match(form.html, /https:\/\/agent\.example/);
  assert.match(form.html, /<label for="passphrase">Owner passphrase<\/label>/);
  assert.equal(form.headers.get('x-frame-options'), 'DENY');
  assert.match(form.headers.get('content-security-policy'), /frame-ancestors 'none'/);
  const approved = await postForm(base, { ...form.fields, passphrase: PASSPHRASE, decision: 'approve' });
  assert.equal(approved.status, 303);
  const location = new URL(approved.headers.get('location'));
  assert.equal(`${location.origin}${location.pathname}`, REDIRECT);
  assert.equal(location.searchParams.get('state'), 'state-123');
  assert.equal(location.searchParams.get('iss'), base);

  const tokens = await tokenRequest(base, {
    grant_type: 'authorization_code', code: location.searchParams.get('code'), redirect_uri: REDIRECT,
    code_verifier: verifier, client_id: client.client_id, resource: `${base}/mcp`,
  });
  assert.equal(tokens.status, 200);
  assert.equal(tokens.headers.get('cache-control'), 'no-store');
  assert.equal(tokens.body.token_type, 'Bearer');
  assert.equal(tokens.body.expires_in, 3600);
  assert.ok(tokens.body.refresh_token);
  const token = tokens.body.access_token;

  const init = await mcp(base, token, { ...INITIALIZE, params: { ...INITIALIZE.params, protocolVersion: '2025-03-26' } });
  assert.equal(init.status, 200);
  assert.equal(init.body.result.protocolVersion, '2025-03-26');
  assert.equal(init.body.result.capabilities.extensions, undefined);
  const session = init.headers.get('mcp-session-id');
  assert.ok(session);
  const oldVersion = await mcp(base, token, { ...INITIALIZE, params: { ...INITIALIZE.params, protocolVersion: '2024-11-05' } });
  assert.equal(oldVersion.body.result.protocolVersion, '2025-06-18');

  const initialized = await mcp(base, token, { jsonrpc: '2.0', method: 'notifications/initialized' }, { session });
  assert.equal(initialized.status, 202);
  const listed = await mcp(base, token, { jsonrpc: '2.0', id: 2, method: 'tools/list' }, { session });
  assert.deepEqual(listed.body.result.tools.map((tool) => tool.name), CALL_TOOL_DEFINITIONS.map((tool) => tool.name));
  const remoteStart = listed.body.result.tools.find((tool) => tool.name === 'start_call');
  assert.equal(remoteStart.inputSchema.properties.agentSession, undefined);
  assert.deepEqual(remoteStart.inputSchema.required, ['channel', 'objective']);
  const started = await mcp(base, token, {
    jsonrpc: '2.0', id: 3, method: 'tools/call', params: { name: 'start_call', arguments: BRIEF },
  }, { session });
  assert.equal(started.status, 200);
  assert.equal(started.body.id, 3);
  assert.equal(started.body.result.structuredContent.id, CALL_ID);
  assert.deepEqual(colleague.seen, [['start', BRIEF]]);
  const meeting = await mcp(base, token, {
    jsonrpc: '2.0', id: 4, method: 'tools/call', params: { name: 'start_meeting', arguments: {} },
  }, { session });
  assert.equal(meeting.body.result.isError, true);
  assert.match(meeting.body.result.structuredContent.message, /unknown tool/);
  const batch = await mcp(base, token, [PING, { jsonrpc: '2.0', method: 'notifications/cancelled', params: {} }], { session });
  assert.deepEqual(batch.body, [{ jsonrpc: '2.0', id: 9, result: {} }]);
});

test('refresh tokens rotate, and reusing an old one revokes the grant', async (t) => {
  const { base, clock } = await startServer(t);
  const { client, tokens } = await signIn(base);
  const rotated = await tokenRequest(base, { grant_type: 'refresh_token', refresh_token: tokens.refresh_token, client_id: client.client_id });
  assert.equal(rotated.status, 200);
  assert.notEqual(rotated.body.access_token, tokens.access_token);
  assert.notEqual(rotated.body.refresh_token, tokens.refresh_token);
  const session = await openSession(base, rotated.body.access_token);

  // A retry within the grace period (lost response, parallel refresh) still works.
  const retried = await tokenRequest(base, { grant_type: 'refresh_token', refresh_token: tokens.refresh_token, client_id: client.client_id });
  assert.equal(retried.status, 200);
  assert.notEqual(retried.body.refresh_token, rotated.body.refresh_token);
  clock.now += 2 * 60_000;

  const other = await signIn(base);
  const crossClient = await tokenRequest(base, {
    grant_type: 'refresh_token', refresh_token: rotated.body.refresh_token, client_id: other.client.client_id,
  });
  assert.equal(crossClient.body.error, 'invalid_grant');

  const replay = await tokenRequest(base, { grant_type: 'refresh_token', refresh_token: tokens.refresh_token, client_id: client.client_id });
  assert.equal(replay.status, 400);
  assert.equal(replay.body.error, 'invalid_grant');
  const revoked = await mcp(base, rotated.body.access_token, PING, { session });
  assert.equal(revoked.status, 401);
  const after = await tokenRequest(base, { grant_type: 'refresh_token', refresh_token: rotated.body.refresh_token, client_id: client.client_id });
  assert.equal(after.body.error, 'invalid_grant');
  assert.equal((await openSession(base, other.tokens.access_token)).length > 0, true);
});

test('rejects a wrong verifier, a reused code, bad redirect URIs, and a tampered form', async (t) => {
  const { base } = await startServer(t);
  const local = 'http://127.0.0.1:9999/cb';
  const { body: client } = await register(base, { redirect_uris: [REDIRECT, local] });
  const exchange = (code, verifier, redirectUri = REDIRECT) => tokenRequest(base, {
    grant_type: 'authorization_code', code, redirect_uri: redirectUri, code_verifier: verifier, client_id: client.client_id,
  });

  let { verifier, challenge } = pkce();
  let code = codeFrom(await approve(base, authorizeUrl(base, client, challenge)));
  const wrong = await exchange(code, pkce().verifier);
  assert.equal(wrong.body.error, 'invalid_grant');
  assert.match(wrong.body.error_description, /code_verifier/);
  assert.equal((await exchange(code, verifier)).body.error, 'invalid_grant');

  ({ verifier, challenge } = pkce());
  code = codeFrom(await approve(base, authorizeUrl(base, client, challenge)));
  const first = await exchange(code, verifier);
  assert.equal(first.status, 200);
  const reused = await exchange(code, verifier);
  assert.equal(reused.status, 400);
  assert.equal(reused.body.error, 'invalid_grant');
  assert.equal((await mcp(base, first.body.access_token, INITIALIZE)).status, 401);

  ({ verifier, challenge } = pkce());
  code = codeFrom(await approve(base, authorizeUrl(base, client, challenge)));
  const mismatch = await exchange(code, verifier, local);
  assert.equal(mismatch.body.error, 'invalid_grant');
  assert.match(mismatch.body.error_description, /redirect_uri/);

  const unregistered = await fetch(authorizeUrl(base, client, challenge, { redirect_uri: 'https://evil.example/cb' }), { redirect: 'manual' });
  assert.equal(unregistered.status, 400);
  assert.equal(unregistered.headers.get('location'), null);
  const plain = await fetch(authorizeUrl(base, client, challenge, { code_challenge_method: 'plain' }), { redirect: 'manual' });
  assert.equal(plain.status, 302);
  const plainError = new URL(plain.headers.get('location')).searchParams;
  assert.equal(plainError.get('error'), 'invalid_request');
  assert.equal(plainError.get('state'), 'state-123');
  const target = await fetch(authorizeUrl(base, client, challenge, { resource: 'https://other.example/mcp' }), { redirect: 'manual' });
  assert.equal(new URL(target.headers.get('location')).searchParams.get('error'), 'invalid_target');
  const noState = await fetch(authorizeUrl(base, client, challenge, { state: undefined }), { redirect: 'manual' });
  assert.equal(new URL(noState.headers.get('location')).searchParams.get('error'), 'invalid_request');

  const tampered = await approve(base, authorizeUrl(base, client, challenge), { tamper: { redirect_uri: local } });
  assert.equal(tampered.status, 400);
  assert.equal(tampered.headers.get('location'), null);
  const denied = await approve(base, authorizeUrl(base, client, challenge), { decision: 'deny', passphrase: '' });
  assert.equal(new URL(denied.headers.get('location')).searchParams.get('error'), 'access_denied');

  for (const redirectUris of [['http://agent.example/cb'], ['myapp://callback'], ['https://agent.example/cb#frag'], []]) {
    const rejected = await register(base, { redirect_uris: redirectUris });
    assert.equal(rejected.status, 400);
    assert.equal(rejected.body.error, 'invalid_redirect_uri');
  }
  const loopback = await register(base, { redirect_uris: ['http://localhost:6274/oauth/callback'] });
  assert.equal(loopback.status, 201);
  const secretClient = await register(base, { token_endpoint_auth_method: 'client_secret_basic' });
  assert.equal(secretClient.body.token_endpoint_auth_method, 'none');
  const implicit = await register(base, { grant_types: ['implicit'] });
  assert.equal(implicit.body.error, 'invalid_client_metadata');
});

test('wrong passphrases are refused, and five lock approval for ten minutes', async (t) => {
  const { base, clock, logs } = await startServer(t);
  const { body: client } = await register(base);
  const url = authorizeUrl(base, client, pkce().challenge);

  const { fields } = await approvalForm(url);
  const first = await postForm(base, { ...fields, passphrase: 'not the passphrase', decision: 'approve' });
  assert.equal(first.status, 403);
  assert.equal(first.headers.get('location'), null);
  assert.match(await first.text(), /That passphrase is not correct/);
  const replayedForm = await postForm(base, { ...fields, passphrase: PASSPHRASE, decision: 'approve' });
  assert.equal(replayedForm.status, 400);

  for (let attempt = 2; attempt <= 4; attempt += 1) {
    assert.equal((await approve(base, url, { passphrase: 'not the passphrase' })).status, 403);
  }
  const fifth = await approve(base, url, { passphrase: 'not the passphrase' });
  assert.equal(fifth.status, 429);
  const locked = await approve(base, url);
  assert.equal(locked.status, 429);
  assert.ok(Number(locked.headers.get('retry-after')) > 0);
  assert.match(await locked.text(), /locked for 10 more minutes/);

  clock.now += 10 * 60_000 + 1_000;
  const unlocked = await approve(base, url);
  assert.equal(unlocked.status, 303);
  assert.ok(new URL(unlocked.headers.get('location')).searchParams.get('code'));
  const logged = logs.join('\n');
  assert.ok(!logged.includes(PASSPHRASE));
  assert.ok(!logged.includes('not the passphrase'));
  assert.match(logged, /approval locked/);
});

test('missing, unknown, and expired tokens get 401 with WWW-Authenticate', async (t) => {
  const { base, clock } = await startServer(t);
  const metadata = `resource_metadata="${base}/.well-known/oauth-protected-resource"`;
  const missing = await mcp(base, null, PING);
  assert.equal(missing.status, 401);
  assert.equal(missing.headers.get('www-authenticate'), `Bearer ${metadata}`);
  const unknown = await mcp(base, 'cai_at_not-a-real-token', PING);
  assert.equal(unknown.status, 401);
  assert.match(unknown.headers.get('www-authenticate'), /^Bearer error="invalid_token"/);
  assert.ok(unknown.headers.get('www-authenticate').includes(metadata));

  const { tokens } = await signIn(base);
  const session = await openSession(base, tokens.access_token);
  assert.equal((await mcp(base, tokens.access_token, PING, { session })).status, 200);
  clock.now += 60 * 60_000 + 1;
  const expired = await mcp(base, tokens.access_token, PING, { session });
  assert.equal(expired.status, 401);
  assert.match(expired.headers.get('www-authenticate'), /error="invalid_token"/);
});

test('Streamable HTTP rules: Origin, sessions, notifications, methods, and body size', async (t) => {
  const inspector = 'http://localhost:6274';
  const { base } = await startServer(t, { allowedOrigins: [inspector] });
  const { tokens } = await signIn(base);
  const token = tokens.access_token;

  const evil = await mcp(base, token, INITIALIZE, { headers: { Origin: 'https://evil.example' } });
  assert.equal(evil.status, 403);
  assert.equal((await register(base, {}, { Origin: 'https://evil.example' })).status, 403);
  const nullOrigin = await mcp(base, token, INITIALIZE, { headers: { Origin: 'null' } });
  assert.equal(nullOrigin.status, 403);
  assert.equal((await mcp(base, token, INITIALIZE, { headers: { Origin: base } })).status, 200);
  const allowed = await mcp(base, token, INITIALIZE, { headers: { Origin: inspector } });
  assert.equal(allowed.status, 200);
  assert.equal(allowed.headers.get('access-control-allow-origin'), inspector);
  const session = allowed.headers.get('mcp-session-id');

  assert.equal((await mcp(base, token, PING)).status, 400);
  assert.equal((await mcp(base, token, PING, { session: 'not-a-session' })).status, 404);
  const notification = await mcp(base, token, { jsonrpc: '2.0', method: 'notifications/initialized' }, { session });
  assert.equal(notification.status, 202);
  assert.equal(notification.body, null);
  const badVersion = await mcp(base, token, PING, { session, headers: { 'MCP-Protocol-Version': '2024-01-01' } });
  assert.equal(badVersion.status, 400);
  const notJson = await mcp(base, token, 'not json', { session });
  assert.equal(notJson.body.error.code, -32700);
  const invalid = await mcp(base, token, { id: 3, method: 'ping' }, { session });
  assert.equal(invalid.body.error.code, -32600);

  const get = await fetch(`${base}/mcp`, { headers: { Authorization: `Bearer ${token}`, 'Mcp-Session-Id': session } });
  assert.equal(get.status, 405);
  const other = await signIn(base);
  assert.equal((await mcp(base, other.tokens.access_token, PING, { session })).status, 404);

  const big = await mcp(base, token, JSON.stringify({ ...PING, params: { pad: 'x'.repeat(1024 * 1024) } }), { session });
  assert.equal(big.status, 413);

  const removed = await fetch(`${base}/mcp`, { method: 'DELETE', headers: { Authorization: `Bearer ${token}`, 'Mcp-Session-Id': session } });
  assert.equal(removed.status, 204);
  assert.equal((await mcp(base, token, PING, { session })).status, 404);
});

test('token files hold only digests, in private files', async (t) => {
  const { base, root, logs } = await startServer(t);
  const { client, tokens, code } = await signIn(base);
  const rotated = (await tokenRequest(base, {
    grant_type: 'refresh_token', refresh_token: tokens.refresh_token, client_id: client.client_id,
  })).body;
  const dir = path.join(root, '.colleague', 'connector');
  const stored = await fs.readFile(path.join(dir, 'tokens.json'), 'utf8');
  const secrets = [tokens.access_token, tokens.refresh_token, rotated.access_token, rotated.refresh_token, code];
  for (const secret of secrets) {
    assert.ok(!stored.includes(secret));
    assert.ok(!logs.join('\n').includes(secret));
  }
  for (const secret of secrets.slice(0, 4)) {
    assert.ok(stored.includes(crypto.createHash('sha256').update(secret).digest('hex')));
  }
  assert.equal(statSync(dir).mode & 0o777, 0o700);
  assert.equal(statSync(path.join(dir, 'tokens.json')).mode & 0o777, 0o600);
  assert.equal(statSync(path.join(dir, 'clients.json')).mode & 0o777, 0o600);
});

test('colleague connector status and revoke act on a running connector', async (t) => {
  const { base, root } = await startServer(t);
  const { client, tokens } = await signIn(base);
  const status = await runCli([cli, 'connector', 'status', '--root', root], { COLLEAGUE_CONNECTOR_URL: 'https://colleague.example.com' });
  assert.equal(status.code, 0, status.stderr);
  const report = JSON.parse(status.stdout);
  assert.equal(report.connectorUrl, 'https://colleague.example.com/mcp');
  assert.equal(report.clients[0].clientId, client.client_id);
  assert.equal(report.clients[0].activeGrants, 1);
  assert.equal(report.grants[0].clientName, 'Test agent');
  assert.ok(!status.stdout.includes(tokens.access_token));
  assert.ok(!status.stdout.includes(tokens.refresh_token));

  const usage = await runCli([cli, 'connector', 'revoke', '--root', root]);
  assert.equal(usage.code, 2);
  const unknown = await runCli([cli, 'connector', 'revoke', '--client', 'cai-unknown', '--root', root]);
  assert.equal(unknown.code, 2);
  const revoked = await runCli([cli, 'connector', 'revoke', '--client', client.client_id, '--root', root]);
  assert.equal(revoked.code, 0, revoked.stderr);
  assert.deepEqual(JSON.parse(revoked.stdout).revoked, { grants: 1, tokens: 2 });
  assert.equal((await mcp(base, tokens.access_token, INITIALIZE)).status, 401);
  const refresh = await tokenRequest(base, { grant_type: 'refresh_token', refresh_token: tokens.refresh_token, client_id: client.client_id });
  assert.equal(refresh.body.error, 'invalid_grant');

  const again = await signIn(base);
  const all = await runCli([cli, 'connector', 'revoke', '--all', '--root', root]);
  assert.equal(JSON.parse(all.stdout).revoked.grants, 1);
  assert.equal((await mcp(base, again.tokens.access_token, INITIALIZE)).status, 401);
});

test('configuration comes from the environment or .env and is required', async (t) => {
  const root = await tempRoot(t);
  assert.throws(() => loadConnectorConfig({ root, env: {} }), (error) => (
    /COLLEAGUE_CONNECTOR_URL is not set/.test(error.message) && /COLLEAGUE_CONNECTOR_PASSPHRASE is not set/.test(error.message)
  ));
  const env = { COLLEAGUE_CONNECTOR_URL: 'https://colleague.example.com', COLLEAGUE_CONNECTOR_PASSPHRASE: PASSPHRASE };
  assert.throws(() => loadConnectorConfig({ root, env: { ...env, COLLEAGUE_CONNECTOR_URL: 'http://colleague.example.com' } }), /https/);
  assert.throws(() => loadConnectorConfig({ root, env: { ...env, COLLEAGUE_CONNECTOR_URL: 'https://colleague.example.com/mcp' } }), /no path/);
  assert.throws(() => loadConnectorConfig({ root, env: { ...env, COLLEAGUE_CONNECTOR_PASSPHRASE: 'short' } }), /at least 12/);
  assert.throws(() => loadConnectorConfig({ root, env: { ...env, COLLEAGUE_CONNECTOR_ALLOWED_ORIGINS: 'nonsense' } }), /invalid origin/);

  await fs.writeFile(path.join(root, '.env'), `COLLEAGUE_CONNECTOR_URL=https://colleague.example.com/\nCOLLEAGUE_CONNECTOR_PASSPHRASE=${PASSPHRASE}\n`);
  const fromFile = loadConnectorConfig({ root, env: {} });
  assert.equal(fromFile.baseUrl, 'https://colleague.example.com');
  assert.equal(fromFile.passphrase, PASSPHRASE);
  assert.equal(fromFile.port, 8767);
  const overridden = loadConnectorConfig({ root, env: { COLLEAGUE_CONNECTOR_URL: 'http://127.0.0.1:9000', COLLEAGUE_CONNECTOR_PORT: '9000' } });
  assert.equal(overridden.baseUrl, 'http://127.0.0.1:9000');
  assert.equal(overridden.port, 9000);

  const empty = await tempRoot(t);
  const childEnv = { COLLEAGUE_ROOT: empty, COLLEAGUE_CONNECTOR_URL: '', COLLEAGUE_CONNECTOR_PASSPHRASE: '' };
  const refused = await runCli([remote], childEnv);
  assert.equal(refused.code, 1);
  assert.match(refused.stderr, /cannot start/);
  assert.match(refused.stderr, /COLLEAGUE_CONNECTOR_URL is not set/);
});
