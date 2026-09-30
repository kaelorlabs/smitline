#!/usr/bin/env node
import fs from 'node:fs/promises';
import { closeSync, mkdirSync, openSync, readFileSync, realpathSync } from 'node:fs';
import { spawn } from 'node:child_process';
import net from 'node:net';
import path from 'node:path';
import process from 'node:process';
import { fileURLToPath } from 'node:url';
import {
  Colleague,
  EXIT,
  ValidationError,
  StartupError,
  RuntimeError,
  FinalizationError,
  InterruptError,
  MANAGED_NOT_RUNNING,
  isManaged,
} from '../../sdk-typescript/src/index.mjs';
import {
  SECRETS_PAGE_MINUTES,
  availableVoices,
  containerRegistration,
  createSignalWireTrunk,
  formatContainerRegistration,
  localMcpConnection,
  openBrowser,
  readEnv,
  registerAgents,
  serveSecretsPage,
  setupStatus,
  validateSetting,
  writeEnv,
} from './setup.mjs';
import { openConnectorStore } from '../../mcp/src/connector-store.mjs';
import { readOrCreateLocalMcpToken } from '../../mcp/src/local-token.mjs';

const interruptState = { requested: false, handler: null };
// The checkout (/app in the container): scripts, skills, and the MCP server live here.
const DEFAULT_COLLEAGUE_ROOT = path.resolve(fileURLToPath(new URL('../../../', import.meta.url)));
const CODE_ROOT = DEFAULT_COLLEAGUE_ROOT;

/** The release: COLLEAGUE_VERSION (set in the image), else the checkout's package.json. */
function colleagueVersion() {
  if (process.env.COLLEAGUE_VERSION) return process.env.COLLEAGUE_VERSION;
  try {
    return JSON.parse(readFileSync(path.join(CODE_ROOT, 'package.json'), 'utf8')).version || 'unknown';
  } catch {
    return 'unknown';
  }
}

/** The data root (.env, .colleague/): --root, else COLLEAGUE_ROOT, else the checkout. */
function dataRoot(args = {}) {
  return path.resolve(args.root && args.root !== true ? String(args.root) : process.env.COLLEAGUE_ROOT || DEFAULT_COLLEAGUE_ROOT);
}

function requestInterrupt() {
  interruptState.requested = true;
  interruptState.handler?.();
}

const USAGE = `Usage:
  smitline call --to <+E.164> --objective <text> [--on-behalf-of <name>] [--context <text>]
               [--agree <a; b>] [--never-share <a; b>] [--success <text>] [--voice <name>]
               [--language <tag>] [--max-minutes <n>] [--rehearsal] [--webhook <url>]
               [--questions <a; b>] [--tone <text>] [--context-file <json or text>]
               [--check] [--wait]
  smitline call --meeting <url> --objective <text> [...same options] [--wait]
  smitline call --channel meeting --to <url> --objective <text> [...same options] [--wait]
  smitline call --brief <json> | --brief-file <path> [--check] [--wait]
  smitline calls list [--limit <n>]
  smitline calls get|wait|end|transfer --call-id <id> [--timeout <seconds>]
  smitline calls instruct --call-id <id> --text <guidance> [--silent]
  smitline profile show
  smitline profile set [--about <text>] [--style <text>] [--boundaries <a; b>]
  smitline profile person --name <name> [--relationship <text>] [--phone <+E.164>] [--notes <text>] [--remove]
  smitline voices
  smitline setup status [--json] [--no-verify]
  smitline setup secrets [--no-open] [--wait]
  smitline setup set <KEY> <value>
  smitline setup start
  smitline setup stop [--force]
  smitline setup register [--agents claude-code,codex,cursor,claude-desktop] [--json]
  smitline setup voice [--set <name>] [--preview <name>]
  smitline setup sip-trunk
  smitline setup call-me [--wait]
  smitline connector status
  smitline connector revoke --all | --client <id>
  smitline mcp                    (MCP server on stdin/stdout, for Claude Desktop and other stdio clients)
  smitline version | --version
`;

