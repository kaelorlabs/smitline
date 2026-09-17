import { readFile } from 'node:fs/promises';
import { spawn } from 'node:child_process';
import { Buffer } from 'node:buffer';
import http from 'node:http';
import net from 'node:net';
import path from 'node:path';

export const SCHEMA_VERSION = 1;
export const SDK_VERSION = '1.0.0';

export const EXIT = Object.freeze({
  ok: 0,
  validation: 2,
  startup: 3,
  runtime: 4,
  partial: 5,
  finalization: 6,
  interrupt: 130,
});

const PROVIDERS = new Set(['codex', 'cursor', 'claude-code', 'generic']);
const WORKSPACE_MODES = new Set(['none', 'read-only', 'workspace-write']);
const COMMAND_MODES = new Set(['disabled', 'approval-required', 'allowed']);
const EDIT_MODES = new Set(['disabled', 'approval-required', 'allowed']);
const NETWORK_MODES = new Set(['disabled', 'approval-required', 'allowed']);
const COMMIT_MODES = new Set(['disabled', 'approval-required']);
const PUSH_MODES = new Set(['disabled', 'approval-required']);
const SECRET_KEYS = new Set([
  'authorization', 'cookie', 'set-cookie', 'token', 'accessToken', 'refreshToken',
  'apiKey', 'secret', 'password', 'credential', 'privateKey',
]);
const PLACEHOLDER_SESSIONS = new Set(['', '--last', 'last']);
const MAX_STRING = 8000;
const MAX_ITEMS = 200;

export class ColleagueError extends Error {
  constructor(message, { code = 'runtime', status, archivePath, handoff } = {}) {
    super(redact(message));
    this.name = this.constructor.name;
    this.code = code;
    if (status != null) this.status = status;
    if (archivePath) this.archivePath = archivePath;
    if (handoff) this.handoff = handoff;
  }
}

export class ValidationError extends ColleagueError {
  constructor(message, extra = {}) {
    super(message, { code: 'validation', ...extra });
  }
}

export class StartupError extends ColleagueError {
  constructor(message, extra = {}) {
    super(message, { code: 'startup', ...extra });
  }
}

export class RuntimeError extends ColleagueError {
  constructor(message, extra = {}) {
    super(message, { code: extra.code || 'runtime', ...extra });
  }
}

export class FinalizationError extends ColleagueError {
  constructor(message, extra = {}) {
    super(message, { code: extra.code || 'finalization', ...extra });
  }
}

export class InterruptError extends ColleagueError {
  constructor(message = 'interrupted', extra = {}) {
    super(message, { code: 'interrupt', ...extra });
  }
}

export function redact(value) {
  return String(value ?? '')
    .replace(/Bearer\s+\S+/gi, 'Bearer [redacted]')
    .replace(/\bsk-[A-Za-z0-9_-]{8,}\b/g, '[redacted]')
    .replace(/[A-Za-z0-9+/_-]{40,}/g, '[redacted]');
}

function isPlainObject(value) {
  return Boolean(value) && typeof value === 'object' && !Array.isArray(value);
}

function rejectSecrets(payload, path = '') {
  if (Array.isArray(payload)) {
    payload.forEach((item, index) => rejectSecrets(item, `${path}[${index}]`));
    return;
  }
  if (!isPlainObject(payload)) return;
  for (const [key, value] of Object.entries(payload)) {
    const next = path ? `${path}.${key}` : key;
    if (SECRET_KEYS.has(key) || /secret|token|password|credential|authorization/i.test(key)) {
      throw new ValidationError(`${next} must not contain secrets`);
    }
    rejectSecrets(value, next);
  }
}

function requireString(value, name, { allowEmpty = false, maxLength = MAX_STRING } = {}) {
  if (typeof value !== 'string') throw new ValidationError(`${name} must be a string`);
  if (!allowEmpty && !value.trim()) throw new ValidationError(`${name} must not be empty`);
  if (value.length > maxLength) throw new ValidationError(`${name} exceeds ${maxLength} characters`);
  return value;
}

function requireAbsoluteWorkspace(value) {
  const workspace = requireString(value, 'agentSession.workspace', { maxLength: 4096 });
  if (!workspace.startsWith('/')) throw new ValidationError('agentSession.workspace must be an absolute path');
  return workspace;
}

function platformForUrl(url) {
  let parsed;
  try {
    parsed = new URL(url);
  } catch {
    throw new ValidationError('url must be a Zoom or Teams invitation');
  }
  if (parsed.protocol !== 'https:') throw new ValidationError('url must be an https Zoom or Teams invitation');
  const host = parsed.hostname.toLowerCase();
  if (host === 'zoom.us' || host.endsWith('.zoom.us')) return 'zoom';
  if (host === 'teams.microsoft.com' || host === 'teams.live.com') return 'teams';
  throw new ValidationError('url must be a Zoom or Teams invitation');
}

