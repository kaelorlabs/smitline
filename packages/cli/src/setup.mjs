// Setup that an agent can drive: status as JSON, a local page for keys, and
// registration with the coding agents installed on this machine. Secrets never
// pass through the agent: they are typed into a loopback page and written to
// the ignored .env file with 0600 permissions.
import { spawnSync } from 'node:child_process';
import crypto from 'node:crypto';
import fs from 'node:fs';
import http from 'node:http';
import net from 'node:net';
import os from 'node:os';
import path from 'node:path';

export const SECRET_KEYS = Object.freeze([
  'OPENAI_API_KEY', 'TWILIO_ACCOUNT_SID', 'TWILIO_AUTH_TOKEN', 'TAVILY_API_KEY', 'COLLEAGUE_CONNECTOR_PASSPHRASE',
]);
export const SETTING_KEYS = Object.freeze([
  'COLLEAGUE_OWNER_NAME', 'COLLEAGUE_OWNER_PHONE', 'COLLEAGUE_VOICE', 'COLLEAGUE_CALLER_ID',
  'TWILIO_FROM_NUMBER', 'COLLEAGUE_ACCEPT_INBOUND', 'COLLEAGUE_ALLOWED_CALLING_CODES',
  'COLLEAGUE_PUBLIC_URL', 'COLLEAGUE_NOTIFY_WEBHOOK', 'COLLEAGUE_RECORD_CALLS', 'COLLEAGUE_CONNECTOR_URL',
  'COLLEAGUE_EXTRA_VOICES', 'COLLEAGUE_MAX_INBOUND',
]);
export const CONNECTOR_PASSPHRASE_MIN = 12;
export const GPT_LIVE_VOICES = Object.freeze([
  'marin', 'vesper', 'quartz', 'ripple', 'willow', 'stone', 'gleam', 'meridian', 'bossa',
  'tempo', 'beacon', 'delta', 'cinder',
]);
const E164 = /^\+[1-9][0-9]{7,14}$/;
const VOICE_NAME = /^[a-z][a-z0-9_-]{1,31}$/;
const MCP_NAME = 'colleague-ai';
// Skills a local agent can use without opening this repository.
const INSTALLED_SKILLS = ['call-with-colleague-ai', 'join-colleague-ai-meeting'];

/** GPT-Live voices plus any listed in COLLEAGUE_EXTRA_VOICES (new voices OpenAI adds). */
export function availableVoices(env = process.env) {
  const extra = String(env.COLLEAGUE_EXTRA_VOICES || '').split(',').map((name) => name.trim())
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

/** Merge values into .env, replacing existing assignments and keeping everything else. */
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
    if (written.has(match[1])) return [];
    written.add(match[1]);
    remaining.delete(match[1]);
    return [`${match[1]}=${updates[match[1]]}`];
  });
  while (lines.length && lines.at(-1) === '') lines.pop();
  for (const [key, value] of remaining) lines.push(`${key}=${value}`);
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
    throw new Error('COLLEAGUE_CONNECTOR_URL must be an https URL such as https://colleague.example.com');
  }
  const loopback = allowLoopback && url.protocol === 'http:' && ['127.0.0.1', 'localhost'].includes(url.hostname);
  if (url.protocol !== 'https:' && !loopback) {
    throw new Error(`COLLEAGUE_CONNECTOR_URL must be an https URL${allowLoopback ? ' (plain http is allowed only for 127.0.0.1 and localhost)' : ''}`);
  }
  if (url.pathname !== '/' || url.search || url.hash || url.username || url.password) {
    throw new Error('COLLEAGUE_CONNECTOR_URL must be only the origin, such as https://colleague.example.com, with no path');
  }
  return url.origin;
}

