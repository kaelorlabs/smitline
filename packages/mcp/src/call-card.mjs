// The call card: one phone call or meeting shown inside the chat, for clients that support MCP
// Apps (ChatGPT, Claude and others). It says who the call was for and how it went, what it
// cost, plays the recording when there is a link to one, and holds the transcript. start_call
// and get_call return it; while the call runs, the card follows it by calling get_call itself.
// Clients without MCP Apps ignore it and read the same result as text.
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

  let cost = '';
  const charge = object(call.charge);
  if (charge.status === 'pending') {
    const price = text(charge.pricePerMinute);
    cost = price ? `Billed when the call ends, at ${price}/min` : 'Worked out when the call ends';
  } else if (text(charge.amount)) {
    const billed = count(charge.billedSeconds);
    const price = text(charge.pricePerMinute);
    cost = [text(charge.amount), billed !== null ? `${length(billed)}${price ? ` at ${price}/min` : ''}` : '']
      .filter(Boolean).join(' · ');
  } else if (typeof object(call.cost).total === 'number' && Number.isFinite(call.cost.total)) {
    cost = `$${call.cost.total.toFixed(2)} at your providers' prices${call.cost.estimated ? ', estimated' : ''}`;
  }

  const recorded = object(call.recording);
  const recording = call.recording && typeof call.recording === 'object'
    ? { url: https(call.recordingUrl), length: count(recorded.seconds) !== null ? clock(recorded.seconds) : '' }
    : null;
  const them = meeting ? 'Meeting' : (contact || 'Them');
  const transcript = (Array.isArray(call.transcript) ? call.transcript : [])
    .map((line) => object(line))
    .filter((line) => text(line.text))
    .map((line) => ({ who: line.speaker === 'agent' ? 'Smitline' : them, text: text(line.text) }));
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

  return {
    id: text(call.id),
    title: meeting ? 'Meeting' : `Call to ${contact || to || 'someone'}`,
    subtitle: [
      text(brief.onBehalfOf) ? `for ${text(brief.onBehalfOf)}` : '',
      contact && to ? to : '',
      seconds !== null ? length(seconds) : '',
    ].filter(Boolean).join(' · '),
    status,
    tone,
    live: !ENDED.includes(call.status),
    outcome: OUTCOMES[result.outcome] || '',
    summary: text(result.summary) || (call.status === 'failed' ? text(call.error) || 'Something went wrong.' : ''),
    details,
    lists,
    cost,
    recording,
    transcript,
    callUrl: https(call.callUrl),
  };
}

