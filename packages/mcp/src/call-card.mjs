// The call card: one phone call or meeting shown inside the chat, for clients that support MCP
// Apps (ChatGPT, Claude and others). It says who the call was for and how it went, plays the
// recording when there is a link to one, and holds the transcript. It leaves out what the call
// cost: the agent still reads that in the result. start_call and get_call return it; while the
// call runs, the card follows it by itself with get_call_update, a tool only the card calls, at
// the pace the call's status sets, and stops once it has ended. Clients without MCP Apps ignore
// it and read the same result as text.
//
// Hosts give the card copies of the call that may be old: the tool result that made the card,
// replayed each time the chat opens, or ChatGPT's toolOutput and saved widgetState. Each record
// carries a version, and the card takes a copy only if it is newer than the one it shows and its
// status is not behind; so a chat reopened after the call shows how it ended, not "Starting".
//
// Everything the card shows comes from the call record and is treated as text: it is set with
// textContent, never parsed as HTML.

// Hosts may keep a copy of the card by its address (ChatGPT asks apps to version it), so the
// version goes up whenever the card's page changes in a way an old copy would get wrong.
export const CALL_CARD_URI = 'ui://smitline/call-card-v4.html';
export const CALL_CARD_MIME = 'text/html;profile=mcp-app';

/** Tool _meta that shows the card: the MCP Apps key, and ChatGPT's alias for it. */
export const CALL_CARD_TOOL_META = Object.freeze({
  ui: { resourceUri: CALL_CARD_URI },
  'openai/outputTemplate': CALL_CARD_URI,
  'openai/widgetAccessible': true,
});

/** Tool _meta for the in-call tools the card calls itself, when the user answers from it. */
export const CALL_CARD_ACTION_META = Object.freeze({ 'openai/widgetAccessible': true });

/** The tool the card follows a call with. Only the card calls it: hosts keep it from the model. */
export const CALL_CARD_UPDATE_TOOL = 'get_call_update';
export const CALL_CARD_UPDATE_META = Object.freeze({
  ui: { visibility: ['app'] },
  'openai/visibility': 'private',
  'openai/widgetAccessible': true,
});

/**
 * How long the card waits before asking for a call again, by status: a few seconds while it
 * starts and rings, a little longer once people talk (the card shows the last line, not every
 * word), and never again once it has ended.
 */
export const CALL_CARD_POLL_MS = Object.freeze({
  queued: 3000,
  connecting: 3000,
  ringing: 3000,
  waiting: 3000,
  in_progress: 5000,
  summarizing: 3000,
  completed: 0,
  failed: 0,
  canceled: 0,
});

// The lines a live call's update carries: the card shows the last one and the count.
const LIVE_TRANSCRIPT_LINES = 3;

/**
 * What get_call_update answers for a call record: { unchanged: true } when the card already has
 * this version, else the record cut to what the card shows. A finished call keeps its whole
 * transcript, for the card's last answer; a live one, its last few lines and the count.
 */
export function callCardUpdate(record, ifVersion) {
  const call = record && typeof record === 'object' ? record : {};
  const status = typeof call.status === 'string' ? call.status : '';
  const version = Number.isInteger(call.version) && call.version > 0 ? call.version : 0;
  const nextPollMs = CALL_CARD_POLL_MS[status] ?? 3000;
  // A record without a version (an older daemon) is never "unchanged": the card gets it whole.
  if (version && ifVersion === version) return { callId: call.id, status, version, unchanged: true, nextPollMs };
  const ended = ['completed', 'failed', 'canceled'].includes(status);
  const object = (value) => (value && typeof value === 'object' && !Array.isArray(value) ? value : null);
  const brief = object(call.brief) || {};
  const result = object(call.result);
  const said = Array.isArray(call.transcript) ? call.transcript : Array.isArray(result?.transcript) ? result.transcript : [];
  const lines = said.map(object).filter((line) => line && typeof line.text === 'string' && line.text.trim())
    .map(({ speaker, text }) => ({ speaker, text }));
  const requests = (Array.isArray(call.requests) ? call.requests : []).map(object).filter(Boolean);
  const update = {
    callId: call.id,
    id: call.id,
    status,
    version,
    nextPollMs,
    channel: call.channel,
    brief: {
      channel: brief.channel,
      to: brief.to,
      ...(object(brief.contact) && typeof brief.contact.name === 'string' ? { contact: { name: brief.contact.name } } : {}),
    },
    answeredAt: call.answeredAt,
    endedAt: call.endedAt,
    error: call.error,
    result: result ? (({ transcript: _whole, ...rest }) => rest)(result) : null,
    cost: call.cost,
    charge: call.charge,
    callUrl: call.callUrl,
    recording: call.recording,
    recordingUrl: call.recordingUrl,
    transcript: ended ? lines : lines.slice(-LIVE_TRANSCRIPT_LINES),
    transcriptCount: lines.length,
    requestCount: requests.length,
  };
  // While the call runs, the user answers its questions from the card.
  if (!ended) {
    update.requests = requests.map((request) => ({
      id: request.id,
      kind: request.kind,
      state: request.state,
      text: request.text,
      askedBy: request.askedBy,
      thread: (Array.isArray(request.thread) ? request.thread : []).map(object).filter(Boolean)
        .map(({ from, type, text }) => ({ from, type, text })),
    }));
  }
  return JSON.parse(JSON.stringify(update)); // without the fields the record does not have
}

