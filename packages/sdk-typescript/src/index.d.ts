/** Versioned Smitline SDK types mirroring the daemon's calls API. */
export const SCHEMA_VERSION = 1;
export const SDK_VERSION: string;

export class SmitlineError extends Error {
  code: string;
  status?: number;
  archivePath?: string;
  /** Extra fields from the daemon's error, such as `missing` questions for an incomplete brief. */
  details?: Record<string, unknown>;
}
export class ValidationError extends SmitlineError {}
export class StartupError extends SmitlineError {}
export class RuntimeError extends SmitlineError {}
export class FinalizationError extends SmitlineError {}
export class InterruptError extends SmitlineError {}

export function redact(value: unknown): string;

export interface CallContext {
  summary?: string;
  facts?: string[];
  decisions?: string[];
  openQuestions?: string[];
  details?: string;
}

export interface CallBrief {
  /** 'phone' places a call; 'meeting' joins Zoom, Teams, Google Meet or Daily, or starts a room. */
  channel: 'phone' | 'meeting';
  /** E.164 phone number, the meeting invite URL, or "new" for a meeting Smitline starts itself
   * (its link comes back as the call's `to`); for a meeting joined by phone, its dial-in number,
   * also as a one-tap number ("+14388097799,,85951289482#,,,,*123456#"). Omit only for a
   * rehearsal, or a meeting whose `dialIn` has the number. */
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
  /** Phone calls, and meetings with `dialIn`: call outside calling hours where the number rings;
   * only when the user confirms they expect it (for a meeting: that it is on now). */
  afterHours?: boolean;
  /** Phone only: record this call; Smitline keeps the recording, each side on its own channel. */
  record?: boolean;
  /** Phone only: let the assistant press keys in an automated phone menu. Off by default; menus make calls longer and cost more. */
  phoneMenus?: boolean;
  notify?: { webhookUrl?: string };
  /** Meetings only: Smitline's virtual camera tile; the avatar is a PNG, JPEG, WebP, or SVG data: URI of up to 80 KB. */
  camera?: { enabled?: boolean; defaultOn?: boolean; avatarDataUri?: string };
  /** The job this call serves, such as roof repair quotes; id is lowercase letters, digits, and dashes. */
  task?: { id: string; title?: string };
  /** Start with notes from earlier calls: in this task, to the same number (phone only), or named calls (at most 10). */
  carryFrom?: { task?: boolean; contact?: boolean; calls?: string[] };
  /** The request on another call this call serves; it reports its progress there by itself. */
  forRequest?: { callId: string; requestId: string };
  /** What the calling agent does for the people on the call when they ask; requests are off without it. */
  canDo?: string;
  /** Meetings only: joining by phone. The dial-in number, the meeting ID (Teams: phone conference
   * ID; Google Meet: PIN) and Zoom's numeric phone passcode, or the invitation text they come in.
   * With a link in `to`, Smitline dials in when the platform turns its browser away; without one,
   * always. A number that is not the platform's published dial-in number (Zoom's are) keeps
   * phone calling hours and call limits. `keys` (one-tap form, a comma per 2-second pause) is
   * rarely needed. */
  dialIn?: {
    number?: string;
    meetingId?: string;
    passcode?: string;
    platform?: 'zoom' | 'teams' | 'meet';
    keys?: string;
    invite?: string;
  };
}

/** Something a person on the call asked the calling agent for. */
export interface CallRequest {
  id: string;
  kind: 'question' | 'task';
  text: string;
  askedBy?: string;
  /** 'speech': the voice said it passed this on without asking its backend; text is what they said. */
  source?: 'speech';
  /** The last lines said before it was asked. */
  context?: string[];
  state: 'open' | 'working' | 'done' | 'canceled';
  ok?: boolean;
  answer?: string;
  openedAt: string;
  updatedAt: string;
  thread: Array<{ from: 'call' | 'agent'; type: 'ask' | 'add' | 'cancel' | 'status' | 'done'; text?: string; at: string; announce?: boolean; ok?: boolean; details?: string; delivered?: boolean }>;
}

export interface RequestReply {
  type: 'status' | 'done';
  text: string;
  /** status only: the voice says it now. */
  announce?: boolean;
  /** done only: false when it could not be done. */
  ok?: boolean;
  /** Links, code, numbers: a meeting's chat gets them. */
  details?: string;
}

export interface FollowOptions {
  /** '' at first, then live.cursor from the last answer. */
  since?: string;
  /** Wake only for requests, without the transcript. */
  only?: 'requests';
  /** Also watch your other calls in progress: their new requests wake the wait, and
   * live.otherRequests lists the ones you have not answered yet. */
  others?: boolean;
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
  details: Array<{ label: string; value: string }>;
  decisions: string[];
  actionItems: string[];
  openQuestions: string[];
  transcript: Array<{ speaker: string; text: string }>;
  durationSeconds: number;
  source?: string;
  /** True when the person asked not to be called again; their number is now on the do-not-call list. */
  doNotCall?: boolean;
  /** Why the call got nowhere, when Smitline knows: such as a meeting platform that did not let it in from its link. */
  reason?: string;
  /** What to do about it, such as asking the user for the invitation's phone details and starting again with dialIn. */
  hint?: string;
}

