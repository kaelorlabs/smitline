#!/usr/bin/env node
// Remote connector: MCP Streamable HTTP with OAuth 2.1 sign-in, so cloud agents
// can place calls through this server. Only the call tools are exposed. The
// daemon stays on loopback; tools reach it through the TypeScript SDK.
import crypto from 'node:crypto';
import http from 'node:http';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { Colleague, redact } from '../../sdk-typescript/src/index.mjs';
import { CONNECTOR_PASSPHRASE_MIN, PAGE_STYLE, connectorOrigin, escapeHtml, readEnv } from '../../cli/src/setup.mjs';
import { createMcpSession } from './session.mjs';
import { openConnectorStore, sha256 } from './connector-store.mjs';

export const PROTOCOL_VERSIONS = Object.freeze(['2025-06-18', '2025-03-26']);
export const DEFAULT_CONNECTOR_PORT = 8767;
const DEFAULT_ROOT = fileURLToPath(new URL('../../../', import.meta.url));
const CODE_TTL_MS = 10 * 60_000;
const FORM_TTL_MS = 10 * 60_000;
const FAILURE_WINDOW_MS = 10 * 60_000;
const LOCKOUT_MS = 10 * 60_000;
const MAX_FAILURES = 5;
const MAX_BODY = 1024 * 1024;
const MAX_SESSIONS = 500;
const SESSION_IDLE_MS = 24 * 60 * 60_000;
const SCOPE = 'calls';
const GRANT_TYPES = ['authorization_code', 'refresh_token'];
const LOOPBACK = new Set(['127.0.0.1', 'localhost', '[::1]']);
const RESOURCE_METADATA_PATHS = new Set(['/.well-known/oauth-protected-resource', '/.well-known/oauth-protected-resource/mcp']);
const SERVER_METADATA_PATH = '/.well-known/oauth-authorization-server';
const CORS_ROUTES = new Set(['/mcp', '/oauth/token', '/oauth/register']);
const PAGE_HEADERS = Object.freeze({
  'Content-Type': 'text/html; charset=utf-8',
  'Cache-Control': 'no-store',
  'X-Content-Type-Options': 'nosniff',
  'X-Frame-Options': 'DENY',
  'Content-Security-Policy': "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'",
  // same-origin keeps the Origin header on the approval form's POST.
  'Referrer-Policy': 'same-origin',
});
const PAGE_EXTRA = `.eyebrow{font-size:13px;font-weight:600;color:var(--muted);margin-bottom:6px}
.field{display:grid;gap:6px}
.card{display:grid;gap:14px}
.facts{margin:0;display:grid;gap:12px}
.facts div{display:grid;gap:2px}
.facts dt{font-size:13px;color:var(--muted)}
.facts dd{margin:0;overflow-wrap:anywhere}
.facts .app{font-size:17px;font-weight:600}
.origin{display:inline-block;font-size:15px;font-weight:600;padding:4px 8px;border-radius:6px;background:var(--accent-soft);color:var(--ink)}
.scope{margin:0;padding:0;list-style:none;display:grid;gap:8px}
.scope li{display:grid;grid-template-columns:22px 1fr;gap:8px;align-items:start}
.scope .icon{width:22px;height:22px;border-radius:50%;display:flex;align-items:center;justify-content:center;font-size:12px;font-weight:700;margin-top:1px}
.scope.can .icon{background:var(--accent-soft);color:var(--accent)}
.scope.cannot .icon{background:var(--good-bg);color:var(--good)}
.decide .actions button{min-width:120px}
@media (max-width:480px){.decide .actions button{min-width:0}}`;
const BRAND = '<div class="brand"><span class="brand-mark" aria-hidden="true"><i></i><i></i><i></i></span><span>Colleague <b>AI</b></span></div>';

// Configuration ---------------------------------------------------------------

