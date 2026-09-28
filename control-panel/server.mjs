import crypto from 'node:crypto';
import { spawn } from 'node:child_process';
import fs from 'node:fs';
import http from 'node:http';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import { parseEnv, publicSettings, validateSettings, MODELS } from './config.mjs';
import { addContext, clearContext, publicContext, readContext } from './context-store.mjs';
import { createDaemonClient } from './daemon-client.mjs';
import { launcherActive } from './lifecycle.mjs';
import {
  buildMeetingCreatePayload,
  clearActiveMeetingId,
  contextHandoffFromSources,
  continuityFromAgentSession,
  ensureDefaultWorkspace,
  meetingIsActive,
  phaseFromDaemon,
  readActiveMeetingId,
  writeActiveMeetingId,
} from './meeting-contract.mjs';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.dirname(HERE);
const CONTEXT_INDEX = path.join(ROOT, 'meeting-runtime', 'context', 'index.json');
const PORT = Number(process.env.COLLEAGUE_CONTROL_PORT || 8095);
export const DOCKER_INFO_TIMEOUT_MS = 8000;

export function loopbackHost(host) {
  const name = String(host || '').replace(/:\d+$/, '').replace(/^\[(.*)\]$/, '$1').toLowerCase();
  return name === '127.0.0.1' || name === 'localhost' || name === '::1';
}

function headers(type = 'application/json; charset=utf-8') {
  return {
    'Content-Type': type,
    'Cache-Control': 'no-store',
    'Content-Security-Policy': "default-src 'self'; style-src 'self'; script-src 'self'; connect-src 'self' http://127.0.0.1:8094; frame-src http://127.0.0.1:6082; img-src 'self' data:; base-uri 'none'; form-action 'self'; frame-ancestors 'none'",
    'X-Content-Type-Options': 'nosniff',
    'Referrer-Policy': 'no-referrer',
  };
}

function json(response, status, body) {
  response.writeHead(status, headers());
  response.end(JSON.stringify(body));
}

function publicRunner(payload) {
  if (!payload || typeof payload !== 'object') {
    return { paired: false, mode: 'loopback', protocolVersion: 1, controlPlane: 'local' };
  }
  const {
    pairingCode, deviceEnrollment, pairingSecret, enrollment, ...rest
  } = payload;
  return rest;
}

function run(command, args, options = {}) {
  const { timeoutMs, cwd = ROOT, ...spawnOptions } = options;
  return new Promise(resolve => {
    const child = spawn(command, args, { cwd, ...spawnOptions });
    let stdout = '', stderr = '';
    let settled = false;
    let timer;
    const finish = (result) => {
      if (settled) return;
      settled = true;
      if (timer) clearTimeout(timer);
      resolve(result);
    };
    child.stdout?.on('data', chunk => { stdout += chunk; });
    child.stderr?.on('data', chunk => { stderr += chunk; });
    child.on('error', error => finish({ code: -1, stdout, stderr: error.message }));
    child.on('exit', code => finish({ code: code ?? -1, stdout, stderr }));
    if (timeoutMs) {
      timer = setTimeout(() => {
        try { child.kill('SIGKILL'); } catch { /* already exited */ }
        finish({ code: -1, stdout, stderr: stderr || 'timed out' });
      }, timeoutMs);
    }
  });
}

function withTimeout(promise, timeoutMs, fallback) {
  let timer;
  return Promise.race([
    Promise.resolve(promise).finally(() => clearTimeout(timer)),
    new Promise((resolve) => {
      timer = setTimeout(() => resolve(fallback), timeoutMs);
    }),
  ]);
}

async function readBody(request, maximumBytes = 64 * 1024) {
  const chunks = [];
  let size = 0;
  for await (const chunk of request) {
    size += chunk.length;
    if (size > maximumBytes) {
      const error = new Error('Request is too large.');
      error.status = 413;
      throw error;
    }
    chunks.push(chunk);
  }
  return JSON.parse(Buffer.concat(chunks).toString('utf8') || '{}');
}

