import {
  Smitline,
  ValidationError,
  StartupError,
  RuntimeError,
  FinalizationError,
  InterruptError,
  redact,
} from '../../sdk-typescript/src/index.mjs';
import { CALL_CARD_TOOL_META, CALL_CARD_URI, callCardResource } from './call-card.mjs';
import { META_SERVER_INFO, META_SUBSCRIPTION_ID, MODERN_PROTOCOL_VERSIONS } from './modern.mjs';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

export const MCP_PROTOCOL_VERSION = '2025-11-25';
export const MCP_SERVER_VERSION = JSON.parse(readFileSync(new URL('../package.json', import.meta.url), 'utf8')).version;
const DEFAULT_SMITLINE_ROOT = fileURLToPath(new URL('../../../', import.meta.url));
const SERVER_INFO = Object.freeze({ name: 'smitline', version: MCP_SERVER_VERSION });
const capabilities = () => ({ tools: { listChanged: false }, resources: { listChanged: false } });
// Modern caching hints (2026-07-28, "Caching"): the tools and the call card are the same for every
// caller, and change only with a new version of Smitline.
const CACHE_HINTS = Object.freeze({ ttlMs: 5 * 60_000, cacheScope: 'public' });

const CALL_ID_SCHEMA = { type: 'string', description: 'Call id returned by start_call, such as call-0123456789abcdef' };