export function validateAgentSession(value) {
  if (!isPlainObject(value)) throw new ValidationError('agentSession is required');
  rejectSecrets(value, 'agentSession');
  const known = new Set(['provider', 'sessionId', 'workspace', 'model', 'metadata']);
  for (const key of Object.keys(value)) {
    if (!known.has(key)) throw new ValidationError(`agentSession.${key} is not allowed`);
  }
  if (!PROVIDERS.has(value.provider)) {
    throw new ValidationError('agentSession.provider must be one of: ' + [...PROVIDERS].join(', '));
  }
  const sessionId = requireString(value.sessionId, 'agentSession.sessionId', { maxLength: 256 });
  if (PLACEHOLDER_SESSIONS.has(sessionId) || sessionId === '--last') {
    throw new ValidationError('agentSession.sessionId must be an originating thread id');
  }
  const out = {
    provider: value.provider,
    sessionId,
    workspace: requireAbsoluteWorkspace(value.workspace),
  };
  if (value.model !== undefined) out.model = requireString(value.model, 'agentSession.model', { maxLength: 256 });
  if (value.metadata !== undefined) {
    if (!isPlainObject(value.metadata)) throw new ValidationError('agentSession.metadata must be an object');
    const metadata = {};
    for (const [key, item] of Object.entries(value.metadata)) {
      metadata[key] = requireString(item, `agentSession.metadata.${key}`, { maxLength: 1024, allowEmpty: true });
    }
    out.metadata = metadata;
  }
  return out;
}

export function validatePermissions(value) {
  if (value === undefined) {
    return {
      workspace: 'read-only',
      commands: 'approval-required',
      edits: 'disabled',
      network: 'approval-required',
      commits: 'disabled',
      pushes: 'disabled',
    };
  }
  if (!isPlainObject(value)) throw new ValidationError('permissions must be an object');
  rejectSecrets(value, 'permissions');
  const known = new Set(['workspace', 'commands', 'edits', 'network', 'commits', 'pushes']);
  for (const key of Object.keys(value)) {
    if (!known.has(key)) throw new ValidationError(`permissions.${key} is not allowed`);
  }
  const check = (field, allowed) => {
    if (!allowed.has(value[field])) throw new ValidationError(`permissions.${field} is invalid`);
    return value[field];
  };
  const out = {
    workspace: check('workspace', WORKSPACE_MODES),
    commands: check('commands', COMMAND_MODES),
    edits: check('edits', EDIT_MODES),
    network: check('network', NETWORK_MODES),
    commits: check('commits', COMMIT_MODES),
    pushes: check('pushes', PUSH_MODES),
  };
  if (out.workspace === 'none') {
    if (out.commands === 'allowed') throw new ValidationError('commands cannot be allowed when workspace is none');
    if (out.edits !== 'disabled' || out.commits !== 'disabled' || out.pushes !== 'disabled') {
      throw new ValidationError('workspace none cannot authorize edits, commits, or pushes');
    }
  }
  if (out.workspace === 'read-only' && out.edits === 'allowed') {
    throw new ValidationError('edits cannot be allowed without workspace-write');
  }
  if (out.workspace !== 'workspace-write' && (out.commits !== 'disabled' || out.pushes !== 'disabled')) {
    throw new ValidationError('commits and pushes require workspace-write');
  }
  if (out.pushes === 'approval-required' && out.commits !== 'approval-required') {
    throw new ValidationError('pushes require commits to be approval-required');
  }
  return out;
}

function requireStringList(value, name) {
  if (!Array.isArray(value)) throw new ValidationError(`${name} must be an array`);
  if (value.length > MAX_ITEMS) throw new ValidationError(`${name} exceeds ${MAX_ITEMS} items`);
  return value.map((item, index) => requireString(item, `${name}[${index}]`, { allowEmpty: true }));
}