export function createServer({
  contextIndex = CONTEXT_INDEX,
  root = ROOT,
  runtimeRoot = path.join(root, 'meeting-runtime'),
  meetingEnv = path.join(root, '.env.meeting'),
  recordings = path.join(runtimeRoot, 'recordings'),
  profileRoot = path.join(runtimeRoot, 'profiles'),
  daemon = null,
  spawnAccount = null,
  runCommand = run,
  dockerTimeoutMs = DOCKER_INFO_TIMEOUT_MS,
} = {}) {
  const token = crypto.randomBytes(24).toString('base64url');
  const logs = [];
  let lastExit = null;
  let accountLauncher = null;
  let startInFlight = false;
  const daemonClient = daemon || createDaemonClient({ root });

  function addLog(source, chunk) {
    for (const line of String(chunk).split(/\r?\n/).filter(Boolean)) {
      const text = line.slice(0, 500);
      if (/Bearer\s+\S+/i.test(text)) continue;
      logs.push({ at: new Date().toISOString(), source, text });
    }
    if (logs.length > 160) logs.splice(0, logs.length - 160);
  }

  function currentEnv() {
    try { return parseEnv(fs.readFileSync(meetingEnv, 'utf8')); } catch { return {}; }
  }

  async function bridgeHealth() {
    try {
      const response = await fetch('http://127.0.0.1:8094/health', { signal: AbortSignal.timeout(700) });
      return response.ok ? await response.json() : null;
    } catch { return null; }
  }

  function sessions() {
    if (!fs.existsSync(recordings)) return [];
    return fs.readdirSync(recordings, { withFileTypes: true })
      .filter(entry => entry.isDirectory() && !entry.isSymbolicLink() && /^[\w-]+$/.test(entry.name))
      .map(entry => {
        const directory = path.join(recordings, entry.name);
        const transcript = path.join(directory, 'transcript.txt');
        const stat = fs.statSync(fs.existsSync(transcript) ? transcript : directory);
        const archive = archiveStatus(directory);
        return {
          id: entry.name,
          updatedAt: stat.mtime.toISOString(),
          hasTranscript: fs.existsSync(transcript),
          hasHandoff: archive.hasHandoff,
          handoffStatus: archive.status,
          partial: archive.partial,
          endReason: archive.endReason,
        };
      }).sort((a, b) => b.updatedAt.localeCompare(a.updatedAt)).slice(0, 30);
  }

  function secretField(name) {
    return /api[_-]?key|token|secret|password|passwd|authorization|credential|cookie|private[_-]?key|bearer|leaseid/i.test(String(name || ''));
  }

  function stripSecrets(value) {
    if (Array.isArray(value)) return value.map(stripSecrets);
    if (value && typeof value === 'object') {
      const out = {};
      for (const [key, item] of Object.entries(value)) {
        if (secretField(key)) continue;
        out[key] = stripSecrets(item);
      }
      return out;
    }
    return value;
  }

  function readJsonFile(file) {
    try {
      if (!fs.existsSync(file) || fs.lstatSync(file).isSymbolicLink()) return null;
      const payload = JSON.parse(fs.readFileSync(file, 'utf8'));
      if (!payload || typeof payload !== 'object' || Array.isArray(payload)) return null;
      return stripSecrets(payload);
    } catch {
      return null;
    }
  }

  function archiveStatus(directory) {
    const finalization = readJsonFile(path.join(directory, 'finalization.json')) || {};
    const handoff = readJsonFile(path.join(directory, 'handoff.json'));
    let status = 'none';
    if (finalization.status === 'ready') status = 'ready';
    else if (finalization.status === 'append_failed') status = 'failed';
    else if (finalization.status === 'local' || finalization.status === 'appended') status = 'pending';
    else if (handoff) status = 'pending';
    return {
      status,
      hasHandoff: Boolean(handoff),
      partial: Boolean(finalization.partial ?? handoff?.partial),
      endReason: finalization.endReason || handoff?.endReason || null,
      handoffId: finalization.handoffId || handoff?.handoffId || null,
      handoff,
    };
  }

  function sessionDirectory(id) {
    if (!/^[\w-]+$/.test(id)) return null;
    const root = path.resolve(recordings);
    const directory = path.resolve(recordings, id);
    if (directory !== root && !directory.startsWith(root + path.sep)) return null;
    try {
      if (!fs.lstatSync(directory).isDirectory() || fs.lstatSync(directory).isSymbolicLink()) return null;
    } catch {
      return null;
    }
    return directory;
  }

  async function loadMeeting() {
    const meetingId = readActiveMeetingId(root);
    if (!meetingId) return { meetingId: null, session: null, daemonError: null };
    try {
      const session = await daemonClient.getMeeting(meetingId, { startIfNeeded: false });
      if (!meetingIsActive(session)) {
        return { meetingId, session, daemonError: null };
      }
      return { meetingId, session, daemonError: null };
    } catch (error) {
      if (error.code === 'not_found' || error.status === 404) {
        clearActiveMeetingId(root);
        return { meetingId: null, session: null, daemonError: null };
      }
      return { meetingId, session: null, daemonError: error };
    }
  }

  async function leaseSnapshot(session) {
    if (!session?.agentSession) return null;
    try {
      return await daemonClient.leaseStatus(session.agentSession.provider, session.agentSession.sessionId);
    } catch {
      return null;
    }
  }

  async function pendingApprovals(meetingId, session) {
    if (!meetingId || !meetingIsActive(session)) return [];
    try {
      const payload = await daemonClient.listApprovals(meetingId);
      return (payload.approvals || []).filter((item) => item.status === 'pending');
    } catch {
      return [];
    }
  }

  async function workspaceSnapshot(meetingId, session) {
    if (!meetingId || !meetingIsActive(session)) return { artifacts: [] };
    try {
      const payload = await daemonClient.listArtifacts(meetingId);
      return { artifacts: payload.artifacts || [] };
    } catch {
      return { artifacts: [] };
    }
  }

  async function status() {
    const health = await bridgeHealth();
    const { meetingId, session, daemonError } = await loadMeeting();
    const phase = phaseFromDaemon({ session, health, daemonError });
    const running = meetingIsActive(session);
    const lease = running ? await leaseSnapshot(session) : null;
    const approvals = running ? await pendingApprovals(meetingId, session) : [];
    const workspace = running ? await workspaceSnapshot(meetingId, session) : { artifacts: [] };
    let gitOperations = [];
    let screenShare = null;
    let providers = [];
    let runner = { paired: false, mode: 'loopback', protocolVersion: 1, controlPlane: 'local' };
    try {
      const listed = await daemonClient.listProviders();
      providers = listed.providers || [];
    } catch {
      providers = [];
    }
    try {
      runner = publicRunner(await daemonClient.runnerStatus());
    } catch {
      runner = { paired: false, mode: 'loopback', protocolVersion: 1, controlPlane: 'local' };
    }
    if (running && meetingId) {
      try {
        const commits = await daemonClient.listCommits(meetingId);
        const pushes = await daemonClient.listPushes(meetingId);
        gitOperations = [...(commits.commits || []), ...(pushes.pushes || [])];
      } catch {
        gitOperations = [];
      }
      try {
        screenShare = await daemonClient.getScreenShare(meetingId);
      } catch {
        screenShare = null;
      }
    }
    return {
      phase,
      running,
      meetingId: meetingId || undefined,
      continuity: session ? continuityFromAgentSession(session.agentSession) : undefined,
      health,
      lease,
      lastExit,
      logs: logs.slice(-40),
      sessions: sessions(),
      daemonError: daemonError ? daemonError.message : undefined,
      pendingApprovals: approvals,
      workspaceArtifacts: workspace.artifacts,
      gitOperations,
      screenShare,
      providers,
      runner,
      provider: session?.agentSession?.provider,
    };
  }

  async function preflight(settings) {
    const validation = validateSettings(settings);
    const errors = { ...validation.errors };
    let secrets = {};
    try { secrets = parseEnv(fs.readFileSync(path.join(root, '.env'), 'utf8')); } catch { errors.credentials = 'Create .env with your OpenAI and Tavily keys.'; }
    if (!secrets.OPENAI_API_KEY || /replace_with|your_/i.test(secrets.OPENAI_API_KEY)) errors.credentials = 'Configure OPENAI_API_KEY in .env.';
    if (settings.tools?.webSearch && (!secrets.TAVILY_API_KEY || /replace_with|your_/i.test(secrets.TAVILY_API_KEY))) errors.webSearch = 'Configure TAVILY_API_KEY in .env or disable web search.';
    const docker = await withTimeout(
      runCommand('docker', ['info', '--format', '{{.ServerVersion}}'], {
        cwd: root,
        timeoutMs: dockerTimeoutMs,
      }),
      dockerTimeoutMs,
      { code: -1, stdout: '', stderr: 'timed out' },
    );
    if (docker.code !== 0) errors.docker = 'Docker is unavailable. Start Docker Desktop and try again.';
    const codex = await runCommand('/bin/bash', ['-lc', 'if command -v codex >/dev/null 2>&1; then codex login status; elif [ -x /Applications/ChatGPT.app/Contents/Resources/codex ]; then /Applications/ChatGPT.app/Contents/Resources/codex login status; else exit 127; fi'], { cwd: root });
    if (settings.tools?.codex && codex.code !== 0) errors.codex = 'Codex is unavailable or signed out. Run codex login.';
    if (settings.tools?.cursor) {
      const cursor = await runCommand('/bin/bash', ['-lc', 'command -v "${CURSOR_BIN:-cursor-agent}" >/dev/null 2>&1'], { cwd: root });
      if (cursor.code !== 0) errors.cursor = 'Cursor CLI not found. Install cursor-agent and complete its official login.';
    }
    if (settings.tools?.claudeCode) {
      const claude = await runCommand('/bin/bash', ['-lc', 'command -v "${CLAUDE_BIN:-claude}" >/dev/null 2>&1'], { cwd: root });
      if (claude.code !== 0) errors.claudeCode = 'Claude Code CLI not found. Install claude and run claude login.';
    }
    return { ready: Object.keys(errors).length === 0, errors };
  }

  function authorized(request) {
    const origin = request.headers.origin;
    const allowedOrigins = [`http://127.0.0.1:${PORT}`, `http://localhost:${PORT}`];
    return allowedOrigins.includes(origin) && request.headers['x-colleague-token'] === token;
  }

  async function meetingActive() {
    const { session, daemonError } = await loadMeeting();
    if (daemonError && readActiveMeetingId(root)) return true;
    return meetingIsActive(session);
  }

  function spawnPlatformAccount(mode) {
    const spawnFn = spawnAccount || ((env) => spawn('/bin/bash', ['start-meeting-agent.sh'], {
      cwd: root, env,
    }));
    return spawnFn({ ...process.env, COLLEAGUE_AUTH_MODE: mode });
  }

  async function api(request, response, pathname) {
    if (request.method === 'GET' && pathname === '/api/bootstrap') {
      return json(response, 200, {
        token,
        settings: publicSettings(currentEnv()),
        context: publicContext(readContext(contextIndex)),
        models: MODELS,
        status: await status(),
      });
    }
    if (request.method === 'GET' && pathname === '/api/platforms/teams/status') {
      return json(response, 200, { connected: fs.existsSync(path.join(profileRoot, 'teams-connected')) });
    }
    if (request.method === 'GET' && pathname === '/api/platforms/google/status') {
      return json(response, 200, { connected: fs.existsSync(path.join(profileRoot, 'google-connected')) });
    }
    if (request.method === 'GET' && pathname === '/api/runner') {
      try {
        return json(response, 200, publicRunner(await daemonClient.runnerStatus()));
      } catch (error) {
        return json(response, error.status || 503, { error: error.message, code: error.code });
      }
    }
    if (request.method === 'GET' && pathname === '/api/status') return json(response, 200, await status());
    if (request.method === 'GET' && pathname.startsWith('/api/sessions/')) {
      const rest = pathname.slice('/api/sessions/'.length);
      if (rest.endsWith('/retry')) {
        return json(response, 405, { error: 'Use POST to retry finalization.' });
      }
      const id = rest;
      const directory = sessionDirectory(id);
      if (!directory) return json(response, 400, { error: 'Invalid session.' });
      const transcriptPath = path.join(directory, 'transcript.txt');
      const archive = archiveStatus(directory);
      if (!fs.existsSync(transcriptPath) && !archive.hasHandoff) {
        return json(response, 404, { error: 'Transcript is not available.' });
      }
      let transcript = '';
      try {
        if (fs.existsSync(transcriptPath) && !fs.lstatSync(transcriptPath).isSymbolicLink()) {
          transcript = fs.readFileSync(transcriptPath, 'utf8').slice(-200_000);
        }
      } catch {
        transcript = '';
      }
      return json(response, 200, {
        id,
        transcript,
        handoff: archive.handoff,
        handoffStatus: archive.status,
        partial: archive.partial,
        endReason: archive.endReason,
        handoffId: archive.handoffId,
      });
    }
    // Call reads follow the transcript routes above: same-origin GETs carry no Origin header.
    const callMatch = pathname.match(/^\/api\/calls(?:\/(call-[0-9a-f]{16})(?:\/(end|transfer|events))?)?$/);
    if (callMatch && request.method === 'GET') {
      const [, callId, action] = callMatch;
      try {
        if (!callId) return json(response, 200, await daemonClient.listCalls(30));
        if (!action) return json(response, 200, await daemonClient.getCall(callId));
        if (action === 'events') {
          const after = new URL(request.url, `http://127.0.0.1:${PORT}`).searchParams.get('after') || '';
          return json(response, 200, await daemonClient.callEvents(callId, after));
        }
      } catch (error) {
        return json(response, error.status || 503, { error: error.message, code: error.code });
      }
      return json(response, 405, { error: 'Method not allowed.' });
    }
    if (!authorized(request)) return json(response, 403, { error: 'Refresh the control panel and try again.' });
    if (callMatch && request.method === 'POST' && ['end', 'transfer'].includes(callMatch[2])) {
      try {
        const callId = callMatch[1];
        const payload = callMatch[2] === 'end'
          ? await daemonClient.endCall(callId)
          : await daemonClient.transferCall(callId);
        return json(response, 200, payload);
      } catch (error) {
        return json(response, error.status || 503, { error: error.message, code: error.code });
      }
    }
    const artifactMatch =pathname.match(/^\/api\/meetings\/([^/]+)\/artifacts(?:\/([^/]+)(?:\/(content))?)?$/);
    if (request.method === 'GET' && artifactMatch) {
      const meetingId = decodeURIComponent(artifactMatch[1]);
      const artifactId = artifactMatch[2] ? decodeURIComponent(artifactMatch[2]) : '';
      const wantContent = artifactMatch[3] === 'content';
      try {
        if (!artifactId) {
          return json(response, 200, await daemonClient.listArtifacts(meetingId));
        }
        if (wantContent) {
          const payload = await daemonClient.getArtifactContent(meetingId, artifactId);
          const media = payload.mediaType || 'application/octet-stream';
          response.writeHead(200, {
            'Content-Type': media,
            'Content-Disposition': `attachment; filename="${artifactId}"`,
            'X-Content-Type-Options': 'nosniff',
            'Cache-Control': 'no-store',
          });
          response.end(payload.body);
          return;
        }
        return json(response, 200, await daemonClient.getArtifact(meetingId, artifactId));
      } catch (error) {
        return json(response, error.status || 503, { error: error.message, code: error.code });
      }
    }
    const shareGet = pathname.match(/^\/api\/meetings\/([^/]+)\/screen-share(?:\/(observations))?$/);
    if (request.method === 'GET' && shareGet) {
      const meetingId = decodeURIComponent(shareGet[1]);
      try {
        if (shareGet[2] === 'observations') {
          return json(response, 200, await daemonClient.listScreenShareObservations(meetingId));
        }
        return json(response, 200, await daemonClient.getScreenShare(meetingId));
      } catch (error) {
        return json(response, error.status || 503, { error: error.message, code: error.code });
      }
    }
    if (request.method === 'POST' && pathname === '/api/context/add') {
      const body = await readBody(request, 20 * 1024 * 1024);
      const { session } = await loadMeeting();
      if (session && session.state === 'ended') {
        return json(response, 409, { error: 'cannot update context after the meeting has ended', code: 'conflict' });
      }
      const result = await addContext(contextIndex, body);
      if (meetingIsActive(session)) {
        const settings = publicSettings(currentEnv());
        const sources = readContext(contextIndex).sources;
        await daemonClient.updateContext(session.id, contextHandoffFromSources(sources, {
          meetingInstructions: settings.meetingInstructions,
        }));
      }
      return json(response, 200, result);
    }
    if (request.method === 'POST' && pathname === '/api/context/clear') {
      await readBody(request);
      const { session } = await loadMeeting();
      if (session && session.state === 'ended') {
        return json(response, 409, { error: 'cannot update context after the meeting has ended', code: 'conflict' });
      }
      const result = clearContext(contextIndex);
      if (meetingIsActive(session)) {
        const settings = publicSettings(currentEnv());
        await daemonClient.updateContext(session.id, contextHandoffFromSources([], {
          meetingInstructions: settings.meetingInstructions,
        }));
      }
      return json(response, 200, result);
    }
    const body = await readBody(request);
    if (request.method === 'POST' && pathname === '/api/platforms/teams/connect') {
      if (launcherActive(accountLauncher) || await meetingActive()) {
        return json(response, 409, { error: 'Stop the current meeting or account connection first.' });
      }
      fs.mkdirSync(profileRoot, { recursive: true, mode: 0o700 });
      accountLauncher = spawnPlatformAccount('teams');
      accountLauncher.stdout?.on('data', chunk => addLog('account', chunk));
      accountLauncher.stderr?.on('data', chunk => addLog('account', chunk));
      accountLauncher.on('error', error => { lastExit = -1; addLog('account', error.message); });
      accountLauncher.on('exit', (code, signal) => { lastExit = code ?? signal; });
      return json(response, 202, { connecting: true });
    }
    if (request.method === 'POST' && pathname === '/api/platforms/teams/disconnect') {
      if (launcherActive(accountLauncher) || await meetingActive()) {
        return json(response, 409, { error: 'Stop the meeting or account browser before disconnecting.' });
      }
      fs.rmSync(path.join(profileRoot, 'teams'), { recursive: true, force: true });
      fs.rmSync(path.join(profileRoot, 'teams-connected'), { force: true });
      return json(response, 200, { connected: false });
    }
    if (request.method === 'POST' && pathname === '/api/platforms/google/connect') {
      if (launcherActive(accountLauncher) || await meetingActive()) {
        return json(response, 409, { error: 'Stop the current meeting or account connection first.' });
      }
      fs.mkdirSync(profileRoot, { recursive: true, mode: 0o700 });
      accountLauncher = spawnPlatformAccount('google');
      accountLauncher.stdout?.on('data', chunk => addLog('account', chunk));
      accountLauncher.stderr?.on('data', chunk => addLog('account', chunk));
      accountLauncher.on('error', error => { lastExit = -1; addLog('account', error.message); });
      accountLauncher.on('exit', (code, signal) => { lastExit = code ?? signal; });
      return json(response, 202, { connecting: true });
    }
    if (request.method === 'POST' && pathname === '/api/platforms/google/disconnect') {
      if (launcherActive(accountLauncher) || await meetingActive()) {
        return json(response, 409, { error: 'Stop the meeting or account browser before disconnecting.' });
      }
      fs.rmSync(path.join(profileRoot, 'google'), { recursive: true, force: true });
      fs.rmSync(path.join(profileRoot, 'google-connected'), { force: true });
      return json(response, 200, { connected: false });
    }
    if (request.method === 'POST' && pathname === '/api/runner/pair') {
      try {
        const started = await daemonClient.pairRunner(body || {});
        return json(response, 201, {
          pairingId: started.pairingId,
          pairingCode: started.pairingCode,
          expiresAt: started.expiresAt,
        });
      } catch (error) {
        return json(response, error.status || 503, { error: error.message, code: error.code });
      }
    }
    if (request.method === 'POST' && pathname === '/api/runner/pair/complete') {
      try {
        const completed = await daemonClient.completeRunnerPair({
          pairingId: body.pairingId,
          pairingCode: body.pairingCode,
        });
        return json(response, 201, publicRunner(completed));
      } catch (error) {
        return json(response, error.status || 503, { error: error.message, code: error.code });
      }
    }
    if (request.method === 'POST' && pathname === '/api/runner/unpair') {
      try {
        return json(response, 200, publicRunner(await daemonClient.unpairRunner()));
      } catch (error) {
        return json(response, error.status || 503, { error: error.message, code: error.code });
      }
    }
    if (request.method === 'POST' && pathname === '/api/preflight') return json(response, 200, await preflight(body));
    if (request.method === 'POST' && pathname === '/api/start') {
      if (startInFlight || launcherActive(accountLauncher)) {
        return json(response, 409, { error: 'An agent is already connected or starting.' });
      }
      startInFlight = true;
      try {
        if (await meetingActive()) {
          return json(response, 409, { error: 'An agent is already connected or starting.' });
        }
        const check = await preflight(body);
        if (!check.ready) return json(response, 422, check);
        const workspace = String(body.workspace || '').trim() || ensureDefaultWorkspace(root);
        const payload = buildMeetingCreatePayload(body, {
          sources: readContext(contextIndex).sources,
          workspace,
          root,
        });
        logs.length = 0;
        lastExit = null;
        addLog('system', 'Starting meeting through the local runtime daemon.');
        const session = await daemonClient.createMeeting(payload);
        writeActiveMeetingId(root, session.id);
        return json(response, 202, { started: true, meetingId: session.id });
      } catch (error) {
        addLog('system', error.message);
        return json(response, error.status || 503, {
          error: error.message,
          code: error.code,
        });
      } finally {
        startInFlight = false;
      }
    }
    if (request.method === 'POST' && pathname.startsWith('/api/sessions/') && pathname.endsWith('/retry')) {
      const id = pathname.slice('/api/sessions/'.length, pathname.length - '/retry'.length).replace(/\/$/, '');
      const directory = sessionDirectory(id);
      if (!directory) return json(response, 400, { error: 'Invalid session.' });
      try {
        const handoff = await daemonClient.retryHandoff(id);
        return json(response, 200, {
          id,
          handoff,
          handoffStatus: 'ready',
        });
      } catch (error) {
        const archive = archiveStatus(directory);
        return json(response, error.status || 503, {
          error: error.message,
          code: error.code,
          id,
          handoff: archive.handoff,
          handoffStatus: archive.status === 'none' ? 'failed' : archive.status,
          partial: archive.partial,
          endReason: archive.endReason,
        });
      }
    }
    if (request.method === 'POST' && pathname === '/api/stop') {
      const meetingId = readActiveMeetingId(root);
      if (!meetingId) return json(response, 200, { stopped: true });
      try {
        await daemonClient.cancelMeeting(meetingId);
        clearActiveMeetingId(root);
        addLog('system', 'Meeting cancelled through the runtime daemon.');
        return json(response, 200, { stopped: true, meetingId });
      } catch (error) {
        if (error.code === 'not_found' || error.status === 404) {
          clearActiveMeetingId(root);
          return json(response, 200, { stopped: true });
        }
        addLog('system', error.message);
        return json(response, error.status || 503, { error: error.message, code: error.code });
      }
    }
    const approvalMatch = pathname.match(/^\/api\/meetings\/([^/]+)\/approvals\/([^/]+)\/decision$/);
    if (request.method === 'POST' && approvalMatch) {
      const meetingId = decodeURIComponent(approvalMatch[1]);
      const approvalId = decodeURIComponent(approvalMatch[2]);
      try {
        const approval = await daemonClient.decideApproval(meetingId, approvalId, {
          decision: body.decision,
        });
        addLog('system', `Approval ${approvalId} ${approval.status}.`);
        return json(response, 200, { approval });
      } catch (error) {
        return json(response, error.status || 503, { error: error.message, code: error.code });
      }
    }
    const shareAction = pathname.match(/^\/api\/meetings\/([^/]+)\/screen-share\/(pause|resume)$/);
    if (request.method === 'POST' && shareAction) {
      const meetingId = decodeURIComponent(shareAction[1]);
      const action = shareAction[2];
      try {
        const payload = action === 'pause'
          ? await daemonClient.pauseScreenShare(meetingId)
          : await daemonClient.resumeScreenShare(meetingId);
        addLog('system', `Screen-share ${action}.`);
        return json(response, 200, payload);
      } catch (error) {
        return json(response, error.status || 503, { error: error.message, code: error.code });
      }
    }
    return json(response, 404, { error: 'Not found.' });
  }

  return http.createServer(async (request, response) => {
    try {
      // DNS rebinding: a page served from another name that resolves to 127.0.0.1
      // must not read this console, so only loopback host names are served.
      if (!loopbackHost(request.headers.host)) {
        response.writeHead(421, headers('text/plain; charset=utf-8'));
        return response.end('Misdirected request');
      }
      const pathname = new URL(request.url, `http://127.0.0.1:${PORT}`).pathname;
      if (pathname.startsWith('/api/')) return await api(request, response, pathname);
      const assets = {
        '/': ['index.html', 'text/html; charset=utf-8'],
        '/app.js': ['app.js', 'text/javascript; charset=utf-8'],
        '/client-validation.mjs': ['client-validation.mjs', 'text/javascript; charset=utf-8'],
        '/visual-preview.mjs': ['visual-preview.mjs', 'text/javascript; charset=utf-8'],
        '/styles.css': ['styles.css', 'text/css; charset=utf-8'],
        '/favicon.svg': ['favicon.svg', 'image/svg+xml'],
        '/calls': ['calls.html', 'text/html; charset=utf-8'],
        '/calls.js': ['calls.js', 'text/javascript; charset=utf-8'],
        '/calls.css': ['calls.css', 'text/css; charset=utf-8'],
      };
      const asset = assets[pathname];
      if (!asset) { response.writeHead(404, headers('text/plain; charset=utf-8')); return response.end('Not found'); }
      response.writeHead(200, headers(asset[1]));
      response.end(fs.readFileSync(path.join(HERE, asset[0])));
    } catch (error) {
      json(response, error.status || 500, { error: error instanceof SyntaxError ? 'Invalid JSON request.' : error.message, code: error.code });
    }
  });
}

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  createServer().listen(PORT, '127.0.0.1', () => {
    console.log(`Colleague AI control panel: http://127.0.0.1:${PORT}`);
  });
}
