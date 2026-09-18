/** Versioned Colleague AI SDK types mirroring daemon schemas. */
export const SCHEMA_VERSION = 1;

export type AgentProvider = 'codex' | 'cursor' | 'claude-code' | 'generic';
export type MeetingState = 'joining' | 'waiting_for_admission' | 'live' | 'ended';
export type Continuity = 'exact' | 'context';

export interface AgentSessionRef {
  provider: AgentProvider;
  sessionId: string;
  workspace: string;
  model?: string;
  metadata?: Record<string, string>;
}

export interface MeetingPermissions {
  workspace: 'none' | 'read-only' | 'workspace-write';
  commands: 'disabled' | 'approval-required' | 'allowed';
  edits: 'disabled' | 'approval-required' | 'allowed';
  network: 'disabled' | 'approval-required' | 'allowed';
  commits: 'disabled' | 'approval-required';
  pushes: 'disabled' | 'approval-required';
}

export interface ContextHandoff {
  version: 1;
  objective: string;
  currentTask: string;
  summary: string;
  decisions: string[];
  constraints: string[];
  openQuestions: string[];
  importantFiles: string[];
  recentConversation: Array<{ role: 'user' | 'assistant'; text: string }>;
  git?: { branch?: string; commit?: string; dirty?: boolean };
}

export interface MeetingHandoff {
  version: 1;
  meetingId: string;
  startedAt: string;
  endedAt: string;
  summary: string;
  decisions: unknown[];
  requirements: string[];
  actionItems: unknown[];
  unresolvedQuestions: string[];
  filesDiscussed: string[];
  workPerformed: unknown[];
  artifacts: unknown[];
  transcriptPath: string;
  recommendedNextAction: string;
  handoffId?: string;
  partial?: boolean;
  endReason?: string;
  archivePath?: string;
  git?: { branch?: string; commit?: string; dirty?: boolean };
  permissions?: MeetingPermissions;
  approvals?: ApprovalRecord[];
}

export interface ApprovalRecord {
  version?: 1;
  id: string;
  meetingId: string;
  category: string;
  permission?: string;
  summary: string;
  scope?: Record<string, string>;
  status: 'pending' | 'approved' | 'denied' | 'expired' | 'cancelled';
  createdAt: string;
  expiresAt: string;
  resolvedAt?: string;
  decision?: 'approved' | 'denied';
  delegationId?: string;
}

export interface MeetingSession {
  id: string;
  platform: 'zoom' | 'teams' | 'meet';
  meetingUrl: string;
  agentSession: AgentSessionRef;
  context: ContextHandoff;
  permissions: MeetingPermissions;
  state: MeetingState;
  startedAt: string;
  cameraEnabled?: boolean;
  cameraState?: 'off' | 'starting' | 'on' | 'blocked' | 'degraded';
  visualState?: 'joining' | 'listening' | 'working' | 'speaking' | 'finalizing' | 'needs_attention' | 'ended';
  degradedReason?: string;
}

export interface ColleagueEvent {
  version: 1;
  id: string;
  meetingId: string;
  timestamp: string;
  type: string;
  [key: string]: unknown;
}

export interface JoinMeetingRequest {
  url: string;
  agentSession: AgentSessionRef;
  context?: ContextHandoff;
  permissions?: MeetingPermissions;
  camera?: {
    enabled?: boolean;
    defaultOn?: boolean;
    avatarDataUri?: string;
  };
  screenShare?: {
    enabled?: boolean;
    captureIntervalMs?: number;
    minChange?: number;
    maxFrames?: number;
    maxBytes?: number;
    retentionSeconds?: number;
  };
}

export type MeetingEventName = 'state' | 'transcript' | 'delegation' | 'approval' | 'approval_required' | 'workspace' | 'git' | 'artifact' | 'screen_share' | 'event';