export function validateContext(value, { required = true } = {}) {
  if (value === undefined) {
    if (!required) {
      return {
        version: 1,
        objective: 'Support this live meeting.',
        currentTask: 'Join the meeting and help when asked.',
        summary: '',
        decisions: [],
        constraints: [],
        openQuestions: [],
        importantFiles: [],
        recentConversation: [],
      };
    }
    throw new ValidationError('context is required');
  }
  if (!isPlainObject(value)) throw new ValidationError('context must be an object');
  rejectSecrets(value, 'context');
  const known = new Set([
    'version', 'objective', 'currentTask', 'summary', 'decisions', 'constraints',
    'openQuestions', 'importantFiles', 'recentConversation', 'git',
  ]);
  for (const key of Object.keys(value)) {
    if (!known.has(key)) throw new ValidationError(`context.${key} is not allowed`);
  }
  if (value.version !== 1) throw new ValidationError('context.version must be 1');
  const out = {
    version: 1,
    objective: requireString(value.objective, 'context.objective', { allowEmpty: true, allowNewlines: true }),
    currentTask: requireString(value.currentTask, 'context.currentTask', { allowEmpty: true }),
    summary: requireString(value.summary, 'context.summary', { allowEmpty: true }),
    decisions: requireStringList(value.decisions, 'context.decisions'),
    constraints: requireStringList(value.constraints, 'context.constraints'),
    openQuestions: requireStringList(value.openQuestions, 'context.openQuestions'),
    importantFiles: requireStringList(value.importantFiles, 'context.importantFiles'),
    recentConversation: Array.isArray(value.recentConversation)
      ? value.recentConversation.map((turn, index) => {
          if (!isPlainObject(turn)) throw new ValidationError(`context.recentConversation[${index}] must be an object`);
          if (turn.role !== 'user' && turn.role !== 'assistant') {
            throw new ValidationError(`context.recentConversation[${index}].role is invalid`);
          }
          return { role: turn.role, text: requireString(turn.text, `context.recentConversation[${index}].text`, { allowEmpty: true }) };
        })
      : (() => { throw new ValidationError('context.recentConversation must be an array'); })(),
  };
  if (value.git !== undefined) {
    if (!isPlainObject(value.git)) throw new ValidationError('context.git must be an object');
    out.git = { ...value.git };
  }
  return out;
}

export function validateJoinRequest(request) {
  if (!isPlainObject(request)) throw new ValidationError('join request must be an object');
  rejectSecrets(request);
  const url = requireString(request.url, 'url');
  platformForUrl(url);
  const agentSession = validateAgentSession(request.agentSession);
  const out = {
    meetingUrl: url,
    agentSession,
    context: validateContext(request.context, { required: false }),
    permissions: validatePermissions(request.permissions),
  };
  if (request.camera !== undefined) out.camera = validateCamera(request.camera);
  if (request.screenShare !== undefined) out.screenShare = validateScreenShare(request.screenShare);
  return out;
}

function validateScreenShare(value) {
  if (!isPlainObject(value)) throw new ValidationError('screenShare must be an object');
  const known = new Set(['enabled', 'captureIntervalMs', 'minChange', 'maxFrames', 'maxBytes', 'retentionSeconds']);
  for (const key of Object.keys(value)) {
    if (!known.has(key)) throw new ValidationError(`screenShare.${key} is not allowed`);
  }
  const out = {};
  if (value.enabled !== undefined) {
    if (typeof value.enabled !== 'boolean') throw new ValidationError('screenShare.enabled must be a boolean');
    out.enabled = value.enabled;
  }
  if (value.captureIntervalMs !== undefined) {
    if (!Number.isInteger(value.captureIntervalMs) || value.captureIntervalMs < 2000 || value.captureIntervalMs > 15000) {
      throw new ValidationError('screenShare.captureIntervalMs is out of bounds');
    }
    out.captureIntervalMs = value.captureIntervalMs;
  }
  if (value.minChange !== undefined) {
    if (typeof value.minChange !== 'number' || Number.isNaN(value.minChange) || value.minChange < 0 || value.minChange > 1) {
      throw new ValidationError('screenShare.minChange is out of bounds');
    }
    out.minChange = value.minChange;
  }
  if (value.maxFrames !== undefined) {
    if (!Number.isInteger(value.maxFrames) || value.maxFrames < 1 || value.maxFrames > 50) {
      throw new ValidationError('screenShare.maxFrames is out of bounds');
    }
    out.maxFrames = value.maxFrames;
  }
  if (value.maxBytes !== undefined) {
    if (!Number.isInteger(value.maxBytes) || value.maxBytes < 50_000 || value.maxBytes > 12_000_000) {
      throw new ValidationError('screenShare.maxBytes is out of bounds');
    }
    out.maxBytes = value.maxBytes;
  }
  if (value.retentionSeconds !== undefined) {
    if (!Number.isInteger(value.retentionSeconds) || value.retentionSeconds < 30 || value.retentionSeconds > 6 * 3600) {
      throw new ValidationError('screenShare.retentionSeconds is out of bounds');
    }
    out.retentionSeconds = value.retentionSeconds;
  }
  return out;
}