/** What the card shows for a call record. Pure: it also runs inside the card. */
export function callCardView(record, now = Date.now()) {
  const STATUS = {
    queued: ['Starting', 'live'],
    connecting: ['Dialing', 'live'],
    ringing: ['Ringing', 'live'],
    waiting: ['Waiting to join', 'live'],
    in_progress: ['On the call', 'live'],
    summarizing: ['Wrapping up', 'live'],
    completed: ['Done', 'done'],
    failed: ['Failed', 'bad'],
    canceled: ['Canceled', 'muted'],
  };
  const OUTCOMES = {
    achieved: 'Got it done',
    partial: 'Partly done',
    not_reached: 'Did not reach them',
    voicemail: 'Left a voicemail',
    declined: 'They declined',
    failed: 'Failed',
    canceled: 'Canceled',
  };
  const ENDED = ['completed', 'failed', 'canceled'];
  const text = (value) => (typeof value === 'string' ? value.trim() : '');
  const object = (value) => (value && typeof value === 'object' && !Array.isArray(value) ? value : {});
  const count = (value) => (typeof value === 'number' && Number.isFinite(value) && value >= 0 ? value : null);
  const clock = (total) => {
    const whole = Math.round(total);
    return `${Math.floor(whole / 60)}:${String(whole % 60).padStart(2, '0')}`;
  };
  const length = (total) => {
    const whole = Math.round(total);
    if (whole < 60) return `${whole} s`;
    return `${Math.floor(whole / 60)} min${whole % 60 ? ` ${whole % 60} s` : ''}`;
  };
  const https = (value) => {
    try {
      const url = new URL(String(value));
      return url.protocol === 'https:' ? url.href : null;
    } catch {
      return null;
    }
  };

  const call = object(record);
  const brief = object(call.brief);
  const result = object(call.result);
  const meeting = (call.channel || brief.channel) === 'meeting';
  const contact = text(object(brief.contact).name);
  const to = text(brief.to);
  const [status, tone] = STATUS[call.status] || [text(call.status) || 'Unknown', 'muted'];
  const answered = Date.parse(call.answeredAt || '');
  const ended = Date.parse(call.endedAt || '');
  let seconds = count(result.durationSeconds);
  if (seconds === null && Number.isFinite(answered)) {
    seconds = Math.max(0, ((Number.isFinite(ended) ? ended : now) - answered) / 1000);
  }

  const recorded = object(call.recording);
  const recording = call.recording && typeof call.recording === 'object'
    ? { url: https(call.recordingUrl), length: count(recorded.seconds) !== null ? clock(recorded.seconds) : '' }
    : null;
  const them = meeting ? 'Meeting' : (contact || 'Them');
  // A finished call's record has its transcript in the result; the card's own update carries
  // the last few lines and how many there are.
  const transcript = (Array.isArray(call.transcript) ? call.transcript : Array.isArray(result.transcript) ? result.transcript : [])
    .map((line) => object(line))
    .filter((line) => text(line.text))
    .map((line) => ({ who: line.speaker === 'agent' ? 'Smitline' : them, text: text(line.text) }));
  const lines = Math.max(count(call.transcriptCount) || 0, transcript.length);
  const details = (Array.isArray(result.details) ? result.details : [])
    .map((item) => object(item))
    .map((item) => ({ label: text(item.label), value: text(item.value == null ? '' : String(item.value)) }))
    .filter((item) => item.label && item.value);
  const lists = [['Decisions', result.decisions], ['Action items', result.actionItems], ['Open questions', result.openQuestions]]
    .map(([title, items]) => ({
      title,
      items: (Array.isArray(items) ? items : [])
        .map((item) => text(typeof item === 'string' ? item : object(item).text))
        .filter(Boolean),
    }))
    .filter((list) => list.items.length);
  // Questions from the call nobody has finished, so the user can answer them from the card
  // while the agent is busy (some apps take no message until the agent's turn ends).
  const live = !ENDED.includes(call.status);
  const asked = (live && Array.isArray(call.requests) ? call.requests : [])
    .map((request) => object(request))
    .filter((request) => /^r[1-9][0-9]{0,2}$/.test(text(request.id)) && text(request.text))
    .map((request) => {
      const thread = (Array.isArray(request.thread) ? request.thread : []).map((entry) => object(entry));
      const added = thread.filter((entry) => entry.from === 'call' && entry.type === 'add' && text(entry.text));
      const done = thread.filter((entry) => entry.from === 'agent' && entry.type === 'done' && text(entry.text));
      return {
        id: text(request.id),
        text: text(request.text),
        added: added.length ? text(added[added.length - 1].text) : '',
        askedBy: text(request.askedBy),
        message: request.kind === 'message',
        open: ['open', 'working'].includes(request.state),
        canceled: request.state === 'canceled',
        answer: done.length ? text(done[done.length - 1].text) : '',
      };
    });
  const requests = asked.filter((request) => request.open && !request.message)
    .map(({ id, text: said, added, askedBy }) => ({ id, text: said, added, askedBy }));
  // Questions that closed while the call runs: answered (by the agent, or from another card) or
  // dropped by the person who asked. The card says so when it was showing one of them.
  const closed = asked.filter((request) => !request.open && !request.message)
    .map(({ id, text: said, askedBy, canceled, answer }) => ({ id, text: said, askedBy, canceled, answer }));
  // Messages people gave for the user ("tell them thanks"): noted, never asked of the user, with
  // the user's reply if they sent one.
  const messages = asked.filter((request) => request.message)
    .map(({ id, text: said, askedBy, answer }) => ({ id, text: said, askedBy, reply: answer }));

  return {
    id: text(call.id),
    title: meeting ? 'Meeting' : `Call to ${contact || to || 'someone'}`,
    subtitle: [
      contact && to ? to : '',
      seconds !== null ? length(seconds) : '',
    ].filter(Boolean).join(' · '),
    status,
    tone,
    live,
    outcome: OUTCOMES[result.outcome] || '',
    summary: text(result.summary) || (call.status === 'failed' ? text(call.error) || 'Something went wrong.' : ''),
    details,
    lists,
    requests,
    closed,
    messages,
    canTell: call.status === 'in_progress',
    recording,
    transcript,
    lines,
    callUrl: https(call.callUrl),
  };
}

