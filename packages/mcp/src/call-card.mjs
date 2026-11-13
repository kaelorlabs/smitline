// The call card: one phone call or meeting shown inside the chat, for clients that support MCP
// Apps (ChatGPT, Claude and others). It says who the call was for and how it went, plays the
// recording when there is a link to one, and holds the transcript. It leaves out what the call
// cost: the agent still reads that in the result. start_call and get_call return it; while the
// call runs, the card follows it by calling get_call itself. Clients without MCP Apps ignore it
// and read the same result as text.
//
// Everything the card shows comes from the call record and is treated as text: it is set with
// textContent, never parsed as HTML.

export const CALL_CARD_URI = 'ui://smitline/call-card-v1.html';
export const CALL_CARD_MIME = 'text/html;profile=mcp-app';

/** Tool _meta that shows the card: the MCP Apps key, and ChatGPT's alias for it. */
export const CALL_CARD_TOOL_META = Object.freeze({
  ui: { resourceUri: CALL_CARD_URI },
  'openai/outputTemplate': CALL_CARD_URI,
  'openai/widgetAccessible': true,
});

/** Tool _meta for the in-call tools the card calls itself, when the user answers from it. */
export const CALL_CARD_ACTION_META = Object.freeze({ 'openai/widgetAccessible': true });

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
  // A note says what happened on the line ("On hold."): nobody said it.
  const who = (speaker) => (speaker === 'agent' ? 'Smitline' : speaker === 'note' ? 'Note' : them);
  const transcript = (Array.isArray(call.transcript) ? call.transcript : [])
    .map((line) => object(line))
    .filter((line) => text(line.text))
    .map((line) => ({ who: who(line.speaker), text: text(line.text) }));
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
  const requests = (live && Array.isArray(call.requests) ? call.requests : [])
    .map((request) => object(request))
    .filter((request) => ['open', 'working'].includes(request.state) && /^r[1-9][0-9]{0,2}$/.test(text(request.id)))
    .map((request) => {
      const added = (Array.isArray(request.thread) ? request.thread : [])
        .map((entry) => object(entry))
        .filter((entry) => entry.from === 'call' && entry.type === 'add' && text(entry.text));
      return {
        id: text(request.id),
        text: text(request.text),
        added: added.length ? text(added[added.length - 1].text) : '',
        askedBy: text(request.askedBy),
      };
    })
    .filter((request) => request.text);

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
    canTell: call.status === 'in_progress',
    recording,
    transcript,
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
`;

const CLIENT = `
const ENDED = new Set(['completed', 'failed', 'canceled']);
const REFRESH_MS = 3000;
const main = document.querySelector('main');
const waiting = new Map();
let nextId = 1;
let call = null;
let timer = null;
let closed = false;
// Whether the user opened the transcript: the card redraws every few seconds and keeps it open.
let transcriptOpen = false;
// What the user is typing in each answer box (a request id, or 'tell'), which ones are being
// sent, and the note under each; the card redraws every few seconds and keeps them.
const drafts = new Map();
const sending = new Set();
const notes = new Map();

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
  try {
    return await ask('tools/call', { name, arguments: args });
  } catch (error) {
    if (window.openai && typeof window.openai.callTool === 'function') return window.openai.callTool(name, args);
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
  const view = callCardView(call);
  const parts = [];
  const head = node('header', 'head');
  const titles = node('div');
  titles.append(node('h1', null, view.title));
  if (view.subtitle) titles.append(node('p', 'sub', view.subtitle));
  head.append(titles, node('span', 'pill ' + view.tone, view.status));
  parts.push(head);
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
  if (view.transcript.length) {
    const box = node('details');
    box.open = transcriptOpen;
    box.addEventListener('toggle', () => { transcriptOpen = box.open; });
    box.append(node('summary', null, 'Transcript · ' + view.transcript.length));
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
    const box = [...main.querySelectorAll('textarea')].find((item) => item.dataset.key === key);
    if (box) {
      box.focus();
      box.setSelectionRange(at[0], at[1]);
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

function show(result) {
  const data = result && typeof result === 'object' && result.structuredContent ? result.structuredContent : result;
  if (!data || typeof data !== 'object' || !/^call-[0-9a-f]{16}$/.test(String(data.id || ''))) return;
  call = data;
  render();
  follow();
}

function follow() {
  clearTimeout(timer);
  if (closed || !call || ENDED.has(call.status)) return;
  timer = setTimeout(refresh, REFRESH_MS);
}

async function refresh() {
  try {
    const result = await callTool('get_call', { callId: call.id });
    if (result && !result.isError) return show(result);
  } catch { /* try again below */ }
  follow();
}

function failure(result) {
  const said = result && Array.isArray(result.content) && result.content[0] && result.content[0].text;
  let message = typeof said === 'string' ? said : '';
  try { message = JSON.parse(message).message || message; } catch { /* plain text */ }
  return 'Not sent' + (message ? ': ' + message.slice(0, 200) : '.');
}

async function send(key, name, args, about) {
  const words = (drafts.get(key) || '').trim();
  if (!words || sending.has(key) || !call) return;
  sending.add(key);
  notes.delete(key);
  render();
  try {
    const result = await callTool(name, { callId: call.id, ...args, text: words });
    if (result && !result.isError) {
      drafts.delete(key);
      // An answered question leaves the card with the next refresh; this line stays instead.
      notes.set(key, {
        text: 'Sent to the call.',
        answered: about ? 'Replied “' + words + '” to “' + about + '”' : '',
      });
    } else {
      notes.set(key, { text: failure(result), bad: true });
    }
  } catch {
    notes.set(key, { text: 'Not sent: this app did not pass it on.', bad: true });
  }
  sending.delete(key);
  render();
  clearTimeout(timer);
  refresh();
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
  box.addEventListener('input', () => { drafts.set(key, box.value); fit(box); });
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
function replies(view) {
  const section = node('section', 'ask');
  for (const [key, note] of notes) {
    if (note.answered && !view.requests.some((request) => request.id === key)) {
      section.append(node('p', 'note sent', note.answered));
    }
  }
  const [request] = view.requests;
  let key = '';
  let parts = null;
  if (request) {
    key = request.id;
    const label = node('div', 'label');
    const waiting = view.requests.length - 1;
    label.append(node('span', null, (request.askedBy || 'They') + ' asks'));
    if (waiting) label.append(node('span', null, waiting + ' more waiting'));
    parts = composer(key, label, 'Your reply', 'reply_to_call_request', { requestId: request.id, type: 'done' }, request.text);
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
    if (note) parts.wrap.append(node('p', note.bad ? 'note bad' : 'note sent', note.text));
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
    if (message.error) waiter.reject(new Error(message.error.message || 'error'));
    else waiter.resolve(message.result);
    return;
  }
  if (message.method === 'ui/notifications/tool-result') show(message.params);
  else if (message.method === 'ui/notifications/host-context-changed') theme(message.params);
  else if (message.method === 'ui/resource-teardown' || message.method === 'ping') {
    if (message.method === 'ui/resource-teardown') { closed = true; clearTimeout(timer); }
    if (message.id !== undefined) post({ jsonrpc: '2.0', id: message.id, result: {} });
  }
});

new ResizeObserver(() => tell('ui/notifications/size-changed', {
  width: document.documentElement.scrollWidth,
  height: document.documentElement.scrollHeight,
})).observe(document.documentElement);

// ChatGPT also says light or dark in its own globals.
if (window.openai) theme({ theme: window.openai.theme });
if (window.openai && window.openai.toolOutput) show(window.openai.toolOutput);
window.addEventListener('openai:set_globals', () => {
  if (window.openai) theme({ theme: window.openai.theme });
  if (window.openai && window.openai.toolOutput) show(window.openai.toolOutput);
});
// Exactly the fields MCP Apps allows here: hosts that check them answer anything else with an
// error, and the card would never get past this step.
ask('ui/initialize', {
  protocolVersion: '2026-01-26',
  appInfo: { name: 'smitline-call-card', version: '1' },
  appCapabilities: { availableDisplayModes: ['inline'] },
}).then((result) => {
  theme(result && result.hostContext);
  tell('ui/notifications/initialized', {});
}).catch(() => { /* not an MCP Apps host: window.openai above, or nothing */ });
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
<main aria-live="polite"><p class="note">Loading the call…</p></main>
<script type="module">
const callCardView = ${callCardView.toString()};
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