/** Read connector settings from the process environment, then <root>/.env. Throws with every problem listed. */
export function loadConnectorConfig({ root = DEFAULT_ROOT, env = process.env } = {}) {
  const fromFile = readEnv(root);
  const value = (key) => {
    const raw = String(env[key] ?? '').trim() || String(fromFile[key] ?? '').trim();
    return raw.startsWith('replace_with') ? '' : raw;
  };
  const problems = [];
  let baseUrl = '';
  if (!value('COLLEAGUE_CONNECTOR_URL')) {
    problems.push('COLLEAGUE_CONNECTOR_URL is not set. Set it to the public https address of this server: colleague setup set COLLEAGUE_CONNECTOR_URL https://colleague.example.com');
  } else {
    try {
      baseUrl = connectorOrigin(value('COLLEAGUE_CONNECTOR_URL'), { allowLoopback: true });
    } catch (error) {
      problems.push(error.message);
    }
  }
  const passphrase = value('COLLEAGUE_CONNECTOR_PASSPHRASE');
  if (!passphrase) {
    problems.push('COLLEAGUE_CONNECTOR_PASSPHRASE is not set. Run colleague setup secrets and choose an owner passphrase under "Remote connector (server mode)".');
  } else if (passphrase.length < CONNECTOR_PASSPHRASE_MIN) {
    problems.push(`COLLEAGUE_CONNECTOR_PASSPHRASE must be at least ${CONNECTOR_PASSPHRASE_MIN} characters.`);
  }
  const port = Number(value('COLLEAGUE_CONNECTOR_PORT') || DEFAULT_CONNECTOR_PORT);
  if (!Number.isInteger(port) || port < 1 || port > 65535) problems.push('COLLEAGUE_CONNECTOR_PORT must be a port number.');
  const allowedOrigins = [];
  for (const item of value('COLLEAGUE_CONNECTOR_ALLOWED_ORIGINS').split(',').map((entry) => entry.trim()).filter(Boolean)) {
    let origin = 'null';
    try { origin = new URL(item).origin; } catch { /* reported below */ }
    if (origin === 'null') problems.push(`COLLEAGUE_CONNECTOR_ALLOWED_ORIGINS has an invalid origin: ${item}`);
    else allowedOrigins.push(origin);
  }
  if (problems.length) throw Object.assign(new Error(problems.join('\n')), { code: 'config' });
  return { root, baseUrl, passphrase, port, allowedOrigins, daemonPort: value('COLLEAGUE_DAEMON_PORT') || undefined };
}

// Helpers -------------------------------------------------------------------------

function defaultLog(line) {
  process.stderr.write(`${new Date().toISOString()} ${redact(line)}\n`);
}

function httpError(status, message) {
  return Object.assign(new Error(message), { status });
}

