// Setup that an agent can drive: status as JSON, a local page for keys, and
// registration with the AI agents installed on this machine. Secrets never
// pass through the agent: they are typed into a loopback page and written to
// the ignored .env file with 0600 permissions.
import { spawnSync } from 'node:child_process';
import crypto from 'node:crypto';
import fs from 'node:fs';
import http from 'node:http';
import net from 'node:net';
import os from 'node:os';
import path from 'node:path';
import { isManaged } from '../../sdk-typescript/src/index.mjs';

export const SECRET_KEYS = Object.freeze([
  'OPENAI_API_KEY', 'TWILIO_ACCOUNT_SID', 'TWILIO_AUTH_TOKEN', 'SIGNALWIRE_API_TOKEN', 'SIGNALWIRE_SIGNING_KEY',
  'SMITLINE_CONNECTOR_PASSPHRASE', 'SMITLINE_SIP_PASSWORD', 'OPENAI_WEBHOOK_SECRET',
]);
export const SETTING_KEYS = Object.freeze([
  'SMITLINE_OWNER_NAME', 'SMITLINE_OWNER_PHONE', 'SMITLINE_VOICE', 'SMITLINE_CALLER_ID',
  'TWILIO_FROM_NUMBER', 'SMITLINE_ACCEPT_INBOUND', 'SMITLINE_ALLOWED_CALLING_CODES',
  'SMITLINE_PUBLIC_URL', 'SMITLINE_NOTIFY_WEBHOOK', 'SMITLINE_RECORD_CALLS', 'SMITLINE_STREAM_REALTIME', 'SMITLINE_AUDIO_TRACE',
  'SMITLINE_MEETING_INTRO', 'SMITLINE_PHONE_WEB_SEARCH', 'SMITLINE_MEETING_WEB_SEARCH',
  'SMITLINE_PHONE_BACKEND_MODEL', 'SMITLINE_MEETING_BACKEND_MODEL', 'SMITLINE_CONNECTOR_URL',
  'SMITLINE_EXTRA_VOICES', 'SMITLINE_MAX_INBOUND', 'SMITLINE_PHONE_PROVIDER',
  'SIGNALWIRE_SPACE', 'SIGNALWIRE_PROJECT_ID', 'SIGNALWIRE_FROM_NUMBER',
  'SMITLINE_PHONE_AUDIO', 'SMITLINE_SIP_TRUNK_URL', 'SMITLINE_SIP_USERNAME', 'OPENAI_PROJECT_ID',
  'SMITLINE_CALLING_HOURS', 'SMITLINE_MAX_CALLS_PER_NUMBER', 'SMITLINE_MAX_CALLS_PER_HOUR',
  'SMITLINE_ALLOW_PREMIUM_NUMBERS', 'SMITLINE_MEETING_CAMERA_STYLE',
]);
export const PHONE_AUDIO_MODES = Object.freeze(['relay', 'sip', 'sip-webhook']);
export const CONNECTOR_PASSPHRASE_MIN = 12;
export const GPT_LIVE_VOICES = Object.freeze([
  'marin', 'vesper', 'quartz', 'ripple', 'willow', 'stone', 'gleam', 'meridian', 'bossa',
  'tempo', 'beacon', 'delta', 'cinder',
]);
const E164 = /^\+[1-9][0-9]{7,14}$/;
const VOICE_NAME = /^[a-z][a-z0-9_-]{1,31}$/;
const MCP_NAME = 'smitline';
// Skills a local agent can use without opening this repository.
const INSTALLED_SKILLS = ['call-with-smitline'];

/** 'twilio' or 'signalwire', chosen the same way as the daemon (call_hooks.phone_provider). */
export function phoneProvider(env) {
  const chosen = String(env.SMITLINE_PHONE_PROVIDER || '').trim().toLowerCase();
  if (['twilio', 'signalwire'].includes(chosen)) return chosen;
  return present(env.SIGNALWIRE_PROJECT_ID) && !present(env.TWILIO_ACCOUNT_SID) ? 'signalwire' : 'twilio';
}

/** The Space host from 'example', 'example.signalwire.com', or its https URL; '' if unusable. */
export function signalwireSpace(value) {
  const text = String(value || '').trim().toLowerCase().replace(/^https?:\/\//, '').split('/')[0];
  if (!/^[a-z0-9][a-z0-9.-]*$/.test(text)) return '';
  return text.includes('.') ? text : `${text}.signalwire.com`;
}

/** GPT-Live voices plus any listed in SMITLINE_EXTRA_VOICES (new voices OpenAI adds). */
export function availableVoices(env = process.env) {
  const extra = String(env.SMITLINE_EXTRA_VOICES || '').split(',').map((name) => name.trim())
    .filter((name) => VOICE_NAME.test(name));
  return [...new Set([...GPT_LIVE_VOICES, ...extra])];
}

// .env handling ------------------------------------------------------------

export function parseEnv(text) {
  const values = {};
  for (const raw of String(text || '').split(/\r?\n/)) {
    const line = raw.trim();
    if (!line || line.startsWith('#') || !line.includes('=')) continue;
    let [key, ...rest] = line.split('=');
    key = key.trim().replace(/^export\s+/, '');
    let value = rest.join('=').trim();
    if (value.length >= 2 && value[0] === value.at(-1) && `"'`.includes(value[0])) value = value.slice(1, -1);
    values[key] = value;
  }
  return values;
}

export function readEnv(root) {
  try {
    return parseEnv(fs.readFileSync(path.join(root, '.env'), 'utf8'));
  } catch {
    return {};
  }
}

function present(value) {
  return typeof value === 'string' && value.trim() !== '' && !value.startsWith('replace_with');
}

/**
 * Merge values into .env, replacing existing assignments and keeping everything else.
 * A null value removes every assignment of that key.
 */
export function writeEnv(root, updates) {
  const file = path.join(root, '.env');
  let lines = [];
  try {
    lines = fs.readFileSync(file, 'utf8').split(/\r?\n/);
  } catch {
    lines = [];
  }
  const remaining = new Map(Object.entries(updates));
  const written = new Set();
  // Readers take the last assignment, so replace the first and drop any repeats.
  lines = lines.flatMap((line) => {
    const match = line.match(/^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=/);
    if (!match || !Object.hasOwn(updates, match[1])) return [line];
    remaining.delete(match[1]);
    if (updates[match[1]] === null || written.has(match[1])) return [];
    written.add(match[1]);
    return [`${match[1]}=${updates[match[1]]}`];
  });
  while (lines.length && lines.at(-1) === '') lines.pop();
  for (const [key, value] of remaining) if (value !== null) lines.push(`${key}=${value}`);
  const tmp = `${file}.${crypto.randomBytes(4).toString('hex')}.tmp`;
  fs.writeFileSync(tmp, `${lines.join('\n')}\n`, { mode: 0o600 });
  fs.renameSync(tmp, file);
  fs.chmodSync(file, 0o600);
  return Object.keys(updates);
}

/** The connector's public address: an https origin with no path (http only on loopback, for tests). */
export function connectorOrigin(value, { allowLoopback = false } = {}) {
  let url;
  try {
    url = new URL(String(value).trim());
  } catch {
    throw new Error('SMITLINE_CONNECTOR_URL must be an https URL such as https://smitline.example.com');
  }
  const loopback = allowLoopback && url.protocol === 'http:' && ['127.0.0.1', 'localhost'].includes(url.hostname);
  if (url.protocol !== 'https:' && !loopback) {
    throw new Error(`SMITLINE_CONNECTOR_URL must be an https URL${allowLoopback ? ' (plain http is allowed only for 127.0.0.1 and localhost)' : ''}`);
  }
  if (url.pathname !== '/' || url.search || url.hash || url.username || url.password) {
    throw new Error('SMITLINE_CONNECTOR_URL must be only the origin, such as https://smitline.example.com, with no path');
  }
  return url.origin;
}

export function validateSetting(key, value, { env = process.env } = {}) {
  if (!SETTING_KEYS.includes(key)) {
    if (SECRET_KEYS.includes(key)) throw new Error(`${key} is a secret: enter it on the page from "smitline setup secrets"`);
    throw new Error(`unknown setting ${key}; allowed: ${SETTING_KEYS.join(', ')}`);
  }
  const text = String(value ?? '').trim();
  if (/[\r\n]/.test(text)) throw new Error(`${key} must be one line`);
  if (['SMITLINE_OWNER_PHONE', 'SMITLINE_CALLER_ID', 'TWILIO_FROM_NUMBER', 'SIGNALWIRE_FROM_NUMBER'].includes(key)) {
    const compact = text.replace(/[\s().-]/g, '');
    if (!E164.test(compact)) throw new Error(`${key} must be an E.164 number such as +14155550142`);
    return compact;
  }
  if (key === 'SMITLINE_VOICE' && !availableVoices(env).includes(text)) {
    throw new Error(`SMITLINE_VOICE must be one of: ${availableVoices(env).join(', ')}`);
  }
  if (key === 'SMITLINE_EXTRA_VOICES') {
    const names = text.split(',').map((name) => name.trim()).filter(Boolean);
    if (names.some((name) => !VOICE_NAME.test(name))) throw new Error('SMITLINE_EXTRA_VOICES must be voice names separated by commas');
    return names.join(',');
  }
  if (key === 'SIGNALWIRE_SPACE') {
    const space = signalwireSpace(text);
    if (!space) throw new Error('SIGNALWIRE_SPACE must be your Space URL, such as yourname.signalwire.com');
    return space;
  }
  if (key === 'SIGNALWIRE_PROJECT_ID' && !/^[A-Za-z0-9-]{8,64}$/.test(text)) {
    throw new Error('SIGNALWIRE_PROJECT_ID must be the Project ID from the API Credentials page, such as 1a2b3c4d-...');
  }
  if (key === 'SMITLINE_PHONE_AUDIO' && !PHONE_AUDIO_MODES.includes(text)) {
    throw new Error(`SMITLINE_PHONE_AUDIO must be one of: ${PHONE_AUDIO_MODES.join(', ')}`);
  }
  if (key === 'SMITLINE_SIP_TRUNK_URL' && !/^sips:[a-z0-9.-]+(:\d{1,5})?$/i.test(text)) {
    throw new Error('SMITLINE_SIP_TRUNK_URL must be a sips: address such as sips:sip.example.com:5061');
  }
  if (key === 'OPENAI_PROJECT_ID' && !/^proj_[A-Za-z0-9]+$/.test(text)) {
    throw new Error('OPENAI_PROJECT_ID must be your OpenAI project ID, which starts with proj_');
  }
  if (key === 'SMITLINE_PHONE_PROVIDER' && !['twilio', 'signalwire'].includes(text)) {
    throw new Error('SMITLINE_PHONE_PROVIDER must be twilio or signalwire');
  }
  if (key === 'SMITLINE_CALLING_HOURS' && text !== 'off') {
    const match = /^(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})$/.exec(text);
    const [startH, startM, endH, endM] = match ? match.slice(1).map(Number) : [];
    if (!match || startH > 23 || endH > 23 || startM > 59 || endM > 59 || startH * 60 + startM >= endH * 60 + endM) {
      throw new Error('SMITLINE_CALLING_HOURS must look like 08:00-21:00 (the recipient\'s time), or be off');
    }
  }
  if (['SMITLINE_MAX_CALLS_PER_NUMBER', 'SMITLINE_MAX_CALLS_PER_HOUR'].includes(key) && !/^[0-9]{1,4}$/.test(text)) {
    throw new Error(`${key} must be a whole number; 0 turns the limit off`);
  }
  if (key === 'SMITLINE_MEETING_CAMERA_STYLE' && !['still', 'animated'].includes(text)) {
    throw new Error('SMITLINE_MEETING_CAMERA_STYLE must be still or animated');
  }
  if (key === 'SMITLINE_MAX_INBOUND' && !/^[0-9]{1,2}$/.test(text)) {
    throw new Error('SMITLINE_MAX_INBOUND must be a number of simultaneous incoming calls, such as 2');
  }
  if (['SMITLINE_ACCEPT_INBOUND', 'SMITLINE_RECORD_CALLS', 'SMITLINE_STREAM_REALTIME', 'SMITLINE_AUDIO_TRACE',
    'SMITLINE_MEETING_INTRO', 'SMITLINE_PHONE_WEB_SEARCH', 'SMITLINE_MEETING_WEB_SEARCH',
    'SMITLINE_ALLOW_PREMIUM_NUMBERS'].includes(key)
    && !['0', '1'].includes(text)) {
    throw new Error(`${key} must be 0 or 1`);
  }
  if (['SMITLINE_PHONE_BACKEND_MODEL', 'SMITLINE_MEETING_BACKEND_MODEL'].includes(key)
    && !/^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$/.test(text)) {
    throw new Error(`${key} must be a model name, such as gpt-5.6-terra`);
  }
  if (['SMITLINE_PUBLIC_URL', 'SMITLINE_NOTIFY_WEBHOOK'].includes(key) && !/^https:\/\/[^\s/]+/.test(text)) {
    throw new Error(`${key} must be an https URL`);
  }
  if (key === 'SMITLINE_CONNECTOR_URL') return connectorOrigin(text);
  if (key === 'SMITLINE_OWNER_NAME' && (!text || text.length > 120)) {
    throw new Error('SMITLINE_OWNER_NAME must be 1 to 120 characters');
  }
  return text;
}