export const BRIEF_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  // to and onBehalfOf can come from setup (owner phone for rehearsals, owner name);
  // when they cannot, the daemon answers brief_incomplete with a question to ask.
  required: ['channel', 'objective'],
  properties: {
    channel: { enum: ['phone', 'meeting'], description: 'phone to place a call; meeting to join a Zoom, Teams, Google Meet or Daily meeting, by link or by phone, or to start a new meeting room (to "new")' },
    to: { type: 'string', description: "E.164 phone number such as +14155550142; the meeting invite URL (Zoom, Teams, Google Meet or Daily; joining by link can take a minute or two, while a hosted service starts a machine for the meeting); \"new\" for a meeting Smitline starts itself, a room people join from a link in any browser with no account, ready in seconds: the call then carries the room's link as to, for you to send them; or, to join a meeting by phone, its dial-in number, also as a one-tap number such as +14388097799,,85951289482#,,,,*123456# (see dialIn). Omit only for a rehearsal, which rings the user's own phone, or a meeting whose dialIn has the number." },
    onBehalfOf: { type: 'string', description: "The person the call is for, normally the user's own name, spoken in the opening: Hi, I'm calling on behalf of NAME about... Never an AI, model, or app name such as ChatGPT or Claude. Defaults to the name given at setup; a hosted service may set it to the account holder." },
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
    rehearsal: { type: 'boolean', description: "Phone only: practice a call to someone else on the user's own phone first, with the user playing that person. Not for calling the user themselves: when they say \"call me\", place a normal call to their number, without rehearsal." },
    record: { type: 'boolean', description: "Phone only: record this call. Smitline keeps the recording with the call, each side on its own channel, and the assistant tells the other person the call is recorded. Only when the user asks for a recording." },
    afterHours: { type: 'boolean', description: 'Phone calls, and meetings with dialIn: call even though it is outside calling hours (8 AM to 9 PM) where the number rings. Set only when the user confirms the person expects a call now, or for a meeting that it is on now and the number is its own.' },
    notify: {
      type: 'object',
      additionalProperties: false,
      properties: { webhookUrl: { type: 'string', description: 'https URL that receives the finished call' } },
    },
    task: {
      type: 'object',
      additionalProperties: false,
      required: ['id'],
      description: 'The job several calls serve, such as getting quotes from ten roofers. Use the same id for each call in it.',
      properties: {
        id: { type: 'string', pattern: '^[a-z0-9][a-z0-9-]{0,63}$', description: 'Lowercase letters, digits, and dashes, such as roof-quotes-oct' },
        title: { type: 'string', maxLength: 120 },
      },
    },
    canDo: {
      type: 'string',
      maxLength: 500,
      description: "What you will do for the people on the call when they ask, such as \"look things up in the repo and edit the docs\". With it, the voice passes their requests to you (live.requests in wait_for_call) and you answer with reply_to_call_request; it also passes on any question it cannot answer itself, and declines other tasks. Omit it and nobody can ask you for anything. Name only what you can do and the user would approve.",
    },
    forRequest: {
      type: 'object',
      additionalProperties: false,
      required: ['callId', 'requestId'],
      description: "When this call does what someone on another call asked (such as phoning Arun because a meeting asked you to): that call's id and the request's id. This call then reports to the request by itself: ringing, picked up, and the outcome or why it failed, so the people who asked hear how it went even while you are busy. You can still reply yourself. If they cancel the request, the call ends: at once if no person has spoken on it yet, otherwise after a short goodbye.",
      properties: {
        callId: CALL_ID_SCHEMA,
        requestId: { type: 'string', pattern: '^r[1-9][0-9]{0,2}$' },
      },
    },
    dialIn: {
      type: 'object',
      additionalProperties: false,
      description: "Meetings: join by phone. Pass the meeting's dial-in details whenever the user has them, or just paste the invitation text as invite: Smitline reads the number, the meeting ID and the passcode from it. Smitline dials in, the phone keys in the ID and passcode, and it takes part by audio, saying out loud that it is an AI. With a link in to, it joins by link and dials in if the platform turns it away (or first, where this service joins that platform by phone); with no link it always dials in. Zoom's own dial-in numbers can be dialed any time; any other number keeps the calling hours and call limits of a phone call.",
      properties: {
        invite: { type: 'string', maxLength: 12000, description: 'The meeting invitation, as pasted: its one-tap lines, dial-in numbers, meeting ID or PIN, and passcode' },
        number: { type: 'string', description: "The meeting's dial-in number in E.164 form, such as +14388097799" },
        meetingId: { type: 'string', description: "What the phone line asks for: Zoom's meeting ID, Teams' phone conference ID (not the Teams meeting ID), or Google Meet's PIN" },
        passcode: { type: 'string', description: "Zoom's numeric phone passcode, printed with the dial-in numbers (not the web passcode). Teams and Meet dial-ins have none." },
        platform: { enum: ['zoom', 'teams', 'meet'], description: 'Whose meeting it is, when neither to nor invite says' },
        keys: { type: 'string', pattern: '^[0-9*#,]{1,64}$', description: 'Rarely needed: the keys to press once the call is answered, as in a one-tap number, a comma for each 2-second pause. Defaults to the platform\'s own timing.' },
      },
    },
    carryFrom: {
      type: 'object',
      additionalProperties: false,
      description: "Start with short notes from earlier calls (at most 10). task: earlier calls in this brief's task; contact: earlier calls to the same number (phone only), or false to skip what the user switched on for that contact; calls: these call ids. Only carry what this call needs: the assistant may mention it to the other party, so do not carry one business's details to another unless the objective calls for it.",
      properties: {
        task: { type: 'boolean' },
        contact: { type: 'boolean' },
        calls: { type: 'array', maxItems: 10, items: CALL_ID_SCHEMA },
      },
    },
  },
};

