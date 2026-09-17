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

function run(command, args, options = {}) {
  return new Promise(resolve => {
    const child = spawn(command, args, { cwd: options.cwd || ROOT, ...options });
    let stdout = '', stderr = '';
    child.stdout?.on('data', chunk => { stdout += chunk; });
    child.stderr?.on('data', chunk => { stderr += chunk; });
    child.on('error', error => resolve({ code: -1, stdout, stderr: error.message }));
    child.on('exit', code => resolve({ code: code ?? -1, stdout, stderr }));
  });
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
      .filter(entry => entry.isDirectory() && /^[\w-]+$/.test(entry.name))
      .map(entry => {
        const directory = path.join(recordings, entry.name);
        const transcript = path.join(directory, 'transcript.txt');
        const stat = fs.statSync(fs.existsSync(transcript) ? transcript : directory);
        return { id: entry.name, updatedAt: stat.mtime.toISOString(), hasTranscript: fs.existsSync(transcript) };
      }).sort((a, b) => b.updatedAt.localeCompare(a.updatedAt)).slice(0, 30);
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

  async function status() {
    const health = await bridgeHealth();
    const { meetingId, session, daemonError } = await loadMeeting();
    const phase = phaseFromDaemon({ session, health, daemonError });
    const running = meetingIsActive(session);
    const lease = running ? await leaseSnapshot(session) : null;
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
    };
  }

  async function preflight(settings) {
    const validation = validateSettings(settings);
    const errors = { ...validation.errors };
    let secrets = {};
    try { secrets = parseEnv(fs.readFileSync(path.join(root, '.env'), 'utf8')); } catch { errors.credentials = 'Create .env with your OpenAI and Tavily keys.'; }
    if (!secrets.OPENAI_API_KEY || /replace_with|your_/i.test(secrets.OPENAI_API_KEY)) errors.credentials = 'Configure OPENAI_API_KEY in .env.';
    if (settings.tools?.webSearch && (!secrets.TAVILY_API_KEY || /replace_with|your_/i.test(secrets.TAVILY_API_KEY))) errors.webSearch = 'Configure TAVILY_API_KEY in .env or disable web search.';
    const docker = await runCommand('docker', ['info', '--format', '{{.ServerVersion}}'], { cwd: root });
    if (docker.code !== 0) errors.docker = 'Docker is unavailable. Start Docker Desktop and try again.';
    const codex = await runCommand('/bin/bash', ['-lc', 'if command -v codex >/dev/null 2>&1; then codex login status; elif [ -x /Applications/ChatGPT.app/Contents/Resources/codex ]; then /Applications/ChatGPT.app/Contents/Resources/codex login status; else exit 127; fi'], { cwd: root });
    if (settings.tools?.codex && codex.code !== 0) errors.codex = 'Codex is unavailable or signed out. Run codex login.';
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

  function spawnTeamsAccount() {
    const spawnFn = spawnAccount || ((env) => spawn('/bin/bash', ['start-meeting-agent.sh'], {
      cwd: root, env,
    }));
    return spawnFn({ ...process.env, COLLEAGUE_AUTH_MODE: 'teams' });
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
    if (request.method === 'GET' && pathname === '/api/status') return json(response, 200, await status());
    if (request.method === 'GET' && pathname.startsWith('/api/sessions/')) {
      const id = pathname.slice('/api/sessions/'.length);
      if (!/^[\w-]+$/.test(id)) return json(response, 400, { error: 'Invalid session.' });
      const transcript = path.join(recordings, id, 'transcript.txt');
      if (!fs.existsSync(transcript)) return json(response, 404, { error: 'Transcript is not available.' });
      return json(response, 200, { id, transcript: fs.readFileSync(transcript, 'utf8').slice(-200_000) });
    }
    if (!authorized(request)) return json(response, 403, { error: 'Refresh the control panel and try again.' });
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
      accountLauncher = spawnTeamsAccount();
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
    if (request.method === 'POST' && pathname === '/api/preflight') return json(response, 200, await preflight(body));
    if (request.method === 'POST' && pathname === '/api/start') {
      if (startInFlight || launcherActive(accountLauncher) || await meetingActive()) {
        return json(response, 409, { error: 'An agent is already connected or starting.' });
      }
      const check = await preflight(body);
      if (!check.ready) return json(response, 422, check);
      startInFlight = true;
      try {
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
    return json(response, 404, { error: 'Not found.' });
  }

  return http.createServer(async (request, response) => {
    try {
      const pathname = new URL(request.url, `http://127.0.0.1:${PORT}`).pathname;
      if (pathname.startsWith('/api/')) return await api(request, response, pathname);
      const assets = {
        '/': ['index.html', 'text/html; charset=utf-8'],
        '/app.js': ['app.js', 'text/javascript; charset=utf-8'],
        '/styles.css': ['styles.css', 'text/css; charset=utf-8'],
        '/favicon.svg': ['favicon.svg', 'image/svg+xml'],
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
