import fs from 'node:fs';
import path from 'node:path';

import { detectPlatform } from './config.mjs';

export const LOCAL_PORTAL_SESSION_ID = 'local-portal';
export const CONTROL_DIR = '.colleague';
export const ACTIVE_MEETING_NAME = 'portal-active.json';
export const SUPERVISOR_ACTIVE_NAME = 'active-meeting.json';
const ACTIVE_PHASES = new Set([
  'starting', 'opening_meeting', 'joining', 'waiting_for_admission', 'admitted',
  'connecting_audio', 'live',
]);

export function continuityFromAgentSession(agentSession = {}) {
  const sessionId = String(agentSession.sessionId || '');
  const metadata = agentSession.metadata || {};
  if (metadata.continuity === 'exact' || metadata.continuity === 'context') {
    if (metadata.continuity === 'exact'
        && (metadata.source === 'local-portal' || sessionId === LOCAL_PORTAL_SESSION_ID)) {
      return 'context';
    }
    return metadata.continuity;
  }
  if (metadata.source === 'local-portal' || sessionId === LOCAL_PORTAL_SESSION_ID) {
    return 'context';
  }
  return 'exact';
}

export function defaultWorkspace(root) {
  return path.join(root, 'meeting-runtime', 'codex-workspace');
}

export function ensureDefaultWorkspace(root) {
  const workspace = defaultWorkspace(root);
  fs.mkdirSync(workspace, { recursive: true, mode: 0o700 });
  return workspace;
}

export function permissionsForTools(tools = {}) {
  return {
    workspace: tools.codex ? 'read-only' : 'none',
    commands: 'disabled',
    edits: 'disabled',
    network: tools.webSearch ? 'allowed' : 'disabled',
    commits: 'disabled',
    pushes: 'disabled',
  };
}

function clip(value, max = 8000) {
  const text = String(value ?? '');
  return text.length <= max ? text : text.slice(0, max);
}

export function contextHandoffFromSources(sources = [], { meetingInstructions = '' } = {}) {
  const names = sources.map(source => source.name).filter(Boolean);
  const summaryParts = sources.map(source => {
    const name = source.name || 'source';
    const text = String(source.text || '').trim();
    return text ? `${name}: ${text}` : name;
  });
  const recentConversation = sources.slice(0, 128).map(source => ({
    role: 'user',
    text: clip(`${source.name || 'source'}\n${String(source.text || '').trim()}`.trim() || 'source'),
  }));
  const objective = clip(meetingInstructions.trim() || 'Support this live meeting from the local portal.');
  return {
    version: 1,
    objective,
    currentTask: 'Join the meeting and help when asked.',
    summary: clip(summaryParts.join('\n\n')),
    decisions: [],
    constraints: [],
    openQuestions: [],
    importantFiles: names.slice(0, 200),
    recentConversation,
  };
}

export function buildMeetingCreatePayload(settings, { sources = [], workspace, root } = {}) {
  const tools = settings.tools || {};
  const resolved = workspace
    || String(settings.workspace || '').trim()
    || (root ? defaultWorkspace(root) : '');
  return {
    meetingUrl: settings.meetingUrl,
    agentSession: {
      provider: tools.codex ? 'codex' : 'generic',
      sessionId: LOCAL_PORTAL_SESSION_ID,
      workspace: resolved,
      model: settings.model,
      metadata: { source: 'local-portal', continuity: 'context' },
    },
    context: contextHandoffFromSources(sources, {
      meetingInstructions: settings.meetingInstructions,
    }),
    permissions: permissionsForTools(tools),
  };
}

export function meetingIsActive(session) {
  return Boolean(session && session.state && session.state !== 'ended');
}

export function phaseFromDaemon({ session, health, daemonError } = {}) {
  if (daemonError && session && meetingIsActive(session)) return 'needs_attention';
  if (daemonError && !session) return 'stopped';
  const healthStage = health?.stage;
  if (healthStage === 'authentication_required' || healthStage === 'needs_attention' || healthStage === 'api_error') {
    return healthStage;
  }
  if (!session || session.state === 'ended') {
    if (healthStage === 'meeting_ended' || session?.state === 'ended') return 'meeting_ended';
    return 'stopped';
  }
  if (session.state === 'waiting_for_admission' || healthStage === 'waiting_for_admission') {
    return 'waiting_for_admission';
  }
  if (session.state === 'live' || healthStage === 'live') return 'live';
  if (healthStage === 'admitted' || healthStage === 'connecting_audio' || healthStage === 'opening_meeting') {
    return healthStage;
  }
  if (session.state === 'joining') return healthStage || 'starting';
  return session.state;
}

export function meetingBusy(status = {}) {
  if (status.running) return true;
  return ACTIVE_PHASES.has(status.phase);
}

export function controlRoot(projectRoot) {
  return path.join(projectRoot, CONTROL_DIR);
}

export function portalActivePath(projectRoot) {
  return path.join(controlRoot(projectRoot), ACTIVE_MEETING_NAME);
}

export function supervisorActivePath(projectRoot) {
  return path.join(controlRoot(projectRoot), SUPERVISOR_ACTIVE_NAME);
}

function readJsonFile(file) {
  try {
    return JSON.parse(fs.readFileSync(file, 'utf8'));
  } catch {
    return null;
  }
}

export function readActiveMeetingId(projectRoot) {
  const portal = readJsonFile(portalActivePath(projectRoot));
  if (portal?.meetingId) return portal.meetingId;
  const supervisor = readJsonFile(supervisorActivePath(projectRoot));
  return supervisor?.meetingId || null;
}

export function writeActiveMeetingId(projectRoot, meetingId) {
  const file = portalActivePath(projectRoot);
  fs.mkdirSync(path.dirname(file), { recursive: true, mode: 0o700 });
  const temporary = `${file}.tmp`;
  fs.writeFileSync(temporary, `${JSON.stringify({ meetingId })}\n`, { mode: 0o600 });
  fs.renameSync(temporary, file);
  fs.chmodSync(file, 0o600);
  try { fs.chmodSync(path.dirname(file), 0o700); } catch {}
  return meetingId;
}

export function clearActiveMeetingId(projectRoot) {
  try { fs.unlinkSync(portalActivePath(projectRoot)); } catch (error) {
    if (error.code !== 'ENOENT') throw error;
  }
}

export { detectPlatform };