// Status -------------------------------------------------------------------

function run(binary, args, options = {}) {
  return spawnSync(binary, args, { encoding: 'utf8', timeout: options.timeout || 10_000, env: process.env });
}

function which(binary) {
  const probe = run(process.platform === 'win32' ? 'where' : 'which', [binary]);
  return probe.status === 0 ? probe.stdout.trim().split(/\r?\n/)[0] : null;
}

function inWsl() {
  if (process.platform !== 'linux') return false;
  try {
    return /microsoft/i.test(fs.readFileSync('/proc/version', 'utf8'));
  } catch {
    return false;
  }
}

function portOpen(port, host = '127.0.0.1') {
  return new Promise((resolve) => {
    const socket = net.connect({ port, host });
    socket.setTimeout(400);
    socket.once('connect', () => { socket.destroy(); resolve(true); });
    socket.once('timeout', () => { socket.destroy(); resolve(false); });
    socket.once('error', () => resolve(false));
  });
}

async function verifyOpenAi(key, fetchImpl) {
  try {
    const response = await fetchImpl('https://api.openai.com/v1/models/gpt-live-1', {
      headers: { Authorization: `Bearer ${key}` },
      signal: AbortSignal.timeout(10_000),
    });
    if (response.status === 200) return { ok: true, detail: 'key works and has GPT-Live access' };
    if (response.status === 401) return { ok: false, detail: 'OpenAI rejected the key', fix: 'smitline setup secrets' };
    if (response.status === 404 || response.status === 403) {
      return { ok: false, detail: 'the key cannot use gpt-live-1; GPT-Live needs a paid API tier', fix: 'Add billing at platform.openai.com, then rerun smitline setup status' };
    }
    return { ok: false, detail: `OpenAI answered ${response.status}` };
  } catch {
    return { ok: null, detail: 'could not reach OpenAI to verify the key' };
  }
}

async function verifyPhoneAccount(env, fetchImpl, provider) {
  const sw = provider === 'signalwire';
  const name = sw ? 'SignalWire' : 'Twilio';
  const sid = sw ? env.SIGNALWIRE_PROJECT_ID : env.TWILIO_ACCOUNT_SID;
  const token = sw ? env.SIGNALWIRE_API_TOKEN : env.TWILIO_AUTH_TOKEN;
  const origin = sw ? `https://${signalwireSpace(env.SIGNALWIRE_SPACE)}/api/laml` : 'https://api.twilio.com';
  const auth = `Basic ${Buffer.from(`${sid}:${token}`).toString('base64')}`;
  const base = `${origin}/2010-04-01/Accounts/${encodeURIComponent(sid)}`;
  try {
    const account = await fetchImpl(`${base}.json`, { headers: { Authorization: auth }, signal: AbortSignal.timeout(10_000) });
    if (account.status === 401 || account.status === 404) {
      return { ok: false, detail: `${name} rejected the ${sw ? 'Space, project ID, or API token' : 'account SID or auth token'}`, fix: 'smitline setup secrets' };
    }
    if (!account.ok) return { ok: null, detail: `${name} answered ${account.status}` };
    const body = await account.json();
    const [numbers, verified] = await Promise.all([
      fetchImpl(`${base}/IncomingPhoneNumbers.json?PageSize=50`, { headers: { Authorization: auth } }).then((r) => r.json()).catch(() => ({})),
      fetchImpl(`${base}/OutgoingCallerIds.json?PageSize=50`, { headers: { Authorization: auth } }).then((r) => r.json()).catch(() => ({})),
    ]);
    const trial = body.type === 'Trial';
    const found = {
      trial,
      numbers: (numbers.incoming_phone_numbers || []).map((item) => item.phone_number),
      verified: (verified.outgoing_caller_ids || []).map((item) => item.phone_number),
    };
    if (!sw && trial) {
      // Twilio's trial strips <Stream> from call instructions: the call audio never reaches GPT-Live.
      return {
        ...found,
        ok: false,
        detail: "Twilio's free trial blocks the live call audio Smitline needs; upgrade the Twilio account (add funds), or use SignalWire's free trial",
        fix: 'Upgrade in the Twilio console, or add SignalWire on the setup page (smitline setup secrets)',
      };
    }
    return {
      ...found,
      ok: true,
      detail: `${name} account ${body.status || 'active'}${trial ? ' (trial: calls only numbers you bought or verified)' : ''}`,
    };
  } catch {
    return { ok: null, detail: `could not reach ${name} to verify the account` };
  }
}

function check(id, label, ok, { required = true, group = 'core', detail = '', ask, fix } = {}) {
  const entry = { id, label, ok, required, group, detail };
  if (ask && ok !== true) entry.ask = ask;
  if (fix && ok !== true) entry.fix = fix;
  return entry;
}

export function registeredAgents(root, { runner = run, home = os.homedir(), find = which } = {}) {
  const agents = [];
  if (find('claude')) {
    const probe = runner('claude', ['mcp', 'get', MCP_NAME]);
    agents.push({ id: 'claude-code', installed: true, registered: probe.status === 0 });
  }
  if (find('codex')) {
    const probe = runner('codex', ['mcp', 'get', MCP_NAME]);
    agents.push({ id: 'codex', installed: true, registered: probe.status === 0 });
  }
  const cursorDir = path.join(home, '.cursor');
  if (fs.existsSync(cursorDir)) {
    let registered = false;
    try {
      registered = Boolean(JSON.parse(fs.readFileSync(path.join(cursorDir, 'mcp.json'), 'utf8')).mcpServers?.[MCP_NAME]);
    } catch { /* not registered */ }
    agents.push({ id: 'cursor', installed: true, registered });
  }
  return agents;
}

/** This build's version: SMITLINE_VERSION (set in the image), else the root package.json, else 'dev'. */
export function colleagueVersion(codeRoot, env = process.env) {
  if (present(env.SMITLINE_VERSION)) return env.SMITLINE_VERSION.trim();
  try {
    const { version } = JSON.parse(fs.readFileSync(path.join(codeRoot, 'package.json'), 'utf8'));
    if (typeof version === 'string' && version.trim()) return version.trim();
  } catch { /* no package.json */ }
  return 'dev';
}

/**
 * options.root      the data root (.env); SMITLINE_ROOT in the container.
 * options.codeRoot  the checkout (launcher, node_modules, .venv); defaults to root.
 * options.managed   in the Smitline container (SMITLINE_MANAGED=1): host-only checks
 *                   (operating system, checkout, line endings, Node dependencies, where the
 *                   daemon runs) are replaced by one passing "runs in its container" check.
 */
