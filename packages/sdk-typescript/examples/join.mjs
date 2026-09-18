/**
 * Blocking TypeScript SDK example for Codex or Cursor host integrations.
 *
 * Pass the originating thread id explicitly. Colleague AI never infers `--last`
 * or hashes the meeting URL into a session id.
 */
import { Colleague } from '../src/index.mjs';

const colleague = new Colleague();

const meeting = await colleague.joinMeeting({
  url: process.env.MEETING_URL,
  agentSession: {
    provider: 'codex',
    sessionId: process.env.CODEX_THREAD_ID,
    workspace: process.cwd(),
    model: process.env.CODEX_MODEL,
  },
  permissions: {
    workspace: 'read-only',
    commands: 'approval-required',
    edits: 'disabled',
    network: 'approval-required',
    commits: 'disabled',
    pushes: 'disabled',
  },
});

meeting.on('state', (event) => {
  process.stderr.write(`${event.type}\n`);
});
meeting.on('delegation', (event) => {
  process.stderr.write(`delegation ${event.type}\n`);
});

const handoff = await meeting.finished;
process.stdout.write(`${JSON.stringify({ meetingId: meeting.id, handoffId: handoff.handoffId, archivePath: handoff.archivePath }, null, 2)}\n`);
