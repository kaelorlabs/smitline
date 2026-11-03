// MCP Streamable HTTP pieces shared by the remote connector (OAuth, remote.mjs) and
// the local endpoint the console serves at /mcp (bearer token, local-http.mjs).
//
// Two eras on one endpoint (2026-07-28, "Backward Compatibility with Initialization-Based
// Versions"):
// - Legacy (2025-11-25 and older): JSON-RPC over POST (single or batch), sessions opened by
//   initialize and named by the Mcp-Session-Id header, DELETE to close one. No GET stream.
// - Modern (2026-07-28): one request per POST, naming its version in params._meta and mirroring it
//   in headers (modern.mjs). It opens no session and ignores Mcp-Session-Id; it is answered with
//   JSON, or with a short event stream for subscriptions/listen.
import crypto from 'node:crypto';
import {
  MODERN_PROTOCOL_VERSIONS, META_PROTOCOL_VERSION, checkModernBody, checkModernHeaders, isModern, listenAcknowledgment,
} from './modern.mjs';

// The initialize-based MCP revisions this server speaks, newest first: `initialize` gets the
// version it asks for when it is listed, else the newest (session.mjs). Revision 2026-07-28 is
// spoken too, without initialize (MODERN_PROTOCOL_VERSIONS).
export const PROTOCOL_VERSIONS = Object.freeze(['2025-11-25', '2025-06-18', '2025-03-26']);
export { MODERN_PROTOCOL_VERSIONS };
export const MAX_BODY = 1024 * 1024;

/** The JSON-RPC error for a legacy request whose MCP-Protocol-Version this server does not speak
 *  in a session; null when it speaks it, or when there is none. A modern version without the
 *  request naming it in _meta is a malformed modern request (-32602) when modern requests are
 *  served; otherwise the error is an ordinary one (-32600), which tells a client that also speaks
 *  older revisions to fall back to initialize. */
function unsupportedVersion(request, servesModern = () => false, id = null) {
  const version = request.headers['mcp-protocol-version'];
  if (version === undefined || PROTOCOL_VERSIONS.includes(version)) return null;
  if (MODERN_PROTOCOL_VERSIONS.includes(version) && servesModern()) {
    return rpcError(id, -32602, `A ${version} request names its version in params._meta["${META_PROTOCOL_VERSION}"]`);
  }
  return rpcError(id, -32600, `Unsupported MCP-Protocol-Version; use one of ${PROTOCOL_VERSIONS.join(', ')}`);
}

/** The id of a single JSON-RPC message, when it is one a reply can carry. */
function replyId(message) {
  const id = message?.id;
  return typeof id === 'string' || Number.isInteger(id) ? id : null;
}

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

/** JSON-RPC messages as a finished event stream (an SSE response to one request). */
export function sendEvents(response, messages) {
  response.writeHead(200, {
    'Cache-Control': 'no-store',
    'X-Content-Type-Options': 'nosniff',
    'Content-Type': 'text/event-stream',
    'X-Accel-Buffering': 'no',
  });
  for (const message of messages) response.write(`event: message\ndata: ${JSON.stringify(message)}\n\n`);
  response.end();
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
 *
 * Modern requests open no session: stateless(owner) gives the handler that answers them, one per
 * owner, reused (answering a modern request changes nothing in it) and never listed in sessions.
 * It is null when createSession gives none, and then modern requests are refused as before.
 */
export function createSessionStore({ createSession, now = Date.now, maxSessions = 500, idleMs = 24 * 60 * 60_000 } = {}) {
  const sessions = new Map();
  const handlers = new Map();
  return {
    sessions,
    stateless(owner) {
      let handler = handlers.get(owner);
      if (handler) {
        handlers.delete(owner);
      } else {
        handler = createSession();
        if (!handler) return null;
        while (handlers.size >= maxSessions) handlers.delete(handlers.keys().next().value);
      }
      handlers.set(owner, handler);
      return handler;
    },
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

function logToolCall(message, label, log) {
  if (message.method === 'tools/call') {
    log(`tool ${String(message.params?.name).replace(/[^\w.-]/g, '?').slice(0, 64)} for ${label}`);
  }
}

/** A modern request (one per POST): its headers and body checked, then answered on its own. A
 *  notification is accepted with 202 and nothing else; an unknown method gets 404. */
async function handleModernPost(request, response, { message, handler, label, log }) {
  const kind = classify(message);
  if (kind === 'notification') return send(response, 202);
  if (kind !== 'request') return send(response, 400, rpcError(replyId(message), -32600, 'Invalid Request'));
  const refused = checkModernHeaders(request.headers, message) || checkModernBody(message);
  if (refused) return send(response, 400, refused);
  logToolCall(message, label, log);
  const reply = await handler.dispatchModern(message);
  if (message.method === 'subscriptions/listen' && reply.result) {
    return sendEvents(response, [listenAcknowledgment(message.id), reply]);
  }
  return send(response, reply.error?.code === -32601 ? 404 : 200, reply);
}

/** POST /mcp: a modern request, or legacy JSON-RPC (one message or a batch). initialize opens a
 *  session and must come alone. options.modern false refuses modern requests, as before 2026-07-28
 *  was spoken. */
export async function handleMcpPost(request, response, { store, owner, label, log = () => {}, modern = true }) {
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
  const modernHandler = () => (modern ? store.stateless?.(owner) ?? null : null);
  const handler = messages.some(isModern) ? modernHandler() : null;
  if (handler) {
    if (batch) return send(response, 400, rpcError(null, -32600, 'Revision 2026-07-28 has no batches; send each request on its own'));
    return handleModernPost(request, response, { message: payload, handler, label, log });
  }
  const initializing = messages.some((message) => message?.method === 'initialize');
  // initialize negotiates the version in its body, whatever the header says: a client may send
  // its own newest version there before it knows ours. Every later request names one of ours.
  const unsupported = initializing ? null
    : unsupportedVersion(request, () => Boolean(modernHandler()), batch ? null : replyId(payload));
  if (unsupported) return send(response, 400, unsupported);
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
    logToolCall(message, label, log);
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

/** Everything after authentication: POST (which checks the protocol version once it knows
 *  whether the request is initialize, and whether it is modern), DELETE, or 405. A modern client
 *  has no session to delete and no stream to open, so GET and DELETE are 405 for it. */
export async function handleMcpRequest(request, response, options) {
  if (request.method === 'POST') return handleMcpPost(request, response, options);
  if (options.modern !== false && MODERN_PROTOCOL_VERSIONS.includes(request.headers['mcp-protocol-version'])) {
    return send(response, 405, rpcError(null, -32000, 'Use POST; this server keeps no session and offers no SSE stream'), { Allow: 'POST, DELETE' });
  }
  const unsupported = unsupportedVersion(request);
  if (unsupported) return send(response, 400, unsupported);
  if (request.method === 'DELETE') {
    const found = options.store.find(request, options.owner);
    if (!found.entry) return send(response, found.status, rpcError(null, -32000, found.message));
    options.store.remove(found.entry.id);
    return send(response, 204);
  }
  return send(response, 405, rpcError(null, -32000, 'Use POST; this server does not offer an SSE stream'), { Allow: 'POST, DELETE' });
}
