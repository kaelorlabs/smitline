import {
  Colleague,
  ValidationError,
  StartupError,
  RuntimeError,
  FinalizationError,
  InterruptError,
  redact,
} from '../../sdk-typescript/src/index.mjs';
import { fileURLToPath } from 'node:url';

export const MCP_PROTOCOL_VERSION = '2025-06-18';
export const MCP_SERVER_VERSION = '1.0.0';
const DEFAULT_COLLEAGUE_ROOT = fileURLToPath(new URL('../../../', import.meta.url));

const CALL_ID_SCHEMA = { type: 'string', description: 'Call id returned by start_call, such as call-0123456789abcdef' };

export const BRIEF_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  // to and onBehalfOf can come from setup (owner phone for rehearsals, owner name);
  // when they cannot, the daemon answers brief_incomplete with a question to ask.
  required: ['channel', 'objective'],
  properties: {
    channel: { enum: ['phone', 'meeting'], description: 'phone to place a call; meeting to join Zoom, Teams, or Google Meet' },
    to: { type: 'string', description: "E.164 phone number such as +14155550142, or the meeting invite URL. Omit only for a rehearsal, which rings the user's own phone." },
    onBehalfOf: { type: 'string', description: "The user's name, spoken in the opening: Hi, this is NAME's AI assistant. Defaults to the name given at setup." },
    objective: { type: 'string', description: 'What the call must achieve, in one or two sentences' },
    context: {
      description: "What you and the user have been working on that the other party may ask about. Text, or an object: summary (a few sentences), facts, decisions, openQuestions, and details (long reference material). The voice starts with a short version and looks details up when asked; it never recites them.",
      anyOf: [
        { type: 'string', maxLength: 6000 },
        {
          type: 'object',
          additionalProperties: false,
          properties: {
            summary: { type: 'string', maxLength: 4000 },
            facts: { type: 'array', maxItems: 40, items: { type: 'string', maxLength: 400 } },
            decisions: { type: 'array', maxItems: 20, items: { type: 'string', maxLength: 400 } },
            openQuestions: { type: 'array', maxItems: 20, items: { type: 'string', maxLength: 400 } },
            details: { type: 'string', maxLength: 24000 },
          },
        },
      ],
    },
    questions: { type: 'array', maxItems: 20, items: { type: 'string', maxLength: 300 }, description: 'What to find out on the call. The result answers each one.' },
    tone: { type: 'string', maxLength: 200, description: 'How to come across, such as "casual, he is a close friend". Defaults to matching the relationship.' },
    contact: {
      type: 'object',
      additionalProperties: false,
      required: ['name'],
      description: "Who is being called, when the user's profile does not already know this number.",
      properties: {
        name: { type: 'string', maxLength: 80 },
        relationship: { type: 'string', maxLength: 120 },
        notes: { type: 'string', maxLength: 600 },
      },
    },
    mayAgreeTo: { type: 'array', items: { type: 'string' }, description: 'What the assistant may agree to without checking back, such as acceptable times or prices' },
    mustNotShare: { type: 'array', items: { type: 'string' }, description: 'Information the assistant must never share' },
    successCriteria: { type: 'string', description: 'How to tell the call succeeded' },
    language: { type: 'string', description: 'Language tag such as en or es' },
    voice: { type: 'string', description: 'GPT-Live voice name; see list_voices' },
    maxMinutes: { type: 'integer', minimum: 1, maximum: 240, description: 'Phone calls: at most 60 (default 10). Meetings: at most 240 (default 120).' },
    rehearsal: { type: 'boolean', description: "Phone only: practice on the user's own phone first, with the user playing the other side" },
    notify: {
      type: 'object',
      additionalProperties: false,
      properties: { webhookUrl: { type: 'string', description: 'https URL that receives the finished call' } },
    },
  },
};