export async function setupStatus({
  root, codeRoot = root, env: overrides, fetchImpl = globalThis.fetch, verify = true, runner = run, find = which,
  managed = isManaged(), version = colleagueVersion(codeRoot),
} = {}) {
  // The daemon reads the process environment first, then .env; mirror that here.
  const relevant = ([key, value]) => /^(OPENAI_|TWILIO_|SIGNALWIRE_|SMITLINE_)/.test(key) && present(value);
  const env = { ...readEnv(root), ...Object.fromEntries(Object.entries(overrides || process.env).filter(relevant)) };
  const checks = [];
  // Always passes; it puts the version in the plain-text status too.
  checks.push(check('version', 'Smitline version', true, { required: false, detail: version }));
  const major = Number(process.versions.node.split('.')[0]);
  checks.push(check('node', 'Node.js 22 or newer', major >= 22, { detail: process.version, fix: 'Install Node.js 22 or newer' }));
  let docker;
  if (managed) {
    // The daemon and console run in the container, which reaches Docker through the mounted socket.
    checks.push(check('runtime', 'Where Smitline runs', true, { detail: 'in its Docker container' }));
    docker = runner('docker', ['info'], { timeout: 12_000 });
    checks.push(check('docker', 'Docker is running', docker.status === 0, {
      group: 'meetings', required: false, detail: 'needed to join meetings; the container uses the Docker socket mounted into it',
      fix: 'Start the container with -v /var/run/docker.sock:/var/run/docker.sock',
      ask: 'Meetings need Docker access from the Smitline container. Please restart it with the Docker socket mounted and tell me when it is running.',
    }));
  } else {
    if (process.platform === 'win32') {
      checks.push(check('platform', 'Operating system', false, { detail: 'Windows', fix: 'Install WSL2 with Ubuntu and clone Smitline inside the Linux home directory' }));
    } else {
      checks.push(check('platform', 'Operating system', true, { detail: inWsl() ? 'Linux on WSL2' : process.platform }));
      if (inWsl() && codeRoot.startsWith('/mnt/')) {
        checks.push(check('checkout', 'Checkout inside Linux', false, { required: false, detail: 'the checkout is under /mnt, where Docker may not see files', fix: 'Clone inside the Linux home directory' }));
      }
    }
    const launcher = path.join(codeRoot, 'start-runtime-daemon.sh');
    const crlf = fs.existsSync(launcher) && fs.readFileSync(launcher, 'utf8').includes('\r\n');
    checks.push(check('line_endings', 'Unix line endings', !crlf, { fix: 'Clone again inside WSL or Linux' }));
    docker = runner('docker', ['info'], { timeout: 12_000 });
    checks.push(check('docker', 'Docker is running', docker.status === 0, {
      group: 'meetings', required: false, detail: 'needed to join meetings; also runs Smitline when Python is not set up',
      fix: 'Start Docker, or install it inside WSL', ask: 'Please start Docker (or Docker Desktop) and tell me when it is running.',
    }));
    checks.push(check('dependencies', 'Node dependencies installed', fs.existsSync(path.join(codeRoot, 'node_modules', 'mammoth')), { fix: 'npm install' }));
    // start-runtime-daemon.sh runs the daemon on this computer's Python when it can make a
    // venv, otherwise in Docker. A venv made without python3-venv has no pip, so look for pip.
    const venvReady = present(process.env.SMITLINE_PYTHON) || fs.existsSync(path.join(codeRoot, '.venv', 'bin', 'pip'));
    const python = venvReady ? { status: 0 } : runner('python3', ['-c', 'import sys, ensurepip, venv; sys.exit(0 if sys.version_info >= (3, 10) else 3)']);
    const forced = process.env.SMITLINE_DAEMON_RUNTIME;
    const onHost = python.status === 0 && forced !== 'docker';
    const inDocker = !onHost && docker.status === 0 && forced !== 'host';
    const runtimeDetail = onHost ? `Python on this computer${venvReady ? '' : '; the first start sets it up'}`
      : inDocker ? 'Docker; the first start builds a small image. Python is not needed'
        : 'needs Docker (recommended) or Python 3.10 or newer with venv';
    checks.push(check('runtime', 'Where Smitline runs', onHost || inDocker, {
      detail: runtimeDetail,
      fix: 'Start or install Docker (or install Python with venv: sudo apt install -y python3-venv)',
      ask: 'Smitline runs in Docker. Please install or start Docker (Docker Desktop on a Mac) and tell me when it is running.',
    }));
  }

  let openai = { ok: present(env.OPENAI_API_KEY) };
  if (openai.ok && verify) openai = await verifyOpenAi(env.OPENAI_API_KEY, fetchImpl);
  checks.push(check('openai_key', 'OpenAI API key with GPT-Live access', openai.ok, {
    detail: openai.detail || (present(env.OPENAI_API_KEY) ? 'saved' : 'missing'),
    ask: 'Please enter your OpenAI API key on the setup page I opened in your browser. Do not paste it into this chat.',
    fix: openai.fix || 'smitline setup secrets',
  }));
  checks.push(check('owner_name', 'Name to call on behalf of', present(env.SMITLINE_OWNER_NAME), {
    detail: present(env.SMITLINE_OWNER_NAME) ? env.SMITLINE_OWNER_NAME : 'missing',
    ask: "Please add your name on the setup page; every call opens with \"I'm calling on behalf of [your name]\".",
    fix: 'smitline setup secrets (or: smitline setup set SMITLINE_OWNER_NAME "<name>")',
  }));

  // Phone calls go through SignalWire (free trial works) or Twilio (upgraded account).
  const provider = phoneProvider(env);
  const sw = provider === 'signalwire';
  const providerName = sw ? 'SignalWire' : 'Twilio';
  const numberKey = sw ? 'SIGNALWIRE_FROM_NUMBER' : 'TWILIO_FROM_NUMBER';
  const accountSaved = sw
    ? ['SIGNALWIRE_SPACE', 'SIGNALWIRE_PROJECT_ID', 'SIGNALWIRE_API_TOKEN'].every((key) => present(env[key]))
    : present(env.TWILIO_ACCOUNT_SID) && present(env.TWILIO_AUTH_TOKEN);
  let account = { ok: accountSaved };
  if (accountSaved && verify) account = await verifyPhoneAccount(env, fetchImpl, provider);
  checks.push(check('phone_account', accountSaved ? `${providerName} account` : 'Phone provider account', account.ok, {
    group: 'phone', required: false, detail: account.detail || (accountSaved ? 'saved' : 'not set up'),
    ask: 'Do you want phone calls too? SignalWire works with Smitline: sign up at https://signalwire.com (free, no card), then enter its details on the setup page. Its free trial calls only numbers you verify in SignalWire, up to 10; adding a card and $5 of credit lets it call anyone (SETUP.md has the steps).',
    fix: account.fix || 'smitline setup secrets',
  }));
  // Outgoing calls show SMITLINE_CALLER_ID (a verified number) or else the provider number.
  // Incoming calls can only ring a number bought from the provider.
  const from = present(env.SMITLINE_CALLER_ID) ? env.SMITLINE_CALLER_ID : env[numberKey];
  let callerOk = present(from) && E164.test(from);
  let callerDetail = !present(from) ? 'no number chosen yet' : (callerOk ? from : `${from} is not an E.164 number`);
  let callerAsk = `Should calls come from your ${providerName} number, or show your own mobile number (verified in ${providerName})?`;
  let callerFix = `smitline setup set ${numberKey} +1... (or SMITLINE_CALLER_ID for a verified mobile)`;
  let suggest = null;
  if (Array.isArray(account.numbers)) {
    const verified = account.verified || [];
    if (callerOk && !account.numbers.includes(from) && !verified.includes(from)) {
      callerOk = false;
      callerDetail = `${from} is not a number or verified caller ID in this ${providerName} account`;
    } else if (!present(from) && account.numbers.length === 1) {
      // One number: nothing to ask; the agent sets it.
      suggest = { key: numberKey, value: account.numbers[0] };
      callerDetail = `this ${providerName} account has one number, ${account.numbers[0]}`;
      callerAsk = undefined;
      callerFix = `smitline setup set ${numberKey} ${account.numbers[0]}`;
    } else if (!present(from) && account.numbers.length > 1) {
      callerDetail = `${providerName} numbers: ${account.numbers.join(', ')}`;
      callerAsk = `Which number should calls come from: ${account.numbers.join(', ')}?`;
    } else if (!present(from) && verified.length) {
      suggest = { key: 'SMITLINE_CALLER_ID', value: verified[0] };
      callerDetail = `no ${providerName} number yet; ${verified[0]} is verified and can be shown on outgoing calls`;
      callerAsk = undefined;
      callerFix = `smitline setup set SMITLINE_CALLER_ID ${verified[0]}`;
    }
  }
  if (callerOk && !present(env[numberKey])) {
    callerDetail += `; incoming calls also need ${numberKey}, a number bought in ${providerName}`;
  }
  const callerCheck = check('caller_id', 'Number calls come from', callerOk, {
    group: 'phone', required: false, detail: callerDetail, ask: callerAsk, fix: callerFix,
  });
  if (suggest && !callerOk) callerCheck.suggest = suggest;
  checks.push(callerCheck);
  let ownerDetail = env.SMITLINE_OWNER_PHONE || 'missing';
  if (present(env.SMITLINE_OWNER_PHONE) && account.trial && !(account.verified || []).includes(env.SMITLINE_OWNER_PHONE)) {
    ownerDetail += `; a ${providerName} trial can only call it once it is verified in ${providerName} (Verified Caller IDs)`;
  }
  checks.push(check('owner_phone', 'Your phone number (for the test call and transfers)', present(env.SMITLINE_OWNER_PHONE), {
    group: 'phone', required: false, detail: ownerDetail,
    ask: 'Please add your phone number on the setup page; I will call it once so you can hear Smitline.',
    fix: 'smitline setup secrets (or: smitline setup set SMITLINE_OWNER_PHONE +1...)',
  }));
  // Direct SIP keeps call audio between the provider and OpenAI; the relay passes it through here.
  const audioMode = PHONE_AUDIO_MODES.includes(env.SMITLINE_PHONE_AUDIO) ? env.SMITLINE_PHONE_AUDIO : 'relay';
  const audioNeeds = { sip: ['SMITLINE_SIP_TRUNK_URL', 'SMITLINE_SIP_USERNAME', 'SMITLINE_SIP_PASSWORD'],
    'sip-webhook': ['OPENAI_PROJECT_ID', 'OPENAI_WEBHOOK_SECRET'], relay: [] }[audioMode];
  const audioMissing = audioNeeds.filter((key) => !present(env[key]));
  const audioDetail = {
    relay: 'relayed through this computer; direct SIP sounds more natural: smitline setup sip-trunk',
    sip: 'direct SIP: OpenAI dials out through the provider trunk. Until OpenAI enables outbound SIP for your organization, calls are relayed',
    'sip-webhook': 'direct SIP: the provider hands answered calls to OpenAI, which notifies Smitline with a webhook',
  }[audioMode];
  checks.push(check('phone_audio', 'How call audio travels', audioMissing.length === 0, {
    group: 'phone', required: false,
    detail: audioMissing.length ? `${audioDetail}; missing ${audioMissing.join(', ')}` : audioDetail,
    fix: audioMode === 'sip' ? 'smitline setup sip-trunk' : 'smitline setup secrets',
  }));
  const reachable = audioMode === 'sip' || present(env.SMITLINE_PUBLIC_URL) || Boolean(find('cloudflared')) || docker.status === 0;
  checks.push(check('public_url', 'The phone provider can reach this computer', reachable, {
    group: 'phone', required: false,
    detail: present(env.SMITLINE_PUBLIC_URL) ? env.SMITLINE_PUBLIC_URL : (reachable ? 'Cloudflare quick tunnel on first call' : 'no tunnel available'),
    fix: 'Start Docker or install cloudflared, or set SMITLINE_PUBLIC_URL on a server',
  }));

  if (managed) {
    // Agents run on the host, which the container cannot see.
    checks.push(check('agents', 'Registered with a local agent', null, {
      group: 'agents', required: false,
      detail: 'agents run outside the container; smitline setup register prints how to connect them',
      fix: 'smitline setup register',
    }));
  } else {
    const agents = registeredAgents(root, { runner, find });
    checks.push(check('agents', 'Registered with a local agent', agents.some((a) => a.registered), {
      group: 'agents', required: false,
      detail: agents.length ? agents.map((a) => `${a.id}${a.registered ? ' (registered)' : ''}`).join(', ') : 'no supported local agent CLI found',
      fix: 'smitline setup register',
    }));
  }
  const daemon = await portOpen(Number(process.env.SMITLINE_DAEMON_PORT || 8765));
  if (managed) {
    checks.push(check('daemon', 'Runtime daemon', daemon, {
      required: false, detail: daemon ? 'running' : 'not running', fix: 'Restart the container: docker restart smitline',
    }));
  } else {
    checks.push(check('daemon', 'Runtime daemon', true, { required: false, detail: daemon ? 'running' : 'starts automatically on first use' }));
  }

  const coreReady = checks.filter((c) => c.required).every((c) => c.ok === true);
  const phoneReady = coreReady && checks.filter((c) => c.group === 'phone' && c.id !== 'owner_phone').every((c) => c.ok === true);
  const meetingsReady = coreReady && checks.find((c) => c.id === 'docker').ok === true;
  // The first call rings the owner's phone; without phone setup it is a meeting instead.
  const firstCallReady = phoneReady && checks.find((c) => c.id === 'owner_phone').ok === true;
  // Phone steps become next steps once the user has started on phone calls; until then
  // only the question "do you want phone calls?" is asked, and the rest is optional.
  const wantsPhone = accountSaved || [numberKey, 'SMITLINE_CALLER_ID', 'SMITLINE_OWNER_PHONE', 'SIGNALWIRE_SPACE', 'SIGNALWIRE_PROJECT_ID', 'TWILIO_ACCOUNT_SID']
    .some((key) => present(env[key]));
  const step = (c) => ({ id: c.id, ask: c.ask, fix: c.fix, ...(c.suggest ? { suggest: c.suggest } : {}) });
  const pending = checks.filter((c) => c.ok !== true);
  const isNext = (c) => c.required || c.group === 'agents' || (c.group === 'phone' && (wantsPhone || c.id === 'phone_account'));
  const next = pending.filter(isNext).map(step);
  const optional = pending.filter((c) => !isNext(c) && ['phone', 'meetings'].includes(c.group)).map(step);
  return {
    version,
    ready: coreReady,
    phoneReady,
    meetingsReady,
    firstCallReady,
    voice: env.SMITLINE_VOICE || 'marin',
    voices: availableVoices(env),
    checks,
    next,
    optional,
  };
}

