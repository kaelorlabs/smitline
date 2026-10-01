// Follow calls live from the local console: brief, status, transcript, result, cost, join, and end.
const TERMINAL = new Set(['completed', 'failed', 'canceled']);
const BEFORE_ANSWER = new Set(['queued', 'connecting', 'ringing', 'waiting']);
const LIVE_POLL_MS = 1500;
const IDLE_POLL_MS = 2500;
const CONFIRM_MS = 8000;
const NARROW = window.matchMedia('(max-width: 760px)');

const OUTCOMES = {
  achieved: { text: 'Achieved', tone: 'good', icon: '✓' },
  partial: { text: 'Partly achieved', tone: 'warn', icon: '½' },
  not_reached: { text: 'Not reached', tone: 'warn', icon: '–' },
  voicemail: { text: 'Left a voicemail', tone: 'warn', icon: '…' },
  declined: { text: 'Declined', tone: 'bad', icon: '✕' },
  failed: { text: 'Failed', tone: 'bad', icon: '!' },
  canceled: { text: 'Canceled', tone: 'neutral', icon: '–' },
};
const END_REASONS = {
  hangup: 'Ended normally', remote_hangup: 'They hung up', no_answer: 'No answer', busy: 'Line was busy',
  voicemail: 'Left a voicemail', max_duration: 'Time limit reached', canceled: 'Canceled',
  meeting_ended: 'Meeting ended', transferred: 'Handed to you', error: 'Something went wrong',
};
const JOIN_TEXT = 'Take over the call';
const PROVIDERS = { twilio: 'Twilio', signalwire: 'SignalWire' };
const COST_ITEMS = { phone: 'Phone line', voice: 'Voice', backend: 'Background model', summary: 'Summary' };
// Result filters, in the order they are offered.
const FILTER_ORDER = ['all', 'live', 'achieved', 'partial', 'not_reached', 'voicemail', 'declined', 'failed', 'canceled'];

const $ = (id) => document.getElementById(id);
const state = {
  token: '',
  tokenReady: null,
  calls: [],
  spend: null,
  filter: 'all',
  loaded: false,
  known: new Set(),
  selected: location.hash.slice(1) || null,
  autoPicked: Boolean(location.hash.slice(1)),
  call: null,
  lines: [],
  cursor: '',
  transferred: false,
  rendered: [],
  renderedFor: null,
  lastStatus: null,
  confirmJoin: false,
  confirmTimer: null,
  transferRequested: new Set(),
  endRequested: new Set(),
  timer: null,
  inFlight: false,
};

// Data --------------------------------------------------------------------------

async function api(path, options = {}) {
  let response;
  try {
    response = await fetch(path, {
      ...options,
      headers: { 'x-colleague-token': state.token, ...(options.body ? { 'Content-Type': 'application/json' } : {}) },
    });
  } catch {
    throw Object.assign(new Error('The console is not responding.'), { code: 'console_offline' });
  }
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    const message = payload.error?.message || payload.error || `Request failed (${response.status})`;
    throw Object.assign(new Error(String(message)), { status: response.status, code: payload.code || payload.error?.code });
  }
  return payload;
}

function loadToken() {
  state.tokenReady = api('/api/bootstrap').then((bootstrap) => { state.token = bootstrap.token || ''; }).catch(() => {});
  return state.tokenReady;
}

// Formatting ----------------------------------------------------------------------

const timeFormat = new Intl.DateTimeFormat([], { hour: 'numeric', minute: '2-digit' });
const dayFormat = new Intl.DateTimeFormat([], { month: 'short', day: 'numeric' });

function when(iso) {
  const date = new Date(iso || '');
  if (Number.isNaN(date.getTime())) return '';
  const today = new Date();
  const yesterday = new Date(today);
  yesterday.setDate(today.getDate() - 1);
  const day = date.toDateString() === today.toDateString() ? 'Today'
    : date.toDateString() === yesterday.toDateString() ? 'Yesterday' : dayFormat.format(date);
  return `${day}, ${timeFormat.format(date)}`;
}

function duration(seconds) {
  const total = Math.max(0, Math.round(seconds));
  if (total < 60) return `${total} s`;
  const minutes = Math.floor(total / 60);
  if (minutes < 60) return total % 60 ? `${minutes} min ${total % 60} s` : `${minutes} min`;
  return `${Math.floor(minutes / 60)} h ${minutes % 60} min`;
}

function callSeconds(call) {
  if (call.result?.durationSeconds) return call.result.durationSeconds;
  const answered = Date.parse(call.answeredAt || '');
  if (Number.isNaN(answered)) return null;
  const ended = call.endedAt ? Date.parse(call.endedAt) : Date.now();
  return Number.isNaN(ended) ? null : (ended - answered) / 1000;
}

function formatPhone(value) {
  const match = /^\+1(\d{3})(\d{3})(\d{4})$/.exec(value || '');
  return match ? `+1 ${match[1]} ${match[2]} ${match[3]}` : (value || 'Unknown number');
}

