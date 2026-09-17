import fs from 'node:fs';
import net from 'node:net';
import path from 'node:path';
import { spawn } from 'node:child_process';

export const DEFAULT_DAEMON_HOST = '127.0.0.1';
export const DEFAULT_DAEMON_PORT = 8765;

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
  const host = options.host || process.env.COLLEAGUE_DAEMON_HOST || DEFAULT_DAEMON_HOST;
  const port = Number(options.port || process.env.COLLEAGUE_DAEMON_PORT || DEFAULT_DAEMON_PORT);
  const tokenPath = options.tokenPath || path.join(root, '.colleague', 'daemon.auth');
  const fetchImpl = options.fetchImpl || fetch;
  const isPortOpen = options.isPortOpen || (() => portOpen(host, port));
  const spawnDaemon = options.spawnDaemon || (() => spawn('/bin/bash', ['start-runtime-daemon.sh'], {
    cwd: root,
    detached: true,
    stdio: 'ignore',
    env: process.env,
  }));
  const wait = options.wait || sleep;
  const timeoutMs = options.timeoutMs || 10_000;
  const pollMs = options.pollMs || 50;
  const owned = { child: null };
  let ensuring = null;

  function baseUrl() {
    return `http://${host}:${port}`;
  }

  async function waitForToken() {
    const deadline = Date.now() + timeoutMs;
    while (Date.now() < deadline) {
      const token = readTokenFile(tokenPath);
      if (token) return token;
      await wait(pollMs);
    }
    throw daemonError('Runtime daemon did not become ready.');
  }

  async function startDaemon() {
    const child = spawnDaemon();
    owned.child = child;
    child?.unref?.();
    child?.on?.('error', () => {});
    return waitForToken();
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
      if (!startIfNeeded) throw daemonError('Runtime daemon is not running.', { code: 'daemon_offline' });
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

  return {
    host,
    port,
    tokenPath,
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
    retryHandoff(meetingId) {
      return send('POST', `/v1/meetings/${meetingId}/handoff/retry`, {}, { startIfNeeded: true });
    },
    listApprovals(meetingId) {
      return send('GET', `/v1/meetings/${meetingId}/approvals`, undefined, { startIfNeeded: false });
    },
    getApproval(meetingId, approvalId) {
      return send('GET', `/v1/meetings/${meetingId}/approvals/${approvalId}`, undefined, { startIfNeeded: false });
    },
    decideApproval(meetingId, approvalId, decision) {
      const body = typeof decision === 'string' ? { decision } : decision;
      return send('POST', `/v1/meetings/${meetingId}/approvals/${approvalId}/decision`, body, { startIfNeeded: true });
    },
    listArtifacts(meetingId) {
      return send('GET', `/v1/meetings/${meetingId}/artifacts`, undefined, { startIfNeeded: false });
    },
    getArtifact(meetingId, artifactId) {
      return send('GET', `/v1/meetings/${meetingId}/artifacts/${artifactId}`, undefined, { startIfNeeded: false });
    },
    async getArtifactContent(meetingId, artifactId, { startIfNeeded = false } = {}) {
      let token = startIfNeeded ? await ensure() : readTokenFile(tokenPath);
      if (!token) {
        if (!startIfNeeded) throw daemonError('Runtime daemon is not running.', { code: 'daemon_offline' });
        token = await ensure();
      }
      const headers = { Authorization: `Bearer ${token}` };
      let response;
      try {
        response = await fetchImpl(`${baseUrl()}/v1/meetings/${meetingId}/artifacts/${artifactId}/content`, {
          method: 'GET',
          headers,
          signal: AbortSignal.timeout(30_000),
        });
      } catch {
        throw daemonError('Runtime daemon is unavailable. Start it or retry from the console.');
      }
      if (response.status === 401) {
        throw daemonError('Runtime daemon rejected the auth token.', { status: 401, code: 'unauthorized' });
      }
      if (!response.ok) {
        const text = await response.text();
        const payload = parseDaemonBody(text);
        throw daemonError(payload?.error?.message || 'Runtime daemon request failed.', {
          status: response.status,
          code: payload?.error?.code || 'daemon_error',
        });
      }
      return {
        mediaType: response.headers.get('content-type') || 'application/octet-stream',
        body: Buffer.from(await response.arrayBuffer()),
      };
    },
    leaseStatus(provider, sessionId) {
      return send('GET', `/v1/agent-sessions/${provider}/${sessionId}/status`, undefined, { startIfNeeded: false });
    },
  };
}