// readOnlyHint: true is what clients such as Claude and ChatGPT may run without asking. Besides the
// lookups, it marks the in-call tools on purpose (guide, answer a request, end, take over, save a note):
// they only act on a call the user already approved, and asking again mid-call would stall the call.
// start_call and update_profile stay write tools, so they still ask.
export const CALL_TOOL_DEFINITIONS = [
  {
    name: 'start_call',
    title: "Start a phone call or join a meeting",
    _meta: CALL_CARD_TOOL_META,
    annotations: { readOnlyHint: false, destructiveHint: false, idempotentHint: false, openWorldHint: true },
    description: "Place a phone call, join a video meeting, or start a new meeting room for the user. Smitline talks with people in real time using GPT-Live and returns a structured result when the call ends. Write a complete brief: the goal, what to find out (questions), what may be agreed to, and what must not be shared. Pass what you and the user have been working on as context (summary, facts, and long details) so the assistant can answer questions like someone who knows the story. The user's profile (who they are, the people they know) is added automatically; keep it current with update_profile. If anything required is unknown, ask the user instead of guessing. For a first call to someone new, offer a rehearsal on the user's own phone. Returns immediately; then call wait_for_call with since: \"\" to follow the call live and supervise it. With canDo, people on the call can ask you for things; follow the call to answer them.",
    inputSchema: BRIEF_SCHEMA,
  },
  {
    name: 'check_call_brief',
    title: "Check a call before placing it",
    annotations: { readOnlyHint: true, openWorldHint: false },
    description: "Validate a brief exactly as start_call would, without placing a phone call or joining a meeting. Returns ok, the brief as it would be placed, the problems that would stop it (such as missing setup or a refused number), and how many earlier calls' notes it would start with. For a phone call it also tests that the phone provider can reach this computer, starting the tunnel if needed (a few seconds), so a network that blocks it shows up before you dial: worth running before a demo. Missing fields come back as brief_incomplete with a question to ask the user. Use it when unsure whether a brief is complete; start_call runs the same checks itself.",
    inputSchema: BRIEF_SCHEMA,
  },
  {
    name: 'wait_for_call',
    title: "Follow a call live",
    annotations: { readOnlyHint: true, openWorldHint: false },
    description: "Wait for a call to finish and return it with its result: outcome, summary, details such as confirmation numbers, decisions, action items, open questions, and the transcript. If status is not completed, failed, or canceled, call again. Tell the user the outcome in plain words. To follow the call while it happens, pass since: \"\" the first time, then live.cursor from the last answer. It then also returns as soon as something new is said, with live.transcript holding the new lines (speaker agent is Smitline; other or meeting is the people on the call). Step in with send_call_instruction only when you know something the call needs, or end_call if it goes wrong; otherwise keep waiting. When the brief has canDo, it returns at once when someone on the call asks you for something: live.requests holds it (id, kind question or task, text, askedBy, and the last lines said; source: \"speech\" when the text is what the person said, passed on because the voice said it would); answer with reply_to_call_request. For a long meeting pass only: \"requests\" to wake only for requests, without the transcript.",
    inputSchema: {
      type: 'object',
      additionalProperties: false,
      required: ['callId'],
      properties: {
        callId: CALL_ID_SCHEMA,
        timeoutSeconds: { type: 'integer', minimum: 0, maximum: 280, description: 'How long to wait in this request. Default 50.' },
        since: { type: 'string', pattern: '^[0-9]{0,12}$', description: 'Follow the call live: "" the first time, then live.cursor from the last answer.' },
        only: { type: 'string', enum: ['requests'], description: 'Wake only for requests from the call, and leave out the transcript. Implies following live.' },
        others: { type: 'boolean', description: 'While following live, also watch your other calls in progress (default true): a new request on any of them wakes this wait, and live.otherRequests lists every request on them you have not answered yet (callId, id, text). Answer those with reply_to_call_request on their own callId, so a call you are busy with never hides another.' },
      },
    },
  },
  {
    name: 'get_call',
    title: "Get call details",
    _meta: CALL_CARD_TOOL_META,
    annotations: { readOnlyHint: true, openWorldHint: false },
    description: "Get one phone call or meeting and its current status, without waiting: queued, connecting, ringing, waiting, in_progress, summarizing, then completed, failed, or canceled. A meeting joined by its link may stay connecting for a minute or two on a hosted service, while a machine starts for it; a new room starts in seconds. A finished call includes its result (outcome, summary, details, transcript) and what it cost: cost, at your providers' prices, when self-hosted; on a hosted service such as Smitline Cloud, charge, what the service billed for the call. Errors with not_found for an unknown call id. To wait for a call to finish, use wait_for_call instead; to find call ids, use list_calls.",
    inputSchema: { type: 'object', additionalProperties: false, required: ['callId'], properties: { callId: CALL_ID_SCHEMA } },
  },
  {
    name: 'list_calls',
    title: "List calls and meetings",
    annotations: { readOnlyHint: true, openWorldHint: false },
    description: "List recent phone calls and meetings, newest first, with each call's id, status, brief, outcome, and cost (on a hosted service, its charge). Filter by channel (phone or meeting), by contact (calls to or from one E.164 number), or by task id. limit defaults to 20 (at most 100); there is no paging, so narrow with the filters. Use get_call or wait_for_call for one call's full result.",
    inputSchema: {
      type: 'object',
      additionalProperties: false,
      properties: {
        limit: { type: 'integer', minimum: 1, maximum: 100 },
        channel: { enum: ['phone', 'meeting'] },
        contact: { type: 'string', description: 'E.164 number, such as +14155550142' },
        task: { type: 'string', description: 'Task id from a brief' },
      },
    },
  },
  {
    name: 'save_call_note',
    title: "Save notes from a call",
    annotations: { readOnlyHint: true, openWorldHint: false },
    description: "After a call ends, save a short note for later calls, such as the quote, terms, and dates a business gave. Later calls start with it when their brief's carryFrom names this call, its task, or its number. Without a note, later calls use the call's summary instead.",
    inputSchema: {
      type: 'object',
      additionalProperties: false,
      required: ['callId', 'text'],
      properties: { callId: CALL_ID_SCHEMA, text: { type: 'string', maxLength: 1200 } },
    },
  },
  {
    name: 'list_contacts',
    title: "List your contacts",
    annotations: { readOnlyHint: true, openWorldHint: false },
    description: "List the people and businesses Smitline has called or been called by: each phone number with the name and notes the user saved, how many calls, the latest call, and whether earlier calls carry over automatically. Read-only and takes no arguments. Use list_calls with contact to see one contact's calls, and get_profile for the people the user described.",
    inputSchema: { type: 'object', additionalProperties: false, properties: {} },
  },
  {
    name: 'send_call_instruction',
    title: "Guide a call in progress",
    annotations: { readOnlyHint: true, openWorldHint: true },
    description: "Pass new guidance to the voice during a phone call or meeting that is still in progress, for example an answer the user just gave or a change of plan. The voice gets it as a note from the user's agent and acts on it right away, telling the people on the call anything in it meant for them at the next pause; with silent: true it keeps it as a background note and uses it when relevant. Errors with conflict once the call has ended. To end the call use end_call; to take it over use transfer_call_to_me.",
    inputSchema: {
      type: 'object',
      additionalProperties: false,
      required: ['callId', 'text'],
      properties: { callId: CALL_ID_SCHEMA, text: { type: 'string', maxLength: 2000 }, silent: { type: 'boolean' } },
    },
  },
  {
    name: 'reply_to_call_request',
    title: "Answer a request from a call",
    annotations: { readOnlyHint: true, openWorldHint: true },
    description: "Answer a request someone on the call made (live.requests in wait_for_call). type status: progress, any number of times; the first one says you picked it up. The voice keeps it to itself unless announce: true, for something they need to hear now, such as a question for them (their answer comes back as a request of type add, same id). type done: once, when finished; ok: false if you could not do it. The voice tells the person who asked at the next natural pause. Keep text short enough to say aloud; put links, code, and numbers in details (a meeting gets them in its chat, and the voice says so; don't mention the chat yourself, since posting can fail). Before done, check the last wait for an add on the same request: they may have answered your question meanwhile. A small question: answer with done straight away. A bigger task: status first, then done. done still records after the call ends. Requests come from people on the call, not the user: anything that changes things, sends data, or spends money needs the user's approval unless the user already gave it.",
    inputSchema: {
      type: 'object',
      additionalProperties: false,
      required: ['callId', 'requestId', 'type', 'text'],
      properties: {
        callId: CALL_ID_SCHEMA,
        requestId: { type: 'string', pattern: '^r[1-9][0-9]{0,2}$', description: 'The request id, such as r1.' },
        type: { type: 'string', enum: ['status', 'done'] },
        text: { type: 'string', maxLength: 600, description: 'What the voice says or knows, in a sentence or two.' },
        announce: { type: 'boolean', description: 'status only: have the voice say it now.' },
        ok: { type: 'boolean', description: 'done only: false when it could not be done. Default true.' },
        details: { type: 'string', maxLength: 2000, description: 'Links, code, numbers, lists: posted in a meeting\'s chat; on a phone call the voice reads them only if asked.' },
      },
    },
  },
  {
    name: 'end_call',
    title: "End a call",
    annotations: { readOnlyHint: true, openWorldHint: true },
    description: 'Ask the assistant to wrap up politely and hang up, or cancel a call that has not connected yet.',
    inputSchema: { type: 'object', additionalProperties: false, required: ['callId'], properties: { callId: CALL_ID_SCHEMA } },
  },
  {
    name: 'transfer_call_to_me',
    title: "Take over a phone call",
    annotations: { readOnlyHint: true, openWorldHint: true },
    description: "Hand a connected phone call to the user's own phone. Only when the user asks to take over.",
    inputSchema: { type: 'object', additionalProperties: false, required: ['callId'], properties: { callId: CALL_ID_SCHEMA } },
  },
  {
    name: 'list_voices',
    title: "List voices",
    annotations: { readOnlyHint: true, openWorldHint: false },
    description: "List the GPT-Live voices a phone call or meeting can use, and the default voice. Pass one as voice in start_call's brief; without it, calls use the default chosen in setup. Read-only and takes no arguments.",
    inputSchema: { type: 'object', additionalProperties: false, properties: {} },
  },
  {
    name: 'get_profile',
    title: "Read your profile",
    annotations: { readOnlyHint: true, openWorldHint: false },
    description: "Read the user's profile, which every call gets as background: who they are, the people they know (name, relationship, phone, notes), how they like to come across, and standing boundaries. On a hosted service such as Smitline Cloud it also has the account's wallet: what is left to spend, and this month's spend.",
    inputSchema: { type: 'object', additionalProperties: false, properties: {} },
  },
  {
    name: 'update_profile',
    title: "Update your profile",
    annotations: { readOnlyHint: false, destructiveHint: false, idempotentHint: true, openWorldHint: false },
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

export const CALLS_INSTRUCTIONS = "To phone someone, join a meeting, or start a new meeting room for the user, call start_call with a complete brief (ask the user for anything missing, and pass what you have been working on as context), then follow it with wait_for_call until it finishes, and report the outcome. Follow it live: pass since (\"\" the first time, then live.cursor) and read live.transcript as the conversation happens; step in with send_call_instruction when you know something the call needs (a fact, a decision, the user's answer), or end_call if it goes wrong, and otherwise keep waiting. With canDo in the brief, people on the call can ask you for things: they arrive in live.requests and you answer each with reply_to_call_request (status as you work, then done); keep following the call while you work, for example with a subagent, and never stop following it to fix something else. Following live also watches your other calls: requests from them come back in live.otherRequests, so answer them too. When you place a call because someone on a call asked, pass forRequest so it reports to them by itself. Questions the voice cannot answer reach you even outside canDo: answer only with what the user would want these people to know. If what they asked for fails, tell them at once with reply_to_call_request (done, ok: false, and why) instead of retrying quietly. For a phone call use channel 'phone' with an E.164 number as to; to join a Zoom, Teams, Google Meet or Daily meeting use channel 'meeting' with the invite URL as to, or to \"new\" to start a meeting of its own and send people the link the call returns as to. When the user has a meeting's dial-in details, add them as dialIn (the invitation text works): Smitline joins by phone if it cannot join by link, or when it has only the number.";

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
    if (args.since !== undefined && (typeof args.since !== 'string' || !/^[0-9]{0,12}$/.test(args.since))) {
      throw new ValidationError('since must be "" or live.cursor from the last answer');
    }
    if (args.only !== undefined && args.only !== 'requests') throw new ValidationError('only must be "requests"');
    const follow = {};
    if (args.since !== undefined) follow.since = args.since;
    if (args.only !== undefined) follow.only = args.only;
    if (args.others !== undefined && typeof args.others !== 'boolean') throw new ValidationError('others must be true or false');
    // Following live watches the agent's other calls too, unless it says not to.
    if (follow.since !== undefined || follow.only !== undefined) follow.others = args.others !== false;
    return colleague.waitForCall(requireCallId(args), timeout, follow);
  }
  if (name === 'get_call') return colleague.getCall(requireCallId(args));
  if (name === 'list_calls') {
    const filters = Object.fromEntries(['channel', 'contact', 'task'].filter((key) => args[key]).map((key) => [key, args[key]]));
    return { calls: await colleague.listCalls(args.limit || 20, filters) };
  }
  if (name === 'save_call_note') {
    if (typeof args.text !== 'string' || !args.text.trim()) throw new ValidationError('text is required');
    return colleague.saveCallNote(requireCallId(args), args.text);
  }
  if (name === 'list_contacts') return { contacts: await colleague.listContacts() };
  if (name === 'get_profile') return colleague.getProfile();
  if (name === 'update_profile') return colleague.updateProfile(args);
  if (name === 'send_call_instruction') {
    if (typeof args.text !== 'string' || !args.text.trim()) throw new ValidationError('text is required');
    return colleague.instructCall(requireCallId(args), args.text, { silent: args.silent === true });
  }
  if (name === 'reply_to_call_request') {
    if (typeof args.requestId !== 'string' || !/^r[1-9][0-9]{0,2}$/.test(args.requestId)) {
      throw new ValidationError('requestId must be a request id from live.requests, such as r1');
    }
    if (args.type !== 'status' && args.type !== 'done') throw new ValidationError('type must be status or done');
    if (typeof args.text !== 'string' || !args.text.trim()) throw new ValidationError('text is required');
    const reply = { type: args.type, text: args.text };
    for (const key of ['announce', 'ok', 'details']) if (args[key] !== undefined) reply[key] = args[key];
    return colleague.replyToRequest(requireCallId(args), args.requestId, reply);
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
  const createColleague = options.createColleague || (() => new Smitline({
    // Data (daemon.auth) under SMITLINE_ROOT; the daemon launcher stays with this code.
    root: options.root || process.env.SMITLINE_ROOT || DEFAULT_SMITLINE_ROOT,
    codeRoot: DEFAULT_SMITLINE_ROOT,
    host: '127.0.0.1',
    port: options.port || process.env.SMITLINE_DAEMON_PORT,
  }));
  const colleague = options.colleague || createColleague();
  const protocolVersions = options.protocolVersions || null;
  // The call card (call-card.mjs); card.mediaOrigins lets it play recordings from those origins.
  const card = callCardResource(options.card);
  const notifications = [];
  let notify = options.notify || ((message) => { notifications.push(message); });
  let initialized = false;
  let clientInfo = {};

  async function callTool(name, args) {
    const callPayload = await callToolFor(colleague, name, args || {});
    if (callPayload) return toolResult(callPayload);
    throw new ValidationError(`unknown tool: ${name}`);
  }

  /** A tool's result; its failure is a result too (isError), so the agent can read it. */
  async function runTool(name, args) {
    try {
      return await callTool(name, args);
    } catch (error) {
      const mapped = mapToolError(error);
      return toolResult({
        code: mapped.code,
        message: redact(error.message),
        archivePath: error.archivePath,
        ...(error.details ? { details: error.details } : {}),
      }, { isError: true });
    }
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
        capabilities: capabilities(),
        serverInfo: { ...SERVER_INFO },
        instructions: CALLS_INSTRUCTIONS,
      });
    }
    if (method === 'ping') return rpcResult(id, {});
    if (method === 'tools/list') return rpcResult(id, { tools: TOOL_DEFINITIONS });
    if (method === 'resources/list') return rpcResult(id, { resources: [card.descriptor] });
    if (method === 'resources/templates/list') return rpcResult(id, { resourceTemplates: [] });
    if (method === 'resources/read') {
      if (params?.uri !== CALL_CARD_URI) return rpcError(id, -32002, 'resource not found');
      return rpcResult(id, { contents: [card.content] });
    }
    if (method === 'tools/call') return rpcResult(id, await runTool(params?.name, params?.arguments || {}));
    return rpcError(id, -32601, `method not found: ${method}`);
  }

  /**
   * A request in revision 2026-07-28, already checked (modern.mjs): answered on its own, with no
   * session and nothing remembered. Every result says resultType "complete" and names the server
   * in _meta; lists, discovery and the call card carry caching hints. An unknown tool or resource
   * is -32602, and an unknown or removed method (ping, logging/setLevel, resources/subscribe) is
   * -32601, which HTTP answers with 404. Notifications get nothing back.
   */
  async function dispatchModern(message) {
    const { id, method, params } = message;
    if (id === undefined || id === null) return null;
    const complete = (result, meta = {}) => rpcResult(id, {
      resultType: 'complete',
      ...result,
      _meta: { ...meta, [META_SERVER_INFO]: { ...SERVER_INFO } },
    });
    if (method === 'server/discover') {
      return complete({
        supportedVersions: [...MODERN_PROTOCOL_VERSIONS],
        capabilities: capabilities(),
        instructions: CALLS_INSTRUCTIONS,
        ...CACHE_HINTS,
      });
    }
    if (method === 'tools/list') return complete({ tools: TOOL_DEFINITIONS, ...CACHE_HINTS });
    if (method === 'resources/list') return complete({ resources: [card.descriptor], ...CACHE_HINTS });
    if (method === 'resources/templates/list') return complete({ resourceTemplates: [], ...CACHE_HINTS });
    if (method === 'resources/read') {
      if (params?.uri !== CALL_CARD_URI) return rpcError(id, -32602, 'Resource not found', { uri: params?.uri });
      return complete({ contents: [card.content], ...CACHE_HINTS });
    }
    if (method === 'tools/call') {
      const name = params?.name;
      if (!CALL_TOOL_NAMES.has(name)) return rpcError(id, -32602, `Unknown tool: ${String(name).slice(0, 128)}`);
      return complete(await runTool(name, params.arguments || {}));
    }
    if (method === 'subscriptions/listen') {
      // The transport sends the acknowledgment first. Nothing here changes while the server runs,
      // so the subscription ends at once, gracefully, with this result.
      return complete({}, { [META_SUBSCRIPTION_ID]: id });
    }
    return rpcError(id, -32601, `Method not found: ${String(method).slice(0, 128)}`);
  }

  // Calls run in the daemon and outlive this MCP process, so there is nothing to stop here.
  async function shutdown() {
    return {};
  }

  return {
    dispatch,
    dispatchModern,
    shutdown,
    notifications,
    setNotify(fn) { notify = fn; },
    get initialized() { return initialized; },
    get clientInfo() { return clientInfo; },
    log,
  };
}