function meetingName(url) {
  try {
    const host = new URL(url).hostname.toLowerCase();
    if (host === 'zoom.us' || host.endsWith('.zoom.us')) return 'Zoom meeting';
    if (host === 'teams.microsoft.com' || host === 'teams.live.com') return 'Teams meeting';
    if (host === 'meet.google.com') return 'Google Meet';
  } catch { /* not a URL */ }
  return 'Meeting';
}

function brief(call) {
  return call.brief || {};
}

function contactName(call) {
  const name = brief(call).contact?.name;
  return typeof name === 'string' ? name.trim() : '';
}

function shortName(call) {
  if (call.channel === 'meeting') return meetingName(brief(call).to);
  return `${call.direction === 'inbound' ? 'From ' : ''}${contactName(call) || formatPhone(brief(call).to)}`;
}

function callTitle(call) {
  if (call.channel === 'meeting') return meetingName(brief(call).to);
  const number = formatPhone(brief(call).to);
  const name = contactName(call);
  return `${call.direction === 'inbound' ? 'Call from' : 'Call to'} ${name ? `${name} (${number})` : number}`;
}

const counter = new Intl.NumberFormat();

// Small amounts keep enough digits to be told apart: $0.0005, $0.017, $1.25.
function money(value) {
  const amount = Number(value) || 0;
  if (amount === 0) return '$0.00';
  return `$${amount.toFixed(amount < 0.01 ? 4 : amount < 1 ? 3 : 2)}`;
}

function localDay(date = new Date()) {
  return [date.getFullYear(), String(date.getMonth() + 1).padStart(2, '0'), String(date.getDate()).padStart(2, '0')].join('-');
}

function pricesDate(iso) {
  const date = new Date(`${iso}T12:00:00`);
  return Number.isNaN(date.getTime()) ? iso : date.toLocaleDateString(undefined, { month: 'long', day: 'numeric', year: 'numeric' });
}

function callCount(count) {
  return count === 1 ? '1 call' : `${counter.format(count)} calls`;
}

function resultKey(call) {
  if (!TERMINAL.has(call.status)) return 'live';
  return call.result?.outcome || call.status;
}

function resultLabel(key) {
  if (key === 'all') return 'All';
  if (key === 'live') return 'In progress';
  return OUTCOMES[key]?.text || capitalize(key.replace(/_/g, ' '));
}

function statusInfo(call) {
  const meeting = call.channel === 'meeting';
  const info = {
    queued: ['Starting', 'progress', true],
    connecting: [meeting ? 'Joining' : 'Dialing', 'progress', true],
    ringing: ['Ringing', 'progress', true],
    waiting: ['Waiting to be let in', 'progress', true],
    in_progress: [meeting ? 'In the meeting' : 'On the call', 'good', true],
    summarizing: ['Writing the result', 'progress', true],
    completed: ['Ended', 'neutral', false],
    failed: ['Failed', 'bad', false],
    canceled: ['Canceled', 'neutral', false],
  }[call.status] || [String(call.status || 'Unknown'), 'neutral', false];
  return { text: info[0], tone: info[1], live: info[2] };
}

// The list shows how a finished call turned out, and the live status otherwise.
function listBadge(call) {
  if (call.status === 'completed' && call.result?.outcome && OUTCOMES[call.result.outcome]) {
    const outcome = OUTCOMES[call.result.outcome];
    return { text: outcome.text, tone: outcome.tone, live: false };
  }
  return statusInfo(call);
}

function setChip(element, { text, tone, live }, extraClass = '') {
  element.className = `chip ${tone} ${extraClass}`.trim();
  const children = [];
  if (live) children.push(Object.assign(document.createElement('span'), { className: 'pulse' }));
  children.push(document.createTextNode(text));
  element.replaceChildren(...children);
}

function speakerName(speaker, call) {
  if (speaker === 'agent') return 'Smitline';
  if (speaker === 'meeting') return 'Meeting';
  return call.direction === 'inbound' ? 'Caller' : 'Other party';
}

function capitalize(text) {
  const value = String(text || '').trim();
  return value ? value[0].toUpperCase() + value.slice(1) : value;
}

function sentence(text) {
  const value = capitalize(text);
  return !value || /[.!?]$/.test(value) ? value : `${value}.`;
}

// Page state and messages -------------------------------------------------------------

function announce(text) {
  const region = $('announcer');
  region.textContent = '';
  setTimeout(() => { region.textContent = text; }, 60);
}

function setConnection(kind) {
  const labels = { live: 'Connected', down: 'Smitline not running', offline: 'Console offline' };
  const element = $('connection');
  element.classList.toggle('live', kind === 'live');
  element.classList.toggle('down', kind === 'offline');
  $('connection-label').textContent = labels[kind];
}