function parseArgs(argv) {
  const args = { _: [] };
  for (let i = 0; i < argv.length; i += 1) {
    const token = argv[i];
    if (token === '--') {
      args._.push(...argv.slice(i + 1));
      break;
    }
    if (token.startsWith('--')) {
      const key = token.slice(2);
      const next = argv[i + 1];
      if (next !== undefined && !next.startsWith('--')) {
        args[key] = next;
        i += 1;
      } else {
        args[key] = true;
      }
    } else {
      args._.push(token);
    }
  }
  return args;
}

// A next step for errors a person can act on; agents read the exit code.
function hintFor(error) {
  const message = error instanceof Error ? error.message : String(error);
  if (error?.code === 'daemon_unavailable') {
    return isManaged() ? 'Restart the container: docker restart smitline' : 'Smitline\'s background service did not answer. Check the setup with: smitline setup status';
  }
  if (error?.code === 'not_configured') return 'See what is missing with: smitline setup status';
  if (error?.code === 'not_found' && /call/i.test(message)) return 'List recent calls with: smitline calls list';
  if (/^unknown (command|\w+ command)/.test(message)) return 'See all commands with: smitline help';
  return '';
}

function fail(error, code) {
  const message = error instanceof Error ? error.message : String(error);
  const hint = hintFor(error);
  const prefix = error instanceof InterruptError ? '' : 'Error: ';
  process.stderr.write(`${prefix}${message}\n${hint ? `Hint: ${hint}\n` : ''}`);
  process.exit(code);
}

function exitForError(error) {
  if (error instanceof ValidationError) fail(error, EXIT.validation);
  if (error instanceof StartupError) fail(error, EXIT.startup);
  if (error instanceof InterruptError) fail(error, EXIT.interrupt);
  if (error instanceof FinalizationError) fail(error, EXIT.finalization);
  if (error instanceof RuntimeError) fail(error, EXIT.runtime);
  fail(error, EXIT.runtime);
}

function progress(line) {
  process.stderr.write(`${line}\n`);
}

function colleagueFromArgs(args) {
  const root = dataRoot(args);
  return {
    root,
    client: new Colleague({
      root,
      codeRoot: CODE_ROOT,
      host: args.host || process.env.COLLEAGUE_DAEMON_HOST || '127.0.0.1',
      port: Number(args.port || process.env.COLLEAGUE_DAEMON_PORT || 8765),
    }),
  };
}


function splitList(value) {
  if (value === undefined || value === true) return undefined;
  return String(value).split(';').map((item) => item.trim()).filter(Boolean);
}

async function contextFromArgs(args, text) {
  // --context-file takes a JSON object (summary, facts, decisions, openQuestions, details) or plain text.
  const file = text(args['context-file']);
  if (!file) return text(args.context);
  const content = await fs.readFile(path.resolve(file), 'utf8');
  try {
    const parsed = JSON.parse(content);
    if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) return parsed;
  } catch { /* plain text */ }
  return content.trim();
}