function readBody(request, limit = MAX_BODY) {
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

function mediaType(request) {
  return String(request.headers['content-type'] || '').split(';')[0].trim().toLowerCase();
}

function send(response, status, body, headers = {}) {
  const text = body === undefined ? '' : JSON.stringify(body);
  response.writeHead(status, {
    'Cache-Control': 'no-store',
    'X-Content-Type-Options': 'nosniff',
    ...(text ? { 'Content-Type': 'application/json' } : {}),
    ...headers,
  });
  response.end(text);
}

function oauthError(response, status, error, description, headers = {}) {
  send(response, status, { error, error_description: description }, headers);
}

function rpcError(id, code, message) {
  return { jsonrpc: '2.0', id: id ?? null, error: { code, message } };
}

function sendPage(response, status, html, headers = {}) {
  response.writeHead(status, { ...PAGE_HEADERS, ...headers });
  response.end(html);
}

function safeEqual(a, b) {
  const left = Buffer.from(String(a));
  const right = Buffer.from(String(b));
  return left.length === right.length && crypto.timingSafeEqual(left, right);
}

function pkceChallenge(verifier) {
  return crypto.createHash('sha256').update(verifier).digest('base64url');
}

function trimSlash(value) {
  return typeof value === 'string' ? value.replace(/\/$/, '') : value;
}

function classify(message) {
  if (!message || typeof message !== 'object' || Array.isArray(message) || message.jsonrpc !== '2.0') return 'invalid';
  if (typeof message.method === 'string') return Object.hasOwn(message, 'id') ? 'request' : 'notification';
  if (Object.hasOwn(message, 'result') || Object.hasOwn(message, 'error')) return 'response';
  return 'invalid';
}

function basicClientId(header) {
  const match = /^Basic\s+([A-Za-z0-9+/=]+)\s*$/i.exec(header || '');
  if (!match) return undefined;
  const decoded = Buffer.from(match[1], 'base64').toString('utf8');
  try {
    return decodeURIComponent(decoded.split(':')[0]);
  } catch {
    return undefined;
  }
}

export function isAllowedRedirect(value) {
  let url;
  try {
    url = new URL(value);
  } catch {
    return false;
  }
  if (url.hash || url.username || url.password) return false;
  return url.protocol === 'https:' || (url.protocol === 'http:' && LOOPBACK.has(url.hostname));
}

// Public clients only: a requested token_endpoint_auth_method is replaced with
// none, which RFC 7591 section 3.2.1 allows.
function checkClientMetadata(metadata) {
  const bad = (error, description) => ({ error, description });
  if (!metadata || typeof metadata !== 'object' || Array.isArray(metadata)) {
    return bad('invalid_client_metadata', 'Client metadata must be a JSON object');
  }
  const uris = metadata.redirect_uris;
  if (!Array.isArray(uris) || !uris.length || uris.length > 10) {
    return bad('invalid_redirect_uri', 'redirect_uris must list 1 to 10 URLs');
  }
  if (!uris.every((uri) => typeof uri === 'string' && uri.length <= 2000 && isAllowedRedirect(uri))) {
    return bad('invalid_redirect_uri', 'Redirect URIs must use https, or http on localhost or 127.0.0.1, and have no fragment');
  }
  const grantTypes = metadata.grant_types ?? GRANT_TYPES;
  if (!Array.isArray(grantTypes) || !grantTypes.includes('authorization_code') || !grantTypes.every((type) => GRANT_TYPES.includes(type))) {
    return bad('invalid_client_metadata', 'grant_types may contain only authorization_code and refresh_token');
  }
  const responseTypes = metadata.response_types ?? ['code'];
  if (!Array.isArray(responseTypes) || !responseTypes.every((type) => type === 'code')) {
    return bad('invalid_client_metadata', 'response_types may contain only code');
  }
  const name = typeof metadata.client_name === 'string'
    ? metadata.client_name.replace(/[\u0000-\u001f\u007f-\u009f​-‏‪-‮⁦-⁩]/g, '').trim().slice(0, 80)
    : '';
  return { client: { clientName: name || 'Unnamed app', redirectUris: [...new Set(uris)], grantTypes: [...new Set(grantTypes)] } };
}

// Pages ------------------------------------------------------------------------------

function page(title, body) {
  return `<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>${escapeHtml(title)}</title><style>
${PAGE_STYLE}
${PAGE_EXTRA}</style></head>
<body><main>${BRAND}${body}</main></body></html>`;
}

function errorPage(message) {
  return page('Colleague AI connection', `<h1>This connection request can’t continue</h1>
<p class="error" role="alert">${escapeHtml(message)}</p>
<p class="lead">Nothing was shared. Go back to the app and connect again.</p>`);
}

// The owner decides here: which app, where it returns, and what it may do.
function approvalPage(request, csrf, error = '') {
  const name = escapeHtml(request.client.clientName);
  const origin = escapeHtml(new URL(request.redirectUri).origin);
  const hidden = Object.entries({ ...request.fields, csrf })
    .map(([key, value]) => `<input type="hidden" name="${escapeHtml(key)}" value="${escapeHtml(value)}">`)
    .join('\n');
  return page('Allow access to Colleague AI', `<header><p class="eyebrow">Connection request</p>
<h1>Allow ${name} to use Colleague AI?</h1></header>
<p class="lead">An app is asking to connect to your Colleague AI server. Check who is asking, then allow or deny.</p>
<section class="card" aria-labelledby="who-title">
<h2 id="who-title">Who is asking</h2>
<dl class="facts">
<div><dt>App name, as the app describes itself</dt><dd class="app">${name}</dd></div>
<div><dt>After you decide, you return to</dt><dd><code class="origin">${origin}</code></dd></div>
<div><dt>Client ID</dt><dd><code>${escapeHtml(request.client.clientId)}</code></dd></div>
</dl>
<p class="notice warn"><span><strong>Allow only if you just added this connector yourself</strong> and ${origin} belongs to the app you are using. Anyone can pick an app name; the return address is what counts.</span></p>
</section>
<section class="card" aria-labelledby="scope-title">
<h2 id="scope-title">If you allow it, it can</h2>
<ul class="scope can">
<li><span class="icon" aria-hidden="true">✓</span><span>Place phone calls and join video meetings on your behalf, following briefs it writes</span></li>
<li><span class="icon" aria-hidden="true">✓</span><span>Read the status, results, and transcripts of those calls</span></li>
<li><span class="icon" aria-hidden="true">✓</span><span>Send guidance during a call, end a call, or hand a call to your phone</span></li>
</ul>
<h2>It cannot</h2>
<ul class="scope cannot">
<li><span class="icon" aria-hidden="true">✕</span><span>Read your API keys, your files, or your coding workspaces</span></li>
</ul>
</section>
<form method="post" action="/oauth/authorize" class="card decide" aria-labelledby="decide-title">
<h2 id="decide-title">Your decision</h2>
${hidden}
${error ? `<p class="error" role="alert" id="passphrase-error">${escapeHtml(error)}</p>` : ''}
<div class="field"><label for="passphrase">Owner passphrase</label>
<input id="passphrase" name="passphrase" type="password" autocomplete="current-password" required aria-describedby="${error ? 'passphrase-error ' : ''}passphrase-hint"${error ? ' aria-invalid="true" autofocus' : ''}>
<small id="passphrase-hint">The passphrase chosen for the remote connector on this server’s setup page. You need it only to allow.</small></div>
<div class="actions"><button type="submit" name="decision" value="approve">Allow</button>
<button type="submit" name="decision" value="deny" class="secondary" formnovalidate>Deny</button></div>
</form>
<p class="hint">Access renews while the app uses it and ends after 30 days without use. Revoke it at any time with <code>colleague connector revoke</code>.</p>`);
}

// Server ---------------------------------------------------------------------------------

export function createConnector({
  root = DEFAULT_ROOT,
  baseUrl,
  passphrase,
  allowedOrigins = [],
  colleague,
  daemonPort,
  now = Date.now,
  log = defaultLog,
} = {}) {
  if (typeof passphrase !== 'string' || passphrase.length < CONNECTOR_PASSPHRASE_MIN) {
    throw new Error(`the owner passphrase must be at least ${CONNECTOR_PASSPHRASE_MIN} characters`);
  }
  const store = openConnectorStore(root, { now });
  const calls = colleague || new Colleague({ root, host: '127.0.0.1', port: daemonPort || process.env.COLLEAGUE_DAEMON_PORT });
  const passphraseDigest = crypto.createHash('sha256').update(passphrase).digest();
  const formKey = crypto.randomBytes(32);
  const allowed = new Set(allowedOrigins);
  const codes = new Map();
  const usedCodes = new Map();
  const usedForms = new Map();
  const sessions = new Map();
  let failures = [];
  let lockedUntil = 0;
  let base = baseUrl ? connectorOrigin(baseUrl, { allowLoopback: true }) : null;

  const resourceUrl = () => `${base}/mcp`;
  const metadataUrl = () => `${base}/.well-known/oauth-protected-resource`;

  function resourceMetadata() {
    return {
      resource: resourceUrl(),
      authorization_servers: [base],
      bearer_methods_supported: ['header'],
      scopes_supported: [SCOPE],
      resource_name: 'Colleague AI',
    };
  }

  function serverMetadata() {
    return {
      issuer: base,
      authorization_endpoint: `${base}/oauth/authorize`,
      token_endpoint: `${base}/oauth/token`,
      registration_endpoint: `${base}/oauth/register`,
      scopes_supported: [SCOPE],
      response_types_supported: ['code'],
      response_modes_supported: ['query'],
      grant_types_supported: GRANT_TYPES,
      token_endpoint_auth_methods_supported: ['none'],
      code_challenge_methods_supported: ['S256'],
      authorization_response_iss_parameter_supported: true,
    };
  }

  function originAllowed(request) {
    const origin = request.headers.origin;
    return origin === undefined || origin === base || allowed.has(origin);
  }

  function applyCors(request, response, pathname) {
    if (pathname.startsWith('/.well-known/')) {
      response.setHeader('Access-Control-Allow-Origin', '*');
      return;
    }
    const origin = request.headers.origin;
    if (!origin || !CORS_ROUTES.has(pathname) || !originAllowed(request)) return;
    response.setHeader('Access-Control-Allow-Origin', origin);
    response.setHeader('Vary', 'Origin');
    response.setHeader('Access-Control-Expose-Headers', 'Mcp-Session-Id, WWW-Authenticate');
  }

  function preflight(request, response, pathname) {
    if (!pathname.startsWith('/.well-known/') && !CORS_ROUTES.has(pathname)) return send(response, 404, { error: 'not_found' });
    if (!pathname.startsWith('/.well-known/') && !originAllowed(request)) {
      return send(response, 403, { error: 'forbidden_origin', error_description: 'This origin is not allowed' });
    }
    response.writeHead(204, {
      'Access-Control-Allow-Methods': 'GET, POST, DELETE, OPTIONS',
      'Access-Control-Allow-Headers': 'Authorization, Content-Type, Mcp-Session-Id, MCP-Protocol-Version, Last-Event-ID',
      'Access-Control-Max-Age': '600',
    });
    return response.end();
  }

  // Authorization -------------------------------------------------------------------------

  function checkAuthorization(params) {
    const one = (key) => {
      const values = params.getAll(key);
      return values.length > 1 ? null : values[0];
    };
    const client = store.getClient(one('client_id'));
    if (!client) return { fatal: 'This app is not registered with this Colleague AI server.' };
    const given = one('redirect_uri');
    const redirectUri = given === undefined ? (client.redirectUris.length === 1 ? client.redirectUris[0] : null) : given;
    if (!redirectUri || !client.redirectUris.includes(redirectUri)) {
      return { fatal: 'The return address does not match the one this app registered.' };
    }
    const state = one('state');
    const fail = (error, description) => ({
      redirect: { redirectUri, params: { error, error_description: description, state: state || undefined } },
    });
    if (one('response_type') !== 'code') return fail('unsupported_response_type', 'response_type must be code');
    if (typeof state !== 'string' || !state || state.length > 1024) return fail('invalid_request', 'state is required');
    if (one('code_challenge_method') !== 'S256') return fail('invalid_request', 'PKCE with code_challenge_method=S256 is required');
    const challenge = one('code_challenge');
    if (!/^[A-Za-z0-9_-]{43}$/.test(challenge || '')) return fail('invalid_request', 'code_challenge must be a base64url SHA-256 digest');
    const resource = one('resource');
    if (resource !== undefined && (typeof resource !== 'string' || trimSlash(resource) !== resourceUrl())) {
      return fail('invalid_target', `resource must be ${resourceUrl()}`);
    }
    const fields = { response_type: 'code', client_id: client.clientId, state, code_challenge: challenge, code_challenge_method: 'S256' };
    if (given !== undefined) fields.redirect_uri = redirectUri;
    if (resource !== undefined) fields.resource = resource;
    return { request: { client, redirectUri, explicitRedirect: given !== undefined, state, challenge, resource: resourceUrl(), fields } };
  }

  // The approval form carries a one-time nonce, signed together with the request parameters.
  function formMac(nonce, expires, fields) {
    return crypto.createHmac('sha256', formKey).update(JSON.stringify([nonce, expires, Object.entries(fields)])).digest('base64url');
  }

  function signForm(fields) {
    const nonce = crypto.randomBytes(16).toString('base64url');
    const expires = now() + FORM_TTL_MS;
    return `${nonce}.${expires}.${formMac(nonce, expires, fields)}`;
  }

  function useForm(token, fields) {
    const [nonce, expires, mac, extra] = String(token || '').split('.');
    if (extra !== undefined || !nonce || !mac || !/^\d{1,15}$/.test(expires || '')) return 'invalid';
    if (!safeEqual(mac, formMac(nonce, Number(expires), fields))) return 'invalid';
    const at = now();
    if (Number(expires) <= at) return 'expired';
    for (const [key, until] of usedForms) if (until <= at) usedForms.delete(key);
    if (usedForms.has(nonce)) return 'used';
    usedForms.set(nonce, Number(expires));
    return 'ok';
  }

  function passphraseMatches(value) {
    const digest = crypto.createHash('sha256').update(String(value ?? '').trim()).digest();
    return crypto.timingSafeEqual(digest, passphraseDigest);
  }

  function lockRemaining() {
    return Math.max(0, lockedUntil - now());
  }

  function recordFailure() {
    const at = now();
    failures = failures.filter((time) => time > at - FAILURE_WINDOW_MS);
    failures.push(at);
    log(`wrong owner passphrase (${failures.length} of ${MAX_FAILURES} in 10 minutes)`);
    if (failures.length >= MAX_FAILURES) {
      lockedUntil = at + LOCKOUT_MS;
      failures = [];
      log('approval locked for 10 minutes after repeated wrong passphrases');
    }
  }

  function issueCode(request) {
    const at = now();
    for (const [digest, record] of codes) if (record.expiresAt <= at) codes.delete(digest);
    for (const [digest, record] of usedCodes) if (record.expiresAt <= at) usedCodes.delete(digest);
    const code = `cai_ac_${crypto.randomBytes(32).toString('base64url')}`;
    codes.set(sha256(code), {
      clientId: request.client.clientId,
      redirectUri: request.redirectUri,
      explicitRedirect: request.explicitRedirect,
      challenge: request.challenge,
      resource: request.resource,
      expiresAt: at + CODE_TTL_MS,
    });
    return code;
  }

  function redirect(response, status, target, params) {
    const url = new URL(target);
    for (const [key, value] of Object.entries({ ...params, iss: base })) {
      if (value !== undefined && value !== null) url.searchParams.set(key, value);
    }
    response.writeHead(status, { Location: url.href, 'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer' });
    response.end();
  }

  async function handleAuthorize(request, response) {
    const posted = request.method === 'POST';
    if (!posted && request.method !== 'GET') return sendPage(response, 405, errorPage('Use the link from the app.'), { Allow: 'GET, POST' });
    if (posted && mediaType(request) !== 'application/x-www-form-urlencoded') {
      return sendPage(response, 415, errorPage('The approval form was not sent correctly.'));
    }
    const params = posted ? new URLSearchParams(await readBody(request)) : new URL(request.url, 'http://connector.invalid').searchParams;
    const checked = checkAuthorization(params);
    if (checked.fatal) return sendPage(response, 400, errorPage(checked.fatal));
    if (checked.redirect) return redirect(response, posted ? 303 : 302, checked.redirect.redirectUri, checked.redirect.params);
    const pending = checked.request;
    if (!posted) return sendPage(response, 200, approvalPage(pending, signForm(pending.fields)));

    const form = useForm(params.get('csrf'), pending.fields);
    if (form === 'expired') return sendPage(response, 400, errorPage('This approval page expired.'));
    if (form !== 'ok') return sendPage(response, 400, errorPage('This approval form was already used or is not valid.'));
    const decision = params.get('decision');
    if (decision === 'deny') {
      log(`owner denied client ${pending.client.clientId}`);
      return redirect(response, 303, pending.redirectUri, { error: 'access_denied', error_description: 'The owner denied access', state: pending.state });
    }
    if (decision !== 'approve') return sendPage(response, 400, errorPage('Choose Allow or Deny.'));
    let wait = lockRemaining();
    if (!wait) {
      if (passphraseMatches(params.get('passphrase'))) {
        failures = [];
        log(`owner approved client ${pending.client.clientId}`);
        return redirect(response, 303, pending.redirectUri, { code: issueCode(pending), state: pending.state });
      }
      recordFailure();
      wait = lockRemaining();
      if (!wait) return sendPage(response, 403, approvalPage(pending, signForm(pending.fields), 'That passphrase is not correct. Try again.'));
    }
    const minutes = Math.ceil(wait / 60_000);
    const message = `Too many wrong passphrases. Approval is locked for ${minutes} more minute${minutes === 1 ? '' : 's'}.`;
    return sendPage(response, 429, approvalPage(pending, signForm(pending.fields), message), { 'Retry-After': String(Math.ceil(wait / 1000)) });
  }

  // Registration and tokens --------------------------------------------------------------------

  async function handleRegister(request, response) {
    if (request.method !== 'POST') return oauthError(response, 405, 'invalid_request', 'Use POST', { Allow: 'POST' });
    let metadata;
    try {
      metadata = JSON.parse(await readBody(request));
    } catch (error) {
      if (error.status) throw error;
      return oauthError(response, 400, 'invalid_client_metadata', 'The body must be JSON client metadata');
    }
    const checked = checkClientMetadata(metadata);
    if (checked.error) return oauthError(response, 400, checked.error, checked.description);
    const client = store.registerClient(checked.client);
    if (!client) {
      return oauthError(response, 429, 'invalid_client_metadata', 'Too many apps registered recently. Try again in a few minutes.', { 'Retry-After': '600' });
    }
    log(`registered client ${client.clientId} (${client.clientName})`);
    return send(response, 201, {
      client_id: client.clientId,
      client_id_issued_at: Math.floor(Date.parse(client.createdAt) / 1000),
      client_name: client.clientName,
      redirect_uris: client.redirectUris,
      grant_types: client.grantTypes,
      response_types: ['code'],
      token_endpoint_auth_method: 'none',
    });
  }

  function sendTokens(response, issued) {
    send(response, 200, {
      access_token: issued.accessToken,
      token_type: 'Bearer',
      expires_in: issued.expiresIn,
      ...(issued.refreshToken ? { refresh_token: issued.refreshToken } : {}),
      scope: SCOPE,
    }, { Pragma: 'no-cache' });
  }

  function exchangeCode(response, client, params) {
    const digest = sha256(params.get('code') || '');
    const record = codes.get(digest);
    codes.delete(digest);
    if (!record) {
      const used = usedCodes.get(digest);
      if (used?.grantId) {
        store.revokeGrant(used.grantId);
        log(`authorization code reused; revoked ${used.grantId}`);
        used.grantId = null;
      }
      return oauthError(response, 400, 'invalid_grant', 'The authorization code is invalid, expired, or already used');
    }
    usedCodes.set(digest, { grantId: null, expiresAt: record.expiresAt });
    const redirectUri = params.get('redirect_uri');
    const verifier = params.get('code_verifier') || '';
    let problem = null;
    if (record.expiresAt <= now()) problem = 'The authorization code expired';
    else if (record.clientId !== client.clientId) problem = 'The authorization code was issued to another client';
    else if (redirectUri === null ? record.explicitRedirect : redirectUri !== record.redirectUri) problem = 'redirect_uri does not match the authorization request';
    else if (!/^[A-Za-z0-9._~-]{43,128}$/.test(verifier) || !safeEqual(pkceChallenge(verifier), record.challenge)) problem = 'code_verifier does not match the code challenge';
    if (problem) return oauthError(response, 400, 'invalid_grant', problem);
    const resource = params.get('resource');
    if (resource !== null && trimSlash(resource) !== record.resource) {
      return oauthError(response, 400, 'invalid_target', `resource must be ${record.resource}`);
    }
    const issued = store.createGrant({
      clientId: client.clientId, resource: record.resource, scope: SCOPE, refresh: client.grantTypes.includes('refresh_token'),
    });
    usedCodes.get(digest).grantId = issued.grantId;
    log(`issued tokens to client ${client.clientId} (${issued.grantId})`);
    return sendTokens(response, issued);
  }

  function refreshTokens(response, client, params) {
    if (!client.grantTypes.includes('refresh_token')) {
      return oauthError(response, 400, 'unauthorized_client', 'This client did not register for refresh tokens');
    }
    const resource = params.get('resource');
    if (resource !== null && trimSlash(resource) !== resourceUrl()) {
      return oauthError(response, 400, 'invalid_target', `resource must be ${resourceUrl()}`);
    }
    const result = store.rotateRefresh(params.get('refresh_token') || '', client.clientId);
    if (result.error === 'reused') log(`refresh token reused; revoked ${result.grantId}`);
    if (!result.error && result.resource !== resourceUrl()) {
      store.revokeGrant(result.grantId);
      result.error = 'moved';
    }
    if (result.error) return oauthError(response, 400, 'invalid_grant', 'The refresh token is invalid, expired, or already used');
    return sendTokens(response, result);
  }

  async function handleToken(request, response) {
    if (request.method !== 'POST') return oauthError(response, 405, 'invalid_request', 'Use POST', { Allow: 'POST' });
    if (mediaType(request) !== 'application/x-www-form-urlencoded') {
      return oauthError(response, 400, 'invalid_request', 'Send the token request as application/x-www-form-urlencoded');
    }
    const params = new URLSearchParams(await readBody(request));
    for (const key of new Set(params.keys())) {
      if (params.getAll(key).length > 1) return oauthError(response, 400, 'invalid_request', `${key} appears more than once`);
    }
    const client = store.getClient(params.get('client_id') || basicClientId(request.headers.authorization));
    if (!client) return oauthError(response, 401, 'invalid_client', 'Unknown client_id; register the client again');
    const grantType = params.get('grant_type');
    if (grantType === 'authorization_code') return exchangeCode(response, client, params);
    if (grantType === 'refresh_token') return refreshTokens(response, client, params);
    return oauthError(response, 400, 'unsupported_grant_type', 'grant_type must be authorization_code or refresh_token');
  }

  // MCP --------------------------------------------------------------------------------------------

  function grantFor(request) {
    const match = /^Bearer\s+(\S+)\s*$/i.exec(request.headers.authorization || '');
    if (!match) return { error: null };
    const grant = store.checkAccess(match[1]);
    if (!grant || grant.resource !== resourceUrl()) return { error: 'invalid_token' };
    return { grant };
  }

  function unauthorized(response, error) {
    const challenge = [
      ...(error ? [`error="${error}"`, 'error_description="The access token is invalid or expired"'] : []),
      `resource_metadata="${metadataUrl()}"`,
    ].join(', ');
    send(response, 401, {
      error: error || 'unauthorized',
      error_description: error ? 'The access token is invalid or expired' : 'Sign in to use this connector',
    }, { 'WWW-Authenticate': `Bearer ${challenge}` });
  }

  function openSession(grant) {
    const cutoff = now() - SESSION_IDLE_MS;
    for (const [id, entry] of sessions) if (entry.lastSeen < cutoff) sessions.delete(id);
    while (sessions.size >= MAX_SESSIONS) sessions.delete(sessions.keys().next().value);
    return {
      id: crypto.randomBytes(24).toString('base64url'),
      grantId: grant.grantId,
      lastSeen: now(),
      session: createMcpSession({ colleague: calls, tools: 'calls', protocolVersions: PROTOCOL_VERSIONS, notify() {}, log }),
    };
  }

  function sessionFor(request, grant) {
    const id = request.headers['mcp-session-id'];
    if (!id) return { status: 400, message: 'The Mcp-Session-Id header is required' };
    const entry = sessions.get(id);
    if (!entry || entry.grantId !== grant.grantId) return { status: 404, message: 'Session not found; initialize a new session' };
    // Keep the map in least-recently-used order.
    sessions.delete(id);
    sessions.set(id, entry);
    entry.lastSeen = now();
    return { entry };
  }

  async function handleMcpPost(request, response, grant) {
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
      entry = openSession(grant);
    } else {
      const found = sessionFor(request, grant);
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
        log(`tool ${String(message.params?.name).replace(/[^\w.-]/g, '?').slice(0, 64)} for client ${grant.clientId}`);
      }
      replies.push(await entry.session.dispatch(message));
    }
    const headers = {};
    if (initializing) {
      sessions.set(entry.id, entry);
      headers['Mcp-Session-Id'] = entry.id;
      log(`session opened for client ${grant.clientId}`);
    }
    if (!replies.length) return send(response, 202, undefined, headers);
    return send(response, 200, batch ? replies : replies[0], headers);
  }

  async function handleMcp(request, response) {
    if (!originAllowed(request)) return send(response, 403, rpcError(null, -32000, 'Origin not allowed'));
    const { grant, error } = grantFor(request);
    if (!grant) return unauthorized(response, error);
    const version = request.headers['mcp-protocol-version'];
    if (version !== undefined && !PROTOCOL_VERSIONS.includes(version)) {
      return send(response, 400, rpcError(null, -32600, `Unsupported MCP-Protocol-Version; use ${PROTOCOL_VERSIONS.join(' or ')}`));
    }
    if (request.method === 'POST') return handleMcpPost(request, response, grant);
    if (request.method === 'DELETE') {
      const found = sessionFor(request, grant);
      if (!found.entry) return send(response, found.status, rpcError(null, -32000, found.message));
      sessions.delete(found.entry.id);
      return send(response, 204);
    }
    return send(response, 405, rpcError(null, -32000, 'Use POST; this server does not offer an SSE stream'), { Allow: 'POST, DELETE' });
  }

  // Routing ------------------------------------------------------------------------------------------

  async function route(request, response, pathname) {
    applyCors(request, response, pathname);
    if (request.method === 'OPTIONS') return preflight(request, response, pathname);
    if (RESOURCE_METADATA_PATHS.has(pathname) || pathname === SERVER_METADATA_PATH) {
      if (request.method !== 'GET' && request.method !== 'HEAD') return send(response, 405, { error: 'method_not_allowed' }, { Allow: 'GET' });
      return send(response, 200, pathname === SERVER_METADATA_PATH ? serverMetadata() : resourceMetadata());
    }
    if (pathname === '/mcp') return handleMcp(request, response);
    if (pathname.startsWith('/oauth/')) {
      if (request.method === 'POST' && !originAllowed(request)) {
        return oauthError(response, 403, 'invalid_request', 'This origin is not allowed');
      }
      if (pathname === '/oauth/register') return handleRegister(request, response);
      if (pathname === '/oauth/authorize') return handleAuthorize(request, response);
      if (pathname === '/oauth/token') return handleToken(request, response);
    }
    return send(response, 404, { error: 'not_found' });
  }

  const server = http.createServer((request, response) => {
    const pathname = new URL(request.url, 'http://connector.invalid').pathname;
    response.on('finish', () => log(`${request.method} ${pathname} ${response.statusCode}`));
    route(request, response, pathname).catch((error) => {
      if (response.headersSent) {
        response.destroy();
        return;
      }
      if (error.status === 413) {
        send(response, 413, { error: 'request_too_large', error_description: error.message }, { Connection: 'close' });
        return;
      }
      log(`internal error: ${error.message}`);
      send(response, 500, { error: 'server_error', error_description: 'Internal error' });
    });
  });

  const connector = {
    server,
    store,
    sessions,
    get url() { return base; },
    listen(port = DEFAULT_CONNECTOR_PORT, host = '127.0.0.1') {
      return new Promise((resolve, reject) => {
        server.once('error', reject);
        server.listen(port, host, () => {
          server.off('error', reject);
          if (!base) base = `http://127.0.0.1:${server.address().port}`;
          resolve(connector);
        });
      });
    },
    close() {
      return new Promise((resolve) => {
        server.close(() => resolve());
        server.closeAllConnections();
      });
    },
  };
  return connector;
}

export function startConnector(options = {}) {
  return createConnector(options).listen(options.port ?? DEFAULT_CONNECTOR_PORT, options.host || '127.0.0.1');
}

async function main() {
  let config;
  try {
    config = loadConnectorConfig({ root: path.resolve(process.env.COLLEAGUE_ROOT || DEFAULT_ROOT) });
  } catch (error) {
    process.stderr.write(`Colleague AI connector cannot start.\n${error.message}\nSee docs/agents.md, "Remote connector for cloud agents".\n`);
    process.exit(1);
  }
  let connector;
  try {
    connector = await startConnector(config);
  } catch (error) {
    process.stderr.write(`Colleague AI connector cannot listen on 127.0.0.1:${config.port}: ${error.code || error.message}\n`);
    process.exit(1);
  }
  process.stderr.write(`Colleague AI connector listening on 127.0.0.1:${config.port}\nConnector URL for cloud agents: ${config.baseUrl}/mcp\n`);
  const stop = () => connector.close().then(() => process.exit(0));
  process.on('SIGINT', stop);
  process.on('SIGTERM', stop);
}

const invoked = process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url);
if (invoked) main();