function showBanner(title, detail, tone) {
  const banner = $('banner');
  banner.className = `banner ${tone}`;
  const heading = Object.assign(document.createElement('strong'), { textContent: title });
  const body = Object.assign(document.createElement('span'), { textContent: detail });
  banner.replaceChildren(heading, body);
  banner.hidden = false;
}

function handleLoadError(error) {
  $('list-loading').hidden = true;
  // Keep what is already on screen; before the first load there is nothing to show.
  $('layout').hidden = !state.loaded;
  if (error.code === 'console_offline') {
    setConnection('offline');
    showBanner('The console stopped responding.', 'Start it again (./start-control-panel.sh), then reload this page.', 'bad');
  } else if (['daemon_offline', 'daemon_unavailable'].includes(error.code) || error.status === 503) {
    setConnection('down');
    showBanner('Smitline isn’t running right now, so calls can’t be shown.',
      'It starts by itself when your agent places a call. This page keeps checking.', 'info');
  } else {
    setConnection('down');
    showBanner('Couldn’t load calls.', `${sentence(error.message)} This page keeps trying.`, 'bad');
  }
}

function setMode() {
  const mode = state.loaded && !state.calls.length ? 'welcome' : (state.selected ? 'detail' : 'list');
  $('layout').dataset.mode = mode;
  document.body.dataset.mode = mode;
}

// Spend ---------------------------------------------------------------------------

function addUp(days) {
  return days.reduce((sum, day) => ({
    calls: sum.calls + day.calls,
    total: sum.total + day.total,
    phone: sum.phone + day.phone,
    openai: sum.openai + day.openai,
    estimated: sum.estimated || day.estimated,
  }), { calls: 0, total: 0, phone: 0, openai: 0, estimated: false });
}

function renderSpend(spend) {
  const days = spend?.days || [];
  $('spend').hidden = !days.length;
  if (!days.length) return;
  const today = localDay();
  const all = addUp(days);
  const figures = {
    today: addUp(days.filter((day) => day.day === today)),
    month: addUp(days.filter((day) => day.day.slice(0, 7) === today.slice(0, 7))),
    all,
  };
  for (const [name, sum] of Object.entries(figures)) {
    $(`spend-${name}`).textContent = `${sum.estimated ? '≈ ' : ''}${money(sum.total)}`;
    $(`spend-${name}-calls`).textContent = callCount(sum.calls);
  }
  $('spend-average').textContent = money(all.calls ? all.total / all.calls : 0);
  $('spend-phone').textContent = money(all.phone);
  $('spend-openai').textContent = money(all.openai);
  const share = all.total ? Math.round((all.phone / all.total) * 100) : 0;
  $('split-phone').style.width = `${share}%`;
  $('split-openai').style.width = `${all.total ? 100 - share : 0}%`;
  $('spend-note').textContent = 'Phone charges are what your phone provider billed for each call. '
    + `OpenAI charges are calculated from each call’s usage at OpenAI’s list prices as of ${pricesDate(spend.pricesAsOf)}; OpenAI does not bill per call.`
    + (all.estimated ? ' ≈ means a phone charge is still an estimate because the provider has not reported its price yet.' : '');
}

// Call list ---------------------------------------------------------------------

const listItems = new Map();

function listItem(call) {
  let entry = listItems.get(call.id);
  if (!entry) {
    const item = document.createElement('li');
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'call-item';
    const top = Object.assign(document.createElement('span'), { className: 'top' });
    const who = Object.assign(document.createElement('span'), { className: 'who' });
    const price = Object.assign(document.createElement('span'), { className: 'price' });
    const goal = Object.assign(document.createElement('span'), { className: 'goal' });
    const row = Object.assign(document.createElement('span'), { className: 'row' });
    const chip = document.createElement('span');
    const time = document.createElement('time');
    top.append(who, price);
    row.append(chip, time);
    button.append(top, goal, row);
    button.addEventListener('click', () => openCall(call.id));
    item.append(button);
    entry = { item, button, who, price, goal, chip, time, signature: '' };
    listItems.set(call.id, entry);
  }
  const badge = listBadge(call);
  const cost = call.cost ? `${call.cost.estimated ? '≈ ' : ''}${money(call.cost.total)}` : '';
  const signature = JSON.stringify([shortName(call), brief(call).objective, badge, call.createdAt, when(call.createdAt), cost]);
  if (signature !== entry.signature) {
    entry.signature = signature;
    entry.who.textContent = shortName(call);
    entry.price.textContent = cost;
    entry.price.title = cost ? 'What this call cost' : '';
    entry.goal.textContent = brief(call).objective || '';
    setChip(entry.chip, badge);
    entry.time.dateTime = call.createdAt || '';
    entry.time.textContent = when(call.createdAt);
  }
  entry.button.setAttribute('aria-current', String(call.id === state.selected));
  return entry.item;
}

const filterButtons = new Map();