// Direct SIP ---------------------------------------------------------------

/**
 * Create what OpenAI needs to dial out through SignalWire: a SWML script that calls the
 * requested number from the account's number, and a password-protected SIP address that
 * runs it (encryption required, Opus offered). Returns the trunk settings to save; the
 * password is generated here and never shown.
 */
export async function createSignalWireTrunk({ env, fetchImpl = globalThis.fetch, password = crypto.randomBytes(24).toString('base64url'), name = 'smitline-openai' }) {
  const space = signalwireSpace(env.SIGNALWIRE_SPACE);
  const number = env.SMITLINE_CALLER_ID || env.SIGNALWIRE_FROM_NUMBER;
  if (!space || !present(env.SIGNALWIRE_PROJECT_ID) || !present(env.SIGNALWIRE_API_TOKEN)) {
    throw new Error('Set up SignalWire first: smitline setup secrets');
  }
  if (!E164.test(String(number || ''))) throw new Error('Choose the number calls come from first (SIGNALWIRE_FROM_NUMBER)');
  const auth = `Basic ${Buffer.from(`${env.SIGNALWIRE_PROJECT_ID}:${env.SIGNALWIRE_API_TOKEN}`).toString('base64')}`;
  const post = async (path, body) => {
    const response = await fetchImpl(`https://${space}${path}`, {
      method: 'POST',
      headers: { Authorization: auth, 'Content-Type': 'application/json', Accept: 'application/json' },
      body: JSON.stringify(body),
      signal: AbortSignal.timeout(20_000),
    });
    const text = await response.text();
    let json = {};
    try { json = text ? JSON.parse(text) : {}; } catch { json = {}; }
    if (!response.ok) {
      const reason = json.errors?.[0]?.detail || json.message || json.error || text.slice(0, 200);
      throw new Error(`SignalWire refused ${path} (${response.status}): ${reason}`);
    }
    return json;
  };
  const script = await post('/api/fabric/resources/swml_scripts', {
    name: `${name}-outbound`,
    contents: {
      version: '1.0.0',
      sections: {
        main: [{
          connect: {
            answer_on_bridge: true,
            from: number,
            // OpenAI dials sips:+1…@host; keep only the number.
            to: "%{call.to.replace(/^sips?:/i, '').replace(/@.*/, '')}",
          },
        }],
      },
    },
  });
  const scriptId = script.id || script.data?.id;
  if (!scriptId) throw new Error('SignalWire did not return the script id');
  const address = await post('/api/fabric/sip_addresses', {
    name,
    calling_handler_resource_id: scriptId,
    user: '*',
    encryption: 'required',
    codecs: ['OPUS', 'PCMU', 'PCMA'],
    password,
  });
  const uri = String(address.uri || address.data?.uri || '');
  const host = uri.replace(/^sips?:/i, '').replace(/^[^@]*@/, '').replace(/[;:?].*$/, '');
  if (!/^[a-z0-9.-]+$/i.test(host)) throw new Error('SignalWire did not return the SIP address');
  return {
    settings: {
      SMITLINE_SIP_TRUNK_URL: `sips:${host}:5061`,
      SMITLINE_SIP_USERNAME: number,
      SMITLINE_SIP_PASSWORD: password,
      SMITLINE_PHONE_AUDIO: 'sip',
    },
    scriptId,
    addressId: address.id || address.data?.id || null,
  };
}

// Secrets page -------------------------------------------------------------

// Where a saved secret is revoked. Removing it from Smitline only deletes Smitline's copy.
const REVOKE = {
  openai: { url: 'https://platform.openai.com/api-keys', where: 'platform.openai.com/api-keys' },
  signalwire: { url: 'https://signalwire.com/signin', where: 'your SignalWire Space, under API Credentials' },
  twilio: { url: 'https://console.twilio.com/', where: 'the Twilio console, under Account > API keys & tokens' },
  webhook: { url: 'https://platform.openai.com/settings', where: 'Settings > Project > Webhooks on platform.openai.com' },
};