// The card's colors, fonts and corners are the host's MCP Apps style variables
// (hostContext.styles, set on :root when the host sends them), so it looks like the app it sits
// in; each falls back to the card's own value for hosts that send none.
const STYLE = `
:root { color-scheme: light dark;
  --fg: var(--color-text-primary, #1d1d1f); --muted: var(--color-text-secondary, #6e6e73);
  --line: var(--color-border-primary, rgba(0, 0, 0, 0.1)); --chip: var(--color-background-secondary, rgba(0, 0, 0, 0.05));
  --live: var(--color-text-success, #0b7a3b); --bad: var(--color-text-danger, #b42318);
  --accent: var(--color-text-info, #2457d6); --ring: var(--color-ring-primary, #2457d6);
  --button: var(--color-background-inverse, #2457d6); --button-text: var(--color-text-inverse, #fff);
  --radius: var(--border-radius-md, 8px); --round: var(--border-radius-full, 999px);
  --strong: var(--font-weight-semibold, 600);
  --small: var(--font-text-xs-size, 12px); --sub: var(--font-text-sm-size, 13px); --title: var(--font-text-lg-size, 16px);
  font-family: var(--font-sans, system-ui, -apple-system, "Segoe UI", Roboto, sans-serif);
  font-size: var(--font-text-md-size, 14px); line-height: var(--font-text-md-line-height, 1.45); }
:root[data-theme="light"] { color-scheme: light; }
:root[data-theme="dark"] { color-scheme: dark;
  --fg: var(--color-text-primary, #f2f2f3); --muted: var(--color-text-secondary, #a1a1a6);
  --line: var(--color-border-primary, rgba(255, 255, 255, 0.14)); --chip: var(--color-background-secondary, rgba(255, 255, 255, 0.08));
  --live: var(--color-text-success, #4ade80); --bad: var(--color-text-danger, #f87171);
  --accent: var(--color-text-info, #8ab4ff); --ring: var(--color-ring-primary, #8ab4ff);
  --button: var(--color-background-inverse, #8ab4ff); --button-text: var(--color-text-inverse, #10131a); }
@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) {
  --fg: var(--color-text-primary, #f2f2f3); --muted: var(--color-text-secondary, #a1a1a6);
  --line: var(--color-border-primary, rgba(255, 255, 255, 0.14)); --chip: var(--color-background-secondary, rgba(255, 255, 255, 0.08));
  --live: var(--color-text-success, #4ade80); --bad: var(--color-text-danger, #f87171);
  --accent: var(--color-text-info, #8ab4ff); --ring: var(--color-ring-primary, #8ab4ff);
  --button: var(--color-background-inverse, #8ab4ff); --button-text: var(--color-text-inverse, #10131a); } }
:root { --radius-lg: var(--border-radius-lg, 12px); }
html, body { margin: 0; background: transparent; color: var(--fg); -webkit-font-smoothing: antialiased; }
main { padding: 16px 18px; display: grid; gap: 16px; }
p { margin: 0; }
button { font: inherit; color: inherit; background: none; border: 0; padding: 0; cursor: pointer; }
.head { display: flex; justify-content: space-between; align-items: center; gap: 12px; }
h1 { font-size: var(--title); font-weight: var(--strong); line-height: 1.3; margin: 0; }
.sub { margin-top: 2px; color: var(--muted); font-size: var(--sub); font-variant-numeric: tabular-nums; }
.pill { flex: none; display: inline-flex; align-items: center; gap: 6px; font-size: var(--small);
  font-weight: var(--strong); padding: 4px 10px; border-radius: var(--round); background: var(--chip); color: var(--muted); }
.pill.live { color: var(--live); } .pill.bad { color: var(--bad); } .pill.done { color: var(--fg); }
.pill.live::before { content: ""; width: 6px; height: 6px; border-radius: 50%; background: currentColor;
  animation: pulse 1.6s ease-in-out infinite; }
@keyframes pulse { 50% { opacity: 0.35; } }
@media (prefers-reduced-motion: reduce) { .pill.live::before { animation: none; } }
.result { display: grid; gap: 4px; }
.outcome { font-weight: var(--strong); }
dl { display: grid; grid-template-columns: fit-content(40%) 1fr; gap: 6px 16px; margin: 0; }
dt { color: var(--muted); overflow-wrap: anywhere; } dd { margin: 0; overflow-wrap: anywhere; }
.group { display: grid; gap: 6px; }
h2 { font-size: var(--sub); font-weight: var(--strong); color: var(--muted); margin: 0; }
ul { margin: 0; padding-left: 18px; display: grid; gap: 2px; }
.ask { display: grid; gap: 8px; }
.label { display: flex; justify-content: space-between; gap: 12px; color: var(--muted); font-size: var(--sub); }
.quote { padding: 10px 12px; border-radius: var(--radius-lg); background: var(--chip); overflow-wrap: anywhere; }
.quote .note { margin-top: 4px; }
.compose { display: flex; align-items: flex-end; gap: 8px; padding: 6px 6px 6px 12px; border: 1px solid var(--line);
  border-radius: var(--radius-lg); transition: border-color 0.15s; }
.compose:focus-within { border-color: var(--ring); }
textarea { box-sizing: border-box; flex: 1; min-width: 0; min-height: 30px; max-height: 120px; resize: none; border: 0; outline: none;
  background: transparent; color: inherit; font: inherit; padding: 4px 0; }
textarea::placeholder { color: var(--muted); }
.send { flex: none; padding: 6px 14px; border-radius: var(--round); background: var(--button);
  color: var(--button-text); font-weight: var(--strong); font-size: var(--sub); transition: opacity 0.15s; }
.send:disabled { opacity: 0.4; cursor: default; }
button:focus-visible, summary:focus-visible { outline: 2px solid var(--ring); outline-offset: 2px; border-radius: var(--radius); }
.note { color: var(--muted); font-size: var(--sub); }
.note.bad { color: var(--bad); }
.note.sent::before { content: "✓ "; color: var(--live); }
.past { display: grid; gap: 2px; padding-left: 10px; border-left: 2px solid var(--line); overflow-wrap: anywhere; }
.by { color: var(--muted); font-size: var(--sub); }
.reply { display: grid; grid-template-columns: auto 1fr; column-gap: 6px; margin-top: 4px; }
.reply::before { content: "↳"; grid-row: span 2; color: var(--muted); }
.reply.mine .by, .by.mine { color: var(--live); }
.told { padding-left: 18px; margin-top: 2px; }
.more { justify-self: start; color: var(--accent); font-weight: var(--strong); font-size: var(--sub); }
audio { width: 100%; height: 36px; }
.foot { display: flex; justify-content: space-between; align-items: flex-start; gap: 16px;
  border-top: 1px solid var(--line); padding-top: 12px; color: var(--muted); font-size: var(--sub); }
details { flex: 1; min-width: 0; }
details[open] { display: grid; gap: 8px; }
.open { flex: none; margin-left: auto; }
summary { cursor: pointer; list-style: none; display: inline-flex; align-items: center; gap: 6px; }
summary::-webkit-details-marker { display: none; }
summary::before { content: ""; width: 6px; height: 6px; border-right: 1.5px solid currentColor;
  border-bottom: 1.5px solid currentColor; transform: rotate(-45deg); transition: transform 0.15s; }
details[open] summary::before { transform: rotate(45deg); }
.line { color: var(--fg); font-size: var(--sub); }
.line strong { font-weight: var(--strong); }
.open { color: var(--accent); font-weight: var(--strong); }
.open:hover { text-decoration: underline; }
.state { display: flex; align-items: baseline; gap: 12px; margin-top: -8px; }
.last { flex: 1; min-width: 0; display: grid; gap: 2px; }
.last .line { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.skeleton { display: grid; gap: 10px; padding: 4px 0; }
.bone { display: block; height: 12px; width: 60%; border-radius: var(--radius); background: var(--chip);
  animation: pulse 1.6s ease-in-out infinite; }
.bone.title { height: 16px; width: 45%; } .bone.short { width: 30%; }
@media (prefers-reduced-motion: reduce) { .bone { animation: none; } }
`;