// Offer a filter for each result present; buttons update in place so focus stays put.
function renderFilters() {
  const counts = new Map([['all', state.calls.length]]);
  for (const call of state.calls) counts.set(resultKey(call), (counts.get(resultKey(call)) || 0) + 1);
  if (!counts.has(state.filter)) state.filter = 'all';
  const keys = [...counts.keys()].sort((a, b) => {
    const rank = (key) => (FILTER_ORDER.includes(key) ? FILTER_ORDER.indexOf(key) : FILTER_ORDER.length);
    return rank(a) - rank(b);
  });
  const container = $('filters');
  container.hidden = keys.length < 3; // "All" plus at least two different results
  if (keys.join() !== [...filterButtons.keys()].join()) {
    filterButtons.clear();
    container.replaceChildren(...keys.map((key) => {
      const button = Object.assign(document.createElement('button'), { type: 'button', className: 'filter' });
      const count = Object.assign(document.createElement('span'), { className: 'count' });
      button.append(resultLabel(key), count);
      button.addEventListener('click', () => {
        state.filter = key;
        renderList();
      });
      filterButtons.set(key, { button, count });
      return button;
    }));
  }
  for (const [key, { button, count }] of filterButtons) {
    button.setAttribute('aria-pressed', String(key === state.filter));
    count.textContent = counter.format(counts.get(key) || 0);
  }
}

// Update items in place so keyboard focus and scroll position survive each refresh.
function renderList() {
  renderFilters();
  const list = $('call-list');
  const shown = state.calls.filter((call) => state.filter === 'all' || resultKey(call) === state.filter);
  const ids = new Set(shown.map((call) => call.id));
  for (const [id, entry] of listItems) {
    if (!ids.has(id)) { entry.item.remove(); listItems.delete(id); }
  }
  shown.forEach((call, index) => {
    const item = listItem(call);
    if (list.children[index] !== item) list.insertBefore(item, list.children[index] || null);
  });
  $('list-loading').hidden = true;
  $('list-empty').hidden = state.calls.length > 0;
  $('list-filtered').hidden = !state.calls.length || shown.length > 0;
  if (state.loaded) {
    for (const call of state.calls) {
      if (!state.known.has(call.id)) announce(`New call: ${callTitle(call)}.`);
    }
  }
  state.known = new Set(state.calls.map((call) => call.id));
}

// Selected call -------------------------------------------------------------------

function resetDetail() {
  cancelConfirm();
  state.call = null;
  state.lines = [];
  state.cursor = '';
  state.transferred = false;
  state.renderedFor = null;
  state.rendered = [];
  state.lastStatus = null;
  $('action-note').textContent = '';
  $('jump-button').hidden = true;
}

function select(callId) {
  if (callId !== state.selected) resetDetail();
  state.selected = callId;
  setMode();
  renderList();
}

function openCall(callId) {
  if (callId === state.selected && state.call) {
    if (NARROW.matches) $('call-title').focus();
    return;
  }
  if (NARROW.matches) history.pushState({ fromList: true }, '', `#${callId}`);
  else history.replaceState(null, '', `#${callId}`);
  select(callId);
  $('call').hidden = true;
  $('pick').hidden = true;
  refresh({ focusTitle: NARROW.matches });
}

function showList() {
  const previous = state.selected;
  resetDetail();
  state.selected = null;
  history.replaceState(null, '', location.pathname);
  setMode();
  renderList();
  renderEmptyDetail();
  listItems.get(previous)?.button.focus();
}

function syncFromHash() {
  const id = location.hash.slice(1) || null;
  if (id === state.selected) return;
  if (id) {
    select(id);
    refresh();
  } else {
    showList();
  }
}

function renderEmptyDetail() {
  const empty = state.loaded && !state.calls.length;
  $('welcome').hidden = !empty;
  $('pick').hidden = empty || !state.loaded;
  $('call').hidden = true;
  document.title = 'Calls · Smitline';
}

function renderMeta(call) {
  const parts = [];
  if (brief(call).onBehalfOf) parts.push(`For ${brief(call).onBehalfOf}`);
  if (call.createdAt) parts.push(when(call.createdAt));
  const seconds = callSeconds(call);
  if (seconds !== null && (TERMINAL.has(call.status) || call.status === 'summarizing')) parts.push(duration(seconds));
  else if (seconds !== null) parts.push(`Connected for ${duration(seconds)}`);
  if (TERMINAL.has(call.status) && call.endReason) parts.push(END_REASONS[call.endReason] || call.endReason.replace(/_/g, ' '));
  $('call-meta').textContent = parts.join(' · ');
}