function validateCamera(value) {
  if (!isPlainObject(value)) throw new ValidationError('camera must be an object');
  const known = new Set(['enabled', 'defaultOn', 'avatarDataUri']);
  for (const key of Object.keys(value)) {
    if (!known.has(key)) throw new ValidationError(`camera.${key} is not allowed`);
  }
  const out = {};
  if (value.enabled !== undefined) {
    if (typeof value.enabled !== 'boolean') throw new ValidationError('camera.enabled must be a boolean');
    out.enabled = value.enabled;
  }
  if (value.defaultOn !== undefined) {
    if (typeof value.defaultOn !== 'boolean') throw new ValidationError('camera.defaultOn must be a boolean');
    out.defaultOn = value.defaultOn;
  }
  if (value.avatarDataUri !== undefined) {
    const uri = requireString(value.avatarDataUri, 'camera.avatarDataUri', { maxLength: 120000 });
    if (!uri.startsWith('data:image/')) throw new ValidationError('camera.avatarDataUri must be an image data URI');
    out.avatarDataUri = uri;
  }
  return out;
}

function joinKey(payload) {
  return JSON.stringify({
    url: payload.meetingUrl,
    provider: payload.agentSession.provider,
    sessionId: payload.agentSession.sessionId,
    workspace: payload.agentSession.workspace,
  });
}

function mapHttpError(status, payload, fallback) {
  const error = payload?.error || {};
  const code = error.code || fallback;
  const message = redact(error.message || fallback);
  if (status === 400 || status === 422) return new ValidationError(message, { status, code });
  if (status === 503 || code === 'supervisor_unavailable' || code === 'daemon_unavailable') {
    return new StartupError(message, { status, code });
  }
  if (code === 'handoff_append_failed' || code === 'finalization_failed') {
    return new FinalizationError(message, {
      status,
      code,
      archivePath: error.archivePath || payload?.archivePath,
      handoff: payload?.handoff,
    });
  }
  return new RuntimeError(message, { status, code });
}

async function readJson(response) {
  const text = await response.text();
  if (!text) return {};
  try {
    return JSON.parse(text);
  } catch {
    return { error: { message: text.slice(0, 200) } };
  }
}

export function parseSseBlock(block) {
  const event = { data: '', event: 'message', id: '' };
  for (const rawLine of block.split('\n')) {
    const line = rawLine.replace(/\r$/, '');
    if (!line || line.startsWith(':')) continue;
    const idx = line.indexOf(':');
    const field = idx === -1 ? line : line.slice(0, idx);
    let value = idx === -1 ? '' : line.slice(idx + 1);
    if (value.startsWith(' ')) value = value.slice(1);
    if (field === 'data') event.data = event.data ? `${event.data}\n${value}` : value;
    else if (field === 'event') event.event = value;
    else if (field === 'id') event.id = value;
  }
  if (!event.data && !event.id) return null;
  let payload = {};
  if (event.data) {
    try {
      payload = JSON.parse(event.data);
    } catch {
      payload = { raw: event.data };
    }
  }
  if (event.id && !payload.id) payload.id = event.id;
  if (event.event && event.event !== 'message' && !payload.type) payload.type = event.event;
  return payload;
}

export async function* iterateSse(stream, { lastEventId, seen } = {}) {
  const decoder = new TextDecoder();
  let buffer = '';
  let delivered = seen || new Set();
  for await (const chunk of stream) {
    buffer += typeof chunk === 'string' ? chunk : decoder.decode(chunk, { stream: true });
    buffer = buffer.replace(/\r\n/g, '\n');
    let idx;
    while ((idx = buffer.indexOf('\n\n')) !== -1) {
      const block = buffer.slice(0, idx);
      buffer = buffer.slice(idx + 2);
      const event = parseSseBlock(block);
      if (!event) continue;
      const id = event.id;
      if (id) {
        if (delivered.has(id)) continue;
        delivered.add(id);
        lastEventId = id;
      }
      yield { event, lastEventId };
    }
  }
}

function defaultDaemonAuthPath(root) {
  return `${root}/.colleague/daemon.auth`;
}

async function readDaemonAuth(root, fsApi) {
  const path = defaultDaemonAuthPath(root);
  let raw;
  try {
    raw = await fsApi.readFile(path, 'utf8');
  } catch {
    return '';
  }
    try {
      const parsed = JSON.parse(raw);
      if (parsed && typeof parsed === 'object' && typeof parsed.token === 'string') return parsed.token;
    } catch {
      // Host-only daemon.auth is a raw token file.
    }
    return raw.trim();
}

