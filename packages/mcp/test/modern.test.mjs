// MCP revision 2026-07-28 next to the initialize-based revisions (modern.mjs): over HTTP (the local
// endpoint, and the bare transport), over stdio, and the remote connector's CORS preflight.
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import http from 'node:http';
import os from 'node:os';
import path from 'node:path';
import { PassThrough } from 'node:stream';
import test from 'node:test';
import { CALL_CARD_URI } from '../src/call-card.mjs';
import { createLocalMcpHandler } from '../src/local-http.mjs';
import { createConnector } from '../src/remote.mjs';
import { startStdioServer } from '../src/server.mjs';
import { CALLS_INSTRUCTIONS, CALL_TOOL_DEFINITIONS, MCP_SERVER_VERSION, createMcpSession } from '../src/session.mjs';
import { createSessionStore, handleMcpRequest } from '../src/streamable-http.mjs';

const TOKEN = 'modern-test-token-0123456789abcdefghijklmnop';
const MODERN = '2026-07-28';
const CALL_ID = 'call-0123456789abcdef';
const SERVER_INFO = { name: 'smitline', version: MCP_SERVER_VERSION };

function fakeColleague() {
  const seen = [];
  return {
    seen,
    async startCall(brief) { seen.push(['start', brief.to]); return { id: CALL_ID, status: 'queued' }; },
    async waitForCall(id, timeout) { seen.push(['wait', id, timeout]); return { id, status: 'completed', result: { outcome: 'achieved' } }; },
    async getCall(id) { seen.push(['get', id]); return { id, status: 'completed' }; },
    async listVoices() { return { default: 'marin', voices: ['marin'] }; },
  };
}

const META = Object.freeze({
  'io.modelcontextprotocol/protocolVersion': MODERN,
  'io.modelcontextprotocol/clientInfo': { name: 'test', version: '1' },
  'io.modelcontextprotocol/clientCapabilities': {},
});

function modern(method, params = {}, id = 1, meta = META) {
  return { jsonrpc: '2.0', id, method, params: { ...params, _meta: { ...meta } } };
}

/** The headers a modern client mirrors from the body. */
function mirrored(message) {
  const headers = { 'MCP-Protocol-Version': MODERN, 'Mcp-Method': message.method };
  if (message.method === 'tools/call') headers['Mcp-Name'] = message.params.name;
  if (message.method === 'resources/read') headers['Mcp-Name'] = message.params.uri;
  return headers;
}

function listen(handler, t) {
  const server = http.createServer(handler);
  return new Promise((resolve) => server.listen(0, '127.0.0.1', () => {
    t.after(() => new Promise((done) => { server.close(done); server.closeAllConnections(); }));
    resolve(server.address().port);
  }));
}

async function startLocal(t, options = {}) {
  const colleague = fakeColleague();
  const handler = createLocalMcpHandler({ token: TOKEN, colleague, ...options });
  return { handler, colleague, port: await listen(handler, t) };
}

function parseEvents(text) {
  return text.split(/\r?\n\r?\n/).filter((block) => block.trim()).map((block) => JSON.parse(
    block.split(/\r?\n/).filter((line) => line.startsWith('data:')).map((line) => line.slice(5).trimStart()).join('\n'),
  ));
}

