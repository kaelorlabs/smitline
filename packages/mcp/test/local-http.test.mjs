import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import { statSync } from 'node:fs';
import http from 'node:http';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';
import { createLocalMcpHandler, localMcpUrl } from '../src/local-http.mjs';
import { localMcpTokenPath, readOrCreateLocalMcpToken } from '../src/local-token.mjs';
import { CALL_TOOL_DEFINITIONS } from '../src/session.mjs';

const TOKEN = 'local-test-token-0123456789abcdefghijklmnopq';
const INITIALIZE = {
  jsonrpc: '2.0', id: 1, method: 'initialize',
  params: { protocolVersion: '2025-06-18', capabilities: {}, clientInfo: { name: 'test', version: '1' } },
};

async function tempRoot(t) {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'colleague-local-mcp-'));
  t.after(() => fs.rm(root, { recursive: true, force: true }));
  return root;
}

function fakeColleague() {
  return {
    async listVoices() { return { default: 'marin', voices: ['marin'] }; },
  };
}

async function startLocal(t, options = {}) {
  const handler = createLocalMcpHandler({ token: TOKEN, colleague: fakeColleague(), ...options });
  const server = http.createServer(handler);
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  t.after(() => new Promise((resolve) => { server.close(resolve); server.closeAllConnections(); }));
  return { handler, port: server.address().port, base: `http://127.0.0.1:${server.address().port}` };
}