// Also the field list of the console's Account page.
export const FIELDS = [
  { key: 'OPENAI_API_KEY', label: 'OpenAI API key', group: 'Required', secret: true, revoke: REVOKE.openai, hint: 'Starts with sk-. Create one at https://platform.openai.com/api-keys. The live voice needs billing turned on (a paid API tier).' },
  { key: 'SMITLINE_OWNER_NAME', label: 'Your name', group: 'Required', hint: 'Every call opens with: “Hi, I’m calling on behalf of [your name] about…”' },
  { key: 'SMITLINE_OWNER_PHONE', label: 'Your phone number', group: 'Phone calls (optional)', hint: 'Smitline rings it for the test call and when you take over a call. Include the country code, such as +1 415 555 0142.' },
  { key: 'SMITLINE_CALLER_ID', label: 'Show my own number (optional)', group: 'Phone calls (optional)', hint: 'A number you verified with SignalWire or Twilio (Verified Caller IDs). Outgoing calls show it instead of the provider number. Incoming calls still ring the provider number.' },
  { key: 'SIGNALWIRE_SPACE', label: 'Space URL', group: 'SignalWire (free trial)', hint: 'The address you sign in at, such as yourname.signalwire.com.' },
  { key: 'SIGNALWIRE_PROJECT_ID', label: 'Project ID', group: 'SignalWire (free trial)', hint: 'On the API Credentials page of your SignalWire Dashboard.' },
  { key: 'SIGNALWIRE_API_TOKEN', label: 'API token', group: 'SignalWire (free trial)', secret: true, revoke: REVOKE.signalwire, hint: 'API Credentials > New, with the Voice and Numbers permissions. Copy it right after you create it; it starts with SWAPI (older tokens start with PT).' },
  { key: 'SIGNALWIRE_SIGNING_KEY', label: 'Signing key', group: 'SignalWire (free trial)', secret: true, revoke: REVOKE.signalwire, hint: 'On the API Credentials page, under Signing Key, select Show (it appears once you have a token). It lets Smitline check that call updates really come from SignalWire.' },
  { key: 'SIGNALWIRE_FROM_NUMBER', label: 'SignalWire phone number', group: 'SignalWire (free trial)', hint: 'A number from Phone Numbers in SignalWire. You can leave it empty and show a verified number instead ("Show my own number" above).' },
  { key: 'TWILIO_ACCOUNT_SID', label: 'Twilio Account SID', group: 'Twilio (upgraded account)', secret: true, hint: 'Starts with AC. Find it under Account Info on the home page of https://console.twilio.com.' },
  { key: 'TWILIO_AUTH_TOKEN', label: 'Twilio Auth Token', group: 'Twilio (upgraded account)', secret: true, revoke: REVOKE.twilio, hint: 'Next to the Account SID in the Twilio console. Press Show, then copy it.' },
  { key: 'TWILIO_FROM_NUMBER', label: 'Twilio phone number', group: 'Twilio (upgraded account)', hint: 'A number you bought in Twilio. Include the country code, such as +1 415 555 0142.' },
  { key: 'OPENAI_PROJECT_ID', label: 'OpenAI project ID', group: 'Direct phone audio (advanced)', hint: 'Only for direct audio through an OpenAI webhook (SMITLINE_PHONE_AUDIO=sip-webhook). Settings > Project > General on platform.openai.com; it starts with proj_.' },
  { key: 'OPENAI_WEBHOOK_SECRET', label: 'OpenAI webhook signing secret', group: 'Direct phone audio (advanced)', secret: true, revoke: REVOKE.webhook, hint: 'Shown once when you create the webhook in Settings > Project > Webhooks. It starts with whsec_.' },
  { key: 'SMITLINE_CONNECTOR_URL', label: 'Connector address', group: 'Remote connector (server mode)', hint: 'This server’s public address, starting with https:// and nothing after the name, such as smitline.example.com. See docs/agents.md.' },
  { key: 'SMITLINE_CONNECTOR_PASSPHRASE', label: 'Owner passphrase', group: 'Remote connector (server mode)', secret: true, spaces: true, minLength: CONNECTOR_PASSPHRASE_MIN, hint: 'At least 12 characters; a few random words work well. You type it each time you approve an app that connects.' },
];

export function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

// Shared by the local setup page and the connector's approval page. Tokens and
// the brand mark follow the local console (control-panel/styles.css).
export const PAGE_STYLE = `:root{color-scheme:light dark;--bg:#f6f6f9;--card:#fff;--field:#fff;--ink:#25262b;--muted:#5f626c;--line:#e5e6eb;--line-strong:#cfd0d8;--accent:#555bc0;--accent-hover:#464ca8;--on-accent:#fff;--accent-soft:#ebecfa;--mark:#30323b;--good:#1d6b45;--good-bg:#e7f4ec;--good-line:#bfe0cc;--warn:#7d4f0b;--warn-bg:#fdf3e1;--warn-line:#f0d7a8;--bad:#a3303d;--bad-bg:#fcecee;--bad-line:#efc9ce}
@media (prefers-color-scheme:dark){:root{--bg:#131418;--card:#1b1c21;--field:#131418;--ink:#e8e9ef;--muted:#a9acb8;--line:#2e3038;--line-strong:#4a4d59;--accent:#a9adf5;--accent-hover:#bfc2f8;--on-accent:#15161b;--accent-soft:#272a4a;--mark:#e8e9ef;--good:#86d9a8;--good-bg:#16301f;--good-line:#25533a;--warn:#f2c678;--warn-bg:#36290f;--warn-line:#5c4518;--bad:#f5a0a9;--bad-bg:#3a1a1f;--bad-line:#62303a}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;padding:0 16px 56px}
main{max-width:600px;margin:0 auto;display:grid;gap:20px}
.brand{display:flex;align-items:center;gap:10px;padding:24px 0 4px;font-size:16px;font-weight:600;letter-spacing:-.4px}
.brand b{font-weight:400;color:var(--muted)}
.brand-mark{width:24px;height:24px;border-radius:6px;background:var(--mark);display:flex;align-items:center;justify-content:center;gap:3px}
.brand-mark i{display:block;width:3px;height:11px;border-radius:2px;background:var(--bg)}
.brand-mark i:nth-child(2){height:16px}
h1{font-size:26px;line-height:1.25;letter-spacing:-.6px;margin:0}
h2{font-size:16px;line-height:1.3;margin:0}
p{margin:0}
a{color:var(--accent);text-underline-offset:2px}
code{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:.88em;overflow-wrap:anywhere}
.lead{color:var(--muted);font-size:16px}
.muted,small{color:var(--muted)}
.hint{color:var(--muted);font-size:13px;line-height:1.5}
.card,fieldset{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:20px;margin:0;min-width:0}
fieldset{display:grid;gap:18px}
legend{float:left;width:100%;padding:0;font-size:16px;font-weight:600}
label{font-weight:600}
input{font:inherit;width:100%;min-height:44px;padding:10px 12px;border:1px solid var(--line-strong);border-radius:8px;background:var(--field);color:var(--ink)}
input::placeholder{color:var(--muted);opacity:1}
input[aria-invalid="true"]{border-color:var(--bad);border-width:2px}
:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
input:focus-visible{outline-offset:0;border-color:var(--accent)}
button{font:inherit;font-weight:600;min-height:44px;padding:10px 20px;border:1px solid var(--accent);border-radius:8px;background:var(--accent);color:var(--on-accent);cursor:pointer}
button:hover{background:var(--accent-hover);border-color:var(--accent-hover)}
button.secondary{background:var(--card);color:var(--ink);border-color:var(--line-strong)}
button.secondary:hover{background:var(--bg);border-color:var(--muted)}
.actions{display:flex;gap:10px;flex-wrap:wrap;align-items:center}
.note,.notice{border:1px solid var(--line);border-left:4px solid var(--accent);border-radius:8px;padding:12px 16px;background:var(--card);display:grid;gap:6px}
.notice.success{border-color:var(--good-line);border-left-color:var(--good);background:var(--good-bg)}
.notice.warn{border-color:var(--warn-line);border-left-color:var(--warn);background:var(--warn-bg)}
.notice.error,.error{border:1px solid var(--bad-line);border-left:4px solid var(--bad);border-radius:8px;padding:12px 16px;background:var(--bad-bg);color:var(--ink)}
.notice ul{margin:0;padding-left:1.2em}
.notice.error a{color:var(--ink)}
.notice strong{font-weight:600}
.notice.success strong{color:var(--good)}
.notice.error strong{color:var(--bad)}
.notice.warn strong{color:var(--warn)}
.visually-hidden{position:absolute;width:1px;height:1px;margin:-1px;padding:0;overflow:hidden;clip:rect(0 0 0 0);white-space:nowrap;border:0}
.promises{list-style:none;margin:0;padding:0;display:grid;gap:10px}
.promises li{display:grid;grid-template-columns:22px 1fr;gap:10px;align-items:start}
.promises .icon{width:22px;height:22px;border-radius:50%;background:var(--accent-soft);color:var(--accent);display:flex;align-items:center;justify-content:center;font-size:12px;font-weight:700;margin-top:1px}
.promises strong{display:block}
.group-hint{color:var(--muted);font-size:14px;margin-top:-10px}
.field{display:grid;gap:6px}
.label-row{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.badge{font-size:12px;font-weight:600;line-height:1;padding:4px 8px;border-radius:999px}
.badge.required{color:var(--bad);background:var(--bad-bg)}
.badge.saved{color:var(--good);background:var(--good-bg)}
.badge.optional{color:var(--muted);background:var(--bg);border:1px solid var(--line)}
.field-error{color:var(--bad);font-size:14px;font-weight:600}
details.card{padding:0}
details.card>summary{cursor:pointer;padding:16px 20px;font-weight:600;list-style-position:inside;border-radius:12px}
details.card[open]>summary{border-bottom:1px solid var(--line);border-radius:12px 12px 0 0}
.details-body{padding:18px 20px 20px;display:grid;gap:18px}
.details-body .group-hint{margin-top:0}
form{display:grid;gap:16px}
.submit{display:grid;gap:10px;margin-top:4px}
.done-check{width:44px;height:44px;border-radius:50%;background:var(--good-bg);color:var(--good);display:flex;align-items:center;justify-content:center;font-size:22px;font-weight:700}
.done-check.warn{background:var(--warn-bg);color:var(--warn)}
.done{display:grid;gap:14px}
.done ul{margin:0;padding-left:1.2em}
@media (max-width:480px){h1{font-size:23px}.card,fieldset{padding:16px}.actions button{flex:1 1 auto}}`;

/**
 * The local page where the user types keys. Secret values are never rendered:
 * a saved secret shows only as "Saved".
 *
 * options.tone      'success' | 'error' | 'info' (default) styles `message`.
 * options.savedNow  keys saved by the last submission, confirmed at the top by label.
 * options.errors    validation messages; each appears next to the field it names
 *                   (by label or key) and in a summary at the top.
 * options.done      adds a "Done" button that submits the form with action=done.
 * options.finished  renders the closing confirmation instead of the form.
 */