async function waitForPort(host, port, timeoutMs, isPortOpen) {
  const started = Date.now();
  while (Date.now() - started < timeoutMs) {
    if (await isPortOpen(host, port)) return true;
    await new Promise((resolve) => setTimeout(resolve, 50));
  }
  return false;
}

function defaultIsPortOpen(host, port) {
  return new Promise((resolve) => {
    const socket = net.connect({ host, port });
    socket.once('connect', () => {
      socket.end();
      resolve(true);
    });
    socket.once('error', () => resolve(false));
    socket.setTimeout(300, () => {
      socket.destroy();
      resolve(false);
    });
  });
}

function defaultSpawnDaemon({ root, host, port }) {
  const repoRoot = path.resolve(root);
  const script = path.join(repoRoot, 'start-runtime-daemon.sh');
  return spawn('bash', [script], {
    cwd: repoRoot,
    detached: true,
    stdio: 'ignore',
    env: {
      ...process.env,
      COLLEAGUE_ROOT: repoRoot,
      COLLEAGUE_DAEMON_HOST: host,
      COLLEAGUE_DAEMON_PORT: String(port),
    },
  });
}

export function createLoopbackTransport(options = {}) {
  const fsApi = options.fs || { readFile };
  const root = options.root || process.cwd();
  const host = options.host || '127.0.0.1';
  const port = Number(options.port || process.env.COLLEAGUE_DAEMON_PORT || 8765);
  const origin = `http://${host}:${port}`;
  const fetchImpl = options.fetchImpl || globalThis.fetch.bind(globalThis);
  const spawnDaemon = options.spawnDaemon === undefined
    ? (options.autostart === false ? null : defaultSpawnDaemon)
    : options.spawnDaemon;
  const isPortOpen = options.isPortOpen || ((checkHost, checkPort) => defaultIsPortOpen(checkHost, checkPort));
  let starting = null;
  let token = '';

  async function ensureDaemon() {
    if (await isPortOpen(host, port)) {
      token = token || await readAuth();
      if (!token) {
        const deadline = Date.now() + (options.startupTimeoutMs || 8000);
        while (Date.now() < deadline && !token) {
          await new Promise((resolve) => setTimeout(resolve, 50));
          token = await readAuth();
        }
      }
      if (!token) throw new StartupError('runtime daemon is running but its auth token is missing', { code: 'daemon_unavailable' });
      return;
    }
    if (!spawnDaemon) {
      throw new StartupError('runtime daemon is not running', { code: 'daemon_unavailable' });
    }
    if (!starting) {
      starting = Promise.resolve()
        .then(() => spawnDaemon({ root, host, port }))
        .then(async (child) => {
          child?.unref?.();
          const ready = await waitForPort(host, port, options.startupTimeoutMs || 8000, isPortOpen);
          if (!ready) throw new StartupError('runtime daemon did not become ready', { code: 'daemon_unavailable' });
        })
        .finally(() => { starting = null; });
    }
    await starting;
    token = await readAuth();
  }

  async function readAuth() {
    if (options.readAuth) return options.readAuth();
    if (!fsApi?.readFile) return token;
    return readDaemonAuth(root, fsApi);
  }

  async function request(method, path, { body, headers = {}, lastEventId, signal } = {}, retried = false) {
    await ensureDaemon();
    if (!token) token = await readAuth();
    const requestHeaders = {
      ...headers,
      Authorization: `Bearer ${token}`,
    };
    if (body !== undefined) requestHeaders['Content-Type'] = 'application/json';
    if (lastEventId) requestHeaders['Last-Event-ID'] = lastEventId;
    let response;
    try {
      response = await fetchImpl(`${origin}${path}`, {
        method,
        headers: requestHeaders,
        body: body === undefined ? undefined : JSON.stringify(body),
        signal,
      });
    } catch (error) {
      throw new StartupError(redact(error.message || 'daemon unreachable'), { code: 'daemon_unavailable' });
    }
    if (response.status === 401 && !retried) {
      token = await readAuth();
      if (token) return request(method, path, { body, headers, lastEventId, signal }, true);
    }
    return response;
  }

  async function json(method, path, body) {
    const response = await request(method, path, { body });
    const payload = await readJson(response);
    if (!response.ok) throw mapHttpError(response.status, payload, `${method} ${path} failed`);
    return payload;
  }

  return {
    origin,
    async createMeeting(payload) {
      try {
        return await json('POST', '/v1/meetings', payload);
      } catch (error) {
        if (error instanceof ValidationError) throw error;
        if (error instanceof ColleagueError && error.code === 'validation') throw error;
        if (error.status && error.status < 500 && error.status !== 409 && !(error instanceof StartupError)) {
          throw new StartupError(error.message, { status: error.status, code: error.code });
        }
        throw error;
      }
    },
    getMeeting(meetingId) {
      return json('GET', `/v1/meetings/${encodeURIComponent(meetingId)}`);
    },
    updateContext(meetingId, context) {
      return json('POST', `/v1/meetings/${encodeURIComponent(meetingId)}/context`, context);
    },
    cancelMeeting(meetingId) {
      return json('POST', `/v1/meetings/${encodeURIComponent(meetingId)}/cancel`, {});
    },
    getHandoff(meetingId) {
      return json('GET', `/v1/meetings/${encodeURIComponent(meetingId)}/handoff`);
    },
    retryHandoff(meetingId) {
      return json('POST', `/v1/meetings/${encodeURIComponent(meetingId)}/handoff/retry`, {});
    },
    listApprovals(meetingId) {
      return json('GET', `/v1/meetings/${encodeURIComponent(meetingId)}/approvals`);
    },
    getApproval(meetingId, approvalId) {
      return json('GET', `/v1/meetings/${encodeURIComponent(meetingId)}/approvals/${encodeURIComponent(approvalId)}`);
    },
    createApproval(meetingId, payload) {
      return json('POST', `/v1/meetings/${encodeURIComponent(meetingId)}/approvals`, payload);
    },
    decideApproval(meetingId, approvalId, decision) {
      const body = typeof decision === 'string' ? { decision } : decision;
      return json('POST', `/v1/meetings/${encodeURIComponent(meetingId)}/approvals/${encodeURIComponent(approvalId)}/decision`, body);
    },
    listArtifacts(meetingId) {
      return json('GET', `/v1/meetings/${encodeURIComponent(meetingId)}/artifacts`);
    },
    getArtifact(meetingId, artifactId) {
      return json('GET', `/v1/meetings/${encodeURIComponent(meetingId)}/artifacts/${encodeURIComponent(artifactId)}`);
    },
    async getArtifactContent(meetingId, artifactId) {
      const response = await request('GET', `/v1/meetings/${encodeURIComponent(meetingId)}/artifacts/${encodeURIComponent(artifactId)}/content`);
      if (!response.ok) {
        const payload = await readJson(response);
        throw mapHttpError(response.status, payload, `GET artifact content failed`);
      }
      return {
        mediaType: response.headers.get('content-type') || 'application/octet-stream',
        body: Buffer.from(await response.arrayBuffer()),
      };
    },
    createCommit(meetingId, payload) {
      return json('POST', `/v1/meetings/${encodeURIComponent(meetingId)}/commits`, payload);
    },
    listCommits(meetingId) {
      return json('GET', `/v1/meetings/${encodeURIComponent(meetingId)}/commits`);
    },
    getCommit(meetingId, operationId) {
      return json('GET', `/v1/meetings/${encodeURIComponent(meetingId)}/commits/${encodeURIComponent(operationId)}`);
    },
    createPush(meetingId, payload) {
      return json('POST', `/v1/meetings/${encodeURIComponent(meetingId)}/pushes`, payload);
    },
    listPushes(meetingId) {
      return json('GET', `/v1/meetings/${encodeURIComponent(meetingId)}/pushes`);
    },
    getPush(meetingId, operationId) {
      return json('GET', `/v1/meetings/${encodeURIComponent(meetingId)}/pushes/${encodeURIComponent(operationId)}`);
    },
    getScreenShare(meetingId) {
      return json('GET', `/v1/meetings/${encodeURIComponent(meetingId)}/screen-share`);
    },
    pauseScreenShare(meetingId) {
      return json('POST', `/v1/meetings/${encodeURIComponent(meetingId)}/screen-share/pause`, {});
    },
    resumeScreenShare(meetingId) {
      return json('POST', `/v1/meetings/${encodeURIComponent(meetingId)}/screen-share/resume`, {});
    },
    listScreenShareObservations(meetingId) {
      return json('GET', `/v1/meetings/${encodeURIComponent(meetingId)}/screen-share/observations`);
    },
    async *events(meetingId, { lastEventId = '', signal, seen } = {}) {
      const delivered = seen || new Set();
      let cursor = lastEventId;
      while (!signal?.aborted) {
        await ensureDaemon();
        if (!token) token = await readAuth();
        const streamPath = `/v1/meetings/${encodeURIComponent(meetingId)}/events`;
        try {
          if (options.sseImpl) {
            const stream = options.sseImpl({ lastEventId: cursor, signal, token });
            for await (const item of iterateSse(stream, { lastEventId: cursor, seen: delivered })) {
              cursor = item.lastEventId || cursor;
              yield item.event;
            }
            continue;
          }
          const incoming = await new Promise((resolve, reject) => {
            const req = http.get({
              hostname: host,
              port,
              path: streamPath,
              headers: {
                Authorization: `Bearer ${token}`,
                Accept: 'text/event-stream',
                ...(cursor ? { 'Last-Event-ID': cursor } : {}),
              },
            }, resolve);
            req.on('error', reject);
            const onAbort = () => {
              req.destroy();
              reject(Object.assign(new Error('aborted'), { name: 'AbortError' }));
            };
            if (signal?.aborted) {
              onAbort();
              return;
            }
            signal?.addEventListener('abort', onAbort, { once: true });
          });
          if (incoming.statusCode === 401) {
            incoming.resume();
            token = await readAuth();
            continue;
          }
          if (incoming.statusCode && incoming.statusCode >= 400) {
            incoming.resume();
            await new Promise((resolve) => setTimeout(resolve, 100));
            continue;
          }
          for await (const item of iterateSse(incoming, { lastEventId: cursor, seen: delivered })) {
            cursor = item.lastEventId || cursor;
            yield item.event;
          }
        } catch (error) {
          if (signal?.aborted || error?.name === 'AbortError') return;
          await new Promise((resolve) => setTimeout(resolve, 100));
        }
      }
    },
  };
}

