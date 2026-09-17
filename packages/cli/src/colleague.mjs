#!/usr/bin/env node
import fs from 'node:fs/promises';
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

const interruptState = { requested: false, handler: null };

function requestInterrupt() {
  interruptState.requested = true;
  interruptState.handler?.();
}

const USAGE = `Usage:
  colleague join --meeting <url> --agent <provider> --workspace <path>
               [--thread <id>] [--model <name>] [--context-file <path>]
               [--context-text <json>] [--context-continuity] [--wait] [--no-camera]
  colleague status [--meeting-id <id>]
  colleague cancel [--meeting-id <id>]
  colleague context add --file <path> | --text <json> [--meeting-id <id>]
  colleague handoff get [--meeting-id <id>]
  colleague handoff retry [--meeting-id <id>]
  colleague approvals list --meeting-id <id>
  colleague approvals get --meeting-id <id> --approval-id <id>
  colleague approvals decide --meeting-id <id> --approval-id <id> --decision approved|denied
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

async function saveMeetingId(root, meetingId) {
  const file = stateFile(root);
  await fs.mkdir(path.dirname(file), { mode: 0o700, recursive: true });
  await fs.writeFile(file, `${JSON.stringify({ meetingId })}\n`, { mode: 0o600 });
}

async function loadMeetingId(root, explicit) {
  if (explicit) return explicit;
  try {
    const payload = JSON.parse(await fs.readFile(stateFile(root), 'utf8'));
    if (payload?.meetingId) return payload.meetingId;
  } catch {
    // No previous meeting.
  }
  throw new ValidationError('no meeting id; pass --meeting-id or run join first');
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
  return type;
}

function colleagueFromArgs(args) {
  const root = path.resolve(args.root || process.env.COLLEAGUE_ROOT || process.cwd());
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
  if (!args.agent) throw new ValidationError('--agent is required');
  if (!args.workspace) throw new ValidationError('--workspace is required');
  const exact = !args['context-continuity'];
  if (exact && !args.thread) {
    throw new ValidationError('exact continuity requires --thread; pass --context-continuity for context-only joins');
  }
  const context = await readContext(args);
  const { root, client } = colleagueFromArgs(args);
  const meeting = await client.joinMeeting({
    url: args.meeting,
    agentSession: {
      provider: args.agent,
      sessionId: args.thread || 'local-portal',
      workspace: path.resolve(args.workspace),
      ...(args.model ? { model: args.model } : {}),
    },
    context,
    ...(args['no-camera'] ? { camera: { enabled: false } } : {}),
  });
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

async function main(argv = process.argv.slice(2)) {
  const args = parseArgs(argv);
  const command = args._[0];
  if (!command || command === 'help' || args.help) {
    process.stdout.write(USAGE);
    return command ? EXIT.ok : EXIT.validation;
  }
  try {
    if (command === 'join') return await joinCommand(args);
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
    throw new ValidationError(`unknown command: ${command}`);
  } catch (error) {
    exitForError(error);
  }
  return EXIT.ok;
}

const invoked = process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url);
if (invoked) {
  process.on('SIGINT', requestInterrupt);
  process.on('SIGTERM', requestInterrupt);
  main().then((code) => process.exit(code ?? 0), (error) => exitForError(error));
}

export { main, parseArgs, describeEvent };