export function validateSetting(key, value, { env = process.env } = {}) {
  if (!SETTING_KEYS.includes(key)) {
    if (SECRET_KEYS.includes(key)) throw new Error(`${key} is a secret: enter it on the page from "colleague setup secrets"`);
    throw new Error(`unknown setting ${key}; allowed: ${SETTING_KEYS.join(', ')}`);
  }
  const text = String(value ?? '').trim();
  if (/[\r\n]/.test(text)) throw new Error(`${key} must be one line`);
  if (['COLLEAGUE_OWNER_PHONE', 'COLLEAGUE_CALLER_ID', 'TWILIO_FROM_NUMBER'].includes(key)) {
    const compact = text.replace(/[\s().-]/g, '');
    if (!E164.test(compact)) throw new Error(`${key} must be an E.164 number such as +14155550142`);
    return compact;
  }
  if (key === 'COLLEAGUE_VOICE' && !availableVoices(env).includes(text)) {
    throw new Error(`COLLEAGUE_VOICE must be one of: ${availableVoices(env).join(', ')}`);
  }
  if (key === 'COLLEAGUE_EXTRA_VOICES') {
    const names = text.split(',').map((name) => name.trim()).filter(Boolean);
    if (names.some((name) => !VOICE_NAME.test(name))) throw new Error('COLLEAGUE_EXTRA_VOICES must be voice names separated by commas');
    return names.join(',');
  }
  if (key === 'COLLEAGUE_MAX_INBOUND' && !/^[0-9]{1,2}$/.test(text)) {
    throw new Error('COLLEAGUE_MAX_INBOUND must be a number of simultaneous incoming calls, such as 2');
  }
  if (['COLLEAGUE_ACCEPT_INBOUND', 'COLLEAGUE_RECORD_CALLS'].includes(key) && !['0', '1'].includes(text)) {
    throw new Error(`${key} must be 0 or 1`);
  }
  if (['COLLEAGUE_PUBLIC_URL', 'COLLEAGUE_NOTIFY_WEBHOOK'].includes(key) && !/^https:\/\/[^\s/]+/.test(text)) {
    throw new Error(`${key} must be an https URL`);
  }
  if (key === 'COLLEAGUE_CONNECTOR_URL') return connectorOrigin(text);
  if (key === 'COLLEAGUE_OWNER_NAME' && (!text || text.length > 120)) {
    throw new Error('COLLEAGUE_OWNER_NAME must be 1 to 120 characters');
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
    if (response.status === 401) return { ok: false, detail: 'OpenAI rejected the key', fix: 'colleague setup secrets' };
    if (response.status === 404 || response.status === 403) {
      return { ok: false, detail: 'the key cannot use gpt-live-1; GPT-Live needs a paid API tier', fix: 'Add billing at platform.openai.com, then rerun colleague setup status' };
    }
    return { ok: false, detail: `OpenAI answered ${response.status}` };
  } catch {
    return { ok: null, detail: 'could not reach OpenAI to verify the key' };
  }
}