async function briefFromArgs(args, root) {
  if (args.brief || args['brief-file']) {
    const text = args.brief ? String(args.brief) : await fs.readFile(path.resolve(String(args['brief-file'])), 'utf8');
    try {
      return JSON.parse(text);
    } catch {
      throw new ValidationError('the brief must be JSON');
    }
  }
  const env = readEnv(root);
  // A flag given without a value parses as true; treat it as absent for text fields.
  const text = (value) => (value === undefined || value === true ? undefined : String(value));
  const to = text(args.meeting) || text(args.to);
  // A meeting is joined with --meeting <url>, --channel meeting, or a --to that is an invite URL.
  const channel = text(args.channel) || (text(args.meeting) || /^https?:\/\//i.test(to || '') ? 'meeting' : 'phone');
  if (!['phone', 'meeting'].includes(channel)) throw new ValidationError('--channel must be phone or meeting');
  const brief = {
    channel,
    to,
    onBehalfOf: text(args['on-behalf-of']) || process.env.COLLEAGUE_OWNER_NAME || env.COLLEAGUE_OWNER_NAME,
    objective: text(args.objective),
    context: await contextFromArgs(args, text),
    questions: splitList(args.questions),
    tone: text(args.tone),
    mayAgreeTo: splitList(args.agree),
    mustNotShare: splitList(args['never-share']),
    successCriteria: text(args.success),
    language: text(args.language),
    voice: text(args.voice),
    maxMinutes: text(args['max-minutes']) === undefined ? undefined : Number(args['max-minutes']),
    rehearsal: args.rehearsal === true ? true : undefined,
    notify: text(args.webhook) ? { webhookUrl: text(args.webhook) } : undefined,
  };
  return Object.fromEntries(Object.entries(brief).filter(([, value]) => value !== undefined));
}

function printJson(value) {
  process.stdout.write(`${JSON.stringify(value, null, 2)}\n`);
}

function explainValidation(error) {
  const missing = error?.details?.missing;
  if (Array.isArray(missing) && missing.length) {
    for (const item of missing) progress(`missing ${item.field}: ${item.question}`);
  }
}

const TERMINAL = new Set(['completed', 'failed', 'canceled']);
const OUTCOME_TEXT = {
  achieved: 'Achieved', partial: 'Partly achieved', not_reached: 'Not reached', voicemail: 'Left a voicemail',
  declined: 'Declined', failed: 'Failed', canceled: 'Canceled',
};
const END_REASON_TEXT = {
  hangup: 'ended normally', remote_hangup: 'they hung up', no_answer: 'no answer', busy: 'the line was busy',
  voicemail: 'left a voicemail', max_duration: 'time limit reached', canceled: 'canceled', meeting_ended: 'the meeting ended',
  transferred: 'handed to you', error: 'something went wrong',
};

function formatPhone(value) {
  const match = /^\+1(\d{3})(\d{3})(\d{4})$/.exec(String(value || ''));
  return match ? `+1 ${match[1]} ${match[2]} ${match[3]}` : String(value || '');
}

function stamp(line) {
  const time = new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false });
  return `${time}  ${line}`;
}

// One plain line per status change, for a person reading over the agent's shoulder.
function describeCallStatus(call) {
  const meeting = call.channel === 'meeting';
  const reason = END_REASON_TEXT[call.endReason] || (call.endReason ? String(call.endReason).replace(/_/g, ' ') : '');
  switch (call.status) {
    case 'queued': return meeting ? 'Queued. Joining the meeting shortly.' : 'Queued. Dialing shortly.';
    case 'connecting': return meeting ? 'Joining the meeting…' : 'Dialing…';
    case 'ringing': return 'Ringing…';
    case 'waiting': return 'Waiting to be let into the meeting…';
    case 'in_progress': return meeting ? 'In the meeting. Smitline is listening.' : 'Connected. Smitline is talking with them.';
    case 'summarizing': return `Call ended${reason ? ` (${reason})` : ''}. Writing the result…`;
    case 'completed': return `Done${reason ? ` (${reason})` : ''}.`;
    case 'failed': return `The call failed${call.error ? `: ${call.error}` : reason ? ` (${reason}).` : '.'}`;
    case 'canceled': return 'Canceled.';
    default: return `Status: ${call.status}`;
  }
}

async function waitUntilDone(client, callId) {
  let last = '';
  for (;;) {
    if (interruptState.requested) {
      throw new InterruptError(`Stopped following call ${callId}. The call keeps going; check it with: smitline calls get --call-id ${callId}`);
    }
    // Short long-polls keep Ctrl-C responsive; the request cannot be aborted mid-wait.
    const call = await client.waitForCall(callId, 10);
    if (call.status !== last) {
      progress(stamp(describeCallStatus(call)));
      last = call.status;
    }
    if (TERMINAL.has(call.status)) {
      const result = call.result;
      if (result?.outcome) progress(`Result: ${OUTCOME_TEXT[result.outcome] || result.outcome}.${result.summary ? ` ${result.summary}` : ''}`);
      return call;
    }
  }
}

async function callCommand(args) {
  const { root, client } = colleagueFromArgs(args);
  const brief = await briefFromArgs(args, root);
  try {
    if (args.check) {
      const report = await client.checkCall(brief);
      printJson(report);
      return report.ok ? EXIT.ok : EXIT.startup;
    }
    const call = await client.startCall(brief);
    const target = call.brief || brief;
    progress(target.channel === 'meeting'
      ? `Joining the meeting (call ${call.id}).`
      : `Calling ${formatPhone(target.to)} (call ${call.id}).`);
    progress(args.wait
      ? 'Following the call until it ends. Ctrl-C stops following; the call keeps going.'
      : `Follow it with: smitline calls wait --call-id ${call.id}`);
    if (!args.wait) {
      printJson(call);
      return EXIT.ok;
    }
    const done = await waitUntilDone(client, call.id);
    printJson(done);
    return done.status === 'completed' ? EXIT.ok : EXIT.runtime;
  } catch (error) {
    explainValidation(error);
    throw error;
  }
}

