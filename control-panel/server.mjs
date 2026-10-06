import crypto from 'node:crypto';
import { spawn, spawnSync } from 'node:child_process';
import fs from 'node:fs';
import http from 'node:http';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import { ownerName, parseEnv, publicSettings, validateSettings } from './config.mjs';
import { addContext, clearContext, publicContext, readContext } from './context-store.mjs';
import { createDaemonClient } from './daemon-client.mjs';
import {
  FIELDS, accountFields, readEnv, removeSetting, sanitizeSubmission, setupStatus, writeEnv,
} from '../packages/cli/src/setup.mjs';
import { LOCAL_MCP_PATH, createLocalMcpHandler } from '../packages/mcp/src/local-http.mjs';
import { redact } from '../packages/sdk-typescript/src/index.mjs';
import { launcherActive } from './lifecycle.mjs';
import {
  buildMeetingBrief,
  clearActiveMeetingId,
  contextHandoffFromSources,
  meetingIsActive,
  phaseFromDaemon,
  readActiveMeeting,
  readActiveMeetingId,
  writeActiveMeetingId,
} from './meeting-contract.mjs';

const HERE = path.dirname(fileURLToPath(import.meta.url));
// The code (this repository, /app in the container): scripts and static files.
const CODE_ROOT = path.dirname(HERE);
const PORT = Number(process.env.COLLEAGUE_CONTROL_PORT || 8095);

/**
 * Where the console keeps and reads data. COLLEAGUE_ROOT is the data root (.env,
 * .env.meeting, .colleague/); COLLEAGUE_MEETING_DATA holds run/, recordings/,
 * profiles/, and context/. Unset, both are the repository as before: the repository
 * root and its meeting-runtime directory. An explicit root (tests) keeps its own
 * meeting-runtime directory unless COLLEAGUE_MEETING_DATA is set.
 */
export function consolePaths({ env = process.env, root, codeRoot = CODE_ROOT } = {}) {
  const dataRoot = path.resolve(root || env.COLLEAGUE_ROOT || codeRoot);
  const runtimeRoot = env.COLLEAGUE_MEETING_DATA
    ? path.resolve(env.COLLEAGUE_MEETING_DATA)
    : path.join(root ? dataRoot : codeRoot, 'meeting-runtime');
  return {
    codeRoot,
    root: dataRoot,
    runtimeRoot,
    contextIndex: path.join(runtimeRoot, 'context', 'index.json'),
    meetingEnv: path.join(dataRoot, '.env.meeting'),
    recordings: path.join(runtimeRoot, 'recordings'),
    profileRoot: path.join(runtimeRoot, 'profiles'),
  };
}
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

