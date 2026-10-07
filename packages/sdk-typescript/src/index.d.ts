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
  /** Name spoken in the opening: "Hi, I'm calling on behalf of NAME about…", and in the AI disclosure that follows. Defaults to the name given at setup. */
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
  /** Phone only: call outside calling hours where the person is; only when the user confirms they expect it. */
  afterHours?: boolean;
  /** Phone only: record this call in the owner's phone account; the assistant says it is recorded. */
  record?: boolean;
  notify?: { webhookUrl?: string };
  /** Meetings only: Smitline's virtual camera tile; the avatar is a PNG, JPEG, WebP, or SVG data: URI of up to 80 KB. */
  camera?: { enabled?: boolean; defaultOn?: boolean; avatarDataUri?: string };
  /** The job this call serves, such as roof repair quotes; id is lowercase letters, digits, and dashes. */
  task?: { id: string; title?: string };
  /** Start with notes from earlier calls: in this task, to the same number (phone only), or named calls (at most 10). */
  carryFrom?: { task?: boolean; contact?: boolean; calls?: string[] };
}

/** A note from an earlier call that a call started with. */
export interface CarriedNote {
  callId: string;
  contact: string;
  at?: string;
  text: string;
  by: 'agent' | 'result';
}

export interface Contact {
  number: string;
  name: string;
  notes: string;
  autoContext: boolean;
  calls: number;
  lastCallAt?: string | null;
  lastObjective?: string | null;
}

export interface ContactDetail extends Contact {
  history: Array<{ id: string; direction?: string; status: string; createdAt?: string; objective?: string; task?: { id: string; title?: string } | null; outcome?: string | null; summary?: string | null }>;
  /** The notes a new call to this number would start with when it carries the contact's earlier calls. */
  nextCall: CarriedNote[];
}

export interface ContactUpdate {
  name?: string;
  notes?: string;
  autoContext?: boolean;
}

export interface CallFilters {
  channel?: 'phone' | 'meeting';
  /** E.164 */
  contact?: string;
  task?: string;
}

export type CallStatus =
  | 'queued' | 'connecting' | 'ringing' | 'waiting' | 'in_progress' | 'summarizing'
  | 'completed' | 'failed' | 'canceled';

export interface CallResult {
  outcome: 'achieved' | 'partial' | 'not_reached' | 'voicemail' | 'declined' | 'failed' | 'canceled';
  summary: string;
  details: Array<{ label: string; value: string   /** True when the person asked not to be called again; their number is now on the do-not-call list. */
  doNotCall?: boolean;
}>;
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
  /** The notes of earlier calls this call started with. */
  carried?: CarriedNote[];
  /** A note saved on this finished call for later calls. */
  carryNote?: { text: string; by: 'agent'; at: string };
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

export interface DoNotCallEntry {
  number: string;
  addedAt: string;
  reason?: string;
  callId?: string;
}

export interface DoNotCallUpdate {
  add?: Array<string | { number: string; reason?: string; callId?: string }>;
  remove?: string[];
}

export interface DaemonTransport {
  checkCall(brief: CallBrief): Promise<{ ok: boolean; brief: CallBrief; problems: string[]; carried?: number }>;
  startCall(brief: CallBrief): Promise<Call>;
  getCall(callId: string): Promise<Call>;
  waitForCall(callId: string, timeoutSeconds?: number): Promise<Call>;
  listCalls(limit?: number, filters?: CallFilters): Promise<{ calls: Call[] }>;
  saveCallNote(callId: string, text: string): Promise<Call>;
  listContacts(): Promise<{ contacts: Contact[] }>;
  getContact(number: string): Promise<ContactDetail>;
  updateContact(number: string, changes: ContactUpdate): Promise<ContactDetail>;
  forgetContact(number: string): Promise<{ forgotten: boolean }>;
  instructCall(callId: string, text: string, options?: { silent?: boolean }): Promise<{ delivered: boolean }>;
  getProfile(): Promise<Profile>;
  updateProfile(update: ProfileUpdate): Promise<Profile>;
  getDoNotCall(): Promise<{ numbers: DoNotCallEntry[] }>;
  updateDoNotCall(update: DoNotCallUpdate): Promise<{ numbers: DoNotCallEntry[] }>;
  endCall(callId: string): Promise<Call>;
  transferCall(callId: string): Promise<Record<string, unknown>>;
  downloadRecording(callId: string, options?: { format?: 'wav' | 'mp3' }): Promise<{ contentType: string; data: Uint8Array }>;
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
  checkCall(brief: CallBrief): Promise<{ ok: boolean; brief: CallBrief; problems: string[]; carried?: number }>;
  /** Place a phone call or join a meeting; returns the queued call at once. */
  startCall(brief: CallBrief): Promise<Call>;
  getCall(callId: string): Promise<Call>;
  waitForCall(callId: string, timeoutSeconds?: number): Promise<Call>;
  listCalls(limit?: number, filters?: CallFilters): Promise<Call[]>;
  /** Save a note on a finished call for later calls to start with (brief.carryFrom). */
  saveCallNote(callId: string, text: string): Promise<Call>;
  listContacts(): Promise<Contact[]>;
  getContact(number: string): Promise<ContactDetail>;
  updateContact(number: string, changes: ContactUpdate): Promise<ContactDetail>;
  /** Forget what was saved for a number; its call records stay. */
  forgetContact(number: string): Promise<{ forgotten: boolean }>;
  instructCall(callId: string, text: string, options?: { silent?: boolean }): Promise<{ delivered: boolean }>;
  getProfile(): Promise<Profile>;
  updateProfile(update: ProfileUpdate): Promise<Profile>;
  getDoNotCall(): Promise<{ numbers: DoNotCallEntry[] }>;
  updateDoNotCall(update: DoNotCallUpdate): Promise<{ numbers: DoNotCallEntry[] }>;
  endCall(callId: string): Promise<Call>;
  transferCall(callId: string): Promise<Record<string, unknown>>;
  downloadRecording(callId: string, options?: { format?: 'wav' | 'mp3' }): Promise<{ contentType: string; data: Uint8Array }>;
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
