// Follow calls live from the local console: transcript, result, join, and end.
const TERMINAL = new Set(['completed', 'failed', 'canceled']);
const STATUS_TEXT = {
  queued: 'Queued', connecting: 'Dialing', ringing: 'Ringing', waiting: 'Waiting to be admitted',
  in_progress: 'Connected', summarizing: 'Writing the result', completed: 'Completed',
  failed: 'Failed', canceled: 'Canceled',
};
const OUTCOME_TEXT = {
  achieved: 'Achieved', partial: 'Partly achieved', not_reached: 'Not reached', voicemail: 'Voicemail',
  declined: 'Declined', failed: 'Failed', canceled: 'Canceled',
};

let token = '';
let selected = location.hash.slice(1) || null;
let confirmJoin = false;
const $ = (id) => document.getElementById(id);

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: { 'x-colleague-token': token, ...(options.body ? { 'Content-Type': 'application/json' } : {}) },
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.error?.message || payload.error || `Request failed (${response.status})`);
  return payload;
}

function statusTone(status) {
  if (status === 'completed') return 'good';
  if (status === 'failed' || status === 'canceled') return 'bad';
  if (status === 'in_progress') return 'good';
  return 'warn';
}

function outcomeTone(outcome) {
  if (outcome === 'achieved') return 'good';
  if (['failed', 'declined', 'canceled'].includes(outcome)) return 'bad';
  return 'warn';
}

function when(iso) {
  if (!iso) return '';
  return new Date(iso).toLocaleString([], { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' });
}

function renderList(calls) {
  const list = $('call-list');
  list.replaceChildren(...calls.map((call) => {
    const item = document.createElement('li');
    const button = document.createElement('button');
    button.type = 'button';
    button.setAttribute('aria-current', String(call.id === selected));
    const title = document.createElement('strong');
    title.textContent = call.channel === 'phone' ? call.brief.to : 'Meeting';
    const meta = document.createElement('small');
    meta.textContent = `${STATUS_TEXT[call.status] || call.status} · ${when(call.createdAt)}`;
    button.append(title, meta);
    button.addEventListener('click', () => select(call.id));
    item.append(button);
    return item;
  }));
  $('list-empty').hidden = calls.length > 0;
}

function renderResult(result) {
  $('result').hidden = !result;
  if (!result) return;
  const outcome = $('result-outcome');
  outcome.textContent = OUTCOME_TEXT[result.outcome] || result.outcome;
  outcome.className = `chip ${outcomeTone(result.outcome)}`;
  $('result-summary').textContent = result.summary || '';
  const details = $('result-details');
  details.replaceChildren(...(result.details || []).flatMap((item) => {
    const dt = document.createElement('dt');
    dt.textContent = item.label;
    const dd = document.createElement('dd');
    dd.textContent = item.value;
    return [dt, dd];
  }));
  const lists = $('result-lists');
  lists.replaceChildren(...[
    ['Decisions', result.decisions], ['Action items', result.actionItems], ['Open questions', result.openQuestions],
  ].filter(([, items]) => items?.length).map(([label, items]) => {
    const wrap = document.createElement('div');
    const heading = document.createElement('strong');
    heading.textContent = label;
    const ul = document.createElement('ul');
    ul.replaceChildren(...items.map((text) => Object.assign(document.createElement('li'), { textContent: text })));
    wrap.append(heading, ul);
    return wrap;
  }));
}

function renderTranscript(lines) {
  const list = $('transcript');
  const atBottom = list.scrollHeight - list.scrollTop - list.clientHeight < 40;
  list.replaceChildren(...lines.map((line) => {
    const item = document.createElement('li');
    item.className = line.speaker === 'agent' ? 'agent' : 'other';
    const who = Object.assign(document.createElement('span'), {
      className: 'speaker', textContent: line.speaker === 'agent' ? 'Colleague AI' : 'Other party',
    });
    const text = Object.assign(document.createElement('span'), { textContent: line.text });
    item.append(who, text);
    return item;
  }));
  $('transcript-empty').hidden = lines.length > 0;
  if (atBottom) list.scrollTop = list.scrollHeight;
}

function renderCall(call, liveLines) {
  $('detail-empty').hidden = true;
  $('call').hidden = false;
  $('call-title').textContent = call.channel === 'phone' ? `Call to ${call.brief.to}` : 'Meeting';
  $('call-objective').textContent = call.brief.objective;
  const status = $('call-status');
  status.textContent = `${STATUS_TEXT[call.status] || call.status}${call.endReason ? ` · ${call.endReason.replace('_', ' ')}` : ''}`;
  status.className = `chip ${statusTone(call.status)}`;
  const live = !TERMINAL.has(call.status) && call.status !== 'summarizing';
  $('actions').hidden = !live;
  $('join-button').hidden = !(call.channel === 'phone' && call.status === 'in_progress');
  $('join-button').textContent = confirmJoin ? 'Press again to ring your phone' : 'Join the call';
  renderResult(call.result);
  renderTranscript(call.result?.transcript?.length ? call.result.transcript : liveLines);
}

let liveLines = [];
let cursor = '';

async function select(callId) {
  selected = callId;
  confirmJoin = false;
  liveLines = [];
  cursor = '';
  $('action-note').textContent = '';
  history.replaceState(null, '', `#${callId}`);
  await refresh();
}

async function refresh() {
  try {
    const { calls } = await api('/api/calls');
    renderList(calls);
    const target = selected;
    if (!target) return;
    // Live transcript lines arrive as call.transcript events; the result holds the final transcript.
    const { events } = await api(`/api/calls/${target}/events${cursor ? `?after=${cursor}` : ''}`);
    const call = await api(`/api/calls/${target}`);
    if (target !== selected) return; // the user picked another call meanwhile
    for (const event of events) {
      if (cursor && Number(event.id) <= Number(cursor)) continue;
      cursor = event.id;
      if (event.type === 'call.transcript') liveLines.push({ speaker: event.data.speaker, text: event.data.text });
    }
    renderCall(call, liveLines);
  } catch (error) {
    $('action-note').textContent = error.message;
  }
}

$('join-button').addEventListener('click', async () => {
  if (!confirmJoin) {
    confirmJoin = true;
    $('join-button').textContent = 'Press again to ring your phone';
    return;
  }
  confirmJoin = false;
  $('join-button').disabled = true;
  try {
    await api(`/api/calls/${selected}/transfer`, { method: 'POST', body: '{}' });
    $('action-note').textContent = 'Connecting you. Your phone will ring.';
  } catch (error) {
    $('action-note').textContent = error.message;
  } finally {
    $('join-button').disabled = false;
  }
});

$('end-button').addEventListener('click', async () => {
  $('end-button').disabled = true;
  try {
    await api(`/api/calls/${selected}/end`, { method: 'POST', body: '{}' });
    $('action-note').textContent = 'Wrapping up the call.';
  } catch (error) {
    $('action-note').textContent = error.message;
  } finally {
    $('end-button').disabled = false;
  }
});

async function start() {
  const bootstrap = await fetch('/api/bootstrap').then((r) => r.json());
  token = bootstrap.token;
  await refresh();
  setInterval(refresh, 1500);
}

start();