function run(command, args, options = {}) {
  const { timeoutMs, cwd = CODE_ROOT, ...spawnOptions } = options;
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

export function createServer(options = {}) {
  const defaults = consolePaths({ root: options.root, codeRoot: options.codeRoot });
  const {
    codeRoot = defaults.codeRoot,
    root = defaults.root,
    runtimeRoot = defaults.runtimeRoot,
    contextIndex = path.join(runtimeRoot, 'context', 'index.json'),
    meetingEnv = path.join(root, '.env.meeting'),
    recordings = path.join(runtimeRoot, 'recordings'),
    profileRoot = path.join(runtimeRoot, 'profiles'),
    daemon = null,
    spawnAccount = null,
    runCommand = run,
    dockerTimeoutMs = DOCKER_INFO_TIMEOUT_MS,
    mcpHandler = null,
    mcpLog = (line) => process.stderr.write(`${new Date().toISOString()} mcp ${redact(line)}\n`),
    // The Account page: the same checks as `smitline setup status`, and the environment
    // that overrides .env (a key set there cannot be changed from the console).
    checkSetup = setupStatus,
    environment = process.env,
    // How long a manual start waits for its call to create the meeting.
    meetingStartWaitMs = 15_000,
  } = options;
  const token = crypto.randomBytes(24).toString('base64url');
  const logs = [];
  let lastExit = null;
  let accountLauncher = null;
  let startInFlight = false;
  const daemonClient = daemon || createDaemonClient({ root, codeRoot });
  // Local agents' MCP endpoint; it checks its own bearer token, Host, and Origin.
  const localMcp = mcpHandler || createLocalMcpHandler({ root, codeRoot, log: mcpLog });

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

  function secretsEnv() {
    return parseEnv(fs.readFileSync(path.join(root, '.env'), 'utf8'));
  }

  // COLLEAGUE_OWNER_NAME lives in .env (smitline setup); .env.meeting may also set it.
  function meetingOwner() {
    let secrets = {};
    try { secrets = secretsEnv(); } catch { /* no .env yet */ }
    return ownerName(secrets) || ownerName(currentEnv());
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

  // The guidance typed for this meeting, else the saved one.
  function meetingGuidance() {
    const active = readActiveMeeting(root);
    return typeof active?.guidance === 'string' ? active.guidance : publicSettings(currentEnv()).meetingInstructions;
  }

  // The saved reference sources are too large for a brief; the meeting gets them once it exists.
  async function adoptMeeting(meetingId, { callId, objective, guidance = '' }) {
    writeActiveMeetingId(root, meetingId, { callId, objective, guidance });
    const sources = readContext(contextIndex).sources;
    if (!sources.length) return;
    try {
      await daemonClient.updateContext(meetingId, contextHandoffFromSources(sources, { meetingInstructions: guidance, objective }));
    } catch (error) {
      addLog('system', `Reference context was not sent: ${error.message}`);
    }
  }

  // A started call creates its meeting a moment later.
  async function meetingOfCall(callId) {
    const deadline = Date.now() + meetingStartWaitMs;
    for (;;) {
      const call = await daemonClient.getCall(callId);
      if (call.line?.meetingId) return call.line.meetingId;
      if (['completed', 'failed', 'canceled'].includes(call.status)) {
        throw Object.assign(new Error(call.error || 'The meeting could not start.'), { status: 409, code: 'call_ended' });
      }
      if (Date.now() >= deadline) return null;
      await new Promise((resolve) => setTimeout(resolve, 200));
    }
  }

  // After a slow start or a console restart: find the meeting of the call this console started.
  async function resolveStartedCall() {
    const active = readActiveMeeting(root);
    if (!active?.callId || active.meetingId) return;
    try {
      const call = await daemonClient.getCall(active.callId);
      if (call.line?.meetingId) await adoptMeeting(call.line.meetingId, active);
      else if (['completed', 'failed', 'canceled'].includes(call.status)) clearActiveMeetingId(root);
    } catch (error) {
      if (error.code === 'not_found' || error.status === 404) clearActiveMeetingId(root);
    }
  }

  async function loadMeeting() {
    await resolveStartedCall();
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

  async function status() {
    const health = await bridgeHealth();
    const { meetingId, session, daemonError } = await loadMeeting();
    return {
      phase: phaseFromDaemon({ session, health, daemonError }),
      running: meetingIsActive(session),
      meetingId: meetingId || undefined,
      health,
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
    try { secrets = secretsEnv(); } catch { errors.credentials = 'Create .env with your OpenAI key.'; }
    if (!secrets.OPENAI_API_KEY || /replace_with|your_/i.test(secrets.OPENAI_API_KEY)) errors.credentials = 'Configure OPENAI_API_KEY in .env.';
    const docker = await withTimeout(
      runCommand('docker', ['info', '--format', '{{.ServerVersion}}'], {
        cwd: root,
        timeoutMs: dockerTimeoutMs,
      }),
      dockerTimeoutMs,
      { code: -1, stdout: '', stderr: 'timed out' },
    );
    if (docker.code !== 0) errors.docker = 'Docker is unavailable. Start Docker (Docker Desktop, or Docker Engine inside WSL) and try again.';
    return { ready: Object.keys(errors).length === 0, errors };
  }

  function authorized(request) {
    const origin = request.headers.origin;
    const allowedOrigins = [`http://127.0.0.1:${PORT}`, `http://localhost:${PORT}`];
    return allowedOrigins.includes(origin) && request.headers['x-colleague-token'] === token;
  }

  // Account page --------------------------------------------------------------

  const FIELD_KEYS = new Set(FIELDS.map((field) => field.key));
  const fieldLabel = (key) => FIELDS.find((field) => field.key === key)?.label || key;

  // The checklist as `smitline setup status` prints it, without the questions meant for agents.
  // Its probes run synchronously, so Docker is probed here first without blocking the console;
  // the agent CLIs (`claude mcp get`, which can take seconds) are not probed, and that check,
  // about agents on another computer in the container, is left out.
  async function setupView({ verify = true } = {}) {
    const docker = await withTimeout(
      runCommand('docker', ['info', '--format', '{{.ServerVersion}}'], { cwd: root, timeoutMs: dockerTimeoutMs }),
      dockerTimeoutMs,
      { code: -1 },
    );
    const runner = (binary, args) => (binary === 'docker'
      ? { status: docker.code === 0 ? 0 : 1 }
      : spawnSync(binary, args, { encoding: 'utf8', timeout: 5000 }));
    const report = await checkSetup({ root, codeRoot, verify, runner, find: () => null });
    return {
      fields: accountFields(readEnv(root), environment),
      status: {
        ready: report.ready,
        phoneReady: report.phoneReady,
        meetingsReady: report.meetingsReady,
        checks: report.checks.filter((check) => check.id !== 'agents')
          .map(({ id, label, ok, required, group, detail, fix }) => (
            { id, label, ok, required, group, detail, ...(fix ? { fix } : {}) })),
      },
    };
  }

  // Validation messages name a setting by its key or label; say which field each one is about.
  function fieldErrors(errors) {
    return errors.map((message) => {
      const field = FIELDS.find((item) => message.startsWith(`${item.key} `) || message.startsWith(item.label));
      return { key: field?.key || null, message: field ? message.replace(`${field.key} `, `${field.label} `) : message };
    });
  }

  async function saveSettings(body) {
    const values = body?.values;
    if (!values || typeof values !== 'object' || Array.isArray(values)
      || Object.entries(values).some(([key, value]) => !FIELD_KEYS.has(key) || typeof value !== 'string')) {
      return [422, { error: 'Only the fields on this page can be saved.' }];
    }
    const errors = [];
    const allowed = {};
    for (const [key, value] of Object.entries(values)) {
      if (String(environment[key] || '').trim()) errors.push(`${fieldLabel(key)} is set in Smitline's environment, so change it there`);
      else allowed[key] = value;
    }
    const checked = sanitizeSubmission({ get: (key) => allowed[key] });
    errors.push(...checked.errors);
    // Valid fields are saved even when another needs a fix, as on the setup page.
    const saved = Object.keys(checked.updates).length ? writeEnv(root, checked.updates) : [];
    return [errors.length ? 422 : 200, { saved, errors: fieldErrors(errors), ...(await setupView()) }];
  }

  async function removeSaved(body) {
    const key = body?.key;
    if (typeof key !== 'string' || !FIELD_KEYS.has(key)) return [422, { error: 'Only the fields on this page can be removed.' }];
    if (String(environment[key] || '').trim()) {
      return [409, { error: `${fieldLabel(key)} is set in Smitline's environment, so remove it there.` }];
    }
    removeSetting(root, key);
    return [200, { removed: key, ...(await setupView()) }];
  }

  async function meetingActive() {
    const { session, daemonError } = await loadMeeting();
    if (daemonError && readActiveMeetingId(root)) return true;
    // A started call whose meeting does not exist yet.
    if (readActiveMeeting(root)?.callId && !readActiveMeetingId(root)) return true;
    return meetingIsActive(session);
  }

  function spawnPlatformAccount(mode) {
    // The launcher lives with the code; COLLEAGUE_* in the environment point it at the data.
    const spawnFn = spawnAccount || ((env) => spawn('/bin/bash', [path.join(codeRoot, 'start-meeting-agent.sh')], {
      cwd: codeRoot, env,
    }));
    return spawnFn({ ...process.env, COLLEAGUE_AUTH_MODE: mode });
  }

  async function api(request, response, pathname) {
    if (request.method === 'GET' && pathname === '/api/bootstrap') {
      return json(response, 200, {
        token,
        settings: publicSettings(currentEnv()),
        context: publicContext(readContext(contextIndex)),
        status: await status(),
      });
    }
    if (request.method === 'GET' && pathname === '/api/platforms/teams/status') {
      return json(response, 200, { connected: fs.existsSync(path.join(profileRoot, 'teams-connected')) });
    }
    if (request.method === 'GET' && pathname === '/api/platforms/google/status') {
      return json(response, 200, { connected: fs.existsSync(path.join(profileRoot, 'google-connected')) });
    }
    if (request.method === 'GET' && pathname === '/api/status') return json(response, 200, await status());
    if (request.method === 'GET' && pathname.startsWith('/api/sessions/')) {
      const id = pathname.slice('/api/sessions/'.length);
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
    const callMatch = pathname.match(/^\/api\/calls(?:\/(call-[0-9a-f]{16})(?:\/(end|transfer|events|recording))?)?$/);
    if (callMatch && request.method === 'GET') {
      const [, callId, action] = callMatch;
      try {
        if (!callId) {
          const query = new URL(request.url, `http://127.0.0.1:${PORT}`).searchParams;
          return json(response, 200, await daemonClient.listCalls(50, query.get('tzOffset') || '', query.get('channel') || ''));
        }
        if (!action) return json(response, 200, await daemonClient.getCall(callId));
        if (action === 'events') {
          const after = new URL(request.url, `http://127.0.0.1:${PORT}`).searchParams.get('after') || '';
          return json(response, 200, await daemonClient.callEvents(callId, after));
        }
        if (action === 'recording') {
          const format = new URL(request.url, `http://127.0.0.1:${PORT}`).searchParams.get('format') === 'mp3' ? 'mp3' : 'wav';
          const audio = await daemonClient.downloadRecording(callId, format);
          response.writeHead(200, {
            ...headers(audio.contentType),
            'Content-Disposition': `attachment; filename="${callId}.${format}"`,
            'Content-Length': audio.body.length,
          });
          return response.end(audio.body);
        }
      } catch (error) {
        return json(response, error.status || 503, { error: error.message, code: error.code });
      }
      return json(response, 405, { error: 'Method not allowed.' });
    }
    // Setup status names the owner's numbers, so even reading it takes the session token.
    // A same-origin GET carries no Origin header; a cross-site page cannot send this header.
    if (request.method === 'GET' && pathname === '/api/setup') {
      if (request.headers['x-colleague-token'] !== token) return json(response, 403, { error: 'Refresh the control panel and try again.' });
      const verify = new URL(request.url, `http://127.0.0.1:${PORT}`).searchParams.get('verify') !== '0';
      return json(response, 200, await setupView({ verify }));
    }
    if (!authorized(request)) return json(response, 403, { error: 'Refresh the control panel and try again.' });
    // Writing keys: the session token and an Origin of this console, like every other change.
    if (request.method === 'POST' && (pathname === '/api/setup/save' || pathname === '/api/setup/remove')) {
      const body = await readBody(request);
      const [status, payload] = pathname.endsWith('/save') ? await saveSettings(body) : await removeSaved(body);
      return json(response, status, payload);
    }
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
    if (request.method === 'POST' && pathname === '/api/context/add') {
      const body = await readBody(request, 20 * 1024 * 1024);
      const { session } = await loadMeeting();
      if (session && session.state === 'ended') {
        return json(response, 409, { error: 'cannot update context after the meeting has ended', code: 'conflict' });
      }
      const result = await addContext(contextIndex, body);
      if (meetingIsActive(session)) {
        const sources = readContext(contextIndex).sources;
        await daemonClient.updateContext(session.id, contextHandoffFromSources(sources, {
          meetingInstructions: meetingGuidance(),
          objective: readActiveMeeting(root)?.objective,
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
        await daemonClient.updateContext(session.id, contextHandoffFromSources([], {
          meetingInstructions: meetingGuidance(),
          objective: readActiveMeeting(root)?.objective,
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
        // Through the calls API, so the meeting is listed and judged like one an agent started.
        const objective = String(body.objective).trim();
        const guidance = String(body.meetingInstructions || '').trim();
        logs.length = 0;
        lastExit = null;
        addLog('system', 'Starting the meeting through the calls API.');
        const call = await daemonClient.createCall(buildMeetingBrief(body, { onBehalfOf: meetingOwner() }));
        writeActiveMeetingId(root, null, { callId: call.id, objective, guidance });
        let meetingId;
        try {
          meetingId = await meetingOfCall(call.id);
        } catch (error) {
          clearActiveMeetingId(root);
          throw error;
        }
        if (meetingId) await adoptMeeting(meetingId, { callId: call.id, objective, guidance });
        return json(response, 202, { started: true, callId: call.id, meetingId: meetingId || undefined });
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
      const active = readActiveMeeting(root);
      const meetingId = readActiveMeetingId(root);
      if (!meetingId && !active?.callId) return json(response, 200, { stopped: true });
      try {
        // Ending the call lets it finish with a result; a meeting started otherwise is cancelled.
        if (active?.callId) await daemonClient.endCall(active.callId);
        else await daemonClient.cancelMeeting(meetingId);
        clearActiveMeetingId(root);
        addLog('system', 'Meeting stopped through the runtime daemon.');
        return json(response, 200, { stopped: true, meetingId: meetingId || undefined });
      } catch (error) {
        // Already gone, or the call already ended.
        if (error.code === 'not_found' || error.status === 404 || (active?.callId && error.status === 409)) {
          clearActiveMeetingId(root);
          return json(response, 200, { stopped: true });
        }
        addLog('system', error.message);
        return json(response, error.status || 503, { error: error.message, code: error.code });
      }
    }
    return json(response, 404, { error: 'Not found.' });
  }

  const server = http.createServer(async (request, response) => {
    try {
      // /mcp has its own checks (bearer token, exact Host, no Origin) and never reaches the console's.
      if (new URL(request.url, `http://127.0.0.1:${PORT}`).pathname === LOCAL_MCP_PATH) {
        return await localMcp(request, response);
      }
      // DNS rebinding: a page served from another name that resolves to 127.0.0.1
      // must not read this console, so only loopback host names are served.
      if (!loopbackHost(request.headers.host)) {
        response.writeHead(421, headers('text/plain; charset=utf-8'));
        return response.end('Misdirected request');
      }
      const pathname = new URL(request.url, `http://127.0.0.1:${PORT}`).pathname;
      if (pathname.startsWith('/api/')) return await api(request, response, pathname);
      const assets = {
        '/': ['meetings.html', 'text/html; charset=utf-8'],
        '/meetings/new': ['index.html', 'text/html; charset=utf-8'],
        '/setup': ['setup.html', 'text/html; charset=utf-8'],
        '/setup.js': ['setup.js', 'text/javascript; charset=utf-8'],
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
  server.localMcp = localMcp;
  return server;
}

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  createServer().listen(PORT, '127.0.0.1', () => {
    console.log(`Smitline control panel: http://127.0.0.1:${PORT}`);
  });
}
