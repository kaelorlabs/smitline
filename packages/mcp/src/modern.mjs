// MCP revision 2026-07-28 ("modern" in its own terms): no initialize and no sessions. Every request
// names its protocol version and the client's capabilities in params._meta, and on HTTP mirrors
// its version, method and, for tools/call, resources/read and prompts/get, the name into headers.
//
// This server is dual-era (2026-07-28, "Backward Compatibility with Initialization-Based
// Versions"): initialize still opens a legacy session (2025-11-25 and older, PROTOCOL_VERSIONS in
// streamable-http.mjs), and a request whose params._meta names a protocol version is served on its
// own. This module tells the two apart and checks what a modern request carries; session.mjs
// answers it (dispatchModern), and streamable-http.mjs and server.mjs carry it.

export const MODERN_PROTOCOL_VERSIONS = Object.freeze(['2026-07-28']);

export const META_PROTOCOL_VERSION = 'io.modelcontextprotocol/protocolVersion';
export const META_CLIENT_CAPABILITIES = 'io.modelcontextprotocol/clientCapabilities';
export const META_SERVER_INFO = 'io.modelcontextprotocol/serverInfo';
export const META_SUBSCRIPTION_ID = 'io.modelcontextprotocol/subscriptionId';

// Codes the MCP specification defines (2026-07-28, "Error Codes").
export const HEADER_MISMATCH = -32020;
export const UNSUPPORTED_PROTOCOL_VERSION = -32022;

/** Methods whose name (or URI) the Mcp-Name header mirrors, and the param it comes from. */
const NAMED_BY = Object.freeze({ 'tools/call': 'name', 'prompts/get': 'name', 'resources/read': 'uri' });

function isObject(value) {
  return Boolean(value) && typeof value === 'object' && !Array.isArray(value);
}

function error(id, code, message, data) {
  return { jsonrpc: '2.0', id: id ?? null, error: { code, message, ...(data ? { data } : {}) } };
}

/** For error messages: a value the client sent, short and on one line. */
function shown(value) {
  return JSON.stringify(String(value).slice(0, 120));
}

/** The protocol version a message names in params._meta, or undefined. Legacy messages name none,
 *  even when they carry _meta for something else (progressToken). */
export function metaVersion(message) {
  const meta = isObject(message?.params) ? message.params._meta : undefined;
  return isObject(meta) && Object.hasOwn(meta, META_PROTOCOL_VERSION) ? meta[META_PROTOCOL_VERSION] : undefined;
}

/** A modern message names its protocol version in _meta. A legacy initialize names none (only its
 *  header may be newer), so it still opens a session; an initialize in modern form is a modern
 *  request for a method revision 2026-07-28 removed. */
export function isModern(message) {
  return isObject(message) && metaVersion(message) !== undefined;
}

/** A modern request's body, checked: null when it can be served, else the JSON-RPC error to send
 *  (on HTTP with 400): -32600 for a malformed request, -32022 for a version this server does not
 *  speak (with the versions it does), -32602 without the client's capabilities. */
export function checkModernBody(message) {
  const { id } = message;
  if (message.jsonrpc !== '2.0' || typeof message.method !== 'string'
    || !(typeof id === 'string' || Number.isInteger(id))) {
    return error(typeof id === 'string' || Number.isInteger(id) ? id : null, -32600, 'Invalid Request');
  }
  const version = metaVersion(message);
  if (!MODERN_PROTOCOL_VERSIONS.includes(version)) {
    return error(id, UNSUPPORTED_PROTOCOL_VERSION, 'Unsupported protocol version', {
      supported: [...MODERN_PROTOCOL_VERSIONS],
      requested: String(version),
    });
  }
  if (!isObject(message.params._meta[META_CLIENT_CAPABILITIES])) {
    return error(id, -32602, `params._meta["${META_CLIENT_CAPABILITIES}"] is required`);
  }
  return null;
}

/** A header value in the =?base64?…?= form decoded; null when that form is not valid Base64. */
export function decodeHeader(value) {
  const match = /^=\?base64\?(.*)\?=$/s.exec(value);
  if (!match) return value;
  const encoded = match[1];
  if (encoded.length % 4 !== 0 || !/^[A-Za-z0-9+/]*={0,2}$/.test(encoded)) return null;
  return Buffer.from(encoded, 'base64').toString('utf8');
}

/** A modern HTTP request's MCP-Protocol-Version, Mcp-Method and Mcp-Name headers, checked against
 *  its body: null when they match, else the HeaderMismatch error (-32020, sent with 400). */
export function checkModernHeaders(headers, message) {
  const id = typeof message.id === 'string' || Number.isInteger(message.id) ? message.id : null;
  const mismatch = (text) => error(id, HEADER_MISMATCH, text);
  const version = headers['mcp-protocol-version'];
  if (version === undefined) return mismatch('The MCP-Protocol-Version header is required');
  if (version !== metaVersion(message)) {
    return mismatch(`Header mismatch: MCP-Protocol-Version header value ${shown(version)} does not match _meta value ${shown(metaVersion(message))}`);
  }
  const method = headers['mcp-method'];
  if (method === undefined) return mismatch('The Mcp-Method header is required');
  if (method !== message.method) {
    return mismatch(`Header mismatch: Mcp-Method header value ${shown(method)} does not match body value ${shown(message.method)}`);
  }
  const field = NAMED_BY[message.method];
  if (!field) return null;
  const raw = headers['mcp-name'];
  if (raw === undefined) return mismatch(`The Mcp-Name header is required for ${message.method}`);
  const name = decodeHeader(raw);
  if (name === null) return mismatch('The Mcp-Name header is not valid Base64');
  const expected = isObject(message.params) ? message.params[field] : undefined;
  if (name !== expected) {
    return mismatch(`Header mismatch: Mcp-Name header value ${shown(name)} does not match body value ${shown(expected)}`);
  }
  return null;
}

/** The first message on a subscriptions/listen stream. This server sends no change notifications
 *  (its tools and resources do not change while it runs), so it agrees to none of them. */
export function listenAcknowledgment(id) {
  return {
    jsonrpc: '2.0',
    method: 'notifications/subscriptions/acknowledged',
    params: { _meta: { [META_SUBSCRIPTION_ID]: id }, notifications: {} },
  };
}