export const CALL_TOOL_DEFINITIONS = [
  {
    name: 'start_call',
    description: "Place a phone call or join a video meeting for the user. Colleague AI talks with people in real time using GPT-Live and returns a structured result when the call ends. Write a complete brief: the goal, what to find out (questions), what may be agreed to, and what must not be shared. Pass what you and the user have been working on as context (summary, facts, and long details) so the assistant can answer questions like someone who knows the story. The user's profile (who they are, the people they know) is added automatically; keep it current with update_profile. If anything required is unknown, ask the user instead of guessing. For a first call to someone new, offer a rehearsal on the user's own phone. Returns immediately; then call wait_for_call.",
    inputSchema: BRIEF_SCHEMA,
  },
  {
    name: 'check_call_brief',
    description: 'Validate a brief and report missing fields or configuration without placing the call. Missing fields come with a question to ask the user.',
    inputSchema: BRIEF_SCHEMA,
  },
  {
    name: 'wait_for_call',
    description: 'Wait for a call to finish and return it with its result: outcome, summary, details such as confirmation numbers, decisions, action items, open questions, and the transcript. If status is not completed, failed, or canceled, call again. Tell the user the outcome in plain words.',
    inputSchema: {
      type: 'object',
      additionalProperties: false,
      required: ['callId'],
      properties: {
        callId: CALL_ID_SCHEMA,
        timeoutSeconds: { type: 'integer', minimum: 0, maximum: 280, description: 'How long to wait in this request. Default 50.' },
      },
    },
  },
  {
    name: 'get_call',
    description: 'Get a call and its current status without waiting.',
    inputSchema: { type: 'object', additionalProperties: false, required: ['callId'], properties: { callId: CALL_ID_SCHEMA } },
  },
  {
    name: 'list_calls',
    description: 'List recent calls, newest first.',
    inputSchema: {
      type: 'object',
      additionalProperties: false,
      properties: { limit: { type: 'integer', minimum: 1, maximum: 100 } },
    },
  },
  {
    name: 'send_call_instruction',
    description: 'Give the assistant new guidance during a call in progress, for example an answer the user just provided. With silent: true it is a background note the assistant uses when relevant, instead of acting on it now.',
    inputSchema: {
      type: 'object',
      additionalProperties: false,
      required: ['callId', 'text'],
      properties: { callId: CALL_ID_SCHEMA, text: { type: 'string', maxLength: 2000 }, silent: { type: 'boolean' } },
    },
  },
  {
    name: 'end_call',
    description: 'Ask the assistant to wrap up politely and hang up, or cancel a call that has not connected yet.',
    inputSchema: { type: 'object', additionalProperties: false, required: ['callId'], properties: { callId: CALL_ID_SCHEMA } },
  },
  {
    name: 'transfer_call_to_me',
    description: "Hand a connected phone call to the user's own phone. Only when the user asks to take over.",
    inputSchema: { type: 'object', additionalProperties: false, required: ['callId'], properties: { callId: CALL_ID_SCHEMA } },
  },
  {
    name: 'list_voices',
    description: 'List the GPT-Live voices available for calls.',
    inputSchema: { type: 'object', additionalProperties: false, properties: {} },
  },
  {
    name: 'get_profile',
    description: "Read the user's profile, which every call gets as background: who they are, the people they know (name, relationship, phone, notes), how they like to come across, and standing boundaries.",
    inputSchema: { type: 'object', additionalProperties: false, properties: {} },
  },
  {
    name: 'update_profile',
    description: "Update the user's profile with what you know about them. Fields you pass replace the saved ones; people are added or updated by name; removePeople drops people. Never put passwords, keys, or payment details here.",
    inputSchema: {
      type: 'object',
      additionalProperties: false,
      properties: {
        about: { type: 'string', maxLength: 2000, description: 'Who the user is: role, work, what they are building.' },
        style: { type: 'string', maxLength: 600, description: 'How the user likes to come across on calls.' },
        boundaries: { type: 'array', maxItems: 20, items: { type: 'string', maxLength: 300 } },
        people: {
          type: 'array',
          maxItems: 100,
          items: {
            type: 'object',
            additionalProperties: false,
            required: ['name'],
            properties: {
              name: { type: 'string', maxLength: 80 },
              relationship: { type: 'string', maxLength: 120 },
              phone: { type: 'string', description: 'E.164, such as +14155550142' },
              notes: { type: 'string', maxLength: 600 },
            },
          },
        },
        removePeople: { type: 'array', items: { type: 'string' } },
      },
    },
  },
];

export const TOOL_DEFINITIONS = CALL_TOOL_DEFINITIONS;

const CALL_TOOL_NAMES = new Set(CALL_TOOL_DEFINITIONS.map((tool) => tool.name));

export const CALLS_INSTRUCTIONS = "To phone someone or join a meeting for the user, call start_call with a complete brief (ask the user for anything missing, and pass what you have been working on as context), then wait_for_call until the call finishes, and report the outcome. For a phone call use channel 'phone' with an E.164 number as to; to join a Zoom, Teams, or Google Meet meeting use channel 'meeting' with the invite URL as to.";

function requireCallId(args) {
  if (typeof args.callId !== 'string' || !/^call-[0-9a-f]{16}$/.test(args.callId)) {
    throw new ValidationError('callId must be a call id returned by start_call');
  }
  return args.callId;
}

/** Run a call tool; returns null for names that are not call tools. */
export async function callToolFor(colleague, name, args) {
  if (!CALL_TOOL_NAMES.has(name)) return null;
  if (name === 'start_call') return colleague.startCall(args);
  if (name === 'check_call_brief') return colleague.checkCall(args);
  if (name === 'wait_for_call') {
    const timeout = args.timeoutSeconds === undefined ? 50 : args.timeoutSeconds;
    return colleague.waitForCall(requireCallId(args), timeout);
  }
  if (name === 'get_call') return colleague.getCall(requireCallId(args));
  if (name === 'list_calls') return { calls: await colleague.listCalls(args.limit || 20) };
  if (name === 'get_profile') return colleague.getProfile();
  if (name === 'update_profile') return colleague.updateProfile(args);
  if (name === 'send_call_instruction') {
    if (typeof args.text !== 'string' || !args.text.trim()) throw new ValidationError('text is required');
    return colleague.instructCall(requireCallId(args), args.text, { silent: args.silent === true });
  }
  if (name === 'end_call') return colleague.endCall(requireCallId(args));
  if (name === 'transfer_call_to_me') return colleague.transferCall(requireCallId(args));
  if (name === 'list_voices') return colleague.listVoices();
  return null;
}