class MeetingHandleImpl {
  constructor(session, transport) {
    this.id = session.id;
    this._session = session;
    this._transport = transport;
    this._listeners = new Map();
    this._seen = new Set();
    this._lastEventId = '';
    this._abort = new AbortController();
    this._cancelled = false;
    this._finishedSettled = false;
    this.finished = this._waitFinished();
    this._pump = this._runPump();
  }

  async status() {
    this._session = await this._transport.getMeeting(this.id);
    return this._session;
  }

  async addContext(context) {
    const validated = validateContext(context);
    this._session = await this._transport.updateContext(this.id, validated);
    return this._session;
  }

  async cancel() {
    if (this._cancelled) return this._session;
    this._cancelled = true;
    try {
      this._session = await this._transport.cancelMeeting(this.id);
    } catch (error) {
      if (error.status === 404 || error.status === 409) return this._session;
      throw error;
    }
    return this._session;
  }

  async retryFinalization() {
    return this._transport.retryHandoff(this.id);
  }

  async listApprovals() {
    return this._transport.listApprovals(this.id);
  }

  async getApproval(approvalId) {
    if (!approvalId) throw new ValidationError('approvalId is required');
    return this._transport.getApproval(this.id, approvalId);
  }

  async decideApproval(approvalId, decision) {
    if (!approvalId) throw new ValidationError('approvalId is required');
    const value = typeof decision === 'string' ? decision : decision?.decision;
    if (value !== 'approved' && value !== 'denied') {
      throw new ValidationError('decision must be approved or denied');
    }
    return this._transport.decideApproval(this.id, approvalId, { decision: value });
  }