const STYLE = `
:root { color-scheme: light dark; --fg: #1d1d1f; --muted: #6e6e73; --line: rgba(0, 0, 0, 0.1);
  --chip: rgba(0, 0, 0, 0.05); --live: #0b7a3b; --bad: #b42318; --accent: #2457d6;
  font: 14px/1.45 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; }
:root[data-theme="dark"] { --fg: #f2f2f3; --muted: #a1a1a6; --line: rgba(255, 255, 255, 0.14);
  --chip: rgba(255, 255, 255, 0.08); --live: #4ade80; --bad: #f87171; --accent: #8ab4ff; }
@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) { --fg: #f2f2f3; --muted: #a1a1a6;
  --line: rgba(255, 255, 255, 0.14); --chip: rgba(255, 255, 255, 0.08); --live: #4ade80; --bad: #f87171;
  --accent: #8ab4ff; } }
html, body { margin: 0; background: transparent; color: var(--fg); }
main { padding: 14px 16px 16px; display: grid; gap: 12px; }
.head { display: flex; justify-content: space-between; align-items: flex-start; gap: 12px; }
h1 { font-size: 16px; font-weight: 600; margin: 0; }
.sub { margin: 2px 0 0; color: var(--muted); font-size: 13px; }
.pill { flex: none; font-size: 12px; font-weight: 600; padding: 3px 10px; border-radius: 999px;
  background: var(--chip); }
.pill.live { color: var(--live); } .pill.bad { color: var(--bad); } .pill.muted { color: var(--muted); }
.pill.live::before { content: ""; display: inline-block; width: 6px; height: 6px; margin-right: 6px;
  border-radius: 50%; background: currentColor; vertical-align: middle; animation: pulse 1.6s infinite; }
@keyframes pulse { 50% { opacity: 0.3; } }
@media (prefers-reduced-motion: reduce) { .pill.live::before { animation: none; } }
p { margin: 0; }
.outcome { font-weight: 600; margin-bottom: 4px; }
dl { display: grid; grid-template-columns: minmax(90px, max-content) 1fr; gap: 4px 14px; margin: 0; }
dt { color: var(--muted); } dd { margin: 0; overflow-wrap: anywhere; }
h2 { font-size: 12px; font-weight: 600; color: var(--muted); text-transform: uppercase;
  letter-spacing: 0.04em; margin: 0 0 4px; }
ul { margin: 0; padding-left: 18px; }
.facts { display: grid; gap: 10px; border-top: 1px solid var(--line); padding-top: 12px; }
.fact { display: grid; gap: 4px; }
audio { width: 100%; height: 36px; }
details { border-top: 1px solid var(--line); padding-top: 10px; }
summary { cursor: pointer; color: var(--muted); }
details p { margin-top: 6px; }
.open { justify-self: start; border: 1px solid var(--line); background: none; color: var(--accent);
  border-radius: 8px; padding: 6px 12px; font: inherit; cursor: pointer; }
.note { color: var(--muted); font-size: 12px; }
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
    const section = node('section');
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
    const section = node('section');
    const items = node('ul');
    for (const item of group.items) items.append(node('li', null, item));
    section.append(node('h2', null, group.title), items);
    parts.push(section);
  }
  const facts = node('div', 'facts');
  if (view.cost) {
    const fact = node('div', 'fact');
    fact.append(node('h2', null, 'Cost'), node('p', null, view.cost));
    facts.append(fact);
  }
  if (view.recording) {
    const fact = node('div', 'fact');
    fact.append(node('h2', null, 'Recording' + (view.recording.length ? ' · ' + view.recording.length : '')));
    if (view.recording.url) {
      const audio = node('audio');
      audio.controls = true;
      audio.preload = 'none';
      audio.src = view.recording.url;
      fact.append(audio);
    } else {
      fact.append(node('p', 'note', 'Listen to it from the call in Smitline.'));
    }
    facts.append(fact);
  }
  if (facts.childElementCount) parts.push(facts);
  if (view.transcript.length) {
    const box = node('details');
    box.append(node('summary', null, 'Transcript · ' + view.transcript.length + (view.transcript.length === 1 ? ' line' : ' lines')));
    for (const line of view.transcript) {
      const said = node('p');
      said.append(node('strong', null, line.who + ': '), document.createTextNode(line.text));
      box.append(said);
    }
    parts.push(box);
  }
  if (view.callUrl) {
    const open = node('button', 'open', 'Open in Smitline');
    open.type = 'button';
    open.addEventListener('click', () => openLink(view.callUrl));
    parts.push(open);
  }
  if (view.live) parts.push(node('p', 'note', 'This card follows the call as it happens.'));
  main.replaceChildren(...parts);
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
  const args = { callId: call.id };
  try {
    const result = await ask('tools/call', { name: 'get_call', arguments: args });
    if (result && !result.isError) return show(result);
  } catch {
    if (window.openai && typeof window.openai.callTool === 'function') {
      try {
        const result = await window.openai.callTool('get_call', args);
        if (result && !result.isError) return show(result);
      } catch { /* try again below */ }
    }
  }
  follow();
}

function theme(context) {
  if (context && (context.theme === 'dark' || context.theme === 'light')) {
    document.documentElement.dataset.theme = context.theme;
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

if (window.openai && window.openai.toolOutput) show(window.openai.toolOutput);
window.addEventListener('openai:set_globals', () => {
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
    'openai/widgetDescription': 'A phone call or meeting placed through Smitline: how it went, what it cost, the recording and the transcript.',
  };
  return {
    descriptor: {
      uri: CALL_CARD_URI,
      name: 'call-card',
      title: 'Call card',
      description: 'Shows one call or meeting: status, result, cost, recording and transcript.',
      mimeType: CALL_CARD_MIME,
      _meta: meta,
    },
    content: { uri: CALL_CARD_URI, mimeType: CALL_CARD_MIME, text: callCardHtml(), _meta: meta },
  };
}