const CLIENT = `
const ENDED = new Set(['completed', 'failed', 'canceled']);
// A call's statuses in order; the card never moves back along it. The three ends rank the same.
const ORDER = ['queued', 'connecting', 'ringing', 'waiting', 'in_progress', 'summarizing', 'completed', 'failed', 'canceled'];
const rank = (status) => Math.min(ORDER.indexOf(status), 6);
const LIFETIME_MS = 30 * 60 * 1000; // then the card pauses until the user asks it to go on
const GIVE_UP_MS = 2 * 60 * 1000; // failing this long, it says so and offers to try again
const LOAD_MS = 20000; // with no call at all after this, it says so instead of a placeholder
const main = document.querySelector('main');
const waiting = new Map();
let nextId = 1;
// The one call the card shows: the newest copy it has seen (record, with its version and status).
// generation goes up when the card is closed, so an answer that comes back later does nothing.
const state = { callId: null, version: -1, status: null, record: null, generation: 0 };
let closed = false;
let began = false; // whether the card has decided to follow the call, or that it has ended
let fresh = false; // whether the card has heard from the server since it opened
let timer = null;
let inFlight = false;
let again = false; // asked to poll while a poll was out: poll once more when it is back
let asleep = false; // a poll came due while nobody could see the card
let onScreen = true;
let paused = false;
let lifetimeEnds = Date.now() + LIFETIME_MS;
let failures = 0;
let failingSince = null;
let lastGood = null;
let bridge = null; // true once the host answered ui/initialize, false if it never will
const loading = setTimeout(() => { if (!state.callId) gone(); }, LOAD_MS);
// Whether the user opened the transcript: the card redraws every few seconds and keeps it open.
let transcriptOpen = false;
// Whether the user opened the older entries of the thread, which the card keeps open the same way.
let earlierOpen = false;
const RECENT_ENTRIES = 3;
// What the user is typing in each answer box (a request id, or 'tell'), which ones are being
// sent, and the note under each; the card redraws every few seconds and keeps them.
const drafts = new Map();
const sending = new Set();
const notes = new Map();
// Questions the card has shown in its box, and each one that has since closed, with how: the
// user answered it, the agent did, or the person dropped it. A question never just disappears.
const shown = new Set();
const past = new Map();
let told = 0; // runs of notes the user sent with "Tell the call", which join the thread in turn
// Those questions whose typed answer moved to "Tell the call": sent from there, the text goes
// with the question it answers, so the voice can pass it on as the user's own late answer.
let late = [];

function post(message) { window.parent.postMessage(message, '*'); }
function ask(method, params) {
  const id = nextId++;
  post({ jsonrpc: '2.0', id, method, params });
  return new Promise((resolve, reject) => {
    const expiry = setTimeout(() => { if (waiting.delete(id)) reject(new Error('no answer')); }, 20000);
    waiting.set(id, { resolve, reject, expiry });
  });
}
function tell(method, params) { post({ jsonrpc: '2.0', method, params }); }
async function callTool(name, args) {
  if (bridge === null) await ready; // the host takes requests once it has answered ui/initialize
  const direct = window.openai && typeof window.openai.callTool === 'function';
  if (bridge === false && direct) return window.openai.callTool(name, args);
  try {
    return await ask('tools/call', { name, arguments: args });
  } catch (error) {
    // Only when the bridge said nothing: an error it answered with would just come back again.
    if (direct && !error.answered) return window.openai.callTool(name, args);
    throw error;
  }
}

function node(tag, className, content) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (content !== undefined && content !== null) element.textContent = String(content);
  return element;
}

function render() {
  if (!state.record) {
    const line = notice();
    if (line) main.replaceChildren(line);
    return;
  }
  const view = callCardView(state.record);
  main.dataset.status = state.status || '';
  main.removeAttribute('aria-busy');
  const parts = [];
  const head = node('header', 'head');
  const titles = node('div');
  titles.append(node('h1', null, view.title));
  if (view.subtitle) titles.append(node('p', 'sub', view.subtitle));
  // A copy the server has not confirmed yet may be old: say the card is checking, not "Starting".
  const checking = view.live && !fresh;
  head.append(titles, node('span', checking ? 'pill muted' : 'pill ' + view.tone, checking ? 'Checking…' : view.status));
  parts.push(head);
  const line = notice();
  if (line) parts.push(line);
  if (view.outcome || view.summary) {
    const section = node('section', 'result');
    if (view.outcome) section.append(node('p', 'outcome', view.outcome));
    if (view.summary) section.append(node('p', null, view.summary));
    parts.push(section);
  }
  if (view.details.length) {
    const list = node('dl');
    for (const item of view.details) list.append(node('dt', null, item.label), node('dd', null, item.value));
    parts.push(list);
  }
  for (const group of view.lists) {
    const section = node('section', 'group');
    const items = node('ul');
    for (const item of group.items) items.append(node('li', null, item));
    section.append(node('h2', null, group.title), items);
    parts.push(section);
  }
  if (view.live) {
    const section = replies(view);
    if (section.childElementCount) parts.push(section);
  }
  if (view.recording && view.recording.url) {
    const section = node('section', 'group');
    const audio = node('audio');
    audio.controls = true;
    audio.preload = 'none';
    audio.src = view.recording.url;
    section.append(node('h2', null, 'Recording' + (view.recording.length ? ' · ' + view.recording.length : '')), audio);
    parts.push(section);
  }
  const foot = node('footer', 'foot');
  if (view.live && view.transcript.length) {
    // While the call runs: what was said last, and how much has been said.
    const said = view.transcript[view.transcript.length - 1];
    const last = node('div', 'last');
    const line = node('p', 'line');
    line.append(node('strong', null, said.who + ': '), document.createTextNode(said.text));
    last.append(node('p', null, 'Transcript · ' + view.lines), line);
    foot.append(last);
  } else if (view.transcript.length) {
    const box = node('details');
    box.open = transcriptOpen;
    box.addEventListener('toggle', () => { transcriptOpen = box.open; });
    box.append(node('summary', null, 'Transcript · ' + view.lines));
    if (view.lines > view.transcript.length) box.append(node('p', null, 'The last ' + view.transcript.length + ':'));
    for (const line of view.transcript) {
      const said = node('p', 'line');
      said.append(node('strong', null, line.who + ': '), document.createTextNode(line.text));
      box.append(said);
    }
    foot.append(box);
  }
  if (view.callUrl) {
    const open = node('button', 'open', 'Open in Smitline ↗');
    open.type = 'button';
    open.addEventListener('click', () => openLink(view.callUrl));
    foot.append(open);
  }
  if (foot.childElementCount) parts.push(foot);
  // Redrawing replaces the answer boxes: put the cursor back where the user was typing.
  const focused = document.activeElement;
  const key = focused && focused.dataset ? focused.dataset.key : undefined;
  const at = key ? [focused.selectionStart, focused.selectionEnd] : null;
  main.replaceChildren(...parts);
  for (const box of main.querySelectorAll('textarea')) if (box.value) fit(box);
  if (key) {
    const boxes = [...main.querySelectorAll('textarea')];
    const box = boxes.find((item) => item.dataset.key === key);
    // The question being answered closed: carry on in "Tell the call", where its text went.
    const moved = !box && (past.get(key) || {}).elsewhere ? boxes.find((item) => item.dataset.key === 'tell') : null;
    if (box) {
      box.focus();
      box.setSelectionRange(at[0], at[1]);
    } else if (moved) {
      moved.focus();
      moved.setSelectionRange(moved.value.length, moved.value.length);
    }
  }
}

async function openLink(url) {
  try {
    await ask('ui/open-link', { url });
  } catch {
    if (window.openai && typeof window.openai.openExternal === 'function') window.openai.openExternal({ href: url });
  }
}

const CALL_ID = /^call-[0-9a-f]{16}$/;

// The call a tool result, toolOutput or saved state holds: its structuredContent, else the JSON in
// its text (which may follow a line with the call id), else the object itself. {} is nothing.
function recordFrom(data) {
  if (!data || typeof data !== 'object') return null;
  let record = data.structuredContent;
  if (!record && Array.isArray(data.content)) {
    const said = data.content[0] && typeof data.content[0].text === 'string' ? data.content[0].text : '';
    try { record = JSON.parse(said.slice(said.indexOf('{'))); } catch { record = null; }
  }
  if (!record && !('content' in data) && !('structuredContent' in data)) record = data;
  return record && typeof record === 'object' && CALL_ID.test(String(record.id || '')) ? record : null;
}

// Just the call's id, for a result that came without its record.
function idFrom(data) {
  if (!data || typeof data !== 'object') return null;
  const meta = data._meta && typeof data._meta === 'object' ? data._meta.callId : null;
  if (CALL_ID.test(String(meta || ''))) return meta;
  const said = Array.isArray(data.content) && data.content[0] && typeof data.content[0].text === 'string' ? data.content[0].text : '';
  const first = said.split('\\n')[0].trim();
  return CALL_ID.test(first) ? first : null;
}

const versionOf = (record) => (Number.isInteger(record.version) && record.version > 0 ? record.version : 0);

/**
 * Take a copy of the call if it is newer than the one shown (a record without a version, from an
 * older server, counts as version 0) and its status is not behind; true when it was taken. Only
 * a poll's answer may replace a copy of the same version 0: it is the server's call as it is now.
 */
function accept(record, source) {
  if (!record || (state.callId && record.id !== state.callId)) return false;
  const version = versionOf(record);
  if (state.record) {
    const newer = version > state.version || (source === 'poll' && version === 0 && state.version === 0);
    if (!newer || rank(record.status) < rank(state.status)) return false;
  }
  state.callId = record.id;
  state.version = version;
  state.status = record.status;
  state.record = record;
  if (ENDED.has(record.status)) stop();
  save();
  render();
  return true;
}

// What a host handed the card: take its call, then start following it if not yet.
function hydrate(data, source) {
  const record = recordFrom(data);
  if (record) accept(record, source);
  else if (!state.callId && idFrom(data)) state.callId = idFrom(data);
  begin();
}

// Once the card knows its call: an ended one is shown as it ended and never asked for again;
// any other is asked for at once, because what the host handed over may be old.
function begin() {
  if (began || !state.callId || closed) return;
  began = true;
  clearTimeout(loading);
  if (!ENDED.has(state.status)) poll(state.generation);
}

// The card's own copy for ChatGPT to give back when it shows the card again: the call cut down
// to its header, outcome and last line, a few hundred bytes.
function save() {
  if (!window.openai || typeof window.openai.setWidgetState !== 'function' || !state.record) return;
  const call = state.record;
  const result = call.result && typeof call.result === 'object' ? call.result : null;
  const brief = call.brief && typeof call.brief === 'object' ? call.brief : {};
  const clip = (value, size) => (typeof value === 'string' ? value.slice(0, size) : undefined);
  const view = callCardView(call);
  const record = JSON.parse(JSON.stringify({
    id: call.id, status: call.status, version: state.version, channel: call.channel,
    brief: { channel: brief.channel, to: brief.to, contact: brief.contact && typeof brief.contact === 'object' ? { name: clip(brief.contact.name, 80) } : undefined },
    answeredAt: call.answeredAt, endedAt: call.endedAt, error: clip(call.error, 300), callUrl: call.callUrl,
    recording: call.recording, recordingUrl: call.recordingUrl,
    result: result ? {
      outcome: result.outcome, summary: clip(result.summary, 400), durationSeconds: result.durationSeconds,
      details: view.details.slice(0, 6).map((item) => ({ label: item.label.slice(0, 60), value: item.value.slice(0, 120) })),
    } : null,
    transcript: (Array.isArray(call.transcript) ? call.transcript : result && Array.isArray(result.transcript) ? result.transcript : [])
      .slice(-1).map((line) => ({ speaker: line && line.speaker, text: clip(line && line.text, 200) })),
    transcriptCount: view.lines,
  }));
  try {
    window.openai.setWidgetState({ callId: state.callId, version: state.version, status: state.status, record });
  } catch { /* the card works without it */ }
}

function savedState() {
  try {
    const saved = window.openai && window.openai.widgetState;
    if (saved && typeof saved === 'object' && CALL_ID.test(String(saved.callId || '')) && recordFrom(saved.record)) return saved;
  } catch { /* nothing kept */ }
  return null;
}

function canPoll() {
  return !closed && began && Boolean(state.callId) && !ENDED.has(state.status) && !paused;
}

function arm(ms) {
  clearTimeout(timer);
  timer = null;
  if (canPoll()) {
    const generation = state.generation;
    timer = setTimeout(() => poll(generation), ms);
  }
}

// How long the card waits at each status when the server says nothing.
const paceFor = (status) => (Object.prototype.hasOwnProperty.call(PACE, status) ? PACE[status] : 3000);

/** Ask the server for the call once, if anyone can see the card; then arm the next ask. */
async function poll(generation) {
  timer = null;
  if (generation !== state.generation || !canPoll()) return;
  if (document.hidden || !onScreen) {
    asleep = true; // nobody is looking: ask again when someone is
    return;
  }
  if (Date.now() >= lifetimeEnds) {
    paused = true;
    render();
    return;
  }
  if (inFlight) {
    again = true;
    return;
  }
  inFlight = true;
  let answer = null;
  let problem = null;
  try {
    answer = await callTool(UPDATE_TOOL, state.version > 0 ? { callId: state.callId, ifVersion: state.version } : { callId: state.callId });
  } catch (error) {
    problem = error;
  }
  inFlight = false;
  if (generation !== state.generation || closed) return;
  const data = answer && !answer.isError ? (answer.structuredContent || recordFrom(answer)) : null;
  if (!data || typeof data !== 'object' || (!data.unchanged && !recordFrom(data))) return failed(problem || answer);
  const wasFailing = failingSince !== null;
  const wasFresh = fresh;
  failures = 0;
  failingSince = null;
  lastGood = Date.now();
  fresh = true;
  const taken = !data.unchanged && accept(data, 'poll');
  if (!taken && (wasFailing || !wasFresh)) render(); // the note or "Checking…" goes
  if (!canPoll()) return;
  if (again) {
    again = false;
    return poll(generation);
  }
  const hinted = Number.isFinite(data.nextPollMs) ? data.nextPollMs : 0;
  arm(Math.max(hinted, paceFor(state.status)));
}

// Seconds a refusal asks the card to wait, as in "try again in 7 s".
function waitAsked(problem) {
  const said = [];
  if (typeof problem === 'string') said.push(problem);
  else if (problem && typeof problem === 'object') {
    if (typeof problem.message === 'string') said.push(problem.message);
    if (problem.structuredContent && typeof problem.structuredContent.message === 'string') said.push(problem.structuredContent.message);
    for (const part of Array.isArray(problem.content) ? problem.content : []) if (part && typeof part.text === 'string') said.push(part.text);
  }
  const found = said.join(' ').match(/(?:try again|retry) (?:in|after) (\\d+(?:\\.\\d+)?) ?(?:s\\b|sec|second)/i);
  return found ? Number(found[1]) * 1000 : 0;
}

// A poll that failed: the card keeps what it shows, says it is reconnecting, and waits longer
// each time (5, 10, 20, 40 s, then every minute, give or take a little), or as long as it was asked.
function failed(problem) {
  failures += 1;
  if (failingSince === null) failingSince = Date.now();
  const backoff = Math.min(60000, 5000 * 2 ** (failures - 1) * (0.9 + 0.2 * Math.random()));
  render();
  if (again) again = false;
  arm(Math.max(backoff, waitAsked(problem)));
}

/** Ask now: after the user sent something from the card, or pressed Refresh. */
function pollNow() {
  if (!canPoll()) return;
  clearTimeout(timer);
  timer = null;
  asleep = false;
  poll(state.generation);
}

// Someone can see the card again: ask once now if a poll came due meanwhile.
function wake() {
  if (!asleep || document.hidden || !onScreen) return;
  pollNow();
}

function stop() {
  clearTimeout(timer);
  timer = null;
  failingSince = null;
  failures = 0;
}

// The host closed the card, or the page is going away: nothing pending may act after this.
function close() {
  closed = true;
  state.generation += 1;
  stop();
  clearTimeout(loading);
}

function refreshButton() {
  const button = node('button', 'more', 'Refresh');
  button.type = 'button';
  button.addEventListener('click', () => {
    paused = false;
    lifetimeEnds = Date.now() + LIFETIME_MS;
    failures = 0;
    render();
    pollNow();
  });
  return button;
}

// The line under the header when the card is not simply following the call.
function notice() {
  if (ENDED.has(state.status) || !began) return null;
  const line = node('div', 'state');
  if (paused) {
    line.append(node('p', 'note', 'Paused'), refreshButton());
    return line;
  }
  if (failingSince === null) return null;
  if (Date.now() - failingSince >= GIVE_UP_MS) {
    const when = lastGood ? ' · last updated ' + new Date(lastGood).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' }) : '';
    line.append(node('p', 'note bad', 'Couldn’t refresh' + when), refreshButton());
    return line;
  }
  line.append(node('p', 'note', 'Reconnecting…'));
  return line;
}

// No call ever came: say so rather than keep the placeholder up.
function gone() {
  main.removeAttribute('aria-busy');
  main.replaceChildren(node('p', 'note', 'This call can’t be shown here. Ask for it again in the chat.'));
}

function failure(result) {
  const said = result && Array.isArray(result.content) && result.content[0] && result.content[0].text;
  let message = typeof said === 'string' ? said : '';
  try { message = JSON.parse(message).message || message; } catch { /* plain text */ }
  return 'Not sent' + (message ? ': ' + message.slice(0, 200) : '.');
}

async function send(key, name, args, about) {
  const words = (drafts.get(key) || '').trim();
  if (!words || sending.has(key) || !state.record) return;
  const answering = key === 'tell' ? [...late] : [];
  sending.add(key);
  notes.delete(key);
  render();
  try {
    const result = await callTool(name, { callId: state.callId, ...args, text: lateAnswer(answering, words) });
    if (result && !result.isError) {
      drafts.delete(key);
      late = late.filter((question) => !answering.includes(question));
      for (const question of answering) past.get(question.id).late = words;
      // An answered question leaves the card with the next refresh; it shows with the reply instead.
      if (about) {
        past.set(key, { ...about, answer: words, mine: true });
        notes.set(key, { text: 'Sent to the call.' });
      } else if (key === 'tell' && !answering.length) {
        // Notes in a row share one label; the note itself shows there, so no "sent" line.
        const last = [...past.values()].at(-1);
        if (last && last.told) last.told.push(words);
        else past.set('told-' + ++told, { told: [words] });
      }
    } else {
      notes.set(key, { text: failure(result), bad: true });
    }
  } catch {
    notes.set(key, { text: 'Not sent: this app did not pass it on.', bad: true });
  }
  sending.delete(key);
  render();
  pollNow();
}

// The box grows with what is typed, up to about five lines.
function fit(box) {
  box.style.height = 'auto';
  if (box.scrollHeight) box.style.height = box.scrollHeight + 'px';
}

function composer(key, label, placeholder, name, args, about) {
  const form = node('form', 'compose');
  const box = node('textarea');
  box.rows = 1;
  box.maxLength = key === 'tell' ? 2000 : 600;
  box.placeholder = placeholder;
  box.value = drafts.get(key) || '';
  box.dataset.key = key;
  box.setAttribute('aria-label', placeholder);
  box.addEventListener('input', () => {
    drafts.set(key, box.value);
    if (key === 'tell' && !box.value.trim()) late = []; // cleared: what comes next is a new note
    fit(box);
  });
  box.addEventListener('keydown', (event) => {
    if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      send(key, name, args, about);
    }
  });
  const button = node('button', 'send', sending.has(key) ? 'Sending…' : 'Send');
  button.type = 'submit';
  button.disabled = sending.has(key);
  form.addEventListener('submit', (event) => {
    event.preventDefault();
    send(key, name, args, about);
  });
  form.append(box, button);
  const wrap = node('div', 'ask');
  wrap.append(label);
  return { wrap, form, box };
}

// One box at a time, so the card keeps to one main action (ChatGPT's inline cards allow two,
// with "Open in Smitline" the second): the oldest open question, else "Tell the call".
// A question the card showed closed without the user's answer: say how, and keep anything the
// user had typed for it in "Tell the call", so it is never lost.
function settle(view) {
  for (const request of view.closed) {
    if (!shown.has(request.id) || past.has(request.id)) continue;
    past.set(request.id, { ...request, elsewhere: true });
    const typed = (drafts.get(request.id) || '').trim();
    drafts.delete(request.id);
    if (typed) {
      const tell = (drafts.get('tell') || '').trim();
      drafts.set('tell', tell ? tell + '\\n' + typed : typed);
      late.push(request);
      notes.set('tell', { text: 'Your reply, kept. Send it as your answer.', plain: true });
    }
  }
}

function clip(words, size) {
  return words.length > size ? words.slice(0, size - 1) + '…' : words;
}

// What the voice gets for a late answer: the question and what was said meanwhile, then the
// user's words as typed (the tool takes 2000 characters).
function lateAnswer(questions, words) {
  if (!questions.length) return words;
  const about = questions.slice(-3).map((question) => {
    const asked = (question.askedBy ? question.askedBy + '’s' : 'the') + ' question “' + clip(question.text, 160) + '”';
    if (question.canceled) return asked + ', which ' + (question.askedBy || 'they') + ' then dropped';
    return asked + ', already answered' + (question.answer ? ' with “' + clip(question.answer, 160) + '”' : '');
  });
  const lead = 'The user’s own answer, sent late, to ' + about.join('; and to ') + '. Pass it on at the next pause as '
    + 'their answer, saying it just came from them; where it differs from the earlier answer, theirs counts: ';
  return lead + words.slice(0, 2000 - lead.length);
}

// A closed question as a short thread: who asked and what, then how it was answered, and the
// user's own answer if they sent it later.
function earlier(question) {
  if (question.said !== undefined) {
    const message = node('div', 'past');
    message.append(node('p', 'by', (question.askedBy || 'They') + ' said'), node('p', null, question.said));
    if (question.reply) {
      const line = node('p', 'reply mine');
      line.append(node('span', 'by', 'You replied'), node('span', null, question.reply));
      message.append(line);
    }
    return message;
  }
  if (question.told) {
    const group = node('div', 'past');
    group.append(node('p', 'by mine', 'You told the call'));
    for (const words of question.told) group.append(node('p', 'told', words));
    return group;
  }
  const who = question.askedBy || 'They';
  const item = node('div', 'past');
  item.append(node('p', 'by', who + ' asked'), node('p', null, question.text));
  const said = (by, words, mine) => {
    const line = node('p', mine ? 'reply mine' : 'reply');
    line.append(node('span', 'by', by));
    if (words) line.append(node('span', null, words));
    item.append(line);
  };
  if (question.canceled) said(who + ' dropped the question');
  else said(question.mine ? 'You replied' : 'Answered for you', question.answer, question.mine);
  if (question.late) said('You, later', question.late, true);
  return item;
}

function replies(view) {
  settle(view);
  // Messages join the thread as they come, as something said, not something to answer.
  for (const message of view.messages) {
    const seen = past.get(message.id);
    if (seen) seen.reply = message.reply;
    else past.set(message.id, { said: message.text, askedBy: message.askedBy, reply: message.reply });
  }
  const section = node('section', 'ask');
  // The last few entries; the rest open with one click, and stay open.
  const entries = [...past].filter(([id]) => !view.requests.some((request) => request.id === id));
  const hidden = earlierOpen ? 0 : Math.max(0, entries.length - RECENT_ENTRIES);
  if (hidden) {
    const more = node('button', 'more', 'Show ' + hidden + ' earlier');
    more.type = 'button';
    more.addEventListener('click', () => { earlierOpen = true; render(); });
    section.append(more);
  }
  for (const [, question] of entries.slice(hidden)) section.append(earlier(question));
  const [request] = view.requests;
  if (request) shown.add(request.id);
  let key = '';
  let parts = null;
  if (request) {
    key = request.id;
    const label = node('div', 'label');
    const waiting = view.requests.length - 1;
    label.append(node('span', null, (request.askedBy || 'They') + ' asks'));
    if (waiting) label.append(node('span', null, waiting + ' more waiting'));
    parts = composer(key, label, 'Your reply', 'reply_to_call_request', { requestId: request.id, type: 'done' },
      { id: request.id, text: request.text, askedBy: request.askedBy });
    const quote = node('div', 'quote');
    quote.append(node('p', null, request.text));
    if (request.added) quote.append(node('p', 'note', 'Then added: ' + request.added));
    parts.wrap.append(quote);
  } else if (view.canTell) {
    key = 'tell';
    const label = node('div', 'label');
    label.append(node('span', null, 'Tell the call'));
    parts = composer(key, label, 'Something for the voice to say or know', 'send_call_instruction', {});
  }
  if (parts) {
    parts.wrap.append(parts.form);
    const note = notes.get(key);
    if (note) parts.wrap.append(node('p', note.bad ? 'note bad' : note.plain ? 'note' : 'note sent', note.text));
    section.append(parts.wrap);
  }
  return section;
}

// The host's theme, style variables and fonts, as the MCP Apps SDK applies them; whatever it
// leaves out keeps the card's own value.
function theme(context) {
  if (!context || typeof context !== 'object') return;
  const root = document.documentElement;
  if (context.theme === 'dark' || context.theme === 'light') root.dataset.theme = context.theme;
  const styles = context.styles && typeof context.styles === 'object' ? context.styles : {};
  const variables = styles.variables && typeof styles.variables === 'object' ? styles.variables : {};
  for (const [name, value] of Object.entries(variables)) {
    if (/^--[a-z0-9-]{1,60}$/.test(name) && typeof value === 'string' && value.length <= 500) {
      root.style.setProperty(name, value);
    }
  }
  const fonts = styles.css && typeof styles.css.fonts === 'string' ? styles.css.fonts : '';
  if (fonts && fonts.length <= 50000 && !document.getElementById('host-fonts')) {
    const sheet = document.createElement('style');
    sheet.id = 'host-fonts';
    sheet.textContent = fonts;
    document.head.append(sheet);
  }
}

window.addEventListener('message', (event) => {
  if (event.source !== window.parent) return;
  const message = event.data;
  if (!message || message.jsonrpc !== '2.0') return;
  if (message.id !== undefined && !message.method) {
    const waiter = waiting.get(message.id);
    if (!waiter) return;
    waiting.delete(message.id);
    clearTimeout(waiter.expiry);
    if (message.error) waiter.reject(Object.assign(new Error(message.error.message || 'error'), { answered: true }));
    else waiter.resolve(message.result);
    return;
  }
  if (message.method === 'ui/notifications/tool-result') hydrate(message.params, 'host');
  else if (message.method === 'ui/notifications/host-context-changed') theme(message.params);
  else if (message.method === 'ui/resource-teardown' || message.method === 'ping') {
    if (message.method === 'ui/resource-teardown') close();
    if (message.id !== undefined) post({ jsonrpc: '2.0', id: message.id, result: {} });
  }
});
window.addEventListener('pagehide', close);
document.addEventListener('visibilitychange', wake);
window.addEventListener('focus', wake);
// A card scrolled out of sight asks for nothing (document.hidden says only whether the tab is).
if (typeof IntersectionObserver === 'function') {
  new IntersectionObserver((entries) => {
    onScreen = entries.some((entry) => entry.isIntersecting);
    wake();
  }).observe(document.documentElement);
}

new ResizeObserver(() => tell('ui/notifications/size-changed', {
  width: document.documentElement.scrollWidth,
  height: document.documentElement.scrollHeight,
})).observe(document.documentElement);

// Exactly the fields MCP Apps allows here: hosts that check them answer anything else with an
// error, and the card would never get past this step.
const ready = ask('ui/initialize', {
  protocolVersion: '2026-01-26',
  appInfo: { name: 'smitline-call-card', version: '1' },
  appCapabilities: { availableDisplayModes: ['inline'] },
}).then((result) => {
  bridge = true;
  theme(result && result.hostContext);
  tell('ui/notifications/initialized', {});
}).catch(() => { bridge = false; /* not an MCP Apps host: window.openai below, or nothing */ });

// ChatGPT also says light or dark in its own globals, and hands over the tool result and what
// the card saved last time it showed. Either may be old, or {}: accept() keeps the newer.
if (window.openai) {
  theme({ theme: window.openai.theme });
  const delivered = window.openai.toolOutput;
  const id = (recordFrom(delivered) || {}).id || idFrom(delivered);
  const saved = savedState();
  if (saved && (!id || saved.callId === id)) accept(recordFrom(saved.record), 'saved');
  if (delivered) hydrate(delivered, 'host');
  else begin();
}
window.addEventListener('openai:set_globals', () => {
  if (!window.openai) return;
  theme({ theme: window.openai.theme });
  if (window.openai.toolOutput) hydrate(window.openai.toolOutput, 'host');
});
`;

