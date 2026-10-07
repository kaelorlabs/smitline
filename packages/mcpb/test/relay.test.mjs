import assert from 'node:assert/strict';
import fs from 'node:fs';
import { spawn } from 'node:child_process';
import http from 'node:http';
import test from 'node:test';
import { fileURLToPath } from 'node:url';

import { snapshot } from '../build.mjs';
import { createRelay } from '../server/index.mjs';

const SERVER = fileURLToPath(new URL('../server/index.mjs', import.meta.url));

test('tools.json matches the MCP server', async () => {
  assert.equal(fs.readFileSync(new URL('../server/tools.json', import.meta.url), 'utf8'), await snapshot(),
    'run node packages/mcpb/build.mjs');
});

test('relays to the local endpoint with the token and session, JSON or event stream', async (t) => {
  const seen = [];
  const server = http.createServer((request, response) => {
    let body = '';
    request.on('data', (chunk) => { body += chunk; });
    request.on('end', () => {
      const message = JSON.parse(body);
      seen.push({ auth: request.headers.authorization, session: request.headers['mcp-session-id'], origin: request.headers.origin, method: message.method });
      if (message.method === 'initialize') {
        response.writeHead(200, { 'Content-Type': 'application/json', 'Mcp-Session-Id': 's1' });
        response.end(JSON.stringify({ jsonrpc: '2.0', id: message.id, result: { serverInfo: { name: 'smitline' } } }));
      } else if (message.id === undefined) {
        response.writeHead(202).end();
      } else {
        response.writeHead(200, { 'Content-Type': 'text/event-stream' });
        response.end(`event: message\ndata: ${JSON.stringify({ jsonrpc: '2.0', id: message.id, result: { tools: [] } })}\n\n`);
      }
    });
  });
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  t.after(() => server.close());
  const out = [];
  const handle = createRelay({ url: `http://127.0.0.1:${server.address().port}/mcp`, token: 'tok', write: (m) => out.push(m) });
  await handle({ jsonrpc: '2.0', id: 1, method: 'initialize', params: {} });
  await handle({ jsonrpc: '2.0', method: 'notifications/initialized' });
  await handle({ jsonrpc: '2.0', id: 2, method: 'tools/list' });
  assert.deepEqual(out.map((m) => m.id), [1, 2]);
  assert.deepEqual(out[1].result, { tools: [] });
  assert.deepEqual(seen.map((s) => [s.method, s.auth, s.session, s.origin]), [
    ['initialize', 'Bearer tok', undefined, undefined],
    ['notifications/initialized', 'Bearer tok', 's1', undefined],
    ['tools/list', 'Bearer tok', 's1', undefined],
  ]);
});

test('without Smitline running, the bundle still lists the tools and explains tool calls', async () => {
  const child = spawn(process.execPath, [SERVER], { env: { ...process.env, SMITLINE_MCP_URL: 'http://127.0.0.1:9/mcp', SMITLINE_MCP_TOKEN: 'x' } });
  let stdout = '';
  child.stdout.on('data', (chunk) => { stdout += chunk; });
  const lines = [
    { jsonrpc: '2.0', id: 1, method: 'initialize', params: { protocolVersion: '2025-06-18', capabilities: {}, clientInfo: { name: 't', version: '1' } } },
    { jsonrpc: '2.0', method: 'notifications/initialized' },
    { jsonrpc: '2.0', id: 2, method: 'tools/list' },
    { jsonrpc: '2.0', id: 3, method: 'tools/call', params: { name: 'list_calls', arguments: {} } },
  ];
  child.stdin.end(lines.map((m) => JSON.stringify(m)).join('\n') + '\n');
  await new Promise((resolve) => child.on('close', resolve));
  const replies = Object.fromEntries(stdout.trim().split('\n').map((line) => JSON.parse(line)).map((m) => [m.id, m]));
  assert.equal(replies[1].result.serverInfo.name, 'smitline');
  assert.ok(replies[2].result.tools.some((tool) => tool.name === 'start_call'));
  assert.equal(replies[3].result.isError, true);
  assert.match(replies[3].result.content[0].text, /Smitline is not reachable at http:\/\/127\.0\.0\.1:9\/mcp/);
});

test('a refused token is reported as such', async (t) => {
  const server = http.createServer((request, response) => response.writeHead(401).end());
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  t.after(() => server.close());
  const out = [];
  const handle = createRelay({ url: `http://127.0.0.1:${server.address().port}/mcp`, token: 'wrong', write: (m) => out.push(m) });
  await handle({ jsonrpc: '2.0', id: 7, method: 'tools/call', params: { name: 'list_calls', arguments: {} } });
  assert.equal(out[0].result.isError, true);
  assert.match(out[0].result.content[0].text, /refused the token/);
});

test('with no token set, a tool call says to add it', async (t) => {
  const server = http.createServer((request, response) => response.writeHead(401).end());
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  t.after(() => server.close());
  const child = spawn(process.execPath, [SERVER], { env: { ...process.env, SMITLINE_MCP_URL: `http://127.0.0.1:${server.address().port}/mcp`, SMITLINE_MCP_TOKEN: '${user_config.token}' } });
  let stdout = '';
  child.stdout.on('data', (chunk) => { stdout += chunk; });
  child.stdin.end(`${JSON.stringify({ jsonrpc: '2.0', id: 1, method: 'tools/call', params: { name: 'list_calls', arguments: {} } })}\n`);
  await new Promise((resolve) => child.on('close', resolve));
  assert.match(JSON.parse(stdout.trim()).result.content[0].text, /No Smitline token is set/);
});

test('every tool has a title and annotations', async () => {
  const { tools } = JSON.parse(fs.readFileSync(new URL('../server/tools.json', import.meta.url), 'utf8'));
  for (const tool of tools) {
    assert.ok(tool.title, tool.name);
    assert.equal(typeof tool.annotations?.readOnlyHint, 'boolean', tool.name);
  }
});