/** What checkCall reports: problems stop the call; warnings may keep it from working but do not stop it. */
export interface CallCheck {
  ok: boolean;
  brief: CallBrief;
  problems: string[];
  warnings?: string[];
  carried?: number;
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
  /** Requests from the people on the call (brief.canDo). */
  requests?: CallRequest[];
  /** With since or only: what was said and asked after the cursor. */
  live?: {
    cursor: string;
    transcript?: Array<{ speaker: string; text: string; at?: string }>;
    requests: Array<{ type: 'ask' | 'add' | 'cancel'; id: string; kind?: 'question' | 'task'; text?: string; askedBy?: string; context?: string[]; at?: string }>;
    /** With others: requests on your other calls in progress you have not answered yet. */
    otherRequests?: Array<{ callId: string; channel: 'phone' | 'meeting'; id: string; kind?: 'question' | 'task'; text?: string; askedBy?: string; openedAt?: string }>;
  };
  error?: string;
}

export interface ProfilePerson {
  name: string;
  relationship?: string;
  phone?: string;
  notes?: string;
}

/** The voice each kind of call speaks with when its brief names none. */
export interface VoiceDefaults {
  phone: string;
  meeting: string;
}

export interface Profile {
  version: number;
  about?: string;
  style?: string;
  boundaries?: string[];
  people?: ProfilePerson[];
  voices?: VoiceDefaults;
}

export interface ProfileUpdate {
  about?: string;
  style?: string;
  boundaries?: string[];
  people?: ProfilePerson[];
  removePeople?: string[];
  /** New default voices: a name from listVoices, or '' to go back to the default. */
  voices?: Partial<VoiceDefaults>;
}

export interface Voices {
  /** The phone calls' default, as before there were two: defaults.phone. */
  default: string;
  defaults: VoiceDefaults;
  voices: string[];
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
  checkCall(brief: CallBrief): Promise<CallCheck>;
  startCall(brief: CallBrief): Promise<Call>;
  getCall(callId: string): Promise<Call>;
  waitForCall(callId: string, timeoutSeconds?: number, since?: string, only?: 'requests', others?: boolean): Promise<Call>;
  replyToRequest(callId: string, requestId: string, reply: RequestReply): Promise<{ request: CallRequest; delivered: boolean }>;
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
  listVoices(): Promise<Voices>;
}

export interface SmitlineOptions {
  transport?: DaemonTransport;
  /** Data root holding .env and .smitline/daemon.auth; defaults to SMITLINE_ROOT, then the working directory. */
  root?: string;
  /** Where start-runtime-daemon.sh lives, when it differs from root. */
  codeRoot?: string;
  /** True in the Smitline container (SMITLINE_MANAGED=1): the daemon is never started from here. */
  managed?: boolean;
  host?: string;
  port?: number;
  /** A remote daemon (a hosted deployment, or server mode) instead of the local one. */
  baseUrl?: string;
  /** The bearer token for baseUrl, sent as is; daemon.auth is never read. */
  token?: string;
  /** Extra headers on every request to baseUrl, e.g. X-Smitline-Owner from an internal token. */
  headers?: Record<string, string>;
  /** Start the local daemon when it is not running (default true); never for baseUrl. */
  autostart?: boolean;
}

export class Smitline {
  constructor(options?: SmitlineOptions);
  checkCall(brief: CallBrief): Promise<CallCheck>;
  /** Place a phone call or join a meeting; returns the queued call at once. */
  startCall(brief: CallBrief): Promise<Call>;
  getCall(callId: string): Promise<Call>;
  /** Follow it live with { since }; { only: 'requests' } wakes for requests alone. */
  waitForCall(callId: string, timeoutSeconds?: number, follow?: FollowOptions): Promise<Call>;
  /** Answer a request from the call: status any number of times, then done once. */
  replyToRequest(callId: string, requestId: string, reply: RequestReply): Promise<{ request: CallRequest; delivered: boolean }>;
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
  listVoices(): Promise<Voices>;
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

/** A daemon on another host: a fixed bearer token and extra headers, never started from here. */
export function createRemoteTransport(options: {
  baseUrl: string;
  token: string;
  headers?: Record<string, string>;
  fetchImpl?: typeof fetch;
}): DaemonTransport;

export const MANAGED_NOT_RUNNING: string;
/** True when SMITLINE_MANAGED=1: the Smitline container runs the daemon. */
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

export default Smitline;

/** Names from before the product was called Smitline. */
export { Smitline as Colleague, SmitlineError as ColleagueError, SmitlineOptions as ColleagueOptions };