export function renderSecretsPage(saved, action, message = '', options = {}) {
  const { tone = 'info', savedNow = [], errors = [], done = false, finished = false } = options;
  const brand = '<div class="brand"><span class="brand-mark" aria-hidden="true"><i></i><i></i><i></i></span><span>Smitline</span></div>';
  const groupTitles = { Required: 'The basics' };
  const groupHints = {
    Required: 'Required for every call and meeting.',
    'Phone calls (optional)': 'Skip the phone sections if you only want Smitline in video meetings. For phone calls, add your number here and one provider below. SignalWire has a free trial that works with Smitline; it calls only numbers you verify in SignalWire (up to 10) until you add a card and $5 of credit.',
    'SignalWire (free trial)': 'Sign up free at https://signalwire.com (no card needed). A trial calls only numbers you verify in SignalWire (Phone Numbers > Verified Caller IDs), up to 10, in the US and Canada. Adding $5 lifts that.',
    'Twilio (upgraded account)': "Twilio's free trial blocks the live call audio Smitline needs, so use Twilio only with an upgraded account.",
    'Remote connector (server mode)': 'Only for running Smitline on a server so cloud agents, such as ChatGPT or Claude on the web, can use it. Most people skip this.',
  };
  const isSaved = (field) => present(saved[field.key]);
  const required = (field) => field.group === 'Required';
  // Escaped text with its https links made clickable; links open in a new tab so this page stays open.
  const linked = (text) => escapeHtml(text).replace(/https:\/\/[a-z0-9][^\s<]*[^\s<.,;:)”]/gi, (url) =>
    `<a href="${url}" target="_blank" rel="noopener noreferrer">${url.replace(/^https:\/\//, '')}</a>`);
  // Validation messages name settings by key; show the field's label and plain words instead.
  const plain = (text) => {
    let value = String(text);
    for (const field of FIELDS) value = value.split(`${field.key} `).join(`${field.label.replace(/ \(optional\)$/, '')} `);
    return value.replace(/must be an E\.164 number such as \+\d+/g, 'must include the country code and start with +, such as +1 415 555 0142');
  };
  const friendly = (text) => plain(text).replace(/^(.)/, (c) => c.toUpperCase()).replace(/([^.])$/, '$1.');
  const fieldFor = (error) => FIELDS.find((f) => String(error).startsWith(f.label) || String(error).startsWith(`${f.key} `));
  const fieldErrors = new Map();
  for (const error of errors) {
    const field = fieldFor(error);
    // Nothing typed is kept after an error, so say so next to the field.
    if (field && !fieldErrors.has(field.key)) {
      fieldErrors.set(field.key, / is required/.test(error) ? friendly(error) : `${friendly(error)} Enter it again.`);
    }
  }
  const head = (title) => `<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>${escapeHtml(title)}</title><style>
${PAGE_STYLE}</style></head>`;

  if (finished) {
    const savedLabels = FIELDS.filter(isSaved).map((f) => `<li>${escapeHtml(f.label)}</li>`).join('');
    const missing = FIELDS.filter((f) => required(f) && !isSaved(f)).map((f) => f.label);
    return `${head('Smitline setup: done')}
<body><main>${brand}
<section class="card done" aria-labelledby="done-title">
<div class="done-check${missing.length ? ' warn' : ''}" aria-hidden="true">${missing.length ? '!' : '✓'}</div>
<h1 id="done-title">${missing.length ? `Saved. ${missing.length === 1 ? 'One thing is' : 'A few things are'} still missing.` : 'All set'}</h1>
<p class="lead">You can close this tab and go back to your agent. It continues setup from here, and it learns which fields you filled in, never what you typed.</p>
${savedLabels ? `<h2>Saved on this computer</h2><ul>${savedLabels}</ul>` : '<p>Nothing new was saved.</p>'}
${missing.length ? `<div class="notice warn"><strong>Still needed: ${escapeHtml(missing.join(', '))}</strong><span>Your agent will ask you about ${missing.length === 1 ? 'it' : 'them'}.</span></div>` : ''}
<p class="hint">This page has stopped working, so no one else can use it. To change a key later, ask your agent to open the setup page again.</p>
</section></main></body></html>`;
  }

  const fieldHtml = (field) => {
    const id = field.key;
    const error = fieldErrors.get(field.key);
    const placeholder = isSaved(field) ? (field.secret ? 'Saved. Leave empty to keep it.' : `Saved: ${saved[field.key]}`) : '';
    const badge = isSaved(field) ? '<span class="badge saved">✓ Saved</span>'
      : required(field) ? '<span class="badge required">Required</span>' : '';
    const describedBy = [error ? `${id}-error` : '', field.hint ? `${id}-hint` : ''].filter(Boolean).join(' ');
    const phone = /PHONE|NUMBER|CALLER_ID/.test(field.key);
    const type = field.secret ? 'password' : phone ? 'tel' : 'text';
    return `<div class="field">
<div class="label-row"><label for="${id}">${escapeHtml(field.label)}</label>${badge}</div>
<input id="${id}" name="${field.key}" type="${type}" autocomplete="off" spellcheck="false" autocapitalize="off"${phone ? ' inputmode="tel"' : ''} placeholder="${escapeHtml(placeholder)}"${describedBy ? ` aria-describedby="${describedBy}"` : ''}${required(field) && !isSaved(field) ? ' aria-required="true"' : ''}${error ? ' aria-invalid="true"' : ''}>
${error ? `<p class="field-error" id="${id}-error">${escapeHtml(error)}</p>` : ''}
${field.hint ? `<p class="hint" id="${id}-hint">${linked(field.hint)}</p>` : ''}
</div>`;
  };
  const groups = [...new Set(FIELDS.map((f) => f.group))];
  const sections = groups.map((group) => {
    const fields = FIELDS.filter((f) => f.group === group);
    const rows = fields.map(fieldHtml).join('\n');
    const hint = groupHints[group] ? `<p class="group-hint">${linked(groupHints[group])}</p>` : '';
    // Server-mode settings stay folded away unless they are in use.
    if (/\((server mode|optional extra|upgraded account|advanced)\)$/.test(group)) {
      const open = fields.some((f) => isSaved(f) || fieldErrors.has(f.key)) ? ' open' : '';
      return `<details class="card"${open}><summary>${escapeHtml(group)}</summary><div class="details-body">${hint}${rows}</div></details>`;
    }
    return `<fieldset><legend>${escapeHtml(groupTitles[group] || group)}</legend>${hint}${rows}</fieldset>`;
  }).join('\n');

  const errorItems = errors.map((error) => {
    const field = fieldFor(error);
    const text = escapeHtml(friendly(error));
    return `<li>${field ? `<a href="#${field.key}">${text}</a>` : text}</li>`;
  }).join('');
  const list = (items) => (items.length < 3 ? items.join(' and ') : `${items.slice(0, -1).join(', ')}, and ${items.at(-1)}`);
  const savedLabels = savedNow.map((key) => FIELDS.find((f) => f.key === key)?.label).filter(Boolean);
  const notices = [];
  if (savedLabels.length) {
    const next = errors.length ? ' Fix the fields marked below, then save again.' : done ? ' Add more, or press Done when you are finished.' : '';
    notices.push(`<div class="notice success" role="status"><strong>Saved on this computer</strong><span>${escapeHtml(list(savedLabels))}.${next}</span></div>`);
  }
  if (errors.length) {
    notices.push(`<div class="notice error" role="alert"><strong>${errors.length === 1 ? 'One field needs a fix' : `${errors.length} fields need a fix`}</strong>${message ? `<span>${escapeHtml(plain(message))}</span>` : ''}<ul>${errorItems}</ul></div>`);
  } else if (message) {
    const title = { success: 'Saved', error: 'Something went wrong' }[tone];
    notices.push(`<div class="notice ${escapeHtml(tone)}" role="${tone === 'error' ? 'alert' : 'status'}">${title ? `<strong>${title}</strong>` : ''}<span>${escapeHtml(plain(message))}</span></div>`);
  }
  const notice = notices.join('\n');
  // Once something is saved, finishing becomes the obvious next step.
  const doneFirst = done && savedLabels.length > 0 && !errors.length;
  const saveButton = `<button type="submit" name="action" value="save"${doneFirst ? ' class="secondary"' : ''}>Save</button>`;
  const doneButton = done ? `<button type="submit" name="action" value="done"${doneFirst ? '' : ' class="secondary"'}>Done, return to your agent</button>` : '';
  const submitHint = done
    ? 'Save as often as you like. Done saves anything you typed, closes this page, and lets your agent continue.'
    : 'Fill in what you have, then press Save. Your agent continues setup from there.';
  return `${head('Smitline setup')}
<body><main>${brand}
<header><h1>Smitline setup</h1></header>
<p class="lead">Type your keys here, not in the chat with your agent.</p>
<ul class="promises">
<li><span class="icon" aria-hidden="true">✓</span><span><strong>Saved only on this computer</strong><span class="muted">In the project’s private <code>.env</code> file, readable only by your user account.</span></span></li>
<li><span class="icon" aria-hidden="true">✓</span><span><strong>Your agent never sees them</strong><span class="muted">It only learns which fields you filled in.</span></span></li>
<li><span class="icon" aria-hidden="true">✓</span><span><strong>Private to this computer</strong><span class="muted">Only this computer can open this page, and it stops working when setup is done.</span></span></li>
</ul>
${notice}
<form method="post" action="${escapeHtml(action)}" autocomplete="off" novalidate>
${sections}
<div class="submit"><div class="actions">${saveButton}${doneButton}</div><p class="hint">${submitHint}</p></div>
</form></main></body></html>`;
}

function readBody(request, limit = 16 * 1024) {
  return new Promise((resolve, reject) => {
    let size = 0;
    const chunks = [];
    request.on('data', (chunk) => {
      size += chunk.length;
      if (size > limit) { reject(new Error('too large')); request.destroy(); return; }
      chunks.push(chunk);
    });
    request.on('end', () => resolve(Buffer.concat(chunks).toString('utf8')));
    request.on('error', reject);
  });
}