function renderActions(call) {
  const live = !TERMINAL.has(call.status) && call.status !== 'summarizing';
  $('live-panel').hidden = !live;
  if (!live) { cancelConfirm(); return; }
  const canJoin = call.channel === 'phone' && call.status === 'in_progress';
  const join = $('join-button');
  join.hidden = !canJoin;
  $('join-help').hidden = !canJoin;
  if (canJoin) {
    const requested = state.transferRequested.has(call.id);
    join.disabled = requested;
    join.classList.toggle('confirming', state.confirmJoin && !requested);
    join.textContent = requested ? 'Ringing your phone…' : (state.confirmJoin ? 'Ring my phone now' : JOIN_TEXT);
  } else {
    cancelConfirm();
  }
  const end = $('end-button');
  const ending = state.endRequested.has(call.id);
  end.disabled = ending;
  end.textContent = ending ? 'Ending…' : (BEFORE_ANSWER.has(call.status) ? 'Cancel call' : 'End call');
}

function fillList(containerId, items) {
  const container = $(containerId);
  container.hidden = !items?.length;
  container.querySelector('ul').replaceChildren(...(items || []).map((text) => Object.assign(document.createElement('li'), { textContent: text })));
}

function renderResult(call) {
  const result = call.result;
  $('result').hidden = !result;
  if (!result) return;
  const outcome = OUTCOMES[result.outcome] || { text: capitalize(result.outcome || 'Unknown'), tone: 'neutral', icon: '•' };
  $('result-outcome-box').className = `outcome callout ${outcome.tone}`;
  $('result-icon').textContent = outcome.icon;
  $('result-outcome').textContent = outcome.text;
  $('result-summary').textContent = result.summary || '';
  $('result-summary').hidden = !result.summary;
  $('result-details').replaceChildren(...(result.details || []).flatMap((item) => [
    Object.assign(document.createElement('dt'), { textContent: item.label }),
    Object.assign(document.createElement('dd'), { textContent: item.value }),
  ]));
  fillList('result-questions', result.openQuestions);
  fillList('result-actions', result.actionItems);
  fillList('result-decisions', result.decisions);
}

function bulletList(items) {
  const list = document.createElement('ul');
  list.append(...items.map((text) => Object.assign(document.createElement('li'), { textContent: text })));
  return list;
}

function textBlock(text, className = '') {
  return Object.assign(document.createElement('p'), { textContent: text, className });
}

function listed(value) {
  return Array.isArray(value) ? value.filter((item) => typeof item === 'string' && item.trim()) : [];
}

// The brief's background: plain text, or the structured object (summary, facts, decisions, ...).
function backgroundNodes(context) {
  if (!context) return [];
  if (typeof context === 'string') return [textBlock(context)];
  const nodes = [];
  if (context.summary) nodes.push(textBlock(context.summary));
  for (const [key, label] of [['facts', ''], ['decisions', 'Already decided'], ['openQuestions', 'Still open']]) {
    const items = listed(context[key]);
    if (!items.length) continue;
    if (label) nodes.push(textBlock(label, 'sub'));
    nodes.push(bulletList(items));
  }
  if (context.details) {
    const more = document.createElement('details');
    more.append(Object.assign(document.createElement('summary'), { textContent: 'More background' }),
      textBlock(context.details, 'more'));
    nodes.push(more);
  }
  return nodes;
}

function renderBrief(call) {
  const section = $('brief');
  if (section.dataset.for === call.id) return;
  section.dataset.for = call.id;
  const b = brief(call);
  const rows = [];
  const add = (label, ...nodes) => {
    const content = nodes.filter(Boolean);
    if (content.length) rows.push([label, content]);
  };
  const contact = b.contact || {};
  if (contact.name) {
    const who = [contact.name, contact.relationship].filter(Boolean).join(', ');
    add('Speaking with', textBlock(who), contact.notes && textBlock(contact.notes, 'muted'));
  }
  if (b.successCriteria) add('Done when', textBlock(b.successCriteria));
  if (listed(b.questions).length) add('Questions to ask', bulletList(listed(b.questions)));
  if (b.tone) add('Tone', textBlock(capitalize(b.tone)));
  add('Background', ...backgroundNodes(b.context));
  if (listed(b.mayAgreeTo).length) add('Can agree to', bulletList(listed(b.mayAgreeTo)));
  if (listed(b.mustNotShare).length) add('Must not share', bulletList(listed(b.mustNotShare)));
  const limits = [b.maxMinutes ? `${b.maxMinutes} min at most` : '', b.voice ? `voice ${b.voice}` : '', b.language ? `language ${b.language}` : ''].filter(Boolean);
  if (limits.length) add('Call settings', textBlock(capitalize(limits.join(' · '))));
  if (b.rehearsal) add('Practice', textBlock('A rehearsal: it called your own phone.'));
  $('brief-fields').replaceChildren(...rows.flatMap(([label, nodes]) => {
    const dd = document.createElement('dd');
    dd.append(...nodes);
    return [Object.assign(document.createElement('dt'), { textContent: label }), dd];
  }));
  section.hidden = rows.length === 0;
}

function rate(value) {
  return `$${Number(value) || 0}/min`;
}

