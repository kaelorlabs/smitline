// Follow calls live from the local console: status, transcript, result, join, and end.
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

const $ = (id) => document.getElementById(id);
const state = {
  token: '',
  tokenReady: null,
  calls: [],
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

function shortName(call) {
  if (call.channel === 'meeting') return meetingName(brief(call).to);
  return `${call.direction === 'inbound' ? 'From ' : ''}${formatPhone(brief(call).to)}`;
}

function callTitle(call) {
  if (call.channel === 'meeting') return meetingName(brief(call).to);
  return `${call.direction === 'inbound' ? 'Call from' : 'Call to'} ${formatPhone(brief(call).to)}`;
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
  if (speaker === 'agent') return 'Colleague AI';
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
  const labels = { live: 'Connected', down: 'Colleague AI not running', offline: 'Console offline' };
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
    showBanner('Colleague AI isn’t running right now, so calls can’t be shown.',
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

// Call list ---------------------------------------------------------------------

const listItems = new Map();

function listItem(call) {
  let entry = listItems.get(call.id);
  if (!entry) {
    const item = document.createElement('li');
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'call-item';
    const who = Object.assign(document.createElement('span'), { className: 'who' });
    const goal = Object.assign(document.createElement('span'), { className: 'goal' });
    const row = Object.assign(document.createElement('span'), { className: 'row' });
    const chip = document.createElement('span');
    const time = document.createElement('time');
    row.append(chip, time);
    button.append(who, goal, row);
    button.addEventListener('click', () => openCall(call.id));
    item.append(button);
    entry = { item, button, who, goal, chip, time, signature: '' };
    listItems.set(call.id, entry);
  }
  const badge = listBadge(call);
  const signature = JSON.stringify([shortName(call), brief(call).objective, badge, call.createdAt, when(call.createdAt)]);
  if (signature !== entry.signature) {
    entry.signature = signature;
    entry.who.textContent = shortName(call);
    entry.goal.textContent = brief(call).objective || '';
    setChip(entry.chip, badge);
    entry.time.dateTime = call.createdAt || '';
    entry.time.textContent = when(call.createdAt);
  }
  entry.button.setAttribute('aria-current', String(call.id === state.selected));
  return entry.item;
}

// Update items in place so keyboard focus and scroll position survive each refresh.
function renderList() {
  const list = $('call-list');
  const ids = new Set(state.calls.map((call) => call.id));
  for (const [id, entry] of listItems) {
    if (!ids.has(id)) { entry.item.remove(); listItems.delete(id); }
  }
  state.calls.forEach((call, index) => {
    const item = listItem(call);
    if (list.children[index] !== item) list.insertBefore(item, list.children[index] || null);
  });
  $('list-loading').hidden = true;
  $('list-empty').hidden = state.calls.length > 0;
  if (state.loaded) {
    for (const call of state.calls) {
      if (!state.known.has(call.id)) announce(`New call: ${callTitle(call)}.`);
    }
  }
  state.known = ids;
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
  document.title = 'Calls · Colleague AI';
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
  document.title = `${title} · Calls · Colleague AI`;
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
  const recording = typeof call.line?.recordingUrl === 'string' ? call.line.recordingUrl : '';
  $('recording').hidden = !recording;
  $('recording-url').textContent = recording;
  $('result-pending').hidden = call.status !== 'summarizing';
  renderResult(call);
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
    const { calls = [] } = await api('/api/calls');
    state.calls = calls;
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
    return 'Colleague AI does not have your phone number yet. Ask your agent to set it (COLLEAGUE_OWNER_PHONE), then try again.';
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
    note('Press again to confirm. Colleague AI tells them it is connecting you, then rings your phone and hands you the call.');
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
  note(beforeAnswer ? 'Canceling the call.' : 'Asked Colleague AI to wrap up and hang up.');
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
