// MCP Streamable HTTP pieces shared by the remote connector (OAuth, remote.mjs) and
// the local endpoint the console serves at /mcp (bearer token, local-http.mjs):
// JSON-RPC over POST (single or batch), sessions opened by initialize and named by
// the Mcp-Session-Id header, DELETE to close one. No SSE stream is offered.
import crypto from 'node:crypto';

export const PROTOCOL_VERSIONS = Object.freeze(['2025-06-18', '2025-03-26']);
export const MAX_BODY = 1024 * 1024;

export function httpError(status, message) {
  return Object.assign(new Error(message), { status });
}

export function readBody(request, limit = MAX_BODY) {
  return new Promise((resolve, reject) => {
    if (Number(request.headers['content-length'] || 0) > limit) {
      reject(httpError(413, 'The request body is larger than 1 MB'));
      return;
    }
    const chunks = [];
    let size = 0;
    request.on('data', (chunk) => {
      if (size > limit) return;
      size += chunk.length;
      if (size > limit) {
        request.pause();
        reject(httpError(413, 'The request body is larger than 1 MB'));
        return;
      }
      chunks.push(chunk);
    });
    request.on('end', () => resolve(Buffer.concat(chunks).toString('utf8')));
    request.on('error', reject);
  });
}

export function mediaType(request) {
  return String(request.headers['content-type'] || '').split(';')[0].trim().toLowerCase();
}

export function send(response, status, body, headers = {}) {
  const text = body === undefined ? '' : JSON.stringify(body);
  response.writeHead(status, {
    'Cache-Control': 'no-store',
    'X-Content-Type-Options': 'nosniff',
    ...(text ? { 'Content-Type': 'application/json' } : {}),
    ...headers,
  });
  response.end(text);
}

export function rpcError(id, code, message) {
  return { jsonrpc: '2.0', id: id ?? null, error: { code, message } };
}

/** Constant-time string comparison (false when the lengths differ). */
export function safeEqual(a, b) {
  const left = Buffer.from(String(a));
  const right = Buffer.from(String(b));
  return left.length === right.length && crypto.timingSafeEqual(left, right);
}

export function classify(message) {
  if (!message || typeof message !== 'object' || Array.isArray(message) || message.jsonrpc !== '2.0') return 'invalid';
  if (typeof message.method === 'string') return Object.hasOwn(message, 'id') ? 'request' : 'notification';
  if (Object.hasOwn(message, 'result') || Object.hasOwn(message, 'error')) return 'response';
  return 'invalid';
}

/**
 * MCP sessions kept in least-recently-used order, at most maxSessions, dropped after
 * idleMs without use. Each belongs to an owner (an OAuth grant, or the local token);
 * another owner cannot use it.
 */
export function createSessionStore({ createSession, now = Date.now, maxSessions = 500, idleMs = 24 * 60 * 60_000 } = {}) {
  const sessions = new Map();
  return {
    sessions,
    open(owner) {
      const cutoff = now() - idleMs;
      for (const [id, entry] of sessions) if (entry.lastSeen < cutoff) sessions.delete(id);
      while (sessions.size >= maxSessions) sessions.delete(sessions.keys().next().value);
      return {
        id: crypto.randomBytes(24).toString('base64url'),
        owner,
        lastSeen: now(),
        session: createSession(),
      };
    },
    add(entry) {
      sessions.set(entry.id, entry);
    },
    find(request, owner) {
      const id = request.headers['mcp-session-id'];
      if (!id) return { status: 400, message: 'The Mcp-Session-Id header is required' };
      const entry = sessions.get(id);
      if (!entry || entry.owner !== owner) return { status: 404, message: 'Session not found; initialize a new session' };
      // Keep the map in least-recently-used order.
      sessions.delete(id);
      sessions.set(id, entry);
      entry.lastSeen = now();
      return { entry };
    },
    remove(id) {
      sessions.delete(id);
    },
  };
}

/** POST /mcp: one JSON-RPC message or a batch. initialize opens a session and must come alone. */
export async function handleMcpPost(request, response, { store, owner, label, log = () => {} }) {
  if (mediaType(request) !== 'application/json') {
    return send(response, 415, rpcError(null, -32700, 'Content-Type must be application/json'));
  }
  let payload;
  try {
    payload = JSON.parse(await readBody(request));
  } catch (error) {
    if (error.status) throw error;
    return send(response, 400, rpcError(null, -32700, 'Parse error'));
  }
  const batch = Array.isArray(payload);
  const messages = batch ? payload : [payload];
  if (!messages.length) return send(response, 400, rpcError(null, -32600, 'Empty batch'));
  const initializing = messages.some((message) => message?.method === 'initialize');
  let entry;
  if (initializing) {
    if (messages.length > 1) return send(response, 400, rpcError(null, -32600, 'initialize must be sent on its own'));
    entry = store.open(owner);
  } else {
    const found = store.find(request, owner);
    if (!found.entry) return send(response, found.status, rpcError(null, -32000, found.message));
    entry = found.entry;
  }
  const replies = [];
  for (const message of messages) {
    const kind = classify(message);
    if (kind === 'invalid') replies.push(rpcError(message?.id, -32600, 'Invalid Request'));
    if (kind === 'notification') await entry.session.dispatch(message);
    if (kind !== 'request') continue;
    if (message.method === 'tools/call') {
      log(`tool ${String(message.params?.name).replace(/[^\w.-]/g, '?').slice(0, 64)} for ${label}`);
    }
    replies.push(await entry.session.dispatch(message));
  }
  const headers = {};
  if (initializing) {
    store.add(entry);
    headers['Mcp-Session-Id'] = entry.id;
    log(`session opened for ${label}`);
  }
  if (!replies.length) return send(response, 202, undefined, headers);
  return send(response, 200, batch ? replies : replies[0], headers);
}

/** Everything after authentication: protocol version, then POST, DELETE, or 405. */
export async function handleMcpRequest(request, response, options) {
  const version = request.headers['mcp-protocol-version'];
  if (version !== undefined && !PROTOCOL_VERSIONS.includes(version)) {
    return send(response, 400, rpcError(null, -32600, `Unsupported MCP-Protocol-Version; use ${PROTOCOL_VERSIONS.join(' or ')}`));
  }
  if (request.method === 'POST') return handleMcpPost(request, response, options);
  if (request.method === 'DELETE') {
    const found = options.store.find(request, options.owner);
    if (!found.entry) return send(response, found.status, rpcError(null, -32000, found.message));
    options.store.remove(found.entry.id);
    return send(response, 204);
  }
  return send(response, 405, rpcError(null, -32000, 'Use POST; this server does not offer an SSE stream'), { Allow: 'POST, DELETE' });
}