function tokenUsage(item) {
  const parts = [`${counter.format(item.input || 0)} in`];
  if (item.cached) parts[0] += ` (${counter.format(item.cached)} cached)`;
  parts.push(`${counter.format(item.output || 0)} out tokens`);
  if (item.webSearches) parts.push(item.webSearches === 1 ? '1 web search' : `${item.webSearches} web searches`);
  return parts.join(' · ');
}

function costRow(item) {
  const provider = PROVIDERS[item.provider] || 'your phone provider';
  let usage = '';
  if (item.kind === 'phone') {
    usage = item.source === 'provider'
      ? `${duration(item.seconds || 0)} · billed by ${provider}`
      : `${duration(item.seconds || 0)} · estimated at ${rate(item.ratePerMinute)} until ${provider} reports its price`;
  } else if (item.kind === 'voice') {
    usage = `${duration(item.seconds || 0)} at ${rate(item.ratePerMinute)}`;
  } else {
    usage = tokenUsage(item);
  }
  const row = document.createElement('tr');
  const name = Object.assign(document.createElement('th'), { scope: 'row', textContent: item.kind === 'phone' && PROVIDERS[item.provider] ? `${COST_ITEMS.phone} (${provider})` : (COST_ITEMS[item.kind] || capitalize(item.kind)) });
  if (item.model) name.append(Object.assign(document.createElement('span'), { className: 'model', textContent: item.model }));
  const amount = item.amount === null || item.amount === undefined ? 'Price unknown'
    : `${item.source === 'estimate' ? '≈ ' : ''}${money(item.amount)}`;
  row.append(name,
    Object.assign(document.createElement('td'), { className: 'usage', textContent: usage }),
    Object.assign(document.createElement('td'), { className: 'num', textContent: amount }));
  return row;
}

function renderCost(call) {
  const cost = call.cost;
  $('cost').hidden = !cost;
  if (!cost) return;
  const total = `${cost.estimated ? '≈ ' : ''}${money(cost.total)}`;
  $('cost-total').textContent = total;
  $('cost-sum').textContent = total;
  const charged = Boolean(cost.items?.length);
  $('cost-rows').replaceChildren(...(cost.items || []).map(costRow));
  $('cost').querySelector('.table-wrap').hidden = !charged;
  if (!charged) {
    $('cost-note').textContent = 'Nothing was charged: the call never connected.';
    return;
  }
  const notes = [`OpenAI amounts are calculated from usage at list prices as of ${pricesDate(cost.pricesAsOf)}.`];
  if (cost.unpriced?.length) notes.push(`No price is known for ${cost.unpriced.join(', ')}, so it is not in the total.`);
  $('cost-note').textContent = notes.join(' ');
}

function lineItem(line, call) {
  const item = document.createElement('li');
  item.className = line.speaker === 'agent' ? 'agent' : 'other';
  const who = Object.assign(document.createElement('span'), { className: 'speaker', textContent: speakerName(line.speaker, call) });
  const colon = Object.assign(document.createElement('span'), { className: 'visually-hidden', textContent: ': ' });
  const text = Object.assign(document.createElement('span'), { className: 'text', textContent: line.text });
  item.append(who, colon, text);
  return item;
}

function sameLine(a, b) {
  return a && b && a.speaker === b.speaker && a.text === b.text;
}

function emptyTranscriptText(call) {
  if (BEFORE_ANSWER.has(call.status)) return 'The transcript starts when someone answers.';
  if (!TERMINAL.has(call.status)) return 'Listening. Lines appear here as people talk.';
  if (['no_answer', 'busy'].includes(call.endReason)) return 'Nobody answered, so nothing was said.';
  return 'Nothing was said on this call.';
}

function renderTranscript(call) {
  const list = $('transcript');
  const lines = call.result?.transcript?.length ? call.result.transcript : state.lines;
  const live = !TERMINAL.has(call.status);
  const fresh = state.renderedFor !== call.id;
  // Announce only lines added while you follow a live call, never the whole transcript.
  if (fresh || !live) list.setAttribute('aria-live', 'off');
  const nearBottom = list.scrollHeight - list.scrollTop - list.clientHeight < 48;
  const extends_ = !fresh && state.rendered.length <= lines.length && state.rendered.every((line, index) => sameLine(line, lines[index]));
  let added = 0;
  if (!extends_) {
    list.replaceChildren(...lines.map((line) => lineItem(line, call)));
  } else if (lines.length > state.rendered.length) {
    const fresher = lines.slice(state.rendered.length);
    list.append(...fresher.map((line) => lineItem(line, call)));
    added = fresher.length;
  }
  state.rendered = lines.slice();
  state.renderedFor = call.id;
  $('transcript-empty').textContent = lines.length ? '' : emptyTranscriptText(call);
  $('transcript-empty').hidden = lines.length > 0;
  list.hidden = lines.length === 0;
  $('transcript-hint').textContent = live && call.status !== 'summarizing'
    ? 'Updates as people talk'
    : (lines.length ? `${lines.length} ${lines.length === 1 ? 'line' : 'lines'}` : '');
  if (fresh) {
    list.scrollTop = live ? list.scrollHeight : 0;
    if (live) setTimeout(() => { if (state.renderedFor === call.id && !TERMINAL.has(state.call?.status)) list.setAttribute('aria-live', 'polite'); }, 1000);
  } else if (added && nearBottom) {
    list.scrollTop = list.scrollHeight;
  } else if (added) {
    const jump = $('jump-button');
    const waiting = Number(jump.dataset.count || 0) + added;
    jump.dataset.count = String(waiting);
    jump.textContent = `${waiting} new ${waiting === 1 ? 'line' : 'lines'} below ↓`;
    jump.hidden = false;
  }
}