async function verifyTwilio(env, fetchImpl) {
  const auth = `Basic ${Buffer.from(`${env.TWILIO_ACCOUNT_SID}:${env.TWILIO_AUTH_TOKEN}`).toString('base64')}`;
  const base = `https://api.twilio.com/2010-04-01/Accounts/${encodeURIComponent(env.TWILIO_ACCOUNT_SID)}`;
  try {
    const account = await fetchImpl(`${base}.json`, { headers: { Authorization: auth }, signal: AbortSignal.timeout(10_000) });
    if (account.status === 401 || account.status === 404) return { ok: false, detail: 'Twilio rejected the account SID or auth token', fix: 'colleague setup secrets' };
    if (!account.ok) return { ok: null, detail: `Twilio answered ${account.status}` };
    const body = await account.json();
    const [numbers, verified] = await Promise.all([
      fetchImpl(`${base}/IncomingPhoneNumbers.json?PageSize=50`, { headers: { Authorization: auth } }).then((r) => r.json()).catch(() => ({})),
      fetchImpl(`${base}/OutgoingCallerIds.json?PageSize=50`, { headers: { Authorization: auth } }).then((r) => r.json()).catch(() => ({})),
    ]);
    return {
      ok: true,
      detail: `account ${body.status || 'active'}${body.type === 'Trial' ? ' (trial: calls only verified numbers)' : ''}`,
      trial: body.type === 'Trial',
      numbers: (numbers.incoming_phone_numbers || []).map((item) => item.phone_number),
      verified: (verified.outgoing_caller_ids || []).map((item) => item.phone_number),
    };
  } catch {
    return { ok: null, detail: 'could not reach Twilio to verify the account' };
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

export async function setupStatus({ root, env: overrides, fetchImpl = globalThis.fetch, verify = true, runner = run, find = which } = {}) {
  // The daemon reads the process environment first, then .env; mirror that here.
  const relevant = ([key, value]) => /^(OPENAI_|TWILIO_|COLLEAGUE_)/.test(key) && present(value);
  const env = { ...readEnv(root), ...Object.fromEntries(Object.entries(overrides || process.env).filter(relevant)) };
  const checks = [];
  const major = Number(process.versions.node.split('.')[0]);
  checks.push(check('node', 'Node.js 22 or newer', major >= 22, { detail: process.version, fix: 'Install Node.js 22 or newer' }));
  if (process.platform === 'win32') {
    checks.push(check('platform', 'Operating system', false, { detail: 'Windows', fix: 'Install WSL2 with Ubuntu and clone Colleague AI inside the Linux home directory' }));
  } else {
    checks.push(check('platform', 'Operating system', true, { detail: inWsl() ? 'Linux on WSL2' : process.platform }));
    if (inWsl() && root.startsWith('/mnt/')) {
      checks.push(check('checkout', 'Checkout inside Linux', false, { required: false, detail: 'the checkout is under /mnt, where Docker may not see files', fix: 'Clone inside the Linux home directory' }));
    }
  }
  const launcher = path.join(root, 'start-runtime-daemon.sh');
  const crlf = fs.existsSync(launcher) && fs.readFileSync(launcher, 'utf8').includes('\r\n');
  checks.push(check('line_endings', 'Unix line endings', !crlf, { fix: 'Clone again inside WSL or Linux' }));
  const docker = runner('docker', ['info'], { timeout: 12_000 });
  checks.push(check('docker', 'Docker is running', docker.status === 0, {
    group: 'meetings', required: false, detail: 'needed to join meetings and for the laptop tunnel',
    fix: 'Start Docker, or install it inside WSL', ask: 'Please start Docker (or Docker Desktop) and tell me when it is running.',
  }));
  checks.push(check('dependencies', 'Node dependencies installed', fs.existsSync(path.join(root, 'node_modules', 'mammoth')), { fix: 'npm install' }));
  // The daemon creates a Python virtual environment on first start; Ubuntu ships without venv.
  const venvReady = present(process.env.COLLEAGUE_PYTHON) || fs.existsSync(path.join(root, '.venv', 'bin', 'python'));
  const python = venvReady ? { status: 0 } : runner('python3', ['-c', 'import sys, ensurepip, venv; sys.exit(0 if sys.version_info >= (3, 10) else 3)']);
  const pythonDetail = venvReady ? 'environment ready'
    : python.status === 0 ? 'the first start creates the environment'
      : python.status === 3 ? 'Python is older than 3.10'
        : python.error ? 'python3 is not installed' : 'python3 cannot create a virtual environment (python3-venv is missing)';
  checks.push(check('python', 'Python 3.10 or newer with venv', python.status === 0, {
    detail: pythonDetail,
    fix: process.platform === 'darwin' ? 'brew install python@3.12' : 'sudo apt install -y python3 python3-venv',
    ask: process.platform === 'darwin' ? undefined : 'Please run this once in a terminal; it needs your password: sudo apt install -y python3 python3-venv',
  }));

  let openai = { ok: present(env.OPENAI_API_KEY) };
  if (openai.ok && verify) openai = await verifyOpenAi(env.OPENAI_API_KEY, fetchImpl);
  checks.push(check('openai_key', 'OpenAI API key with GPT-Live access', openai.ok, {
    detail: openai.detail || (present(env.OPENAI_API_KEY) ? 'saved' : 'missing'),
    ask: 'Please enter your OpenAI API key on the setup page I opened in your browser. Do not paste it into this chat.',
    fix: openai.fix || 'colleague setup secrets',
  }));
  checks.push(check('owner_name', 'Name to call on behalf of', present(env.COLLEAGUE_OWNER_NAME), {
    detail: present(env.COLLEAGUE_OWNER_NAME) ? env.COLLEAGUE_OWNER_NAME : 'missing',
    ask: 'What name should I say I am calling on behalf of?',
    fix: 'colleague setup set COLLEAGUE_OWNER_NAME "<name>"',
  }));

  const twilioSaved = present(env.TWILIO_ACCOUNT_SID) && present(env.TWILIO_AUTH_TOKEN);
  let twilio = { ok: twilioSaved };
  if (twilioSaved && verify) twilio = await verifyTwilio(env, fetchImpl);
  checks.push(check('twilio', 'Twilio account', twilio.ok, {
    group: 'phone', required: false, detail: twilio.detail || (twilioSaved ? 'saved' : 'not set up'),
    ask: 'Do you want phone calls too? If yes, enter your Twilio Account SID and Auth Token on the setup page.',
    fix: 'colleague setup secrets',
  }));
  // Outgoing calls show COLLEAGUE_CALLER_ID (a verified number) or else TWILIO_FROM_NUMBER.
  // Incoming calls can only ring TWILIO_FROM_NUMBER, a number bought in Twilio.
  const from = present(env.COLLEAGUE_CALLER_ID) ? env.COLLEAGUE_CALLER_ID : env.TWILIO_FROM_NUMBER;
  let callerOk = present(from) && E164.test(from);
  let callerDetail = !present(from) ? 'no number chosen yet' : (callerOk ? from : `${from} is not an E.164 number`);
  let callerAsk = 'Should calls come from your Twilio number, or show your own mobile number (verified in Twilio)?';
  let callerFix = 'colleague setup set TWILIO_FROM_NUMBER +1... (or COLLEAGUE_CALLER_ID for a verified mobile)';
  let suggest = null;
  if (Array.isArray(twilio.numbers)) {
    const verified = twilio.verified || [];
    if (callerOk && !twilio.numbers.includes(from) && !verified.includes(from)) {
      callerOk = false;
      callerDetail = `${from} is not a number or verified caller ID in this Twilio account`;
    } else if (!present(from) && twilio.numbers.length === 1) {
      // One number: nothing to ask; the agent sets it.
      suggest = { key: 'TWILIO_FROM_NUMBER', value: twilio.numbers[0] };
      callerDetail = `this Twilio account has one number, ${twilio.numbers[0]}`;
      callerAsk = undefined;
      callerFix = `colleague setup set TWILIO_FROM_NUMBER ${twilio.numbers[0]}`;
    } else if (!present(from) && twilio.numbers.length > 1) {
      callerDetail = `Twilio numbers: ${twilio.numbers.join(', ')}`;
      callerAsk = `Which number should calls come from: ${twilio.numbers.join(', ')}?`;
    } else if (!present(from) && verified.length) {
      suggest = { key: 'COLLEAGUE_CALLER_ID', value: verified[0] };
      callerDetail = `no Twilio number yet; ${verified[0]} is verified and can be shown on outgoing calls`;
      callerAsk = undefined;
      callerFix = `colleague setup set COLLEAGUE_CALLER_ID ${verified[0]}`;
    }
  }
  if (callerOk && !present(env.TWILIO_FROM_NUMBER)) {
    callerDetail += '; incoming calls also need TWILIO_FROM_NUMBER, a number bought in Twilio';
  }
  const callerCheck = check('caller_id', 'Number calls come from', callerOk, {
    group: 'phone', required: false, detail: callerDetail, ask: callerAsk, fix: callerFix,
  });
  if (suggest && !callerOk) callerCheck.suggest = suggest;
  checks.push(callerCheck);
  checks.push(check('owner_phone', 'Your phone number (for the test call and transfers)', present(env.COLLEAGUE_OWNER_PHONE), {
    group: 'phone', required: false, detail: env.COLLEAGUE_OWNER_PHONE || 'missing',
    ask: 'What is your phone number? I will call it once to prove setup works.',
    fix: 'colleague setup set COLLEAGUE_OWNER_PHONE +1...',
  }));
  const reachable = present(env.COLLEAGUE_PUBLIC_URL) || Boolean(find('cloudflared')) || docker.status === 0;
  checks.push(check('public_url', 'Twilio can reach this computer', reachable, {
    group: 'phone', required: false,
    detail: present(env.COLLEAGUE_PUBLIC_URL) ? env.COLLEAGUE_PUBLIC_URL : (reachable ? 'Cloudflare quick tunnel on first call' : 'no tunnel available'),
    fix: 'Start Docker or install cloudflared, or set COLLEAGUE_PUBLIC_URL on a server',
  }));

  const agents = registeredAgents(root, { runner, find });
  checks.push(check('agents', 'Registered with a local agent', agents.some((a) => a.registered), {
    group: 'agents', required: false,
    detail: agents.length ? agents.map((a) => `${a.id}${a.registered ? ' (registered)' : ''}`).join(', ') : 'no supported local agent CLI found',
    fix: 'colleague setup register',
  }));
  const daemon = await portOpen(Number(process.env.COLLEAGUE_DAEMON_PORT || 8765));
  checks.push(check('daemon', 'Runtime daemon', true, { required: false, detail: daemon ? 'running' : 'starts automatically on first use' }));

  const coreReady = checks.filter((c) => c.required).every((c) => c.ok === true);
  const phoneReady = coreReady && checks.filter((c) => c.group === 'phone' && c.id !== 'owner_phone').every((c) => c.ok === true);
  const meetingsReady = coreReady && checks.find((c) => c.id === 'docker').ok === true;
  // The first call rings the owner's phone; without phone setup it is a meeting instead.
  const firstCallReady = phoneReady && checks.find((c) => c.id === 'owner_phone').ok === true;
  // Phone steps become next steps once the user has started on phone calls; until then
  // only the question "do you want phone calls?" is asked, and the rest is optional.
  const wantsPhone = twilioSaved || ['TWILIO_FROM_NUMBER', 'COLLEAGUE_CALLER_ID', 'COLLEAGUE_OWNER_PHONE'].some((key) => present(env[key]));
  const step = (c) => ({ id: c.id, ask: c.ask, fix: c.fix, ...(c.suggest ? { suggest: c.suggest } : {}) });
  const pending = checks.filter((c) => c.ok !== true);
  const isNext = (c) => c.required || c.group === 'agents' || (c.group === 'phone' && (wantsPhone || c.id === 'twilio'));
  const next = pending.filter(isNext).map(step);
  const optional = pending.filter((c) => !isNext(c) && ['phone', 'meetings'].includes(c.group)).map(step);
  return {
    ready: coreReady,
    phoneReady,
    meetingsReady,
    firstCallReady,
    voice: env.COLLEAGUE_VOICE || 'marin',
    voices: availableVoices(env),
    checks,
    next,
    optional,
  };
}

// Secrets page -------------------------------------------------------------

const FIELDS = [
  { key: 'OPENAI_API_KEY', label: 'OpenAI API key', group: 'Required', secret: true, hint: 'From platform.openai.com. GPT-Live needs a paid tier.' },
  { key: 'COLLEAGUE_OWNER_NAME', label: 'Your name', group: 'Required', hint: 'Spoken in the AI disclosure: calling on behalf of this name.' },
  { key: 'TWILIO_ACCOUNT_SID', label: 'Twilio Account SID', group: 'Phone calls (optional)', secret: true },
  { key: 'TWILIO_AUTH_TOKEN', label: 'Twilio Auth Token', group: 'Phone calls (optional)', secret: true },
  { key: 'TWILIO_FROM_NUMBER', label: 'Twilio phone number', group: 'Phone calls (optional)', hint: 'E.164, such as +14155550142.' },
  { key: 'COLLEAGUE_OWNER_PHONE', label: 'Your phone number', group: 'Phone calls (optional)', hint: 'For the test call and for taking over calls.' },
  { key: 'COLLEAGUE_CONNECTOR_URL', label: 'Connector address', group: 'Remote connector (server mode)', hint: 'The https address of this server, such as https://colleague.example.com. Only for cloud agents; see docs/agents.md.' },
  { key: 'COLLEAGUE_CONNECTOR_PASSPHRASE', label: 'Owner passphrase', group: 'Remote connector (server mode)', secret: true, spaces: true, minLength: CONNECTOR_PASSPHRASE_MIN, hint: 'At least 12 characters. You type it to approve each app that connects.' },
];

export function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

export const PAGE_STYLE = `:root{color-scheme:light dark;--bg:#f4f6f6;--card:#fff;--ink:#111719;--muted:#56636a;--line:#d9dfe2;--accent:#0f766e}
@media (prefers-color-scheme:dark){:root{--bg:#0e1315;--card:#151c1f;--ink:#e4eaec;--muted:#97a5ab;--line:#28343a;--accent:#3fbfae;--on-accent:#0e1315}}
body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.5 system-ui,sans-serif;padding:32px 16px}
main{max-width:560px;margin:0 auto;display:grid;gap:18px}h1{font-size:1.5rem;margin:0}
p{margin:0;color:var(--muted)}fieldset{border:1px solid var(--line);border-radius:10px;background:var(--card);padding:16px;display:grid;gap:14px}
legend{font-weight:600;padding:0 6px}label{display:grid;gap:4px}small{color:var(--muted)}
input{font:inherit;padding:9px 11px;border:1px solid var(--line);border-radius:7px;background:var(--bg);color:var(--ink)}
input:focus-visible,button:focus-visible{outline:2px solid var(--accent);outline-offset:1px}
button{font:inherit;font-weight:600;padding:10px 16px;border:0;border-radius:8px;background:var(--accent);color:var(--on-accent,#fff);cursor:pointer;justify-self:start}
.note{border-left:3px solid var(--accent);padding:8px 12px;background:var(--card)}`;

export function renderSecretsPage(saved, action, message = '') {
  const groups = [...new Set(FIELDS.map((f) => f.group))];
  const sections = groups.map((group) => {
    const rows = FIELDS.filter((f) => f.group === group).map((f) => {
      const isSaved = present(saved[f.key]);
      const placeholder = isSaved ? (f.secret ? 'Saved. Leave empty to keep it.' : `Saved: ${saved[f.key]}`) : '';
      return `<label><span>${escapeHtml(f.label)}</span>
<input name="${f.key}" type="${f.secret ? 'password' : 'text'}" autocomplete="off" spellcheck="false" placeholder="${escapeHtml(placeholder)}">
${f.hint ? `<small>${escapeHtml(f.hint)}</small>` : ''}</label>`;
    }).join('\n');
    return `<fieldset><legend>${escapeHtml(group)}</legend>${rows}</fieldset>`;
  }).join('\n');
  return `<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Colleague AI setup</title><style>
${PAGE_STYLE}</style></head>
<body><main><h1>Colleague AI setup</h1>
<p>Keys stay on this computer, in the project's ignored <code>.env</code> file. Your agent never sees them.</p>
${message ? `<p class="note">${escapeHtml(message)}</p>` : ''}
<form method="post" action="${escapeHtml(action)}">${sections}<button type="submit">Save</button></form></main></body></html>`;
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
    const value = String(form.get(field.key) || '').trim();
    if (!value) continue;
    if (field.secret && (field.spaces ? /[\r\n]/ : /\s/).test(value)) {
      errors.push(`${field.label} must ${field.spaces ? 'be one line' : 'not contain spaces'}`);
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

/** Serve the one-time secrets page until it is submitted; resolves with the saved key names. */
export function serveSecretsPage({ root, port = 0, timeoutMs = 15 * 60_000, onUrl } = {}) {
  const token = crypto.randomBytes(18).toString('base64url');
  const pathName = `/setup/${token}`;
  return new Promise((resolve, reject) => {
    const server = http.createServer(async (request, response) => {
      const url = new URL(request.url, 'http://127.0.0.1');
      const headers = { 'Content-Type': 'text/html; charset=utf-8', 'Cache-Control': 'no-store', 'X-Frame-Options': 'DENY', 'Referrer-Policy': 'no-referrer' };
      if (url.pathname !== pathName) {
        response.writeHead(404, headers).end('Not found');
        return;
      }
      if (request.method === 'GET') {
        response.writeHead(200, headers).end(renderSecretsPage(readEnv(root), pathName));
        return;
      }
      if (request.method !== 'POST') {
        response.writeHead(405, headers).end();
        return;
      }
      try {
        const form = new URLSearchParams(await readBody(request));
        const { updates, errors } = sanitizeSubmission(form);
        if (errors.length) {
          response.writeHead(422, headers).end(renderSecretsPage(readEnv(root), pathName, errors.join(' ')));
          return;
        }
        const saved = Object.keys(updates).length ? writeEnv(root, updates) : [];
        response.writeHead(200, headers).end(renderSecretsPage(readEnv(root), pathName,
          saved.length ? 'Saved. You can close this page and return to your agent.' : 'Nothing changed.'));
        clearTimeout(timer);
        server.close();
        resolve({ saved });
      } catch (error) {
        response.writeHead(400, headers).end('Bad request');
      }
    });
    const timer = setTimeout(() => { server.close(); reject(new Error('setup page timed out without a submission')); }, timeoutMs);
    server.on('error', reject);
    server.listen(port, '127.0.0.1', () => {
      const address = server.address();
      const url = `http://127.0.0.1:${address.port}${pathName}`;
      onUrl?.(url);
    });
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

export function registerAgents(root, {
  agents, runner = run, home = os.homedir(), find = which,
  windows = inWsl() ? windowsProfile({ runner, find }) : null,
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
    results.push({ id: 'codex', registered: added.status === 0, detail: added.status === 0 ? 'for exact meeting continuity also run scripts/install-codex-integration.sh' : (added.stderr || '').trim().slice(0, 200) });
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
    note: 'Other MCP clients (OpenClaw, Hermes, and others): add a stdio server with this command. Cloud agents use the remote connector; see docs/agents.md.',
  };
  return { results, skills, manual };
}