function stripSecrets(value) {
  if (Array.isArray(value)) return value.map(stripSecrets);
  if (!value || typeof value !== 'object') return value;
  const out = {};
  for (const [key, item] of Object.entries(value)) {
    if (/secret|token|password|credential|authorization|apiKey|cookie/i.test(key)) continue;
    out[key] = stripSecrets(item);
  }
  return out;
}

function toolResult(payload, { isError = false } = {}) {
  const safe = stripSecrets(payload);
  return {
    content: [{ type: 'text', text: JSON.stringify(safe) }],
    structuredContent: safe,
    isError,
  };
}

function rpcError(id, code, message, data) {
  return {
    jsonrpc: '2.0',
    id,
    error: {
      code,
      message: redact(message),
      ...(data ? { data: stripSecrets(data) } : {}),
    },
  };
}

function rpcResult(id, result) {
  return { jsonrpc: '2.0', id, result };
}

function mapToolError(error) {
  const code = error?.code;
  if (error instanceof ValidationError || code === 'validation') return { code: 'validation', status: 2, error };
  if (error instanceof StartupError || code === 'startup' || code === 'daemon_unavailable' || code === 'supervisor_unavailable') {
    return { code: 'startup', status: 3, error };
  }
  if (error instanceof InterruptError || code === 'interrupt') return { code: 'interrupt', status: 130, error };
  if (error instanceof FinalizationError || code === 'finalization') return { code: 'finalization', status: 6, error };
  if (error instanceof RuntimeError) return { code: 'runtime', status: 4, error };
  return { code: code || 'runtime', status: 4, error };
}

export function createMcpSession(options = {}) {
  const log = options.log || ((line) => {
    process.stderr.write(`${redact(line)}\n`);
  });
  const createColleague = options.createColleague || (() => new Colleague({
    // Data (daemon.auth) under COLLEAGUE_ROOT; the daemon launcher stays with this code.
    root: options.root || process.env.COLLEAGUE_ROOT || DEFAULT_COLLEAGUE_ROOT,
    codeRoot: DEFAULT_COLLEAGUE_ROOT,
    host: '127.0.0.1',
    port: options.port || process.env.COLLEAGUE_DAEMON_PORT,
  }));
  const colleague = options.colleague || createColleague();
  const protocolVersions = options.protocolVersions || null;
  const notifications = [];
  let notify = options.notify || ((message) => { notifications.push(message); });
  let initialized = false;
  let clientInfo = {};

  async function callTool(name, args) {
    const callPayload = await callToolFor(colleague, name, args || {});
    if (callPayload) return toolResult(callPayload);
    throw new ValidationError(`unknown tool: ${name}`);
  }

  async function dispatch(message) {
    if (!message || message.jsonrpc !== '2.0') {
      return rpcError(message?.id ?? null, -32600, 'invalid JSON-RPC message');
    }
    const { id, method, params } = message;
    if (method && method.startsWith('notifications/')) {
      if (method === 'notifications/initialized') initialized = true;
      return null;
    }
    if (method === 'initialize') {
      clientInfo = params?.clientInfo || {};
      initialized = true;
      const requested = params?.protocolVersion;
      const protocolVersion = protocolVersions
        ? (protocolVersions.includes(requested) ? requested : protocolVersions[0])
        : requested || MCP_PROTOCOL_VERSION;
      return rpcResult(id, {
        protocolVersion,
        capabilities: { tools: { listChanged: false } },
        serverInfo: { name: 'colleague-ai', version: MCP_SERVER_VERSION },
        instructions: CALLS_INSTRUCTIONS,
      });
    }
    if (method === 'ping') return rpcResult(id, {});
    if (method === 'tools/list') return rpcResult(id, { tools: TOOL_DEFINITIONS });
    if (method === 'tools/call') {
      try {
        const result = await callTool(params?.name, params?.arguments || {});
        return rpcResult(id, result);
      } catch (error) {
        const mapped = mapToolError(error);
        return rpcResult(id, toolResult({
          code: mapped.code,
          message: redact(error.message),
          archivePath: error.archivePath,
          ...(error.details ? { details: error.details } : {}),
        }, { isError: true }));
      }
    }
    return rpcError(id, -32601, `method not found: ${method}`);
  }

  // Calls run in the daemon and outlive this MCP process, so there is nothing to stop here.
  async function shutdown() {
    return {};
  }

  return {
    dispatch,
    shutdown,
    notifications,
    setNotify(fn) { notify = fn; },
    get initialized() { return initialized; },
    get clientInfo() { return clientInfo; },
    log,
  };
}