// Raw requests, so the Host and Origin headers are exactly what the test says.
function request(port, { method = 'POST', headers = {}, body } = {}) {
  return new Promise((resolve, reject) => {
    const text = body === undefined ? undefined : (typeof body === 'string' ? body : JSON.stringify(body));
    const req = http.request({
      host: '127.0.0.1', port, path: '/mcp', method,
      headers: {
        Host: `127.0.0.1:${port}`,
        ...(text !== undefined ? { 'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(text) } : {}),
        ...headers,
      },
    }, (response) => {
      let data = '';
      response.on('data', (chunk) => { data += chunk; });
      response.on('end', () => resolve({ status: response.statusCode, headers: response.headers, body: data ? JSON.parse(data) : null }));
    });
    req.on('error', reject);
    req.end(text);
  });
}

const auth = (token = TOKEN) => ({ Authorization: `Bearer ${token}` });

test('local MCP needs the bearer token', async (t) => {
  const { port } = await startLocal(t);
  const missing = await request(port, { body: INITIALIZE });
  assert.equal(missing.status, 401);
  assert.match(missing.headers['www-authenticate'], /^Bearer/);
  assert.equal((await request(port, { body: INITIALIZE, headers: auth('wrong') })).status, 401);
  assert.equal((await request(port, { body: INITIALIZE, headers: auth(`${TOKEN}x`) })).status, 401);
  assert.equal((await request(port, { body: INITIALIZE, headers: { Authorization: TOKEN } })).status, 401);
  assert.equal((await request(port, { body: INITIALIZE, headers: auth() })).status, 200);
});

test('local MCP refuses browsers and other host names', async (t) => {
  const { port } = await startLocal(t);
  const browser = await request(port, { body: INITIALIZE, headers: { ...auth(), Origin: `http://127.0.0.1:${port}` } });
  assert.equal(browser.status, 403);
  assert.equal((await request(port, { body: INITIALIZE, headers: { ...auth(), Origin: 'null' } })).status, 403);
  for (const host of [`evil.example:${port}`, `127.0.0.1.nip.io:${port}`, '127.0.0.1:1', '127.0.0.1', `[::1]:${port}`]) {
    const rebound = await request(port, { body: INITIALIZE, headers: { ...auth(), Host: host } });
    assert.equal(rebound.status, 421, host);
  }
  assert.equal((await request(port, { body: INITIALIZE, headers: { ...auth(), Host: `localhost:${port}` } })).status, 200);
});

test('local MCP sessions: initialize, the call tools, notifications, and DELETE', async (t) => {
  const { port, handler } = await startLocal(t);
  const opened = await request(port, { body: INITIALIZE, headers: auth() });
  assert.equal(opened.status, 200);
  assert.equal(opened.body.result.serverInfo.name, 'colleague-ai');
  assert.equal(opened.body.result.protocolVersion, '2025-06-18');
  const session = opened.headers['mcp-session-id'];
  assert.match(session, /^[A-Za-z0-9_-]{32}$/);

  const withSession = { ...auth(), 'Mcp-Session-Id': session };
  assert.equal((await request(port, { body: { jsonrpc: '2.0', id: 2, method: 'tools/list' }, headers: auth() })).status, 400);
  assert.equal((await request(port, { body: { jsonrpc: '2.0', id: 2, method: 'tools/list' }, headers: { ...auth(), 'Mcp-Session-Id': 'nope' } })).status, 404);
  const notified = await request(port, { body: { jsonrpc: '2.0', method: 'notifications/initialized' }, headers: withSession });
  assert.equal(notified.status, 202);
  const listed = await request(port, { body: { jsonrpc: '2.0', id: 2, method: 'tools/list' }, headers: { ...withSession, 'MCP-Protocol-Version': '2025-06-18' } });
  assert.equal(listed.status, 200);
  assert.equal(listed.body.result.tools.length, 11);
  assert.deepEqual(listed.body.result.tools.map((tool) => tool.name), CALL_TOOL_DEFINITIONS.map((tool) => tool.name));
  const batch = await request(port, {
    body: [{ jsonrpc: '2.0', id: 3, method: 'ping' }, { jsonrpc: '2.0', id: 4, method: 'tools/call', params: { name: 'list_voices', arguments: {} } }],
    headers: withSession,
  });
  assert.equal(batch.body.length, 2);
  assert.deepEqual(batch.body[1].result.structuredContent, { default: 'marin', voices: ['marin'] });
  assert.equal((await request(port, { body: { jsonrpc: '2.0', id: 5, method: 'ping' }, headers: { ...withSession, 'MCP-Protocol-Version': '1999-01-01' } })).status, 400);
  assert.equal((await request(port, { body: 'not json', headers: withSession })).body.error.code, -32700);
  assert.equal((await request(port, { method: 'GET', headers: withSession })).status, 405);
  const big = await request(port, { body: JSON.stringify({ jsonrpc: '2.0', id: 6, method: 'ping', params: { pad: 'x'.repeat(1024 * 1024) } }), headers: withSession });
  assert.equal(big.status, 413);

  assert.equal(handler.sessions.size, 1);
  assert.equal((await request(port, { method: 'DELETE', headers: withSession })).status, 204);
  assert.equal(handler.sessions.size, 0);
  assert.equal((await request(port, { body: { jsonrpc: '2.0', id: 7, method: 'ping' }, headers: withSession })).status, 404);
});

test('local MCP keeps at most maxSessions, dropping the least recently used', async (t) => {
  const { port, handler } = await startLocal(t, { maxSessions: 2 });
  const ids = [];
  for (let i = 0; i < 3; i += 1) ids.push((await request(port, { body: INITIALIZE, headers: auth() })).headers['mcp-session-id']);
  assert.equal(handler.sessions.size, 2);
  assert.equal((await request(port, { body: { jsonrpc: '2.0', id: 2, method: 'ping' }, headers: { ...auth(), 'Mcp-Session-Id': ids[0] } })).status, 404);
  assert.equal((await request(port, { body: { jsonrpc: '2.0', id: 2, method: 'ping' }, headers: { ...auth(), 'Mcp-Session-Id': ids[2] } })).status, 200);
});

test('by default the token comes from <root>/.colleague/mcp.token', async (t) => {
  const root = await tempRoot(t);
  const { port } = await startLocal(t, { token: undefined, root });
  assert.equal((await request(port, { body: INITIALIZE, headers: auth() })).status, 401);
  const token = (await fs.readFile(localMcpTokenPath(root), 'utf8')).trim();
  assert.equal((await request(port, { body: INITIALIZE, headers: auth(token) })).status, 200);
  assert.equal(localMcpUrl(), 'http://127.0.0.1:8095/mcp');
});

test('the local MCP token is made once, private, and stable', async (t) => {
  const root = await tempRoot(t);
  const token = readOrCreateLocalMcpToken(root);
  assert.match(token, /^[A-Za-z0-9_-]{43}$/);
  const file = path.join(root, '.colleague', 'mcp.token');
  assert.equal(localMcpTokenPath(root), file);
  assert.equal(statSync(file).mode & 0o777, 0o600);
  assert.equal(statSync(path.dirname(file)).mode & 0o777, 0o700);
  assert.equal(readOrCreateLocalMcpToken(root), token);
  assert.equal((await fs.readFile(file, 'utf8')).trim(), token);
  assert.deepEqual((await fs.readdir(path.dirname(file))).sort(), ['mcp.token']);
  // An empty file is replaced.
  await fs.writeFile(file, '');
  const replaced = readOrCreateLocalMcpToken(root);
  assert.match(replaced, /^[A-Za-z0-9_-]{43}$/);
  assert.equal(readOrCreateLocalMcpToken(root), replaced);
  // Another data root has its own token.
  assert.notEqual(readOrCreateLocalMcpToken(await tempRoot(t)), replaced);
});