export function sanitizeSubmission(form) {
  const updates = {};
  const errors = [];
  for (const field of FIELDS) {
    let value = String(form.get(field.key) || '').trim();
    if (!value) continue;
    if (field.secret && !field.spaces) {
      // Keys never contain whitespace; copying one from a wrapped display can add line
      // breaks or invisible spaces, so drop them instead of rejecting the paste.
      value = value.replace(/[\s\u200B-\u200D\u2060\uFEFF]+/g, '');
    } else if (field.secret && /[\r\n]/.test(value)) {
      errors.push(`${field.label} must be one line`);
      continue;
    }
    if (field.minLength && value.length < field.minLength) {
      errors.push(`${field.label} must be at least ${field.minLength} characters`);
      continue;
    }
    try {
      updates[field.key] = field.secret ? value : validateSetting(field.key, value);
    } catch (error) {
      errors.push(error.message);
    }
  }
  return { updates, errors };
}

// Done must not leave a required key missing or a phone provider half set up.
const REQUIRED_TOGETHER = [
  ['SignalWire', ['SIGNALWIRE_SPACE', 'SIGNALWIRE_PROJECT_ID', 'SIGNALWIRE_API_TOKEN', 'SIGNALWIRE_SIGNING_KEY']],
  ['Twilio', ['TWILIO_ACCOUNT_SID', 'TWILIO_AUTH_TOKEN']],
];

export function missingForDone(env) {
  const errors = [];
  for (const field of FIELDS.filter((f) => f.group === 'Required')) {
    if (!present(env[field.key])) errors.push(`${field.label} is required`);
  }
  for (const [name, keys] of REQUIRED_TOGETHER) {
    if (!keys.some((key) => present(env[key]))) continue;
    for (const key of keys) {
      if (!present(env[key])) errors.push(`${FIELDS.find((f) => f.key === key).label} is required for ${name} phone calls`);
    }
  }
  return errors;
}

// Account page (control panel) ---------------------------------------------

// The fixed starts of key formats, which say what kind of key is saved and nothing secret.
const KEY_PREFIXES = ['sk-proj-', 'sk-svcacct-', 'sk-admin-', 'sk-', 'whsec_', 'SWAPI', 'PSK_', 'PT', 'AC'];

/** A saved secret's known prefix, such as "sk-proj-", or '' for a key with none. Never a random character. */
export function secretPreview(field, value) {
  const text = String(value || '');
  if (field.spaces) return '';
  return KEY_PREFIXES.find((prefix) => text.startsWith(prefix) && text.length > prefix.length * 4) || '';
}

/**
 * What the console's Account page shows for each field: whether it is saved, and for a
 * secret only its preview, never its value. A value from the process environment wins
 * over .env, as in the daemon, and can only be changed where it is set.
 */
export function accountFields(env, environment = process.env) {
  return FIELDS.map((field) => {
    const fromEnvironment = present(environment[field.key]);
    const value = fromEnvironment ? environment[field.key] : env[field.key];
    const saved = present(value);
    const entry = {
      key: field.key,
      label: field.label,
      group: field.group,
      hint: field.hint,
      secret: Boolean(field.secret),
      saved,
      fromEnvironment,
    };
    if (saved) entry.shown = field.secret ? secretPreview(field, value) : value;
    if (field.revoke) entry.revoke = field.revoke;
    return entry;
  });
}

/** Delete one field from .env. It is only Smitline's copy: a key stays valid where it was issued. */
export function removeSetting(root, key) {
  if (!FIELDS.some((field) => field.key === key)) throw new Error(`unknown setting ${key}`);
  return writeEnv(root, { [key]: null });
}

/**
 * Serve the one-time secrets page until the user presses Done. Each Save writes
 * what was typed and keeps the page open; onSaved(keys) runs after each save.
 * Resolves with every key saved during the session. On timeout it resolves with
 * timedOut: true if anything was saved, and rejects otherwise.
 */
// Users often sign up for OpenAI or a phone provider while the page waits, so it lasts an hour.
export const SECRETS_PAGE_MINUTES = 60;
// A fixed port, so the address is predictable (Docker Desktop's host networking, SSH
// forwarding). SMITLINE_SETUP_PORT overrides it; if it is busy, any free port is used.
export const SETUP_PAGE_PORT = 8096;

export function setupPagePort(env = process.env) {
  const port = Number(env.SMITLINE_SETUP_PORT);
  return Number.isInteger(port) && port > 0 && port < 65536 ? port : SETUP_PAGE_PORT;
}

export function serveSecretsPage({ root, port = setupPagePort(), timeoutMs = SECRETS_PAGE_MINUTES * 60_000, onUrl, onSaved } = {}) {
  const token = crypto.randomBytes(18).toString('base64url');
  const pathName = `/setup/${token}`;
  const savedKeys = [];
  let closed = false;
  return new Promise((resolve, reject) => {
    const finish = () => {
      closed = true;
      clearTimeout(timer);
      server.close();
      server.closeIdleConnections?.();
    };
    const server = http.createServer(async (request, response) => {
      const url = new URL(request.url, 'http://127.0.0.1');
      const headers = {
        'Content-Type': 'text/html; charset=utf-8',
        'Cache-Control': 'no-store',
        'X-Frame-Options': 'DENY',
        'Referrer-Policy': 'no-referrer',
        'Content-Security-Policy': "default-src 'none'; style-src 'unsafe-inline'; img-src data:; form-action 'self'; frame-ancestors 'none'; base-uri 'none'",
      };
      if (url.pathname !== pathName) {
        response.writeHead(404, headers).end('Not found');
        return;
      }
      if (closed) {
        response.writeHead(410, headers).end('This setup page is closed. Ask your agent to open it again.');
        return;
      }
      if (request.method === 'GET') {
        response.writeHead(200, headers).end(renderSecretsPage(readEnv(root), pathName, '', { done: true }));
        return;
      }
      if (request.method !== 'POST') {
        response.writeHead(405, headers).end();
        return;
      }
      try {
        const form = new URLSearchParams(await readBody(request));
        const { updates, errors } = sanitizeSubmission(form);
        // Valid fields are saved even when another field needs a fix, so nothing typed correctly is lost.
        const saved = Object.keys(updates).length ? writeEnv(root, updates) : [];
        for (const key of saved) if (!savedKeys.includes(key)) savedKeys.push(key);
        if (saved.length) onSaved?.(saved);
        const env = readEnv(root);
        if (errors.length) {
          response.writeHead(422, headers).end(renderSecretsPage(env, pathName, '', { savedNow: saved, errors, done: true }));
          return;
        }
        if (form.get('action') === 'done') {
          const missing = missingForDone(env);
          if (missing.length) {
            response.writeHead(422, headers).end(renderSecretsPage(env, pathName, '', { savedNow: saved, errors: missing, done: true }));
            return;
          }
          response.writeHead(200, headers).end(renderSecretsPage(env, pathName, '', { finished: true }));
          finish();
          resolve({ saved: [...savedKeys] });
          return;
        }
        const message = saved.length ? '' : 'Nothing new to save. Type a value first, or press Done when you are finished.';
        response.writeHead(200, headers).end(renderSecretsPage(env, pathName, message, { savedNow: saved, done: true }));
      } catch (error) {
        response.writeHead(400, headers).end('Bad request');
      }
    });
    const timer = setTimeout(() => {
      finish();
      if (savedKeys.length) resolve({ saved: [...savedKeys], timedOut: true });
      else reject(new Error('setup page timed out without a submission'));
    }, timeoutMs);
    // Another setup page (or anything else) on the preferred port: take a free one instead.
    let fallback = port !== 0;
    server.on('error', (error) => {
      if (fallback && error.code === 'EADDRINUSE') {
        fallback = false;
        server.listen(0, '127.0.0.1');
        return;
      }
      clearTimeout(timer);
      reject(error);
    });
    server.once('listening', () => {
      fallback = false;
      const address = server.address();
      const url = `http://127.0.0.1:${address.port}${pathName}`;
      onUrl?.(url);
    });
    server.listen(port, '127.0.0.1');
  });
}

export function openBrowser(url, { runner = run, find = which } = {}) {
  const candidates = inWsl()
    ? [['wslview', [url]], ['cmd.exe', ['/c', 'start', '', url]]]
    : process.platform === 'darwin' ? [['open', [url]]] : [['xdg-open', [url]]];
  for (const [binary, args] of candidates) {
    if (!find(binary)) continue;
    const result = runner(binary, args, { timeout: 5_000 });
    if (result.status === 0) return true;
  }
  return false;
}

// Agent registration ---------------------------------------------------------

export function mcpServerPath(root) {
  return path.join(root, 'packages', 'mcp', 'src', 'server.mjs');
}

/** From WSL: the Windows user folders as Linux paths, or null outside WSL. */
export function windowsProfile({ runner = run, find = which } = {}) {
  if (!find('cmd.exe') || !find('wslpath')) return null;
  const folder = (variable) => {
    const echo = runner('cmd.exe', ['/c', 'echo', `%${variable}%`], { timeout: 8_000 });
    const value = (echo.stdout || '').trim().split(/\r?\n/).pop();
    if (echo.status !== 0 || !value || value.includes('%')) return null;
    const converted = runner('wslpath', ['-u', value]);
    return converted.status === 0 ? converted.stdout.trim() : null;
  };
  const userProfile = folder('USERPROFILE');
  if (!userProfile) return null;
  return { userProfile, appData: folder('APPDATA'), distro: process.env.WSL_DISTRO_NAME || null };
}

function addToMcpJson(file, entry) {
  let config = {};
  try { config = JSON.parse(fs.readFileSync(file, 'utf8')); } catch { config = {}; }
  config.mcpServers = { ...(config.mcpServers || {}), [MCP_NAME]: entry };
  fs.writeFileSync(file, `${JSON.stringify(config, null, 2)}\n`);
}

