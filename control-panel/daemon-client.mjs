import fs from 'node:fs';
import net from 'node:net';
import path from 'node:path';
import { spawn } from 'node:child_process';
import { fileURLToPath } from 'node:url';

import { MANAGED_NOT_RUNNING, isManaged } from '../packages/sdk-typescript/src/index.mjs';

export const DEFAULT_DAEMON_HOST = '127.0.0.1';
export const DEFAULT_DAEMON_PORT = 8765;
export const DEFAULT_DAEMON_READY_MS = 60_000;
// start-runtime-daemon.sh lives with this code, never under the data root.
const CODE_ROOT = path.dirname(path.dirname(fileURLToPath(import.meta.url)));

function sleep(ms) {
  return new Promise(resolve => setTimeout(resolve, ms));
}

function readTokenFile(tokenPath) {
  try {
    const token = fs.readFileSync(tokenPath, 'utf8').trim();
    return token || null;
  } catch (error) {
    if (error.code === 'ENOENT') return null;
    throw error;
  }
}

function portOpen(host, port, timeout = 250) {
  return new Promise(resolve => {
    const socket = net.connect({ host, port });
    const timer = setTimeout(() => {
      socket.destroy();
      resolve(false);
    }, timeout);
    socket.once('connect', () => {
      clearTimeout(timer);
      socket.end();
      resolve(true);
    });
    socket.once('error', () => {
      clearTimeout(timer);
      resolve(false);
    });
  });
}

export function daemonError(message, { status = 503, code = 'daemon_unavailable' } = {}) {
  const error = new Error(message);
  error.status = status;
  error.code = code;
  return error;
}

function parseDaemonBody(text) {
  if (!text) return {};
  try {
    return JSON.parse(text);
  } catch {
    return {};
  }
}

