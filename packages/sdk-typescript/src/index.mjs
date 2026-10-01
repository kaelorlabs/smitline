import { readFile } from 'node:fs/promises';
import { spawn } from 'node:child_process';
import net from 'node:net';
import path from 'node:path';

export const SCHEMA_VERSION = 1;
export const SDK_VERSION = '0.1.1';

export const EXIT = Object.freeze({
  ok: 0,
  validation: 2,
  startup: 3,
  runtime: 4,
  partial: 5,
  finalization: 6,
  interrupt: 130,
});

// The first start creates a Python environment and installs packages.
const DEFAULT_STARTUP_TIMEOUT_MS = 60000;

// In the Smitline container (COLLEAGUE_MANAGED=1) the container runs the daemon;
// nothing else may start one.
export const MANAGED_NOT_RUNNING = 'Smitline is not running; restart the container: docker restart smitline';

export function isManaged(env = process.env) {
  return String(env?.COLLEAGUE_MANAGED ?? '').trim() === '1';
}

export class ColleagueError extends Error {
  constructor(message, { code = 'runtime', status, archivePath } = {}) {
    super(redact(message));
    this.name = this.constructor.name;
    this.code = code;
    if (status != null) this.status = status;
    if (archivePath) this.archivePath = archivePath;
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

function mapHttpError(status, payload, fallback) {
  const mapped = mapHttpErrorClass(status, payload, fallback);
  const { code: _code, message: _message, ...details } = payload?.error || {};
  if (Object.keys(details).length) mapped.details = details;
  return mapped;
}

function mapHttpErrorClass(status, payload, fallback) {
  const error = payload?.error || {};
  const code = error.code || fallback;
  const message = redact(error.message || fallback);
  if (status === 400 || status === 422) return new ValidationError(message, { status, code });
  if (status === 503 || code === 'supervisor_unavailable' || code === 'daemon_unavailable') {
    return new StartupError(message, { status, code });
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
  return new Promise((resolve, reject) => {
    const socket = net.connect({ host, port });
    socket.once('connect', () => {
      socket.end();
      resolve(true);
    });
    socket.once('error', (error) => {
      if (error?.code === 'EPERM' || error?.code === 'EACCES') {
        reject(new StartupError(
          `local loopback access to ${host}:${port} was blocked; allow the command to access the Smitline daemon and retry`,
          { code: 'loopback_access_denied' },
        ));
        return;
      }
      resolve(false);
    });
    socket.setTimeout(300, () => {
      socket.destroy();
      resolve(false);
    });
  });
}

// start-runtime-daemon.sh lives with the code (codeRoot); the daemon keeps its data,
// .env, and auth token under the data root (COLLEAGUE_ROOT).
function defaultSpawnDaemon({ root, codeRoot, host, port }) {
  const dataRoot = path.resolve(root);
  const scriptRoot = path.resolve(codeRoot || root);
  const script = path.join(scriptRoot, 'start-runtime-daemon.sh');
  return spawn('bash', [script], {
    cwd: scriptRoot,
    detached: true,
    stdio: 'ignore',
    env: {
      ...process.env,
      COLLEAGUE_ROOT: dataRoot,
      COLLEAGUE_DAEMON_HOST: host,
      COLLEAGUE_DAEMON_PORT: String(port),
    },
  });
}
export function createLoopbackTransport(options = {}) {
  const fsApi = options.fs || { readFile };
  const root = options.root || process.env.COLLEAGUE_ROOT || process.cwd();
  const codeRoot = options.codeRoot || root;
  const managed = options.managed ?? isManaged();
  const host = options.host || '127.0.0.1';
  const port = Number(options.port || process.env.COLLEAGUE_DAEMON_PORT || 8765);
  const origin = `http://${host}:${port}`;
  const fetchImpl = options.fetchImpl || globalThis.fetch.bind(globalThis);
  // Managed: the container owns the daemon, so it is never started from here.
  const spawnDaemon = managed ? null : options.spawnDaemon === undefined
    ? (options.autostart === false ? null : defaultSpawnDaemon)
    : options.spawnDaemon;
  const isPortOpen = options.isPortOpen || ((checkHost, checkPort) => defaultIsPortOpen(checkHost, checkPort));
  let starting = null;
  let token = '';

  async function ensureDaemon() {
    if (await isPortOpen(host, port)) {
      token = token || await readAuth();
      if (!token) {
        const deadline = Date.now() + (options.startupTimeoutMs || DEFAULT_STARTUP_TIMEOUT_MS);
        while (Date.now() < deadline && !token) {
          await new Promise((resolve) => setTimeout(resolve, 50));
          token = await readAuth();
        }
      }
      if (!token) throw new StartupError('runtime daemon is running but its auth token is missing', { code: 'daemon_unavailable' });
      return;
    }
    if (!spawnDaemon) {
      throw new StartupError(managed ? MANAGED_NOT_RUNNING : 'runtime daemon is not running', { code: 'daemon_unavailable' });
    }
    if (!starting) {
      starting = Promise.resolve()
        .then(() => spawnDaemon({ root, codeRoot, host, port }))
        .then(async (child) => {
          child?.unref?.();
          const ready = await waitForPort(host, port, options.startupTimeoutMs || DEFAULT_STARTUP_TIMEOUT_MS, isPortOpen);
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

  async function request(method, path, { body, headers = {}, signal } = {}, retried = false) {
    await ensureDaemon();
    if (!token) token = await readAuth();
    const requestHeaders = {
      ...headers,
      Authorization: `Bearer ${token}`,
    };
    if (body !== undefined) requestHeaders['Content-Type'] = 'application/json';
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
      if (token) return request(method, path, { body, headers, signal }, true);
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
    checkCall(brief) {
      return json('POST', '/v1/calls/check', brief);
    },
    startCall(brief) {
      return json('POST', '/v1/calls', brief);
    },
    getCall(callId) {
      return json('GET', `/v1/calls/${encodeURIComponent(callId)}`);
    },
    waitForCall(callId, timeoutSeconds = 60) {
      // Node's fetch gives up on response headers after 300 s, so stay below it.
      const timeout = Math.max(0, Math.min(Number(timeoutSeconds) || 0, 280));
      return json('GET', `/v1/calls/${encodeURIComponent(callId)}/wait?timeout=${timeout}`);
    },
    listCalls(limit = 20) {
      return json('GET', `/v1/calls?limit=${Math.max(1, Math.min(Number(limit) || 20, 100))}`);
    },
    instructCall(callId, text, { silent = false } = {}) {
      return json('POST', `/v1/calls/${encodeURIComponent(callId)}/instructions`, silent ? { text, silent: true } : { text });
    },
    getProfile() {
      return json('GET', '/v1/profile');
    },
    updateProfile(update) {
      return json('PATCH', '/v1/profile', update);
    },
    getDoNotCall() {
      return json('GET', '/v1/do-not-call');
    },
    async downloadRecording(callId, { format = 'wav' } = {}) {
      const target = `/v1/calls/${encodeURIComponent(callId)}/recording?format=${encodeURIComponent(format)}`;
      const response = await request('GET', target);
      if (!response.ok) throw mapHttpError(response.status, await readJson(response), `GET ${target} failed`);
      return {
        contentType: response.headers.get('content-type') || '',
        data: new Uint8Array(await response.arrayBuffer()),
      };
    },
    updateDoNotCall(update) {
      return json('PATCH', '/v1/do-not-call', update);
    },
    endCall(callId) {
      return json('POST', `/v1/calls/${encodeURIComponent(callId)}/end`, {});
    },
    transferCall(callId) {
      return json('POST', `/v1/calls/${encodeURIComponent(callId)}/transfer`, {});
    },
    listVoices() {
      return json('GET', '/v1/voices');
    },
    // Operations, not calls: used by smitline setup stop, not part of the public SDK.
    stopDaemon() {
      return json('POST', '/v1/daemon/stop', {});
    },
  };
}

export class Colleague {
  constructor(options = {}) {
    this._transport = options.transport || createLoopbackTransport(options);
  }

  /** Validate a call brief and report missing configuration without dialing. */
  async checkCall(brief) {
    return this._transport.checkCall(brief);
  }

  /**
   * Place a phone call (channel 'phone', `to` an E.164 number) or join a meeting
   * (channel 'meeting', `to` the Zoom, Teams, or Google Meet invite URL); returns the queued call.
   */
  async startCall(brief) {
    return this._transport.startCall(brief);
  }

  async getCall(callId) {
    return this._transport.getCall(callId);
  }

  /** Long-poll until the call is terminal or the timeout (at most 280 s) passes. */
  async waitForCall(callId, timeoutSeconds = 60) {
    return this._transport.waitForCall(callId, timeoutSeconds);
  }

  async listCalls(limit = 20) {
    return (await this._transport.listCalls(limit)).calls;
  }

  /** Guidance for a call in progress; `{ silent: true }` sends a note it uses when relevant. */
  async instructCall(callId, text, options = {}) {
    return this._transport.instructCall(callId, text, options);
  }

  /** The owner's profile: who they are, the people they know, how they come across. */
  async getProfile() {
    return this._transport.getProfile();
  }

  /** Merge into the profile: fields replace, people upsert by name, removePeople drops names. */
  async updateProfile(update) {
    return this._transport.updateProfile(update);
  }

  /** A recorded call's audio: wav (each side on its own channel) or mp3 (mixed). */
  async downloadRecording(callId, options) {
    return this._transport.downloadRecording(callId, options);
  }

  /** Numbers Smitline refuses to call because the person asked not to be called again. */
  async getDoNotCall() {
    return this._transport.getDoNotCall();
  }

  /** Add numbers ({ add: [...] }) or remove them ({ remove: [...] }). */
  async updateDoNotCall(update) {
    return this._transport.updateDoNotCall(update);
  }

  async endCall(callId) {
    return this._transport.endCall(callId);
  }

  async transferCall(callId) {
    return this._transport.transferCall(callId);
  }

  async listVoices() {
    return this._transport.listVoices();
  }
}

export default Colleague;
