// Local MCP over Streamable HTTP, served by the console at http://127.0.0.1:8095/mcp.
// Agents on this computer (Claude Code, Codex, Cursor) connect with the bearer token in
// <COLLEAGUE_ROOT>/.colleague/mcp.token instead of starting a stdio server. There is no
// OAuth: the token is the only credential, browsers are refused (any Origin header),
// and only 127.0.0.1 or localhost Host names on the listening port are answered, so a
// page on a rebound DNS name cannot reach it.
import crypto from 'node:crypto';
import { fileURLToPath } from 'node:url';
import { Colleague, redact } from '../../sdk-typescript/src/index.mjs';
import { createMcpSession } from './session.mjs';
import { readOrCreateLocalMcpToken } from './local-token.mjs';
import {
  PROTOCOL_VERSIONS, createSessionStore, handleMcpRequest, rpcError, send,
} from './streamable-http.mjs';

export const LOCAL_MCP_PATH = '/mcp';
export const DEFAULT_CONSOLE_PORT = 8095;
const MAX_LOCAL_SESSIONS = 100;
const LOCAL_SESSION_IDLE_MS = 24 * 60 * 60_000;
const CODE_ROOT = fileURLToPath(new URL('../../../', import.meta.url));

export function localMcpUrl(port = DEFAULT_CONSOLE_PORT) {
  return `http://127.0.0.1:${port}${LOCAL_MCP_PATH}`;
}

function digest(value) {
  return crypto.createHash('sha256').update(String(value)).digest();
}

/** Constant time: both sides are hashed to the same length first. */
function tokenMatches(given, expected) {
  if (!given || !expected) return false;
  return crypto.timingSafeEqual(digest(given), digest(expected));
}

/**
 * Returns handler(request, response) for requests to /mcp.
 *
 * options.token      the expected token, or a function returning it; by default the
 *                    token file under root is read (and made) on each request, so a
 *                    replaced file takes effect at once.
 * options.colleague  the SDK client the call tools use; by default one that talks to
 *                    the loopback daemon with the token in <root>/.colleague/daemon.auth.
 * options.port       the port the Host header must name; by default the port the
 *                    request arrived on.
 */
export function createLocalMcpHandler({
  colleague,
  root = process.env.COLLEAGUE_ROOT || CODE_ROOT,
  codeRoot = CODE_ROOT,
  token,
  port,
  log = () => {},
  now = Date.now,
  maxSessions = MAX_LOCAL_SESSIONS,
  idleMs = LOCAL_SESSION_IDLE_MS,
} = {}) {
  const expectedToken = typeof token === 'function' ? token
    : token ? () => token
      : () => readOrCreateLocalMcpToken(root);
  let client = colleague || null;
  const calls = () => {
    client ||= new Colleague({ root, codeRoot, host: '127.0.0.1', port: process.env.COLLEAGUE_DAEMON_PORT });
    return client;
  };
  const store = createSessionStore({
    now,
    maxSessions,
    idleMs,
    createSession: () => createMcpSession({ colleague: calls(), protocolVersions: PROTOCOL_VERSIONS, notify() {}, log }),
  });

  function hostAllowed(request) {
    const listening = port || request.socket?.localPort;
    const host = String(request.headers.host || '').toLowerCase();
    return Boolean(listening) && (host === `127.0.0.1:${listening}` || host === `localhost:${listening}`);
  }

  function authorized(request) {
    const match = /^Bearer\s+(\S+)\s*$/i.exec(request.headers.authorization || '');
    return Boolean(match) && tokenMatches(match[1], expectedToken());
  }

  async function handle(request, response) {
    try {
      if (!hostAllowed(request)) {
        return send(response, 421, rpcError(null, -32000, 'Use http://127.0.0.1 or http://localhost with this port'));
      }
      if (request.headers.origin !== undefined) {
        return send(response, 403, rpcError(null, -32000, 'Browsers cannot use this endpoint'));
      }
      if (!authorized(request)) {
        return send(response, 401, rpcError(null, -32001, 'A valid bearer token is required; see smitline setup register'), {
          'WWW-Authenticate': 'Bearer realm="smitline"',
        });
      }
      return await handleMcpRequest(request, response, { store, owner: 'local', label: 'a local agent', log });
    } catch (error) {
      if (response.headersSent) {
        response.destroy();
        return undefined;
      }
      if (error.status === 413) return send(response, 413, rpcError(null, -32600, error.message), { Connection: 'close' });
      log(`internal error: ${redact(error.message)}`);
      return send(response, 500, rpcError(null, -32603, 'Internal error'));
    }
  }
  handle.sessions = store.sessions;
  return handle;
}
