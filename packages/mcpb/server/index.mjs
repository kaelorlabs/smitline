#!/usr/bin/env node
// Smitline's MCP bundle: relays MCP between a stdio client (Claude Desktop, Smithery, and other
// MCPB hosts) and the Smitline container's local endpoint, http://127.0.0.1:8095/mcp, with its
// bearer token. No dependencies, so the bundle is these few files.
//
// When Smitline is not running, initialize and tools/list are answered from the snapshot in
// tools.json (written by build.mjs from the real server), so clients still see the tools, and a
// tool call says how to start Smitline.
import fs from 'node:fs';
import path from 'node:path';
import readline from 'node:readline';
import { fileURLToPath } from 'node:url';

const HERE = new URL('.', import.meta.url);
const SNAPSHOT = JSON.parse(fs.readFileSync(new URL('tools.json', HERE), 'utf8'));
const NOT_RUNNING = 'Smitline is not reachable at {url}. Start the smitline container '
  + '(https://github.com/kaelorlabs/smitline#readme), and check the token: '
  + 'docker exec smitline smitline setup register --json prints it.';

export function createRelay({ url, token, fetchImpl = fetch, write }) {
  let sessionId = null;

  function local(message, error) {
    const { id, method } = message;
    if (id === undefined || id === null) return null;
    if (method === 'initialize') {
      return { jsonrpc: '2.0', id, result: { ...SNAPSHOT.initialize, protocolVersion: message.params?.protocolVersion || SNAPSHOT.initialize.protocolVersion } };
    }
    if (method === 'tools/list') return { jsonrpc: '2.0', id, result: { tools: SNAPSHOT.tools } };
    if (method === 'ping') return { jsonrpc: '2.0', id, result: {} };
    if (method === 'tools/call') {
      const text = !/token was refused/.test(error) ? `${NOT_RUNNING.replace('{url}', url)} (${error})`
        : token ? `Smitline at ${url} refused the token. Copy it again into this bundle's settings: docker exec smitline smitline setup register --json prints it.`
          : `No Smitline token is set. Add it in this bundle's settings: docker exec smitline smitline setup register --json prints it.`;
      return { jsonrpc: '2.0', id, result: { isError: true, content: [{ type: 'text', text }] } };
    }
    return { jsonrpc: '2.0', id, error: { code: -32603, message: NOT_RUNNING.replace('{url}', url) } };
  }

  async function forward(message) {
    const headers = { 'Content-Type': 'application/json', Accept: 'application/json, text/event-stream' };
    if (token) headers.Authorization = `Bearer ${token}`;
    if (sessionId) headers['Mcp-Session-Id'] = sessionId;
    const response = await fetchImpl(url, { method: 'POST', headers, body: JSON.stringify(message) });
    if (response.status === 401 || response.status === 403) {
      throw new Error(`the token was refused (HTTP ${response.status})`);
    }
    if (response.status === 404 && sessionId) {
      sessionId = null; // The server restarted; the client initializes again.
    }
    sessionId = response.headers.get('mcp-session-id') || sessionId;
    const type = response.headers.get('content-type') || '';
    const text = await response.text();
    if (!text.trim()) return;
    if (type.includes('text/event-stream')) {
      for (const block of text.split(/\r?\n\r?\n/)) {
        const data = block.split(/\r?\n/).filter((line) => line.startsWith('data:')).map((line) => line.slice(5).trimStart()).join('\n');
        if (data) write(JSON.parse(data));
      }
      return;
    }
    if (!response.ok && !type.includes('json')) throw new Error(`HTTP ${response.status}`);
    write(JSON.parse(text));
  }

  return async function handle(message) {
    try {
      await forward(message);
    } catch (error) {
      const reply = local(message, error.message || String(error));
      if (reply) write(reply);
    }
  };
}

function main() {
  // An unset optional setting can arrive as its literal placeholder, such as ${user_config.token}.
  const setting = (name) => {
    const value = (process.env[name] || '').trim();
    return value.startsWith('${') ? '' : value;
  };
  const url = setting('SMITLINE_MCP_URL') || 'http://127.0.0.1:8095/mcp';
  const token = setting('SMITLINE_MCP_TOKEN');
  const handle = createRelay({ url, token, write: (message) => process.stdout.write(`${JSON.stringify(message)}\n`) });
  // The session id comes back with the reply to initialize and every later message must carry
  // it, so messages wait for the latest initialize; after that they run side by side, so a long
  // wait_for_call does not hold up the rest.
  let initialized = Promise.resolve();
  const pending = new Set();
  const lines = readline.createInterface({ input: process.stdin });
  lines.on('line', (line) => {
    if (!line.trim()) return;
    let message;
    try {
      message = JSON.parse(line);
    } catch {
      process.stderr.write('smitline bundle: ignored a line that is not JSON\n');
      return;
    }
    const work = message.method === 'initialize'
      ? (initialized = initialized.then(() => handle(message)))
      : initialized.then(() => handle(message));
    pending.add(work);
    work.finally(() => pending.delete(work));
  });
  lines.on('close', async () => {
    await Promise.allSettled([...pending]);
    process.exit(0);
  });
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) main();