export function createDaemonClient(options = {}) {
  const root = options.root;
  const codeRoot = options.codeRoot || CODE_ROOT;
  // In the container (COLLEAGUE_MANAGED=1) the container runs the daemon; never start one here.
  const managed = options.managed ?? isManaged();
  const host = options.host || process.env.COLLEAGUE_DAEMON_HOST || DEFAULT_DAEMON_HOST;
  const port = Number(options.port || process.env.COLLEAGUE_DAEMON_PORT || DEFAULT_DAEMON_PORT);
  const tokenPath = options.tokenPath || path.join(root, '.colleague', 'daemon.auth');
  const fetchImpl = options.fetchImpl || fetch;
  const isPortOpen = options.isPortOpen || (() => portOpen(host, port));
  const spawnDaemon = options.spawnDaemon || (() => {
    const logDir = path.join(root, '.colleague');
    fs.mkdirSync(logDir, { recursive: true, mode: 0o700 });
    const log = fs.openSync(path.join(logDir, 'daemon.log'), 'a');
    const child = spawn('/bin/bash', [path.join(codeRoot, 'start-runtime-daemon.sh')], {
      cwd: codeRoot,
      detached: true,
      stdio: ['ignore', log, log],
      env: process.env,
    });
    fs.closeSync(log);
    return child;
  });
  const wait = options.wait || sleep;
  const timeoutMs = options.timeoutMs || DEFAULT_DAEMON_READY_MS;
  const pollMs = options.pollMs || 50;
  const owned = { child: null };
  let ensuring = null;

  function baseUrl() {
    return `http://${host}:${port}`;
  }

  async function waitForToken(child) {
    const deadline = Date.now() + timeoutMs;
    let exitCode = child != null && child.exitCode != null ? child.exitCode : null;
    child?.on?.('exit', (code) => { exitCode = code ?? 1; });
    child?.on?.('error', () => { exitCode = -1; });
    while (Date.now() < deadline) {
      const token = readTokenFile(tokenPath);
      if (token) return token;
      if (exitCode !== null) {
        throw daemonError('Runtime daemon failed to start. Check .colleague/daemon.log.');
      }
      await wait(pollMs);
    }
    throw daemonError('Runtime daemon did not become ready.');
  }

  async function startDaemon() {
    if (managed) throw daemonError(MANAGED_NOT_RUNNING, { code: 'daemon_unavailable' });
    const child = spawnDaemon();
    owned.child = child;
    child?.unref?.();
    return waitForToken(child);
  }

  async function doEnsure() {
    if (await isPortOpen()) {
      const token = readTokenFile(tokenPath) || await waitForToken();
      if (!token) throw daemonError('Runtime daemon is running but its auth token is missing.');
      return token;
    }
    return startDaemon();
  }

  async function ensure() {
    if (!ensuring) {
      ensuring = doEnsure().finally(() => { ensuring = null; });
    }
    return ensuring;
  }

  async function rawRequest(method, pathname, body, token) {
    const headers = { Authorization: `Bearer ${token}` };
    const init = { method, headers, signal: AbortSignal.timeout(30_000) };
    if (body !== undefined) {
      headers['Content-Type'] = 'application/json';
      init.body = JSON.stringify(body);
    }
    let response;
    try {
      response = await fetchImpl(`${baseUrl()}${pathname}`, init);
    } catch {
      throw daemonError('Runtime daemon is unavailable. Start it or retry from the console.');
    }
    const text = await response.text();
    const payload = parseDaemonBody(text);
    if (response.status === 401) {
      const error = daemonError('Runtime daemon rejected the auth token.', {
        status: 401, code: 'unauthorized',
      });
      error.retryable = true;
      throw error;
    }
    if (!response.ok) {
      const message = payload?.error?.message || 'Runtime daemon request failed.';
      throw daemonError(message, {
        status: response.status,
        code: payload?.error?.code || 'daemon_error',
      });
    }
    return payload;
  }

  async function send(method, pathname, body, { startIfNeeded = true } = {}) {
    let token = startIfNeeded ? await ensure() : readTokenFile(tokenPath);
    if (!token) {
      if (!startIfNeeded) throw daemonError(managed ? MANAGED_NOT_RUNNING : 'Runtime daemon is not running.', { code: 'daemon_offline' });
      token = await ensure();
    }
    try {
      return await rawRequest(method, pathname, body, token);
    } catch (error) {
      if (error.retryable) {
        const refreshed = readTokenFile(tokenPath);
        if (refreshed && refreshed !== token) {
          return rawRequest(method, pathname, body, refreshed);
        }
      }
      throw error;
    }
  }

  // A recorded call's audio, passed through as bytes rather than JSON.
  async function downloadRecording(callId, format) {
    const token = readTokenFile(tokenPath);
    if (!token) throw daemonError(managed ? MANAGED_NOT_RUNNING : 'Runtime daemon is not running.', { code: 'daemon_offline' });
    let response;
    try {
      response = await fetchImpl(`${baseUrl()}/v1/calls/${encodeURIComponent(callId)}/recording?format=${format}`, {
        headers: { Authorization: `Bearer ${token}` }, signal: AbortSignal.timeout(120_000),
      });
    } catch {
      throw daemonError('Runtime daemon is unavailable. Start it or retry from the console.');
    }
    if (!response.ok) {
      const payload = parseDaemonBody(await response.text());
      throw daemonError(payload?.error?.message || 'The recording could not be downloaded.', {
        status: response.status, code: payload?.error?.code || 'daemon_error',
      });
    }
    return {
      contentType: response.headers.get('content-type') || 'application/octet-stream',
      body: Buffer.from(await response.arrayBuffer()),
    };
  }

  return {
    host,
    port,
    tokenPath,
    downloadRecording,
    managed,
    get child() { return owned.child; },
    ensure,
    request(method, pathname, body) {
      return send(method, pathname, body, { startIfNeeded: true });
    },
    probe(method, pathname, body) {
      return send(method, pathname, body, { startIfNeeded: false });
    },
    createMeeting(payload) {
      return send('POST', '/v1/meetings', payload, { startIfNeeded: true });
    },
    getMeeting(meetingId, { startIfNeeded = false } = {}) {
      return send('GET', `/v1/meetings/${meetingId}`, undefined, { startIfNeeded });
    },
    updateContext(meetingId, payload) {
      return send('POST', `/v1/meetings/${meetingId}/context`, payload, { startIfNeeded: true });
    },
    cancelMeeting(meetingId) {
      return send('POST', `/v1/meetings/${meetingId}/cancel`, {}, { startIfNeeded: true });
    },
    getHandoff(meetingId, { startIfNeeded = false } = {}) {
      return send('GET', `/v1/meetings/${meetingId}/handoff`, undefined, { startIfNeeded });
    },
    listCalls(limit = 30, tzOffset = '') {
      // tzOffset: the reader's minutes east of UTC, so spend is grouped by their local day.
      const zone = /^-?\d{1,3}$/.test(String(tzOffset)) ? `&tzOffset=${tzOffset}` : '';
      return send('GET', `/v1/calls?limit=${Number(limit) || 30}${zone}`, undefined, { startIfNeeded: false });
    },
    getCall(callId) {
      return send('GET', `/v1/calls/${encodeURIComponent(callId)}`, undefined, { startIfNeeded: false });
    },
    callEvents(callId, after = '') {
      const cursor = /^\d+$/.test(String(after)) ? `&after=${after}` : '';
      return send('GET', `/v1/calls/${encodeURIComponent(callId)}/events?format=json${cursor}`, undefined, { startIfNeeded: false });
    },
    endCall(callId) {
      return send('POST', `/v1/calls/${encodeURIComponent(callId)}/end`, {}, { startIfNeeded: false });
    },
    transferCall(callId) {
      return send('POST', `/v1/calls/${encodeURIComponent(callId)}/transfer`, {}, { startIfNeeded: false });
    },
  };
}