function requireCallId(args) {
  const callId = args['call-id'] || args._[2];
  if (!callId || callId === true) throw new ValidationError('--call-id is required');
  return String(callId);
}

async function callsCommand(args) {
  const { client } = colleagueFromArgs(args);
  const action = args._[1] || 'list';
  if (action === 'list') {
    printJson({ calls: await client.listCalls(Number(args.limit || 20)) });
    return EXIT.ok;
  }
  const callId = requireCallId(args);
  if (action === 'get') printJson(await client.getCall(callId));
  else if (action === 'wait') {
    const timeout = args.timeout === undefined ? null : Number(args.timeout);
    printJson(timeout === null ? await waitUntilDone(client, callId) : await client.waitForCall(callId, timeout));
  } else if (action === 'end') printJson(await client.endCall(callId));
  else if (action === 'transfer') printJson(await client.transferCall(callId));
  else if (action === 'instruct') {
    if (!args.text || args.text === true) throw new ValidationError('--text is required');
    printJson(await client.instructCall(callId, String(args.text), { silent: args.silent === true }));
  } else throw new ValidationError('unknown calls command');
  return EXIT.ok;
}

const STATUS_GROUPS = [
  ['core', 'The basics'],
  ['phone', 'Phone calls (optional)'],
  ['meetings', 'Meetings (optional)'],
  ['agents', 'Your agents'],
];

