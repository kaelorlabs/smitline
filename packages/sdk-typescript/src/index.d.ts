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
}

export interface MeetingSession {
  id: string;
  platform: 'zoom' | 'teams';
  meetingUrl: string;
  agentSession: AgentSessionRef;
  context: ContextHandoff;
  permissions: MeetingPermissions;
  state: MeetingState;
  startedAt: string;
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
}

export type MeetingEventName = 'state' | 'transcript' | 'delegation' | 'approval_required' | 'event';

export interface DaemonTransport {
  createMeeting(payload: Record<string, unknown>): Promise<MeetingSession>;
  getMeeting(meetingId: string): Promise<MeetingSession>;
  updateContext(meetingId: string, context: ContextHandoff): Promise<MeetingSession>;
  cancelMeeting(meetingId: string): Promise<MeetingSession>;
  getHandoff(meetingId: string): Promise<MeetingHandoff>;
  retryHandoff(meetingId: string): Promise<MeetingHandoff>;
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
