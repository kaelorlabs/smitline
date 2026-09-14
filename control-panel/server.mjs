import crypto from 'node:crypto';
import { spawn } from 'node:child_process';
import fs from 'node:fs';
import http from 'node:http';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { parseEnv, publicSettings, serializeSettings, validateSettings, MODELS } from './config.mjs';
import { addContext, clearContext, publicContext, readContext } from './context-store.mjs';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.dirname(HERE);
const MEETING_ENV = path.join(ROOT, '.env.zoom');
const RECORDINGS = path.join(ROOT, 'zoom-live', 'recordings');
const CONTEXT_INDEX = path.join(ROOT, 'zoom-live', 'context', 'index.json');
const PORT = Number(process.env.COLLEAGUE_CONTROL_PORT || 8095);
const token = crypto.randomBytes(24).toString('base64url');
const logs = [];
let launcher = null;
let lastExit = null;

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

function addLog(source, chunk) {
  for (const line of String(chunk).split(/\r?\n/).filter(Boolean)) {
    logs.push({ at: new Date().toISOString(), source, text: line.slice(0, 500) });
  }
  if (logs.length > 160) logs.splice(0, logs.length - 160);
}

function run(command, args, options = {}) {
  return new Promise(resolve => {
    const child = spawn(command, args, { cwd: ROOT, ...options });
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

function currentEnv() {
  try { return parseEnv(fs.readFileSync(MEETING_ENV, 'utf8')); } catch { return {}; }
}

async function bridgeHealth() {
  try {
    const response = await fetch('http://127.0.0.1:8094/health', { signal: AbortSignal.timeout(700) });
    return response.ok ? await response.json() : null;
  } catch { return null; }
}

function sessions() {
  if (!fs.existsSync(RECORDINGS)) return [];
  return fs.readdirSync(RECORDINGS, { withFileTypes: true })
    .filter(entry => entry.isDirectory() && /^[\w-]+$/.test(entry.name))
    .map(entry => {
      const directory = path.join(RECORDINGS, entry.name);
      const transcript = path.join(directory, 'transcript.txt');
      const stat = fs.statSync(fs.existsSync(transcript) ? transcript : directory);
      return { id: entry.name, updatedAt: stat.mtime.toISOString(), hasTranscript: fs.existsSync(transcript) };
    }).sort((a, b) => b.updatedAt.localeCompare(a.updatedAt)).slice(0, 30);
}

async function status() {
  const health = await bridgeHealth();
  return {
    phase: health?.stage || (launcher && launcher.exitCode === null ? 'starting' : 'stopped'),
    running: Boolean(health), health, lastExit, logs: logs.slice(-40), sessions: sessions(),
  };
}

async function preflight(settings) {
  const validation = validateSettings(settings);
  const errors = { ...validation.errors };
  let secrets = {};
  try { secrets = parseEnv(fs.readFileSync(path.join(ROOT, '.env'), 'utf8')); } catch { errors.credentials = 'Create .env with your OpenAI and Tavily keys.'; }
  if (!secrets.OPENAI_API_KEY || /replace_with|your_/i.test(secrets.OPENAI_API_KEY)) errors.credentials = 'Configure OPENAI_API_KEY in .env.';
  if (settings.tools?.webSearch && (!secrets.TAVILY_API_KEY || /replace_with|your_/i.test(secrets.TAVILY_API_KEY))) errors.webSearch = 'Configure TAVILY_API_KEY in .env or disable web search.';
  const docker = await run('docker', ['info', '--format', '{{.ServerVersion}}']);
  if (docker.code !== 0) errors.docker = 'Docker is unavailable. Start Docker Desktop and try again.';
  const codex = await run('/bin/bash', ['-lc', 'if command -v codex >/dev/null 2>&1; then codex login status; elif [ -x /Applications/ChatGPT.app/Contents/Resources/codex ]; then /Applications/ChatGPT.app/Contents/Resources/codex login status; else exit 127; fi']);
  if (settings.tools?.codex && codex.code !== 0) errors.codex = 'Codex is unavailable or signed out. Run codex login.';
  return { ready: Object.keys(errors).length === 0, errors };
}

function authorized(request) {
  const origin = request.headers.origin;
  const allowedOrigins = [`http://127.0.0.1:${PORT}`, `http://localhost:${PORT}`];
  return allowedOrigins.includes(origin) && request.headers['x-colleague-token'] === token;
}

async function api(request, response, pathname, contextIndex = CONTEXT_INDEX) {
  if (request.method === 'GET' && pathname === '/api/bootstrap') {
    return json(response, 200, {
      token,
      settings: publicSettings(currentEnv()),
      context: publicContext(readContext(contextIndex)),
      models: MODELS,
      status: await status(),
    });
  }
  if (request.method === 'GET' && pathname === '/api/status') return json(response, 200, await status());
  if (request.method === 'GET' && pathname.startsWith('/api/sessions/')) {
    const id = pathname.slice('/api/sessions/'.length);
    if (!/^[\w-]+$/.test(id)) return json(response, 400, { error: 'Invalid session.' });
    const transcript = path.join(RECORDINGS, id, 'transcript.txt');
    if (!fs.existsSync(transcript)) return json(response, 404, { error: 'Transcript is not available.' });
    return json(response, 200, { id, transcript: fs.readFileSync(transcript, 'utf8').slice(-200_000) });
  }
  if (!authorized(request)) return json(response, 403, { error: 'Refresh the control panel and try again.' });
  if (request.method === 'POST' && pathname === '/api/context/add') {
    const body = await readBody(request, 20 * 1024 * 1024);
    return json(response, 200, await addContext(contextIndex, body));
  }
  if (request.method === 'POST' && pathname === '/api/context/clear') {
    await readBody(request);
    return json(response, 200, clearContext(contextIndex));
  }
  const body = await readBody(request);
  if (request.method === 'POST' && pathname === '/api/preflight') return json(response, 200, await preflight(body));
  if (request.method === 'POST' && pathname === '/api/start') {
    if ((launcher && launcher.exitCode === null) || await bridgeHealth()) {
      return json(response, 409, { error: 'An agent is already connected or starting.' });
    }
    const check = await preflight(body);
    if (!check.ready) return json(response, 422, check);
    const previous = currentEnv();
    const temporary = `${MEETING_ENV}.tmp`;
    fs.writeFileSync(temporary, serializeSettings(body, previous), { mode: 0o600 });
    fs.renameSync(temporary, MEETING_ENV);
    fs.chmodSync(MEETING_ENV, 0o600);
    logs.length = 0;
    lastExit = null;
    launcher = spawn('/bin/bash', ['start-zoom-live.sh'], { cwd: ROOT, env: process.env });
    launcher.stdout.on('data', chunk => addLog('agent', chunk));
    launcher.stderr.on('data', chunk => addLog('agent', chunk));
    launcher.on('exit', code => { lastExit = code; addLog('system', `Launcher exited with code ${code}`); });
    launcher.on('error', error => { lastExit = -1; addLog('system', error.message); });
    return json(response, 202, { started: true });
  }
  if (request.method === 'POST' && pathname === '/api/stop') {
    if (launcher && launcher.exitCode === null) launcher.kill('SIGTERM');
    const stopped = await run('docker', ['compose', '-f', 'compose.zoom.yaml', 'stop', 'zoom-live']);
    if (stopped.stdout) addLog('system', stopped.stdout);
    if (stopped.stderr) addLog('system', stopped.stderr);
    return json(response, stopped.code === 0 ? 200 : 500, { stopped: stopped.code === 0 });
  }
  return json(response, 404, { error: 'Not found.' });
}

export function createServer({ contextIndex = CONTEXT_INDEX } = {}) {
  return http.createServer(async (request, response) => {
    try {
      const pathname = new URL(request.url, `http://127.0.0.1:${PORT}`).pathname;
      if (pathname.startsWith('/api/')) return await api(request, response, pathname, contextIndex);
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
      json(response, error.status || 500, { error: error instanceof SyntaxError ? 'Invalid JSON request.' : error.message });
    }
  });
}

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  createServer().listen(PORT, '127.0.0.1', () => {
    console.log(`Colleague AI control panel: http://127.0.0.1:${PORT}`);
  });
}