function renderCall(call, { focusTitle = false } = {}) {
  const fresh = state.renderedFor !== call.id;
  $('welcome').hidden = true;
  $('pick').hidden = true;
  $('call').hidden = false;
  const title = callTitle(call);
  $('call-title').textContent = title;
  document.title = `${title} · Calls · Smitline`;
  renderMeta(call);
  const status = statusInfo(call);
  setChip($('call-status'), status);
  $('call-objective').textContent = brief(call).objective || 'No goal was given.';
  renderActions(call);

  // A failed call with a result explains itself there; without one, say what went wrong here.
  const failed = call.status === 'failed' && !call.result;
  $('problem').hidden = !failed;
  $('problem-title').textContent = call.answeredAt ? 'This call ended because of a problem' : 'This call did not go through';
  $('problem-text').textContent = failed ? sentence(call.error || 'Something went wrong before the call could start.') : '';
  $('handed-over').hidden = !(state.transferred || call.endReason === 'transferred');
  // The recording stays in the phone account; the console fetches it with the account's keys.
  const recording = call.recording && typeof call.recording.url === 'string' ? call.recording : null;
  $('recording').hidden = !recording;
  $('recording-text').textContent = recording
    ? [Number.isFinite(recording.seconds) ? duration(recording.seconds) : '', 'kept in your phone account; the WAV has each side on its own channel'].filter(Boolean).join(' · ')
    : '';
  for (const format of ['wav', 'mp3']) {
    $(`recording-${format}`).href = recording ? `/api/calls/${encodeURIComponent(call.id)}/recording?format=${format}` : '';
  }
  $('result-pending').hidden = call.status !== 'summarizing';
  renderBrief(call);
  renderResult(call);
  renderCost(call);
  renderTranscript(call);

  if (!fresh && state.lastStatus && state.lastStatus !== call.status) {
    const outcome = call.result && OUTCOMES[call.result.outcome];
    announce(outcome && TERMINAL.has(call.status)
      ? `Call ended. Result: ${outcome.text}. ${call.result.summary || ''}`
      : `${status.text}.`);
  }
  state.lastStatus = call.status;
  if (focusTitle) $('call-title').focus();
}

// Refresh loop ---------------------------------------------------------------------

async function refreshDetail(options) {
  const target = state.selected;
  if (state.call?.id === target && TERMINAL.has(state.call.status)) {
    const fromList = state.calls.find((call) => call.id === target);
    if (fromList?.cost && JSON.stringify(fromList.cost) !== JSON.stringify(state.call.cost)) {
      state.call = { ...state.call, cost: fromList.cost };
      renderCost(state.call);
    }
    renderMeta(state.call);
    return;
  }
  const query = state.cursor ? `?after=${encodeURIComponent(state.cursor)}` : '';
  let call;
  let events;
  try {
    [call, { events = [] }] = await Promise.all([api(`/api/calls/${target}`), api(`/api/calls/${target}/events${query}`)]);
  } catch (error) {
    if (error.status === 404 && target === state.selected) {
      showList();
      $('pick').textContent = 'That call is no longer available. Choose another one.';
      return;
    }
    throw error;
  }
  if (target !== state.selected) return; // another call was chosen meanwhile
  for (const event of events) {
    if (state.cursor && Number(event.id) <= Number(state.cursor)) continue;
    state.cursor = event.id;
    if (event.type === 'call.transcript' && event.data?.text) state.lines.push({ speaker: event.data.speaker, text: event.data.text });
    if (event.type === 'call.transferred') state.transferred = true;
  }
  if (!TERMINAL.has(call.status) && call.status !== 'in_progress') state.transferRequested.delete(call.id);
  state.call = call;
  renderCall(call, options);
}

async function refresh(options = {}) {
  if (state.inFlight) return;
  state.inFlight = true;
  try {
    // Spend is grouped by the reader's local day.
    const { calls = [], spend = null } = await api(`/api/calls?tzOffset=${-new Date().getTimezoneOffset()}`);
    state.calls = calls;
    state.spend = spend;
    renderSpend(spend);
    if (!state.autoPicked) {
      state.autoPicked = true;
      // On a wide screen, open the call most worth watching: a live one, else the latest.
      const pick = calls.find((call) => !TERMINAL.has(call.status)) || calls[0];
      if (pick && !NARROW.matches) {
        history.replaceState(null, '', `#${pick.id}`);
        select(pick.id);
      }
    }
    renderList();
    state.loaded = true;
    setMode();
    setConnection('live');
    $('banner').hidden = true;
    $('layout').hidden = false;
    if (state.selected) await refreshDetail(options);
    else renderEmptyDetail();
  } catch (error) {
    handleLoadError(error);
  } finally {
    state.inFlight = false;
  }
}