/** The card: one self-contained HTML document (no external scripts, styles or fonts). */
export function callCardHtml() {
  return `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Smitline call</title>
<style>${STYLE}</style>
</head>
<body>
<main aria-live="polite" aria-busy="true"><div class="skeleton" role="status" aria-label="Loading the call">
<span class="bone title"></span><span class="bone"></span><span class="bone short"></span></div></main>
<script type="module">
const callCardView = ${callCardView.toString()};
const PACE = ${JSON.stringify(CALL_CARD_POLL_MS)};
const UPDATE_TOOL = ${JSON.stringify(CALL_CARD_UPDATE_TOOL)};
${CLIENT}
</script>
</body>
</html>
`;
}

/**
 * The card as an MCP resource. mediaOrigins: https origins the card may play recordings from
 * (Smitline Cloud passes its own); anything else is dropped.
 */
export function callCardResource({ mediaOrigins = [] } = {}) {
  const origins = [...new Set((Array.isArray(mediaOrigins) ? mediaOrigins : [])
    .filter((origin) => typeof origin === 'string' && /^https:\/\/[a-z0-9.-]+(:\d{1,5})?$/i.test(origin)))];
  const meta = {
    ui: { prefersBorder: true, csp: { connectDomains: [], resourceDomains: origins } },
    'openai/widgetCSP': { connect_domains: [], resource_domains: origins },
    'openai/widgetDescription': 'A phone call or meeting placed through Smitline: how it went, the recording and the transcript.',
  };
  return {
    descriptor: {
      uri: CALL_CARD_URI,
      name: 'call-card',
      title: 'Call card',
      description: 'Shows one call or meeting: status, result, recording and transcript.',
      mimeType: CALL_CARD_MIME,
      _meta: meta,
    },
    content: { uri: CALL_CARD_URI, mimeType: CALL_CARD_MIME, text: callCardHtml(), _meta: meta },
  };
}