function installSkills(root, skillsDir) {
  const installed = [];
  for (const name of INSTALLED_SKILLS) {
    const source = path.join(root, '.agents', 'skills', name);
    if (!fs.existsSync(path.join(source, 'SKILL.md'))) continue;
    fs.mkdirSync(skillsDir, { recursive: true });
    fs.cpSync(source, path.join(skillsDir, name), { recursive: true, force: true });
    installed.push(name);
  }
  return installed;
}

/** The console's MCP endpoint for local agents, with the header they send. */
export function localMcpConnection({ token, port = Number(process.env.SMITLINE_CONTROL_PORT || 8095) } = {}) {
  return {
    url: `http://127.0.0.1:${port}/mcp`,
    headers: { Authorization: `Bearer ${token}` },
  };
}

/**
 * In the container, registration cannot touch the host's agent settings, so it returns
 * what to run or paste on the host: the MCP URL and header, one snippet per agent, and
 * where the call skill is. Nothing is written.
 */
export function containerRegistration({
  codeRoot, token, port, container = process.env.SMITLINE_CONTAINER_NAME || 'smitline',
} = {}) {
  const mcp = localMcpConnection({ token, port });
  const authorization = mcp.headers.Authorization;
  const httpEntry = { url: mcp.url, headers: { Authorization: authorization } };
  const skill = INSTALLED_SKILLS[0];
  const skillDir = path.posix.join(String(codeRoot).split(path.sep).join('/'), '.agents', 'skills', skill);
  return {
    managed: true,
    registered: [],
    mcp,
    agents: {
      'claude-code': {
        command: `claude mcp add --transport http --scope user ${MCP_NAME} ${mcp.url} --header "Authorization: ${authorization}"`,
        note: 'Run on this computer, then restart Claude Code.',
      },
      codex: {
        file: '~/.codex/config.toml',
        toml: `[mcp_servers.${MCP_NAME}]\nurl = "${mcp.url}"\nhttp_headers = { Authorization = "${authorization}" }\n`,
        note: 'Add to the file, then restart Codex.',
      },
      cursor: {
        file: '~/.cursor/mcp.json',
        json: { mcpServers: { [MCP_NAME]: httpEntry } },
        note: 'Merge into the file, then restart Cursor.',
      },
      'claude-desktop': {
        file: 'claude_desktop_config.json (Claude Desktop: Settings > Developer > Edit Config)',
        json: { mcpServers: { [MCP_NAME]: { command: 'docker', args: ['exec', '-i', container, 'smitline', 'mcp'] } } },
        note: 'Merge into the file, then quit and reopen Claude Desktop. If it cannot start the server, it may not find docker '
          + '(apps opened from the macOS Dock often lack /usr/local/bin): replace "docker" with the full path that '
          + '`which docker` prints (`where docker` on Windows), such as /usr/local/bin/docker on macOS.',
      },
      other: {
        note: `Other MCP clients: Streamable HTTP at ${mcp.url} with the Authorization header, or stdio with: docker exec -i ${container} smitline mcp`,
      },
    },
    skill: {
      name: skill,
      path: `${skillDir}/SKILL.md`,
      // docker cp copies a folder into an existing folder, but renames it to a missing one,
      // so each command makes the skills folder first. `copy` is for bash and zsh.
      copy: {
        'claude-code': `mkdir -p "$HOME/.claude/skills" && docker cp ${container}:${skillDir} "$HOME/.claude/skills/"`,
        codex: `mkdir -p "$HOME/.codex/skills" && docker cp ${container}:${skillDir} "$HOME/.codex/skills/"`,
      },
      // Windows PowerShell 5.1 does not expand ~, and a trailing \ before a closing quote breaks native arguments.
      copyPowerShell: {
        'claude-code': `New-Item -ItemType Directory -Force "$HOME\\.claude\\skills" | Out-Null; docker cp ${container}:${skillDir} "$HOME\\.claude\\skills"`,
        codex: `New-Item -ItemType Directory -Force "$HOME\\.codex\\skills" | Out-Null; docker cp ${container}:${skillDir} "$HOME\\.codex\\skills"`,
      },
      note: 'The skill teaches an agent to write a good call brief and wait for the result. Run the copy command on this computer: `copy` in bash or zsh, `copyPowerShell` in PowerShell.',
    },
  };
}

/** Plain-text version of containerRegistration, for a person reading the terminal. */
export function formatContainerRegistration(report) {
  const { mcp, agents, skill } = report;
  const indent = (text) => String(text).trimEnd().split('\n').map((line) => `  ${line}`).join('\n');
  return [
    'Smitline runs in its container, so connect your agents on this computer (the host):',
    '',
    `MCP URL:  ${mcp.url}`,
    `Header:   Authorization: ${mcp.headers.Authorization}`,
    '',
    'Claude Code (run in a terminal):',
    indent(agents['claude-code'].command),
    '',
    `Codex (add to ${agents.codex.file}):`,
    indent(agents.codex.toml),
    '',
    `Cursor (merge into ${agents.cursor.file}):`,
    indent(JSON.stringify(agents.cursor.json, null, 2)),
    '',
    `Claude Desktop (merge into ${agents['claude-desktop'].file}):`,
    indent(JSON.stringify(agents['claude-desktop'].json, null, 2)),
    indent(agents['claude-desktop'].note),
    '',
    agents.other.note,
    '',
    `Call skill (${skill.path} in the container); copy it out for Claude Code, then Codex.`,
    'bash or zsh:',
    indent(skill.copy['claude-code']),
    indent(skill.copy.codex),
    'PowerShell:',
    indent(skill.copyPowerShell['claude-code']),
    indent(skill.copyPowerShell.codex),
    '',
    'Restart each agent after adding Smitline. Keep the token private: it lets a program on this computer place calls.',
    '',
  ].join('\n');
}

export function registerAgents(root, {
  agents, runner = run, home = os.homedir(), find = which,
  windows = inWsl() ? windowsProfile({ runner, find }) : null,
  http = null,
} = {}) {
  const server = mcpServerPath(root);
  const results = [];
  const wanted = agents ? new Set(agents) : null;
  const want = (id) => !wanted || wanted.has(id);
  if (want('claude-code') && find('claude')) {
    runner('claude', ['mcp', 'remove', '--scope', 'user', MCP_NAME]);
    const added = runner('claude', ['mcp', 'add', '--scope', 'user', MCP_NAME, '--', process.execPath, server]);
    results.push({ id: 'claude-code', registered: added.status === 0, detail: added.status === 0 ? 'restart Claude Code to load it' : (added.stderr || '').trim().slice(0, 200) });
  }
  if (want('codex') && find('codex')) {
    runner('codex', ['mcp', 'remove', MCP_NAME]);
    const added = runner('codex', ['mcp', 'add', MCP_NAME, '--', process.execPath, server]);
    results.push({ id: 'codex', registered: added.status === 0, detail: added.status === 0 ? 'restart Codex to load it' : (added.stderr || '').trim().slice(0, 200) });
  }
  const cursorDir = path.join(home, '.cursor');
  if (want('cursor') && fs.existsSync(cursorDir)) {
    const file = path.join(cursorDir, 'mcp.json');
    let config = {};
    try { config = JSON.parse(fs.readFileSync(file, 'utf8')); } catch { config = {}; }
    config.mcpServers = { ...(config.mcpServers || {}), [MCP_NAME]: { command: process.execPath, args: [server] } };
    fs.writeFileSync(file, `${JSON.stringify(config, null, 2)}\n`);
    results.push({ id: 'cursor', registered: true, detail: 'restart Cursor to load it' });
  }
  // Windows apps start the server inside this WSL distribution.
  const bridged = {
    command: 'wsl.exe',
    args: [...(windows?.distro ? ['-d', windows.distro] : []), '--exec', process.execPath, server],
  };
  if (windows) {
    const desktopDir = windows.appData && path.join(windows.appData, 'Claude');
    if (want('claude-desktop') && desktopDir && fs.existsSync(desktopDir)) {
      addToMcpJson(path.join(desktopDir, 'claude_desktop_config.json'), bridged);
      results.push({ id: 'claude-desktop', registered: true, detail: 'quit and reopen Claude Desktop to load it' });
    }
    const windowsCursor = path.join(windows.userProfile, '.cursor');
    if (want('cursor') && fs.existsSync(windowsCursor)) {
      addToMcpJson(path.join(windowsCursor, 'mcp.json'), bridged);
      results.push({ id: 'cursor-windows', registered: true, detail: 'restart Cursor on Windows to load it' });
    }
    if (want('claude-code') && runner('cmd.exe', ['/c', 'where', 'claude'], { timeout: 8_000 }).status === 0) {
      runner('cmd.exe', ['/c', 'claude', 'mcp', 'remove', '--scope', 'user', MCP_NAME], { timeout: 20_000 });
      const added = runner('cmd.exe', ['/c', 'claude', 'mcp', 'add', '--scope', 'user', MCP_NAME, '--', bridged.command, ...bridged.args], { timeout: 20_000 });
      results.push({ id: 'claude-code-windows', registered: added.status === 0, detail: added.status === 0 ? 'restart Claude Code on Windows to load it' : (added.stderr || '').trim().slice(0, 200) });
    }
  }
  // Skills teach an agent to write a good brief and wait for the result.
  const skills = [];
  for (const [id, dir] of [['claude', path.join(home, '.claude')], ['codex', path.join(home, '.codex')],
    ...(windows ? [['claude-windows', path.join(windows.userProfile, '.claude')]] : [])]) {
    if (fs.existsSync(dir)) skills.push({ id, installed: installSkills(root, path.join(dir, 'skills')) });
  }
  const manual = {
    command: process.execPath,
    args: [server],
    ...(windows ? { windows: bridged } : {}),
    ...(http ? { http } : {}),
    note: `Other MCP clients (OpenClaw, Hermes, and others): add a stdio server with this command${http ? `, or, while the console runs, connect over Streamable HTTP to ${http.url} with the Authorization header under http` : ''}. Cloud agents use the remote connector; see docs/agents.md.`,
  };
  return { results, skills, manual };
}