// Raw requests, so the headers are exactly what the test says (no Origin).
function post(port, message, { headers, method = 'POST' } = {}) {
  return new Promise((resolve, reject) => {
    const text = message === undefined ? undefined : JSON.stringify(message);
    const req = http.request({
      host: '127.0.0.1', port, path: '/mcp', method,
      headers: {
        Host: `127.0.0.1:${port}`,
        Authorization: `Bearer ${TOKEN}`,
        Accept: 'application/json, text/event-stream',
        ...(text !== undefined ? { 'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(text) } : {}),
        ...(headers === undefined && message !== undefined ? mirrored(message) : headers),
      },
    }, (response) => {
      let data = '';
      response.on('data', (chunk) => { data += chunk; });
      response.on('end', () => {
        const events = String(response.headers['content-type']).startsWith('text/event-stream');
        resolve({
          status: response.statusCode,
          headers: response.headers,
          text: data,
          body: !data || events ? null : JSON.parse(data),
          events: events ? parseEvents(data) : null,
        });
      });
    });
    req.on('error', reject);
    req.end(text);
  });
}

test('a modern client needs no initialize: discovery, tools, a call end to end, and the card', async (t) => {
  const { port, handler, colleague } = await startLocal(t);

  const discover = await post(port, modern('server/discover'));
  assert.equal(discover.status, 200);
  assert.equal(discover.headers['content-type'], 'application/json');
  assert.equal(discover.headers['mcp-session-id'], undefined);
  assert.deepEqual(discover.body, {
    jsonrpc: '2.0',
    id: 1,
    result: {
      resultType: 'complete',
      supportedVersions: [MODERN],
      capabilities: { tools: { listChanged: false }, resources: { listChanged: false } },
      instructions: CALLS_INSTRUCTIONS,
      ttlMs: 300000,
      cacheScope: 'public',
      _meta: { 'io.modelcontextprotocol/serverInfo': SERVER_INFO },
    },
  });

  const listed = await post(port, modern('tools/list', {}, 2));
  assert.equal(listed.status, 200);
  assert.equal(listed.body.result.resultType, 'complete');
  assert.deepEqual(listed.body.result.tools.map((tool) => tool.name), CALL_TOOL_DEFINITIONS.map((tool) => tool.name));
  assert.equal(listed.body.result.ttlMs, 300000);
  assert.equal(listed.body.result.cacheScope, 'public');
  assert.deepEqual(listed.body.result._meta, { 'io.modelcontextprotocol/serverInfo': SERVER_INFO });

  // A stray Mcp-Session-Id is ignored, and none is minted.
  const started = await post(port, modern('tools/call', { name: 'start_call', arguments: { channel: 'phone', to: '+14155550142', objective: 'Book a table' } }, 3), {
    headers: { ...mirrored({ method: 'tools/call', params: { name: 'start_call' } }), 'Mcp-Session-Id': 'not-a-session' },
  });
  assert.equal(started.status, 200);
  assert.equal(started.headers['mcp-session-id'], undefined);
  assert.equal(started.body.result.resultType, 'complete');
  assert.equal(started.body.result.isError, false);
  assert.equal(started.body.result.structuredContent.id, CALL_ID);
  assert.deepEqual(JSON.parse(started.body.result.content[0].text), started.body.result.structuredContent);

  const waited = await post(port, modern('tools/call', { name: 'wait_for_call', arguments: { callId: CALL_ID, timeoutSeconds: 5 } }, 4));
  assert.equal(waited.body.result.structuredContent.result.outcome, 'achieved');
  const got = await post(port, modern('tools/call', { name: 'get_call', arguments: { callId: CALL_ID } }, 5));
  assert.equal(got.body.result.structuredContent.status, 'completed');
  assert.deepEqual(colleague.seen, [['start', '+14155550142'], ['wait', CALL_ID, 5], ['get', CALL_ID]]);

  // A tool's own failure is still a result the agent can read.
  const invalid = await post(port, modern('tools/call', { name: 'get_call', arguments: { callId: 'nope' } }, 6));
  assert.equal(invalid.status, 200);
  assert.equal(invalid.body.result.resultType, 'complete');
  assert.equal(invalid.body.result.isError, true);
  assert.equal(invalid.body.result.structuredContent.code, 'validation');

  const resources = await post(port, modern('resources/list', {}, 7));
  assert.deepEqual(resources.body.result.resources.map((resource) => resource.uri), [CALL_CARD_URI]);
  assert.equal(resources.body.result.cacheScope, 'public');
  const card = await post(port, modern('resources/read', { uri: CALL_CARD_URI }, 8));
  assert.equal(card.status, 200);
  assert.equal(card.body.result.contents[0].uri, CALL_CARD_URI);
  assert.equal(card.body.result.ttlMs, 300000);
  const templates = await post(port, modern('resources/templates/list', {}, 9));
  assert.deepEqual(templates.body.result.resourceTemplates, []);

  assert.equal(handler.sessions.size, 0, 'modern requests keep no session');
});

test('a modern request is checked against its headers and body', async (t) => {
  const { port, colleague } = await startLocal(t);
  const discover = modern('server/discover');
  const call = modern('tools/call', { name: 'list_voices', arguments: {} });
  const base64 = (text) => `=?base64?${Buffer.from(text, 'utf8').toString('base64')}?=`;
  const rows = [
    ['no MCP-Protocol-Version header', discover, { 'Mcp-Method': 'server/discover' }, 400, -32020],
    ['MCP-Protocol-Version other than _meta', discover, { ...mirrored(discover), 'MCP-Protocol-Version': '2025-11-25' }, 400, -32020],
    ['no Mcp-Method header', discover, { 'MCP-Protocol-Version': MODERN }, 400, -32020],
    ['Mcp-Method other than the body', discover, { ...mirrored(discover), 'Mcp-Method': 'tools/list' }, 400, -32020],
    ['tools/call without Mcp-Name', call, { 'MCP-Protocol-Version': MODERN, 'Mcp-Method': 'tools/call' }, 400, -32020],
    ['Mcp-Name other than the tool', call, { ...mirrored(call), 'Mcp-Name': 'start_call' }, 400, -32020],
    ['Mcp-Name not valid Base64', call, { ...mirrored(call), 'Mcp-Name': '=?base64?not base64!?=' }, 400, -32020],
    ['Mcp-Name in Base64', call, { ...mirrored(call), 'Mcp-Name': base64('list_voices') }, 200, null],
    ['no clientCapabilities', modern('server/discover', {}, 1, { 'io.modelcontextprotocol/protocolVersion': MODERN }), undefined, 400, -32602],
    ['a removed method (ping)', modern('ping'), undefined, 404, -32601],
    ['initialize in modern form', modern('initialize', { protocolVersion: '2025-11-25', capabilities: {} }), undefined, 404, -32601],
    ['an unknown method', modern('logging/setLevel', { level: 'info' }), undefined, 404, -32601],
    ['an unknown tool', modern('tools/call', { name: 'join_meeting', arguments: {} }), undefined, 200, -32602],
    ['an unknown resource (never -32002)', modern('resources/read', { uri: 'ui://smitline/other.html' }), undefined, 200, -32602],
    ['a null id', { ...discover, id: null }, undefined, 400, -32600],
  ];
  for (const [name, message, headers, status, code] of rows) {
    const reply = await post(port, message, { headers });
    assert.equal(reply.status, status, name);
    assert.equal(reply.body.error?.code ?? null, code, name);
    if (code !== null && code !== -32600) assert.equal(reply.body.id, message.id, name);
  }

  // A version this server does not speak, even an older one named in _meta, lists the ones it does.
  for (const version of ['2027-01-01', '2025-11-25']) {
    const message = modern('server/discover', {}, 1, { ...META, 'io.modelcontextprotocol/protocolVersion': version });
    const reply = await post(port, message, { headers: { ...mirrored(message), 'MCP-Protocol-Version': version } });
    assert.equal(reply.status, 400, version);
    assert.deepEqual(reply.body.error, {
      code: -32022, message: 'Unsupported protocol version', data: { supported: [MODERN], requested: version },
    }, version);
  }

  // One request per POST; a notification is accepted with nothing back.
  const batch = await post(port, [discover, modern('tools/list', {}, 2)], { headers: mirrored(discover) });
  assert.equal(batch.status, 400);
  assert.equal(batch.body.error.code, -32600);
  const notification = { jsonrpc: '2.0', method: 'notifications/progress', params: { _meta: { ...META } } };
  const accepted = await post(port, notification, { headers: mirrored(notification) });
  assert.equal(accepted.status, 202);
  assert.equal(accepted.text, '');

  // The modern version without the request naming it in _meta is a malformed modern request, and
  // the error carries the request's id.
  for (const params of [{}, { _meta: { 'io.modelcontextprotocol/clientCapabilities': {} } }]) {
    const unnamed = await post(port, { jsonrpc: '2.0', id: 11, method: 'tools/list', params }, {
      headers: { 'MCP-Protocol-Version': MODERN, 'Mcp-Method': 'tools/list' },
    });
    assert.equal(unnamed.status, 400);
    assert.equal(unnamed.body.error.code, -32602);
    assert.equal(unnamed.body.id, 11);
  }

  // A modern client has no session to delete and no stream to open.
  for (const method of ['GET', 'DELETE']) {
    const reply = await post(port, undefined, { method, headers: { 'MCP-Protocol-Version': MODERN } });
    assert.equal(reply.status, 405, method);
  }
  assert.deepEqual(colleague.seen, [], 'only list_voices ran, and it records nothing');
});

test('subscriptions/listen is acknowledged first, then ends gracefully', async (t) => {
  const { port } = await startLocal(t);
  const reply = await post(port, modern('subscriptions/listen', { notifications: { toolsListChanged: true } }, 'listen-1'));
  assert.equal(reply.status, 200);
  assert.equal(reply.headers['content-type'], 'text/event-stream');
  assert.equal(reply.headers['x-accel-buffering'], 'no');
  assert.deepEqual(reply.events, [
    {
      jsonrpc: '2.0',
      method: 'notifications/subscriptions/acknowledged',
      params: { _meta: { 'io.modelcontextprotocol/subscriptionId': 'listen-1' }, notifications: {} },
    },
    {
      jsonrpc: '2.0',
      id: 'listen-1',
      result: {
        resultType: 'complete',
        _meta: { 'io.modelcontextprotocol/subscriptionId': 'listen-1', 'io.modelcontextprotocol/serverInfo': SERVER_INFO },
      },
    },
  ]);
});

test('both eras on one endpoint: a legacy session and modern requests side by side', async (t) => {
  const { port, handler } = await startLocal(t);
  const initialize = {
    jsonrpc: '2.0', id: 1, method: 'initialize',
    params: { protocolVersion: '2025-11-25', capabilities: {}, clientInfo: { name: 'legacy', version: '1' } },
  };
  // A dual-era client that falls back may still send the modern header with initialize.
  const init = await post(port, initialize, { headers: { 'MCP-Protocol-Version': MODERN } });
  assert.equal(init.status, 200);
  assert.equal(init.body.result.protocolVersion, '2025-11-25');
  const session = init.headers['mcp-session-id'];
  assert.ok(session);
  const legacy = { 'Mcp-Session-Id': session, 'MCP-Protocol-Version': '2025-11-25' };

  const modernList = await post(port, modern('tools/list', {}, 2));
  const legacyList = await post(port, { jsonrpc: '2.0', id: 3, method: 'tools/list', params: { _meta: { progressToken: 'p1' } } }, { headers: legacy });
  assert.equal(modernList.body.result.resultType, 'complete');
  assert.equal(legacyList.status, 200);
  assert.equal(legacyList.body.result.resultType, undefined, 'legacy results are as before');
  assert.deepEqual(legacyList.body.result.tools, modernList.body.result.tools);
  const ping = await post(port, { jsonrpc: '2.0', id: 4, method: 'ping' }, { headers: legacy });
  assert.deepEqual(ping.body.result, {}, 'ping is still answered in a legacy session');
  assert.equal(handler.sessions.size, 1);
});

test('with modern requests off, or no handler for them, a dual-era client is sent back to initialize', async (t) => {
  const colleague = fakeColleague();
  const stores = {
    off: createSessionStore({ createSession: () => createMcpSession({ colleague, log() {} }) }),
    // As the hosted gateway made its store before 2026-07-28: sessions are made in open().
    'no handler': createSessionStore({ createSession: () => null }),
  };
  for (const [name, store] of Object.entries(stores)) {
    const port = await listen((request, response) => handleMcpRequest(request, response, {
      store, owner: 'o', label: 'test', modern: name !== 'off',
    }), t);
    const reply = await post(port, modern('server/discover'));
    assert.equal(reply.status, 400, name);
    assert.equal(reply.body.error.code, -32600, name);
    assert.match(reply.body.error.message, /use one of 2025-11-25, 2025-06-18, 2025-03-26$/, name);
  }
});

test('stdio serves both eras in one process, with errors in-band and cancellation honored', async () => {
  const stdin = new PassThrough();
  const stdout = new PassThrough();
  const out = [];
  let buffer = '';
  stdout.on('data', (chunk) => {
    buffer += chunk;
    let nl;
    while ((nl = buffer.indexOf('\n')) !== -1) {
      out.push(JSON.parse(buffer.slice(0, nl)));
      buffer = buffer.slice(nl + 1);
    }
  });
  let release;
  const colleague = {
    ...fakeColleague(),
    waitForCall: (id) => new Promise((resolve) => { release = () => resolve({ id, status: 'completed' }); }),
  };
  const { session } = startStdioServer({ stdin, stdout, colleague, installSignals: false, log() {} });
  const write = (message) => stdin.write(`${JSON.stringify(message)}\n`);
  const settle = () => new Promise((resolve) => setTimeout(resolve, 30));

  write(modern('server/discover', {}, 'd1'));
  write({ jsonrpc: '2.0', id: 'i1', method: 'initialize', params: { protocolVersion: '2025-11-25', capabilities: {} } });
  write({ jsonrpc: '2.0', id: 'l1', method: 'tools/list', params: {} });
  write(modern('server/discover', {}, 'v1', { ...META, 'io.modelcontextprotocol/protocolVersion': '2099-01-01' }));
  write(modern('ping', {}, 'p1'));
  write(modern('initialize', { protocolVersion: '2025-11-25', capabilities: {} }, 'm1'));
  write(modern('subscriptions/listen', { notifications: { toolsListChanged: true } }, 's1'));
  write(modern('tools/call', { name: 'wait_for_call', arguments: { callId: CALL_ID } }, 'w1'));
  await settle();
  write({ jsonrpc: '2.0', method: 'notifications/cancelled', params: { requestId: 'w1', reason: 'the user stopped it' } });
  await settle();
  release();
  await settle();
  await session.shutdown();

  const byId = Object.fromEntries(out.filter((message) => message.id !== undefined).map((message) => [message.id, message]));
  assert.deepEqual(byId.d1.result.supportedVersions, [MODERN]);
  assert.equal(byId.i1.result.protocolVersion, '2025-11-25');
  assert.equal(byId.l1.result.resultType, undefined);
  assert.equal(byId.v1.error.code, -32022);
  assert.deepEqual(byId.v1.error.data, { supported: [MODERN], requested: '2099-01-01' });
  assert.equal(byId.p1.error.code, -32601);
  assert.equal(byId.m1.error.code, -32601, 'an initialize in modern form opens no legacy session');
  assert.equal(byId.s1.result.resultType, 'complete');
  const ack = out.findIndex((message) => message.method === 'notifications/subscriptions/acknowledged');
  assert.ok(ack !== -1 && ack < out.indexOf(byId.s1), 'the acknowledgment comes first');
  assert.equal(out[ack].params._meta['io.modelcontextprotocol/subscriptionId'], 's1');
  assert.equal(byId.w1, undefined, 'nothing is sent for a cancelled request');
});

test('the remote connector lets browsers send the 2026-07-28 headers', async (t) => {
  const inspector = 'http://localhost:6274';
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'smitline-modern-'));
  t.after(() => fs.rm(root, { recursive: true, force: true }));
  const connector = createConnector({
    root,
    passphrase: 'correct horse battery staple',
    colleague: fakeColleague(),
    allowedOrigins: [inspector],
    log() {},
  });
  await connector.listen(0, '127.0.0.1');
  t.after(() => connector.close());
  const preflight = await fetch(`${connector.url}/mcp`, {
    method: 'OPTIONS',
    headers: { Origin: inspector, 'Access-Control-Request-Method': 'POST', 'Access-Control-Request-Headers': 'mcp-method, mcp-name' },
  });
  assert.equal(preflight.status, 204);
  const allowed = preflight.headers.get('access-control-allow-headers').split(/,\s*/).map((name) => name.toLowerCase());
  for (const name of ['mcp-protocol-version', 'mcp-method', 'mcp-name']) assert.ok(allowed.includes(name), name);
});