export interface DaemonTransport {
  createMeeting(payload: Record<string, unknown>): Promise<MeetingSession>;
  getMeeting(meetingId: string): Promise<MeetingSession>;
  updateContext(meetingId: string, context: ContextHandoff): Promise<MeetingSession>;
  cancelMeeting(meetingId: string): Promise<MeetingSession>;
  getHandoff(meetingId: string): Promise<MeetingHandoff>;
  retryHandoff(meetingId: string): Promise<MeetingHandoff>;
  listApprovals(meetingId: string): Promise<{ approvals: ApprovalRecord[] }>;
  getApproval(meetingId: string, approvalId: string): Promise<ApprovalRecord>;
  decideApproval(meetingId: string, approvalId: string, decision: { decision: 'approved' | 'denied' } | 'approved' | 'denied'): Promise<ApprovalRecord>;
  listArtifacts(meetingId: string): Promise<{ artifacts: Array<Record<string, unknown>> }>;
  getArtifact(meetingId: string, artifactId: string): Promise<Record<string, unknown>>;
  getArtifactContent(meetingId: string, artifactId: string): Promise<{ mediaType: string; body: Buffer }>;
  createCommit(meetingId: string, payload: Record<string, unknown>): Promise<Record<string, unknown>>;
  listCommits(meetingId: string): Promise<{ commits: Array<Record<string, unknown>> }>;
  getCommit(meetingId: string, operationId: string): Promise<Record<string, unknown>>;
  createPush(meetingId: string, payload: Record<string, unknown>): Promise<Record<string, unknown>>;
  listPushes(meetingId: string): Promise<{ pushes: Array<Record<string, unknown>> }>;
  getPush(meetingId: string, operationId: string): Promise<Record<string, unknown>>;
  getScreenShare(meetingId: string): Promise<Record<string, unknown>>;
  pauseScreenShare(meetingId: string): Promise<Record<string, unknown>>;
  resumeScreenShare(meetingId: string): Promise<Record<string, unknown>>;
  listScreenShareObservations(meetingId: string): Promise<{ observations: Array<Record<string, unknown>> }>;
  listProviders(): Promise<{ providers: Array<Record<string, unknown>> }>;
  runnerStatus(): Promise<Record<string, unknown>>;
  pairRunner(payload?: Record<string, unknown>): Promise<Record<string, unknown>>;
  completeRunnerPair(payload: { pairingId: string; pairingCode: string }): Promise<Record<string, unknown>>;
  unpairRunner(): Promise<Record<string, unknown>>;
  events(meetingId: string, options?: { lastEventId?: string; signal?: AbortSignal }): AsyncIterable<ColleagueEvent>;
}

export class ColleagueError extends Error {
  code: string;
  status?: number;
  archivePath?: string;
  handoff?: MeetingHandoff;
}
export class ValidationError extends ColleagueError {}
export class StartupError extends ColleagueError {}
export class RuntimeError extends ColleagueError {}
export class FinalizationError extends ColleagueError {}
export class InterruptError extends ColleagueError {}

export interface MeetingHandle {
  readonly id: string;
  readonly finished: Promise<MeetingHandoff>;
  status(): Promise<MeetingSession>;
  addContext(context: ContextHandoff): Promise<MeetingSession>;
  cancel(): Promise<MeetingSession>;
  retryFinalization(): Promise<MeetingHandoff>;
  listApprovals(): Promise<{ approvals: ApprovalRecord[] }>;
  getApproval(approvalId: string): Promise<ApprovalRecord>;
  decideApproval(approvalId: string, decision: 'approved' | 'denied' | { decision: 'approved' | 'denied' }): Promise<ApprovalRecord>;
  listArtifacts(): Promise<{ artifacts: Array<Record<string, unknown>> }>;
  getArtifact(artifactId: string): Promise<Record<string, unknown>>;
  getArtifactContent(artifactId: string): Promise<{ mediaType: string; body: Buffer }>;
  createCommit(payload: Record<string, unknown>): Promise<Record<string, unknown>>;
  listCommits(): Promise<{ commits: Array<Record<string, unknown>> }>;
  getCommit(operationId: string): Promise<Record<string, unknown>>;
  createPush(payload: Record<string, unknown>): Promise<Record<string, unknown>>;
  listPushes(): Promise<{ pushes: Array<Record<string, unknown>> }>;
  getPush(operationId: string): Promise<Record<string, unknown>>;
  getScreenShare(): Promise<Record<string, unknown>>;
  pauseScreenShare(): Promise<Record<string, unknown>>;
  resumeScreenShare(): Promise<Record<string, unknown>>;
  listScreenShareObservations(): Promise<{ observations: Array<Record<string, unknown>> }>;
  on(event: MeetingEventName, handler: (payload: unknown) => void): () => void;
  events(): AsyncIterable<ColleagueEvent>;
}

export interface ColleagueOptions {
  transport?: DaemonTransport;
  root?: string;
  host?: string;
  port?: number;
}

export class Colleague {
  constructor(options?: ColleagueOptions);
  joinMeeting(request: JoinMeetingRequest): Promise<MeetingHandle>;
  listProviders(): Promise<{ providers: Array<Record<string, unknown>> }>;
  runnerStatus(): Promise<Record<string, unknown>>;
  pairRunner(payload?: Record<string, unknown>): Promise<Record<string, unknown>>;
  completeRunnerPair(payload: { pairingId: string; pairingCode: string }): Promise<Record<string, unknown>>;
  unpairRunner(): Promise<Record<string, unknown>>;
}

export function createLoopbackTransport(options?: {
  root?: string;
  host?: string;
  port?: number;
  fetchImpl?: typeof fetch;
  spawnDaemon?: () => { unref?: () => void; on?: Function };
  isPortOpen?: () => Promise<boolean>;
}): DaemonTransport;

export const EXIT = {
  ok: 0,
  validation: 2,
  startup: 3,
  runtime: 4,
  partial: 5,
  finalization: 6,
  interrupt: 130,
} as const;
