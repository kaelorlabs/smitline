/** Versioned Smitline SDK types mirroring the daemon's calls API. */
export const SCHEMA_VERSION = 1;
export const SDK_VERSION: string;

export class ColleagueError extends Error {
  code: string;
  status?: number;
  archivePath?: string;
  /** Extra fields from the daemon's error, such as `missing` questions for an incomplete brief. */
  details?: Record<string, unknown>;
}
export class ValidationError extends ColleagueError {}
export class StartupError extends ColleagueError {}
export class RuntimeError extends ColleagueError {}
export class FinalizationError extends ColleagueError {}
export class InterruptError extends ColleagueError {}

export function redact(value: unknown): string;

export interface CallContext {
  summary?: string;
  facts?: string[];
  decisions?: string[];
  openQuestions?: string[];
  details?: string;
}

export interface CallBrief {
  /** 'phone' places a call; 'meeting' joins Zoom, Teams, or Google Meet. */
  channel: 'phone' | 'meeting';
  /** E.164 phone number, or the meeting invite URL. Omit only for a rehearsal. */
  to?: string;
  /** Name spoken in the opening: "Hi, this is NAME's AI assistant". Defaults to the name given at setup. */
  onBehalfOf?: string;
  objective: string;
  context?: string | CallContext;
  questions?: string[];
  tone?: string;
  contact?: { name: string; relationship?: string; notes?: string };
  mayAgreeTo?: string[];
  mustNotShare?: string[];
  successCriteria?: string;
  language?: string;
  voice?: string;
  maxMinutes?: number;
  rehearsal?: boolean;
  notify?: { webhookUrl?: string };
}

export type CallStatus =
  | 'queued' | 'connecting' | 'ringing' | 'waiting' | 'in_progress' | 'summarizing'
  | 'completed' | 'failed' | 'canceled';

export interface CallResult {
  outcome: 'achieved' | 'partial' | 'not_reached' | 'voicemail' | 'declined' | 'failed' | 'canceled';
  summary: string;
  details: Array<{ label: string; value: string }>;
  decisions: string[];
  actionItems: string[];
  openQuestions: string[];
  transcript: Array<{ speaker: string; text: string }>;
  durationSeconds: number;
  source?: string;
}

export interface Call {
  id: string;
  owner?: string;
  channel: 'phone' | 'meeting';
  direction?: 'outbound' | 'inbound';
  status: CallStatus;
  endReason?: string;
  brief: CallBrief;
  createdAt: string;
  updatedAt?: string;
  answeredAt?: string;
  endedAt?: string;
  line?: Record<string, unknown>;
  result: CallResult | null;
  usage?: Record<string, unknown>;
  error?: string;
}

export interface ProfilePerson {
  name: string;
  relationship?: string;
  phone?: string;
  notes?: string;
}

export interface Profile {
  version: number;
  about?: string;
  style?: string;
  boundaries?: string[];
  people?: ProfilePerson[];
}

export interface ProfileUpdate {
  about?: string;
  style?: string;
  boundaries?: string[];
  people?: ProfilePerson[];
  removePeople?: string[];
}

export interface DaemonTransport {
  checkCall(brief: CallBrief): Promise<{ ok: boolean; brief: CallBrief; problems: string[] }>;
  startCall(brief: CallBrief): Promise<Call>;
  getCall(callId: string): Promise<Call>;
  waitForCall(callId: string, timeoutSeconds?: number): Promise<Call>;
  listCalls(limit?: number): Promise<{ calls: Call[] }>;
  instructCall(callId: string, text: string, options?: { silent?: boolean }): Promise<{ delivered: boolean }>;
  getProfile(): Promise<Profile>;
  updateProfile(update: ProfileUpdate): Promise<Profile>;
  endCall(callId: string): Promise<Call>;
  transferCall(callId: string): Promise<Record<string, unknown>>;
  listVoices(): Promise<{ default: string; voices: string[] }>;
}

export interface ColleagueOptions {
  transport?: DaemonTransport;
  /** Data root holding .env and .colleague/daemon.auth; defaults to COLLEAGUE_ROOT, then the working directory. */
  root?: string;
  /** Where start-runtime-daemon.sh lives, when it differs from root. */
  codeRoot?: string;
  /** True in the Smitline container (COLLEAGUE_MANAGED=1): the daemon is never started from here. */
  managed?: boolean;
  host?: string;
  port?: number;
}

export class Colleague {
  constructor(options?: ColleagueOptions);
  checkCall(brief: CallBrief): Promise<{ ok: boolean; brief: CallBrief; problems: string[] }>;
  /** Place a phone call or join a meeting; returns the queued call at once. */
  startCall(brief: CallBrief): Promise<Call>;
  getCall(callId: string): Promise<Call>;
  waitForCall(callId: string, timeoutSeconds?: number): Promise<Call>;
  listCalls(limit?: number): Promise<Call[]>;
  instructCall(callId: string, text: string, options?: { silent?: boolean }): Promise<{ delivered: boolean }>;
  getProfile(): Promise<Profile>;
  updateProfile(update: ProfileUpdate): Promise<Profile>;
  endCall(callId: string): Promise<Call>;
  transferCall(callId: string): Promise<Record<string, unknown>>;
  listVoices(): Promise<{ default: string; voices: string[] }>;
}

export function createLoopbackTransport(options?: {
  root?: string;
  codeRoot?: string;
  managed?: boolean;
  host?: string;
  port?: number;
  autostart?: boolean;
  startupTimeoutMs?: number;
  fetchImpl?: typeof fetch;
  readAuth?: () => Promise<string>;
  spawnDaemon?: ((options: { root: string; codeRoot: string; host: string; port: number }) => { unref?: () => void }) | null;
  isPortOpen?: (host: string, port: number) => Promise<boolean>;
}): DaemonTransport;

export const MANAGED_NOT_RUNNING: string;
/** True when COLLEAGUE_MANAGED=1: the Smitline container runs the daemon. */
export function isManaged(env?: Record<string, string | undefined>): boolean;

export const EXIT: {
  readonly ok: 0;
  readonly validation: 2;
  readonly startup: 3;
  readonly runtime: 4;
  readonly partial: 5;
  readonly finalization: 6;
  readonly interrupt: 130;
};

export default Colleague;