// Human-readable setup status, grouped, with one mark per check and the fix under
// anything that is not done. Colors only on a terminal, and never with NO_COLOR.
function printStatus(report, { stream = process.stdout } = {}) {
  const color = Boolean(stream.isTTY) && !process.env.NO_COLOR;
  const paint = (code, text) => (color ? `\u001b[${code}m${text}\u001b[0m` : text);
  const marks = {
    ok: paint('32', '✓'), missing: paint('31', '✗'), optional: paint('33', '○'), unknown: paint('33', '?'),
  };
  const lines = [paint('1', 'Smitline setup'), ''];
  const known = new Set(STATUS_GROUPS.map(([id]) => id));
  const groups = [...STATUS_GROUPS, ...[...new Set(report.checks.map((c) => c.group))]
    .filter((id) => !known.has(id)).map((id) => [id, id[0].toUpperCase() + id.slice(1)])];
  for (const [id, title] of groups) {
    const checks = report.checks.filter((c) => (c.group || 'core') === id);
    if (!checks.length) continue;
    lines.push(paint('1', title));
    for (const item of checks) {
      const mark = item.ok === true ? marks.ok : item.ok === null ? marks.unknown : item.required ? marks.missing : marks.optional;
      lines.push(`  ${mark} ${item.label}${item.detail ? paint('2', ` — ${item.detail}`) : ''}`);
      if (item.ok !== true && item.fix) lines.push(`      Fix: ${item.fix}`);
    }
    lines.push('');
  }
  const blocking = report.checks.filter((c) => c.required && c.ok !== true).length;
  lines.push(report.ready
    ? paint('32', 'Ready. Your agent can use Smitline.')
    : paint('31', `Not ready yet: ${blocking} required ${blocking === 1 ? 'item needs' : 'items need'} a fix (marked ✗).`));
  const phone = report.checks.filter((c) => c.group === 'phone');
  const phoneText = report.phoneReady ? 'ready' : phone.some((c) => c.ok === true && c.id !== 'public_url') ? 'not finished' : 'not set up';
  const docker = report.checks.find((c) => c.id === 'docker');
  const meetingsText = report.meetingsReady ? 'ready' : docker && docker.ok !== true ? 'need Docker' : 'ready once the basics are done';
  lines.push(`Phone calls: ${phoneText}. Meetings: ${meetingsText}.`);
  const next = (report.next || []).find((item) => item.fix);
  if (next) lines.push(`Next: ${next.fix}`);
  else if (report.firstCallReady) lines.push('Try it: smitline setup call-me --wait');
  stream.write(`${lines.join('\n')}\n`);
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

function tail(file, lines = 15) {
  try {
    return readFileSync(file, 'utf8').trimEnd().split('\n').slice(-lines).join('\n');
  } catch {
    return '';
  }
}

/**
 * Start the runtime daemon in the background and wait for it, showing progress.
 * In the container (COLLEAGUE_MANAGED=1) the container runs the daemon: nothing is
 * started, and a closed port is an error that says to restart the container.
 */
async function startDaemon(root, { timeoutMs = 240_000, managed = isManaged() } = {}) {
  const port = Number(process.env.COLLEAGUE_DAEMON_PORT || 8765);
  if (await portOpen(port)) return { running: true, started: false, port, ...(managed ? { managed: true } : {}) };
  if (managed) throw new StartupError(MANAGED_NOT_RUNNING, { code: 'daemon_unavailable' });
  const dir = path.join(root, '.colleague');
  mkdirSync(dir, { recursive: true, mode: 0o700 });
  const log = path.join(dir, 'daemon.log');
  const fd = openSync(log, 'a', 0o600);
  // The launcher is part of the code; COLLEAGUE_ROOT tells it where the data is.
  const child = spawn('bash', [path.join(CODE_ROOT, 'start-runtime-daemon.sh')], {
    cwd: CODE_ROOT,
    detached: true,
    stdio: ['ignore', fd, fd],
    env: { ...process.env, COLLEAGUE_ROOT: root, COLLEAGUE_DAEMON_PORT: String(port) },
  });
  closeSync(fd);
  let exitCode = null;
  child.once('exit', (code) => { exitCode = code ?? 1; });
  child.unref();
  progress('Starting Smitline. The first start builds a small Docker image or installs Python packages, which can take a minute or two.');
  const started = Date.now();
  let lastNote = started;
  while (Date.now() - started < timeoutMs) {
    if (await portOpen(port)) {
      progress(`Smitline is running (log: ${log}).`);
      return { running: true, started: true, port, log };
    }
    if (exitCode !== null) {
      const recent = tail(log);
      const hint = /ensurepip|venv/.test(recent)
        ? ' Python cannot create a virtual environment: start Docker to run Smitline there, or run: sudo apt install -y python3-venv'
        : '';
      throw new StartupError(`Smitline stopped while starting (exit ${exitCode}).${hint}\n${recent}`, { code: 'daemon_unavailable' });
    }
    if (Date.now() - lastNote >= 15_000) {
      lastNote = Date.now();
      progress(`Still starting (${Math.round((Date.now() - started) / 1000)} s)...`);
    }
    await new Promise((resolve) => setTimeout(resolve, 500));
  }
  throw new StartupError(`Smitline did not start within ${Math.round(timeoutMs / 1000)} s. Recent log:\n${tail(log)}`, { code: 'daemon_unavailable' });
}

/** Run the setup page in a background process, so the agent gets the address at once. */
function launchSecretsPage(root, { open }) {
  return new Promise((resolve, reject) => {
    const child = spawn(process.execPath, [fileURLToPath(import.meta.url), 'setup', 'secrets', '--serve', '--root', root], {
      cwd: root,
      detached: true,
      stdio: ['ignore', 'pipe', 'ignore'],
    });
    let buffered = '';
    const timer = setTimeout(() => { child.kill(); reject(new StartupError('the setup page did not start', { code: 'setup_page' })); }, 15_000);
    child.stdout.on('data', (chunk) => {
      buffered += chunk;
      const line = buffered.split('\n')[0];
      if (!buffered.includes('\n')) return;
      clearTimeout(timer);
      child.stdout.destroy();
      child.unref();
      try {
        const { url } = JSON.parse(line);
        resolve({ url, opened: open ? openBrowser(url) : false });
      } catch (error) {
        reject(error);
      }
    });
    child.once('exit', (code) => {
      clearTimeout(timer);
      reject(new StartupError(`the setup page exited early (${code})`, { code: 'setup_page' }));
    });
    child.once('error', reject);
  });
}

async function setupCommand(args) {
  const root = dataRoot(args);
  const action = args._[1] || 'status';
  if (action === 'status') {
    const report = await setupStatus({ root, codeRoot: CODE_ROOT, verify: !args['no-verify'] });
    if (args.json) printJson(report);
    else printStatus(report);
    return report.ready ? EXIT.ok : EXIT.startup;
  }
  if (['secrets', 'start'].includes(action)) {
    // Nothing to clean up: stop at once (a daemon that is starting keeps starting).
    interruptState.handler = () => process.exit(EXIT.interrupt);
  }
  if (action === 'secrets' && args.serve) {
    // Background page process: announce the address on stdout, then stay quiet.
    await serveSecretsPage({
      root,
      timeoutMs: Number(process.env.COLLEAGUE_SETUP_PAGE_TIMEOUT_MS) || undefined,
      onUrl(url) {
        process.stdout.write(`${JSON.stringify({ url })}\n`);
      },
    }).catch(() => {});
    return EXIT.ok;
  }
  if (action === 'secrets' && args.wait) {
    const result = await serveSecretsPage({
      root,
      onUrl(url) {
        const opened = args['no-open'] ? false : openBrowser(url);
        progress(`Enter keys on this page (this computer only; it closes after ${SECRETS_PAGE_MINUTES} minutes):\n${url}`);
        if (!opened && !args['no-open']) progress('Could not open a browser automatically; open the address above.');
      },
      onSaved(keys) {
        progress(`Saved: ${keys.join(', ')}`);
      },
    });
    printJson({ saved: result.saved });
    return EXIT.ok;
  }
  if (action === 'secrets') {
    const { url, opened } = await launchSecretsPage(root, { open: !args['no-open'] });
    printJson({
      url,
      opened,
      next: `${opened ? 'The setup page is open in your browser' : `Open ${url} in a browser on this computer`}. `
        + `Enter your keys there, press Done, then tell me. The page stays available for ${SECRETS_PAGE_MINUTES} minutes.`,
    });
    return EXIT.ok;
  }
  if (action === 'start') {
    if (isManaged()) {
      // The container runs the daemon; only say whether it is up.
      const port = Number(process.env.COLLEAGUE_DAEMON_PORT || 8765);
      const running = await portOpen(port);
      printJson({ running, started: false, port, managed: true, ...(running ? {} : { error: MANAGED_NOT_RUNNING }) });
      return running ? EXIT.ok : EXIT.startup;
    }
    printJson(await startDaemon(root));
    return EXIT.ok;
  }
  if (action === 'stop') {
    // Stops the daemon, and with it the phone tunnel. It starts again on the next call
    // or with smitline setup start.
    const port = Number(process.env.COLLEAGUE_DAEMON_PORT || 8765);
    if (isManaged()) {
      printJson({ stopped: false, managed: true, next: 'Smitline runs in its container; on this computer run: docker stop smitline' });
      return EXIT.ok;
    }
    if (!(await portOpen(port))) {
      printJson({ running: false, stopped: false, port });
      return EXIT.ok;
    }
    const { client } = colleagueFromArgs(args);
    if (args.force !== true) {
      const active = (await client.listCalls(100)).filter((call) => !TERMINAL.has(call.status));
      if (active.length) {
        throw new ValidationError(`A call is in progress (${active.map((call) => call.id).join(', ')}). `
          + 'End it first with smitline calls end --call-id <id>, or run smitline setup stop --force to end it now.');
      }
    }
    await client._transport.stopDaemon();
    const deadline = Date.now() + 30_000;
    while (Date.now() < deadline && await portOpen(port)) await new Promise((resolve) => setTimeout(resolve, 250));
    const stopped = !(await portOpen(port));
    progress(stopped ? 'Smitline stopped. It starts again on the next call, or with: smitline setup start' : 'Smitline is still stopping.');
    printJson({ running: !stopped, stopped, port });
    return stopped ? EXIT.ok : EXIT.startup;
  }
  if (action === 'set') {
    const key = args._[2];
    const value = args._[3];
    if (!key || value === undefined) throw new ValidationError('usage: smitline setup set <KEY> <value>');
    let clean;
    try {
      clean = validateSetting(String(key), String(value));
    } catch (error) {
      throw new ValidationError(error.message);
    }
    writeEnv(root, { [key]: clean });
    printJson({ saved: [key] });
    return EXIT.ok;
  }
  if (action === 'sip-trunk') {
    // Direct SIP: create the SignalWire trunk OpenAI dials out through, and switch calls to it.
    const env = { ...readEnv(root), ...process.env };
    let trunk;
    try {
      trunk = await createSignalWireTrunk({ env });
    } catch (error) {
      throw new ValidationError(error.message);
    }
    writeEnv(root, trunk.settings);
    printJson({
      saved: Object.keys(trunk.settings),
      trunk: trunk.settings.COLLEAGUE_SIP_TRUNK_URL,
      next: 'Calls now use direct SIP. OpenAI must enable outbound SIP for your organization; until then each call is relayed as before.',
    });
    return EXIT.ok;
  }
  if (action === 'register') {
    const token = readOrCreateLocalMcpToken(root);
    if (isManaged()) {
      // Inside the container the host's agent settings are out of reach: print what to run there.
      const report = containerRegistration({ codeRoot: CODE_ROOT, token });
      if (args.json) printJson(report);
      else process.stdout.write(formatContainerRegistration(report));
      return EXIT.ok;
    }
    const agents = args.agents && args.agents !== true ? String(args.agents).split(',').map((a) => a.trim()) : undefined;
    printJson(registerAgents(CODE_ROOT, { agents, http: localMcpConnection({ token }) }));
    return EXIT.ok;
  }
  if (action === 'voice') {
    const env = { ...readEnv(root), ...process.env };
    const voices = availableVoices(env);
    const pick = (value, flag) => {
      if (!voices.includes(String(value))) throw new ValidationError(`--${flag} must be one of: ${voices.join(', ')}`);
      return String(value);
    };
    if (args.set && args.set !== true) {
      writeEnv(root, { COLLEAGUE_VOICE: pick(args.set, 'set') });
    }
    if (args.preview && args.preview !== true) {
      // A short call to the owner's phone in that voice; nothing is saved.
      const voice = pick(args.preview, 'preview');
      const phone = env.COLLEAGUE_OWNER_PHONE;
      const name = env.COLLEAGUE_OWNER_NAME;
      if (!phone || !name) {
        throw new ValidationError('a voice preview calls your phone; set your name and phone first on the setup page');
      }
      return callCommand({
        ...args,
        brief: JSON.stringify({
          channel: 'phone',
          to: phone,
          onBehalfOf: name,
          voice,
          objective: `This is a voice preview for ${name}. In two or three sentences, say this is the ${voice} voice for Smitline, and ask whether they would like to keep it. Then say goodbye and end the call.`,
          maxMinutes: 2,
        }),
      });
    }
    printJson({ voice: readEnv(root).COLLEAGUE_VOICE || 'marin', voices });
    return EXIT.ok;
  }
  if (action === 'call-me') {
    const env = readEnv(root);
    const phone = process.env.COLLEAGUE_OWNER_PHONE || env.COLLEAGUE_OWNER_PHONE;
    const name = process.env.COLLEAGUE_OWNER_NAME || env.COLLEAGUE_OWNER_NAME;
    if (!phone || !name) {
      throw new ValidationError('set your name and phone first: smitline setup set COLLEAGUE_OWNER_NAME "<name>" and COLLEAGUE_OWNER_PHONE +1...');
    }
    return callCommand({
      ...args,
      brief: JSON.stringify({
        channel: 'phone',
        to: phone,
        onBehalfOf: name,
        objective: `This is the setup test call to ${name}, the owner. Say that Smitline is set up and working and that this is your voice. Tell them they can ask their agent for a different voice at any time, and that from now on they can ask their agent to call someone or join a meeting. Answer a quick question if they have one, then say goodbye. Keep it under a minute.`,
        maxMinutes: 3,
      }),
    });
  }
  throw new ValidationError('unknown setup command');
}

// The owner's profile: level-1 context every call gets (who they are, people they know).
async function profileCommand(args) {
  const { client } = colleagueFromArgs(args);
  const action = args._[1] || 'show';
  const text = (value) => (value === undefined || value === true ? undefined : String(value));
  if (action === 'show') {
    printJson(await client.getProfile());
    return EXIT.ok;
  }
  let update;
  if (action === 'set') {
    update = Object.fromEntries(Object.entries({
      about: text(args.about),
      style: text(args.style),
      boundaries: args.boundaries === undefined ? undefined : splitList(args.boundaries) || [],
    }).filter(([, value]) => value !== undefined));
    if (!Object.keys(update).length) throw new ValidationError('usage: smitline profile set --about <text> | --style <text> | --boundaries <a; b>');
  } else if (action === 'person') {
    const name = text(args.name);
    if (!name) throw new ValidationError('--name is required');
    update = args.remove === true ? { removePeople: [name] } : {
      people: [Object.fromEntries(Object.entries({
        name, relationship: text(args.relationship), phone: text(args.phone), notes: text(args.notes),
      }).filter(([, value]) => value !== undefined))],
    };
  } else {
    throw new ValidationError('unknown profile command');
  }
  try {
    printJson(await client.updateProfile(update));
  } catch (error) {
    explainValidation(error);
    throw error;
  }
  return EXIT.ok;
}

// Remote connector grants: listed and revoked without showing any token.
async function connectorCommand(args) {
  const root = dataRoot(args);
  const store = openConnectorStore(root);
  const action = args._[1] || 'status';
  if (action === 'status') {
    const url = process.env.COLLEAGUE_CONNECTOR_URL || readEnv(root).COLLEAGUE_CONNECTOR_URL || null;
    printJson({ connectorUrl: url ? `${url.replace(/\/$/, '')}/mcp` : null, ...store.summary() });
    return EXIT.ok;
  }
  if (action === 'revoke') {
    if (args.all === true) {
      printJson({ revoked: store.revoke({ all: true }) });
      return EXIT.ok;
    }
    if (!args.client || args.client === true) throw new ValidationError('usage: smitline connector revoke --all | --client <id>');
    const clientId = String(args.client);
    const known = store.getClient(clientId) || store.summary().grants.some((grant) => grant.clientId === clientId);
    if (!known) throw new ValidationError(`no registered client ${clientId}; see smitline connector status`);
    printJson({ revoked: store.revoke({ clientId }) });
    return EXIT.ok;
  }
  throw new ValidationError('unknown connector command');
}

async function main(argv = process.argv.slice(2)) {
  const args = parseArgs(argv);
  const command = args._[0];
  if (command === 'version' || args.version === true) {
    process.stdout.write(`smitline ${colleagueVersion()}\n`);
    return EXIT.ok;
  }
  if (!command || command === 'help' || args.help) {
    process.stdout.write(USAGE);
    return command ? EXIT.ok : EXIT.validation;
  }
  try {
    if (command === 'call') return await callCommand(args);
    if (command === 'calls') return await callsCommand(args);
    if (command === 'setup') return await setupCommand(args);
    if (command === 'connector') return await connectorCommand(args);
    if (command === 'profile') return await profileCommand(args);
    if (command === 'mcp') {
      // The stdio MCP server in this process (docker exec -i smitline smitline mcp).
      // stdout carries protocol frames only; this returns when stdin closes.
      const { startStdioServer } = await import('../../mcp/src/server.mjs');
      const { drained } = startStdioServer({ root: dataRoot(args) });
      await drained;
      return EXIT.ok;
    }
    if (command === 'voices') {
      const { client } = colleagueFromArgs(args);
      printJson(await client.listVoices());
      return EXIT.ok;
    }
    throw new ValidationError(`unknown command: ${command}`);
  } catch (error) {
    exitForError(error);
  }
  return EXIT.ok;
}

let invoked = false;
if (process.argv[1]) {
  try {
    invoked = realpathSync(process.argv[1]) === realpathSync(fileURLToPath(import.meta.url));
  } catch {
    invoked = path.resolve(process.argv[1]) === fileURLToPath(import.meta.url);
  }
}
if (invoked) {
  process.on('SIGINT', requestInterrupt);
  process.on('SIGTERM', requestInterrupt);
  main().then((code) => process.exit(code ?? 0), (error) => exitForError(error));
}

export { main, parseArgs, dataRoot, DEFAULT_COLLEAGUE_ROOT };
