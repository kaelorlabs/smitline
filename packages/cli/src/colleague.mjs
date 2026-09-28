#!/usr/bin/env node
import fs from 'node:fs/promises';
import { realpathSync } from 'node:fs';
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
  validateContext,
} from '../../sdk-typescript/src/index.mjs';
import {
  GPT_LIVE_VOICES,
  openBrowser,
  readEnv,
  registerAgents,
  serveSecretsPage,
  setupStatus,
  validateSetting,
  writeEnv,
} from './setup.mjs';
import { openConnectorStore } from '../../mcp/src/connector-store.mjs';

const interruptState = { requested: false, handler: null };
const DEFAULT_COLLEAGUE_ROOT = path.resolve(fileURLToPath(new URL('../../../', import.meta.url)));

function requestInterrupt() {
  interruptState.requested = true;
  interruptState.handler?.();
}

const USAGE = `Usage:
  colleague call --to <+E.164> --objective <text> [--on-behalf-of <name>] [--context <text>]
               [--agree <a; b>] [--never-share <a; b>] [--success <text>] [--voice <name>]
               [--language <tag>] [--max-minutes <n>] [--rehearsal] [--webhook <url>]
               [--check] [--wait]
  colleague call --meeting <url> --objective <text> [...same options] [--wait]
  colleague call --brief <json> | --brief-file <path> [--check] [--wait]
  colleague calls list [--limit <n>]
  colleague calls get|wait|end|transfer --call-id <id> [--timeout <seconds>]
  colleague calls instruct --call-id <id> --text <guidance>
  colleague voices
  colleague setup status [--json] [--no-verify]
  colleague setup secrets [--no-open]
  colleague setup set <KEY> <value>
  colleague setup register [--agents claude-code,codex,cursor]
  colleague setup voice [--set <name>]
  colleague setup call-me [--wait]
  colleague connector status
  colleague connector revoke --all | --client <id>
  colleague join --meeting <url> [--agent <provider>] [--workspace <path>]
               [--thread <id>] [--model <name>] [--context-file <path>]
               [--context-text <json>] [--context-continuity] [--wait] [--no-camera]
               [--screen-share] [--replace]
  colleague status [--meeting-id <id>]
  colleague cancel [--meeting-id <id>]
  colleague context add --file <path> | --text <json> [--meeting-id <id>]
  colleague context validate --file <path> | --text <json>
  colleague handoff get [--meeting-id <id>]
  colleague handoff retry [--meeting-id <id>]
  colleague approvals list --meeting-id <id>
  colleague approvals get --meeting-id <id> --approval-id <id>
  colleague approvals decide --meeting-id <id> --approval-id <id> --decision approved|denied
  colleague artifacts list --meeting-id <id>
  colleague artifacts get --meeting-id <id> --artifact-id <id>
  colleague commits list --meeting-id <id>
  colleague commits get --meeting-id <id> --operation-id <id>
  colleague commits create --meeting-id <id> --expected-head <sha> --message <text> --file <path:sha256>
  colleague pushes list --meeting-id <id>
  colleague pushes get --meeting-id <id> --operation-id <id>
  colleague pushes create --meeting-id <id> --commit-sha <sha> --remote <name> --branch <name>
  colleague screen-share status --meeting-id <id>
  colleague screen-share pause --meeting-id <id>
  colleague screen-share resume --meeting-id <id>
  colleague screen-share observations --meeting-id <id>
  colleague providers
  colleague runner status
  colleague runner pair
  colleague runner complete --pairing-id <id> --pairing-code <code>
  colleague runner unpair
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

function fail(error, code) {
  const message = error instanceof Error ? error.message : String(error);
  process.stderr.write(`${message}\n`);
  process.exit(code);
}

function exitForError(error) {
  if (error instanceof ValidationError) fail(error, EXIT.validation);
  if (error instanceof StartupError) fail(error, EXIT.startup);
  if (error instanceof InterruptError) fail(error, EXIT.interrupt);
  if (error instanceof FinalizationError) {
    if (error.handoff?.partial) {
      writeFinal(error.handoff);
      process.exit(EXIT.partial);
    }
    if (error.archivePath) process.stderr.write(`archive: ${error.archivePath}\n`);
    fail(error, EXIT.finalization);
  }
  if (error instanceof RuntimeError) fail(error, EXIT.runtime);
  fail(error, EXIT.runtime);
}

function progress(line) {
  process.stderr.write(`${line}\n`);
}

function writeFinal(handoff) {
  const archivePath = handoff?.archivePath || (handoff?.meetingId ? `recordings/${handoff.meetingId}` : null);
  process.stdout.write(`${JSON.stringify({ handoff, archivePath }, null, 2)}\n`);
}

function stateFile(root) {
  return path.join(root, '.colleague', 'cli-meeting.json');
}

function activeMeetingFile(root) {
  return path.join(root, '.colleague', 'active-meeting.json');
}

async function saveMeetingId(root, meetingId) {
  const file = stateFile(root);
  await fs.mkdir(path.dirname(file), { mode: 0o700, recursive: true });
  await fs.writeFile(file, `${JSON.stringify({ meetingId })}\n`, { mode: 0o600 });
}

async function readMeetingId(file) {
  try {
    const payload = JSON.parse(await fs.readFile(file, 'utf8'));
    return typeof payload?.meetingId === 'string' && payload.meetingId ? payload.meetingId : null;
  } catch {
    return null;
  }
}

async function meetingIdCandidates(root) {
  const ids = await Promise.all([
    readMeetingId(activeMeetingFile(root)),
    readMeetingId(stateFile(root)),
  ]);
  return [...new Set(ids.filter(Boolean))];
}

async function loadMeetingId(root, explicit) {
  if (explicit) return explicit;
  const [meetingId] = await meetingIdCandidates(root);
  if (meetingId) return meetingId;
  throw new ValidationError('no meeting id; pass --meeting-id or run join first');
}

async function findActiveMeeting(root, client) {
  for (const meetingId of await meetingIdCandidates(root)) {
    try {
      const meeting = await client._transport.getMeeting(meetingId);
      if (!new Set(['ended', 'cancelled', 'failed']).has(meeting?.state)) return meeting;
    } catch (error) {
      if (error?.code !== 'not_found') throw error;
    }
  }
  return null;
}

function activeMeetingMessage(meetingId) {
  return `meeting agent ${meetingId} is already running; run colleague cancel --meeting-id ${meetingId} or retry join with --replace`;
}

async function readContext(args) {
  if (args['context-file'] && args['context-text']) {
    throw new ValidationError('use only one of --context-file or --context-text');
  }
  if (args['context-file']) {
    const raw = await fs.readFile(args['context-file'], 'utf8');
    return validateContext(JSON.parse(raw));
  }
  if (args['context-text']) return validateContext(JSON.parse(args['context-text']));
  return undefined;
}

function describeEvent(event) {
  const type = event?.type || 'event';
  if (String(type).startsWith('transcript.')) return `transcript ${type}`;
  if (type.startsWith('delegation.')) {
    const task = event.taskId || event.delegationId || '';
    return `delegation ${type.replace('delegation.', '')}${task ? ` ${task}` : ''}`;
  }
  if (type.startsWith('meeting.')) return `state ${type.replace('meeting.', '')}`;
  if (type === 'presence.updated') return `presence ${event.visualState || 'updated'}`;
  if (type === 'handoff.ready') return 'handoff ready';
  if (type === 'handoff.append_failed') return 'handoff append failed';
  if (String(type).startsWith('approval.')) {
    const category = event.request?.category || event.request?.permission || event.decision?.decision || '';
    return `approval ${type.replace('approval.', '')}${category ? ` ${category}` : ''}`;
  }
  if (String(type).startsWith('workspace.action.')) {
    return `workspace ${type.replace('workspace.action.', '')}`;
  }
  if (type === 'artifact.created') return `artifact ${event.artifact?.kind || event.artifact?.id || ''}`.trim();
  if (type.startsWith('screen_share.')) return `screen-share ${type.replace('screen_share.', '')}`;
  return type;
}

function colleagueFromArgs(args) {
  const root = path.resolve(args.root || process.env.COLLEAGUE_ROOT || DEFAULT_COLLEAGUE_ROOT);
  return {
    root,
    client: new Colleague({
      root,
      host: args.host || process.env.COLLEAGUE_DAEMON_HOST || '127.0.0.1',
      port: Number(args.port || process.env.COLLEAGUE_DAEMON_PORT || 8765),
    }),
  };
}

async function joinCommand(args) {
  if (!args.meeting) throw new ValidationError('--meeting is required');
  const provider = args.agent || 'codex';
  const workspace = path.resolve(args.workspace || process.cwd());
  const exact = !args['context-continuity'];
  const thread = args.thread || (provider === 'codex' ? process.env.CODEX_THREAD_ID : undefined);
  if (exact && !thread) {
    throw new ValidationError('exact continuity requires --thread or CODEX_THREAD_ID; pass --context-continuity for context-only joins');
  }
  const context = await readContext(args);
  const { root, client } = colleagueFromArgs(args);
  const active = await findActiveMeeting(root, client);
  if (active && !args.replace) {
    throw new RuntimeError(activeMeetingMessage(active.id), { code: 'capacity_exceeded' });
  }
  if (active) {
    progress(`replacing active meeting ${active.id}`);
    await client._transport.cancelMeeting(active.id);
  }
  let meeting;
  try {
    meeting = await client.joinMeeting({
      url: args.meeting,
      agentSession: {
        provider,
        sessionId: thread || 'local-portal',
        workspace,
        ...(args.model ? { model: args.model } : {}),
      },
      context,
      ...(args['no-camera'] ? { camera: { enabled: false } } : {}),
      ...(args['screen-share'] ? { screenShare: { enabled: true } } : {}),
    });
  } catch (error) {
    if (error?.code === 'capacity_exceeded') {
      const known = await findActiveMeeting(root, client);
      if (known?.id) throw new RuntimeError(activeMeetingMessage(known.id), { code: error.code });
    }
    throw error;
  }
  await saveMeetingId(root, meeting.id);
  progress(`joined ${meeting.id}`);
  if (!args.wait) {
    process.stdout.write(`${JSON.stringify({ meetingId: meeting.id }, null, 2)}\n`);
    return EXIT.ok;
  }

  let cancelStarted = false;
  const onInterrupt = () => {
    interruptState.requested = true;
    if (!cancelStarted) progress('interrupt: requesting cancellation');
    cancelStarted = true;
    meeting.cancel().catch((error) => progress(error.message));
  };
  interruptState.handler = onInterrupt;
  if (interruptState.requested) onInterrupt();
  meeting.on('event', (event) => progress(describeEvent(event)));
  try {
    const handoff = await meeting.finished;
    writeFinal(handoff);
    if (interruptState.requested) return EXIT.interrupt;
    if (handoff?.partial) return EXIT.partial;
    return EXIT.ok;
  } finally {
    if (interruptState.handler === onInterrupt) interruptState.handler = null;
  }
}

async function daemonCall(args, method, ...rest) {
  const { root, client } = colleagueFromArgs(args);
  const meetingId = await loadMeetingId(root, args['meeting-id']);
  const transport = client._transport;
  return { root, meetingId, result: await transport[method](meetingId, ...rest) };
}

function splitList(value) {
  if (value === undefined || value === true) return undefined;
  return String(value).split(';').map((item) => item.trim()).filter(Boolean);
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
  const brief = {
    channel: text(args.meeting) ? 'meeting' : 'phone',
    to: text(args.meeting) || text(args.to),
    onBehalfOf: text(args['on-behalf-of']) || process.env.COLLEAGUE_OWNER_NAME || env.COLLEAGUE_OWNER_NAME,
    objective: text(args.objective),
    context: text(args.context),
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

async function waitUntilDone(client, callId) {
  let last = '';
  for (;;) {
    if (interruptState.requested) throw new InterruptError('interrupted; the call keeps running');
    // Short long-polls keep Ctrl-C responsive; the request cannot be aborted mid-wait.
    const call = await client.waitForCall(callId, 10);
    if (call.status !== last) {
      progress(`call ${call.status}${call.endReason ? ` (${call.endReason})` : ''}`);
      last = call.status;
    }
    if (TERMINAL.has(call.status)) return call;
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
    progress(`call ${call.id} queued`);
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
    printJson(await client.instructCall(callId, String(args.text)));
  } else throw new ValidationError('unknown calls command');
  return EXIT.ok;
}

function printStatus(report) {
  for (const item of report.checks) {
    const mark = item.ok === true ? 'PASS' : item.required ? 'FAIL' : 'WARN';
    process.stdout.write(`${mark}  ${item.label}${item.detail ? ` — ${item.detail}` : ''}\n`);
    if (item.ok !== true && item.fix) process.stdout.write(`      fix: ${item.fix}\n`);
  }
  process.stdout.write(`\nready: ${report.ready}  phone: ${report.phoneReady}  meetings: ${report.meetingsReady}\n`);
}

async function setupCommand(args) {
  const root = path.resolve(args.root || process.env.COLLEAGUE_ROOT || DEFAULT_COLLEAGUE_ROOT);
  const action = args._[1] || 'status';
  if (action === 'status') {
    const report = await setupStatus({ root, verify: !args['no-verify'] });
    if (args.json) printJson(report);
    else printStatus(report);
    return report.ready ? EXIT.ok : EXIT.startup;
  }
  if (action === 'secrets') {
    const result = await serveSecretsPage({
      root,
      onUrl(url) {
        const opened = args['no-open'] ? false : openBrowser(url);
        progress(`Open this page to enter keys (it works once, on this computer only):\n${url}`);
        if (!opened) progress('Could not open a browser automatically; open the address above.');
      },
    });
    printJson({ saved: result.saved });
    return EXIT.ok;
  }
  if (action === 'set') {
    const key = args._[2];
    const value = args._[3];
    if (!key || value === undefined) throw new ValidationError('usage: colleague setup set <KEY> <value>');
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
  if (action === 'register') {
    const agents = args.agents && args.agents !== true ? String(args.agents).split(',').map((a) => a.trim()) : undefined;
    printJson(registerAgents(root, { agents }));
    return EXIT.ok;
  }
  if (action === 'voice') {
    if (args.set && args.set !== true) {
      try {
        writeEnv(root, { COLLEAGUE_VOICE: validateSetting('COLLEAGUE_VOICE', String(args.set)) });
      } catch (error) {
        throw new ValidationError(error.message);
      }
    }
    printJson({ voice: readEnv(root).COLLEAGUE_VOICE || 'marin', voices: GPT_LIVE_VOICES });
    return EXIT.ok;
  }
  if (action === 'call-me') {
    const env = readEnv(root);
    const phone = process.env.COLLEAGUE_OWNER_PHONE || env.COLLEAGUE_OWNER_PHONE;
    const name = process.env.COLLEAGUE_OWNER_NAME || env.COLLEAGUE_OWNER_NAME;
    if (!phone || !name) {
      throw new ValidationError('set your name and phone first: colleague setup set COLLEAGUE_OWNER_NAME "<name>" and COLLEAGUE_OWNER_PHONE +1...');
    }
    return callCommand({
      ...args,
      brief: JSON.stringify({
        channel: 'phone',
        to: phone,
        onBehalfOf: name,
        objective: `This is the setup test call to ${name}, the owner. Say that Colleague AI is set up and working and that this is your voice. Tell them they can ask their agent for a different voice at any time, and that from now on they can ask their agent to call someone or join a meeting. Answer a quick question if they have one, then say goodbye. Keep it under a minute.`,
        maxMinutes: 3,
      }),
    });
  }
  throw new ValidationError('unknown setup command');
}

// Remote connector grants: listed and revoked without showing any token.
async function connectorCommand(args) {
  const root = path.resolve(args.root || process.env.COLLEAGUE_ROOT || DEFAULT_COLLEAGUE_ROOT);
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
    if (!args.client || args.client === true) throw new ValidationError('usage: colleague connector revoke --all | --client <id>');
    const clientId = String(args.client);
    const known = store.getClient(clientId) || store.summary().grants.some((grant) => grant.clientId === clientId);
    if (!known) throw new ValidationError(`no registered client ${clientId}; see colleague connector status`);
    printJson({ revoked: store.revoke({ clientId }) });
    return EXIT.ok;
  }
  throw new ValidationError('unknown connector command');
}

async function main(argv = process.argv.slice(2)) {
  const args = parseArgs(argv);
  const command = args._[0];
  if (!command || command === 'help' || args.help) {
    process.stdout.write(USAGE);
    return command ? EXIT.ok : EXIT.validation;
  }
  try {
    if (command === 'context' && args._[1] === 'validate') {
      if (!args.file && !args.text) throw new ValidationError('context validate requires --file or --text');
      const context = await readContext({
        'context-file': args.file,
        'context-text': args.text,
      });
      process.stdout.write(`${JSON.stringify({ valid: true, version: context.version }, null, 2)}\n`);
      return EXIT.ok;
    }
    if (command === 'join') return await joinCommand(args);
    if (command === 'call') return await callCommand(args);
    if (command === 'calls') return await callsCommand(args);
    if (command === 'setup') return await setupCommand(args);
    if (command === 'connector') return await connectorCommand(args);
    if (command === 'voices') {
      const { client } = colleagueFromArgs(args);
      printJson(await client.listVoices());
      return EXIT.ok;
    }
    if (command === 'status') {
      const { result } = await daemonCall(args, 'getMeeting');
      process.stdout.write(`${JSON.stringify(result, null, 2)}\n`);
      return EXIT.ok;
    }
    if (command === 'cancel') {
      const { result } = await daemonCall(args, 'cancelMeeting');
      process.stdout.write(`${JSON.stringify({ id: result.id, state: result.state }, null, 2)}\n`);
      return EXIT.ok;
    }
    if (command === 'context' && args._[1] === 'add') {
      const payload = args.file
        ? validateContext(JSON.parse(await fs.readFile(args.file, 'utf8')))
        : validateContext(JSON.parse(args.text || 'null'));
      const { result } = await daemonCall(args, 'updateContext', payload);
      process.stdout.write(`${JSON.stringify({ id: result.id, state: result.state }, null, 2)}\n`);
      return EXIT.ok;
    }
    if (command === 'handoff' && args._[1] === 'get') {
      const { result } = await daemonCall(args, 'getHandoff');
      writeFinal(result);
      return result?.partial ? EXIT.partial : EXIT.ok;
    }
    if (command === 'handoff' && args._[1] === 'retry') {
      const { result } = await daemonCall(args, 'retryHandoff');
      writeFinal(result);
      return result?.partial ? EXIT.partial : EXIT.ok;
    }
    if (command === 'approvals') {
      if (!args['meeting-id']) throw new ValidationError('approvals commands require --meeting-id');
      const { client } = colleagueFromArgs(args);
      const transport = client._transport;
      const meetingId = args['meeting-id'];
      if (args._[1] === 'list') {
        const result = await transport.listApprovals(meetingId);
        process.stdout.write(`${JSON.stringify(result, null, 2)}\n`);
        return EXIT.ok;
      }
      if (!args['approval-id']) throw new ValidationError('this command requires --approval-id');
      if (args._[1] === 'get') {
        const result = await transport.getApproval(meetingId, args['approval-id']);
        process.stdout.write(`${JSON.stringify(result, null, 2)}\n`);
        return EXIT.ok;
      }
      if (args._[1] === 'decide') {
        const decision = args.decision;
        if (decision !== 'approved' && decision !== 'denied') {
          throw new ValidationError('--decision must be approved or denied');
        }
        const result = await transport.decideApproval(meetingId, args['approval-id'], { decision });
        process.stdout.write(`${JSON.stringify(result, null, 2)}\n`);
        return EXIT.ok;
      }
      throw new ValidationError('unknown approvals command');
    }
    if (command === 'artifacts') {
      if (!args['meeting-id']) throw new ValidationError('artifacts commands require --meeting-id');
      const { client } = colleagueFromArgs(args);
      const transport = client._transport;
      const meetingId = args['meeting-id'];
      if (args._[1] === 'list') {
        const result = await transport.listArtifacts(meetingId);
        process.stdout.write(`${JSON.stringify(result, null, 2)}\n`);
        return EXIT.ok;
      }
      if (!args['artifact-id']) throw new ValidationError('this command requires --artifact-id');
      if (args._[1] === 'get') {
        const result = await transport.getArtifact(meetingId, args['artifact-id']);
        process.stdout.write(`${JSON.stringify(result, null, 2)}\n`);
        return EXIT.ok;
      }
      throw new ValidationError('unknown artifacts command');
    }
    if (command === 'commits' || command === 'pushes') {
      if (!args['meeting-id']) throw new ValidationError(`${command} commands require --meeting-id`);
      const { client } = colleagueFromArgs(args);
      const transport = client._transport;
      const meetingId = args['meeting-id'];
      const methods = command === 'commits'
        ? { list: 'listCommits', get: 'getCommit', create: 'createCommit' }
        : { list: 'listPushes', get: 'getPush', create: 'createPush' };
      if (args._[1] === 'list') {
        const result = await transport[methods.list](meetingId);
        process.stdout.write(`${JSON.stringify(result, null, 2)}\n`);
        return EXIT.ok;
      }
      if (args._[1] === 'get') {
        if (!args['operation-id']) throw new ValidationError('this command requires --operation-id');
        const result = await transport[methods.get](meetingId, args['operation-id']);
        process.stdout.write(`${JSON.stringify(result, null, 2)}\n`);
        return EXIT.ok;
      }
      if (args._[1] === 'create') {
        if (command === 'commits') {
          if (!args['expected-head'] || !args.message || !args.file) {
            throw new ValidationError('commits create requires --expected-head, --message, and --file');
          }
          const [filePath, digest] = String(args.file).split(':');
          if (!filePath || !digest) throw new ValidationError('--file must be path:sha256');
          const result = await transport[methods.create](meetingId, {
            expectedHead: args['expected-head'],
            message: args.message,
            files: [{ path: filePath, sha256: digest }],
            id: args['operation-id'],
          });
          process.stdout.write(`${JSON.stringify(result, null, 2)}\n`);
          return EXIT.ok;
        }
        if (!args['commit-sha'] || !args.remote || !args.branch) {
          throw new ValidationError('pushes create requires --commit-sha, --remote, and --branch');
        }
        const result = await transport[methods.create](meetingId, {
          commitSha: args['commit-sha'],
          remote: args.remote,
          branch: args.branch,
          id: args['operation-id'],
        });
        process.stdout.write(`${JSON.stringify(result, null, 2)}\n`);
        return EXIT.ok;
      }
      throw new ValidationError(`unknown ${command} command`);
    }
    if (command === 'screen-share') {
      if (!args['meeting-id']) throw new ValidationError('screen-share commands require --meeting-id');
      const { client } = colleagueFromArgs(args);
      const transport = client._transport;
      const meetingId = args['meeting-id'];
      if (args._[1] === 'status') {
        const result = await transport.getScreenShare(meetingId);
        process.stdout.write(`${JSON.stringify(result, null, 2)}\n`);
        return EXIT.ok;
      }
      if (args._[1] === 'pause') {
        const result = await transport.pauseScreenShare(meetingId);
        process.stdout.write(`${JSON.stringify(result, null, 2)}\n`);
        return EXIT.ok;
      }
      if (args._[1] === 'resume') {
        const result = await transport.resumeScreenShare(meetingId);
        process.stdout.write(`${JSON.stringify(result, null, 2)}\n`);
        return EXIT.ok;
      }
      if (args._[1] === 'observations') {
        const result = await transport.listScreenShareObservations(meetingId);
        process.stdout.write(`${JSON.stringify(result, null, 2)}\n`);
        return EXIT.ok;
      }
      throw new ValidationError('unknown screen-share command');
    }
    if (command === 'providers') {
      const { client } = colleagueFromArgs(args);
      const result = await client.listProviders();
      process.stdout.write(`${JSON.stringify(result, null, 2)}\n`);
      return EXIT.ok;
    }
    if (command === 'runner') {
      const { client } = colleagueFromArgs(args);
      const action = args._[1] || 'status';
      if (action === 'status') {
        const result = await client.runnerStatus();
        process.stdout.write(`${JSON.stringify(result, null, 2)}\n`);
        return EXIT.ok;
      }
      if (action === 'pair') {
        const result = await client.pairRunner();
        process.stdout.write(`${JSON.stringify(result, null, 2)}\n`);
        return EXIT.ok;
      }
      if (action === 'complete') {
        if (!args['pairing-id'] || !args['pairing-code']) {
          throw new ValidationError('runner complete requires --pairing-id and --pairing-code');
        }
        const result = await client.completeRunnerPair({
          pairingId: args['pairing-id'],
          pairingCode: args['pairing-code'],
        });
        process.stdout.write(`${JSON.stringify(result, null, 2)}\n`);
        return EXIT.ok;
      }
      if (action === 'unpair') {
        const result = await client.unpairRunner();
        process.stdout.write(`${JSON.stringify(result, null, 2)}\n`);
        return EXIT.ok;
      }
      throw new ValidationError('unknown runner command');
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

export { main, parseArgs, describeEvent, DEFAULT_COLLEAGUE_ROOT };