  async listArtifacts() {
    return this._transport.listArtifacts(this.id);
  }

  async getArtifact(artifactId) {
    if (!artifactId) throw new ValidationError('artifactId is required');
    return this._transport.getArtifact(this.id, artifactId);
  }

  async getArtifactContent(artifactId) {
    if (!artifactId) throw new ValidationError('artifactId is required');
    return this._transport.getArtifactContent(this.id, artifactId);
  }

  async createCommit(payload) {
    return this._transport.createCommit(this.id, payload);
  }

  async listCommits() {
    return this._transport.listCommits(this.id);
  }

  async getCommit(operationId) {
    if (!operationId) throw new ValidationError('operationId is required');
    return this._transport.getCommit(this.id, operationId);
  }

  async createPush(payload) {
    return this._transport.createPush(this.id, payload);
  }

  async listPushes() {
    return this._transport.listPushes(this.id);
  }

  async getPush(operationId) {
    if (!operationId) throw new ValidationError('operationId is required');
    return this._transport.getPush(this.id, operationId);
  }

  async getScreenShare() {
    return this._transport.getScreenShare(this.id);
  }

  async pauseScreenShare() {
    return this._transport.pauseScreenShare(this.id);
  }

  async resumeScreenShare() {
    return this._transport.resumeScreenShare(this.id);
  }

  async listScreenShareObservations() {
    return this._transport.listScreenShareObservations(this.id);
  }

  on(name, handler) {
    if (typeof handler !== 'function') throw new ValidationError('event handler must be a function');
    const bucket = this._listeners.get(name) || new Set();
    bucket.add(handler);
    this._listeners.set(name, bucket);
    return () => bucket.delete(handler);
  }