function schedule() {
  clearTimeout(state.timer);
  const live = state.calls.some((call) => !TERMINAL.has(call.status));
  state.timer = setTimeout(async () => {
    if (!document.hidden) await refresh();
    schedule();
  }, live ? LIVE_POLL_MS : IDLE_POLL_MS);
}

// Actions -------------------------------------------------------------------------

function note(text, isError = false) {
  const element = $('action-note');
  element.classList.toggle('error', isError);
  element.textContent = text;
}

function cancelConfirm() {
  clearTimeout(state.confirmTimer);
  if (!state.confirmJoin) return;
  state.confirmJoin = false;
  const join = $('join-button');
  join.classList.remove('confirming');
  if (!join.disabled) join.textContent = JOIN_TEXT;
  if ($('action-note').dataset.kind === 'confirm') note('');
}

function actionError(error) {
  if (/COLLEAGUE_OWNER_PHONE/.test(error.message)) {
    return 'Smitline does not have your phone number yet. Ask your agent to set it (COLLEAGUE_OWNER_PHONE), then try again.';
  }
  if (error.status === 403) return 'This page is out of date. Reload it and try again.';
  if (error.code === 'console_offline') return 'The console is not responding. Reload the page and try again.';
  return `That didn’t work: ${sentence(error.message)}`;
}

async function withToken() {
  if (!state.token) await (state.tokenReady || loadToken());
  if (!state.token) await loadToken();
}

$('join-button').addEventListener('click', async () => {
  const callId = state.selected;
  if (!callId || !state.call) return;
  if (!state.confirmJoin) {
    state.confirmJoin = true;
    renderActions(state.call);
    note('Press again to confirm. Smitline tells them it is connecting you, then rings your phone and hands you the call.');
    $('action-note').dataset.kind = 'confirm';
    state.confirmTimer = setTimeout(cancelConfirm, CONFIRM_MS);
    return;
  }
  cancelConfirm();
  state.transferRequested.add(callId);
  renderActions(state.call);
  $('action-note').dataset.kind = '';
  note('Connecting you. Your phone will ring in a few seconds.');
  try {
    await withToken();
    await api(`/api/calls/${callId}/transfer`, { method: 'POST', body: '{}' });
  } catch (error) {
    state.transferRequested.delete(callId);
    note(actionError(error), true);
  }
  if (state.call?.id === callId) renderActions(state.call);
});

$('join-button').addEventListener('keydown', (event) => {
  if (event.key === 'Escape') cancelConfirm();
});

$('end-button').addEventListener('click', async () => {
  const callId = state.selected;
  if (!callId || !state.call) return;
  const beforeAnswer = BEFORE_ANSWER.has(state.call.status);
  cancelConfirm();
  state.endRequested.add(callId);
  renderActions(state.call);
  $('action-note').dataset.kind = '';
  note(beforeAnswer ? 'Canceling the call.' : 'Asked Smitline to wrap up and hang up.');
  try {
    await withToken();
    await api(`/api/calls/${callId}/end`, { method: 'POST', body: '{}' });
    // Wrapping up takes a few seconds; if the call is still going after a while, allow another try.
    setTimeout(() => {
      if (!state.endRequested.delete(callId) || state.call?.id !== callId) return;
      if (TERMINAL.has(state.call.status) || state.call.status === 'summarizing') return;
      note('The call is still going. Press End call again to retry.');
      renderActions(state.call);
    }, 20_000);
  } catch (error) {
    state.endRequested.delete(callId);
    note(actionError(error), true);
  }
  if (state.call?.id === callId) renderActions(state.call);
});

$('back-button').addEventListener('click', () => {
  if (history.state?.fromList) history.back();
  else showList();
});

$('jump-button').addEventListener('click', () => {
  const list = $('transcript');
  list.scrollTop = list.scrollHeight;
  $('jump-button').hidden = true;
  $('jump-button').dataset.count = '0';
  list.focus();
});

$('transcript').addEventListener('scroll', () => {
  const list = $('transcript');
  if (list.scrollHeight - list.scrollTop - list.clientHeight < 48) {
    $('jump-button').hidden = true;
    $('jump-button').dataset.count = '0';
  }
});

window.addEventListener('popstate', syncFromHash);
window.addEventListener('hashchange', syncFromHash);
NARROW.addEventListener('change', setMode);
document.addEventListener('visibilitychange', () => {
  if (!document.hidden) refresh().then(schedule);
});

loadToken();
refresh().then(schedule);