  events() {
    const queue = [];
    let notify;
    const wake = () => { notify?.(); };
    const off = this.on('event', (event) => {
      queue.push(event);
      wake();
    });
    const handle = this;
    return {
      async *[Symbol.asyncIterator]() {
        try {
          while (!handle._finishedSettled || queue.length) {
            if (!queue.length) {
              await new Promise((resolve) => { notify = resolve; });
              notify = null;
              continue;
            }
            yield queue.shift();
          }
        } finally {
          off();
        }
      },
    };
  }

  _emit(event) {
    const type = event?.type || '';
    const fire = (name, payload) => {
      for (const handler of this._listeners.get(name) || []) handler(payload);
    };
    fire('event', event);
    if (type.startsWith('meeting.')) fire('state', event);
    if (type.startsWith('transcript.')) fire('transcript', event);
    if (type.startsWith('delegation.')) fire('delegation', event);
    if (type.startsWith('approval.')) fire('approval', event);
    if (type === 'approval.required' || type === 'approval_required') fire('approval_required', event);
    if (type.startsWith('workspace.action.')) fire('workspace', event);
    if (type.startsWith('git.action.')) fire('git', event);
    if (type === 'artifact.created') fire('artifact', event);
    if (type.startsWith('screen_share.')) fire('screen_share', event);
  }

  async _runPump() {
    try {
      for await (const event of this._transport.events(this.id, {
        lastEventId: this._lastEventId,
        signal: this._abort.signal,
        seen: this._seen,
      })) {
        if (event?.id) this._lastEventId = event.id;
        this._emit(event);
        if (event?.type === 'handoff.ready' && event.handoff) this._resolveFinished(event.handoff);
        if (event?.type === 'handoff.append_failed') this._noteAppendFailed(event);
      }
    } catch {
      // Reconnect is the transport's job; never treat this as completion.
    }
  }

  _noteAppendFailed(event) {
    this._appendFailed = event;
  }

  _resolveFinished(handoff) {
    if (this._finishedSettled) return;
    this._finishedSettled = true;
    this._handoff = handoff;
    this._abort.abort();
    this._finishResolve?.(handoff);
  }

  _rejectFinished(error) {
    if (this._finishedSettled) return;
    this._finishedSettled = true;
    this._abort.abort();
    this._finishReject?.(error);
  }

  _waitFinished() {
    return new Promise((resolve, reject) => {
      this._finishResolve = resolve;
      this._finishReject = reject;
      this._pollHandoff();
    });
  }

  async _pollHandoff() {
    let delay = 40;
    while (!this._finishedSettled) {
      try {
        const handoff = await this._transport.getHandoff(this.id);
        if (handoff?.meetingId) {
          this._resolveFinished(handoff);
          return;
        }
      } catch (error) {
        if (error instanceof FinalizationError && error.handoff?.partial) {
          this._resolveFinished(error.handoff);
          return;
        }
        if (error instanceof FinalizationError && !error.handoff) {
          if (this._appendFailed?.retryable) {
            try {
              const handoff = await this._transport.retryHandoff(this.id);
              this._resolveFinished(handoff);
              return;
            } catch {
              // Keep waiting through retryable append failures.
            }
          } else if (this._appendFailed && this._appendFailed.retryable === false) {
            this._rejectFinished(new FinalizationError(error.message, {
              archivePath: error.archivePath || `recordings/${this.id}`,
              status: error.status,
            }));
            return;
          }
        }
        if (error instanceof StartupError && error.code === 'daemon_unavailable') {
          // Keep polling; socket closure is not completion.
        }
      }
      await new Promise((resolve) => setTimeout(resolve, delay));
      delay = Math.min(delay * 1.5, 250);
    }
  }
}

export class Colleague {
  constructor(options = {}) {
    this._transport = options.transport || createLoopbackTransport(options);
    this._inflight = new Map();
    this._handles = new Map();
  }

  async joinMeeting(request) {
    const payload = validateJoinRequest(request);
    const key = joinKey(payload);
    if (this._inflight.has(key)) return this._inflight.get(key);
    const pending = this._join(payload, key);
    this._inflight.set(key, pending);
    try {
      return await pending;
    } finally {
      this._inflight.delete(key);
    }
  }

  async _join(payload, key) {
    const existing = [...this._handles.values()].find((handle) => joinKey({
      meetingUrl: handle._session.meetingUrl,
      agentSession: handle._session.agentSession,
    }) === key);
    if (existing) return existing;
    const session = await this._transport.createMeeting(payload);
    if (this._handles.has(session.id)) return this._handles.get(session.id);
    const handle = new MeetingHandleImpl(session, this._transport);
    this._handles.set(session.id, handle);
    return handle;
  }
}

export default Colleague;
